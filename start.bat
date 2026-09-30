@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0venv\Scripts\python.exe" (
    echo Virtual environment missing. Run: python -m venv venv
    exit /b 1
)
"%~dp0venv\Scripts\python.exe" -m app.main
exit /b %errorlevel%
