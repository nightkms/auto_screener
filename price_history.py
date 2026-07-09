"""
S9: 분석 후 성과 추적 — 분석일 기준 +1/3/6/12개월 주가 수익률.

가격 소스:
  1) 야후 파이낸스 chart API (主). `close`는 **분할보정 O / 배당보정 X** 값이다.
     - 배당은 '눈에 보이는 주가'에 없으므로 무시(수정종가 adjclose는 배당까지 반영 → 안 씀).
     - 액면분할은 야후 close가 이미 소급 반영하므로 우리가 비율을 곱할 필요 없다.
       events=split로 분할 구간을 감지해 배지만 표시(표시가격이 그날 본 값과 달라 보이는 이유 고지).
     - 한국 종목은 접미사 필요: 코스피 `.KS` / 코스닥 `.KQ`.
  2) 네이버 siseJson (fallback). 원종가(분할 미보정). 야후가 못 잡는 종목 보강용.

한 종목의 여러 분석 회차 각각에 대해 그 분석일 기준 성과를 따로 계산한다.
시계열은 티커별로 TTL 캐시 → 같은 종목 다른 회차를 눌러도 재조회하지 않는다.

CLI:
    python price_history.py 035720 KOSPI 2021-03-01   # 카카오 5:1 분할 검증
    python price_history.py 260970 KOSDAQ 2026-01-02
"""
from __future__ import annotations

import calendar
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

log = logging.getLogger("price_history")

_KST = timezone(timedelta(hours=9))
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36"}

# (키, 개월). 표시 라벨은 _LABELS.
HORIZONS = (("m1", 1), ("m3", 3), ("m6", 6), ("m12", 12))
_LABELS = {"m1": "1개월", "m3": "3개월", "m6": "6개월", "m12": "1년"}

# 휴장(주말·공휴일) 보정: target 당일 종가가 없으면 직전 거래일까지 며칠 되짚을지.
_MAX_LOOKBACK_DAYS = 10

# 티커별 시계열 캐시. {ticker: {ts, start, series, splits, source}}
_CACHE: dict[str, dict] = {}
_TTL_SEC = 6 * 3600


# ---------------------------------------------------------------------------
# 날짜 유틸
# ---------------------------------------------------------------------------
def add_months(d: date, n: int) -> date:
    """d의 n개월 뒤. 말일 보정(예: 1/31 + 1개월 = 2/28)."""
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _close_on_or_before(series: dict[date, float], target: date,
                        max_lookback: int = _MAX_LOOKBACK_DAYS
                        ) -> tuple[float | None, date | None]:
    """target 당일 종가, 없으면 직전 거래일 종가. (가격, 실제사용일)."""
    for i in range(max_lookback + 1):
        d = target - timedelta(days=i)
        if d in series:
            return series[d], d
    return None, None


# ---------------------------------------------------------------------------
# 야후 파이낸스
# ---------------------------------------------------------------------------
def _yahoo_symbols(ticker: str, market: str) -> list[str]:
    """시장에 맞는 야후 심볼 우선순위. 모르면 둘 다 시도."""
    m = (market or "").upper()
    if "KOSPI" in m or m == "KS":
        return [f"{ticker}.KS", f"{ticker}.KQ"]
    if "KOSDAQ" in m or m == "KQ":
        return [f"{ticker}.KQ", f"{ticker}.KS"]
    return [f"{ticker}.KS", f"{ticker}.KQ"]


def _parse_yahoo_splits(node: dict) -> list[dict]:
    """chart 응답의 events.splits → [{date, ratio, factor}]. factor=신주/구주."""
    out: list[dict] = []
    ev = (node.get("events") or {}).get("splits") or {}
    for v in ev.values():
        try:
            num = float(v.get("numerator"))
            den = float(v.get("denominator"))
            d = datetime.fromtimestamp(int(v.get("date")), _KST).date()
            out.append({
                "date": d,
                "ratio": (v.get("splitRatio") or f"{num:g}:{den:g}"),
                "factor": num / den if den else None,
            })
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    return out


def _fetch_yahoo(ticker: str, market: str, start: date, end: date):
    """(series, splits, 'yahoo') 또는 None. series = {date: close(분할보정)}."""
    p1 = int(datetime(start.year, start.month, start.day, tzinfo=_KST).timestamp())
    p2 = int(datetime(end.year, end.month, end.day, 23, 59, tzinfo=_KST).timestamp())
    for sym in _yahoo_symbols(ticker, market):
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"period1": p1, "period2": p2,
                        "interval": "1d", "events": "split"},
                headers=_HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            res = (r.json().get("chart") or {}).get("result") or []
            if not res:
                continue
            node = res[0]
            ts = node.get("timestamp") or []
            quote = ((node.get("indicators") or {}).get("quote") or [{}])[0]
            closes = quote.get("close") or []
            series: dict[date, float] = {}
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                series[datetime.fromtimestamp(int(t), _KST).date()] = float(c)
            if not series:
                continue
            return series, _parse_yahoo_splits(node), "yahoo"
        except Exception as e:
            log.warning("yahoo fetch fail %s: %s", sym, e)
            continue
    return None


# ---------------------------------------------------------------------------
# 네이버 siseJson (fallback)
# ---------------------------------------------------------------------------
# 행 예: ["20260601", 53800, 57500, 50700, 54000, 50842, 5.94]
#        [   날짜   ,  시가 ,  고가 ,  저가 ,  종가 , 거래량, 외인]
_NAVER_ROW = re.compile(
    r'\["(\d{8})",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)')


def _fetch_naver(ticker: str, start: date, end: date):
    """(series, [], 'naver') 또는 None. series = {date: close(원종가·분할 미보정)}."""
    try:
        r = requests.get(
            "https://api.finance.naver.com/siseJson.naver",
            params={"symbol": ticker, "requestType": 1,
                    "startTime": start.strftime("%Y%m%d"),
                    "endTime": end.strftime("%Y%m%d"),
                    "timeframe": "day"},
            headers=_HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        series: dict[date, float] = {}
        for m in _NAVER_ROW.finditer(r.text):
            ymd = m.group(1)
            series[date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))] = float(m.group(5))
        if not series:
            return None
        return series, [], "naver"
    except Exception as e:
        log.warning("naver siseJson fail %s: %s", ticker, e)
        return None


# ---------------------------------------------------------------------------
# 시계열 (캐시 + 소스 폴백)
# ---------------------------------------------------------------------------
def _get_series(ticker: str, market: str, need_start: date) -> dict | None:
    """[need_start, 오늘] 을 커버하는 시계열 엔트리. 야후 → 네이버 폴백. TTL 캐시."""
    now = time.time()
    today = date.today()
    c = _CACHE.get(ticker)
    if c and now - c["ts"] < _TTL_SEC and c["start"] <= need_start:
        return c
    start = min(need_start, c["start"]) if c else need_start
    fetched = (_fetch_yahoo(ticker, market, start, today)
               or _fetch_naver(ticker, start, today))
    if not fetched:
        return None
    series, splits, source = fetched
    _CACHE[ticker] = {"ts": now, "start": start, "series": series,
                      "splits": splits, "source": source}
    return _CACHE[ticker]


# ---------------------------------------------------------------------------
# 성과 계산 (진입점)
# ---------------------------------------------------------------------------
def compute_performance(ticker: str, market: str, base_dt: date) -> dict:
    """base_dt(분석일) 기준 성과.

    반환:
      성공: {ok:True, source, base_price, base_date_used, horizons:[...]}
      실패: {ok:False, error, source?}
    horizons 각 항목:
      {key,label,target, status} + status='ok'면 {price,date_used,pct, split?}
      status: 'ok' | 'pending'(미도래) | 'nodata'(데이터 없음)
    분할(split)은 base~horizon 구간에 있으면 배지 문자열. 야후 close는 이미
    분할보정되어 pct 자체는 정확하며, split은 표시가격 착시 고지용이다.
    """
    today = date.today()
    entry = _get_series(ticker, market, base_dt - timedelta(days=10))
    if not entry:
        return {"ok": False, "error": "가격 데이터를 가져오지 못했습니다."}
    series, splits, source = entry["series"], entry["splits"], entry["source"]

    base_price, base_used = _close_on_or_before(series, base_dt)
    if base_price is None:
        return {"ok": False, "source": source,
                "error": "분석일 부근 종가가 없습니다."}

    horizons: list[dict] = []
    for key, months in HORIZONS:
        target = add_months(base_dt, months)
        item = {"key": key, "label": _LABELS[key], "target": target.isoformat()}
        if target > today:
            item["status"] = "pending"
            horizons.append(item)
            continue
        hp, hused = _close_on_or_before(series, target)
        if hp is None:
            item["status"] = "nodata"
            horizons.append(item)
            continue
        item["status"] = "ok"
        item["price"] = hp
        item["date_used"] = hused.isoformat()
        item["pct"] = round((hp - base_price) / base_price * 100, 1)
        win = [s for s in splits if base_used < s["date"] <= hused]
        if win:
            item["split"] = ", ".join(s["ratio"] for s in win)
        horizons.append(item)

    return {"ok": True, "source": source, "base_price": base_price,
            "base_date_used": base_used.isoformat(), "horizons": horizons}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from pprint import pprint

    logging.basicConfig(level="INFO", format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python price_history.py <ticker> [market] [base_date=YYYY-MM-DD]")
        sys.exit(1)
    _t = sys.argv[1]
    _m = sys.argv[2] if len(sys.argv) > 2 else ""
    _bd = (sys.argv[3] if len(sys.argv) > 3
           else (date.today() - timedelta(days=200)).isoformat())
    pprint(compute_performance(_t, _m, date.fromisoformat(_bd)))
