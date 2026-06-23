"""Этап 3 — Перевод сегментов через LLM (OpenAI-совместимый API).

- SQLite-кэш translations.db (ключ sha256(zh+prompt+model))
- Глоссарий из glossary.csv
- Системный промпт с запретом перевода якорей/имён параметров
- Постпроверка наличия якорей; повторный запрос при потере
- ThreadPoolExecutor параллелизм
- JSONL-дамп ошибок
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import (ROOT, load_config, setup_logger, load_json, save_json,
                   ensure_dirs, resolve_path, load_glossary)

SYSTEM_PROMPT = (
    "Ты — профессиональный переводчик технической документации с китайского на русский язык. "
    "Переводи фрагмент Руководства пользователя промышленной камеры.\n"
    "Правила:\n"
    "1. Переводи на грамотный технический русский.\n"
    "2. Соблюдай терминологию из глоссария (если применимо).\n"
    "3. НЕ переводи и не изменяй следующие токены-якоря и идентификаторы:\n"
    "   - ссылки на рисунки: 图N-M -> Рис. N-M (заменяй 图 на «Рис. », сохраняй N-M);\n"
    "   - ссылки на таблицы: 表N-M -> Табл. N-M;\n"
    "   - номера разделов вида «N.M», «N.M.K», «第N章», «第N.M 章节» — сохраняй числовую часть;\n"
    "   - имена параметров интерфейса на английском (Acquisition Control, Trigger Mode, "
    "Line Selector, Gamma Enable, HDR Selector, Balance White Auto и подобные) — НЕ переводи;\n"
    "   - маркеры списка (цифра-точка, символы ●/•/-) и нумерацию шагов — сохраняй;\n"
    "   - литералы вида {{...}} сохраняй дословно.\n"
    "4. Не добавляй пояснений, комментариев, примечаний. Выведи только перевод.\n"
    "5. Сохраняй пунктуацию и форматирование (переносы строк) близко к оригиналу.\n"
)

GLOSSARY_PROMPT_TMPL = (
    "Глоссарий (термины zh -> ru): {glossary}"
)

CONTEXT_TMPL = (
    "Контекст: тип фрагмента={stype}; раздел={section}. "
    "Переведи фрагмент, соблюдая правила."
)

RETRY_HINT = (
    "В предыдущем переводе потеряны или изменены якоря/номера. "
    "Повтори перевод, строго сохранив все числовые якоря в виде «Рис. N-M», «Табл. N-M», "
    "номера разделов и имена параметров. Выведи только перевод."
)

# Имена параметров — эвристика: ASCII-токены длиной >= 4 с заглавной буквой
RE_PARAM_NAME = re.compile(r"\b([A-Z][A-Za-z][A-Za-z0-9 _]{2,})\b")


class Cache:
    def __init__(self, path: str):
        self.path = str(resolve_path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS translations ("
            "key TEXT PRIMARY KEY, zh TEXT, ru TEXT, ts TEXT, model TEXT, "
            "prompt_hash TEXT, accepts_review INTEGER DEFAULT 1)"
        )
        self.conn.commit()

    @staticmethod
    def make_key(zh: str, model: str, prompt_hash: str) -> str:
        return hashlib.sha256((zh + "\x00" + model + "\x00" + prompt_hash).encode("utf-8")).hexdigest()

    def get(self, key: str):
        with self._lock:
            cur = self.conn.execute(
                "SELECT ru FROM translations WHERE key=?", (key,))
            row = cur.fetchone()
            return row[0] if row else None

    def put(self, key: str, zh: str, ru: str, model: str, prompt_hash: str, accepts_review: int = 1):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO translations "
                "(key, zh, ru, ts, model, prompt_hash, accepts_review) "
                "VALUES (?,?,?,?,datetime('now'),?,?)",
                (key, zh, ru, model, prompt_hash, accepts_review))
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()


class Translator:
    def __init__(self, cfg: dict, logger):
        self.cfg = cfg
        self.logger = logger
        self.client = OpenAI(
            base_url=cfg["llm_base_url"],
            api_key=cfg.get("llm_api_key", "not-needed"),
            timeout=cfg.get("request_timeout", 300),
        )
        self.model = cfg["llm_model"]
        self.enable_thinking = bool(cfg.get("enable_thinking", False))
        self.max_tokens = int(cfg.get("max_tokens", 4096))
        self.temperature = float(cfg.get("temperature", 0.2))
        self.top_p = float(cfg.get("top_p", 0.9))
        self.workers = int(cfg.get("workers", 2))
        self.cache = Cache(cfg["cache_db"])
        self.glossary = load_glossary(cfg["glossary_path"])
        self.prompt_hash = self._compute_prompt_hash()
        self.errors_path = resolve_path(cfg["errors_path"])
        self.errors_fh = open(self.errors_path, "a", encoding="utf-8")

    def _compute_prompt_hash(self) -> str:
        sig = SYSTEM_PROMPT + "|" + json.dumps(self.glossary, ensure_ascii=False) + "|" + self.model
        return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]

    def _glossary_str(self, section_terms: list[str] | None = None) -> str:
        items = list(self.glossary.items())
        if section_terms:
            # поднять термины главы в начало
            extra = [(t, self.glossary[t]) for t in section_terms if t in self.glossary]
            items = extra + [(k, v) for k, v in items if k not in section_terms]
        # ограничим длину
        rendered = ", ".join(f"{k}→{v}" for k, v in items[:60])
        return rendered

    def _build_messages(self, seg: dict) -> list[dict]:
        glossary = self._glossary_str()
        context = CONTEXT_TMPL.format(stype=seg["type"], section=seg.get("section_id") or "—")
        user = f"{context}\nГлоссарий: {glossary}\n\nФрагмент (ZH):\n\"\"\"\n{seg['text']}\n\"\"\"\n\nВыведи только перевод на русский."
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    def _call_llm(self, messages: list[dict], hint: str | None = None) -> str:
        msgs = messages
        if hint:
            msgs = messages + [{"role": "user", "content": hint}]
        kwargs = dict(
            model=self.model,
            messages=msgs,
            temperature=self.temperature,
            top_p=self.top_p,
            max_tokens=self.max_tokens,
        )
        if self.enable_thinking is False:
            try:
                kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            except Exception:
                pass
        resp = self.client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        return content.strip()

    @staticmethod
    def _extract_anchors_nums(text: str) -> set[str]:
        nums = set()
        for m in re.finditer(r"图\s*(\d+-\d+)", text):
            nums.add(("fig", m.group(1)))
        for m in re.finditer(r"表\s*(\d+-\d+)", text):
            nums.add(("tab", m.group(1)))
        return nums

    def _check_anchors(self, zh: str, ru: str) -> bool:
        zh_anchors = self._extract_anchors_nums(zh)
        if not zh_anchors:
            return True
        ru_anchors = self._extract_anchors_nums(ru)
        # Проверяем, что все числовые N-M из ZH присутствуют в RU (как fig/tab)
        for kind, num in zh_anchors:
            if (kind, num) not in ru_anchors:
                # допустимо, если переведено как Рис./Табл. — число встречается
                if num in ru:
                    continue
                return False
        return True

    def translate_one(self, seg: dict) -> dict:
        text = seg["text"]
        if not text.strip():
            return {"id": seg["id"], "zh": text, "ru": text, "cached": True, "ok": True}

        key = Cache.make_key(text, self.model, self.prompt_hash)
        cached = self.cache.get(key)
        if cached is not None:
            return {"id": seg["id"], "zh": text, "ru": cached, "cached": True, "ok": True}

        messages = self._build_messages(seg)
        last_err = None
        ru = ""
        for attempt in range(2):
            try:
                ru = self._call_llm(messages, hint=RETRY_HINT if attempt == 1 else None)
            except Exception as e:
                last_err = str(e)
                continue
            if self._check_anchors(text, ru):
                self.cache.put(key, text, ru, self.model, self.prompt_hash, accepts_review=1)
                return {"id": seg["id"], "zh": text, "ru": ru, "cached": False, "ok": True}
            # повтор с подсказкой
            continue

        # не удалось пройти проверку якорей, но перевод есть — сохраняем с флагом
        if ru:
            self.cache.put(key, text, ru, self.model, self.prompt_hash, accepts_review=0)
            self._log_error(seg, text, ru, "anchors_lost")
            return {"id": seg["id"], "zh": text, "ru": ru, "cached": False, "ok": False,
                    "warn": "anchors_lost"}
        self._log_error(seg, text, "", f"llm_error:{last_err}")
        return {"id": seg["id"], "zh": text, "ru": text, "cached": False, "ok": False,
                "error": last_err}

    def _log_error(self, seg: dict, zh: str, ru: str, reason: str):
        rec = {"id": seg["id"], "page": seg["page"], "type": seg["type"],
               "section": seg.get("section_id"), "reason": reason, "zh": zh, "ru": ru}
        self.errors_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.errors_fh.flush()

    def translate_all(self, segments: list[dict]) -> dict[int, str]:
        results: dict[int, str] = {}
        ok_cnt = 0
        cached_cnt = 0
        fail_cnt = 0

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = {ex.submit(self.translate_one, seg): seg for seg in segments}
            for fut in tqdm(as_completed(futs), total=len(futs), desc="translate", unit="seg"):
                seg = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    self._log_error(seg, seg["text"], "", f"exception:{e}")
                    fail_cnt += 1
                    results[seg["id"]] = seg["text"]
                    continue
                results[seg["id"]] = res["ru"]
                if res.get("cached"):
                    cached_cnt += 1
                if res.get("ok"):
                    ok_cnt += 1
                else:
                    fail_cnt += 1

        self.logger.info("Перевод завершён: ok=%d cached=%d fail=%d", ok_cnt, cached_cnt, fail_cnt)
        return results

    def close(self):
        try:
            self.errors_fh.close()
        except Exception:
            pass
        self.cache.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Перевод segments.json -> translations.db + segments_ru.json")
    ap.add_argument("--in", dest="inp", help="segments.json")
    ap.add_argument("--out", dest="out", help="segments_ru.json (сегменты с полем ru)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число сегментов (отладка)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)

    inp = args.inp or cfg["segments_path"]
    out = args.out or "intermediate/segments_ru.json"
    segs = load_json(inp)
    if args.limit:
        segs = segs[:args.limit]

    tr = Translator(cfg, logger)
    try:
        translations = tr.translate_all(segs)
    finally:
        tr.close()

    for s in segs:
        s["ru"] = translations.get(s["id"], s["text"])
    save_json(segs, out)
    logger.info("Готово: %s", resolve_path(out))


if __name__ == "__main__":
    main()
