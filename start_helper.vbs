' VBS 래퍼: cmd /c로 pythonw를 hidden 모드 + 로그 redirect.
' Wscript.Shell.Run with intWindowStyle=0, bWaitOnReturn=False
'   → cmd 가 hidden console로 떠서 pythonw 부모가 됨
'   → VBS는 즉시 종료, 부모 cmd 창과 lifecycle 완전 분리.
Dim shell, base, cmd
Set shell = CreateObject("WScript.Shell")
base = WScript.Arguments(0)
shell.CurrentDirectory = base
cmd = "cmd /c .venv\Scripts\pythonw.exe scheduler.py 1>logs\screener.out.log 2>logs\screener.err.log"
shell.Run cmd, 0, False
