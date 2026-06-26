const fs = require("fs");
const path = require("path");
const { TextDecoder } = require("util");

const ROOT = __dirname;
const INPUT = path.join(ROOT, "comments.csv");
const OUTPUT_TMP = path.join(ROOT, "comments_ru_full.csv");
const OUTPUT_MAIN = path.join(ROOT, "comments_ru.csv");
const CACHE_FILE = path.join(ROOT, "retranslate_plc_cache.json");
const LOG_FILE = path.join(ROOT, "retranslate_plc.log");

const BASE_URL = process.env.CSVTRANS_BASE_URL || "http://127.0.0.1:8080/v1";
const MODEL = process.env.CSVTRANS_MODEL || "Qwen3.6-35B-A3B-UD-Q3_K_M.gguf";
const API_KEY = process.env.CSVTRANS_API_KEY || "not-needed";
const BATCH_SIZE = Number.parseInt(process.env.CSVTRANS_BATCH_SIZE || "64", 10);
const MAX_RETRIES = 2;
const HAN_RE = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/;

function now() {
  return new Date().toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

function log(message) {
  const line = `[${now()}] ${message}`;
  console.log(line);
  fs.appendFileSync(LOG_FILE, `${line}\n`, "utf8");
}

function parseCsv(text, delimiter = ",") {
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  const rows = [];
  let row = [];
  let field = "";
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    const next = text[i + 1];

    if (char === "\"") {
      if (inQuotes && next === "\"") {
        field += "\"";
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
      continue;
    }

    if (char === delimiter && !inQuotes) {
      row.push(field);
      field = "";
      continue;
    }

    if ((char === "\n" || char === "\r") && !inQuotes) {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
      if (char === "\r" && next === "\n") i += 1;
      continue;
    }

    field += char;
  }

  if (field.length || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

function escapeCsv(value, delimiter = ",") {
  const text = value == null ? "" : String(value);
  const mustQuote = text.includes("\"") || text.includes("\n") ||
    text.includes("\r") || text.includes(delimiter);
  if (!mustQuote) return text;
  return `"${text.replace(/"/g, "\"\"")}"`;
}

function stringifyCsv(rows, delimiter = ",") {
  return rows.map(row => row.map(value => escapeCsv(value, delimiter)).join(delimiter)).join("\r\n");
}

function cleanTranslation(value, fallback) {
  let text = value == null ? "" : String(value);
  text = text.trim();
  text = text.replace(/^```(?:json|text)?/i, "").replace(/```$/i, "").trim();
  if ((text.startsWith("\"") && text.endsWith("\"")) ||
      (text.startsWith("'") && text.endsWith("'"))) {
    text = text.slice(1, -1).trim();
  }
  return text || fallback;
}

function parseJsonArray(content) {
  const cleaned = content
    .replace(/^```(?:json)?/i, "")
    .replace(/```$/i, "")
    .trim();
  try {
    return JSON.parse(cleaned);
  } catch (error) {
    const start = cleaned.indexOf("[");
    const end = cleaned.lastIndexOf("]");
    if (start >= 0 && end > start) {
      return JSON.parse(cleaned.slice(start, end + 1));
    }
    throw error;
  }
}

function systemPrompt(jsonMode) {
  return [
    "You are a professional technical translator.",
    "Translate Simplified Chinese into Russian.",
    "The CSV contains comments from a PLC/HMI industrial automation project.",
    "Use PLC automation context: sensors, cylinders, axes, motors, valves, vacuum, pressure, cooling water, graphite disk, stations, commands, states, alarms, HMI labels, registers and interlocks.",
    jsonMode ? "Return only a valid JSON array of strings." : "Return only the translated text.",
    jsonMode ? "The output array length and order must match the input array." : "",
    "Translate every Chinese word, including Chinese embedded inside PLC signal names, alarm names, command/status strings and strings with #, _, /, -, parentheses.",
    "Preserve PLC identifiers, register names, addresses, model names, axis letters, numbers, punctuation and separators, but translate the Chinese fragments around them.",
    "Use concise PLC-comment wording.",
    "Preferred Russian terms: 指令=команда; 运行中=выполняется; 运行完成=выполнено; 工位=рабочая позиция/станция; 原点=ноль/исходное положение; 动点=рабочее положение; 气缸=цилиндр; 传感器=датчик; 报警=авария; 故障=ошибка; 信号=сигнал; 通信=связь; 继电器=реле; 设定=настройка; 页面/页=страница; 行=строка; 属性=атрибуты; 模板=шаблон; 使能=разрешение; 去能=снятие разрешения; 当前显示页=текущая отображаемая страница.",
    "Examples: 27#指令_运行完成 -> 27#команда_выполнено; 28#指令_运行中 -> 28#команда_выполняется; KV-D30 19页第1行属性 -> KV-D30 страница 19, строка 1, атрибуты.",
    "Do not leave Simplified Chinese characters in the output.",
    "Do not add explanations, markdown or comments.",
  ].filter(Boolean).join(" ");
}

async function callChat(messages, maxTokens) {
  const response = await fetch(`${BASE_URL.replace(/\/+$/, "")}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${API_KEY}`,
    },
    body: JSON.stringify({
      model: MODEL,
      messages,
      temperature: 0.2,
      top_p: 0.9,
      max_tokens: maxTokens,
      chat_template_kwargs: { enable_thinking: false },
    }),
  });

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`HTTP ${response.status}: ${text.slice(0, 300)}`);
  }

  const data = await response.json();
  const content = data?.choices?.[0]?.message?.content;
  if (!content) throw new Error("empty LLM response");
  return content.trim();
}

async function translateBatch(texts) {
  const chars = texts.reduce((sum, text) => sum + text.length, 0);
  const content = await callChat([
    { role: "system", content: systemPrompt(true) },
    { role: "user", content: `Input JSON array:\n${JSON.stringify(texts)}` },
  ], Math.max(512, Math.min(4096, Math.ceil(chars * 3.2) + 700)));

  const parsed = parseJsonArray(content);
  if (!Array.isArray(parsed) || parsed.length !== texts.length) {
    throw new Error("API returned invalid JSON array length");
  }
  return parsed.map((value, index) => cleanTranslation(value, texts[index]));
}

async function translateSingle(text) {
  const content = await callChat([
    { role: "system", content: systemPrompt(false) },
    {
      role: "user",
      content: [
        "Strict retry: the previous answer was incomplete or still contained Chinese.",
        "Translate all Chinese fragments now while preserving only non-Chinese codes, numbers and separators.",
        "",
        "Text:",
        text,
      ].join("\n"),
    },
  ], Math.max(256, Math.min(1200, text.length * 4 + 240)));
  return cleanTranslation(content, text);
}

async function translateWithRetries(text) {
  let last = text;
  for (let attempt = 1; attempt <= MAX_RETRIES + 1; attempt += 1) {
    last = await translateSingle(text);
    if (!HAN_RE.test(last)) return last;
    log(`strict retry kept Han attempt=${attempt} text=${JSON.stringify(text)} out=${JSON.stringify(last)}`);
  }
  return last;
}

function writeCache(translations) {
  const payload = {
    model: MODEL,
    baseUrl: BASE_URL,
    batchSize: BATCH_SIZE,
    updatedAt: new Date().toISOString(),
    translations: Object.fromEntries(translations),
  };
  fs.writeFileSync(CACHE_FILE, JSON.stringify(payload, null, 2), "utf8");
}

function writeOutput(rows, file) {
  fs.writeFileSync(file, `\ufeff${stringifyCsv(rows)}`, "utf8");
}

async function main() {
  fs.writeFileSync(LOG_FILE, "", "utf8");
  if (fs.existsSync(CACHE_FILE)) fs.unlinkSync(CACHE_FILE);
  log(`start full retranslation input=${INPUT}`);
  log(`llm base=${BASE_URL} model=${MODEL} batch=${BATCH_SIZE}`);

  const sourceText = new TextDecoder("gb18030").decode(fs.readFileSync(INPUT));
  const rows = parseCsv(sourceText);
  const jobs = [];
  for (let rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
    const row = rows[rowIndex];
    for (let colIndex = 0; colIndex < row.length; colIndex += 1) {
      const cell = row[colIndex];
      if (HAN_RE.test(cell)) jobs.push({ rowIndex, colIndex, text: cell });
    }
  }

  const uniqueTexts = [...new Map(jobs.map(job => [job.text, job.text])).values()];
  log(`rows=${rows.length} chineseCells=${jobs.length} unique=${uniqueTexts.length}`);

  const translations = new Map();
  const started = Date.now();
  for (let offset = 0; offset < uniqueTexts.length; offset += BATCH_SIZE) {
    const chunk = uniqueTexts.slice(offset, offset + BATCH_SIZE);
    try {
      const out = await translateBatch(chunk);
      for (let i = 0; i < chunk.length; i += 1) {
        let translated = out[i];
        if (HAN_RE.test(translated)) {
          translated = await translateWithRetries(chunk[i]);
        }
        translations.set(chunk[i], translated);
      }
    } catch (error) {
      log(`batch fallback offset=${offset} size=${chunk.length} reason=${error.message}`);
      for (const text of chunk) {
        translations.set(text, await translateWithRetries(text));
      }
    }

    const done = Math.min(offset + BATCH_SIZE, uniqueTexts.length);
    const elapsedSec = Math.max(1, Math.round((Date.now() - started) / 1000));
    const rate = (done / elapsedSec).toFixed(2);
    const etaSec = Math.round((uniqueTexts.length - done) / Math.max(0.01, Number(rate)));
    writeCache(translations);
    log(`progress unique=${done}/${uniqueTexts.length} rate=${rate}/s eta=${etaSec}s`);
  }

  for (const job of jobs) {
    rows[job.rowIndex][job.colIndex] = translations.get(job.text) || job.text;
  }

  const leftovers = [];
  for (let rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
    const row = rows[rowIndex];
    for (let colIndex = 0; colIndex < row.length; colIndex += 1) {
      if (HAN_RE.test(row[colIndex])) {
        leftovers.push({ line: rowIndex + 1, col: colIndex + 1, value: row[colIndex] });
      }
    }
  }

  writeOutput(rows, OUTPUT_TMP);
  if (leftovers.length === 0) {
    fs.copyFileSync(OUTPUT_TMP, OUTPUT_MAIN);
    log(`done output=${OUTPUT_MAIN} rows=${rows.length} hanCells=0`);
  } else {
    fs.writeFileSync(path.join(ROOT, "retranslate_plc_leftovers.json"), JSON.stringify(leftovers, null, 2), "utf8");
    log(`done with leftovers=${leftovers.length} outputTmp=${OUTPUT_TMP}`);
    process.exitCode = 2;
  }
}

main().catch(error => {
  log(`fatal ${error.stack || error.message}`);
  process.exitCode = 1;
});
