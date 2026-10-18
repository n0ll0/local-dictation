@echo off
setlocal
set "APP_DIR=%~dp0"
echo Local dictation, first-time setup
echo.
echo App folder: %APP_DIR%
echo.
echo The setup uses the internet once, to download Python packages.
echo.

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py"
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo Python was not found on this computer.
    echo.
    echo Install Python from https://www.python.org/downloads/
    echo On the very first installer screen, tick "Add python.exe to PATH"
    echo at the bottom, then click Install Now. Run setup.cmd again after.
    echo.
    pause
    exit /b 1
)
echo Found Python (%PY%).

if exist "%APP_DIR%.venv\Scripts\python.exe" (
    "%APP_DIR%.venv\Scripts\python.exe" -c "import faster_whisper, sounddevice, numpy; import sys; sys.exit(0)" >nul 2>nul
    if not errorlevel 1 goto already_set_up
    set "VENV_MISSING="
    if not exist "%APP_DIR%.venv\Scripts\pip.exe" goto skip_venv
    goto pip_only
)

echo [1/2] Creating a private Python environment next to the app...
"%PY%" -m venv "%APP_DIR%.venv"
if errorlevel 1 goto venvfail

:pip_only
echo [2/2] Installing Python packages (faster-whisper, sounddevice, numpy)...
"%APP_DIR%.venv\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade pip
"%APP_DIR%.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%APP_DIR%requirements.txt"
if errorlevel 1 goto pipfail
echo.
echo Done. NEXT STEP: double-click start.cmd to start dictating.
echo.
pause
exit /b 0

:skip_venv
echo [1/2] Reusing the existing private Python environment...
goto pip_only

:already_set_up
echo Everything is already installed. Nothing to do.
echo.
echo NEXT STEP: double-click start.cmd to start dictating.
echo.
pause
exit /b 0

:venvfail
echo.
echo Creating the Python environment failed. A working Python 3.10 or newer
echo is needed. Install it from https://www.python.org/downloads/ (tick
echo "Add python.exe to PATH") and run setup.cmd again.
echo If the message above mentions Permission denied, a running copy of
echo Local dictation is holding its folder; end python.exe in Task
echo Manager and try again.
echo.
pause
exit /b 1

:pipfail
echo.
echo Installing the Python packages failed. Check this window for lines
echo mentioning a network error, check your internet connection, and run
echo setup.cmd again. If those lines mention Permission denied, end any
echo running copy (Task Manager, python.exe) first.
echo.
pause
exit /b 1
