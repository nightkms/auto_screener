@echo off
setlocal
cd /d "%~dp0"

if not exist data mkdir data
if not exist logs mkdir logs

REM Restart logic lives in _restart.ps1 (avoids cmd escaping hell).
REM It prefers the 'AutoScreener' scheduled task: a task-launched instance runs
REM in session 0 and cannot be seen or killed from this interactive session.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_restart.ps1"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [FAIL] restart failed - see the messages above.
) else (
    echo You can close this window. The server keeps running.
)
pause
endlocal
