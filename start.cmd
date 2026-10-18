@echo off
REM Visible console launcher: watch the log output live while it runs.
REM background.cmd is the hidden variant used by the Startup shortcut.
REM Everything resolves from this script's own folder.
setlocal
set "APP_DIR=%~dp0"
set "HF_HOME=%APP_DIR%huggingface"
set "UV_CACHE_DIR=%APP_DIR%uv-cache"
"%APP_DIR%.venv\Scripts\python.exe" "%APP_DIR%dictate.py"
pause
endlocal
