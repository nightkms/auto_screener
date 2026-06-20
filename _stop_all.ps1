# scheduler.py를 돌리는 모든 pythonw 프로세스를 트리째 종료한다.
# venv 런처(스텁→실제 인터프리터)로 1 인스턴스가 pythonw 2개로 뜨거나, 실수로
# 중복 기동된 경우까지 한 번에 정리한다. (PID 파일 하나만 보던 기존 한계 보완)
$ErrorActionPreference = "SilentlyContinue"
Set-Location -Path $PSScriptRoot

$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*scheduler.py*" }

if (-not $procs) {
    Write-Host "no running scheduler instance"
} else {
    foreach ($p in $procs) {
        taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
        Write-Host "stopped PID=$($p.ProcessId)"
    }
}

$pidFile = Join-Path $PSScriptRoot "data\screener.pid"
if (Test-Path $pidFile) {
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}
exit 0
