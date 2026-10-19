"""
주말 회고: 과거 보고서의 등급 vs 이후 실제 수익률 → 역할별 교훈 (TradingAgents reflection).

1) 분석일이 RETRO_MIN_AGE_DAYS~RETRO_MAX_AGE_DAYS 전인 보고서를 고르고(종목당 최신 1건),
   price_history로 분석일 종가 대비 1·3개월 수익률을 계산한다(코드 — LLM 경유 X).
2) 등급별 통계(건수·평균·중앙값·상승 비율)를 낸다.
3) '높은 등급인데 크게 빠진' / '낮은 등급인데 크게 오른' 사례를 골라 당시 보고서 원문과 함께
   Opus 감사관(prompts/retrospect.txt)에게 넘긴다. 감사관은 분석일 이후 일어난 일을 검색해
   예측 가능/불가능을 가르고, 반복 패턴만 역할별(bull/bear/synthesizer) 교훈으로 낸다.
4) 교훈을 lesson 테이블에 저장(기존 교훈 폐기 포함) → 다음 분석부터 debate·synthesizer에 자동 주입
   (lessons.py). 회고 전문은 analysis/retrospect/<주차>.md + 텔레그램.
   prompts/*.txt는 건드리지 않는다 — 교훈은 메모리로만 쌓인다(원작과 같음).

실행 조건(scheduler hourly 잡): 토·일 + 이번 주차 미완료 + RETROSPECT_ENABLED.
노트북이 주말 내내 꺼져 있으면 그 주는 건너뛴다(다음 주에 다시).

CLI:
    python retrospect.py            # 지금 1회 실행 (텔레그램 전송 포함)
    python retrospect.py --no-send  # 저장만
    python retrospect.py --stats    # 통계·사례만 출력 (LLM 호출 없음)
    python retrospect.py --lessons  # 지금 쌓여 있는 활성 교훈
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import re
import statistics
import time
from datetime import date, datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

import config
import lessons
import notifier
import price_history
import storage
import synthesizer

log = logging.getLogger("retrospect")

RETRO_MIN_AGE_DAYS = 35          # 1개월 수익률이 확정된 보고서부터
RETRO_MAX_AGE_DAYS = 200         # 너무 옛 보고서는 프롬프트가 지금과 달라 교훈이 약하다
SAMPLE_PER_GRADE = 80            # 등급별 표본 상한 (STRONG/WATCH는 보통 전수)
CASES_PER_SIDE = 6               # 과대평가 의심 / 놓침 의심 사례 수
CASE_MD_CHARS = 20000            # 사례 보고서 원문 상한 (트레이더·최종 결정·토론 전문까지)
RETRO_MAX_TURNS = 40
RETRO_TIMEOUT_S = 2400
MAX_ATTEMPTS_PER_WEEK = 3        # 실패 시 다음 정각 재시도 상한

_STATE_KEY = "retrospect_week"   # 값: "<주차>|done" 또는 "<주차>|<시도횟수>"
_GRADES = ("STRONG", "WATCH", "INTEREST", "SKIP")
_DECISIONS = ("BUY", "HOLD", "SELL")

_running: asyncio.Task | None = None


def _week_label(d: date | None = None) -> str:
    y, w, _ = (d or date.today()).isocalendar()
    return f"{y}-W{w:02d}"


# ---------------------------------------------------------------------------
# 1) 표본 + 성과
# ---------------------------------------------------------------------------
def _load_reports() -> list[dict]:
    """윈도우 안 보고서, 종목당 최신 1건. 시장은 candidates에서(대부분 '?'라 빈 값이면 야후가 둘 다 시도)."""
    with storage._connect() as c:
        rows = c.execute(
            """SELECT r.id, r.ticker, r.name, r.grade, r.avg_rating, r.md_path, r.decision,
                      runs.started_at,
                      (SELECT market FROM candidates cd
                        WHERE cd.run_id = r.run_id AND cd.ticker = r.ticker LIMIT 1) AS market
               FROM reports r JOIN runs ON r.run_id = runs.id
               WHERE date(runs.started_at) BETWEEN date('now', 'localtime', ?)
                                               AND date('now', 'localtime', ?)
               ORDER BY r.id DESC""",
            (f"-{RETRO_MAX_AGE_DAYS} days", f"-{RETRO_MIN_AGE_DAYS} days"),
        ).fetchall()
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        if r["ticker"] in seen or r["grade"] not in _GRADES:
            continue
        seen.add(r["ticker"])
        out.append(dict(r))
    return out


def _sample(reports: list[dict]) -> list[dict]:
    rng = random.Random(_week_label())      # 같은 주 재시도면 같은 표본
    out: list[dict] = []
    for g in _GRADES:
        rows = [r for r in reports if r["grade"] == g]
        out += rows if len(rows) <= SAMPLE_PER_GRADE else rng.sample(rows, SAMPLE_PER_GRADE)
    return out


def _with_performance(rows: list[dict]) -> list[dict]:
    """각 보고서에 m1/m3 수익률(%)을 붙인다. 가격을 못 받은 건 버린다. (동기 — 스레드에서 호출)"""
    out: list[dict] = []
    for r in rows:
        try:
            base = datetime.fromisoformat(r["started_at"]).date()
        except ValueError:
            base = date.fromisoformat(r["started_at"][:10])
        market = "" if (r.get("market") or "?") == "?" else r["market"]
        try:
            perf = price_history.compute_performance(r["ticker"], market, base)
        except Exception as e:
            log.warning("[%s] 성과 계산 실패: %s", r["ticker"], e)
            continue
        if not perf.get("ok"):
            continue
        hz = {h["key"]: h.get("pct") for h in perf["horizons"] if h.get("status") == "ok"}
        if hz.get("m1") is None:
            continue
        out.append({**r, "base_date": base.isoformat(), "m1": hz["m1"], "m3": hz.get("m3")})
        time.sleep(0.2)                     # 야후/네이버 비공식 엔드포인트 — 몰아치지 않는다
    return out


# ---------------------------------------------------------------------------
# 2) 통계 + 사례
# ---------------------------------------------------------------------------
def _table(rows: list[dict], field: str, keys: tuple[str, ...], label: str) -> str:
    lines = [f"| {label} | 건수 | 1개월 평균 | 1개월 중앙값 | 1개월 상승비율 | 3개월 건수 | 3개월 평균 | 3개월 중앙값 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for g in keys:
        m1 = [r["m1"] for r in rows if r.get(field) == g]
        m3 = [r["m3"] for r in rows if r.get(field) == g and r["m3"] is not None]
        if not m1:
            lines.append(f"| {g} | 0 | - | - | - | 0 | - | - |")
            continue
        up = sum(1 for x in m1 if x > 0) / len(m1) * 100
        lines.append(
            f"| {g} | {len(m1)} | {statistics.mean(m1):+.1f}% | {statistics.median(m1):+.1f}% | "
            f"{up:.0f}% | {len(m3)} | "
            + (f"{statistics.mean(m3):+.1f}% | {statistics.median(m3):+.1f}% |" if m3 else "- | - |"))
    return "\n".join(lines)


def _stats(rows: list[dict]) -> str:
    """등급별 표 + (최종 결정이 있는 보고서가 있으면) BUY/HOLD/SELL별 표."""
    out = _table(rows, "grade", _GRADES, "등급")
    if any(r.get("decision") for r in rows):
        out += "\n\n" + _table(rows, "decision", _DECISIONS, "최종 결정")
    return out


def _pick_cases(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """과대평가 의심(STRONG/WATCH 또는 BUY 중 최악) / 놓침 의심(INTEREST/SKIP이면서 BUY가
    아니거나, SELL 중 최고). 3개월 있으면 3개월 기준."""
    def key(r):
        return r["m3"] if r["m3"] is not None else r["m1"]
    high = sorted((r for r in rows
                   if r["grade"] in ("STRONG", "WATCH") or r.get("decision") == "BUY"), key=key)
    low = sorted((r for r in rows
                  if (r["grade"] in ("INTEREST", "SKIP") and r.get("decision") != "BUY")
                  or r.get("decision") == "SELL"), key=key, reverse=True)
    return ([r for r in high if key(r) < 0][:CASES_PER_SIDE],
            [r for r in low if key(r) > 0][:CASES_PER_SIDE])


def _case_block(r: dict, kind: str) -> str:
    p = config.resolve_report_md(r["md_path"]) if r.get("md_path") else None
    md = p.read_text(encoding="utf-8") if p and p.exists() else "(보고서 파일 없음)"
    if len(md) > CASE_MD_CHARS:
        md = md[:CASE_MD_CHARS] + "\n…(이하 생략)"
    m3 = f"{r['m3']:+.1f}%" if r["m3"] is not None else "미도래"
    dec = f" · 최종 결정 {r['decision']}" if r.get("decision") else ""
    return (f"### [{kind}] {r['name']} ({r['ticker']}) — {r['grade']} (평균★ {r['avg_rating']}){dec}\n"
            f"- 분석일: {r['base_date']} / 이후 수익률: 1개월 {r['m1']:+.1f}% · 3개월 {m3}\n\n"
            f"<보고서 원문>\n{md}\n</보고서 원문>\n")


def build_input(rows: list[dict]) -> str:
    over, missed = _pick_cases(rows)
    parts = [f"# 회고 대상: {_week_label()} (분석일 {RETRO_MIN_AGE_DAYS}~{RETRO_MAX_AGE_DAYS}일 전 보고서, "
             f"종목당 최신 1건, 등급별 최대 {SAMPLE_PER_GRADE}건 표본)", "",
             "# 등급·최종 결정별 성과 통계 (분석일 종가 대비, 분할 보정)", _stats(rows), "", "# 사례", ""]
    parts += [_case_block(r, "과대평가 의심") for r in over]
    parts += [_case_block(r, "놓침 의심") for r in missed]
    if not over and not missed:
        parts.append("(해당 사례 없음 — 통계 해석만 하라)")
    parts += ["", "# 현재 활성 교훈 (같은 취지면 새로 만들지 말 것 · 틀렸으면 RETIRE)"]
    current = storage.active_lessons()
    parts += ([f"- #{r['id']} [{r['role']}] 상황: {r['situation']} → {r['lesson']}"
               for r in current] or ["(없음)"])
    return "\n".join(parts)


_LESSON_PAT = re.compile(
    r"\[\[LESSON\]\]\s*ROLE:\s*(\w+)\s*SITUATION:\s*(.+?)\s*LESSON:\s*(.+?)\s*"
    r"EVIDENCE:\s*(.+?)\s*\[\[/LESSON\]\]", re.S)
_RETIRE_PAT = re.compile(r"^RETIRE:\s*#(\d+)\s*[—\-:]*\s*(.*)$", re.M)


def apply_lessons(text: str, week: str) -> tuple[str, list[str]]:
    """감사관 출력의 LESSON/RETIRE를 DB에 반영. (블록을 걷어낸 본문, 반영 내역) 반환."""
    done: list[str] = []
    active_ids = {r["id"] for r in storage.active_lessons()}
    for lid, why in _RETIRE_PAT.findall(text):
        if int(lid) in active_ids:
            storage.retire_lesson(int(lid), f"{week} 회고: {why.strip()}"[:300])
            done.append(f"🗑 교훈#{lid} 폐기 — {why.strip()}")
    for role, situation, lesson, evidence in _LESSON_PAT.findall(text):
        role = role.strip().lower()
        if role not in lessons.ROLES:
            log.warning("알 수 없는 역할의 교훈 무시: %s", role)
            continue
        lid = storage.add_lesson(role, situation.strip(), lesson.strip(),
                                 evidence.strip(), week)
        done.append(f"➕ 교훈#{lid} [{role}] 상황: {situation.strip()} → {lesson.strip()}")
    for role in lessons.ROLES:
        n = lessons.enforce_cap(role)
        if n:
            done.append(f"🗑 [{role}] 상한 초과로 오래된 교훈 {n}개 비활성")
    body = _RETIRE_PAT.sub("", _LESSON_PAT.sub("", text))
    body = re.sub(r"\n?```\s*$", "", body.rstrip()).rstrip()
    return body, done


# ---------------------------------------------------------------------------
# 3) 감사관 호출
# ---------------------------------------------------------------------------
async def _audit(user_prompt: str) -> tuple[str, int, int]:
    opts = ClaudeAgentOptions(
        system_prompt=(config.PROMPTS_DIR / "retrospect.txt").read_text(encoding="utf-8"),
        model=config.CLAUDE_SYNTH_MODEL,
        permission_mode="bypassPermissions",
        allowed_tools=["WebSearch", "WebFetch"],
        max_turns=RETRO_MAX_TURNS,
        env=config.sdk_env(),
    )
    pieces: list[str] = []
    usage: dict = {}

    async def _consume():
        async for msg in query(prompt=user_prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                pieces.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                usage.update(getattr(msg, "usage", None) or {})

    await asyncio.wait_for(_consume(), timeout=RETRO_TIMEOUT_S)
    text = "\n".join(pieces).strip()
    # 검색 중간 독백이 앞에 붙는다 → 최종 보고서 머리부터 자른다
    i = text.rfind("# 📈 주간 분석 회고")
    text = text[i:] if i >= 0 else text
    t_in = (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
    return text, t_in, usage.get("output_tokens") or 0


async def run(send: bool = True) -> str:
    """회고 1회. 저장 경로(ANALYSIS 루트 기준 상대) 반환. 실패면 예외."""
    week = _week_label()
    reports = _sample(_load_reports())
    log.info("회고 %s: 표본 %d건 성과 계산", week, len(reports))
    rows = await asyncio.to_thread(_with_performance, reports)
    if len(rows) < 10:
        raise RuntimeError(f"성과 계산된 보고서가 {len(rows)}건뿐 — 회고 생략")
    user_prompt = build_input(rows)
    log.info("회고 %s: 감사관 호출 (성과 %d건, 입력 %d자)", week, len(rows), len(user_prompt))
    text, t_in, t_out = await _audit(user_prompt)
    if not text or synthesizer.is_usage_limit(text):
        raise RuntimeError("감사관 응답 없음/한도초과")
    storage.log_usage(None, config.CLAUDE_SYNTH_MODEL, t_in, t_out, f"retrospect:{week}")

    body, applied = apply_lessons(text, week)
    md = (body + "\n\n## 🧠 메모리 반영 (다음 분석부터 자동 주입)\n"
          + ("\n".join(f"- {x}" for x in applied) if applied else "- 변경 없음")
          + "\n\n---\n## 부록: 등급별 성과 통계 (코드 계산)\n" + _stats(rows) + "\n")
    config.RETROSPECT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RETROSPECT_DIR / f"{week}.md"
    path.write_text(md, encoding="utf-8")
    log.info("회고 저장: %s", path)
    if send:
        await notifier.notify_retrospect(md, path.name)
    return str(path)


# ---------------------------------------------------------------------------
# 스케줄 (scheduler hourly 잡에서 호출)
# ---------------------------------------------------------------------------
def maybe_start() -> bool:
    """주말 + 이번 주차 미완료 + 실행 중 아님이면 백그라운드로 띄운다. 띄웠으면 True."""
    global _running
    if not config.RETROSPECT_ENABLED or date.today().weekday() < 5:
        return False
    if _running and not _running.done():
        return False
    week = _week_label()
    st_week, _, st_val = (storage.get_state(_STATE_KEY) or "").partition("|")
    attempts = 0
    if st_week == week:
        if st_val == "done":
            return False
        attempts = int(st_val) if st_val.isdigit() else 0
        if attempts >= MAX_ATTEMPTS_PER_WEEK:
            return False
    storage.set_state(_STATE_KEY, f"{week}|{attempts + 1}")

    async def _job():
        try:
            await run(send=True)
            storage.set_state(_STATE_KEY, f"{week}|done")
        except Exception as e:
            log.exception("회고 실패 (%d/%d회)", attempts + 1, MAX_ATTEMPTS_PER_WEEK)
            if attempts + 1 >= MAX_ATTEMPTS_PER_WEEK:
                try:
                    await notifier.notify_error(str(e)[:500], context=f"retrospect {week}")
                except Exception:
                    pass

    _running = asyncio.create_task(_job())
    log.info("주말 회고 시작 (%s, %d번째 시도)", week, attempts + 1)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--stats", action="store_true", help="통계·사례만 출력 (LLM 호출 없음)")
    ap.add_argument("--lessons", action="store_true", help="활성 교훈 목록 출력")
    a = ap.parse_args()
    storage.init_db()
    if a.lessons:
        for _r in storage.active_lessons():
            print(f"#{_r['id']} [{_r['role']}] ({_r['week_label']}) 상황: {_r['situation']}\n"
                  f"    → {_r['lesson']}\n    근거: {_r['evidence']}")
    elif a.stats:
        _rows = _with_performance(_sample(_load_reports()))
        print(_stats(_rows))
        _over, _missed = _pick_cases(_rows)
        for _k, _rs in (("과대평가 의심", _over), ("놓침 의심", _missed)):
            print(f"\n[{_k}]")
            for _r in _rs:
                print(f"  {_r['name']}({_r['ticker']}) {_r['grade']} {_r['base_date']} "
                      f"1M {_r['m1']:+.1f}% 3M {_r['m3'] if _r['m3'] is not None else '-'}")
    else:
        print(asyncio.run(run(send=not a.no_send)))
