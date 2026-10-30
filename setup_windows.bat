@echo off
REM One-time setup on Windows: creates .venv and installs packages.
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    echo Python was not found.
    echo Install Python 3.11+ from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH", then run this file again.
    pause
    exit /b 1
)
if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    py -3 -m venv .venv || (echo Failed to create .venv & pause & exit /b 1)
)
echo Installing packages (first time takes a few minutes)...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt || (echo Package install failed & pause & exit /b 1)
echo.
echo Setup complete. Double-click run_demo.bat to start.
pause
