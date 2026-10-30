@echo off
REM Opens the dashboard for the demo data at http://127.0.0.1:8000 (close this window to stop).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup_windows.bat
REM open the browser a few seconds later, once the server is up
start "" /min cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000"
.venv\Scripts\python.exe main.py demo-dashboard %*
pause
