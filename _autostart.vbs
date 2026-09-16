' 작업 스케줄러 자동 기동용 래퍼 (로그온 시 실행).
' start_screener.bat은 콘솔 출력과 pause가 있어 무인 기동에 부적합하다.
' 여기서는 창을 전혀 띄우지 않고, 남아 있는 인스턴스를 먼저 정리한 뒤 띄운다
' (중복 기동 = 같은 DB에 큐 워커 2개 → 이중 분석·중복 알림).
Dim shell, fso, base, logf, bkOut, bkErr
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base

If Not fso.FolderExists(base & "\data") Then fso.CreateFolder base & "\data"
If Not fso.FolderExists(base & "\logs") Then fso.CreateFolder base & "\logs"

' 남은 인스턴스 정리 (True = 끝날 때까지 대기)
shell.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & base & "\_stop_all.ps1""", 0, True
' taskkill 직후엔 로그 핸들이 곧바로 풀리지 않는다 (start_screener.bat의 timeout /t 2와 같은 이유)
WScript.Sleep 2000

' 직전 로그 1세대 보존 — start_helper.vbs가 redirect로 덮어쓰기 때문에, 그대로 두면
' 재부팅 직후 기동에서 '왜 죽었나'의 단서가 매번 사라진다(2026-08-12 진단 때 겪음).
' 반드시 정리 '이후'에 한다 — 살아 있는 프로세스가 열어둔 로그는 복사·이동이 막히고,
' wscript는 런타임 오류에 보이지 않는 대화상자를 띄운 채 멈춘다(초판이 여기서 죽었다).
bkOut = BackupLog(base & "\logs\screener.out.log", base & "\logs\screener.out.1.log")
bkErr = BackupLog(base & "\logs\screener.err.log", base & "\logs\screener.err.1.log")

' 기동 (start_helper.vbs가 hidden cmd로 pythonw + 로그 redirect)
shell.Run "wscript.exe """ & base & "\start_helper.vbs"" """ & base & """", 0, False

' 자동 기동 흔적 — 다음 사고 때 '기동 자체는 됐나'를 이 파일로 판별한다.
Set logf = fso.OpenTextFile(base & "\logs\autostart.log", 8, True)
logf.WriteLine Now & " autostart triggered (log backup out=" & bkOut & " err=" & bkErr & ")"
logf.Close

' 로그 백업은 부차적이라 실패해도 기동을 막지 않는다. 다만 오류를 삼키는 범위는
' 이 함수 안으로만 좁힌다 — 바깥까지 덮으면 오타·미정의 호출까지 조용히 묻힌다.
Function BackupLog(src, dst)
    On Error Resume Next
    BackupLog = False
    If fso.FileExists(src) Then
        fso.CopyFile src, dst, True
        BackupLog = (Err.Number = 0)
    End If
End Function
