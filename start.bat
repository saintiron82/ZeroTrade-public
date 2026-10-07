@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found. Please create one in .venv
    exit /b 1
)

call .venv\Scripts\activate.bat

echo Starting FF Trading Bot (Web UI)...
python -m src.main serve --port 8080
