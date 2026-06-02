"""중앙 설정 로더. .env에서 모든 환경 변수 읽고 검증."""
from __future__ import annotations
import os
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

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "screener.db"
ANALYSIS_DIR = ROOT.parent / "analysis" / "auto"
BY_TICKER_DIR = ROOT.parent / "analysis" / "by_ticker"
PROMPTS_DIR = ROOT / "prompts"

DATA_DIR.mkdir(exist_ok=True)
ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
BY_TICKER_DIR.mkdir(parents=True, exist_ok=True)


def require_telegram() -> tuple[str, str]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 .env에 없습니다.")
    return TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
