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
from pipeline.io.artifacts import source_hash, workdir
from pipeline.translate.translator import translate_filename_stem

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
  .topbar{display:flex;align-items:center;justify-content:space-between;
    gap:12px;flex-wrap:wrap;margin-bottom:4px}
  h1{font-size:1.6rem;margin:8px 0 4px;letter-spacing:.2px}
  .lang-switch{display:inline-flex;background:var(--panel2);border:1px solid
    var(--border);border-radius:99px;padding:3px;gap:2px;flex-shrink:0}
  .lang-switch button{padding:5px 14px;border-radius:99px;background:transparent;
    color:var(--muted);font-weight:600;font-size:.8rem;border:0;cursor:pointer;
    transition:.15s}
  .lang-switch button.active{background:var(--accent);color:#fff}
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
  button#resetBtn{border-color:var(--err);color:var(--err)}
  button#resetBtn:hover:not(:disabled){background:var(--err);color:#fff}
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
  .modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);
    display:flex;align-items:center;justify-content:center;z-index:1000}
  .modal-overlay.hidden{display:none}
  .modal{background:var(--panel);border:1px solid var(--border);
    border-radius:14px;padding:24px;width:min(460px,92vw);
    box-shadow:0 8px 40px rgba(0,0,0,.4)}
  .modal h3{margin:0 0 8px;font-size:1.1rem}
  .modal-sub{color:var(--muted);font-size:.85rem;margin:0 0 16px}
  .reset-stages{display:flex;flex-direction:column;gap:10px;margin-bottom:18px}
  .reset-stages label{display:flex;align-items:center;gap:8px;cursor:pointer;
    font-size:.9rem;padding:8px 12px;background:var(--panel2);
    border:1px solid var(--border);border-radius:8px}
  .reset-stages label:hover{border-color:var(--accent)}
  .reset-stages code{margin-left:auto;color:var(--muted);font-size:.78rem}
  .reset-stages .select-all{border-color:var(--accent)}
  .modal-row{display:flex;gap:12px;justify-content:flex-end}
  .modal-row button{padding:9px 18px}

</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1 data-i18n="title">PDF переводчик <span class="badge" id="langBadge">… → …</span></h1>
    <div class="lang-switch" id="langSwitch">
      <button data-lang="ru" class="active">RU</button>
      <button data-lang="en">EN</button>
    </div>
  </div>
  <p class="sub" data-i18n="subtitle">Локальная LLM · сохранение структуры, изображений и оглавления</p>

  <div class="card">
    <div class="drop" id="drop">
      <div class="big">📄</div>
      <p><b data-i18n="drop_title">Перетащите PDF сюда</b> <span data-i18n="drop_or">или нажмите для выбора</span></p>
      <p class="sub" style="margin:0" id="langHint">исходный → целевой</p>
      <input type="file" id="file" accept="application/pdf">
    </div>
    <div class="file-name" id="fileName"></div>

    <div class="opts">
      <label><input type="checkbox" id="resume" checked> <span data-i18n="resume">Resume (пропустить готовые этапы)</span></label>
      <label><input type="checkbox" id="fromTranslate"> <span data-i18n="translate_only">Только перевод</span></label>
      <label><span data-i18n="limit">Лимит сегментов:</span> <input type="number" id="limit" min="0" value="0" title="0 = все"></label>
    </div>

    <div class="row">
      <button id="startBtn" disabled data-i18n="start">Перевести</button>
      <button id="cancelBtn" class="ghost" disabled data-i18n="cancel">Отмена</button>
      <button id="resetBtn" class="ghost" disabled data-i18n="reset">Сбросить кэш</button>
    </div>
  </div>

  <div class="card hidden" id="statusCard">
    <div class="banner idle" id="banner">
      <span id="bannerText" data-i18n="waiting">Ожидание запуска…</span>
      <span class="dot-anim hidden" id="dotAnim"><span></span><span></span><span></span></span>
      <span class="timer" id="timer">00:00</span>
    </div>
    <div class="stage-list" id="stages">
      <div class="stage" data-s="parse"><span class="t" data-i18n="stage1">1. Парсинг</span>parse.json</div>
      <div class="stage" data-s="segment"><span class="t" data-i18n="stage2">2. Сегментация</span>segments.json</div>
      <div class="stage" data-s="translate"><span class="t" data-i18n="stage3">3. Перевод</span>LLM · кэш</div>
      <div class="stage" data-s="build"><span class="t" data-i18n="stage4">4. Сборка</span>_RU.pdf</div>
      <div class="stage" data-s="validate"><span class="t" data-i18n="stage5">5. Валидация</span>проверка</div>
    </div>
    <div class="prog-wrap">
      <div class="prog" id="prog"></div>
      <div class="prog-pct" id="progPct">0%</div>
    </div>
    <div class="stats" id="stats"></div>
    <div class="log" id="log"></div>
    <div class="download hidden" id="downloadBox">
      <a id="downloadLink" href="#" download><button data-i18n="download">⬇ Скачать результат PDF</button></a>
    </div>
  </div>

  <footer data-i18n="footer">Конвейер: PyMuPDF + OpenAI-совместимая LLM · <a href="/api/health" target="_blank">/api/health</a></footer>
</div>

<!-- Модальное окно сброса этапов -->
<div class="modal-overlay hidden" id="resetModal">
  <div class="modal">
    <h3 data-i18n="reset_title">Выберите этапы для удаления</h3>
    <p class="modal-sub" data-i18n="reset_sub">Следующий запуск начнётся с первого удалённого этапа.</p>
    <div class="reset-stages" id="resetStages">
      <label><input type="checkbox" value="parse" checked> <span data-i18n="stage1">1. Парсинг</span> <code>parse.json</code></label>
      <label><input type="checkbox" value="segment" checked> <span data-i18n="stage2">2. Сегментация</span> <code>segments.json</code></label>
      <label><input type="checkbox" value="translate" checked> <span data-i18n="stage3">3. Перевод</span> <code>segments_ru.json</code> + <code>translations.db</code></label>
      <label class="select-all"><input type="checkbox" id="selectAllStages" checked> <b data-i18n="reset_all">Выбрать все</b></label>
    </div>
    <div class="modal-row">
      <button id="resetConfirmBtn" data-i18n="reset_confirm_btn">Удалить и продолжить</button>
      <button id="resetCancelBtn" class="ghost" data-i18n="reset_cancel_btn">Отмена</button>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const I18N = {
  ru: {
    title: 'PDF переводчик',
    subtitle: 'Локальная LLM · сохранение структуры, изображений и оглавления',
    drop_title: 'Перетащите PDF сюда',
    drop_or: 'или нажмите для выбора',
    lang_hint: 'исходный → целевой',
    resume: 'Resume (пропустить готовые этапы)',
    translate_only: 'Только перевод',
    limit: 'Лимит сегментов:',
    start: 'Перевести',
    cancel: 'Отмена',
    reset: 'Сбросить кэш',
    reset_title: 'Выберите этапы для удаления',
    reset_sub: 'Следующий запуск начнётся с первого удалённого этапа.',
    reset_all: 'Выбрать все',
    reset_confirm_btn: 'Удалить и продолжить',
    reset_cancel_btn: 'Отмена',
    reset_done: 'Удалено: {items}. Следующий запуск начнётся с этого этапа.',
    reset_none: 'Не выбран ни один этап.',
    reset_err: 'Ошибка сброса: ',
    waiting: 'Ожидание запуска…',
    stage1: '1. Парсинг', stage2: '2. Сегментация', stage3: '3. Перевод',
    stage4: '4. Сборка', stage5: '5. Валидация',
    download: '⬇ Скачать результат PDF',
    footer: 'Конвейер: PyMuPDF + OpenAI-совместимая LLM',
    need_pdf: 'Нужен PDF-файл',
    err_upload: 'Ошибка загрузки: ',
    err_start: 'Не удалось запустить конвейер',
    err_net: 'Сетевая ошибка: ',
    cancel_req: 'Запрошена отмена…',
    starting: 'Запуск конвейера…',
    stage_running: 'Идёт «{stage}»…',
    done: '✓ Готово — перевод завершён',
    error: '✗ Ошибка — см. лог',
    cancelled: 'Отменено',
    waiting_short: 'Ожидание…',
    stat_stage: 'Этап: ', stat_segs: 'Сегментов: ', stat_ok: 'OK: ',
    stat_cached: 'Из кэша: ', stat_fail: 'Ошибок: ',
    stat_pages: 'Страниц: ', stat_images: 'Изображений: ',
    stat_status: 'Статус: ',
    pipeline_err: 'Конвейер завершился с ошибкой. См. log/translate.log',
    stages: {parse:'Парсинг PDF', segment:'Сегментация',
      translate:'Перевод через LLM', build:'Сборка PDF', validate:'Валидация'},
    states: {idle:'ожидание',running:'выполняется',done:'готово',
      error:'ошибка',cancelled:'отменено'},
  },
  en: {
    title: 'PDF translator',
    subtitle: 'Local LLM · preserves structure, images and table of contents',
    drop_title: 'Drop PDF here',
    drop_or: 'or click to browse',
    lang_hint: 'source → target',
    resume: 'Resume (skip finished stages)',
    translate_only: 'Translation only',
    limit: 'Segment limit:',
    start: 'Translate',
    cancel: 'Cancel',
    reset: 'Reset cache',
    reset_title: 'Select stages to delete',
    reset_sub: 'Next run will start from the first deleted stage.',
    reset_all: 'Select all',
    reset_confirm_btn: 'Delete and continue',
    reset_cancel_btn: 'Cancel',
    reset_done: 'Deleted: {items}. Next run will start from this stage.',
    reset_none: 'No stage selected.',
    reset_err: 'Reset error: ',
    waiting: 'Waiting to start…',
    stage1: '1. Parse', stage2: '2. Segment', stage3: '3. Translate',
    stage4: '4. Build', stage5: '5. Validate',
    download: '⬇ Download result PDF',
    footer: 'Pipeline: PyMuPDF + OpenAI-compatible LLM',
    need_pdf: 'PDF file required',
    err_upload: 'Upload error: ',
    err_start: 'Failed to start pipeline',
    err_net: 'Network error: ',
    cancel_req: 'Cancel requested…',
    starting: 'Starting pipeline…',
    stage_running: 'Running «{stage}»…',
    done: '✓ Done — translation finished',
    error: '✗ Error — see log',
    cancelled: 'Cancelled',
    waiting_short: 'Waiting…',
    stat_stage: 'Stage: ', stat_segs: 'Segments: ', stat_ok: 'OK: ',
    stat_cached: 'Cached: ', stat_fail: 'Errors: ',
    stat_pages: 'Pages: ', stat_images: 'Images: ',
    stat_status: 'Status: ',
    pipeline_err: 'Pipeline finished with error. See log/translate.log',
    stages: {parse:'Parsing PDF', segment:'Segmentation',
      translate:'LLM translation', build:'Building PDF', validate:'Validation'},
    states: {idle:'idle',running:'running',done:'done',
      error:'error',cancelled:'cancelled'},
  },
};
let LANG = localStorage.getItem('ui_lang') || 'ru';
function t(key, vars){
  let s = (I18N[LANG] && I18N[LANG][key]) || (I18N.ru[key]) || key;
  if (vars) for (const k in vars) s = s.replace('{'+k+'}', vars[k]);
  return s;
}
function applyI18n(){
  document.documentElement.lang = LANG;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    // сохраняем встроенные дочерние элементы (badge, ссылки)
    const child = el.querySelector('.badge, a');
    if (child){
      const childHtml = child.outerHTML;
      el.innerHTML = t(key) + childHtml;
    } else {
      el.textContent = t(key);
    }
  });
  // кнопки переключателя
  document.querySelectorAll('#langSwitch button').forEach(b => {
    b.classList.toggle('active', b.dataset.lang === LANG);
  });
  // обновить динамические части
  const job = currentJob;
  if (job && JOBS_STATE) update(JOBS_STATE);
}
document.querySelectorAll('#langSwitch button').forEach(b => {
  b.addEventListener('click', () => {
    LANG = b.dataset.lang;
    localStorage.setItem('ui_lang', LANG);
    applyI18n();
  });
});

let currentJob = null, pollTimer = null, selectedFile = null, JOBS_STATE = null;

fetch('/api/config').then(r=>r.json()).then(c=>{
  const src = c.source_lang||'?', tgt = c.target_lang||'?';
  $('#langBadge').textContent = src.toUpperCase()+' → '+tgt.toUpperCase();
  $('#langHint').textContent = src+' → '+tgt;
});

const drop = $('#drop'), fileInput = $('#file'), fileName = $('#fileName'),
      startBtn = $('#startBtn'), cancelBtn = $('#cancelBtn'),
      resetBtn = $('#resetBtn');

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
    alert(t('need_pdf')); return;
  }
  selectedFile = f;
  fileName.textContent = '📄 ' + f.name + '  (' + (f.size/1024/1024).toFixed(2) + ' MB)';
  startBtn.disabled = false;
  resetBtn.disabled = false;
}

resetBtn.addEventListener('click', () => {
  if (!selectedFile) return;
  $('#resetModal').classList.remove('hidden');
});
$('#resetCancelBtn').addEventListener('click', () => {
  $('#resetModal').classList.add('hidden');
});
// select-all
$('#selectAllStages').addEventListener('change', e => {
  document.querySelectorAll('#resetStages input[value]').forEach(cb => {
    cb.checked = e.target.checked;
  });
});
document.querySelectorAll('#resetStages input[value]').forEach(cb => {
  cb.addEventListener('change', () => {
    const all = document.querySelectorAll('#resetStages input[value]');
    const checked = document.querySelectorAll('#resetStages input[value]:checked');
    $('#selectAllStages').checked = all.length === checked.length;
  });
});
$('#resetConfirmBtn').addEventListener('click', async () => {
  const stages = Array.from(document.querySelectorAll(
    '#resetStages input[value]:checked')).map(cb => cb.value);
  if (!stages.length){ alert(t('reset_none')); return; }
  $('#resetConfirmBtn').disabled = true;
  try {
    const fd = new FormData(); fd.append('file', selectedFile);
    fd.append('stages', stages.join(','));
    const r = await fetch('/api/reset', {method:'POST', body:fd});
    if (!r.ok){ const txt = await r.text(); alert(t('reset_err')+txt); return; }
    const data = await r.json();
    $('#resetModal').classList.add('hidden');
    alert(t('reset_done', {items: (data.removed||[]).join(', ') || stages.join(', ')}));
  } catch(e) { alert(t('reset_err')+e); }
  finally { $('#resetConfirmBtn').disabled = false; }
});

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
  $('#bannerText').textContent = t('starting');
  $('#dotAnim').classList.remove('hidden');
  $('#downloadBox').classList.add('hidden');
  startTimer();

  const fd = new FormData(); fd.append('file', selectedFile);
  try{
    const r = await fetch('/api/upload', {method:'POST', body:fd});
    if (!r.ok){ const txt = await r.text(); log(t('err_upload')+txt, 'err'); return; }
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
    if (!r2.ok){ log(t('err_start'), 'err'); return; }
    currentJob = data.job_id;
    cancelBtn.disabled = false;
    pollStatus();
  }catch(e){ log(t('err_net')+e, 'err'); }
});

cancelBtn.addEventListener('click', async () => {
  if (!currentJob) return;
  await fetch('/api/cancel?job='+currentJob, {method:'POST'});
  log(t('cancel_req'), 'warn');
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
  JOBS_STATE = d;
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
    btext.textContent = t('stage_running', {stage: t('stages.'+d.stage) || (I18N[LANG].stages[d.stage]||d.stage)});
    dotAnim.classList.remove('hidden');
  } else if (d.state === 'done'){
    btext.textContent = t('done');
    dotAnim.classList.add('hidden');
    banner.classList.add('ok');
  } else if (d.state === 'error'){
    btext.textContent = t('error');
    dotAnim.classList.add('hidden');
    banner.classList.add('err');
  } else if (d.state === 'cancelled'){
    btext.textContent = t('cancelled'); dotAnim.classList.add('hidden');
  } else {
    btext.textContent = t('waiting_short'); dotAnim.classList.add('hidden');
    banner.classList.add('idle');
  }

  const stats = [];
  if (d.stage) stats.push('<span>'+t('stat_stage')+'<b>'+(I18N[LANG].stages[d.stage]||d.stage)+'</b></span>');
  if (d.total > 0) stats.push('<span>'+t('stat_segs')+'<b>'+d.progress+' / '+d.total+'</b></span>');
  if (d.ok>=0) stats.push('<span>'+t('stat_ok')+'<b>'+d.ok+'</b></span>');
  if (d.cached>=0) stats.push('<span>'+t('stat_cached')+'<b>'+d.cached+'</b></span>');
  if (d.fail>=0) stats.push('<span>'+t('stat_fail')+'<b>'+d.fail+'</b></span>');
  if (d.pages) stats.push('<span>'+t('stat_pages')+'<b>'+d.pages+'</b></span>');
  if (d.images) stats.push('<span>'+t('stat_images')+'<b>'+d.images+'</b></span>');
  stats.push('<span>'+t('stat_status')+'<b>'+(I18N[LANG].states[d.state]||d.state)+'</b></span>');
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
    log(t('pipeline_err'), 'err');
  }
}

applyI18n();
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
    tgt = cfg.get("target_lang", "ru").upper()
    stem = Path(src_path).stem
    # срезаем job_id-префикс, добавленный при upload
    m = re.match(r"^[0-9a-f]{12}_(.+)$", stem)
    clean_stem = m.group(1) if m else stem
    # временно — переведённое имя подставится после успеха конвейера
    out_name = f"{clean_stem}_{tgt}.pdf"
    return {
        "job_id": uuid.uuid4().hex[:12],
        "src": src_path,
        "out_path": str(ROOT / "uploads" / out_name),
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
        else:
            # Переименуем результат в переведённое оригинальное имя,
            # если файл создан (даже при провале validate — файл годный).
            out_p = Path(job["out_path"])
            if out_p.exists():
                try:
                    cfg = load_config()
                    tgt = cfg.get("target_lang", "ru").upper()
                    src_stem = Path(job["src"]).stem
                    translated = translate_filename_stem(src_stem, cfg)
                    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", translated)
                    safe = re.sub(r"\s+", " ", safe).strip()
                    if len(safe) > 80:
                        safe = safe[:80].rsplit(" ", 1)[0].rstrip()
                    if safe:
                        new_name = f"{safe}_{tgt}.pdf"
                        new_path = out_p.with_name(new_name)
                        if new_path != out_p:
                            if new_path.exists():
                                new_path.unlink()
                            out_p.rename(new_path)
                            job["out_path"] = str(new_path)
                            job["logs"].append(
                                f"[rename] {out_p.name} -> {new_path.name}")
                        out_p = new_path
                except Exception as e:
                    job["logs"].append(f"[rename skipped] {e}")
                job["result_path"] = str(out_p)
            if rc == 0:
                job["state"] = "done"
                job["stage"] = "validate"
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


@app.post("/api/reset")
async def reset_cache(file: UploadFile = File(...),
                      stages: str = ""):
    """Удаляет указанные артефакты для данного PDF."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Нужен PDF-файл")
    job_id = uuid.uuid4().hex[:12]
    safe = f"{job_id}_{Path(file.filename).name}"
    dest = UPLOADS / safe
    with open(dest, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    try:
        cfg = load_config()
        sh = source_hash(str(dest))
        wd = workdir(cfg, sh)
        stage_files = {
            "parse": ["parse.json"],
            "segment": ["segments.json"],
            "translate": ["segments_ru.json", "translations.db"],
        }
        requested = [s.strip() for s in stages.split(",") if s.strip()]
        if not requested:
            requested = list(stage_files.keys())
        removed = []
        for stage in requested:
            for item in stage_files.get(stage, []):
                p = wd / item
                if p.exists():
                    p.unlink()
                    removed.append(item)
        try:
            wd.rmdir()
        except OSError:
            pass
        return {"job_id": job_id, "path": str(dest), "removed": removed,
                "stages": requested}
    except Exception as e:
        return JSONResponse({"job_id": job_id, "path": str(dest),
                             "error": str(e)}, status_code=500)


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