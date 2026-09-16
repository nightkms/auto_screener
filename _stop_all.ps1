# scheduler.py를 돌리는 pythonw 프로세스를 트리째 종료한다.
# venv 런처(스텁 → 실제 인터프리터)로 1 인스턴스가 pythonw 2개로 뜨고, 실수로
# 중복 기동된 경우까지 한 번에 정리한다. (PID 파일 하나만 보면 놓친다)
#
# 주의: 작업 스케줄러 작업 'AutoScreener'(S4U principal)로 기동하면 스크리너가
# **세션 0**에서 돈다. 이때 대화형 세션의 PowerShell은 그 프로세스의
# CommandLine·실행경로를 아예 못 읽고(Access denied) taskkill도 막힌다.
# 그래서 대상 탐색을 3단(CommandLine → PID 파일 → 대시보드 포트 리스너)으로 하고,
# 종료가 막히면 -AllowElevate 일 때 관리자 권한으로 한 번 더 시도한다.
#
#   -AllowElevate : UAC 창을 띄워 승격 재시도 허용(대화형에서만 의미 있다).
#                   _autostart.vbs는 이미 세션 0 컨텍스트라 붙이지 않는다 —
#                   무인 기동 중 UAC를 띄우면 그대로 멈춰 선다.
#
# 종료 코드: 0 = 남은 인스턴스 없음, 1 = 못 죽인 프로세스가 남음.
param([switch]$AllowElevate)

$ErrorActionPreference = "SilentlyContinue"
Set-Location -Path $PSScriptRoot

$pidFile = Join-Path $PSScriptRoot "data\screener.pid"

function Get-DashboardPort {
    # .env의 DASHBOARD_PORT (config.py 기본값과 동일하게 8765 폴백)
    $port = 8765
    $envFile = Join-Path $PSScriptRoot ".env"
    if (Test-Path $envFile) {
        foreach ($line in (Get-Content $envFile)) {
            if ($line -match '^\s*DASHBOARD_PORT\s*=\s*(\d+)') { $port = [int]$Matches[1] }
        }
    }
    return $port
}

function Resolve-Targets {
    $all = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'"
    $byId = @{}
    foreach ($p in $all) { $byId[[string]$p.ProcessId] = $p }

    $seed = @()

    # (1) CommandLine이 읽히는 경우 — 같은 컨텍스트에서 띄운 인스턴스
    foreach ($p in $all) {
        if ($p.CommandLine -like "*scheduler.py*") { $seed += $p }
    }

    # (2) scheduler.py가 스스로 남긴 PID 파일 — 권한과 무관하게 읽힌다
    if (Test-Path $pidFile) {
        $fromFile = (Get-Content $pidFile -Raw).Trim()
        if ($byId.ContainsKey($fromFile)) { $seed += $byId[$fromFile] }
    }

    # (3) 대시보드 포트 리스너 — PID 파일이 없거나 낡았을 때의 마지막 보루
    foreach ($c in (Get-NetTCPConnection -LocalPort (Get-DashboardPort) -State Listen)) {
        $owner = [string]$c.OwningProcess
        if ($byId.ContainsKey($owner)) { $seed += $byId[$owner] }
    }

    # 씨앗에서 python 부모 사슬을 거슬러 올라가 venv 런처 스텁까지 포함시킨다.
    # ($byId에 python 계열만 담겨 있어 cmd /c 래퍼에서 자연히 멈춘다.
    #  래퍼는 자식이 끝나면 스스로 빠지므로 굳이 죽일 필요도 없다)
    $found = @{}
    foreach ($p in $seed) {
        $cur = $p
        while ($cur -and -not $found.ContainsKey([string]$cur.ProcessId)) {
            $found[[string]$cur.ProcessId] = $cur
            $cur = $byId[[string]$cur.ParentProcessId]
        }
    }
    return @($found.Values)
}

$targets = Resolve-Targets

if ($targets.Count -eq 0) {
    Write-Host "no running scheduler instance"
    if (Test-Path $pidFile) { Remove-Item $pidFile -Force }
    exit 0
}

$survived = @()
foreach ($p in $targets) {
    taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
    Start-Sleep -Milliseconds 300
    if (Get-Process -Id $p.ProcessId) {
        $survived += [int]$p.ProcessId
        Write-Host "stop FAILED PID=$($p.ProcessId)  (access denied - session 0?)"
    } else {
        Write-Host "stopped PID=$($p.ProcessId)"
    }
}

if ($survived.Count -gt 0 -and $AllowElevate) {
    # 세션 0(작업 스케줄러 기동분)은 관리자 권한이 있어야 죽는다.
    Write-Host ""
    Write-Host "retrying as administrator (approve the UAC prompt)..."
    try {
        Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -ErrorAction Stop -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath)
    } catch {
        Write-Host "elevation cancelled or failed: $($_.Exception.Message)"
    }
    $survived = @($survived | Where-Object { Get-Process -Id $_ })
}

if (Test-Path $pidFile) { Remove-Item $pidFile -Force }

if ($survived.Count -gt 0) {
    Write-Host ("still running: " + ($survived -join ", "))
    exit 1
}
exit 0
