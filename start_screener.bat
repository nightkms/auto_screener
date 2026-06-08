@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist data mkdir data
if not exist logs mkdir logs

REM 이미 떠 있으면 죽이고 새로 띄운다 (stop 따로 안 해도 되게).
if exist data\screener.pid (
    set /p OLDPID=<data\screener.pid
    if defined OLDPID (
        tasklist /fi "PID eq !OLDPID!" 2>nul | findstr /i "pythonw" >nul
        if not errorlevel 1 (
            echo Found running instance PID=!OLDPID! - stopping it...
            taskkill /PID !OLDPID! /T /F >nul 2>&1
            REM 포트(8765)/PID 해제까지 잠깐 대기
            timeout /t 2 /nobreak >nul
            echo [OK] Old instance stopped.
            echo.
        )
    )
    del data\screener.pid >nul 2>&1
    set "OLDPID="
)

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
