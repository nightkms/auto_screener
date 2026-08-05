"""
S5: 텔레그램 알림.

종목 1건은 **메시지 2건**으로 나눠 보낸다 (`build_single_messages`):
1. 요약(`summarize_brief`) — 등급·평균★·실적변화 이벤트·전문 링크 + 분석일/시장/종가
   + 한 줄 요약 + 영역별 매트릭스(점수와 함께 요지·평균까지)
2. 상세(`summarize_detail`) — 위에 담기지 않은 **나머지 섹션 전부**를 요약 없이 그대로

밖에서 대시보드 링크를 못 여는 상황을 전제하므로, 2건을 합치면 보고서 내용이 빠짐없이
전달된다. 1건만 읽고 넘길지는 사용자가 고른다.

CLI:
    python notifier.py test                  # 봇 연결 검증
    python notifier.py last                  # 가장 최근 run 결과 다시 전송
    python notifier.py 5                     # run_id=5 결과 전송
    python notifier.py preview 42            # 전송 없이 report 42 메시지 미리보기
    python notifier.py preview 42 manual     # source 지정(알림 정책 그대로 적용)
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

# 실적변화 이벤트 알림에 붙는 지표 범례 (레버리지·신뢰도 뜻 상기용)
#   A~D = 실적 기여 크기(A: 연매출 +30%↑ / B: +10~30% / C: +3~10% / D: <3%·규모 미공개)
#   ★1~5 = 실현 신뢰도(5: confirmed+수주확보+금액 명확 / 1: rumor·계획 단계뿐)
EVENT_LEGEND = "ℹ️ 레버리지 A~D=실적 기여 크기(A 최대) · ★1~5=실현 신뢰도(5 최고)"


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


# ---------------------------------------------------------------------------
# 보고서 → 텔레그램 본문 (1차 요약 / 2차 상세)
#   1차는 '상세를 열지 말지' 판단용으로 압축하되, 2차는 나머지를 통째로 옮긴다.
#   둘을 합쳐 보고서 전체가 되도록 — 링크를 못 여는 상황이 기본 전제다.
# ---------------------------------------------------------------------------
SUMMARY_MAX = 7000          # 상세 본문 글자 상한 (넘치면 _chunk가 여러 건으로 분할)
BRIEF_MAX = 1200            # 1차 요약 글자 상한 (한 줄 요약 + 영역별 점수·요지)

# 1차 요약이 이미 담는 섹션 → 2차 상세에선 중복이라 뺀다
_BRIEF_SECTIONS = ("한 줄 요약", "영역별 매트릭스")

# 매트릭스 영역명 축약 (부분 문자열 매칭)
_AREA_SHORT = {
    "밸류": "밸류", "산업": "산업", "주가": "수급",
    "카탈리스트": "촉매", "리스크": "리스크",
}
# 본문 메타 태그([시점=…] 등). 2차 상세는 원문 그대로 보내므로 태그를 살리고,
# 1차 요약(한 줄 요약·매트릭스 요지)에서만 소음으로 보고 제거한다 → `_clean(keep_meta=)`
_META_PAT = re.compile(r"\s*\[(?:시점|예정시점|발생일|확정도|상태|반영)[^\]]*\]")


def _norm(t: str) -> str:
    """섹션 제목 비교용 정규화 — `요약(경량)`과 `요약 (경량)`을 같게 본다."""
    return re.sub(r"\s+", "", t)


def _sections(md: str) -> dict[str, str]:
    """`## 제목` 단위로 (제목 → 본문) 매핑. 같은 제목이면 뒤엣것이 이긴다."""
    out: dict[str, str] = {}
    cur, buf = "", []
    for line in md.split("\n"):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if cur:
                out[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1), []
        elif cur:
            buf.append(line)
    if cur:
        out[cur] = "\n".join(buf).strip()
    return out


def _sec(secs: dict[str, str], *keys: str) -> str:
    """제목에 keys 중 하나라도 포함된 첫 섹션 본문 (공백 무시 비교)."""
    for title, body in secs.items():
        nt = _norm(title)
        if any(_norm(k) in nt for k in keys):
            return body
    return ""


def _clean(text: str, limit: int = 90, keep_meta: bool = False) -> str:
    """마크다운 장식 제거 후 길이 제한. keep_meta면 `[시점=…]` 태그를 남긴다."""
    t = re.sub(r"`([^`]+?)`", r"\1", text)     # 백틱 먼저 풀어야 메타 태그가 잡힌다
    if not keep_meta:
        t = _META_PAT.sub("", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = t.replace("`", "").strip()             # 짝 안 맞는 잔재 백틱
    t = re.sub(r"\s+", " ", t)
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _matrix_block(sec: str, note_len: int = 0) -> str:
    """영역별 매트릭스 표 → 점수 한 줄. note_len>0이면 영역별 '요지'까지 줄바꿈해 붙인다.

        ⭐ 밸류 4.0 · 산업 4.0 · 수급 3.5 · 촉매 3.5 · 리스크 3.5  (평균 3.70)
        • 밸류 4.0 — PER 19.8·PBR 2.4, 목표가 74,500원(+27.6% 여력)…
        ...
    점수만으론 왜 그 등급인지 알 수 없어서, 1차 요약엔 요지를 반드시 함께 넣는다.
    """
    rows: list[tuple[str, str, str]] = []
    avg = ""
    for row in sec.split("\n"):
        if not row.lstrip().startswith("|"):
            continue
        cells = [c.strip().strip("*").strip()
                 for c in row.strip().strip("|").split("|")]
        if len(cells) < 2 or re.match(r"^[\s\-:]*$", cells[0]):
            continue
        name, score = cells[0], cells[1]
        note = cells[2] if len(cells) > 2 else ""
        if "영역" in name and "점수" in score:      # 헤더 행
            continue
        if "평균" in name:
            avg = score
            continue
        short = next((v for k, v in _AREA_SHORT.items() if k in name), name)
        rows.append((short, score, note))
    if not rows:
        return ""
    head = "⭐ " + " · ".join(f"{n} {s}" for n, s, _ in rows)
    if avg:
        head += f"  (평균 {avg})"
    if note_len <= 0:
        return head
    lines = [head]
    for n, s, note in rows:
        lines.append(f"• {n} {s} — {_clean(note, note_len)}" if note
                     else f"• {n} {s}")
    return "\n".join(lines)


def _cap(blocks: list[str], max_len: int) -> str:
    text = "\n\n".join(b for b in blocks if b)
    if len(text) > max_len:
        text = text[:max_len].rsplit("\n", 1)[0].rstrip() + "\n…(이하 생략)"
    return text


def _meta_line(md: str) -> str:
    """보고서 머리의 `> 분석일: … / 시장: … / 종가: …` 인용줄."""
    m = re.search(r"^>\s*(.+)$", md, flags=re.MULTILINE)
    return _clean(m.group(1), 200) if m else ""


def _drop_tokens(body: str) -> str:
    """기계 판독용 토큰 줄(GRADE:/EARNINGS_EVENT:) 제거 — 사람이 읽을 내용만 남긴다."""
    keep = [ln for ln in body.split("\n")
            if not re.match(r"^\s*[-*•]?\s*(GRADE|EARNINGS_EVENT)\s*:", ln)]
    return "\n".join(keep).strip()


def summarize_brief(md: str, max_len: int = BRIEF_MAX) -> str:
    """1차 요약: 분석일·시장·종가 + 한 줄 요약 + 영역별 매트릭스(요지·평균까지).

    여기까지 보고 상세를 열지 말지 판단한다. 나머지는 전부 2차(`summarize_detail`).
    """
    secs = _sections(md)
    blocks = [
        _meta_line(md),
        _clean(_sec(secs, "한 줄 요약"), 400),
        _matrix_block(_sec(secs, "매트릭스"), note_len=120),
    ]
    return _cap(blocks, max_len)


def summarize_detail(md: str, max_len: int = SUMMARY_MAX) -> str:
    """2차 상세: 1차에 담은 섹션을 뺀 **나머지 전부**를 원문 순서대로.

    급등 사유·핵심 포인트·단기 트리거·중장기 실적변화 이벤트·모니터링 우선순위·
    등급 근거·이전 회차 대비 변동 등 — 요약하지 않고 그대로 옮긴다. 담을 내용이
    없으면 빈 문자열(→ 상세 메시지 자체를 보내지 않는다).
    """
    parts: list[str] = []
    for title, body in _sections(md).items():
        if any(_norm(k) in _norm(title) for k in _BRIEF_SECTIONS):
            continue                      # 1차와 중복
        body = _drop_tokens(body)
        if not body:
            continue                      # 토큰만 있던 섹션(실적변화 이벤트 판정 등)
        parts.append(f"## {title}\n{body}")
    if not parts:
        return ""
    return _cap([_strip_markdown("\n\n".join(parts))], max_len)


def summarize_report(md: str, max_len: int = SUMMARY_MAX) -> str:
    """요약+상세 합본 1건 (run 재전송·폴백용). 파싱 실패 시 전문 앞부분."""
    text = _cap([summarize_brief(md), summarize_detail(md)], max_len)
    return text or _strip_markdown(md)[:max_len]


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
            report_url = config.dashboard_url(f"/report/{r['id']}")
            header = (
                f"{emoji} {r['name']} ({r['ticker']})\n"
                f"등급 {r['grade']} · 평균 ★ {r['avg_rating']}\n"
                f"전문: {report_url}"
            )
            md_path = config.resolve_report_md(r["md_path"]) if r.get("md_path") else None
            if md_path and md_path.exists():
                body = summarize_report(md_path.read_text(encoding="utf-8"))
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


def build_single_messages(report_id: int,
                           source: str = "manual") -> list[str]:
    """단건 보고서 → 텔레그램 메시지 **2건**. 알림 대상이 아니면 빈 리스트.

    밖에서는 대시보드 링크를 못 여는 경우가 많아, 전문을 두 단계로 나눠 보낸다:
    1) 요약 — 종목·등급·평균★·실적변화 이벤트·전문 링크 + 분석일/시장/종가 +
       한 줄 요약 + 영역별 매트릭스(요지·평균까지)
    2) 상세 — 위에 담기지 않은 **나머지 섹션 전부**(급등 사유·핵심 포인트·단기
       트리거·중장기 실적변화 이벤트·모니터링·등급 근거·이전 회차 대비 변동)
    요약만 보고 넘길지, 이어지는 상세까지 읽을지 사용자가 고른다.

    알림 정책 (source별):
    - 'manual' / 'telegram': 등급 무관 무조건 전송 (사용자가 직접 지정한 종목)
    - 'auto_weekly' / 'auto_hourly': grade=='STRONG' **또는** 향후 1~2년 실적변화
      이벤트(earnings_event)가 잡힌 종목이면 전송.
      단 **SKIP 등급은 이벤트가 있어도 제외** (볼 가치 낮음).
    """
    with storage._connect() as c:
        row = c.execute("SELECT * FROM reports WHERE id=?",
                        (report_id,)).fetchone()
    if not row:
        log.warning("report %d 없음", report_id)
        return []
    r = dict(row)
    # DB에 저장된 이벤트 노트엔 마크다운 잔재(백틱 등)가 섞여 있을 수 있다
    event_note = _clean((r.get("earnings_event") or "").strip(), 140)
    if source in ("auto_weekly", "auto_hourly"):
        grade = r.get("grade")
        if grade == "SKIP":
            log.info("[skip alert] report=%d %s grade=SKIP (%s는 SKIP 제외)",
                     report_id, r.get("ticker"), source)
            return []
        if grade != "STRONG" and not event_note:
            log.info("[skip alert] report=%d %s grade=%s (%s는 STRONG 또는 "
                     "실적변화 이벤트만 알림)",
                     report_id, r.get("ticker"), grade, source)
            return []

    emoji = EMOJI.get(r["grade"], "❓")
    src_tag = {
        "manual": "📌 수동",
        "telegram": "📱 텔레그램",
        "auto_weekly": "🔁 주간자동",
        "auto_hourly": "⏱️ 시간자동",
    }.get(source, source)
    title = f"{emoji} {r['name']} ({r['ticker']})"
    md_path = config.resolve_report_md(r["md_path"]) if r.get("md_path") else None
    md = md_path.read_text(encoding="utf-8") if md_path and md_path.exists() else ""

    # 1차 — 요약
    report_url = config.dashboard_url(f"/report/{report_id}")
    head = [f"{title} — {src_tag}",
            f"등급 {r['grade']} · 평균 ★ {r['avg_rating']}"]
    if event_note:
        head.append(f"🏭 실적변화 이벤트(1~2년): {event_note}")
    head.append(f"전문: {report_url}")
    # 분석이 실패해 섹션 없는 에러 문구만 남은 보고서도 있다 → 원문 앞부분으로 폴백
    brief_body = (summarize_brief(md) or _strip_markdown(md)[:BRIEF_MAX]
                  ) if md else "(보고서 파일 없음)"
    detail_body = summarize_detail(md) if md else ""
    if detail_body:
        head.append("▼ 상세는 바로 다음 메시지 (필요 없으면 넘기세요)")
    brief = "\n".join(head) + f"\n{'─' * 30}\n{brief_body}"
    if not detail_body:
        return [brief]

    # 2차 — 상세
    tail = [f"{title} · 상세"]
    # 범례는 실제로 `레버리지 A~D` 표기가 붙은 심층 보고에만 (경량 보고엔 정량평가 없음).
    # 본문 다른 곳의 '고레버리지' 같은 단어에 걸리지 않게 등급 문자까지 본다.
    if re.search(r"레버리지\s*[A-D]\b", detail_body + " " + event_note):
        tail.append(EVENT_LEGEND)
    detail = ("\n".join(tail) + f"\n{'─' * 30}\n{detail_body}"
              + f"\n\n전문: {report_url}")
    return [brief, detail]


async def notify_single_report(report_id: int,
                                source: str = "manual") -> bool:
    """종목 1개 분석 완료 즉시 알림. 요약 1건 + 상세 1건 연속 전송.

    정책·본문 구성은 `build_single_messages` 참고.
    """
    msgs = build_single_messages(report_id, source)
    if not msgs:
        return False
    async with aiohttp.ClientSession() as session:
        ok = True
        for msg in msgs:
            for chunk in _chunk(msg):
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


async def notify_auth_expired(detail: str = "") -> bool:
    """Claude OAuth 인증 만료 알림. 받으면 원격 접속해 `claude` 로 재로그인하면
    다음 정각에 자동 복구된다(홈 토큰 갱신 → 격리본으로 자동 반영)."""
    lines = [
        "🔑 AutoScreener 인증 만료",
        "Claude OAuth 토큰이 만료되어 자동 분석이 멈췄습니다.",
        "원격 접속 후 `claude` 로 한 번 로그인하면 다음 정각에 자동 복구됩니다.",
    ]
    if detail:
        lines.append("")
        lines.append(detail)
    async with aiohttp.ClientSession() as session:
        return await _send(session, "\n".join(lines), parse_mode=None)


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
    elif cmd == "preview":
        # 전송 없이 실제 메시지 모양만 확인 (알림 정책도 그대로 적용)
        if len(sys.argv) < 3 or not sys.argv[2].isdigit():
            print("Usage: python notifier.py preview <report_id> [source]")
            return
        src = sys.argv[3] if len(sys.argv) > 3 else "auto_hourly"
        msgs = build_single_messages(int(sys.argv[2]), src)
        if not msgs:
            print(f"(알림 대상 아님 — source={src} 정책상 제외)")
        for i, msg in enumerate(msgs, 1):
            print(f"\n───── {i}/{len(msgs)}번째 메시지 ({len(msg)}자) ─────")
            print(msg)
    elif cmd.isdigit():
        ok = asyncio.run(notify_run(int(cmd)))
        print("OK" if ok else "FAILED")
    else:
        print("Usage: python notifier.py [test|last|preview <report_id>|<run_id>]")


if __name__ == "__main__":
    _main()
