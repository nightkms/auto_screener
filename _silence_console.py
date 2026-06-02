"""Windows에서 자식 프로세스의 콘솔 창이 깜빡이며 뜨지 않게 강제.

claude-agent-sdk → anyio.open_process → asyncio.create_subprocess_exec
→ ProactorEventLoop → subprocess.Popen 까지 모두 결국 subprocess.Popen을 거치므로
__init__에서 CREATE_NO_WINDOW 플래그를 OR해 모든 경로를 한꺼번에 차단.

scheduler.py 최상단에서 첫 import로 사용한다.
"""
import os

if os.name == "nt":
    import subprocess
    CREATE_NO_WINDOW = 0x08000000

    _orig_popen_init = subprocess.Popen.__init__

    def _silent_popen_init(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0) or 0
        kwargs["creationflags"] = flags | CREATE_NO_WINDOW
        return _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _silent_popen_init  # type: ignore[method-assign]
