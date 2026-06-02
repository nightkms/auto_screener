@echo off
setlocal
cd /d "%~dp0"

if not exist data\screener.pid (
    echo PID file not found. Nothing to stop.
    pause
    exit /b 1
)

set /p PID=<data\screener.pid
if "%PID%"=="" (
    echo PID file empty. Cleaning up.
    del data\screener.pid
    pause
    exit /b 1
)

echo Stopping PID %PID% and child processes...
taskkill /PID %PID% /T /F >nul 2>&1
if errorlevel 1 (
    echo Process may have already exited.
) else (
    echo [OK] Stopped PID=%PID%
)

del data\screener.pid >nul 2>&1
pause
endlocal
