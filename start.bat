@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%~dp0
cd /d "%~dp0"
title PDF translator (port 8765)

echo ============================================================
echo   PDF translator (any language)
echo   Web UI: http://127.0.0.1:8765
echo   Stop: Ctrl+C
echo ============================================================
echo.

python -m app.web
if errorlevel 1 (
    echo.
    echo [ERROR] Server failed to start.
    echo Make sure deps are installed:
    echo   pip install -r requirements.txt
    echo.
    pause
)