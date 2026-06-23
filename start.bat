@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title PDF ZH-^>RU переводчик (port 8765)

echo ============================================================
echo   PDF переводчик ZH -^> RU
echo   Веб-интерфейс: http://127.0.0.1:8765
echo   Остановить: Ctrl+C
echo ============================================================
echo.

python webapp.py
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось запустить сервер.
    echo Проверьте, что установлен Python и зависимости:
    echo   pip install pymupdf openai tqdm pyyaml fastapi uvicorn python-multipart
    echo.
    pause
)
