"""
S2: DART OpenAPI에서 종목별 컨텍스트(기업 개요 + 재무 주요계정 + 최근 공시) 수집.

종목 코드(6자리) → DART corp_code 매핑은 corpCode.xml을 1회 다운받아 캐시.

CLI:
    python data_loader.py 005930
    python data_loader.py 260970 064400
"""
from __future__ import annotations

import io
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import requests

import config

log = logging.getLogger("data_loader")

DART_BASE = "https://opendart.fss.or.kr/api"
CACHE_DIR = config.DATA_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)
CORP_MAP_PATH = CACHE_DIR / "corp_codes.json"
CORP_MAP_TTL = 30 * 86400          # 30일
KRX_MAP_PATH = CACHE_DIR / "krx_tickers.json"   # 네이버 marketValue 미러
KRX_MAP_TTL = 7 * 86400            # 7일 (신규 상장·우선주 반영용)

MAJOR_ACCOUNTS = (
    "매출액", "영업이익", "당기순이익",
    "자산총계", "부채총계", "자본총계",
)

# 우선주 종목명 패턴 + 신형/구형 식별
import re as _re
_PREF_NAME_PAT = _re.compile(r'우[BC]?$|우[BC]?\(전환\)$')


def _classify_preferred(ticker: str, name: str) -> dict | None:
    """우선주 식별 + 본주 ticker 추정 + 신형/구형 라벨링.
    None이면 우선주 아님."""
    if not _PREF_NAME_PAT.search(name or ""):
        return None
    # 본주 ticker 추정: 끝자리 5/7/9 → 0, 알파벳(K/L 등) → 0
    last = ticker[-1]
    common_ticker = None
    if last in "579":
        common_ticker = ticker[:-1] + "0"
    elif last.isalpha():
        common_ticker = ticker[:-1] + "0"
    # 신형/구형 분류
    series = "구형"
    if "우B" in name:
        series = "신형(우B, 최저배당률 보장)"
    elif "우C" in name:
        series = "신형(우C, 과거 외국인 전용)"
    elif "(전환)" in name or last == "K":
        series = "전환우선주(만기 후 보통주 전환)"
    elif name.endswith("우"):
        series = "구형(최저배당률 없음)"
    return {
        "is_preferred": True,
        "common_ticker": common_ticker,
        "series": series,
    }


@dataclass
class StockContext:
    ticker: str
    corp_code: str
    name: str
    industry: str
    listing_date: str
    ceo: str
    homepage: str
    financials: dict[str, dict[str, float]] = field(default_factory=dict)  # year → {account: value}
    recent_disclosures: list[dict] = field(default_factory=list)
    valuation: dict[str, str] = field(default_factory=dict)        # PER/PBR/EPS/시총/배당 등
    consensus: dict | None = None                                  # 목표가/추정EPS (대형주만)
    peers: list[dict] = field(default_factory=list)                # 동종업종 비교
    short_sale: dict | None = None                                 # 공매도 잔고
    market_snapshot: dict = field(default_factory=dict)            # 코스피/코스닥 지수
    # 우선주 분석 시 본주 비교용. 보통주 분석에선 None.
    # {'common_ticker','common_name','series','common_valuation'}
    preferred_info: dict | None = None

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker, "corp_code": self.corp_code, "name": self.name,
            "industry": self.industry, "listing_date": self.listing_date,
            "ceo": self.ceo, "homepage": self.homepage,
            "financials": self.financials,
            "recent_disclosures": self.recent_disclosures,
            "valuation": self.valuation, "consensus": self.consensus,
            "peers": self.peers, "short_sale": self.short_sale,
            "preferred_info": self.preferred_info,
        }


# ---------------------------------------------------------------------------
# corp_code 매핑
# ---------------------------------------------------------------------------
def _fetch_naver_market_tickers() -> list[dict]:
    """네이버 marketValue API로 KOSPI/KOSDAQ 전체 종목 수집. 우선주 포함.
    DART corpCode.xml은 보통주만 등록되어 있어 이걸로 우선주를 보강한다."""
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36"}
    out: dict[str, dict] = {}
    for market in ("KOSPI", "KOSDAQ"):
        for page in range(1, 30):
            url = ("https://m.stock.naver.com/api/stocks/marketValue/"
                   f"{market}?page={page}&pageSize=100")
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                break
            d = r.json()
            stocks = d.get("stocks", [])
            if not stocks:
                break
            for s in stocks:
                code = s.get("itemCode")
                if not code:
                    continue
                out[code] = {
                    "ticker": code,
                    "name": s.get("stockName", ""),
                    "market": market,
                }
            if len(stocks) < 100:
                break
    return list(out.values())


def _load_krx_tickers() -> list[dict]:
    """KRX 전체 상장 종목 (우선주 포함). 7일 캐시."""
    if KRX_MAP_PATH.exists():
        age = time.time() - KRX_MAP_PATH.stat().st_mtime
        if age < KRX_MAP_TTL:
            return json.loads(KRX_MAP_PATH.read_text(encoding="utf-8"))
    log.info("KRX 종목 리스트(네이버) 수집 중...")
    rows = _fetch_naver_market_tickers()
    KRX_MAP_PATH.write_text(json.dumps(rows, ensure_ascii=False),
                             encoding="utf-8")
    log.info("KRX 종목 저장: %d개", len(rows))
    return rows


def _load_corp_mapping() -> dict[str, dict]:
    """ticker(6자리) → {'corp_code', 'corp_name', 'market', 'is_preferred',
    'common_ticker', 'series'} 매핑.

    1. DART corpCode.xml: 1법인 1보통주(stock_code). 30일 캐시.
    2. KRX 네이버 시총 리스트: 우선주 포함. 7일 캐시.
    3. 우선주 → 본주 ticker 변환 규칙(끝자리 5/7/9 → 0, 알파벳 → 0)으로
       우선주에 보통주 corp_code 공유. 우선주는 공시·재무가 본주와 동일하므로."""
    cached_valid = False
    if CORP_MAP_PATH.exists():
        age = time.time() - CORP_MAP_PATH.stat().st_mtime
        if age < CORP_MAP_TTL:
            cached_valid = True
    if cached_valid:
        try:
            mapping = json.loads(CORP_MAP_PATH.read_text(encoding="utf-8"))
            # 우선주 보강 키 'is_preferred'가 들어가 있으면 그대로 사용
            sample = next(iter(mapping.values()), None)
            if sample and "is_preferred" in sample:
                return mapping
            # 옛 스키마(우선주 미보강) → 아래에서 보강 후 재저장
        except json.JSONDecodeError:
            cached_valid = False

    if not cached_valid:
        log.info("corpCode.xml 다운로드 중...")
        r = requests.get(f"{DART_BASE}/corpCode.xml",
                         params={"crtfc_key": config.DART_API_KEY}, timeout=30)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            with z.open("CORPCODE.xml") as f:
                xml = f.read()
        root = ET.fromstring(xml)
        mapping: dict[str, dict] = {}
        for node in root.findall("list"):
            stock = (node.findtext("stock_code") or "").strip()
            if not stock or stock == " ":
                continue
            mapping[stock] = {
                "corp_code": (node.findtext("corp_code") or "").strip(),
                "corp_name": (node.findtext("corp_name") or "").strip(),
            }
    else:
        # 캐시는 살아있지만 우선주 미보강 → 그대로 재사용
        mapping = json.loads(CORP_MAP_PATH.read_text(encoding="utf-8"))

    # 1차 보강: 모든 보통주에 is_preferred=False, market 정보 부착
    krx_rows = _load_krx_tickers()
    krx_by_ticker = {r["ticker"]: r for r in krx_rows}
    for ticker, info in mapping.items():
        info.setdefault("is_preferred", False)
        info.setdefault("common_ticker", ticker)  # 자기 자신
        info.setdefault("series", "")
        if ticker in krx_by_ticker:
            info.setdefault("market", krx_by_ticker[ticker]["market"])
            info.setdefault("name", krx_by_ticker[ticker]["name"])

    # 2차 보강: 우선주 추가. corp_code는 본주 것 공유
    added = 0
    for row in krx_rows:
        ticker = row["ticker"]
        if ticker in mapping:
            continue
        pref = _classify_preferred(ticker, row["name"])
        if not pref:
            continue
        common = pref["common_ticker"]
        if not common or common not in mapping:
            continue
        common_info = mapping[common]
        mapping[ticker] = {
            "corp_code": common_info["corp_code"],
            "corp_name": common_info["corp_name"],
            "name": row["name"],
            "market": row["market"],
            "is_preferred": True,
            "common_ticker": common,
            "series": pref["series"],
        }
        added += 1
    log.info("우선주 보강: +%d개 (총 %d개)", added, len(mapping))

    CORP_MAP_PATH.write_text(json.dumps(mapping, ensure_ascii=False),
                              encoding="utf-8")
    return mapping


# ---------------------------------------------------------------------------
# 개별 API 호출
# ---------------------------------------------------------------------------
def _api(path: str, **params) -> dict:
    params["crtfc_key"] = config.DART_API_KEY
    r = requests.get(f"{DART_BASE}/{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _fetch_company(corp_code: str) -> dict:
    j = _api("company.json", corp_code=corp_code)
    if j.get("status") != "000":
        return {}
    return j


REPORT_CODES = {
    "11013": "1Q",   # 1분기보고서
    "11012": "반기", # 반기보고서
    "11014": "3Q",   # 3분기보고서
    "11011": "연간", # 사업보고서
}


def _parse_amount(s: str) -> float | None:
    s = (s or "").replace(",", "").replace(" ", "")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fetch_financials_one(corp_code: str, year: int, reprt_code: str) -> dict[str, float]:
    """corp_code/year/reprt_code 한 조합의 주요계정 추출. CFS 우선, OFS fallback."""
    j = _api("fnlttSinglAcnt.json", corp_code=corp_code,
             bsns_year=year, reprt_code=reprt_code)
    if j.get("status") != "000":
        return {}
    for div in ("CFS", "OFS"):
        out: dict[str, float] = {}
        for row in j.get("list", []):
            if row.get("fs_div") != div:
                continue
            name = row.get("account_nm", "").strip()
            if name not in MAJOR_ACCOUNTS:
                continue
            v = _parse_amount(row.get("thstrm_amount", ""))
            if v is not None:
                out[name] = v
        if out:
            return out
    return {}


def _fetch_financials_all(corp_code: str, years: int = 2) -> dict[str, dict[str, float]]:
    """가장 최신 분기/연간 보고서 우선으로 여러 period 데이터 반환.
    key 예: '2026-1Q', '2025-연간', '2025-반기' 등.
    """
    out: dict[str, dict[str, float]] = {}
    today_year = date.today().year
    # 최신부터: 올해 1Q / 작년 연간 / 작년 3Q / 작년 반기 / 작년 1Q / 재작년 연간 ...
    candidates: list[tuple[int, str]] = []
    for y in range(today_year, today_year - years - 1, -1):
        for code in ("11013", "11012", "11014", "11011"):  # 분기 우선
            candidates.append((y, code))
    for y, code in candidates:
        fin = _fetch_financials_one(corp_code, y, code)
        if fin:
            key = f"{y}-{REPORT_CODES[code]}"
            out[key] = fin
        if len(out) >= 4:        # 최대 4개 period까지
            break
    return out


# 공시 분류 (DART pblntf_ty) 중 종목 분석에 중요한 것들
DISCLOSURE_TYPES = {
    "A": "정기",       # 사업/분기/반기
    "B": "주요사항",    # 부도/회생/배임/횡령/채무보증/감사의견 등 ★중요
    "C": "발행",       # 유상증자/CB/BW/주식관련사채 등
    "D": "지분",       # 대량보유/임원·주요주주 보유 변동
    "I": "거래소",      # 관리종목·투자주의·거래정지·자율공시
}

# 본문 제목에서 추출하는 위험/이벤트 키워드 → 등급
SIGNAL_KEYWORDS: list[tuple[str, str, str]] = [
    # (키워드, 레벨, 카테고리)
    ("부도",          "fatal", "사업위기"),
    ("회생절차",       "fatal", "사업위기"),
    ("영업정지",       "fatal", "사업위기"),
    ("거래정지",       "fatal", "거래"),
    ("관리종목",       "fatal", "거래"),
    ("상장폐지",       "fatal", "거래"),
    ("배임",          "fatal", "지배구조"),
    ("횡령",          "fatal", "지배구조"),
    ("감사의견 거절",   "fatal", "감사"),
    ("감사의견 한정",   "warn",  "감사"),
    ("의견거절",       "fatal", "감사"),
    ("투자주의환기",    "warn",  "거래"),
    ("투자경고",       "warn",  "거래"),
    ("소송",          "warn",  "법적"),
    ("주식매수청구권",  "warn",  "구조변경"),
    ("합병",          "info",  "구조변경"),
    ("분할",          "info",  "구조변경"),
    ("주식분할",       "info",  "구조변경"),
    ("감자",          "warn",  "자본"),
    ("증자",          "info",  "자본"),
    ("유상증자",       "warn",  "자본"),
    ("무상증자",       "info",  "자본"),
    ("전환사채",       "warn",  "자본"),
    ("신주인수권부사채", "warn",  "자본"),
    ("교환사채",       "info",  "자본"),
    ("채무보증",       "warn",  "재무"),
    ("자기주식 취득",   "info",  "주주환원"),
    ("자기주식 소각",   "info",  "주주환원"),
    ("배당",          "info",  "주주환원"),
    ("기업가치제고",    "info",  "주주환원"),
    ("최대주주 변경",   "warn",  "지배구조"),
    ("임원 변경",      "info",  "지배구조"),
    ("대량보유",       "info",  "지분"),
    ("정정",          "info",  "정정"),
]


def _classify_disclosure(title: str) -> tuple[str | None, str | None]:
    """(level, category) 반환. 해당 없으면 (None, None)."""
    for kw, level, cat in SIGNAL_KEYWORDS:
        if kw in title:
            return level, cat
    return None, None


def _fetch_disclosures(corp_code: str, days: int = 180,
                       per_type: int = 30,
                       since: str | None = None) -> list[dict]:
    """주요 카테고리(A/B/C/D/I)를 폭넓게 수집. 위험·이벤트 키워드 분류 추가.

    since: 'YYYYMMDD' 문자열. 주어지면 그 날짜부터 (그 이전 공시는 잘라냄).
           증분 수집 시 사용. 그래도 days보다 더 과거를 잡지는 않음."""
    end = date.today()
    start = end - timedelta(days=days)
    if since and len(since) == 8 and since.isdigit():
        since_date = date(int(since[:4]), int(since[4:6]), int(since[6:8]))
        # since가 더 최근이면 since 사용 (= 그날 이후만), 더 과거면 days 기간 유지
        if since_date > start:
            start = since_date
    all_rows: dict[str, dict] = {}    # rcept_no → row

    # 전체 + 각 카테고리. None=전체로 한 번 더 받아 빠진 거 보강.
    for pblntf_ty in (None, "B", "C", "D", "I"):
        params: dict = dict(corp_code=corp_code,
                            bgn_de=start.strftime("%Y%m%d"),
                            end_de=end.strftime("%Y%m%d"),
                            page_count=per_type)
        if pblntf_ty:
            params["pblntf_ty"] = pblntf_ty
        j = _api("list.json", **params)
        if j.get("status") != "000":
            continue
        # 목록 API는 row에 pblntf_ty를 주지 않으므로, 어느 카테고리 필터로
        # 받았는지(루프변수)로 유형을 판정한다. None(전체) 조회분은 일단 '기타'로
        # 두고, 이후 B/C/D/I 조회에서 같은 rcept_no를 만나면 정확한 유형으로 보강.
        cat_type = DISCLOSURE_TYPES.get(pblntf_ty, "기타") if pblntf_ty else None
        for r in j.get("list", []):
            no = r["rcept_no"]
            if no in all_rows:
                if cat_type and all_rows[no]["type"] == "기타":
                    all_rows[no]["type"] = cat_type
                continue
            title = r["report_nm"]
            level, category = _classify_disclosure(title)
            all_rows[no] = {
                "date": r["rcept_dt"],
                "title": title.strip(),
                "rcept_no": no,
                "type": cat_type or "기타",
                "signal_level": level,        # fatal/warn/info 또는 None
                "signal_category": category,  # 사업위기/거래/감사/자본/지배구조 등
            }

    # 날짜 내림차순, 위험 등급 우선 정렬
    LEVEL_RANK = {"fatal": 0, "warn": 1, "info": 2, None: 3}
    sorted_rows = sorted(all_rows.values(),
                         key=lambda r: (LEVEL_RANK[r["signal_level"]],
                                        -int(r["date"])))
    return sorted_rows[:60]


# ---------------------------------------------------------------------------
# 공시 원문(본문) 수집 — 제목만으로 호재/악재가 안 드러나는 공시를 위해.
# 예: "주식등의 대량보유상황보고서(일반)" 제목 뒤에 실제로는
#     "주식담보계약 및 주식증여계약 체결"(승계+자금조달)이 숨어 있음.
# ---------------------------------------------------------------------------

# 본문을 받아 요약할 가치가 있는 공시 유형 (DISCLOSURE_TYPES 라벨 기준)
DOC_FETCH_TYPES = ("지분", "주요사항")

# 유형 분류가 누락돼도(목록 API 한계) 제목으로 본문 대상을 잡기 위한 패턴.
# 제목만으로는 호재/악재가 안 드러나는 지분·자본·구조변경 공시들.
DOC_FETCH_TITLE_PATTERNS = (
    "대량보유", "소유상황", "특정증권",          # 지분·증여·담보 (5%룰/임원주주)
    "최대주주", "주식분할", "합병", "분할",
    "유상증자", "무상증자", "전환사채", "신주인수권", "교환사채",
    "감자", "주식교환", "영업양수", "영업양도", "자기주식",
)


def should_fetch_document(d: dict) -> bool:
    """이 공시의 원문 본문을 받아 요약할 대상인지 판정.
    - 지분(증여·대량보유)·주요사항: 제목만으로 호재/악재가 안 드러나 본문 필요.
    - signal 키워드(fatal/warn)가 걸린 건: 맥락 확인 가치 있음.
    - 위 유형 판정이 누락돼도 제목 패턴으로 한 번 더 거른다.
    - 정기보고서(사업/분기)는 제외 — 실적은 fnlttSinglAcnt로 이미 구조화 수신하므로
      방대한 본문을 받을 필요가 없다."""
    if d.get("type") in DOC_FETCH_TYPES:
        return True
    if d.get("signal_level") in ("fatal", "warn"):
        return True
    title = d.get("title", "")
    if any(p in title for p in DOC_FETCH_TITLE_PATTERNS):
        return True
    return False


# ACODE → 사람이 읽는 라벨 (지분/대량보유/주요사항 공시의 핵심 셀)
_DART_FIELD_LABELS = {
    "RPT_RSP_NM": "보고자",
    "SUM_CHN_RWN": "보고사유",
    "CHN_RSM": "변경사유",
    "CHN_RSN": "변동사유",
    "TRD_RVL": "계약상대방",
    "TRD_KND": "계약종류",
    "TRD_RMK": "계약비고",
}


def _extract_dart_fields(xml: str) -> dict[str, list[str]]:
    """DART 원문 XML에서 ACODE 기반 핵심필드 추출 (요약 보조·fallback용)."""
    out: dict[str, list[str]] = {}
    for code, label in _DART_FIELD_LABELS.items():
        for m in re.finditer(rf'ACODE="{code}"[^>]*>([^<]*)<', xml):
            v = m.group(1).strip()
            if v and v != "-":
                out.setdefault(label, [])
                if v not in out[label]:
                    out[label].append(v)
    return out


_TAG_ROW_END = re.compile(r"</T[RDEUH]>")
_TAG_P = re.compile(r"</?P>")
_TAG_ANY = re.compile(r"<[^>]+>")
_WS_INLINE = re.compile(r"[ \t]+")
_WS_NL = re.compile(r"\n\s*\n+")


def _clean_dart_xml(xml: str) -> str:
    """DART 원문 XML → 평문. 셀/행 경계만 공백·개행으로 남기고 태그 제거.
    원본 50~110KB가 보통 4~9천자로 줄어 LLM 요약 입력으로 적합."""
    s = _TAG_ROW_END.sub(" ", xml)
    s = _TAG_P.sub("\n", s)
    s = _TAG_ANY.sub("", s)
    s = _WS_INLINE.sub(" ", s)
    s = _WS_NL.sub("\n", s)
    return s.strip()


def fetch_document(rcept_no: str,
                   max_chars: int = 12000) -> tuple[str, dict] | None:
    """접수번호의 원문(document.xml)을 받아 (정제 평문, 핵심필드) 반환.
    실패 시 None. document API는 ZIP 안에 '{rcept_no}.xml' 1개(UTF-8)를 준다."""
    try:
        r = requests.get(f"{DART_BASE}/document.xml",
                         params={"crtfc_key": config.DART_API_KEY,
                                 "rcept_no": rcept_no},
                         timeout=30)
        r.raise_for_status()
    except Exception as e:
        log.warning("document fetch 실패 rcept=%s: %s", rcept_no, e)
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            names = z.namelist()
            if not names:
                return None
            raw = z.read(names[0])
    except zipfile.BadZipFile:
        log.warning("document가 ZIP이 아님 rcept=%s", rcept_no)
        return None
    try:
        xml = raw.decode("utf-8")
    except UnicodeDecodeError:
        xml = raw.decode("utf-8", errors="replace")
    text = _clean_dart_xml(xml)
    fields = _extract_dart_fields(xml)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…(이하 생략)"
    return text, fields


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def lookup_ticker(ticker: str) -> dict | None:
    """ticker 1개의 메타 조회. 검색·큐 추가 분기에 사용.
    반환: {'ticker','name','market','corp_code','is_preferred','common_ticker','series'}
    None이면 매핑에 없음.
    """
    mapping = _load_corp_mapping()
    info = mapping.get(ticker)
    if not info:
        return None
    return {
        "ticker": ticker,
        "name": info.get("name") or info.get("corp_name", ""),
        "market": info.get("market", ""),
        "corp_code": info.get("corp_code", ""),
        "is_preferred": bool(info.get("is_preferred")),
        "common_ticker": info.get("common_ticker", ticker),
        "series": info.get("series", ""),
    }


_search_cache: list[dict] | None = None


def search_stocks(query: str, limit: int = 20) -> list[dict]:
    """corp_codes + KRX 매핑에서 종목명/티커로 부분 일치 검색. 우선주 포함.
    반환 dict에 is_preferred, series 포함 → UI에서 라벨 표시 가능.

    DART corpCode.xml은 합병·상폐된 과거 stock_code도 포함하므로 KRX 현재
    상장 리스트(market 필드 유무)로 1차 필터링한다. 안 그러면 구 미래에셋증권
    (037620) 같은 상폐 종목이 검색 결과에 노출된다."""
    global _search_cache
    if _search_cache is None:
        mapping = _load_corp_mapping()
        _search_cache = []
        for ticker, info in mapping.items():
            if len(ticker) != 6:
                continue
            # 6자리 (숫자 or 끝에 알파벳 1자리. ex: 00680K)
            head = ticker[:5]
            last = ticker[5]
            if not (head.isdigit() and (last.isdigit() or last.isalpha())):
                continue
            # 현재 상장 종목만 (market은 KRX 매핑에서만 들어옴)
            if not info.get("market"):
                continue
            display_name = info.get("name") or info.get("corp_name", "")
            _search_cache.append({
                "ticker": ticker,
                "name": display_name,
                "is_preferred": bool(info.get("is_preferred")),
                "series": info.get("series", ""),
                "common_ticker": info.get("common_ticker", ticker),
            })
    q = (query or "").strip()
    if not q:
        return []
    ql = q.lower()
    hits: list[dict] = []
    # 1) ticker 정확 일치 먼저
    for s in _search_cache:
        if s["ticker"] == q:
            hits.append(s)
    # 2) 이름 prefix
    for s in _search_cache:
        if s["name"].lower().startswith(ql) and s not in hits:
            hits.append(s)
            if len(hits) >= limit:
                return hits
    # 3) 이름 부분 일치
    for s in _search_cache:
        if ql in s["name"].lower() and s not in hits:
            hits.append(s)
            if len(hits) >= limit:
                return hits
    return hits[:limit]


NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}


def _fetch_naver_integration(ticker: str) -> dict:
    """네이버 모바일 integration endpoint. PER/PBR/시총·컨센서스·업종비교 한 번에."""
    try:
        r = requests.get(
            f"https://m.stock.naver.com/api/stock/{ticker}/integration",
            headers=NAVER_HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return {}
        return r.json()
    except Exception as e:
        log.warning("naver integration fail %s: %s", ticker, e)
        return {}


# totalInfos 의 key 한국어 → 영문 키 매핑
_VALUATION_KEYS = {
    "시총": "market_cap", "PER": "per", "EPS": "eps", "추정PER": "fwd_per",
    "추정EPS": "fwd_eps", "PBR": "pbr", "BPS": "bps",
    "배당수익률": "dividend_yield", "주당배당금": "dps",
    "외인소진율": "foreign_rate",
    "52주 최고": "high52w", "52주 최저": "low52w",
}


def _parse_naver_valuation(j: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for info in j.get("totalInfos", []):
        key = (info.get("key") or "").strip()
        val = (info.get("value") or "").strip()
        mapped = _VALUATION_KEYS.get(key)
        if mapped and val and val != "N/A":
            out[mapped] = val
    return out


def _parse_naver_peers(j: dict, self_ticker: str, limit: int = 6) -> list[dict]:
    peers: list[dict] = []
    for x in j.get("industryCompareInfo", []) or []:
        if not isinstance(x, dict):
            continue
        if x.get("itemCode") == self_ticker:
            continue
        peers.append({
            "ticker": x.get("itemCode"),
            "name": x.get("stockName"),
            "close": x.get("closePrice"),
            "fluctuation_pct": x.get("fluctuationsRatio"),
            "market_cap_munit": x.get("marketValue"),  # 백만원 단위 추정
        })
        if len(peers) >= limit:
            break
    return peers


def _parse_naver_consensus(j: dict) -> dict | None:
    c = j.get("consensusInfo")
    if not c:
        return None
    return c if isinstance(c, dict) else None


def _fetch_short_sale(ticker: str) -> dict | None:
    """네이버 공매도 페이지에서 최근 잔고 추출. 실패 시 None."""
    try:
        url = f"https://finance.naver.com/item/sise_dd_lc_short.naver?code={ticker}"
        r = requests.get(url, headers=NAVER_HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        # 간단 추출: 페이지의 첫 데이터 행에서 공매도잔고·잔고비율
        import re
        m = re.search(
            r'<td[^>]*class="num"[^>]*>([\d,]+)</td>\s*'
            r'<td[^>]*class="num"[^>]*>([\d.]+)%</td>',
            r.text,
        )
        if not m:
            return None
        return {
            "shares": int(m.group(1).replace(",", "")),
            "ratio_pct": float(m.group(2)),
        }
    except Exception as e:
        log.debug("short sale fetch fail %s: %s", ticker, e)
        return None


_market_cache: tuple[float, dict] = (0.0, {})


def fetch_market_snapshot() -> dict:
    """코스피·코스닥 지수 일등락률. 5분 캐시."""
    global _market_cache
    now = time.time()
    if now - _market_cache[0] < 300 and _market_cache[1]:
        return _market_cache[1]
    out: dict = {}
    try:
        for code in ("KOSPI", "KOSDAQ"):
            r = requests.get(
                f"https://m.stock.naver.com/api/index/{code}/basic",
                headers=NAVER_HEADERS, timeout=10,
            )
            if r.status_code != 200:
                continue
            j = r.json()
            out[code] = {
                "close": j.get("closePrice"),
                "change_pct": j.get("fluctuationsRatio"),
                "change_dir": (j.get("compareToPreviousPrice") or {}).get("text"),
            }
    except Exception as e:
        log.warning("market snapshot fail: %s", e)
    _market_cache = (now, out)
    return out


DISCLOSURE_WINDOW_DAYS = 180        # 분석 컨텍스트에 유지하는 공시 기간


def collect_disclosures_cached(ticker: str, name: str, corp: str,
                               force_since: str | None = None) -> list[dict]:
    """증분 공시 수집 + 과거 캐시 머지.

    - 마지막 조회일(metadata.last_disclosure_scan_date) '그 날부터' 다시 조회한다
      (하루 겹침). 오전에 조회한 뒤 같은 날 오후에 올라온 공시를 놓치지 않기 위함.
      첫 방문이면 since=None → 180일 전체.
    - DART 신규분 + jsonl 캐시(과거 전체)를 rcept_no로 머지하고 180일 이내로 컷한다.
      → 매번 전체를 다시 받지 않아도 분석 컨텍스트에는 180일 공시가 다 들어간다.
    - 신규 제목을 jsonl에 누적하고 last_disclosure_scan_date를 오늘로 갱신한다.
    - summary(본문 요약)는 여기서 붙이지 않는다(LLM 단계 enrich 담당). 단 캐시에
      이미 있던 summary는 머지에서 보존한다.
    """
    import ticker_archive  # 지연 import (data_loader ↔ ticker_archive 순환 방지)

    meta = ticker_archive.read_metadata(ticker, name)
    since = force_since or meta.get("last_disclosure_scan_date")
    new_rows = _fetch_disclosures(corp, since=since)
    log.info("[%s] 공시 증분 조회: since=%s → 신규/갱신 %d건",
             ticker, since or "(첫 수집·180일)", len(new_rows))

    cached = ticker_archive.read_disclosure_summaries(ticker, name)  # rcept_no→row
    merged: dict[str, dict] = dict(cached)
    for r in new_rows:
        no = r["rcept_no"]
        if no in merged:
            keep_summary = merged[no].get("summary")
            merged[no] = {**merged[no], **r}     # 최신 메타로 갱신
            if keep_summary and not merged[no].get("summary"):
                merged[no]["summary"] = keep_summary   # 캐시 요약 보존
        else:
            merged[no] = r

    cutoff = (date.today() - timedelta(days=DISCLOSURE_WINDOW_DAYS)).strftime("%Y%m%d")
    rows = [r for r in merged.values() if (r.get("date") or "0") >= cutoff]
    LEVEL_RANK = {"fatal": 0, "warn": 1, "info": 2, None: 3}
    rows.sort(key=lambda r: (LEVEL_RANK.get(r.get("signal_level"), 3),
                             -int(r.get("date") or 0)))

    # 신규 제목 누적(append-only, rcept_no dedup) + 마지막 조회일 갱신
    try:
        ticker_archive.append_disclosures(ticker, name, new_rows)
        ticker_archive.write_metadata(
            ticker, name,
            last_disclosure_scan_date=date.today().strftime("%Y%m%d"))
    except Exception as e:
        log.warning("[%s] 공시 캐시 누적/메타 갱신 실패: %s", ticker, e)

    return rows[:60]


def load_context(ticker: str, years: int = 2,
                  since: str | None = None) -> StockContext | None:
    """공시는 캐시 기반 증분 수집(collect_disclosures_cached). since를 주면 그 날짜를
    강제 시작점으로 쓴다(보통은 metadata의 마지막 조회일을 자동 사용).
    재무·발행물 등은 분기 단위라 항상 최신 보고서를 새로 받는다.
    """
    mapping = _load_corp_mapping()
    info = mapping.get(ticker)
    if not info:
        log.warning("매핑 없음: %s", ticker)
        return None
    corp = info["corp_code"]

    # 종목 표시명을 먼저 확정 (공시 캐시 폴더명에 필요)
    display_name = info.get("name") if info.get("is_preferred") else info.get("corp_name", "")
    if not display_name:
        display_name = info.get("corp_name", "")

    company = _fetch_company(corp)

    financials = _fetch_financials_all(corp, years=years)
    disclosures = collect_disclosures_cached(ticker, display_name, corp,
                                             force_since=since)

    nav = _fetch_naver_integration(ticker)
    valuation = _parse_naver_valuation(nav)
    peers = _parse_naver_peers(nav, ticker)
    consensus = _parse_naver_consensus(nav)
    short_sale = _fetch_short_sale(ticker)
    market_snapshot = fetch_market_snapshot()

    # 우선주면 본주 시세도 같이 받기. 분석 시 디스카운트율/배당률 비교용.
    preferred_info: dict | None = None
    if info.get("is_preferred"):
        common_t = info.get("common_ticker") or ""
        common_meta = mapping.get(common_t) or {}
        common_valuation: dict[str, str] = {}
        try:
            common_nav = _fetch_naver_integration(common_t)
            common_valuation = _parse_naver_valuation(common_nav)
        except Exception as e:
            log.warning("[%s] 본주(%s) 시세 fetch 실패: %s", ticker, common_t, e)
        preferred_info = {
            "is_preferred": True,
            "common_ticker": common_t,
            "common_name": common_meta.get("name") or common_meta.get("corp_name", ""),
            "series": info.get("series", ""),
            "common_valuation": common_valuation,
        }

    return StockContext(
        ticker=ticker,
        corp_code=corp,
        name=display_name,
        industry=company.get("induty_code_nm", "") or company.get("induty", ""),
        listing_date=company.get("ipo_dt", ""),
        ceo=company.get("ceo_nm", ""),
        homepage=company.get("hm_url", ""),
        financials=financials,
        recent_disclosures=disclosures,
        valuation=valuation, peers=peers,
        consensus=consensus, short_sale=short_sale,
        market_snapshot=market_snapshot,
        preferred_info=preferred_info,
    )


def load_many(tickers: Iterable[str]) -> dict[str, StockContext]:
    out: dict[str, StockContext] = {}
    for t in tickers:
        ctx = load_context(t)
        if ctx:
            out[t] = ctx
        time.sleep(0.1)        # DART rate limit 완화
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_ctx(ctx: StockContext) -> None:
    print(f"\n=== {ctx.name} ({ctx.ticker}) corp={ctx.corp_code} ===")
    print(f"  업종: {ctx.industry} / 대표: {ctx.ceo} / 상장: {ctx.listing_date}")
    print(f"  홈페이지: {ctx.homepage}")
    print(f"  연간 재무:")
    for y, accs in ctx.financials.items():
        line = ", ".join(f"{k} {v/1e8:,.0f}억" for k, v in accs.items())
        print(f"    [{y}] {line}")
    print(f"  최근 공시 {len(ctx.recent_disclosures)}건:")
    for d in ctx.recent_disclosures[:5]:
        print(f"    {d['date']}  {d['title']}")


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python data_loader.py <ticker> [<ticker> ...]")
        sys.exit(1)
    for t in sys.argv[1:]:
        ctx = load_context(t)
        if ctx:
            _print_ctx(ctx)
        else:
            print(f"{t}: 데이터 없음")
