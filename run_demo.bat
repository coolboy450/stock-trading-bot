@echo off
REM Runs the full demo (real prices if online, synthetic if offline). Paper trading only.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup_windows.bat
chcp 65001 >nul
.venv\Scripts\python.exe main.py demo %*
echo.
echo To see the dashboard, double-click dashboard.bat
pause
