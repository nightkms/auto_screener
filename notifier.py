"""
S5: 텔레그램 알림.

분석 끝나면 모든 종목 결과 + 마크다운 보고서 본문까지 전송한다.
순서:
    1. 요약 1건 (등급별 카운트)
    2. 종목별: 헤더 + 마크다운 본문 (4000자 단위 분할)
    3. 마무리 1건 (대시보드 링크)

CLI:
    python notifier.py test         # 봇 연결 검증
    python notifier.py last         # 가장 최근 run 결과 다시 전송
    python notifier.py 5            # run_id=5 결과 전송
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

import aiohttp

import config
import storage

log = logging.getLogger("notifier")

TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_MAX = 4000          # 텔레그램 한 메시지 글자 한도 (safety margin)
RATE_GAP = 0.3         # 메시지 사이 텀

EMOJI = {
    "STRONG": "🟢", "WATCH": "🟡", "INTEREST": "⚪",
    "SKIP": "⚫", "crashed": "💥",
}


# ---------------------------------------------------------------------------
# 텔레그램 호출
# ---------------------------------------------------------------------------
async def _send(session: aiohttp.ClientSession, text: str,
                parse_mode: str | None = "Markdown") -> bool:
    try:
        token, chat = config.require_telegram()
    except RuntimeError as e:
        log.warning("텔레그램 비활성: %s", e)
        return False

    url = TG_API.format(token=token, method="sendMessage")
    payload = {
        "chat_id": chat,
        "text": text[:TG_MAX],
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    async with session.post(url, json=payload, timeout=15) as r:
        body = await r.text()
        if r.status != 200:
            log.error("send 실패 status=%s body=%s", r.status, body[:200])
            # Markdown 파싱 에러면 plain으로 재시도
            if parse_mode and "can't parse entities" in body.lower():
                payload.pop("parse_mode", None)
                async with session.post(url, json=payload, timeout=15) as r2:
                    return r2.status == 200
            return False
        return True


def _convert_tables(md: str) -> str:
    """마크다운 표를 bullet 리스트로 변환. 영역·점수·요지 패턴 우선."""
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        is_table_start = (
            line.lstrip().startswith("|")
            and i + 1 < len(lines)
            and re.match(r"^\s*\|[\s\-:|]+\|\s*$", lines[i + 1])
        )
        if not is_table_start:
            out.append(line)
            i += 1
            continue

        # 헤더 행 스킵, 구분 행 스킵
        i += 2
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            row = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            row = [c for c in row if c is not None]
            if not any(row):
                i += 1
                continue
            # ** 제거하고 평가
            name = row[0].strip("* ").strip()
            score = row[1].strip("* ").strip() if len(row) > 1 else ""
            note  = row[2].strip("* ").strip() if len(row) > 2 else ""
            is_avg = "평균" in name or "average" in name.lower()
            if is_avg:
                out.append(f"➤ {name} ★{score}".rstrip())
            elif re.match(r"^[0-9.]+$", score):
                out.append(f"• {name} ★{score}")
                if note:
                    out.append(f"   {note}")
            else:
                cells = [c for c in row if c]
                out.append("• " + " / ".join(cells))
            i += 1
    return "\n".join(out)


def _strip_markdown(md: str) -> str:
    """마크다운 보고서를 텔레그램 plain text 친화 형식으로 변환."""
    text = _convert_tables(md)
    # 헤더 (## → ▌, ### → ▸)
    text = re.sub(r"^###\s+(.+)$", r"▸ \1", text, flags=re.MULTILINE)
    text = re.sub(r"^##\s+(.+)$", r"\n▌ \1", text, flags=re.MULTILINE)
    text = re.sub(r"^#\s+(.+)$", r"\n■ \1", text, flags=re.MULTILINE)
    # **굵게** → 그대로 (텔레그램 plain에선 굵게 표시 안되므로 표식만 제거)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    # 단일 *는 일부러 안 건드림 (별점 등에 쓰여 헷갈림 방지)
    # `inline code` → 그대로
    text = re.sub(r"`([^`]+?)`", r"\1", text)
    # > 인용 prefix 제거
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # 표 잔재(파이프) 제거
    text = re.sub(r"^\s*\|.*\|\s*$", "", text, flags=re.MULTILINE)
    # 연속 빈 줄 압축
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _chunk(text: str, max_len: int = TG_MAX - 200) -> list[str]:
    """긴 텍스트를 max_len 안에서 분할. 가능하면 newline 경계."""
    text = text.rstrip()
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


# ---------------------------------------------------------------------------
# 메인 알림 흐름
# ---------------------------------------------------------------------------
async def notify_run(run_id: int) -> bool:
    runs = storage.recent_runs(limit=50)
    run = next((r for r in runs if r["id"] == run_id), None)
    if not run:
        log.warning("run %d 없음", run_id)
        return False
    reports = storage.reports_for_run(run_id)

    counts = {"STRONG": 0, "WATCH": 0, "INTEREST": 0, "SKIP": 0}
    for r in reports:
        if r["grade"] in counts:
            counts[r["grade"]] += 1

    async with aiohttp.ClientSession() as session:
        # 1) 요약
        summary = (
            f"📊 *주간 자동 분석* — {run['week_label']}\n"
            f"실행 #{run['id']} : {run['started_at']} → {run['ended_at'] or '?'}\n"
            f"총 {len(reports)}건 — "
            f"🟢 STRONG {counts['STRONG']} / 🟡 WATCH {counts['WATCH']} / "
            f"⚪ INTEREST {counts['INTEREST']} / ⚫ SKIP {counts['SKIP']}"
        )
        await _send(session, summary)
        await asyncio.sleep(RATE_GAP)

        # 2) 종목별 (등급 좋은 순). 헤더 + 본문 1건으로 합쳐서 전송
        rank = {"STRONG": 3, "WATCH": 2, "INTEREST": 1, "SKIP": 0}
        ordered = sorted(reports, key=lambda r: (-rank.get(r["grade"], -1),
                                                  -(r["avg_rating"] or 0)))
        for r in ordered:
            emoji = EMOJI.get(r["grade"], "❓")
            header = (
                f"{emoji} {r['name']} ({r['ticker']})\n"
                f"등급 {r['grade']} · 평균 ★ {r['avg_rating']}"
            )
            md_path = Path(r["md_path"]) if r.get("md_path") else None
            if md_path and md_path.exists():
                body = _strip_markdown(md_path.read_text(encoding="utf-8"))
            else:
                body = "(보고서 파일 없음)"
            full = f"{header}\n{'─' * 30}\n{body}"
            for chunk in _chunk(full):
                await _send(session, chunk, parse_mode=None)
                await asyncio.sleep(RATE_GAP)

        # 3) 마무리
        url = config.dashboard_url("/")          # run 상세 페이지 제거 → 대시보드 홈
        tail = f"✅ 분석 완료. 자세히 보기: {url}"
        await _send(session, tail)
    return True


async def notify_single_report(report_id: int,
                                source: str = "manual") -> bool:
    """종목 1개 분석 완료 즉시 보고서 알림. 헤더 + 본문 1건(긴 보고서는 chunk).

    알림 정책 (source별):
    - 'manual' / 'telegram': 등급 무관 무조건 전송 (사용자가 직접 지정한 종목)
    - 'auto_weekly' / 'auto_hourly': grade=='STRONG' 일 때만 전송 (자동 스크리닝)
    """
    with storage._connect() as c:
        row = c.execute("SELECT * FROM reports WHERE id=?",
                        (report_id,)).fetchone()
    if not row:
        log.warning("report %d 없음", report_id)
        return False
    r = dict(row)
    if source in ("auto_weekly", "auto_hourly") and r.get("grade") != "STRONG":
        log.info("[skip alert] report=%d %s grade=%s (%s는 STRONG만 알림)",
                 report_id, r.get("ticker"), r.get("grade"), source)
        return False
    emoji = EMOJI.get(r["grade"], "❓")
    src_tag = {
        "manual": "📌 수동",
        "telegram": "📱 텔레그램",
        "auto_weekly": "🔁 주간자동",
        "auto_hourly": "⏱️ 시간자동",
    }.get(source, source)
    report_url = config.dashboard_url(f"/report/{report_id}")
    header = (
        f"{emoji} {r['name']} ({r['ticker']}) — {src_tag}\n"
        f"등급 {r['grade']} · 평균 ★ {r['avg_rating']}\n"
        f"보고서: {report_url}"
    )
    md_path = Path(r["md_path"]) if r.get("md_path") else None
    if md_path and md_path.exists():
        body = _strip_markdown(md_path.read_text(encoding="utf-8"))
    else:
        body = "(보고서 파일 없음)"
    full = f"{header}\n{'─' * 30}\n{body}"
    async with aiohttp.ClientSession() as session:
        ok = True
        for chunk in _chunk(full):
            if not await _send(session, chunk, parse_mode=None):
                ok = False
            await asyncio.sleep(RATE_GAP)
        return ok


async def notify_run_summary(run_id: int) -> bool:
    """모든 종목 분석이 끝난 후 등급 카운트 + 대시보드 링크 1건."""
    runs = storage.recent_runs(limit=50)
    run = next((r for r in runs if r["id"] == run_id), None)
    if not run:
        return False
    reports = storage.reports_for_run(run_id)
    counts = {"STRONG": 0, "WATCH": 0, "INTEREST": 0, "SKIP": 0}
    for r in reports:
        if r["grade"] in counts:
            counts[r["grade"]] += 1
    url = config.dashboard_url("/")          # run 상세 페이지 제거 → 대시보드 홈
    text = (
        f"✅ 분석 완료 — {run['week_label']} run #{run_id}\n"
        f"총 {len(reports)}건 — "
        f"🟢 {counts['STRONG']} / 🟡 {counts['WATCH']} / "
        f"⚪ {counts['INTEREST']} / ⚫ {counts['SKIP']}\n"
        f"대시보드: {url}"
    )
    async with aiohttp.ClientSession() as session:
        return await _send(session, text, parse_mode=None)


async def notify_error(message: str, run_id: int | None = None,
                        context: str = "") -> bool:
    """파이프라인·워커 실패를 짧게 텔레그램으로."""
    lines = ["🚨 AutoScreener 에러"]
    if run_id:
        lines.append(f"run #{run_id}")
    if context:
        lines.append(f"위치: {context}")
    lines.append("")
    lines.append(message[:1500])
    text = "\n".join(lines)
    async with aiohttp.ClientSession() as session:
        return await _send(session, text, parse_mode=None)


async def notify_price_alert(ticker: str, name: str, base_price: float,
                              current_price: float, change_pct: float,
                              base_grade: str | None = None,
                              base_date: str | None = None) -> bool:
    sign = "📈" if change_pct >= 0 else "📉"
    # base_date는 ISO 'YYYY-MM-DDTHH:MM:SS' 형태로 들어옴. 날짜만 떼서 표시.
    base_date_str = (base_date or "")[:10]
    head_meta = []
    if base_date_str:
        head_meta.append(f"기준일 {base_date_str}")
    if base_grade:
        head_meta.append(base_grade)
    parts = [f"{sign} 주가 알림 {name} ({ticker})"]
    if head_meta:
        parts.append(" · ".join(head_meta))
    parts.append(
        f"기준 {base_price:,.0f}원 → 현재 {current_price:,.0f}원 ({change_pct:+.2f}%)"
    )
    async with aiohttp.ClientSession() as session:
        return await _send(session, "\n".join(parts), parse_mode=None)


async def notify_test() -> bool:
    async with aiohttp.ClientSession() as session:
        return await _send(session, "✅ *AutoScreener* 봇 연결 테스트 성공")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main():
    logging.basicConfig(level=config.LOG_LEVEL,
                        format="%(asctime)s %(name)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    if cmd == "test":
        ok = asyncio.run(notify_test())
        print("OK" if ok else "FAILED")
    elif cmd == "last":
        runs = storage.recent_runs(limit=1)
        if not runs:
            print("실행 이력 없음")
            return
        ok = asyncio.run(notify_run(runs[0]["id"]))
        print("OK" if ok else "FAILED")
    elif cmd.isdigit():
        ok = asyncio.run(notify_run(int(cmd)))
        print("OK" if ok else "FAILED")
    else:
        print("Usage: python notifier.py [test|last|<run_id>]")


if __name__ == "__main__":
    _main()
