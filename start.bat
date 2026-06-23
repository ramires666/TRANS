@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title PDF translator (port 8765)

echo ============================================================
echo   PDF translator (any language -^> RU)
echo   Web UI: http://127.0.0.1:8765
echo   Stop: Ctrl+C
echo ============================================================
echo.

python webapp.py
if errorlevel 1 (
    echo.
    echo [ERROR] Server failed to start.
    echo Make sure Python and deps are installed:
    echo   pip install pymupdf openai tqdm pyyaml fastapi uvicorn python-multipart
    echo.
    pause
)