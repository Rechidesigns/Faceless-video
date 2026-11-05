@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Setting up Python environment...
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist .env copy .env.example .env >nul
start "" http://localhost:8000
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
