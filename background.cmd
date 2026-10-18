@echo off
REM Background launcher: no console window, everything logged to a file.
REM Used by the Startup shortcut. Run start.cmd instead if you want to watch.
REM Everything resolves from this script's own folder, so the repo can be
REM checked out or moved anywhere.
setlocal
set "APP_DIR=%~dp0"
set "HF_HOME=%APP_DIR%huggingface"
set "PYTHONUNBUFFERED=1"
"%APP_DIR%.venv\Scripts\python.exe" "%APP_DIR%dictate.py" >> "%APP_DIR%dictate.log" 2>&1
endlocal
