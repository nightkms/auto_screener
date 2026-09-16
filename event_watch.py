"""S10: 이벤트 기반 재분석 트리거.

selector는 최근 30일 안에 분석한 종목을 dedup으로 제외한다(같은 종목을 매시각
반복 분석하지 않기 위해서다 → docs/design/selector.md). 그런데 그 30일 사이에
주가·거래량이 급변하면 "오늘 왜 튀었는지, 무슨 뉴스가 있었는지"를 그날 바로
확인해야 한다. 이 모듈은 **이미 분석한 종목만** 되짚어보며 급변을 찾아내고,
걸린 종목은 dedup을 무시하고 큐에 다시 넣는다.

selector와 반대 방향이라는 점이 핵심이다:
    selector    시장 전체 → 아직 안 본 종목을 찾는다 (dedup 적용)
    event_watch 이미 본 종목 → 그 사이 뭔가 터진 종목을 찾는다 (dedup 무시)

감시 대상은 최근 `EVENT_WATCH_DAYS`일 안에 보고서가 나온 종목 전부다. 등급으로
거르지 않는다 — INTEREST로 흘려보낸 종목이 갑자기 상한가를 가는 경우가 오히려
봐야 할 케이스다. 대상이 수십 종목뿐이라 매시각 감시해도 비용이 거의 없다.

데이터는 네이버 일봉 chart API **1콜/종목**이면 충분하다. 이 API는 장중에도
당일 봉을 실시간 갱신해 주므로(종가=현재가, 거래량=현재까지 누적) 장 마감을
기다리지 않고 그날 안에 잡아낼 수 있다.

이벤트는 **당일성 지표만** 쓴다 — 알고 싶은 건 "오늘 이 종목에 무슨 일이
있었나"이지 "한 달간 얼마나 흘렀나"가 아니기 때문:
    surge            당일 등락률 +EVENT_SURGE_PCT% 이상 (급등)
    high52           52주 신고가 경신 (EVENT_HIGH52)

급락(plunge)은 기본으로 잡지 않는다 — "오늘 왜 튀었나"를 그날 확인할 가치가
급등 쪽에 있고, 하락은 같은 토큰을 쓰면서 되짚을 실익이 적다는 사용자 판단이다
(2026-09-06). 대칭으로 -EVENT_SURGE_PCT%도 잡고 싶으면 .env에 EVENT_PLUNGE=1.

거래량 급증 배수는 쓰지 않는다(사용자 룰 — 가격만 본다). 초기 후보였던
'분석일 종가 대비 누적 이탈'과 '52주 신저가'는 실측 후 뺐다: 전자는 30일에 걸친
완만한 하락까지 잡아 당일성이 없고, 후자는 하락장에 하루 40~86건씩 쏟아져
개별 종목의 특이사항이 되지 못한다(2026-08-06, 398종목 20거래일 측정).

쿨다운도, 하루 상한도 두지 않는다(사용자 룰). 같은 날 같은 종류의 이벤트만
event_trigger UNIQUE로 1회로 묶일 뿐이고, **날짜가 바뀌면 같은 종류라도 다시
트리거된다** — 이틀 연속 상한가면 이틀 다 분석한다.

CLI:
    python event_watch.py             # 감지만 (큐에 넣지 않음)
    python event_watch.py --enqueue   # 감지 + 큐 투입
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import aiohttp

import config
import selector
import storage

log = logging.getLogger("event_watch")

CHART_LOOKBACK_DAYS = 400      # 52주 판정에 쓸 일봉 조회 범위 (영업일 ≈ 250개 확보)
MIN_ROWS_FOR_52W = 60          # 상장 직후 종목은 52주 판정 자체를 하지 않음
# 한국 시장 일일 가격제한폭은 ±30%. 그보다 큰 하루 변동은 시세가 아니라 액면분할·
# 권리락·병합 같은 인위적 가격조정이다. 네이버 일봉은 원주가(수정주가 아님)라
# 이런 날이 섞이면 급등락·신고가가 전부 오탐이 된다 → 그 종목의 판정을 건너뛴다.
PRICE_ADJUST_PCT = 32.0
MAX_PICK_SOURCE_LEN = 110      # 큐·보고서에 남길 사유 문자열 상한


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------
@dataclass
class Event:
    kind: str
    detail: str            # 사람이 읽는 사유 ('당일 +12.4%')
    metric: float


@dataclass
class TickerEvents:
    """한 종목에서 같은 날 감지된 이벤트 묶음."""
    ticker: str
    name: str
    event_date: str                     # YYYY-MM-DD (일봉 마지막 봉 = 거래일)
    close: int
    analyzed_at: str = ""               # 직전 분석 시각 (ISO)
    last_grade: str = ""
    events: list[Event] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """이벤트 사유 요약. 프롬프트·알림·큐 표시에 공통으로 쓴다."""
        s = " · ".join(e.detail for e in self.events[:3])
        return s[:MAX_PICK_SOURCE_LEN]

    def pick_source(self) -> str:
        """큐/보고서에 남길 선정근거. agents·synthesizer가 'event:' 접두로 분기한다."""
        return f"event:{self.summary}"


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------
def _fmt_date(local_date: str) -> str:
    """'20260806' → '2026-08-06'."""
    s = str(local_date)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def _detect(rows: list[dict]) -> tuple[str, int, list[Event]]:
    """일봉(정렬 무관)에서 당일 이벤트를 뽑는다.

    반환: (거래일 YYYY-MM-DD, 당일 종가, 이벤트 목록). 데이터 부족이면 빈 목록.
    """
    if len(rows) < 2:
        return "", 0, []
    rows = sorted(rows, key=lambda r: str(r["localDate"]))
    today, prev = rows[-1], rows[-2]
    event_date = _fmt_date(today["localDate"])
    close = float(today["closePrice"])
    prev_close = float(prev["closePrice"])
    if not close or not prev_close:
        return event_date, int(close), []

    chg = (close - prev_close) / prev_close * 100
    # 가격제한폭(±30%)을 넘는 변동은 시세가 아니라 액면분할·권리락 등 인위적 조정.
    # 네이버 일봉은 원주가라 이런 날을 그대로 믿으면 급락·신고가가 전부 오탐이 된다.
    if abs(chg) > PRICE_ADJUST_PCT:
        log.info("가격조정 의심(전일 대비 %+.1f%%) → 판정 skip", chg)
        return event_date, int(close), []

    events: list[Event] = []

    # 1) 당일 급등 — 이 모듈의 주 트리거. 거래량 배수는 보지 않는다(사용자 룰).
    # 급락은 config.EVENT_PLUNGE=1일 때만 대칭으로 잡는다(기본 off).
    if abs(chg) >= config.EVENT_SURGE_PCT and (chg > 0 or config.EVENT_PLUNGE):
        kind = "surge" if chg > 0 else "plunge"
        events.append(Event(kind, f"당일 {chg:+.1f}%", round(chg, 2)))

    # 2) 52주 신고가 — 당일 고가가 직전 1년 최고가를 넘었는지.
    # 상장 1년 미만이면 표본이 1년치가 안 되므로 문구를 '상장 이래'로 낮춘다
    # (없는 기간을 52주라고 적지 않는다 → event-time-rule).
    # 신저가는 트리거로 쓰지 않는다: 하락장에 하루 수십 건씩 동반 발생해
    # 개별 종목의 특이사항이 되지 못한다(하락 쪽은 plunge도 기본 off).
    if config.EVENT_HIGH52 and len(rows) >= MIN_ROWS_FOR_52W:
        year = rows[-251:-1] if len(rows) > 251 else rows[:-1]
        span_label = "52주" if len(year) >= 200 else "상장 이래"
        highs = [float(r["highPrice"]) for r in year if r.get("highPrice")]
        today_high = float(today.get("highPrice") or close)
        if highs and today_high > max(highs):
            events.append(Event("high52", f"{span_label} 신고가 경신",
                                round(today_high, 2)))

    return event_date, int(close), events


# ---------------------------------------------------------------------------
# 감지
# ---------------------------------------------------------------------------
async def detect_all(days: int | None = None) -> list[TickerEvents]:
    """최근 분석 종목 전체를 훑어 이벤트가 있는 종목만 반환. 큐는 건드리지 않는다."""
    days = days if days is not None else config.EVENT_WATCH_DAYS
    targets = storage.recently_analyzed_meta(days=days)
    if not targets:
        log.info("감시 대상 없음 (최근 %d일 내 분석 종목 0개)", days)
        return []

    today = date.today()
    start = (today - timedelta(days=CHART_LOOKBACK_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(headers=selector.HEADERS,
                                     timeout=timeout) as session:
        sem = asyncio.Semaphore(10)
        results = dict(await asyncio.gather(*[
            selector._fetch_chart(session, t["ticker"], sem, start, end)
            for t in targets
        ]))

    hits: list[TickerEvents] = []
    for t in targets:
        rows = results.get(t["ticker"]) or []
        if not rows:
            continue
        event_date, close, events = _detect(rows)
        if not events:
            continue
        # 오늘 이미 분석한 종목은 오늘 이벤트로 다시 부르지 않는다.
        # selector가 상한가·거래량급증으로 뽑아 방금 분석한 종목은 당연히 그날 크게
        # 움직인 종목이라, 막지 않으면 selector → 분석 → event_watch → 재분석의
        # 자기참조 루프가 된다. 쿨다운이 아니라 같은 날 중복 제거다 —
        # **다음 날 또 급변하면 그날 다시 잡힌다**.
        if (t.get("analyzed_at") or "")[:10] == event_date:
            log.info("오늘 이미 분석함 → skip: %s(%s) %s",
                     t.get("name"), t["ticker"],
                     " · ".join(e.detail for e in events))
            continue
        hits.append(TickerEvents(
            ticker=t["ticker"], name=t.get("name") or t["ticker"],
            event_date=event_date, close=close,
            analyzed_at=t.get("analyzed_at") or "",
            last_grade=t.get("last_grade") or "",
            events=events,
        ))
    log.info("이벤트 감지: 대상 %d종목 중 %d종목 (%s)",
             len(targets), len(hits), [h.ticker for h in hits])
    return hits


async def enqueue_event_picks(days: int | None = None,
                              source: str = "auto_event") -> tuple[int, list[str]]:
    """이벤트 감지 → 신규 건만 큐에 투입. (추가 수, ticker 목록) 반환.

    신규 판정은 storage.event_trigger의 UNIQUE(ticker, event_date, kind)가 한다.
    같은 날 같은 이벤트를 매시각 다시 큐에 넣지 않되, 날짜가 바뀌면 다시 넣는다.
    한 종목에서 여러 이벤트가 동시에 잡히면 사유를 합쳐 큐 1건으로 넣는다.
    """
    storage.init_db()
    hits = await detect_all(days=days)
    added: list[str] = []
    for h in hits:
        fresh = [e for e in h.events
                 if storage.record_event_trigger(
                     h.ticker, h.name, h.event_date, e.kind, e.detail, e.metric)]
        if not fresh:
            continue        # 이 종목의 오늘 이벤트는 이미 큐에 넣어봤음
        # 큐 표시·프롬프트에는 '새로 잡힌' 사유만 싣는다 (어제 이미 본 사유 제외)
        note = TickerEvents(ticker=h.ticker, name=h.name,
                            event_date=h.event_date, close=h.close,
                            analyzed_at=h.analyzed_at, last_grade=h.last_grade,
                            events=fresh)
        # priority=1 — '오늘 왜 튀었나'는 그날 안에 답이 나와야 의미가 있어
        # 앞서 쌓인 정기 핫픽을 앞질러 처리한다 (건수 상한은 두지 않음).
        ok = storage.add_to_queue(h.ticker, name=h.name, market="",
                                  source=source, pick_source=note.pick_source(),
                                  priority=1)
        if ok:
            storage.mark_event_queued(h.ticker, h.event_date)
            added.append(h.ticker)
            log.info("이벤트 재분석 큐 추가: %s(%s) — %s [직전분석 %s %s]",
                     h.name, h.ticker, note.summary,
                     (h.analyzed_at or "")[:10], h.last_grade)
        else:
            # 이미 큐에 pending/processing/failed로 있음 → 곧 분석된다. 사유는 기록됨.
            log.info("이벤트 감지했으나 큐에 이미 있음: %s(%s) — %s",
                     h.name, h.ticker, note.summary)
    log.info("이벤트 재분석: %d종목 큐 추가 (감지 %d)", len(added), len(hits))
    return len(added), added


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print(hits: list[TickerEvents]) -> None:
    if not hits:
        print("감지된 이벤트 없음")
        return
    print(f"{'코드':<8}{'종목명':<16}{'거래일':<12}{'직전분석':<12}{'등급':<10}사유")
    print("-" * 100)
    for h in hits:
        print(f"{h.ticker:<8}{h.name:<16}{h.event_date:<12}"
              f"{(h.analyzed_at or '-')[:10]:<12}{h.last_grade or '-':<10}{h.summary}")


def _main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--enqueue", action="store_true", help="감지 후 큐에 투입")
    p.add_argument("--days", type=int, default=None, help="감시 대상 기간(기본 30일)")
    args = p.parse_args()
    logging.basicConfig(level=config.LOG_LEVEL,
                        format="%(asctime)s %(name)s %(message)s")
    if args.enqueue:
        n, tickers = asyncio.run(enqueue_event_picks(days=args.days))
        print(f"큐 추가: {n}종목 {tickers}")
    else:
        _print(asyncio.run(detect_all(days=args.days)))


if __name__ == "__main__":
    _main()
