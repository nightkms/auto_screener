@echo off
setlocal
cd /d "%~dp0"

echo Stopping ALL scheduler.py instances (stub + child + duplicates)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_stop_all.ps1"

echo.
echo [OK] Stop done.
pause
endlocal
