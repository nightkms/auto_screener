#!/usr/bin/env python3
"""
커밋 전 비밀값/개인정보 차단 스캐너 (pre-commit 훅에서 호출).

staged 내용에서 아래를 탐지하면 커밋을 거부(exit 1)한다:
  1) 로컬 .env 의 실제 값이 그대로 들어간 경우      ← 하드블록 (pragma 무시 불가)
  2) 비밀값 패턴 (텔레그램 토큰/프라이빗키/AWS키/일반 secret 할당 등)
  3) 개인정보 (이메일, Windows 사용자 경로)
  4) 올리면 안 되는 파일명 (.env, *.pem, *.key, id_rsa ...)

오탐(false positive) 회피:
  - 해당 라인 끝에 `pragma: allowlist secret` 주석이 있으면 (1) 외 패턴 탐지는 건너뜀
  - .env.example / .env.sample / .env.template 은 파일명 차단에서 제외
  - 값이 비어있거나 os.getenv/<...>/your-.../changeme 같은 플레이스홀더면 무시

실제 비밀 문자열은 이 파일에 박지 않는다 (그러면 그 자체가 유출). .env 에서 런타임에 읽는다.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# 콘솔 인코딩(특히 한국어 Windows cp949)에서 메시지가 깨지지 않게 UTF-8 고정.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

ALLOW_PRAGMA = "pragma: allowlist secret"

# --- 올리면 안 되는 파일명 -------------------------------------------------
BLOCK_FILENAME = re.compile(
    r"(^|/)(\.env(\.local|\.prod|\.production)?|"
    r"id_rsa|id_dsa|id_ecdsa|id_ed25519|"
    r".*\.(pem|key|pfx|p12|keystore|ppk|jks))$",
    re.IGNORECASE,
)
ALLOW_FILENAME_SUFFIX = (".env.example", ".env.sample", ".env.template")

# --- 콘텐츠 패턴 -----------------------------------------------------------
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Email address (PII)", re.compile(
        r"\b[A-Za-z0-9._%+\-]+@(?!example\.com|anthropic\.com|sentry\.io|"
        r"schemas\.|w3\.org)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("Windows user/work path", re.compile(r"[A-Za-z]:\\(Users|Work)\\[^\\\s\"']+", re.IGNORECASE)),
]

# 일반 secret 할당: key = "value" — 값이 진짜 비밀처럼 보일 때만
SECRET_ASSIGN = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|access[_-]?key|client[_-]?secret|"
    r"password|passwd|pwd|auth)\b\s*[:=]\s*[\"']?([A-Za-z0-9_\-./+=]{12,})[\"']?"
)
PLACEHOLDER = re.compile(
    r"^(os\.getenv|os\.environ|getenv|process\.env|<.*>|\{\{.*\}\}|\$\{?\w+|"
    r"your[_\-]|xxx+|changeme|placeholder|dummy|example|none|null|true|false)",
    re.IGNORECASE,
)


def _run(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout


def _repo_root() -> Path:
    return Path(_run(["rev-parse", "--show-toplevel"]).strip())


def _staged_files() -> list[str]:
    out = _run(["diff", "--cached", "--name-only", "--diff-filter=ACM"])
    return [f for f in out.splitlines() if f.strip()]


# .env 항목 중 "진짜 비밀"만 하드블록 대상으로 본다.
# (CRON/TIMEZONE/PORT/MODEL/LOG_LEVEL 같은 설정값이 코드·문서에 등장하는 오탐 방지)
SENSITIVE_ENV_KEY = re.compile(
    r"(KEY|TOKEN|SECRET|PASS(WORD|WD)?|PWD|CREDENTIAL|AUTH|PRIVATE|CHAT_ID|SESSION|COOKIE)",
    re.IGNORECASE,
)


def _env_secret_values(root: Path) -> list[str]:
    """로컬 .env 의 비밀 값들 (하드블록 대상). 코드에 박지 않고 런타임에 읽음.

    포함 기준: 키 이름이 비밀스럽거나(KEY/TOKEN/SECRET…), 값이 길고 공백 없는
    고엔트로피 토큰(len>=20)일 때만. cron/timezone/port/model 등 설정값은 제외.
    """
    env = root / ".env"
    vals: list[str] = []
    if not env.exists():
        return vals
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        v = raw.strip().strip("\"'")
        if len(v) < 6:  # 짧은 값(포트 등) 제외
            continue
        key_sensitive = bool(SENSITIVE_ENV_KEY.search(key))
        value_secretish = len(v) >= 20 and not re.search(r"\s", v)
        if key_sensitive or value_secretish:
            vals.append(v)
    return vals


def _added_lines() -> list[tuple[str, int, str]]:
    """staged diff 의 추가(+) 라인들 → (파일, 라인번호, 내용)."""
    diff = _run(["diff", "--cached", "--unified=0", "--no-color"])
    results: list[tuple[str, int, str]] = []
    cur_file = ""
    new_ln = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur_file = line[6:]
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_ln = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            results.append((cur_file, new_ln, line[1:]))
            new_ln += 1
    return results


def main() -> int:
    root = _repo_root()
    findings: list[str] = []        # 일반(패턴) — pragma로 무시 가능
    hard: list[str] = []            # 하드블록 — .env 실값/금지 파일명

    # 1) 금지 파일명
    for f in _staged_files():
        low = f.lower()
        if any(low.endswith(s) for s in ALLOW_FILENAME_SUFFIX):
            continue
        if BLOCK_FILENAME.search(f):
            hard.append(f"  [금지 파일]  {f}  ← 이 파일은 절대 커밋 불가")

    # 2) 콘텐츠 스캔
    env_vals = _env_secret_values(root)
    for fpath, lineno, text in _added_lines():
        loc = f"{fpath}:{lineno}"

        # 2a) .env 실제 값이 그대로 노출 → 하드블록
        for v in env_vals:
            if v in text:
                hard.append(f"  [.env 실값 노출]  {loc}  ← .env 의 비밀값이 코드에 들어감")
                break

        if ALLOW_PRAGMA in text:
            continue  # 의도적 허용 라인은 패턴 검사 생략

        # 2b) 명시 패턴
        for name, pat in PATTERNS:
            if pat.search(text):
                findings.append(f"  [{name}]  {loc}")

        # 2c) 일반 secret 할당
        m = SECRET_ASSIGN.search(text)
        if m and not PLACEHOLDER.match(m.group(2)):
            findings.append(f"  [secret 할당: {m.group(1)}]  {loc}")

    if not hard and not findings:
        return 0

    sys.stderr.write("\n" + "=" * 64 + "\n")
    sys.stderr.write("🚫 커밋 차단: 올리면 안 되는 내용이 staged 에 있습니다.\n")
    sys.stderr.write("=" * 64 + "\n")
    if hard:
        sys.stderr.write("\n■ 하드블록 (반드시 제거):\n")
        sys.stderr.write("\n".join(dict.fromkeys(hard)) + "\n")
    if findings:
        sys.stderr.write("\n■ 의심 항목:\n")
        sys.stderr.write("\n".join(dict.fromkeys(findings)) + "\n")
    sys.stderr.write(
        "\n조치:\n"
        "  • 해당 값을 .env 로 옮기고 코드는 config/os.getenv 로 읽으세요.\n"
        "  • 파일을 빼려면: git restore --staged <파일>\n"
        "  • 오탐이면 해당 라인 끝에 주석 `# pragma: allowlist secret` 추가\n"
        "    (단 .env 실값 노출/금지 파일명은 pragma 로도 통과 안 됨)\n\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
