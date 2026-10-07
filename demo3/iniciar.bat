@echo off
REM Arranca la oficina multiagente en http://localhost:8001
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install -U pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
start "" http://localhost:8001
.venv\Scripts\python -m uvicorn app.main:app --port 8001 --reload --reload-dir app
