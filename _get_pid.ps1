# scheduler.py를 돌리는 pythonw의 PID를 찾아 data\screener.pid에 기록.
# 별도 .ps1 파일이라 cmd 의 escape 지옥을 우회.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$max = 10
for ($i = 0; $i -lt $max; $i++) {
    $proc = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" `
        | Where-Object { $_.CommandLine -like "*scheduler.py*" } `
        | Sort-Object CreationDate -Descending `
        | Select-Object -First 1
    if ($proc) {
        $proc.ProcessId | Out-File -Encoding ascii -NoNewline -FilePath "data\screener.pid"
        Write-Host "PID=$($proc.ProcessId)"
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
Write-Host "FAIL: pythonw scheduler.py not found"
exit 1
