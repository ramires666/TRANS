"""Веб-интерфейс (FastAPI) для перевода PDF.

Запуск: python -m app.web  -> http://127.0.0.1:8765
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import uvicorn

from pipeline.config.loader import ROOT, configure_target_language, load_config
from pipeline.io.artifacts import source_hash, workdir
from pipeline.translate.translator import translate_filename_stem

UPLOADS = ROOT / "uploads"
UPLOADS.mkdir(exist_ok=True)

app = FastAPI(title="PDF translator")
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
IMAGE_POST_SEMAPHORE = threading.Semaphore(1)

# ---------------------------- HTML ----------------------------
HTML_PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF переводчик</title>
<style>
  :root{
    color-scheme:light;
    --bg:#f5f5f3; --panel:#ffffff; --panel2:#f8f8f7; --border:#dededb;
    --border-strong:#c8c8c4; --text:#181817; --muted:#6f6f6a;
    --accent:#2457d6; --accent-hover:#1d46ad; --accent-soft:#eef3ff;
    --accent2:#18794e; --success-soft:#edf8f2; --warn:#9a6700;
    --warn-soft:#fff8e6; --err:#c93c37; --error-soft:#fff1f0;
    --divider:#e8e8e5; --switch-bg:#ececea; --track:#e9e9e6;
    --brand-bg:#181817; --brand-fg:#ffffff; --log-bg:#1e1e1c;
    --log-border:#30302d; --log-text:#d8d8d2; --overlay:rgba(24,24,23,.48);
    --shadow:0 1px 2px rgba(24,24,23,.04),0 12px 32px rgba(24,24,23,.06);
  }
  html[data-theme="dark"]{
    color-scheme:dark;
    --bg:#121310; --panel:#1b1c19; --panel2:#22231f; --border:#34352f;
    --border-strong:#4b4c45; --text:#f1f1ec; --muted:#aaa9a1;
    --accent:#7ea2ff; --accent-hover:#94b2ff; --accent-soft:#202c49;
    --accent2:#70c99a; --success-soft:#193126; --warn:#e2b85c;
    --warn-soft:#362d18; --err:#ff8e87; --error-soft:#3a2220;
    --divider:#30312c; --switch-bg:#242520; --track:#34352f;
    --brand-bg:#f1f1ec; --brand-fg:#181817; --log-bg:#10110f;
    --log-border:#2b2c27; --log-text:#deded7; --overlay:rgba(0,0,0,.66);
    --shadow:0 1px 2px rgba(0,0,0,.2),0 16px 40px rgba(0,0,0,.22);
  }
  *{box-sizing:border-box}
  html{background:var(--bg)}
  html,body{margin:0;padding:0;color:var(--text);
    font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  body{min-height:100vh;display:flex;flex-direction:column;align-items:center}
  .wrap{width:min(980px,calc(100% - 32px));padding:52px 0 36px}
  .topbar{display:flex;align-items:center;justify-content:space-between;
    gap:20px;margin-bottom:8px}
  .header-actions{display:flex;align-items:center;gap:8px;flex-shrink:0}
  .brand{display:flex;align-items:center;gap:12px;min-width:0}
  .brand-mark{width:38px;height:38px;display:grid;place-items:center;flex:0 0 auto;
    border-radius:10px;background:var(--brand-bg);color:var(--brand-fg)}
  .brand-mark svg{width:20px;height:20px}
  h1{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
    font-size:1.45rem;line-height:1.2;margin:0;letter-spacing:-.025em;font-weight:680}
  .lang-switch{display:inline-flex;background:var(--switch-bg);border:1px solid var(--border);
    border-radius:8px;padding:3px;gap:2px;flex-shrink:0}
  .lang-switch button{min-height:30px;padding:4px 11px;border-radius:6px;
    background:transparent;color:var(--muted);font-weight:650;font-size:.75rem;
    line-height:1;border:0;box-shadow:none}
  .lang-switch button:hover:not(:disabled){background:var(--panel);color:var(--text)}
  .lang-switch button.active{background:var(--panel);color:var(--text);
    box-shadow:0 1px 2px rgba(24,24,23,.09)}
  .theme-toggle{width:38px;height:38px;min-height:38px;display:grid;place-items:center;
    padding:0;border-color:var(--border);background:var(--panel);color:var(--text)}
  .theme-toggle:hover:not(:disabled){background:var(--panel2);border-color:var(--border-strong)}
  .theme-toggle svg{width:17px;height:17px}
  .theme-toggle .moon{display:none}
  html[data-theme="dark"] .theme-toggle .sun{display:none}
  html[data-theme="dark"] .theme-toggle .moon{display:block}
  .hero-copy{margin:24px 0 28px}
  .hero-copy h2{margin:0;font-size:clamp(1.65rem,3vw,2.35rem);line-height:1.12;
    letter-spacing:-.04em;font-weight:690}
  .hero-copy .sub{color:var(--muted);margin:9px 0 0;padding:0;
    font-size:.91rem;line-height:1.5}
  .sub{color:var(--muted);font-size:.91rem;line-height:1.5}
  .card{background:var(--panel);border:1px solid var(--border);
    border-radius:14px;padding:24px;margin-bottom:16px;box-shadow:var(--shadow)}
  .drop{border:1px dashed var(--border-strong);border-radius:10px;padding:38px 24px;
    text-align:center;cursor:pointer;transition:border-color .16s,background .16s;
    background:var(--panel2);outline:none}
  .drop:hover,.drop.over{border-color:var(--accent);background:var(--accent-soft)}
  .drop:focus-visible{border-color:var(--accent);box-shadow:0 0 0 3px rgba(36,87,214,.15)}
  .drop .big{width:44px;height:44px;display:grid;place-items:center;margin:0 auto 14px;
    color:var(--accent);background:var(--panel);border:1px solid var(--border);
    border-radius:10px;box-shadow:0 1px 2px rgba(24,24,23,.06)}
  .drop .big svg{width:22px;height:22px}
  .drop p{margin:4px 0;color:var(--muted);font-size:.91rem}
  .drop p b{color:var(--text);font-weight:640}
  .drop .sub{padding:0;font-size:.8rem;letter-spacing:.04em;text-transform:uppercase}
  .file-name{display:flex;align-items:center;margin-top:12px;padding:10px 12px;
    border:1px solid var(--border);border-radius:8px;background:var(--panel2);
    font-size:.86rem;font-weight:550;color:var(--text);word-break:break-all}
  .file-name:empty{display:none}
  .file-name::before{content:"PDF";flex:0 0 auto;margin-right:9px;padding:3px 5px;
    border-radius:4px;background:var(--error-soft);color:var(--err);
    font-size:.64rem;font-weight:750;letter-spacing:.04em}
  input[type=file]{display:none}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:20px}
  button{min-height:40px;font:inherit;padding:9px 16px;border-radius:8px;
    border:1px solid var(--accent);cursor:pointer;background:var(--accent);color:#fff;
    font-size:.88rem;font-weight:630;transition:background .15s,border-color .15s,
    color .15s,box-shadow .15s,transform .15s}
  button:hover:not(:disabled){background:var(--accent-hover);border-color:var(--accent-hover)}
  button:active:not(:disabled){transform:translateY(1px)}
  button:focus-visible{outline:0;box-shadow:0 0 0 3px rgba(36,87,214,.18)}
  button:disabled{opacity:.42;cursor:not-allowed}
  button.ghost{background:var(--panel);color:var(--text);border-color:var(--border-strong)}
  button.ghost:hover:not(:disabled){background:var(--panel2);border-color:var(--border-strong)}
  button#resetBtn{margin-left:auto;color:var(--err);border-color:var(--border-strong)}
  button#resetBtn:hover:not(:disabled){background:var(--error-soft);border-color:var(--err)}
  .opts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 16px;
    color:var(--muted);font-size:.86rem;margin-top:18px;padding:18px 0 0;
    border-top:1px solid var(--divider)}
  .opts label{display:flex;min-height:34px;align-items:center;gap:8px;cursor:pointer}
  input[type=checkbox]{width:16px;height:16px;margin:0;accent-color:var(--accent)}
  input[type=number],select{height:34px;background:var(--panel);border:1px solid
    var(--border-strong);color:var(--text);border-radius:7px;padding:5px 9px;
    font:inherit;font-size:.84rem;outline:none}
  .opts input[type=number]{width:82px;margin-left:auto}
  .opts select{width:min(220px,100%);margin-left:auto}
  input[type=number]:focus,select:focus{border-color:var(--accent);
    box-shadow:0 0 0 3px rgba(36,87,214,.12)}
  .stage-list{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:18px 0 8px}
  .stage{min-width:0;background:var(--panel2);border:1px solid var(--border);
    border-radius:8px;padding:10px;font-size:.73rem;line-height:1.4;
    text-align:left;color:var(--muted);transition:.18s}
  .stage .t{font-weight:650;color:var(--text);display:block;margin-bottom:2px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .stage.active{border-color:var(--accent);background:var(--accent-soft);color:var(--accent);
    box-shadow:inset 3px 0 0 var(--accent)}
  .stage.done{border-color:var(--accent2);background:var(--success-soft);color:var(--accent2)}
  .stage.err{border-color:var(--err);background:var(--error-soft);color:var(--err)}
  .stage .spin{display:inline-block;width:12px;height:12px;border:2px solid
    currentColor;border-top-color:transparent;border-radius:50%;
    animation:spin .8s linear infinite;vertical-align:-2px;margin-left:5px}
  .stage.done .spin{display:none}
  @keyframes spin{to{transform:rotate(360deg)}}
  .prog-wrap{background:var(--track);border-radius:999px;height:7px;
    margin:34px 0 18px;position:relative}
  .prog{height:100%;width:0;background:var(--accent);border-radius:inherit;
    transition:width .35s ease;position:relative}
  .prog.running::after{content:"";position:absolute;inset:0;
    background:rgba(255,255,255,.22);border-radius:inherit;
    animation:pulse 1.4s ease-in-out infinite}
  @keyframes pulse{50%{opacity:.25}}
  .prog.indeterminate{width:35% !important;animation:slide 1.4s ease-in-out infinite}
  @keyframes slide{0%{margin-left:-35%}50%{margin-left:100%}100%{margin-left:-35%}}
  .prog-pct{position:absolute;right:0;top:-25px;font-size:.76rem;font-weight:650;
    color:var(--muted);font-variant-numeric:tabular-nums}
  .banner{display:flex;align-items:center;gap:10px;margin:6px 0 4px;
    padding:12px 14px;background:var(--panel2);border:1px solid var(--border);
    border-radius:8px;font-size:.88rem;font-weight:620}
  .banner .dot-anim{display:inline-flex;gap:3px}
  .banner .dot-anim span{width:5px;height:5px;border-radius:50%;background:var(--accent);
    animation:bounce 1.2s infinite ease-in-out}
  .banner .dot-anim span:nth-child(2){animation-delay:.15s}
  .banner .dot-anim span:nth-child(3){animation-delay:.3s}
  @keyframes bounce{0%,80%,100%{transform:scale(.5);opacity:.5}
    40%{transform:scale(1);opacity:1}}
  .banner .timer{margin-left:auto;color:var(--muted);
    font-variant-numeric:tabular-nums;font-weight:500;font-size:.85rem}
  .banner.ok{border-color:var(--accent2);background:var(--success-soft);color:var(--accent2)}
  .banner.err{border-color:var(--err);background:var(--error-soft);color:var(--err)}
  .banner.idle{color:var(--muted)}
  .log{background:var(--log-bg);border:1px solid var(--log-border);border-radius:8px;
    padding:14px;height:220px;overflow:auto;font-family:"SFMono-Regular",Consolas,
    "Liberation Mono",monospace;font-size:.78rem;line-height:1.55;color:var(--log-text);
    white-space:pre-wrap;margin-top:14px;scrollbar-color:var(--muted) var(--log-bg)}
  .log .err{color:#ff8f89}
  .log .ok{color:#73c99b}
  .log .warn{color:#e7bd61}
  .stats{display:flex;gap:8px;flex-wrap:wrap;color:var(--muted);
    font-size:.79rem;margin-top:8px}
  .stats span{padding:5px 8px;border-radius:6px;background:var(--panel2);
    border:1px solid var(--divider)}
  .stats span b{color:var(--text)}
  .download{margin-top:18px;padding-top:18px;border-top:1px solid var(--divider)}
  .result-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .result-actions a{display:inline-flex;text-decoration:none}
  .image-post{margin-top:18px;padding:18px;background:var(--panel2);
    border:1px solid var(--border);border-radius:10px}
  .image-post h3{margin:0 0 6px;font-size:.95rem;letter-spacing:-.01em}
  .image-post .hint{margin:0;color:var(--muted);font-size:.82rem;line-height:1.5}
  .image-post .log{height:120px;margin-top:10px}
  .image-post .prog-wrap{margin:34px 0 18px}
  .badge{font-size:.65rem;padding:4px 7px;border-radius:5px;
    background:var(--accent-soft);border:1px solid var(--accent);color:var(--accent);
    letter-spacing:.045em;font-weight:700;vertical-align:middle}
  .hidden{display:none}
  footer{color:var(--muted);font-size:.74rem;margin-top:24px;text-align:center}
  a{color:var(--accent);text-underline-offset:2px}
  .modal-overlay{position:fixed;inset:0;background:var(--overlay);
    display:flex;align-items:center;justify-content:center;z-index:1000;padding:20px}
  .modal-overlay.hidden{display:none}
  .modal{background:var(--panel);border:1px solid var(--border);
    border-radius:14px;padding:24px;width:min(480px,100%);
    box-shadow:0 24px 80px rgba(24,24,23,.22)}
  .modal h3{margin:0 0 8px;font-size:1.05rem;letter-spacing:-.015em}
  .modal-sub{color:var(--muted);font-size:.83rem;line-height:1.5;margin:0 0 18px}
  .reset-stages{display:flex;flex-direction:column;gap:8px;margin-bottom:20px}
  .reset-stages label{display:flex;align-items:center;gap:8px;cursor:pointer;
    font-size:.85rem;padding:10px 12px;background:var(--panel2);
    border:1px solid var(--border);border-radius:8px}
  .reset-stages label:hover{border-color:var(--accent)}
  .reset-stages code{margin-left:auto;color:var(--muted);font-size:.72rem}
  .reset-stages .select-all{border-color:var(--accent);background:var(--accent-soft)}
  .modal-row{display:flex;gap:10px;justify-content:flex-end}
  .modal-row button{padding:8px 14px}

  @media (max-width:760px){
    .wrap{width:min(100% - 24px,980px);padding-top:28px}
    .hero-copy{margin-top:20px}
    .card{padding:18px}
    .drop{padding:30px 16px}
    .opts{grid-template-columns:1fr}
    .stage-list{grid-template-columns:repeat(2,1fr)}
    .stage:last-child{grid-column:1/-1}
  }
  @media (max-width:480px){
    .topbar{align-items:flex-start}
    .header-actions{gap:6px}
    .brand-mark{width:34px;height:34px}
    h1{font-size:1.22rem}
    .lang-switch button{padding-inline:9px}
    .card{padding:14px;border-radius:12px}
    .drop{padding:26px 12px}
    .row>button{flex:1}
    button#resetBtn{margin-left:0;flex-basis:100%}
    .stage-list{grid-template-columns:1fr}
    .stage:last-child{grid-column:auto}
    .modal-row{flex-direction:column-reverse}
    .modal-row button{width:100%}
  }
  @media (prefers-reduced-motion:reduce){
    *,*::before,*::after{scroll-behavior:auto!important;animation-duration:.01ms!important;
      animation-iteration-count:1!important;transition-duration:.01ms!important}
  }

</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
          <path d="M7.5 3.75h6l3 3v13.5h-9z"/>
          <path d="M13.5 3.75v3h3M9.75 11h4.5M9.75 14h4.5M9.75 17h3"/>
        </svg>
      </div>
      <h1 data-i18n="title">PDF переводчик <span class="badge" id="langBadge">… → …</span></h1>
    </div>
    <div class="header-actions">
      <button class="theme-toggle" id="themeToggle" type="button"
              aria-label="Переключить тему" title="Переключить тему">
        <svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
          <circle cx="12" cy="12" r="3.5"/>
          <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M18.7 5.3l-1.4 1.4M6.7 17.3l-1.4 1.4"/>
        </svg>
        <svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
          <path d="M19.2 15.2A8 8 0 0 1 8.8 4.8a8 8 0 1 0 10.4 10.4z"/>
        </svg>
      </button>
      <div class="lang-switch" id="langSwitch" aria-label="Язык перевода">
        <button data-lang="ru" class="active" type="button">RU</button>
        <button data-lang="en" type="button">EN</button>
      </div>
    </div>
  </div>
  <div class="hero-copy">
    <h2 data-i18n="feature_title">Технический перевод без слепых зон.</h2>
    <p class="sub" data-i18n="subtitle">Переводит весь документ, сохраняя исходное форматирование и структуру. Даже текст на изображениях.</p>
  </div>

  <div class="card">
    <div class="drop" id="drop" tabindex="0" role="button">
      <div class="big" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
          <path d="M12 16V4M8 8l4-4 4 4"/>
          <path d="M5 13v5.25A1.75 1.75 0 0 0 6.75 20h10.5A1.75 1.75 0 0 0 19 18.25V13"/>
        </svg>
      </div>
      <p><b data-i18n="drop_title">Перетащите PDF сюда</b> <span data-i18n="drop_or">или нажмите для выбора</span></p>
      <p class="sub" style="margin:0" id="langHint">исходный → целевой</p>
      <input type="file" id="file" accept="application/pdf">
    </div>
    <div class="file-name" id="fileName"></div>

    <div class="opts">
      <label><input type="checkbox" id="resume" checked> <span data-i18n="resume">Продолжить с сохранённых этапов</span></label>
      <label><input type="checkbox" id="fromTranslate"> <span data-i18n="translate_only">Начать с этапа перевода</span></label>
      <label><span data-i18n="limit">Ограничение сегментов:</span> <input type="number" id="limit" min="0" value="0" title="0 = все"></label>
      <label><span data-i18n="mode">Способ обработки:</span>
        <select id="mode">
          <option value="pipeline" data-i18n="mode_pipeline">По сегментам</option>
          <option value="markdown" data-i18n="mode_markdown">Постранично</option>
        </select>
      </label>
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
      <div class="stage" data-s="parse"><span class="t" data-i18n="stage1">1. Извлечение</span><span data-i18n="stage1_hint">Структура PDF</span></div>
      <div class="stage" data-s="segment"><span class="t" data-i18n="stage2">2. Разметка</span><span data-i18n="stage2_hint">Текстовые блоки</span></div>
      <div class="stage" data-s="translate"><span class="t" data-i18n="stage3">3. Перевод</span><span data-i18n="stage3_hint">Модель и кэш</span></div>
      <div class="stage" data-s="build"><span class="t" data-i18n="stage4">4. Сборка</span><span data-i18n="stage4_hint">Итоговый PDF</span></div>
      <div class="stage" data-s="validate"><span class="t" data-i18n="stage5">5. Проверка</span><span data-i18n="stage5_hint">Контроль качества</span></div>
    </div>
    <div class="prog-wrap">
      <div class="prog" id="prog"></div>
      <div class="prog-pct" id="progPct">0%</div>
    </div>
    <div class="stats" id="stats"></div>
    <div class="log" id="log"></div>
    <div class="download hidden" id="downloadBox">
      <div class="result-actions">
        <a id="previewLink" href="#" target="_blank" rel="noopener"><button class="ghost" data-i18n="preview_base">Просмотреть базовый PDF</button></a>
        <a id="downloadLink" href="#" download><button data-i18n="download">Скачать результат PDF</button></a>
      </div>
      <div class="image-post hidden" id="imagePostBox">
        <h3 data-i18n="image_title">Перевести текст внутри изображений</h3>
        <p class="hint" id="imagePostHint" data-i18n="image_preview_hint">Сначала просмотрите базовый PDF, затем запустите дополнительную обработку.</p>
        <div class="prog-wrap hidden" id="imageProgWrap">
          <div class="prog" id="imageProg"></div>
          <div class="prog-pct" id="imageProgPct">0%</div>
        </div>
        <div class="stats" id="imageStats"></div>
        <div class="row">
          <button id="imagePostBtn" disabled data-i18n="image_start">Обработать изображения</button>
          <button id="imageCancelBtn" class="ghost" disabled data-i18n="image_cancel">Отменить обработку</button>
          <a id="imagePreviewLink" class="hidden" href="#" target="_blank" rel="noopener"><button class="ghost" data-i18n="image_preview">Просмотреть улучшенный PDF</button></a>
          <a id="imageDownloadLink" class="hidden" href="#" download><button data-i18n="image_download">Скачать улучшенный PDF</button></a>
        </div>
        <div class="log hidden" id="imageLog"></div>
      </div>
    </div>
  </div>

  <footer data-i18n="footer">Основа: PyMuPDF + OpenAI-совместимая модель · <a href="/api/health" target="_blank">/api/health</a></footer>
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
    title: 'Переводчик PDF',
    feature_title: 'Технический перевод без слепых зон.',
    subtitle: 'Переводит весь документ, сохраняя исходное форматирование и структуру. Даже текст на изображениях.',
    drop_title: 'Перетащите PDF сюда',
    drop_or: 'или выберите файл',
    lang_hint: 'китайский → русский',
    resume: 'Продолжить с сохранённых этапов',
    translate_only: 'Начать с этапа перевода',
    limit: 'Ограничение сегментов:',
    mode: 'Способ обработки:',
    mode_pipeline: 'По сегментам',
    mode_markdown: 'Постранично',
    start: 'Перевести на русский',
    cancel: 'Отмена',
    reset: 'Сбросить этапы',
    reset_title: 'Какие этапы выполнить заново?',
    reset_sub: 'Сохранённые результаты выбранных этапов будут удалены.',
    reset_all: 'Выбрать все',
    reset_confirm_btn: 'Сбросить выбранное',
    reset_cancel_btn: 'Отмена',
    reset_done: 'Сброшено: {items}.',
    reset_none: 'Не выбран ни один этап.',
    reset_err: 'Ошибка сброса: ',
    waiting: 'Ожидание запуска…',
    stage1: '1. Извлечение', stage2: '2. Разметка', stage3: '3. Перевод',
    stage4: '4. Сборка', stage5: '5. Проверка',
    stage1_hint: 'Структура PDF', stage2_hint: 'Текстовые блоки',
    stage3_hint: 'Модель и кэш', stage4_hint: 'Итоговый PDF',
    stage5_hint: 'Контроль качества',
    download: 'Скачать перевод',
    preview_base: 'Открыть перевод',
    image_title: 'Перевести надписи на изображениях',
    image_preview_hint: 'Сначала откройте основной перевод и проверьте результат.',
    image_ready_hint: 'Основной перевод просмотрен. Можно обработать изображения.',
    image_running_hint: 'Перевод надписей на изображениях выполняется…',
    image_done_hint: 'Версия с переведёнными изображениями готова. Основной файл сохранён.',
    image_error_hint: 'Не удалось обработать изображения. Основной перевод доступен.',
    image_cancelled_hint: 'Обработка изображений отменена. Основной перевод не изменён.',
    image_start: 'Перевести изображения',
    image_retry: 'Повторить',
    image_cancel: 'Остановить',
    image_preview: 'Открыть версию с изображениями',
    image_download: 'Скачать версию с изображениями',
    image_start_error: 'Не удалось запустить обработку изображений: ',
    image_cancel_req: 'Останавливаю обработку изображений…',
    image_phase: 'Этап: ',
    footer: 'Основа: PyMuPDF + OpenAI-совместимая модель · ',
    theme_light: 'Включить светлую тему',
    theme_dark: 'Включить тёмную тему',
    language_label: 'Язык перевода',
    limit_title: '0 — перевести все сегменты',
    need_pdf: 'Нужен PDF-файл',
    err_upload: 'Ошибка загрузки: ',
    err_start: 'Не удалось запустить перевод',
    err_net: 'Сетевая ошибка: ',
    cancel_req: 'Останавливаю перевод…',
    starting: 'Подготовка к переводу…',
    stage_running: 'Идёт «{stage}»…',
    done: 'Готово — перевод завершён',
    error: 'Ошибка — подробности в журнале',
    cancelled: 'Отменено',
    waiting_short: 'Ожидание…',
    stat_stage: 'Этап: ', stat_segs: 'Сегментов: ', stat_ok: 'Успешно: ',
    stat_cached: 'Из кэша: ', stat_fail: 'Ошибок: ',
    stat_pages: 'Страниц: ', stat_images: 'Изображений: ',
    stat_status: 'Статус: ',
    pipeline_err: 'Перевод завершился с ошибкой. Подробности указаны в журнале.',
    stages: {parse:'Извлечение структуры', segment:'Разметка текста',
      translate:'Перевод текста', build:'Сборка PDF', validate:'Проверка результата'},
    states: {idle:'ожидание',queued:'в очереди',running:'выполняется',done:'готово',
      error:'ошибка',cancelled:'отменено'},
  },
  en: {
    title: 'PDF translator',
    feature_title: 'Technical translation with no blind spots.',
    subtitle: 'It translates the entire document while preserving its original formatting and structure — even text embedded in images.',
    drop_title: 'Drop PDF here',
    drop_or: 'or choose a file',
    lang_hint: 'Chinese → English',
    resume: 'Continue from saved stages',
    translate_only: 'Start from translation',
    limit: 'Segment limit:',
    mode: 'Processing method:',
    mode_pipeline: 'Segment by segment',
    mode_markdown: 'Page by page',
    start: 'Translate to English',
    cancel: 'Cancel',
    reset: 'Reset stages',
    reset_title: 'Which stages should run again?',
    reset_sub: 'Saved results for the selected stages will be removed.',
    reset_all: 'Select all',
    reset_confirm_btn: 'Reset selected stages',
    reset_cancel_btn: 'Cancel',
    reset_done: 'Reset: {items}.',
    reset_none: 'No stage selected.',
    reset_err: 'Reset error: ',
    waiting: 'Waiting to start…',
    stage1: '1. Extract', stage2: '2. Structure', stage3: '3. Translate',
    stage4: '4. Build', stage5: '5. Review',
    stage1_hint: 'PDF structure', stage2_hint: 'Text blocks',
    stage3_hint: 'Model and cache', stage4_hint: 'Translated PDF',
    stage5_hint: 'Quality checks',
    download: 'Download translation',
    preview_base: 'Open translation',
    image_title: 'Translate text in embedded images',
    image_preview_hint: 'Open and review the main translation before processing images.',
    image_ready_hint: 'The main translation has been reviewed. Image processing is available.',
    image_running_hint: 'Translating text in embedded images…',
    image_done_hint: 'The image-translated version is ready. The main file is unchanged.',
    image_error_hint: 'Image processing failed. The main translation is still available.',
    image_cancelled_hint: 'Image processing was cancelled. The main translation is unchanged.',
    image_start: 'Translate images',
    image_retry: 'Try again',
    image_cancel: 'Stop',
    image_preview: 'Open image-translated version',
    image_download: 'Download image-translated version',
    image_start_error: 'Could not start image processing: ',
    image_cancel_req: 'Stopping image processing…',
    image_phase: 'Stage: ',
    footer: 'Built with PyMuPDF and an OpenAI-compatible model · ',
    theme_light: 'Switch to light theme',
    theme_dark: 'Switch to dark theme',
    language_label: 'Translation language',
    limit_title: '0 translates all segments',
    need_pdf: 'PDF file required',
    err_upload: 'Upload error: ',
    err_start: 'Failed to start translation',
    err_net: 'Network error: ',
    cancel_req: 'Stopping translation…',
    starting: 'Preparing translation…',
    stage_running: 'Running «{stage}»…',
    done: 'Done — translation finished',
    error: 'Error — see the processing log',
    cancelled: 'Cancelled',
    waiting_short: 'Waiting…',
    stat_stage: 'Stage: ', stat_segs: 'Segments: ', stat_ok: 'Completed: ',
    stat_cached: 'Cached: ', stat_fail: 'Errors: ',
    stat_pages: 'Pages: ', stat_images: 'Images: ',
    stat_status: 'Status: ',
    pipeline_err: 'Translation failed. See the processing log for details.',
    stages: {parse:'Extracting structure', segment:'Structuring text',
      translate:'Translating text', build:'Building PDF', validate:'Reviewing output'},
    states: {idle:'idle',queued:'queued',running:'running',done:'done',
      error:'error',cancelled:'cancelled'},
  },
};
let LANG = localStorage.getItem('ui_lang') || 'ru';
if (!['ru','en'].includes(LANG)) LANG = 'ru';
let SOURCE_LANG = 'zh';
let THEME = localStorage.getItem('ui_theme');
if (!['light','dark'].includes(THEME)){
  THEME = window.matchMedia &&
    window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
document.documentElement.dataset.theme = THEME;

function targetLang(){
  return LANG === 'en' ? 'en' : 'ru';
}
function t(key, vars){
  let s = (I18N[LANG] && I18N[LANG][key]) || (I18N.ru[key]) || key;
  if (vars) for (const k in vars) s = s.replace('{'+k+'}', vars[k]);
  return s;
}
function updateLanguagePair(){
  const target = targetLang();
  $('#langBadge').textContent = SOURCE_LANG.toUpperCase()+' → '+target.toUpperCase();
  $('#langHint').textContent = LANG === 'en'
    ? 'Chinese → English'
    : 'китайский → русский';
}
function updateThemeControl(){
  const label = THEME === 'dark' ? t('theme_light') : t('theme_dark');
  const button = $('#themeToggle');
  button.setAttribute('aria-label', label);
  button.title = label;
}
function applyI18n(){
  document.documentElement.lang = LANG;
  document.title = t('title');
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
  $('#langSwitch').setAttribute('aria-label', t('language_label'));
  $('#limit').title = t('limit_title');
  drop?.setAttribute('aria-label', t('drop_title')+' — '+t('drop_or'));
  updateLanguagePair();
  updateThemeControl();
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
$('#themeToggle').addEventListener('click', () => {
  THEME = THEME === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = THEME;
  localStorage.setItem('ui_theme', THEME);
  updateThemeControl();
});

let currentJob = null, pollTimer = null, selectedFile = null, JOBS_STATE = null;
let imagePostAvailable = false, basePreviewed = false;

fetch('/api/config').then(r=>r.json()).then(c=>{
  SOURCE_LANG = c.source_lang || 'zh';
  updateLanguagePair();
  imagePostAvailable = !!(c.image_postprocess && c.image_postprocess.available);
  if (JOBS_STATE) updateImagePost(JOBS_STATE.image_post);
});

const drop = $('#drop'), fileInput = $('#file'), fileName = $('#fileName'),
      startBtn = $('#startBtn'), cancelBtn = $('#cancelBtn'),
      resetBtn = $('#resetBtn'), imagePostBtn = $('#imagePostBtn'),
      imageCancelBtn = $('#imageCancelBtn');

drop.addEventListener('click', () => fileInput.click());
drop.addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' '){
    e.preventDefault();
    fileInput.click();
  }
});
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
  fileName.textContent = f.name + '  ·  ' + (f.size/1024/1024).toFixed(2) + ' MB';
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
  resetImagePostUi();
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
      mode: $('#mode').value,
      target_lang: targetLang(),
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

$('#previewLink').addEventListener('click', () => {
  basePreviewed = true;
  if (JOBS_STATE) updateImagePost(JOBS_STATE.image_post || {state:'idle'});
});

imagePostBtn.addEventListener('click', async () => {
  if (!currentJob || !basePreviewed || !imagePostAvailable) return;
  imagePostBtn.disabled = true;
  imageCancelBtn.disabled = false;
  $('#imageLog').classList.remove('hidden');
  $('#imageLog').textContent = '';
  $('#imageLog').dataset.seen = '0';
  try {
    const r = await fetch('/api/jobs/'+encodeURIComponent(currentJob)+'/image-postprocess',
      {method:'POST'});
    const data = await r.json().catch(() => ({}));
    if (!r.ok){
      imageLog(t('image_start_error')+(data.detail || r.status), 'err');
      imagePostBtn.disabled = !basePreviewed;
      imageCancelBtn.disabled = true;
      return;
    }
    const queued = Object.assign({}, (JOBS_STATE && JOBS_STATE.image_post) || {},
      {state:'queued', progress:0, total:0, logs:[]});
    updateImagePost(queued);
    pollStatus();
  } catch(e) {
    imageLog(t('image_start_error')+e, 'err');
    imagePostBtn.disabled = !basePreviewed;
    imageCancelBtn.disabled = true;
  }
});

imageCancelBtn.addEventListener('click', async () => {
  if (!currentJob) return;
  await fetch('/api/jobs/'+encodeURIComponent(currentJob)+'/image-postprocess/cancel',
    {method:'POST'});
  imageLog(t('image_cancel_req'), 'warn');
});

function resetImagePostUi(){
  basePreviewed = false;
  $('#imagePostBox').classList.add('hidden');
  $('#imageProgWrap').classList.add('hidden');
  $('#imageProg').className = 'prog';
  $('#imageProg').style.width = '0%';
  $('#imageProgPct').textContent = '0%';
  $('#imageStats').textContent = '';
  $('#imageLog').textContent = '';
  $('#imageLog').dataset.seen = '0';
  $('#imageLog').classList.add('hidden');
  $('#imagePreviewLink').classList.add('hidden');
  $('#imageDownloadLink').classList.add('hidden');
  imagePostBtn.textContent = t('image_start');
  imagePostBtn.disabled = true;
  imageCancelBtn.disabled = true;
  $('#imagePostHint').textContent = t('image_preview_hint');
}

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

function imageLog(msg, cls){
  const el = $('#imageLog'); const span = document.createElement('span');
  if (cls) span.className = cls;
  span.textContent = msg + '\n';
  el.appendChild(span); el.scrollTop = el.scrollHeight;
}

function updateImagePost(p){
  const box = $('#imagePostBox');
  if (!imagePostAvailable || !JOBS_STATE || JOBS_STATE.state !== 'done'){
    box.classList.add('hidden'); return;
  }
  box.classList.remove('hidden');
  p = p || {state:'idle', progress:0, total:0, logs:[]};
  const state = p.state || 'idle';
  const active = state === 'queued' || state === 'running';
  const prog = $('#imageProg'), pct = $('#imageProgPct');
  prog.classList.remove('indeterminate','running');

  if (state === 'idle'){
    $('#imageProgWrap').classList.add('hidden');
  } else {
    $('#imageProgWrap').classList.remove('hidden');
    let percent = 0;
    if (state === 'done') percent = 100;
    else if ((p.total||0) > 0) percent = Math.min(100,
      Math.round((p.progress||0) / p.total * 100));
    prog.style.width = percent + '%';
    pct.textContent = percent + '%';
    if (active && !(p.total > 0)){
      prog.classList.add('indeterminate','running'); pct.textContent = '…';
    } else if (active) prog.classList.add('running');
  }

  imagePostBtn.disabled = active || state === 'done' || !basePreviewed;
  imageCancelBtn.disabled = !active;
  imagePostBtn.textContent = (state === 'error' || state === 'cancelled')
    ? t('image_retry') : t('image_start');
  if (active) $('#imagePostHint').textContent = t('image_running_hint');
  else if (state === 'done') $('#imagePostHint').textContent = t('image_done_hint');
  else if (state === 'error') $('#imagePostHint').textContent = t('image_error_hint');
  else if (state === 'cancelled') $('#imagePostHint').textContent = t('image_cancelled_hint');
  else $('#imagePostHint').textContent = basePreviewed
    ? t('image_ready_hint') : t('image_preview_hint');

  const details = [];
  if (p.phase) details.push(t('image_phase')+p.phase);
  if ((p.total||0) > 0) details.push((p.progress||0)+' / '+p.total);
  if ((p.ok||0) > 0) details.push('OK: '+p.ok);
  if ((p.cached||0) > 0) details.push(t('stat_cached')+p.cached);
  if ((p.failed||0) > 0) details.push(t('stat_fail')+p.failed);
  details.push(t('stat_status')+(I18N[LANG].states[state]||state));
  $('#imageStats').textContent = details.join(' · ');

  const seen = ($('#imageLog').dataset.seen||'0')|0;
  if (p.logs && p.logs.length > seen){
    $('#imageLog').classList.remove('hidden');
    for (let i = seen; i < p.logs.length; i++){
      const line = p.logs[i]; let cls = '';
      if (/ошибк|error|exception|fail/i.test(line)) cls = 'err';
      else if (/готово|done|success|ok=/i.test(line)) cls = 'ok';
      else if (/warn|предупр/i.test(line)) cls = 'warn';
      imageLog(line, cls);
    }
    $('#imageLog').dataset.seen = p.logs.length;
  }

  const imageReady = state === 'done' && p.result_ready;
  $('#imagePreviewLink').classList.toggle('hidden', !imageReady);
  $('#imageDownloadLink').classList.toggle('hidden', !imageReady);
  if (imageReady){
    const root = '/api/jobs/'+encodeURIComponent(currentJob)+'/result?variant=images';
    $('#imagePreviewLink').href = root+'&disposition=inline';
    $('#imageDownloadLink').href = root+'&disposition=attachment';
  }
}

function pollStatus(){
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!currentJob) return;
    try{
      const r = await fetch('/api/status?job='+currentJob);
      const d = await r.json();
      update(d);
      const imageActive = d.image_post &&
        (d.image_post.state === 'queued' || d.image_post.state === 'running');
      if ((d.state === 'done' || d.state === 'error' || d.state === 'cancelled') &&
          !imageActive){
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
    const root = '/api/jobs/'+encodeURIComponent(currentJob)+'/result?variant=base';
    $('#previewLink').href = root+'&disposition=inline';
    $('#downloadLink').href = root+'&disposition=attachment';
  }
  updateImagePost(d.image_post);
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
VISION_EVENT_PREFIX = "@@VISION@@"
RE_IMAGE_PROGRESS = re.compile(
    r"(?:progress|image|изображ\w*)[^\d]*(\d+)\s*/\s*(\d+)", re.IGNORECASE)
RE_IMAGE_COUNTS = re.compile(
    r"(?:ok|processed)=(\d+)(?:\s+cached=(\d+))?\s+(?:fail|failed|errors?)=(\d+)",
    re.IGNORECASE)
RE_IMAGE_PHASE = re.compile(r"(?:phase|этап)\s*[:=]\s*([\w.-]+)", re.IGNORECASE)


def _new_image_post_state() -> dict:
    return {
        "state": "idle", "phase": "", "progress": 0, "total": 0,
        "ok": 0, "cached": 0, "failed": 0, "logs": [],
        "proc": None, "cancel": False, "result_path": None,
        "base_path": None, "partial_path": None, "final_path": None,
        "download_name": None, "started_at": None, "finished_at": None,
    }


def _image_postprocess_capability(cfg: dict | None = None) -> tuple[bool, str]:
    """Return availability without treating a regular text model as vision."""
    try:
        cfg = cfg if cfg is not None else load_config()
    except Exception:
        return False, "configuration unavailable"
    if not str(cfg.get("vision_llm_model", "") or "").strip():
        return False, "vision_llm_model is not configured"
    try:
        importlib.import_module("pipeline.vision.image_overlay")
    except Exception:
        return False, "pipeline.vision.image_overlay is unavailable"
    return True, ""


def _resolve_upload_pdf(value: str | os.PathLike, *, must_exist: bool = True) -> Path:
    """Resolve a job-owned PDF and reject paths outside the upload directory."""
    uploads = UPLOADS.resolve()
    path = Path(value).resolve(strict=must_exist)
    try:
        path.relative_to(uploads)
    except ValueError as exc:
        raise ValueError("PDF path is outside the upload directory") from exc
    if path.suffix.lower() != ".pdf":
        raise ValueError("result is not a PDF")
    if must_exist and (not path.is_file() or path.stat().st_size == 0):
        raise ValueError("PDF file is missing or empty")
    return path


def _image_output_paths(job_id: str, base_pdf: Path) -> tuple[Path, Path, str]:
    safe_job = re.sub(r"[^0-9A-Za-z_-]", "_", str(job_id))[:32] or "job"
    base_stem = base_pdf.stem
    if base_stem.startswith(f"{safe_job}_"):
        base_stem = base_stem[len(safe_job) + 1:]
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base_stem)
    safe_stem = re.sub(r"\s+", " ", safe_stem).strip(" ._")[:100] or "translated"
    download_name = f"{safe_stem}_IMG.pdf"
    final_path = UPLOADS.resolve() / f"{safe_job}_{download_name}"
    partial_path = final_path.with_name(f"{final_path.stem}.partial.pdf")
    return final_path, partial_path, download_name


def _append_image_log_locked(image_post: dict, line: str) -> None:
    image_post["logs"].append(str(line))
    if len(image_post["logs"]) > 400:
        image_post["logs"] = image_post["logs"][-250:]


def _parse_image_post_line(job: dict, line: str) -> None:
    """Parse structured vision events, while retaining ordinary CLI output."""
    line = line.strip()
    if not line:
        return
    payload = None
    if line.startswith(VISION_EVENT_PREFIX):
        try:
            candidate = json.loads(line[len(VISION_EVENT_PREFIX):].strip())
            if isinstance(candidate, dict):
                payload = candidate
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None

    with JOBS_LOCK:
        image_post = job["image_post"]
        if payload is not None:
            phase = payload.get("phase") or payload.get("stage") or payload.get("event")
            if phase and str(phase).lower() not in {"progress", "log"}:
                image_post["phase"] = str(phase)[:80]
            number_fields = {
                "current": "progress", "progress": "progress", "total": "total",
                "ok": "ok", "processed": "ok", "cached": "cached",
                "failed": "failed", "fail": "failed", "errors": "failed",
            }
            for source_key, state_key in number_fields.items():
                if source_key in payload:
                    try:
                        image_post[state_key] = max(0, int(payload[source_key]))
                    except (TypeError, ValueError):
                        pass
            message = payload.get("message") or payload.get("log")
            if message:
                _append_image_log_locked(image_post, str(message))
            return

        _append_image_log_locked(image_post, line)
        match = RE_IMAGE_PROGRESS.search(line) or RE_TQDM.search(line)
        if match:
            # RE_TQDM has percentage as group 1, progress/total as groups 2/3.
            offset = 1 if match.re is RE_TQDM else 0
            image_post["progress"] = int(match.group(1 + offset))
            image_post["total"] = int(match.group(2 + offset))
        match = RE_IMAGE_COUNTS.search(line)
        if match:
            image_post["ok"] = int(match.group(1))
            image_post["cached"] = int(match.group(2) or 0)
            image_post["failed"] = int(match.group(3))
        match = RE_IMAGE_PHASE.search(line)
        if match:
            image_post["phase"] = match.group(1)[:80]


def _validate_image_pdf(base_pdf: Path, candidate_pdf: Path) -> tuple[bool, str, int]:
    """Open every output page and reject truncated image-postprocess results."""
    try:
        import fitz

        if not candidate_pdf.is_file() or candidate_pdf.stat().st_size == 0:
            return False, "image postprocess did not create a PDF", 0
        with fitz.open(str(base_pdf)) as base_doc:
            base_pages = base_doc.page_count
        with fitz.open(str(candidate_pdf)) as candidate_doc:
            if not candidate_doc.is_pdf or candidate_doc.needs_pass:
                return False, "image postprocess output is not a readable PDF", 0
            output_pages = candidate_doc.page_count
            if output_pages != base_pages:
                return False, (
                    "image postprocess changed page count: "
                    f"base={base_pages}, out={output_pages}"
                ), output_pages
            for page_no in range(output_pages):
                candidate_doc.load_page(page_no)
        return True, "", output_pages
    except Exception as exc:
        return False, f"invalid image postprocess PDF: {exc}", 0


def _finish_image_post(job: dict, state: str, message: str | None = None) -> None:
    with JOBS_LOCK:
        image_post = job["image_post"]
        image_post["state"] = state
        image_post["proc"] = None
        image_post["finished_at"] = time.time()
        if message:
            _append_image_log_locked(image_post, message)


def _run_image_postprocess(job: dict) -> None:
    """Run the optional image pass without mutating the main pipeline state."""
    acquired = False
    partial_path: Path | None = None
    final_path: Path | None = None
    partial_report: Path | None = None
    final_report: Path | None = None
    proc = None
    try:
        while not acquired:
            acquired = IMAGE_POST_SEMAPHORE.acquire(timeout=0.2)
            with JOBS_LOCK:
                cancelled = bool(job["image_post"].get("cancel"))
            if cancelled:
                _finish_image_post(job, "cancelled", "[cancelled before start]")
                return

        with JOBS_LOCK:
            image_post = job["image_post"]
            if image_post.get("cancel"):
                image_post["state"] = "cancelled"
                image_post["finished_at"] = time.time()
                _append_image_log_locked(image_post, "[cancelled before start]")
                return
            image_post["state"] = "running"
            image_post["phase"] = "start"
            image_post["started_at"] = time.time()
            base_path = Path(image_post["base_path"])
            partial_path = Path(image_post["partial_path"])
            final_path = Path(image_post["final_path"])
            partial_report = Path(str(partial_path) + ".vision.json")
            final_report = Path(str(final_path) + ".vision.json")

        # These are unique, server-derived derivative paths; never touch base_path.
        for stale in (partial_path, final_path, partial_report, final_report):
            if stale.exists():
                stale.unlink()

        cmd = [
            sys.executable, "-m", "app.cli",
            "--target-lang", str(job.get("target_lang") or "ru"),
            "--image-postprocess",
            str(base_path), "--out", str(partial_path),
        ]
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
            image_post = job["image_post"]
            image_post["proc"] = proc
            cancelled = bool(image_post.get("cancel"))
        if cancelled:
            try:
                proc.terminate()
            except Exception:
                pass

        if proc.stdout is not None:
            for raw_line in proc.stdout:
                for chunk in re.split(r"\r", raw_line.rstrip("\r\n")):
                    if chunk.strip():
                        _parse_image_post_line(job, chunk)
                with JOBS_LOCK:
                    cancelled = bool(job["image_post"].get("cancel"))
                if cancelled:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
        waited_rc = proc.wait()
        rc = proc.returncode if proc.returncode is not None else waited_rc
        with JOBS_LOCK:
            cancelled = bool(job["image_post"].get("cancel"))
            job["image_post"]["proc"] = None
        if cancelled:
            _finish_image_post(job, "cancelled", "[cancelled by user]")
            return
        if rc != 0:
            _finish_image_post(job, "error", f"[error] image postprocess exited with code {rc}")
            return

        valid, reason, page_count = _validate_image_pdf(base_path, partial_path)
        if not valid:
            _finish_image_post(job, "error", f"[error] {reason}")
            return
        with JOBS_LOCK:
            cancelled = bool(job["image_post"].get("cancel"))
            if not cancelled:
                job["image_post"]["phase"] = "promote"
        if cancelled:
            _finish_image_post(job, "cancelled", "[cancelled by user]")
            return

        os.replace(partial_path, final_path)
        if partial_report.exists():
            try:
                try:
                    report_data = json.loads(
                        partial_report.read_text(encoding="utf-8")
                    )
                    if isinstance(report_data, dict):
                        report_data["output_pdf"] = str(final_path)
                        report_data["report_path"] = str(final_report)
                        partial_report.write_text(
                            json.dumps(
                                report_data, ensure_ascii=False, indent=2
                            ),
                            encoding="utf-8",
                        )
                except (json.JSONDecodeError, OSError, TypeError) as exc:
                    with JOBS_LOCK:
                        _append_image_log_locked(
                            job["image_post"],
                            f"[warning] vision report paths were not updated: {exc}",
                        )
                os.replace(partial_report, final_report)
            except OSError as exc:
                with JOBS_LOCK:
                    _append_image_log_locked(
                        job["image_post"],
                        f"[warning] vision report was not promoted: {exc}",
                    )
        with JOBS_LOCK:
            image_post = job["image_post"]
            if image_post.get("cancel"):
                cancelled = True
            else:
                cancelled = False
                image_post["state"] = "done"
                image_post["phase"] = "done"
                if image_post["total"] > 0:
                    image_post["progress"] = image_post["total"]
                else:
                    image_post["progress"] = page_count
                    image_post["total"] = page_count
                image_post["result_path"] = str(final_path)
                image_post["proc"] = None
                image_post["finished_at"] = time.time()
                _append_image_log_locked(image_post, "[done] image postprocess completed")
        if cancelled:
            if final_path.exists():
                final_path.unlink()
            if final_report is not None and final_report.exists():
                final_report.unlink()
            _finish_image_post(job, "cancelled", "[cancelled by user]")
    except Exception as exc:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        with JOBS_LOCK:
            cancelled = bool(job.get("image_post", {}).get("cancel"))
        _finish_image_post(
            job, "cancelled" if cancelled else "error",
            "[cancelled by user]" if cancelled else f"[exception] {exc}",
        )
    finally:
        if partial_path is not None:
            try:
                if partial_path.exists():
                    partial_path.unlink()
            except OSError:
                pass
        if partial_report is not None:
            try:
                partial_report.unlink(missing_ok=True)
            except OSError:
                pass
        if acquired:
            IMAGE_POST_SEMAPHORE.release()


def _new_job(src_path: str, job_id: str | None = None,
             target_lang: str | None = None) -> dict:
    cfg = load_config()
    target_lang = str(target_lang or cfg.get("target_lang", "ru")).lower()
    if target_lang not in {"ru", "en"}:
        target_lang = "ru"
    tgt = target_lang.upper()
    job_id = job_id or uuid.uuid4().hex[:12]
    stem = Path(src_path).stem
    # срезаем job_id-префикс, добавленный при upload
    m = re.match(r"^[0-9a-f]{12}_(.+)$", stem)
    clean_stem = m.group(1) if m else stem
    # временно — переведённое имя подставится после успеха конвейера
    download_name = f"{clean_stem}_{tgt}.pdf"
    # Внутреннее имя всегда уникально для job: параллельные загрузки файлов с
    # одинаковым названием не перезаписывают результаты друг друга.
    out_name = f"{job_id}_{download_name}"
    return {
        "job_id": job_id,
        "target_lang": target_lang,
        "src": src_path,
        "out_path": str(UPLOADS.resolve() / out_name),
        "download_name": download_name,
        "state": "idle", "stage": "", "progress": 0, "total": 0,
        "ok": -1, "cached": -1, "fail": -1,
        "pages": None, "images": None,
        "logs": [], "result_path": None, "base_result_path": None,
        "proc": None, "cancel": False, "image_post": _new_image_post_state(),
        "started_at": time.time(),
    }


def _run_pipeline(job: dict, resume: bool, from_translate: bool, limit: int,
                  mode: str = "pipeline"):
    cmd = [sys.executable, "-m", "app.cli",
           "--in", job["src"], "--out", job["out_path"],
           "--mode", mode,
           "--target-lang", str(job.get("target_lang") or "ru")]
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

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", text=True, bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as exc:
        with JOBS_LOCK:
            job["state"] = "error"
            job["logs"].append(f"[exception] CLI не запущен: {exc}")
        return
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
        cancelled = bool(job["cancel"])
        job["proc"] = None
        out_value = job["out_path"]
        job_id = job["job_id"]
        src_value = job["src"]

    if cancelled:
        with JOBS_LOCK:
            job["state"] = "cancelled"
        return
    if rc != 0:
        with JOBS_LOCK:
            job["state"] = "error"
            job["logs"].append(f"[error] cli завершился с кодом {rc}")
        return

    # Перевод имени и файловые операции выполняются без JOBS_LOCK: status и
    # cancel других заданий не должны ждать сетевой LLM-вызов.
    out_p = Path(out_value)
    download_name = Path(out_value).name
    rename_log = None
    rename_error = None
    if out_p.exists():
        try:
            cfg = load_config()
            configure_target_language(
                cfg, str(job.get("target_lang") or "ru")
            )
            tgt = cfg["target_lang"].upper()
            src_stem = Path(src_value).stem
            translated = translate_filename_stem(src_stem, cfg)
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", translated)
            safe = re.sub(r"\s+", " ", safe).strip()
            if len(safe) > 80:
                safe = safe[:80].rsplit(" ", 1)[0].rstrip()
            if safe:
                download_name = f"{safe}_{tgt}.pdf"
                new_path = out_p.with_name(f"{job_id}_{download_name}")
                if new_path != out_p:
                    # Путь содержит job_id и принадлежит только этому заданию.
                    old_path = out_p
                    os.replace(old_path, new_path)
                    out_p = new_path
                    rename_log = f"[rename] {old_path.name} -> {new_path.name}"
                    old_report = Path(str(old_path) + ".layout.json")
                    if old_report.exists():
                        try:
                            os.replace(
                                old_report,
                                Path(str(new_path) + ".layout.json"),
                            )
                        except OSError as exc:
                            rename_error = f"[layout report rename skipped] {exc}"
        except Exception as exc:
            rename_error = f"[rename skipped] {exc}"

    with JOBS_LOCK:
        if job["cancel"]:
            job["state"] = "cancelled"
            return
        if not out_p.is_file():
            job["state"] = "error"
            job["logs"].append("[error] итоговый PDF не найден")
            return
        job["out_path"] = str(out_p)
        job["result_path"] = str(out_p)
        job["base_result_path"] = str(out_p)
        job["download_name"] = download_name
        if rename_log:
            job["logs"].append(rename_log)
        if rename_error:
            job["logs"].append(rename_error)
        job["state"] = "done"
        job["stage"] = "validate"
        job["logs"].append("[done] Конвейер завершён успешно.")


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
    image_available, image_reason = _image_postprocess_capability(cfg)
    return {"source_lang": cfg.get("source_lang", "?"),
            "target_lang": cfg.get("target_lang", "?"),
            "target_languages": ["ru", "en"],
            "image_postprocess": {
                "available": image_available,
                "reason": image_reason,
            }}


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
    mode = payload.get("mode", "pipeline")
    target_lang = str(payload.get("target_lang") or "ru").lower()
    if mode not in ("pipeline", "markdown"):
        mode = "pipeline"
    if target_lang not in {"ru", "en"}:
        raise HTTPException(400, "поддерживаются языки результата: ru, en")
    if not job_id or not src:
        raise HTTPException(400, "требуются поля job и src")
    if not re.fullmatch(r"[0-9a-f]{12}", str(job_id)):
        raise HTTPException(400, "некорректный job id")
    try:
        src_path = _resolve_upload_pdf(src)
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "исходный PDF не найден") from exc
    if not src_path.name.startswith(f"{job_id}_"):
        raise HTTPException(403, "исходный PDF не принадлежит этому job")
    with JOBS_LOCK:
        if job_id in JOBS:
            raise HTTPException(409, "job уже запущен")
        j = _new_job(str(src_path), job_id, target_lang=target_lang)
        JOBS[job_id] = j
    background_tasks.add_task(_run_pipeline, j, resume, from_translate, limit, mode)
    return {"job_id": job_id, "state": "running",
            "target_lang": target_lang}


@app.get("/api/status")
async def status(job: str):
    with JOBS_LOCK:
        j = JOBS.get(job)
        if not j:
            raise HTTPException(404, "job не найден")
        image_post = j.setdefault("image_post", _new_image_post_state())
        image_result = image_post.get("result_path")
        image_public = {
            "state": image_post.get("state", "idle"),
            "phase": image_post.get("phase", ""),
            "progress": image_post.get("progress", 0),
            "total": image_post.get("total", 0),
            "ok": image_post.get("ok", 0),
            "cached": image_post.get("cached", 0),
            "failed": image_post.get("failed", 0),
            "logs": list(image_post.get("logs", [])[-100:]),
            "result_ready": bool(
                image_post.get("state") == "done" and image_result
                and Path(image_result).is_file()
            ),
        }
        return {
            "state": j["state"], "stage": j["stage"],
            "target_lang": j.get("target_lang", "ru"),
            "progress": j["progress"], "total": j["total"],
            "ok": j["ok"], "cached": j["cached"], "fail": j["fail"],
            "pages": j["pages"], "images": j["images"],
            "logs": j["logs"][-200:],
            # Не публикуем абсолютный серверный путь; старому UI достаточно
            # truthy имени, новые клиенты используют result_ready.
            "result_path": (Path(j["result_path"]).name
                            if j.get("result_path") else None),
            "result_ready": bool(
                j.get("state") == "done" and j.get("result_path")
                and Path(j["result_path"]).is_file()
            ),
            "image_post": image_public,
        }


@app.post("/api/cancel")
async def cancel(job: str):
    proc = None
    with JOBS_LOCK:
        j = JOBS.get(job)
        if j:
            j["cancel"] = True
            proc = j.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/jobs/{job_id}/image-postprocess", status_code=202)
async def start_image_postprocess(job_id: str, background_tasks: BackgroundTasks):
    available, reason = _image_postprocess_capability()
    if not available:
        raise HTTPException(503, reason)

    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "job не найден")
        if job.get("state") != "done":
            raise HTTPException(409, "основной перевод ещё не завершён")
        image_post = job.setdefault("image_post", _new_image_post_state())
        if image_post.get("state") in {"queued", "running"}:
            raise HTTPException(409, "обработка изображений уже выполняется")
        if image_post.get("state") == "done":
            raise HTTPException(409, "обработка изображений уже завершена")
        base_value = job.get("base_result_path") or job.get("result_path")
    if not base_value:
        raise HTTPException(404, "базовый результат недоступен")
    try:
        base_path = _resolve_upload_pdf(base_value)
        final_path, partial_path, download_name = _image_output_paths(job_id, base_path)
        _resolve_upload_pdf(final_path, must_exist=False)
        _resolve_upload_pdf(partial_path, must_exist=False)
        if final_path.resolve() == base_path.resolve():
            raise ValueError("derivative path collides with base PDF")
    except (OSError, ValueError) as exc:
        raise HTTPException(403, f"небезопасный путь результата: {exc}") from exc

    with JOBS_LOCK:
        # Re-check after filesystem validation so concurrent requests cannot queue twice.
        current = JOBS.get(job_id)
        if current is not job or job.get("state") != "done":
            raise HTTPException(409, "состояние job изменилось")
        if job["image_post"].get("state") in {"queued", "running", "done"}:
            raise HTTPException(409, "обработка изображений уже запущена")
        image_post = _new_image_post_state()
        image_post.update({
            "state": "queued", "phase": "queue",
            "base_path": str(base_path), "partial_path": str(partial_path),
            "final_path": str(final_path), "download_name": download_name,
        })
        job["image_post"] = image_post
    background_tasks.add_task(_run_image_postprocess, job)
    return {"job_id": job_id, "state": "queued"}


@app.post("/api/jobs/{job_id}/image-postprocess/cancel")
async def cancel_image_postprocess(job_id: str):
    proc = None
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "job не найден")
        image_post = job.setdefault("image_post", _new_image_post_state())
        if image_post.get("state") in {"queued", "running"}:
            image_post["cancel"] = True
            proc = image_post.get("proc")
        state = image_post.get("state", "idle")
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    return {"ok": True, "state": state}


def _result_response(job_id: str, variant: str, disposition: str) -> FileResponse:
    normalized_variant = variant.lower()
    if normalized_variant not in {"base", "image", "images"}:
        raise HTTPException(400, "variant должен быть base или images")
    if disposition not in {"inline", "attachment"}:
        raise HTTPException(400, "disposition должен быть inline или attachment")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "job не найден")
        if normalized_variant == "base":
            value = job.get("base_result_path") or job.get("result_path")
            download_name = (job.get("download_name")
                             or (Path(value).name if value else "result.pdf"))
        else:
            image_post = job.setdefault("image_post", _new_image_post_state())
            if image_post.get("state") != "done":
                raise HTTPException(404, "улучшенный PDF ещё не готов")
            value = image_post.get("result_path")
            download_name = image_post.get("download_name") or "result_IMG.pdf"
    if not value:
        raise HTTPException(404, "результат недоступен")
    try:
        path = _resolve_upload_pdf(value)
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "файл результата недоступен") from exc
    response = FileResponse(path, media_type="application/pdf", filename=download_name)
    if disposition == "inline":
        response.headers["content-disposition"] = (
            "inline; filename*=UTF-8''" + quote(download_name, safe="")
        )
    return response


@app.get("/api/jobs/{job_id}/result")
async def job_result(job_id: str, variant: str = "base",
                     disposition: str = "attachment"):
    return _result_response(job_id, variant, disposition)


@app.get("/api/download")
async def download(job: str, variant: str = "base"):
    """Backward-compatible download route with optional derivative selection."""
    return _result_response(job, variant, "attachment")


if __name__ == "__main__":
    uvicorn.run("app.web:app", host="127.0.0.1", port=8765,
                reload=False, log_level="warning")
