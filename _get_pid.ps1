# 실행 중인 scheduler.py의 PID를 data\screener.pid에 확정 기록한다.
# 별도 .ps1 파일인 이유: cmd의 escape 지옥을 피하기 위해.
#
# 1순위는 scheduler.py가 스스로 쓴 PID 파일(_write_pid_file)이다. 작업 스케줄러
# (S4U)로 세션 0에서 돌면 대화형 PowerShell이 CommandLine을 못 읽어 WMI 탐색이
# 통째로 실패하는데, 프로세스가 직접 쓴 파일은 권한과 무관하게 읽힌다.
# 2·3순위(WMI CommandLine, 대시보드 포트 리스너)는 구버전·예외 상황 폴백.
$ErrorActionPreference = "SilentlyContinue"
Set-Location -Path $PSScriptRoot

$pidFile = "data\screener.pid"

function Get-DashboardPort {
    $port = 8765
    if (Test-Path ".env") {
        foreach ($line in (Get-Content ".env")) {
            if ($line -match '^\s*DASHBOARD_PORT\s*=\s*(\d+)') { $port = [int]$Matches[1] }
        }
    }
    return $port
}

$max = 40   # 0.5초 × 40 = 최대 20초 (uvicorn 바인딩까지 여유)
for ($i = 0; $i -lt $max; $i++) {
    # (1) 프로세스가 스스로 남긴 PID
    if (Test-Path $pidFile) {
        $fromFile = (Get-Content $pidFile -Raw).Trim()
        if ($fromFile -match '^\d+$' -and (Get-Process -Id ([int]$fromFile))) {
            Write-Host "PID=$fromFile"
            exit 0
        }
    }

    # (2) WMI CommandLine — 같은 컨텍스트에서 띄운 경우에만 읽힌다
    $proc = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*scheduler.py*" } |
        Sort-Object CreationDate -Descending |
        Select-Object -First 1

    # (3) 대시보드 포트를 잡고 있는 python 프로세스
    if (-not $proc) {
        $owner = Get-NetTCPConnection -LocalPort (Get-DashboardPort) -State Listen |
            Select-Object -First 1 -ExpandProperty OwningProcess
        if ($owner) {
            $cand = Get-CimInstance Win32_Process -Filter "ProcessId=$owner"
            if ($cand -and $cand.Name -like "python*") { $proc = $cand }
        }
    }

    if ($proc) {
        $proc.ProcessId | Out-File -Encoding ascii -NoNewline -FilePath $pidFile
        Write-Host "PID=$($proc.ProcessId)"
        exit 0
    }
    Start-Sleep -Milliseconds 500
}

Write-Host "FAIL: scheduler.py process not found"
exit 1
