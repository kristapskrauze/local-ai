@echo off
cd /d %~dp0
if not defined SERVICE_PYTHON set "SERVICE_PYTHON=python"
if not exist config\messaging.json (
  echo No config yet: copy config.example\* to config\ and fill it in.
  exit /b 1
)
"%SERVICE_PYTHON%" -m pip install -r requirements.txt
"%SERVICE_PYTHON%" -m uvicorn app.main:app --host 0.0.0.0 --port 8765
