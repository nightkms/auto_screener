@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist data mkdir data
if not exist logs mkdir logs

REM Kill every running instance (venv stub + child + accidental duplicates) before starting.
echo Stopping any running instances...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_stop_all.ps1"
timeout /t 2 /nobreak >nul

if not exist .venv\Scripts\pythonw.exe (
    echo ERROR: .venv\Scripts\pythonw.exe not found.
    echo Run: python -m venv .venv
    echo Then: .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

echo Starting AutoScreener in detached mode...

wscript.exe "%~dp0start_helper.vbs" "%~dp0."

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_get_pid.ps1"

if not exist data\screener.pid (
    echo.
    echo ERROR: PID capture failed.
    echo Check logs\screener.err.log and logs\screener.out.log
    pause
    exit /b 1
)

set /p PID=<data\screener.pid
echo.
echo [OK] Started
echo      PID       = %PID%
echo      Dashboard = http://localhost:8765
echo      Log files = logs\screener.out.log / screener.err.log
echo      Stop      = stop_screener.bat
echo.
echo You can close this window. The server keeps running.
pause
endlocal
