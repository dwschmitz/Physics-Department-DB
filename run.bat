@echo off
rem Set up (first time only) and start the web interface. Windows.
rem Usage: double-click, or run run.bat from any folder.
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>nul || (echo Python 3 is required: https://www.python.org/downloads/ & pause & exit /b 1)

if not exist .venv (
    echo Creating virtual environment...
    %PY% -m venv .venv || (pause & exit /b 1)
)
fc /b requirements.txt .venv\requirements.installed >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    .venv\Scripts\python -m pip install --quiet -r requirements.txt || (pause & exit /b 1)
    copy /y requirements.txt .venv\requirements.installed >nul
)

.venv\Scripts\python webapp.py ensure-admin || (pause & exit /b 1)
.venv\Scripts\python webapp.py run
pause
