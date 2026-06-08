"""
S1: 주간 핫 종목 선정 (네이버 금융 모바일 API 기반).

전략:
    1. 코스피·코스닥 시가총액 상위 N(=200)을 후보풀로 한다.
    2. 각 종목 일봉 25개를 비동기 병렬로 받아 주간 등락률·거래대금 급증도를 계산.
    3. 외국인 보유율 변화로 수급 점수를 보조.
    4. 1차 필터(ETF/SPAC/우선주/관리/동전주 제외) 후 가중 z-score로 상위 N 추출.

CLI:
    python selector.py
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Iterable

import aiohttp

import config

log = logging.getLogger("selector")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

CANDIDATE_POOL_PER_MARKET = 100  # 시장별 시총 상위 N
SCORE_WEIGHTS = {"return": 0.45, "value_surge": 0.35, "foreign_delta": 0.20}
LASTSEARCH_TOP_N = 30  # 네이버 검색 상위 페이지에서 가져올 종목 수 (1차 풀)
MOVERS_TOP_N = 30      # 상한가·거래량 급증 등 시세 리스트에서 가져올 종목 수 (1.5차 보강)

EXCLUDE_NAME_PAT = re.compile(r"(스팩|우선주|리츠|ETN|ETF)")
# ETF 브랜드는 항상 종목명 맨 앞에 옴(예: "SOL AI반도체소부장", "KODEX 200").
# 영문 회사명 오탐 방지를 위해 선두 + 단어경계로 anchor. 신규/구브랜드 모두 포함.
ETF_BRAND_PAT = re.compile(
    r"^(KODEX|TIGER|RISE|KBSTAR|ACE|KINDEX|SOL|PLUS|ARIRANG|HANARO|KOSEF|"
    r"HK|WON|BNK|FOCUS|TIMEFOLIO|KIWOOM|히어로즈|마이다스|UNICORN|VITA|ITF)\b")
EXCLUDE_NAME_SUFFIX = re.compile(r"(우|우B|우C|\(전환\))$")
PRICE_MIN = 1000           # 동전주 컷


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    ticker: str
    name: str
    market: str
    close: int
    market_cap_billion: float        # 단위: 억원
    weekly_return: float             # %
    value_surge: float               # 이번 주 평균 거래대금 / 이전 4주 평균
    foreign_delta: float             # 외국인 보유율 5거래일 변화 (%p)
    score: float
    source_tag: str = ""             # 선정근거: search/upper/quant/z-score/manual

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 후보풀 수집 (시총 상위)
# ---------------------------------------------------------------------------
async def _fetch_market_value(session: aiohttp.ClientSession, market: str,
                              size: int) -> list[dict]:
    """market: 'KOSPI' | 'KOSDAQ'"""
    url = ("https://m.stock.naver.com/api/stocks/marketValue/"
           f"{market}?page=1&pageSize={size}")
    async with session.get(url) as r:
        r.raise_for_status()
        data = await r.json()
    return data.get("stocks", [])


# ---------------------------------------------------------------------------
# 검색 상위 (네이버 finance.naver.com/sise/lastsearch2.naver)
# ---------------------------------------------------------------------------
# 페이지는 EUC-KR HTML. <tr><td>순위</td><td><a href="/item/main.naver?code=...">이름</a>
# </td><td>검색비율%</td>... 구조. 순위 그대로가 인기 정렬이므로 추가 정렬 불필요.
_LASTSEARCH_ROW = re.compile(
    r'<tr[^>]*>\s*<td[^>]*>(\d+)</td>\s*'
    r'<td[^>]*>\s*<a[^>]*href="/item/main\.naver\?code=(\d{6})"[^>]*>'
    r'([^<]+)</a>.*?</tr>',
    re.S,
)


async def _fetch_lastsearch_top(session: aiohttp.ClientSession,
                                 limit: int) -> list[dict]:
    """네이버 '검색 상위' 페이지 상위 limit개. {ticker, name} 리스트.
    market은 여기 안 나옴 — 호출자가 시총상위 응답으로 매핑."""
    url = "https://finance.naver.com/sise/lastsearch2.naver"
    try:
        async with session.get(url) as r:
            r.raise_for_status()
            raw = await r.read()
    except Exception as e:
        log.warning("lastsearch 페이지 fetch 실패: %s", e)
        return []
    # 네이버 finance.naver.com은 EUC-KR
    html = raw.decode("euc-kr", errors="replace")
    out: list[dict] = []
    seen: set[str] = set()
    for m in _LASTSEARCH_ROW.finditer(html):
        rank, code, name = m.group(1), m.group(2), m.group(3).strip()
        if code in seen:
            continue
        seen.add(code)
        out.append({"rank": int(rank), "ticker": code, "name": name})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 시세 리스트 (상한가·거래량 급증 등 finance.naver.com/sise/sise_<kind>.naver)
# ---------------------------------------------------------------------------
# 페이지마다 컬럼 구성이 달라 본문 행 마크업은 제각각이지만(상한가는 class="tltle"가
# 없고 순위와 이름 사이에 number 칸이 더 있음), **순위 데이터 행에는 공통으로
# <td class="no">순위</td> 셀이 있고** 내비/연관 링크에는 없다. 이 순위셀을 앵커로
# 잡고 직후의 첫 종목 링크를 본문 종목으로 본다. (검색상위 페이지와 별개 구조)
_SISE_MOVER_ROW = re.compile(
    r'class="no"\s*>\s*(\d+)\s*</td>.*?'
    r'/item/main\.naver\?code=(\d{6})[^>]*>([^<]+)</a>',
    re.S,
)


async def _fetch_sise_movers(session: aiohttp.ClientSession, kind: str,
                              limit: int) -> list[dict]:
    """finance.naver.com/sise/sise_<kind>.naver 상위 limit개. {rank,ticker,name} 리스트.
    kind: 'upper'(상한가) | 'quant'(거래량 급증) | 'rise'(급등) | 'fall'(급락).
    검색상위와 마찬가지로 market 컬럼이 없어 호출자가 시총 응답으로 사이드 매핑.
    rise/fall은 전체 시장을 나열하므로 limit으로 자른다."""
    url = f"https://finance.naver.com/sise/sise_{kind}.naver"
    try:
        async with session.get(url) as r:
            r.raise_for_status()
            raw = await r.read()
    except Exception as e:
        log.warning("sise_%s 페이지 fetch 실패: %s", kind, e)
        return []
    html = raw.decode("euc-kr", errors="replace")  # finance.naver.com은 EUC-KR
    out: list[dict] = []
    seen: set[str] = set()
    for m in _SISE_MOVER_ROW.finditer(html):
        rank, code, name = m.group(1), m.group(2), m.group(3).strip()
        if code in seen:
            continue
        seen.add(code)
        out.append({"rank": int(rank), "ticker": code, "name": name})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 일봉 25개 (비동기)
# ---------------------------------------------------------------------------
async def _fetch_chart(session: aiohttp.ClientSession, code: str,
                       sem: asyncio.Semaphore,
                       start: str, end: str) -> tuple[str, list[dict]]:
    url = (f"https://api.stock.naver.com/chart/domestic/item/{code}/day"
           f"?startDateTime={start}&endDateTime={end}")
    async with sem:
        try:
            async with session.get(url) as r:
                if r.status != 200:
                    return code, []
                rows = await r.json()
                return code, rows if isinstance(rows, list) else []
        except Exception as e:                            # noqa
            log.warning("chart fail %s: %s", code, e)
            return code, []


# ---------------------------------------------------------------------------
# 점수 계산
# ---------------------------------------------------------------------------
def _score_stock(rows: list[dict]) -> dict | None:
    """일봉 25개 → 지표 dict. 데이터 부족 시 None."""
    if len(rows) < 10:
        return None
    # 신구 순서가 일정한지 보장: localDate 오름차순 정렬
    rows = sorted(rows, key=lambda r: r["localDate"])
    closes = [float(r["closePrice"]) for r in rows]
    vols   = [float(r["accumulatedTradingVolume"]) for r in rows]
    # 거래대금 근사: vol × close
    trading_values = [c * v for c, v in zip(closes, vols)]

    # 주간(직전 5거래일) vs 그 이전 (전 4주)
    week = trading_values[-5:]
    prior = trading_values[:-5] if len(trading_values) > 5 else []
    week_avg = sum(week) / len(week)
    prior_avg = (sum(prior) / len(prior)) if prior else float("nan")
    value_surge = (week_avg / prior_avg) if prior_avg and not math.isnan(prior_avg) else 0.0

    weekly_return = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else 0.0

    # 외국인 보유율 변화 (5거래일)
    fr = [r.get("foreignRetentionRate") for r in rows]
    fr = [float(x) for x in fr if x is not None]
    foreign_delta = (fr[-1] - fr[-6]) if len(fr) >= 6 else 0.0

    return {
        "close": int(closes[-1]),
        "weekly_return": weekly_return,
        "value_surge": value_surge,
        "foreign_delta": foreign_delta,
    }


def _zscore(values: list[float]) -> list[float]:
    if not values:
        return []
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = math.sqrt(var) or 1.0
    return [(v - mean) / std for v in values]


# ---------------------------------------------------------------------------
# 필터
# ---------------------------------------------------------------------------
def _is_excluded(name: str) -> bool:
    if EXCLUDE_NAME_PAT.search(name):
        return True
    if ETF_BRAND_PAT.search(name):
        return True
    if EXCLUDE_NAME_SUFFIX.search(name):
        return True
    return False


async def _fetch_stock_end_type(session: aiohttp.ClientSession,
                                 code: str) -> str | None:
    """네이버 basic API의 stockEndType('stock'|'etf'|'etn'...). 실패 시 None."""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            j = await r.json()
            return j.get("stockEndType")
    except Exception:
        return None


async def _confirm_not_etf(candidates: list[dict]) -> list[dict]:
    """최종 후보 중 ETF/ETN을 basic API의 stockEndType으로 한 번 더 걸러낸다.

    시총풀 후보는 이미 stockEndType으로 제외됐지만, 검색상위·상한가/거래량은
    이름만 스크랩돼(타입 필드 없음) 이름 패턴을 빠져나간 ETF가 섞일 수 있다.
    후보가 소수(top_n)라 호출 부담이 작다. 룰: 모자라도 강제로 안 채움."""
    if not candidates:
        return candidates
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
        sem = asyncio.Semaphore(10)

        async def _typ(code: str):
            async with sem:
                return code, await _fetch_stock_end_type(session, code)

        types = dict(await asyncio.gather(
            *[_typ(c["ticker"]) for c in candidates]))
    kept = []
    for c in candidates:
        if types.get(c["ticker"]) in ("etf", "etn"):
            log.info("ETF/ETN 최종 제외(stockEndType): %s (%s)",
                     c.get("name"), c["ticker"])
            continue
        kept.append(c)
    return kept


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
async def select_top_async(top_n: int = config.TOP_N) -> list[Candidate]:
    """3단계 선정:
    1)   네이버 검색 상위 LASTSEARCH_TOP_N에서 dedup → 검색 순위 그대로 top_n개
    1.5) 부족분을 상한가·거래량 급증(시세 리스트)으로 보강 — 우선순위 상한가 > 거래량
    2)   그래도 모자라면 시총상위 z-score 룰로 보강

    "오늘 시장 관심사·실제로 움직인 종목" 우선, "한 주간 추세 강한 종목"은 마지막
    보강용. 검색상위/시총풀은 거의 고정이라 자주 돌리면 dedup으로 고갈되는데, 상한가·
    거래량 급증은 매일 명단이 크게 바뀌어 30일 dedup을 통과할 새 종목을 공급한다.
    모두 dedup에 걸리면 결과 0건도 정상 (사용자 룰: 강제로 채우지 않음)."""
    # 분석 이력 dedup. manual 큐는 이 경로 안 거치므로 영향 없음.
    import storage
    exclude = storage.recently_analyzed_tickers(days=30)

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
        # === 데이터 fetch: 검색상위 + 시총 풀 + 일봉 ===
        # 검색상위는 market 컬럼이 없어 시총 응답으로 사이드 매핑.
        kospi_task = asyncio.create_task(
            _fetch_market_value(session, "KOSPI", CANDIDATE_POOL_PER_MARKET))
        kosdaq_task = asyncio.create_task(
            _fetch_market_value(session, "KOSDAQ", CANDIDATE_POOL_PER_MARKET))
        search_task = asyncio.create_task(
            _fetch_lastsearch_top(session, LASTSEARCH_TOP_N))
        upper_task = asyncio.create_task(
            _fetch_sise_movers(session, "upper", MOVERS_TOP_N))
        quant_task = asyncio.create_task(
            _fetch_sise_movers(session, "quant", MOVERS_TOP_N))
        kospi, kosdaq, search_hits, upper_hits, quant_hits = await asyncio.gather(
            kospi_task, kosdaq_task, search_task, upper_task, quant_task,
        )

        # 상한가·거래량 급증 = "오늘 실제로 움직인 종목". 검색상위 다음 우선순위로
        # 보강한다. 우선순위 상한가 > 거래량. 두 소스 간 중복은 source_tag 보존하며 dedup.
        movers_hits: list[dict] = []
        movers_seen: set[str] = set()
        for tag, hits in (("upper", upper_hits), ("quant", quant_hits)):
            for hit in hits:
                if hit["ticker"] in movers_seen:
                    continue
                movers_seen.add(hit["ticker"])
                movers_hits.append({**hit, "source_tag": tag})

        # 시총 풀 → meta dict (이름·market). 필터(우선주/ETF) 적용.
        # 시총 API는 항목마다 stockEndType('stock'|'etf'|'etn')을 주므로 추가 호출
        # 없이 ETF/ETN을 정확히 걸러낸다(이름 패턴보다 견고). 이름 패턴은 병행 백업.
        meta: dict[str, dict] = {}
        for market, lst in (("KOSPI", kospi), ("KOSDAQ", kosdaq)):
            for s in lst:
                code, name = s["itemCode"], s["stockName"]
                if s.get("stockEndType") in ("etf", "etn"):
                    continue
                if _is_excluded(name):
                    continue
                meta[code] = {"name": name, "market": market}

        # 검색상위·상한가·거래량에 시총 풀 밖 종목이 있으면 추가 (market은 ? 로 둠).
        # 일봉을 받아 점수를 내야 1차/1.5차 통과가 가능하므로 여기서 meta에 합류시킨다.
        pool_extra = 0
        for hit in [*search_hits, *movers_hits]:
            code = hit["ticker"]
            if _is_excluded(hit["name"]):
                continue
            if code not in meta:
                meta[code] = {"name": hit["name"], "market": "?"}
                pool_extra += 1
        log.info("후보풀: 시총 KOSPI %d + KOSDAQ %d, 검색상위 %d·상한가 %d·거래량 %d "
                 "중 풀 밖 %d개 합류 → 총 %d",
                 len(kospi), len(kosdaq), len(search_hits),
                 len(upper_hits), len(quant_hits), pool_extra, len(meta))

        # 일봉 병렬 수집 (45일 → 영업일 25개 이상)
        today = date.today()
        start = (today - timedelta(days=45)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        sem = asyncio.Semaphore(20)
        results = await asyncio.gather(*[
            _fetch_chart(session, code, sem, start, end) for code in meta
        ])

    # === 점수 계산: 시총 풀 종목만 z-score 산정 (보강용) ===
    scored: list[dict] = []
    for code, rows in results:
        m = _score_stock(rows)
        if not m:
            continue
        if m["close"] < PRICE_MIN:
            continue
        scored.append({"ticker": code, **meta[code], **m})

    score_map = {r["ticker"]: r for r in scored}
    if scored:
        z_ret = _zscore([r["weekly_return"] for r in scored])
        z_val = _zscore([r["value_surge"] for r in scored])
        z_fr  = _zscore([r["foreign_delta"] for r in scored])
        for i, r in enumerate(scored):
            r["score"] = (
                z_ret[i] * SCORE_WEIGHTS["return"]
                + z_val[i] * SCORE_WEIGHTS["value_surge"]
                + z_fr[i] * SCORE_WEIGHTS["foreign_delta"]
            )

    # === 1단계: 검색상위에서 dedup → 검색 순위 그대로 ===
    primary: list[dict] = []
    primary_seen: set[str] = set()
    for hit in search_hits:
        code = hit["ticker"]
        if code in exclude or code in primary_seen:
            continue
        rec = score_map.get(code)
        if rec is None:
            # 일봉 부족·동전주 컷 등으로 점수 못 낸 종목. 검색상위 신호는
            # 강하므로 _fetch_chart 결과 없어도 통과시키지 않고 스킵.
            # (보고서 단계에서 데이터 누락이 더 큰 문제)
            continue
        primary.append({**rec, "source_tag": "search"})
        primary_seen.add(code)
        if len(primary) >= top_n:
            break
    n_search = len(primary)

    # === 1.5단계: 부족분을 상한가·거래량 급증으로 보강 (검색상위 다음 우선) ===
    # 검색상위와 동일 로직: dedup·점수 산정 통과분만. 매일 명단이 크게 바뀌는
    # 소스라 dedup 고갈 상황에서도 새 종목이 들어온다.
    for hit in movers_hits:
        if len(primary) >= top_n:
            break
        code = hit["ticker"]
        if _is_excluded(hit["name"]):
            continue
        if code in exclude or code in primary_seen:
            continue
        rec = score_map.get(code)
        if rec is None:
            continue
        primary.append({**rec, "source_tag": hit["source_tag"]})
        primary_seen.add(code)
    n_movers = len(primary) - n_search

    # === 2단계: 부족분을 시총 풀 z-score 상위로 보강 ===
    extra: list[dict] = []
    shortfall = top_n - len(primary)
    if shortfall > 0 and scored:
        scored_sorted = sorted(scored, key=lambda r: r["score"], reverse=True)
        for r in scored_sorted:
            if r["ticker"] in exclude or r["ticker"] in primary_seen:
                continue
            extra.append({**r, "source_tag": "z-score"})
            primary_seen.add(r["ticker"])
            if len(extra) >= shortfall:
                break

    top = primary + extra
    # 이름만 스크랩되는 검색상위·상한가/거래량 경로의 잔여 ETF를 최종 타입 확인으로 컷.
    top = await _confirm_not_etf(top)
    log.info("선정 결과: 검색상위 %d + 상한가·거래량 %d + z-score 보강 %d = 총 %d개 "
             "(dedup 제외 %d종목)",
             n_search, n_movers, len(extra), len(top), len(exclude))

    return [
        Candidate(
            ticker=r["ticker"],
            name=r["name"],
            market=r["market"],
            close=r["close"],
            market_cap_billion=0.0,
            weekly_return=round(r["weekly_return"], 2),
            value_surge=round(r["value_surge"], 2),
            foreign_delta=round(r["foreign_delta"], 3),
            score=round(r.get("score", 0.0), 3),
            source_tag=r.get("source_tag", ""),
        )
        for r in top
    ]


def select_top(top_n: int = config.TOP_N) -> list[Candidate]:
    return asyncio.run(select_top_async(top_n))


async def fetch_single_candidate(ticker: str, name: str = "",
                                  market: str = "",
                                  pick_source: str = "manual") -> Candidate | None:
    """단일 종목에 대한 Candidate 빌더. 큐 분석용 (selector 우회).
    pick_source: 큐에 적재될 때의 선정근거(search/upper/quant/...). 분석 시
    '오늘 상한가/거래량급증으로 잡힌 종목'임을 프롬프트에 알리는 데 쓴다."""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
        today = date.today()
        start = (today - timedelta(days=45)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        sem = asyncio.Semaphore(1)
        _, rows = await _fetch_chart(session, ticker, sem, start, end)
    if not rows:
        return None
    m = _score_stock(rows)
    if not m:
        return None
    return Candidate(
        ticker=ticker,
        name=name or ticker,
        market=market or "?",
        close=m["close"],
        market_cap_billion=0.0,
        weekly_return=round(m["weekly_return"], 2),
        value_surge=round(m["value_surge"], 2),
        foreign_delta=round(m["foreign_delta"], 3),
        score=0.0,
        source_tag=pick_source or "manual",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_table(rows: Iterable[Candidate]) -> None:
    print(f"{'순위':<4}{'코드':<8}{'종목명':<16}{'시장':<8}{'종가':>10}"
          f"{'주간%':>9}{'거래대금배수':>13}{'외인Δ%p':>10}{'score':>8}")
    print("-" * 86)
    for i, c in enumerate(rows, 1):
        print(f"{i:<4}{c.ticker:<8}{c.name:<16}{c.market:<8}"
              f"{c.close:>10,}{c.weekly_return:>9.2f}{c.value_surge:>13.2f}"
              f"{c.foreign_delta:>10.3f}{c.score:>8.3f}")


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(name)s %(message)s")
    rows = select_top()
    _print_table(rows)
