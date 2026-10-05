@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Lance installer.cmd en premier.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" main.py --setup
pause
