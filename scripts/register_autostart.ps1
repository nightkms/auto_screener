# auto_screener 자동 기동 작업(AutoScreener) 등록 — **관리자 권한 필요**.
#
# 트리거 2개: 부팅 2분 후 + 로그온 1분 후. 로그온 트리거만 두면 Windows Update
# 자동 재부팅 뒤 잠금 화면에 머무는 동안 스크리너가 뜨지 않는다(2026-08-12 사고:
# 06:04 부팅 → 23:08 로그온까지 17시간 공백 → 07:12 Claude 인증 토큰 만료).
# 그래서 로그온 없이도 도는 S4U principal을 쓴다(비밀번호 저장 불필요).
#
# 실행: 관리자 PowerShell에서  .\scripts\register_autostart.ps1
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$vbs = Join-Path $root '_autostart.vbs'
if (-not (Test-Path $vbs)) { throw "not found: $vbs" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"' + $vbs + '"') -WorkingDirectory $root

$tLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$tLogon.Delay = 'PT1M'
$tBoot = New-ScheduledTaskTrigger -AtStartup
$tBoot.Delay = 'PT2M'          # 부팅 직후 네트워크·디스크가 안정될 때까지 여유

# S4U = 사용자 로그온 여부와 무관하게 실행(비밀번호 저장 없음).
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

# 노트북 대비: 배터리 전원에서도 실행. schtasks 기본값은 이걸 막는다.
# StartWhenAvailable = 트리거 시점에 꺼져 있었으면 켜진 뒤 보충 실행.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

$desc = 'auto_screener 스케줄러 자동 기동 (부팅 2분 후 / 로그온 1분 후). ' +
        'Windows Update 재부팅 뒤 미기동으로 Claude 인증 토큰이 만료되는 것을 막는다.'

$t = Register-ScheduledTask -TaskName 'AutoScreener' -Description $desc `
    -Action $action -Trigger $tLogon, $tBoot -Principal $principal -Settings $settings -Force

Write-Host ''
Write-Host ('[OK] registered: ' + $t.TaskName + '  state=' + $t.State) -ForegroundColor Green
$reg = Get-ScheduledTask -TaskName 'AutoScreener'
$reg.Triggers | ForEach-Object { Write-Host ('     trigger: ' + $_.CimClass.CimClassName + '  delay=' + $_.Delay) }
Write-Host ('     principal: ' + $reg.Principal.UserId + '  logonType=' + $reg.Principal.LogonType)
Write-Host ''
Write-Host '검증: 다음 재부팅 후 logs\autostart.log 에 줄이 추가되면 정상.' -ForegroundColor Cyan
