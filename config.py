"""중앙 설정 로더. .env에서 모든 환경 변수 읽고 검증."""
from __future__ import annotations
import json
import os
import shutil
import time
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _req(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"환경 변수 {name} 이(가) .env에 없습니다.")
    return val


def _opt(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


DART_API_KEY = os.getenv("DART_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TOP_N = int(_opt("SCREENER_TOP_N", "5"))
TIMEZONE = _opt("SCREENER_TIMEZONE", "Asia/Seoul")
CRON = _opt("SCREENER_CRON", "0 8 * * SAT")
DASHBOARD_PORT = int(_opt("DASHBOARD_PORT", "8765"))
# 텔레그램 메시지에 들어갈 대시보드 공개 URL. 공인 IP/터널 사용 시 .env에 설정:
#   DASHBOARD_PUBLIC_URL=https://stock.mydomain.com
# 비워두면 http://localhost:<port> 로 대체.
DASHBOARD_PUBLIC_URL = _opt("DASHBOARD_PUBLIC_URL", "").rstrip("/")


def dashboard_url(path: str = "") -> str:
    """알림용 대시보드 URL. path는 '/run/123' 같이 슬래시 포함."""
    base = DASHBOARD_PUBLIC_URL or f"http://localhost:{DASHBOARD_PORT}"
    if path and not path.startswith("/"):
        path = "/" + path
    return base + path
CLAUDE_SUB_MODEL = _opt("CLAUDE_SUB_MODEL", "claude-sonnet-4-6")
CLAUDE_SYNTH_MODEL = _opt("CLAUDE_SYNTH_MODEL", "claude-opus-4-7")
LOG_LEVEL = _opt("LOG_LEVEL", "INFO")

# 모든 산출 데이터는 이 패키지 폴더(ROOT) 밑에 둔다 — 폴더째 옮겨도 데이터가 따라옴.
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "screener.db"
ANALYSIS_DIR = ROOT / "analysis" / "auto"
BY_TICKER_DIR = ROOT / "analysis" / "by_ticker"
LOG_DIR = ROOT / "logs"
PROMPTS_DIR = ROOT / "prompts"

DATA_DIR.mkdir(exist_ok=True)
ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
BY_TICKER_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


def resolve_report_md(md_path) -> Path:
    """저장된 보고서 md 경로를 절대경로로 해석.
    신규는 ANALYSIS_DIR 기준 **상대경로**로 저장돼 폴더를 통째로 옮겨도 안전하다.
    구버전 절대경로도 받되, 그 위치에 파일이 없으면(=폴더 이동됨) 'auto/' 뒷부분을
    현재 ANALYSIS_DIR에 재결합해 찾는다."""
    p = Path(md_path)
    if p.is_absolute():
        if p.exists():
            return p
        parts = p.parts
        if "auto" in parts:                       # ...\analysis\auto\<주차>\x.md
            return ANALYSIS_DIR.joinpath(*parts[parts.index("auto") + 1:])
        return p
    return ANALYSIS_DIR / p


def require_telegram() -> tuple[str, str]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 .env에 없습니다.")
    return TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


# ---------------------------------------------------------------------------
# Claude Code SDK 설정 디렉토리 격리
# ---------------------------------------------------------------------------
# 스크리너는 종목당 5개 서브에이전트(+공시 요약/종합)를 병렬로 돌리는데, 각각
# claude_agent_sdk가 claude.exe 서브프로세스를 띄운다. 이 프로세스들이 홈의
# ~/.claude.json(계정·상태 파일)을 동시에 read-modify-write 하면 경합으로 깨질
# 수 있다 — 특히 같은 머신에서 대화형 Claude Code 세션을 병행할 때.
# → 프로젝트 전용 CLAUDE_CONFIG_DIR로 분리해 홈 config와의 충돌을 원천 차단한다.
# 첫 호출 시 홈의 계정/설정을 1회 시드하므로 별도 재로그인이 필요 없다. 단 OAuth
# 토큰은 갱신 시 리프레시 토큰이 회전(rotate)돼 복사본이 무효화(401)되므로, sdk_env
# 가 매 호출마다 홈의 신선한 토큰을 격리본으로 끌어온다(refresh_isolated_credentials).
# 홈·격리본 모두 만료되면 scheduler가 텔레그램으로 알린다(원격 재로그인 유도).
#
# 격리를 끄려면 .env에 SCREENER_CLAUDE_CONFIG_DIR=off (또는 none/0) → 홈 config를
# 그대로 쓴다. 다른 경로를 쓰려면 그 경로를 지정.
# (_opt가 빈 값을 default로 덮으므로 빈 값이 아니라 명시 sentinel로 끈다)
_cfg_raw = _opt("SCREENER_CLAUDE_CONFIG_DIR", str(ROOT / ".claude_config"))
CLAUDE_CONFIG_DIR = "" if _cfg_raw.lower() in ("off", "none", "0") else _cfg_raw

# 홈에서 격리 dir로 1회 시드할 인증/설정 파일.
# (src 상대경로는 홈 기준, dst는 CLAUDE_CONFIG_DIR 기준)
_SEED_FILES = (
    ".claude.json",                 # 계정(userID·oauthAccount) + 상태 → ~/.claude.json
    ".claude/settings.json",        # 사용자 설정(선택)
)   # .credentials.json(OAuth 토큰)은 refresh_isolated_credentials가 매번 신선하게 관리

_sdk_env_cache: dict[str, str] | None = None


def _seed_claude_config(cfg: Path) -> None:
    """격리 dir에 홈의 계정/설정을 1회 시드(idempotent)해 재로그인을 피한다.
    OAuth 토큰(.credentials.json)은 여기서 다루지 않는다 — 토큰은 갱신 시 회전돼
    옛 복사본이 무효화되므로 refresh_isolated_credentials가 '매 호출마다 홈에서
    신선본을 끌어오는' 방식으로 따로 관리한다.
    실패해도 치명적이지 않다 — 필요 시 해당 dir에서 직접 `claude` 로그인하면 된다."""
    home = Path.home()
    for rel in _SEED_FILES:
        src = home / rel
        dst = cfg / Path(rel).name      # 격리 dir에는 평탄하게
        try:
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
        except Exception:
            pass


# 홈 OAuth 토큰 파일 (대화형 Claude Code가 사용·자동 갱신하는 원본).
_HOME_CRED = Path.home() / ".claude" / ".credentials.json"


def _cred_expiry_ms(path: Path) -> float | None:
    """credentials.json의 OAuth 만료시각(ms). 읽기 실패·필드 없음이면 None."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        exp = (d.get("claudeAiOauth") or {}).get("expiresAt")
        return float(exp) if exp else None
    except Exception:
        return None


def refresh_isolated_credentials(cfg: Path) -> None:
    """홈 OAuth 토큰이 격리본보다 신선하면 격리 dir로 복사해 항상 최신을 유지한다.
    대화형 Claude Code가 홈 토큰을 자동 갱신하므로 격리본도 따라 신선해진다 →
    '한 번 복사 후 회전으로 무효화'되어 401 나던 문제를 막는다.

    안전장치: 홈 토큰이 이미 만료됐으면 복사하지 않는다(만료본을 격리본에 덮으면
    서브프로세스가 자가 갱신→리프레시 토큰 회전→대화형 세션까지 깨질 수 있다).
    이 경우 격리본을 그대로 두고, scheduler의 만료 점검이 텔레그램 알림을 보낸다.
    동시 복사로 파일이 깨지지 않게 임시본 생성 후 os.replace로 원자 교체한다."""
    dst = cfg / ".credentials.json"
    tmp = None
    try:
        if not _HOME_CRED.exists():
            return
        home_exp = _cred_expiry_ms(_HOME_CRED)
        if home_exp is not None and home_exp / 1000.0 <= time.time():
            return                              # 홈도 만료 → 덮지 않음
        if dst.exists() and _HOME_CRED.stat().st_mtime <= dst.stat().st_mtime:
            return                              # 격리본이 이미 같거나 더 신선
        tmp = dst.with_name(f".credentials.json.tmp{os.getpid()}")
        shutil.copy2(_HOME_CRED, tmp)
        os.replace(tmp, dst)
    except Exception:
        if tmp is not None:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


def credential_seconds_left() -> float | None:
    """서브프로세스가 실제로 쓸 토큰의 만료까지 남은 초. 만료면 음수, 모르면 None.
    (격리 ON이면 격리본, OFF면 홈 토큰 기준.)"""
    p = (Path(CLAUDE_CONFIG_DIR) / ".credentials.json"
         if CLAUDE_CONFIG_DIR else _HOME_CRED)
    exp = _cred_expiry_ms(p)
    return None if exp is None else exp / 1000.0 - time.time()


def home_credential_seconds_left() -> float | None:
    """홈 OAuth 토큰의 만료까지 남은 초(격리와 무관, 항상 홈 기준).
    keepalive 갱신 판단에 쓴다 — 홈을 갱신해야 대화형 세션과 충돌하지 않는다."""
    exp = _cred_expiry_ms(_HOME_CRED)
    return None if exp is None else exp / 1000.0 - time.time()


def sdk_env() -> dict[str, str]:
    """ClaudeAgentOptions(env=...)에 넣을 환경변수.
    홈 ~/.claude.json 동시 write 경합을 막기 위해 CLAUDE_CONFIG_DIR을 프로젝트
    전용으로 고정한다. SDK는 이 dict를 부모 환경에 '병합'하므로 PATH 등은 유지된다.
    빈 값으로 격리를 끄면 {} 를 반환(=홈 config 사용).
    매 호출마다 홈의 신선한 OAuth 토큰을 격리본으로 끌어온다 — 모든 LLM 호출이 이
    함수를 거치므로 토큰이 항상 최신으로 유지된다."""
    global _sdk_env_cache
    if not CLAUDE_CONFIG_DIR:
        _sdk_env_cache = {}
        return _sdk_env_cache
    cfg = Path(CLAUDE_CONFIG_DIR)
    if _sdk_env_cache is None:
        cfg.mkdir(parents=True, exist_ok=True)
        _seed_claude_config(cfg)            # 최초 1회: 계정/설정 시드
        _sdk_env_cache = {"CLAUDE_CONFIG_DIR": str(cfg)}
    refresh_isolated_credentials(cfg)       # 매 호출: 홈이 더 신선하면 토큰 갱신
    return _sdk_env_cache
