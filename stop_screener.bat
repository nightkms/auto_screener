@echo off
setlocal
cd /d "%~dp0"

echo Stopping ALL scheduler.py instances (stub + child + duplicates)...
REM -AllowElevate: an instance started by the 'AutoScreener' scheduled task runs
REM in session 0 and needs admin rights to kill, so a UAC prompt may appear.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_stop_all.ps1" -AllowElevate
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [FAIL] some processes are still running - check Task Manager.
) else (
    echo [OK] Stop done.
    echo      NOTE: scheduled task 'AutoScreener' starts it again at next logon/boot.
)
pause
endlocal
