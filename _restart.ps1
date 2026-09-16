# start_screener.bat의 실제 재기동 로직 (cmd escape 회피용 별도 .ps1).
#
# 기본 경로 = 작업 스케줄러 작업 'AutoScreener'를 한 번 더 실행시킨다.
#   이 작업은 S4U principal이라 스크리너가 **세션 0**에서 돈다. 대화형 세션에서
#   띄운 PowerShell은 그 프로세스의 CommandLine조차 못 읽고(Access denied)
#   taskkill도 막힌다 → 예전처럼 _stop_all.ps1 → start_helper.vbs를 직접 부르면
#   "기존 인스턴스 정리 실패 → 포트 충돌 → 새 인스턴스 즉사 → PID 캡처 실패"가 된다.
#   (2026-08-31 증상: start_screener.bat이 'ERROR: PID capture failed'로 끝남)
#   같은 컨텍스트를 가진 건 작업 자신뿐이므로, 작업이 실행하는 _autostart.vbs가
#   정리 → 로그 1세대 백업 → 기동을 대신 하게 한다. UAC 승격이 필요 없다.
#
# 폴백 = 작업이 없거나(다른 PC·클론 직후) 실행이 거부되면 예전 경로:
#   _stop_all.ps1 -AllowElevate(필요 시 UAC) → start_helper.vbs.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$TaskName = "AutoScreener"

function Get-DashboardPort {
    $port = 8765
    if (Test-Path ".env") {
        foreach ($line in (Get-Content ".env")) {
            if ($line -match '^\s*DASHBOARD_PORT\s*=\s*(\d+)') { $port = [int]$Matches[1] }
        }
    }
    return $port
}

$port = Get-DashboardPort

function Get-LivePid {
    # 지금 대시보드 포트를 잡고 있는 프로세스 PID (없으면 $null).
    # 세션 0 인스턴스도 이 경로로는 보인다 — 재기동 성공 판정의 기준.
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { return $null }
    return [int]$c.OwningProcess
}

if (-not (Test-Path ".venv\Scripts\pythonw.exe")) {
    Write-Host "ERROR: .venv\Scripts\pythonw.exe not found."
    Write-Host "Run:  python -m venv .venv"
    Write-Host "Then: .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$before = Get-LivePid
if ($before) {
    Write-Host "current instance : PID=$before (port $port)"
} else {
    Write-Host "current instance : none"
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$viaTask = $false

if ($task) {
    Write-Host "restarting via scheduled task '$TaskName' ..."
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $viaTask = $true
    } catch {
        Write-Host "  task start failed: $($_.Exception.Message)"
        Write-Host "  falling back to direct start."
    }
} else {
    Write-Host "scheduled task '$TaskName' not registered - direct start."
}

if (-not $viaTask) {
    Write-Host "stopping any running instances ..."
    & "$PSScriptRoot\_stop_all.ps1" -AllowElevate
    Start-Sleep -Seconds 2
    Write-Host "starting AutoScreener in detached mode ..."
    Start-Process -FilePath "wscript.exe" -ArgumentList @(('"' + $PSScriptRoot + '\start_helper.vbs"'), ('"' + $PSScriptRoot + '"'))
}

Write-Host "waiting for the dashboard on port $port ..."
$deadline = (Get-Date).AddSeconds(90)
$newPid = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 1
    $cur = Get-LivePid
    if ($cur -and $cur -ne $before) { $newPid = $cur; break }
}

if (-not $newPid) {
    Write-Host ""
    Write-Host "ERROR: dashboard did not come up on port $port within 90s."
    if ($viaTask) {
        Write-Host "       check logs\autostart.log (did the task fire?)"
    }
    Write-Host "       check logs\screener.err.log / logs\screener.out.log"
    exit 1
}

# PID 파일 확정 (scheduler.py가 이미 썼지만 폴백 경로·구버전 대비로 한 번 더 맞춘다)
& "$PSScriptRoot\_get_pid.ps1"

Write-Host ""
Write-Host "[OK] Restarted"
Write-Host "     PID       = $newPid"
Write-Host "     Dashboard = http://localhost:$port"
Write-Host "     Log files = logs\screener.out.log / screener.err.log"
Write-Host "     Stop      = stop_screener.bat"
exit 0
