"""Веб-интерфейс (FastAPI) для перевода PDF.

Запуск: python -m app.web  -> http://127.0.0.1:8765
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import uvicorn

from pipeline.config.loader import ROOT, load_config

UPLOADS = ROOT / "uploads"
UPLOADS.mkdir(exist_ok=True)

app = FastAPI(title="PDF translator")
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# ---------------------------- HTML ----------------------------
HTML_PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF переводчик</title>
<style>
  :root{
    --bg:#0f172a; --panel:#1e293b; --panel2:#273449; --border:#334155;
    --text:#e2e8f0; --muted:#94a3b8; --accent:#6366f1; --accent2:#22c55e;
    --warn:#f59e0b; --err:#ef4444;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
    font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif}
  body{min-height:100vh;display:flex;flex-direction:column;align-items:center}
  .wrap{width:min(880px,94vw);padding:32px 16px 64px}
  h1{font-size:1.6rem;margin:8px 0 4px;letter-spacing:.2px}
  .sub{color:var(--muted);margin:0 0 24px;font-size:.95rem}
  .card{background:var(--panel);border:1px solid var(--border);
    border-radius:14px;padding:22px;margin-bottom:18px}
  .drop{border:2px dashed var(--border);border-radius:12px;padding:34px;
    text-align:center;cursor:pointer;transition:.2s;background:var(--panel2)}
  .drop:hover,.drop.over{border-color:var(--accent);background:#2c3a52}
  .drop .big{font-size:2.4rem;margin-bottom:6px}
  .drop p{margin:4px 0;color:var(--muted)}
  .file-name{margin-top:12px;font-size:.95rem;color:var(--text);word-break:break-all}
  input[type=file]{display:none}
  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:16px}
  button{font:inherit;padding:11px 22px;border-radius:10px;border:0;cursor:pointer;
    background:var(--accent);color:white;font-weight:600;transition:.15s}
  button:hover:not(:disabled){filter:brightness(1.08)}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.ghost{background:var(--panel2);color:var(--text);border:1px solid var(--border)}
  .opts{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);
    font-size:.9rem;margin-top:14px}
  .opts label{display:flex;align-items:center;gap:6px;cursor:pointer}
  .opts input[type=number]{width:90px;background:var(--panel2);
    border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 8px}
  .stage-list{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0 4px}
  .stage{flex:1;min-width:120px;background:var(--panel2);border:1px solid var(--border);
    border-radius:8px;padding:9px 10px;font-size:.82rem;text-align:center;color:var(--muted)}
  .stage .t{font-weight:600;color:var(--text);display:block;margin-bottom:2px}
  .stage.active{border-color:var(--accent);color:var(--text);
    box-shadow:0 0 0 2px rgba(99,102,241,.25)}
  .stage.done{border-color:var(--accent2);color:var(--accent2)}
  .stage.err{border-color:var(--err);color:var(--err)}
  .stage .spin{display:inline-block;width:12px;height:12px;border:2px solid
    var(--accent);border-top-color:transparent;border-radius:50%;
    animation:spin .8s linear infinite;vertical-align:middle;margin-left:5px}
  .stage.done .spin{display:none}
  @keyframes spin{to{transform:rotate(360deg)}}
  .prog-wrap{background:var(--panel2);border-radius:99px;height:22px;
    overflow:hidden;margin:14px 0 6px;border:1px solid var(--border);position:relative}
  .prog{height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));
    transition:width .35s ease;position:relative}
  .prog.running::after{content:"";position:absolute;inset:0;
    background:repeating-linear-gradient(45deg,rgba(255,255,255,.18) 0 12px,
      rgba(255,255,255,0) 12px 24px);background-size:34px 34px;
      animation:stripes 1s linear infinite}
  @keyframes stripes{to{background-position:34px 0}}
  .prog.indeterminate{width:35% !important;animation:slide 1.4s ease-in-out infinite}
  @keyframes slide{0%{margin-left:-35%}50%{margin-left:100%}100%{margin-left:-35%}}
  .prog-pct{position:absolute;inset:0;display:flex;align-items:center;
    justify-content:center;font-size:.72rem;font-weight:700;color:#fff;
    text-shadow:0 1px 2px rgba(0,0,0,.6);letter-spacing:.3px;z-index:2}
  .banner{display:flex;align-items:center;gap:10px;margin:6px 0 4px;
    padding:10px 14px;background:var(--panel2);border:1px solid var(--border);
    border-radius:10px;font-weight:600}
  .banner .dot-anim{display:inline-flex;gap:3px}
  .banner .dot-anim span{width:7px;height:7px;border-radius:50%;background:var(--accent);
    animation:bounce 1.2s infinite ease-in-out}
  .banner .dot-anim span:nth-child(2){animation-delay:.15s}
  .banner .dot-anim span:nth-child(3){animation-delay:.3s}
  @keyframes bounce{0%,80%,100%{transform:scale(.5);opacity:.5}
    40%{transform:scale(1);opacity:1}}
  .banner .timer{margin-left:auto;color:var(--muted);
    font-variant-numeric:tabular-nums;font-weight:500;font-size:.85rem}
  .banner.ok{border-color:var(--accent2);color:var(--accent2)}
  .banner.err{border-color:var(--err);color:var(--err)}
  .banner.idle{opacity:.6}
  .log{background:#0b1220;border:1px solid var(--border);border-radius:10px;
    padding:12px;height:240px;overflow:auto;font-family:Consolas,Menlo,monospace;
    font-size:.82rem;color:#cbd5e1;white-space:pre-wrap;margin-top:12px}
  .log .err{color:var(--err)}
  .log .ok{color:var(--accent2)}
  .log .warn{color:var(--warn)}
  .stats{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);
    font-size:.85rem;margin-top:8px}
  .stats span b{color:var(--text)}
  .download{margin-top:14px}
  .badge{font-size:.7rem;padding:2px 8px;border-radius:99px;
    background:var(--panel2);border:1px solid var(--border);color:var(--muted)}
  .hidden{display:none}
  footer{color:var(--muted);font-size:.78rem;margin-top:24px;text-align:center}
  a{color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">
  <h1>PDF переводчик <span class="badge" id="langBadge">… → …</span></h1>
  <p class="sub">Локальная LLM · сохранение структуры, изображений и оглавления</p>

  <div class="card">
    <div class="drop" id="drop">
      <div class="big">📄</div>
      <p><b>Перетащите PDF сюда</b> или нажмите для выбора</p>
      <p class="sub" style="margin:0" id="langHint">исходный → целевой</p>
      <input type="file" id="file" accept="application/pdf">
    </div>
    <div class="file-name" id="fileName"></div>

    <div class="opts">
      <label><input type="checkbox" id="resume" checked> Resume (пропустить готовые этапы)</label>
      <label><input type="checkbox" id="fromTranslate"> Только перевод</label>
      <label>Лимит сегментов: <input type="number" id="limit" min="0" value="0" title="0 = все"></label>
    </div>

    <div class="row">
      <button id="startBtn" disabled>Перевести</button>
      <button id="cancelBtn" class="ghost" disabled>Отмена</button>
    </div>
  </div>

  <div class="card hidden" id="statusCard">
    <div class="banner idle" id="banner">
      <span id="bannerText">Ожидание запуска…</span>
      <span class="dot-anim hidden" id="dotAnim"><span></span><span></span><span></span></span>
      <span class="timer" id="timer">00:00</span>
    </div>
    <div class="stage-list" id="stages">
      <div class="stage" data-s="parse"><span class="t">1. Парсинг</span>parse.json</div>
      <div class="stage" data-s="segment"><span class="t">2. Сегментация</span>segments.json</div>
      <div class="stage" data-s="translate"><span class="t">3. Перевод</span>LLM · кэш</div>
      <div class="stage" data-s="build"><span class="t">4. Сборка</span>_RU.pdf</div>
      <div class="stage" data-s="validate"><span class="t">5. Валидация</span>проверка</div>
    </div>
    <div class="prog-wrap">
      <div class="prog" id="prog"></div>
      <div class="prog-pct" id="progPct">0%</div>
    </div>
    <div class="stats" id="stats"></div>
    <div class="log" id="log"></div>
    <div class="download hidden" id="downloadBox">
      <a id="downloadLink" href="#" download><button>⬇ Скачать结果 PDF</button></a>
    </div>
  </div>

  <footer>Конвейер: PyMuPDF + OpenAI-совместимая LLM · <a href="/api/health" target="_blank">/api/health</a></footer>
</div>

<script>
const $ = s => document.querySelector(s);
let currentJob = null, pollTimer = null, selectedFile = null;

fetch('/api/config').then(r=>r.json()).then(c=>{
  const src = c.source_lang||'?', tgt = c.target_lang||'?';
  $('#langBadge').textContent = src.toUpperCase()+' → '+tgt.toUpperCase();
  $('#langHint').textContent = src+' → '+tgt;
});

const drop = $('#drop'), fileInput = $('#file'), fileName = $('#fileName'),
      startBtn = $('#startBtn'), cancelBtn = $('#cancelBtn');

drop.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', e => { if (e.target.files[0]) pickFile(e.target.files[0]); });
['dragenter','dragover'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add('over');
}));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.remove('over');
}));
drop.addEventListener('drop', e => {
  const f = e.dataTransfer.files[0];
  if (f) pickFile(f);
});

function pickFile(f){
  if (f.type !== 'application/pdf' && !f.name.toLowerCase().endsWith('.pdf')){
    alert('Нужен PDF-файл'); return;
  }
  selectedFile = f;
  fileName.textContent = '📄 ' + f.name + '  (' + (f.size/1024/1024).toFixed(2) + ' МБ)';
  startBtn.disabled = false;
}

startBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  startBtn.disabled = true;
  $('#statusCard').classList.remove('hidden');
  resetStages();
  $('#log').textContent = '';
  $('#log').dataset.seen = '0';
  $('#prog').style.width = '0%';
  $('#prog').style.marginLeft = '0';
  $('#prog').classList.remove('indeterminate','running');
  $('#progPct').textContent = '0%';
  $('#timer').textContent = '00:00';
  $('#banner').classList.remove('ok','err');
  $('#bannerText').textContent = 'Запуск конвейера…';
  $('#dotAnim').classList.remove('hidden');
  $('#downloadBox').classList.add('hidden');
  startTimer();

  const fd = new FormData(); fd.append('file', selectedFile);
  try{
    const r = await fetch('/api/upload', {method:'POST', body:fd});
    if (!r.ok){ const t = await r.text(); log('Ошибка загрузки: '+t, 'err'); return; }
    const data = await r.json();
    const params = {
      job: data.job_id, src: data.path,
      resume: $('#resume').checked, from_translate: $('#fromTranslate').checked,
      limit: parseInt($('#limit').value)||0,
    };
    const r2 = await fetch('/api/start', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(params),
    });
    if (!r2.ok){ log('Не удалось запустить конвейер', 'err'); return; }
    currentJob = data.job_id;
    cancelBtn.disabled = false;
    pollStatus();
  }catch(e){ log('Сетевая ошибка: '+e, 'err'); }
});

cancelBtn.addEventListener('click', async () => {
  if (!currentJob) return;
  await fetch('/api/cancel?job='+currentJob, {method:'POST'});
  log('Запрошена отмена…', 'warn');
});

function resetStages(){
  document.querySelectorAll('.stage').forEach(s => {
    s.className='stage';
    const spin = s.querySelector('.spin'); if (spin) spin.remove();
  });
}
function setStage(name, state){
  const el = document.querySelector('.stage[data-s="'+name+'"]'); if (!el) return;
  el.classList.remove('active','done','err');
  let spin = el.querySelector('.spin');
  if (state === 'active'){
    el.classList.add('active');
    if (!spin){ spin = document.createElement('span'); spin.className = 'spin'; el.appendChild(spin); }
  } else {
    if (spin) spin.remove();
    if (state) el.classList.add(state);
  }
}

const STAGE_LABELS = {parse:'Парсинг PDF', segment:'Сегментация',
  translate:'Перевод через LLM', build:'Сборка PDF', validate:'Валидация'};
let startedAt = 0, timerInt = null;
function startTimer(){ startedAt = Date.now(); if (timerInt) clearInterval(timerInt);
  timerInt = setInterval(()=>{ if (!startedAt) return;
    const s = Math.floor((Date.now()-startedAt)/1000);
    $('#timer').textContent = String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');
  }, 1000); }
function stopTimer(){ if (timerInt){ clearInterval(timerInt); timerInt=null; } }

function log(msg, cls){
  const el = $('#log'); const span = document.createElement('span');
  if (cls) span.className = cls;
  span.textContent = msg + '\n';
  el.appendChild(span); el.scrollTop = el.scrollHeight;
}

function pollStatus(){
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!currentJob) return;
    try{
      const r = await fetch('/api/status?job='+currentJob);
      const d = await r.json();
      update(d);
      if (d.state === 'done' || d.state === 'error' || d.state === 'cancelled'){
        clearInterval(pollTimer); pollTimer = null;
        stopTimer(); cancelBtn.disabled = true; startBtn.disabled = false;
      }
    }catch(e){}
  }, 1000);
}

function update(d){
  const order = ['parse','segment','translate','build','validate'];
  const idx = order.indexOf(d.stage);
  order.forEach((s, i) => {
    if (i < idx) setStage(s, 'done');
    else if (i === idx) setStage(s, d.state === 'error' ? 'err' : 'active');
    else setStage(s, null);
  });
  if (d.state === 'done') order.forEach(s => setStage(s, 'done'));

  const prog = $('#prog'), pct = $('#progPct');
  prog.classList.remove('indeterminate','running');
  let percent = 0;
  if (d.state === 'done'){ percent = 100; prog.style.width = '100%'; }
  else if (d.total > 0){
    percent = Math.min(100, Math.round(d.progress / d.total * 100));
    prog.style.width = percent + '%'; prog.classList.add('running');
  } else if (d.stage && idx >= 0){
    const base = idx / order.length * 100;
    const span = (1 / order.length) * 100;
    prog.style.width = (span * 0.6) + '%';
    prog.style.marginLeft = base + '%';
    prog.classList.add('running');
    percent = Math.round(base);
  } else { prog.style.width = '0%'; prog.style.marginLeft = '0'; }
  if (d.state === 'running' && d.total === 0 && d.stage){
    prog.classList.add('indeterminate');
    prog.style.marginLeft = '';
    pct.textContent = '…';
  } else { pct.textContent = percent + '%'; }
  if (d.state === 'done'){ prog.classList.remove('indeterminate','running'); }

  const banner = $('#banner'), btext = $('#bannerText'), dotAnim = $('#dotAnim');
  banner.classList.remove('idle','ok','err');
  if (d.state === 'running'){
    btext.textContent = 'Идёт «' + (STAGE_LABELS[d.stage]||d.stage) + '»…';
    dotAnim.classList.remove('hidden');
  } else if (d.state === 'done'){
    btext.textContent = '✓ Готово — перевод завершён';
    dotAnim.classList.add('hidden');
    banner.classList.add('ok');
  } else if (d.state === 'error'){
    btext.textContent = '✗ Ошибка — см. лог';
    dotAnim.classList.add('hidden');
    banner.classList.add('err');
  } else if (d.state === 'cancelled'){
    btext.textContent = 'Отменено'; dotAnim.classList.add('hidden');
  } else {
    btext.textContent = 'Ожидание…'; dotAnim.classList.add('hidden');
    banner.classList.add('idle');
  }

  const stats = [];
  if (d.stage) stats.push('<span>Этап: <b>'+(STAGE_LABELS[d.stage]||d.stage)+'</b></span>');
  if (d.total > 0) stats.push('<span>Сегментов: <b>'+d.progress+' / '+d.total+'</b></span>');
  if (d.ok>=0) stats.push('<span>OK: <b>'+d.ok+'</b></span>');
  if (d.cached>=0) stats.push('<span>Из кэша: <b>'+d.cached+'</b></span>');
  if (d.fail>=0) stats.push('<span>Ошибок: <b>'+d.fail+'</b></span>');
  if (d.pages) stats.push('<span>Страниц: <b>'+d.pages+'</b></span>');
  if (d.images) stats.push('<span>Изображений: <b>'+d.images+'</b></span>');
  stats.push('<span>Статус: <b>' + ({idle:'ожидание',running:'выполняется',
    done:'готово',error:'ошибка',cancelled:'отменено'}[d.state]||d.state) + '</b></span>');
  $('#stats').innerHTML = stats.join('');

  const seen = ($('#log').dataset.seen||'0')|0;
  if (d.logs && d.logs.length > seen){
    for (let i = seen; i < d.logs.length; i++){
      const line = d.logs[i]; let cls = '';
      if (/ошибк|error|exception/i.test(line)) cls = 'err';
      else if (/готово|пройдена|ok=|успешно/i.test(line)) cls = 'ok';
      else if (/warn|предупр/i.test(line)) cls = 'warn';
      log(line, cls);
    }
    $('#log').dataset.seen = d.logs.length;
  }
  if (d.state === 'done' && d.result_path){
    $('#downloadBox').classList.remove('hidden');
    $('#downloadLink').href = '/api/download?job='+currentJob;
  }
  if (d.state === 'error'){
    log('Конвейер завершился с ошибкой. См. log/translate.log', 'err');
  }
}
</script>
</body>
</html>
"""

# ---------------------------- парсинг прогресса ----------------------------
RE_TQDM = re.compile(r"(\d+)%\|.*?\|\s*(\d+)/(\d+)")
RE_OK = re.compile(r"ok=(\d+)\s+cached=(\d+)\s+fail=(\d+)")
RE_PARSE_PAGE = re.compile(r"parse page (\d+)/(\d+)")
RE_STAGE = re.compile(r"=== ЭТАП: (\w+) ===")
RE_VALID_OK = re.compile(r"ВАЛИДАЦИЯ ПРОЙДЕНА")
RE_VALID_FAIL = re.compile(r"НАЙДЕНЫ ПРОБЛЕМЫ")
RE_PAGES_IMG = re.compile(
    r"страниц:\s+src=(\d+)\s+out=(\d+).*?изображ\.:?\s+src=(\d+)\s+out=(\d+)",
    re.IGNORECASE | re.DOTALL)
RE_SEG_COUNT = re.compile(r"Сегментов:\s+(\d+)")


def _new_job(src_path: str) -> dict:
    cfg = load_config()
    out_name = Path(src_path).stem + ("_" + cfg["target_lang"].upper() + ".pdf")
    return {
        "job_id": uuid.uuid4().hex[:12],
        "src": src_path,
        "out_path": str(ROOT / "uploads" / out_name.replace("__", "_")),
        "state": "idle", "stage": "", "progress": 0, "total": 0,
        "ok": -1, "cached": -1, "fail": -1,
        "pages": None, "images": None,
        "logs": [], "result_path": None, "proc": None, "cancel": False,
        "started_at": time.time(),
    }


def _run_pipeline(job: dict, resume: bool, from_translate: bool, limit: int):
    cmd = [sys.executable, "-m", "app.cli",
           "--in", job["src"], "--out", job["out_path"]]
    if resume:
        cmd.append("--resume")
    if from_translate:
        cmd += ["--from-stage", "translate"]
    if limit and limit > 0:
        cmd += ["--limit", str(limit)]

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)

    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", text=True, bufsize=1,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    with JOBS_LOCK:
        job["proc"] = proc
        job["state"] = "running"

    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if not line:
                continue
            for chunk in re.split(r"\r", line):
                chunk = chunk.strip()
                if not chunk:
                    continue
                _parse_line(job, chunk)
                with JOBS_LOCK:
                    if job["cancel"]:
                        try: proc.terminate()
                        except Exception: pass
                        job["state"] = "cancelled"
                        job["logs"].append("[отменено пользователем]")
                        return
        proc.wait()
    except Exception as e:
        with JOBS_LOCK:
            job["state"] = "error"
            job["logs"].append(f"[exception] {e}")
        return

    rc = proc.returncode
    with JOBS_LOCK:
        if job["cancel"]:
            job["state"] = "cancelled"
        elif rc == 0:
            job["state"] = "done"
            job["stage"] = "validate"
            if Path(job["out_path"]).exists():
                job["result_path"] = job["out_path"]
            job["logs"].append("[done] Конвейер завершён успешно.")
        else:
            job["state"] = "error"
            job["logs"].append(f"[error] cli завершился с кодом {rc}")


def _parse_line(job: dict, line: str):
    with JOBS_LOCK:
        job["logs"].append(line)
        if len(job["logs"]) > 600:
            job["logs"] = job["logs"][-400:]
    m = RE_STAGE.search(line)
    if m:
        with JOBS_LOCK:
            job["stage"] = m.group(1).lower()
            job["progress"] = 0
            job["total"] = 0
        return
    m = RE_SEG_COUNT.search(line)
    if m:
        with JOBS_LOCK: job["total"] = int(m.group(1))
        return
    m = RE_TQDM.search(line)
    if m:
        with JOBS_LOCK:
            job["progress"] = int(m.group(2))
            job["total"] = int(m.group(3))
        return
    m = RE_OK.search(line)
    if m:
        with JOBS_LOCK:
            job["ok"] = int(m.group(1))
            job["cached"] = int(m.group(2))
            job["fail"] = int(m.group(3))
        return
    m = RE_PARSE_PAGE.search(line)
    if m:
        with JOBS_LOCK:
            job["stage"] = "parse"
            job["progress"] = int(m.group(1))
            job["total"] = int(m.group(2))
        return
    if RE_VALID_OK.search(line) or RE_VALID_FAIL.search(line):
        with JOBS_LOCK: job["stage"] = "validate"
    m = RE_PAGES_IMG.search(line)
    if m:
        with JOBS_LOCK:
            job["pages"] = int(m.group(2))
            job["images"] = int(m.group(4))


# ---------------------------- API ----------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/api/health")
async def health():
    return {"status": "ok", "jobs": len(JOBS)}


@app.get("/api/config")
async def api_config():
    cfg = load_config()
    return {"source_lang": cfg.get("source_lang", "?"),
            "target_lang": cfg.get("target_lang", "?")}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Нужен PDF-файл")
    job_id = uuid.uuid4().hex[:12]
    safe = f"{job_id}_{Path(file.filename).name}"
    dest = UPLOADS / safe
    with open(dest, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    return {"job_id": job_id, "path": str(dest), "name": file.filename}


@app.post("/api/start")
async def start(payload: dict, background_tasks: BackgroundTasks = None):
    job_id = payload.get("job")
    src = payload.get("src")
    resume = bool(payload.get("resume", True))
    from_translate = bool(payload.get("from_translate", False))
    limit = int(payload.get("limit", 0) or 0)
    if not job_id or not src:
        raise HTTPException(400, "требуются поля job и src")
    if not Path(src).exists():
        raise HTTPException(404, "исходный PDF не найден")
    with JOBS_LOCK:
        j = _new_job(src)
        j["job_id"] = job_id
        JOBS[job_id] = j
    background_tasks.add_task(_run_pipeline, j, resume, from_translate, limit)
    return {"job_id": job_id, "state": "running"}


@app.get("/api/status")
async def status(job: str):
    with JOBS_LOCK:
        j = JOBS.get(job)
        if not j:
            raise HTTPException(404, "job не найден")
        return {
            "state": j["state"], "stage": j["stage"],
            "progress": j["progress"], "total": j["total"],
            "ok": j["ok"], "cached": j["cached"], "fail": j["fail"],
            "pages": j["pages"], "images": j["images"],
            "logs": j["logs"][-200:],
            "result_path": j["result_path"],
        }


@app.post("/api/cancel")
async def cancel(job: str):
    with JOBS_LOCK:
        j = JOBS.get(job)
        if j:
            j["cancel"] = True
            if j.get("proc"):
                try: j["proc"].terminate()
                except Exception: pass
    return {"ok": True}


@app.get("/api/download")
async def download(job: str):
    with JOBS_LOCK:
        j = JOBS.get(job)
        if not j or not j.get("result_path"):
            raise HTTPException(404, "результат недоступен")
        p = j["result_path"]
    if not Path(p).exists():
        raise HTTPException(404, "файл не найден")
    return FileResponse(p, media_type="application/pdf",
                        filename=Path(p).name)


if __name__ == "__main__":
    uvicorn.run("app.web:app", host="127.0.0.1", port=8765,
                reload=False, log_level="warning")