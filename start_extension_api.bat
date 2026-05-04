@echo off
setlocal
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if "%EXTENSION_API_HOST%"=="" set EXTENSION_API_HOST=127.0.0.1
if "%EXTENSION_API_PORT%"=="" set EXTENSION_API_PORT=8010
if "%EXTENSION_API_BASE_URL%"=="" set EXTENSION_API_BASE_URL=http://%EXTENSION_API_HOST%:%EXTENSION_API_PORT%
python scripts\sync_extension_config.py
python -m uvicorn extension_api.main:app --host %EXTENSION_API_HOST% --port %EXTENSION_API_PORT% --reload
