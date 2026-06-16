"""
S3: Claude Agent SDK 기반 멀티 서브에이전트.

5개 서브에이전트(밸류에이션·산업·주가수급·카탈리스트·리스크)를 종목별로 병렬 실행.
사용자가 Claude Code에 로그인된 상태면 Max 구독 한도 안에서 동작 (별도 API 키 불요).

CLI:
    python agents.py 260970        # 단일 종목 end-to-end 테스트
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

import config
import data_loader
import selector
import ticker_archive

log = logging.getLogger("agents")

SUB_AGENTS = ("valuation", "industry", "price_flow", "catalyst", "risk")
RATING_PAT = re.compile(r"RATING:\s*([0-9]\.[05]|[1-5])")

# WebSearch 사용이 많은 에이전트는 turn 여유를 더 준다
MAX_TURNS_PER_AGENT = {
    "valuation": 6,
    "industry": 10,
    "price_flow": 6,
    "catalyst": 12,
    "risk": 8,
}

# SDK 호출이 hang되면 종목 1개가 큐 워커와 스케줄러 이벤트 루프를 무기한 점유한다
# (실제로 2026-05-27 코오롱티슈진 950160 건이 15시간 hang → hourly cron 전체 동결).
# sub들은 병렬이므로 종목당 wall-clock도 동일. timeout 도달 시 RetryableError로
# 전환되어 다음 정각에 재시도된다.
SUB_AGENT_TIMEOUT_S = 600


@dataclass
class SubAgentResult:
    name: str
    text: str
    rating: float | None
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_s: float = 0.0
    error: str = ""


@dataclass
class StockAnalysis:
    ticker: str
    name: str
    sub_results: dict[str, SubAgentResult] = field(default_factory=dict)
    total_tokens_in: int = 0
    total_tokens_out: int = 0

    def average_rating(self) -> float:
        ratings = [r.rating for r in self.sub_results.values() if r.rating is not None]
        return round(sum(ratings) / len(ratings), 2) if ratings else 0.0


# ---------------------------------------------------------------------------
# 프롬프트 로딩
# ---------------------------------------------------------------------------
def _load_prompt(name: str) -> str:
    return (config.PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def _build_user_prompt(name: str, ticker: str, candidate: selector.Candidate,
                       ctx: data_loader.StockContext | None,
                       prior_summary: str = "") -> str:
    """서브에이전트에 넘길 사용자 메시지(컨텍스트).

    prior_summary: 이전 회차 종합 보고서의 한 줄 결론. 같은 종목 재분석 시
    LLM이 등급 변동 사유를 명시할 수 있도록 컨텍스트로 주입.
    """
    lines: list[str] = []
    if prior_summary:
        lines.append("## 📌 이전 회차 분석 요약 (참고용)")
        lines.append(prior_summary)
        lines.append("")
        lines.append("_이번 회차는 자료를 전체 새로 수집했다. 위 이전 결론을 그대로 "
                     "베끼지 말고, 신규 자료로 본 현재 상황을 독립 평가한 뒤 "
                     "등급/판단이 달라졌다면 변동 사유를 짧게 명시하라._")
        lines.append("")
    lines.append(f"## 종목 정보")
    lines.append(f"- 회사명: {name}")
    lines.append(f"- 종목코드: {ticker}")
    lines.append(f"- 시장: {candidate.market}")
    lines.append(f"- 현재가(직전 종가): {candidate.close:,}원")
    lines.append("")

    # 오늘 실제로 움직여서 선정된 종목(상한가·거래량급증)은 '당일 트리거' 규명이 핵심.
    mover = {"upper": "상한가", "quant": "거래량 급증"}.get(candidate.source_tag or "")
    if mover:
        lines.append(f"## 🚨 당일 급등 종목 (선정 사유: 오늘 {mover})")
        lines.append(f"- 이 종목은 **오늘(직전 거래일) {mover}**로 시세 리스트에 포착되어 "
                     "분석 대상이 됐다.")
        lines.append(f"- **반드시 '오늘 왜 {mover}가 나왔는지' 당일 트리거를 WebSearch로 "
                     "규명하라**: 당일 공시·뉴스·테마/정책·수급(외국인·기관·개인) 중 무엇이 "
                     "방아쇠였는지 구체적 사실로 짚을 것.")
        lines.append("- 트리거를 못 찾으면 '당일 트리거 미확인'이라고 명시하고 추정 수위를 "
                     "낮춰라 (지어내기 금지).")
        lines.append("")

    # 우선주 분석 가이드 (본주와 같이 분석하라는 인라인 지시)
    pref = ctx.preferred_info if ctx and ctx.preferred_info else None
    if pref:
        lines.append("## 🔔 우선주 분석 가이드 (필독)")
        lines.append(f"- **이 종목은 우선주이고, 본주는 {pref.get('common_name','?')}"
                     f"({pref.get('common_ticker','?')})다.**")
        lines.append(f"- 종류: {pref.get('series','?')}")
        lines.append("- 분석 방식: **본주 + 우선주를 같이 분석하라.**")
        lines.append("  - 산업·재무·카탈리스트 등 사업 흐름은 본주 기준 (공시·재무는 본주와 동일 법인이므로 본 컨텍스트가 본주 데이터다)")
        lines.append("  - 우선주 특유 요소도 같이 짚을 것: 본주 대비 디스카운트율과 역사적 위치, 시가배당률 비교, 유동성, "
                     "의결권 부재의 함의, 신형/구형 구분에 따른 최저배당률·의결권 부활 조건, "
                     "자사주 매입·소각 시 우선주 비중, 상장폐지·강제매수 리스크(발행 잔량) 등 — 자료 부족하면 WebSearch로 보강")
        lines.append("- 별점은 우선주 매수자의 관점에서 매기되, 본주 펀더멘털 분석도 보고서에 포함")
        lines.append("")
        # 본주 시세 비교 표
        common_v = pref.get("common_valuation") or {}
        my_v = ctx.valuation if ctx else {}
        if common_v or my_v:
            lines.append("### 본주 vs 우선주 시세·밸류 (네이버 실시간)")
            keys = [("market_cap","시총"), ("per","PER"), ("pbr","PBR"),
                    ("eps","EPS"), ("bps","BPS"),
                    ("dividend_yield","배당수익률"), ("dps","주당배당금"),
                    ("foreign_rate","외인소진율"),
                    ("high52w","52주 최고"), ("low52w","52주 최저"),
                    ("fwd_per","추정PER"), ("fwd_eps","추정EPS")]
            lines.append(f"| 항목 | 본주({pref.get('common_ticker','')}) | 우선주({ticker}) |")
            lines.append("|---|---|---|")
            for k, label in keys:
                cv = common_v.get(k, "-")
                mv = my_v.get(k, "-")
                if cv == "-" and mv == "-":
                    continue
                lines.append(f"| {label} | {cv} | {mv} |")
            lines.append("")
            lines.append("_위 표만으로 디스카운트율 추정 가능. 시총·종가 차이로 % 계산하라._")
            lines.append("")
    lines.append("## 주간 시세 지표")
    lines.append(f"- 주간 등락률: {candidate.weekly_return:+.2f}%")
    lines.append(f"- 거래대금 급증도(주간 평균 / 직전 4주 평균): {candidate.value_surge:.2f}배")
    lines.append(f"- 외국인 보유율 5일 변화: {candidate.foreign_delta:+.3f}%p")
    lines.append("")

    if ctx and ctx.market_snapshot:
        lines.append("## 시장 벤치마크 (오늘)")
        for code, info in ctx.market_snapshot.items():
            lines.append(f"- {code}: 종가 {info.get('close')}, "
                         f"{info.get('change_dir', '')} {info.get('change_pct')}%")
        lines.append("")

    if ctx:
        lines.append("## 회사 개요 (DART)")
        if ctx.industry:
            lines.append(f"- 업종: {ctx.industry}")
        if ctx.ceo:
            lines.append(f"- 대표이사: {ctx.ceo}")
        if ctx.homepage:
            lines.append(f"- 홈페이지: {ctx.homepage}")
        lines.append("")
        if ctx.valuation:
            lines.append("## 시장 가격 지표 (네이버 실시간)")
            v = ctx.valuation
            for k, label in [("market_cap", "시총"), ("per", "PER"),
                              ("pbr", "PBR"), ("eps", "EPS"), ("bps", "BPS"),
                              ("dividend_yield", "배당수익률"),
                              ("dps", "주당배당금"),
                              ("foreign_rate", "외인소진율"),
                              ("high52w", "52주 최고"),
                              ("low52w", "52주 최저"),
                              ("fwd_per", "추정PER"),
                              ("fwd_eps", "추정EPS")]:
                if k in v:
                    lines.append(f"- {label}: {v[k]}")
            lines.append("")
        if ctx.short_sale:
            lines.append(f"## 공매도 잔고: {ctx.short_sale.get('shares', '?'):,}주 "
                         f"(시총 대비 {ctx.short_sale.get('ratio_pct', '?')}%)")
            lines.append("")
        if ctx.consensus:
            lines.append("## 증권사 컨센서스")
            lines.append(f"- {ctx.consensus}")
            lines.append("")
        if ctx.peers:
            lines.append("## 동종업종 비교 (참고)")
            for p in ctx.peers[:5]:
                lines.append(f"- {p.get('name')} ({p.get('ticker')}): "
                             f"등락 {p.get('fluctuation_pct')}%, 시총 {p.get('market_cap_munit')}")
            lines.append("")
        if ctx.financials:
            lines.append("## 재무 (백만원, 최신 보고서 우선)")
            for period, accs in ctx.financials.items():
                row = ", ".join(
                    f"{k} {v/1e6:,.0f}" for k, v in accs.items()
                )
                lines.append(f"- [{period}] {row}")
            # 핵심 비율 직접 계산 (가장 최신 period)
            if ctx.financials:
                latest = next(iter(ctx.financials.values()))
                rev = latest.get("매출액"); op = latest.get("영업이익")
                ni = latest.get("당기순이익")
                assets = latest.get("자산총계"); liab = latest.get("부채총계")
                eq = latest.get("자본총계")
                ratios = []
                if rev and op:
                    ratios.append(f"영업이익률 {op/rev*100:.1f}%")
                if rev and ni:
                    ratios.append(f"순이익률 {ni/rev*100:.1f}%")
                if eq and liab is not None:
                    ratios.append(f"부채비율 {liab/eq*100:.1f}%")
                if eq and ni:
                    ratios.append(f"ROE {ni/eq*100:.1f}% (분기 기준)")
                if assets and ni:
                    ratios.append(f"ROA {ni/assets*100:.1f}% (분기 기준)")
                if ratios:
                    lines.append("- 핵심 비율(최신 period 기준): " + " / ".join(ratios))
            lines.append("")
        if ctx.recent_disclosures:
            # 자동 분류된 주목 공시. 라벨은 '단서'일 뿐 최종 판단은 LLM이 뉴스로
            critical = [d for d in ctx.recent_disclosures
                        if d.get("signal_level") in ("fatal", "warn")]
            if critical:
                lines.append("## 📌 자동 분류 주목 공시 (라벨은 단서 — 뉴스로 맥락 확인 필수)")
                lines.append("_같은 키워드라도 호재/악재가 갈립니다. 예: 유상증자는 자금 사정 악화일 수도, "
                             "신사업 M&A 자금 조달의 호재일 수도. 반드시 뉴스로 목적·시장 반응 확인 후 판단할 것._")
                for d in critical[:15]:
                    lvl = d.get("signal_level", "")
                    cat = d.get("signal_category", "")
                    hint = "잠재위험" if lvl == "fatal" else "주목"
                    lines.append(f"- [{hint}/{cat}] {d['date']} {d['title']}")
                    if d.get("summary"):
                        s = d["summary"].replace("\n", " ").strip()
                        lines.append(f"    ↳ 본문요약: {s}")
                lines.append("")
            lines.append(f"## 최근 공시 (180일, {len(ctx.recent_disclosures)}건)")
            lines.append("_↳ 본문요약이 있는 건은 제목이 아니라 요약(원문 기반)을 근거로 판단할 것._")
            for d in ctx.recent_disclosures[:20]:
                tag = f"[{d.get('type','')}]" if d.get("type") else ""
                lines.append(f"- {d['date']} {tag} {d['title']}")
                if d.get("summary"):
                    s = d["summary"].replace("\n", " ").strip()
                    lines.append(f"    ↳ 본문요약: {s}")
            lines.append("")

    lines.append("위 컨텍스트만 사실로 사용하라. 부족하면 WebSearch로 보강.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SDK 호출
# ---------------------------------------------------------------------------
async def _run_one(sub_name: str, user_prompt: str,
                   model: str | None = None) -> SubAgentResult:
    system = _load_prompt(sub_name)
    opts = ClaudeAgentOptions(
        system_prompt=system,
        model=model or config.CLAUDE_SUB_MODEL,
        permission_mode="bypassPermissions",
        allowed_tools=["WebSearch", "WebFetch"],
        max_turns=MAX_TURNS_PER_AGENT.get(sub_name, 6),
        env=config.sdk_env(),          # 홈 ~/.claude.json 동시 write 경합 회피
    )
    started = time.time()
    pieces: list[str] = []
    tokens_in = tokens_out = 0
    err = ""

    async def _consume() -> tuple[int, int]:
        t_in = t_out = 0
        async for msg in query(prompt=user_prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        pieces.append(block.text)
            elif isinstance(msg, ResultMessage):
                usage = getattr(msg, "usage", None)
                if usage:
                    t_in = (usage.get("input_tokens") or 0) + \
                           (usage.get("cache_read_input_tokens") or 0)
                    t_out = usage.get("output_tokens") or 0
        return t_in, t_out

    try:
        tokens_in, tokens_out = await asyncio.wait_for(
            _consume(), timeout=SUB_AGENT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        err = f"TimeoutError: sub agent exceeded {SUB_AGENT_TIMEOUT_S}s"
        log.warning("[%s] %ds timeout 도달 → 강제 중단", sub_name,
                    SUB_AGENT_TIMEOUT_S)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log.exception("[%s] 실패", sub_name)
    text = "\n".join(pieces).strip()
    m = RATING_PAT.search(text)
    rating = float(m.group(1)) if m else None
    return SubAgentResult(
        name=sub_name, text=text, rating=rating,
        tokens_in=tokens_in, tokens_out=tokens_out,
        elapsed_s=round(time.time() - started, 1), error=err,
    )


# ---------------------------------------------------------------------------
# 공시 본문 요약 (증분 캐시) — 제목만으로 호재/악재가 안 드러나는 공시를 위해.
# 비싼 건 본문 fetch + LLM 요약이므로 그 결과만 캐시하고, 목록(list.json)은
# 기존 정책대로 매번 180일 전체를 새로 받는다.
# ---------------------------------------------------------------------------
MAX_DOCS_PER_STOCK = 8           # 종목당 본문 fetch+요약 상한 (signal/최신 우선)
DISCLOSURE_SUMMARY_TIMEOUT_S = 120

_DISCLOSURE_SUMMARY_SYSTEM = (
    "너는 한국 DART 공시 원문을 분석가용으로 압축하는 도구다. "
    "주어진 공시 본문에서 투자 판단에 필요한 사실만 2~4문장으로 요약하라.\n"
    "규칙:\n"
    "- 공시일 시점의 사실만 적는다. 추측·전망·매수/매도 의견 금지.\n"
    "- 누가/무엇을/얼마나(주식수·금액·지분율)/왜(사유)를 구체 수치로.\n"
    "- 증여·담보·계약 건은 상대방과 목적(승계·자금조달 등)을 드러낸다.\n"
    "- 본문에 없는 내용은 지어내지 말 것. 한국어로 답한다."
)


async def _summarize_document(d: dict, body: str, fields: dict) -> str:
    """공시 원문 1건을 2~4문장으로 요약. 실패 시 빈 문자열."""
    field_hint = ""
    if fields:
        field_hint = "\n".join(f"- {k}: {' / '.join(v)}"
                               for k, v in fields.items())
    prompt = (
        f"공시 제목: {d.get('title','')}\n"
        f"공시일: {d.get('date','')}\n"
        f"유형: {d.get('type','')}\n"
        f"접수번호: {d.get('rcept_no','')}\n"
        + (f"\n[자동추출 핵심필드]\n{field_hint}\n" if field_hint else "")
        + f"\n[본문]\n{body}\n\n위 공시를 규칙에 맞게 요약하라."
    )
    opts = ClaudeAgentOptions(
        system_prompt=_DISCLOSURE_SUMMARY_SYSTEM,
        model=config.CLAUDE_SUB_MODEL,
        permission_mode="bypassPermissions",
        allowed_tools=[],          # 본문만 보고 요약 (웹검색 불필요)
        max_turns=1,
        env=config.sdk_env(),      # 홈 ~/.claude.json 동시 write 경합 회피
    )
    pieces: list[str] = []

    async def _consume() -> None:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        pieces.append(block.text)

    try:
        await asyncio.wait_for(_consume(), timeout=DISCLOSURE_SUMMARY_TIMEOUT_S)
    except Exception as e:
        log.warning("공시 요약 실패 rcept=%s: %s", d.get("rcept_no"), e)
        return ""
    return "\n".join(pieces).strip()


async def enrich_disclosures(ticker: str, name: str,
                             disclosures: list[dict],
                             max_docs: int = MAX_DOCS_PER_STOCK) -> int:
    """recent_disclosures를 in-place 보강: 본문 요약(summary)을 붙인다.
    1) 캐시(jsonl)에 summary 있는 rcept_no는 그대로 재사용 (LLM 재호출 없음).
    2) 캐시에 없고 should_fetch_document 대상인 건만 본문 fetch + LLM 요약.
    3) 새로 요약한 것만 jsonl에 캐시 (다음 회차 재사용).
    반환: 새로 요약한 건수."""
    if not disclosures:
        return 0
    cache = ticker_archive.read_disclosure_summaries(ticker, name)
    # 1) 캐시 재사용
    for d in disclosures:
        cached = cache.get(d.get("rcept_no", ""))
        if cached and cached.get("summary"):
            d["summary"] = cached["summary"]

    # 2) 미캐시 + 대상만 (recent_disclosures는 이미 signal/최신 우선 정렬)
    todo = [d for d in disclosures
            if not d.get("summary") and data_loader.should_fetch_document(d)]
    todo = todo[:max_docs]
    if not todo:
        return 0
    log.info("[%s] 공시 본문 요약 신규 대상 %d건", ticker, len(todo))

    async def _one(d: dict) -> dict | None:
        got = await asyncio.to_thread(data_loader.fetch_document, d["rcept_no"])
        if not got:
            return None
        body, fields = got
        summary = await _summarize_document(d, body, fields)
        if not summary:
            return None
        d["summary"] = summary
        return d

    done = await asyncio.gather(*[_one(d) for d in todo])
    newly = [d for d in done if d]
    if newly:
        ticker_archive.cache_disclosure_summaries(ticker, name, newly)
    return len(newly)


async def analyze_stock(candidate: selector.Candidate,
                        ctx: data_loader.StockContext | None,
                        prior_summary: str = "") -> StockAnalysis:
    """1개 종목에 대해 5개 서브에이전트 병렬 실행.
    prior_summary: 이전 회차 종합 보고서의 한 줄 결론. 같은 종목 재분석 시
    LLM이 등급 변동 사유를 명시할 수 있도록 컨텍스트로 주입."""
    name = ctx.name if ctx else candidate.name
    # 5개 서브에이전트 전에 공시 본문을 요약해 컨텍스트에 주입 (증분 캐시).
    if ctx and ctx.recent_disclosures:
        try:
            n = await enrich_disclosures(candidate.ticker, name,
                                         ctx.recent_disclosures)
            if n:
                log.info("[%s] 공시 본문 요약 %d건 추가", candidate.ticker, n)
        except Exception as e:
            log.warning("[%s] 공시 본문 요약 실패: %s", candidate.ticker, e)
    user_prompt = _build_user_prompt(
        name, candidate.ticker, candidate, ctx,
        prior_summary=prior_summary,
    )

    log.info("[%s] %s 분석 시작 (5개 병렬)", candidate.ticker, name)
    results = await asyncio.gather(*[
        _run_one(sub, user_prompt) for sub in SUB_AGENTS
    ])
    sub_map = {r.name: r for r in results}

    analysis = StockAnalysis(ticker=candidate.ticker, name=name, sub_results=sub_map)
    analysis.total_tokens_in = sum(r.tokens_in for r in results)
    analysis.total_tokens_out = sum(r.tokens_out for r in results)
    log.info("[%s] 완료 avg★=%.2f tokens=%d/%d",
             candidate.ticker, analysis.average_rating(),
             analysis.total_tokens_in, analysis.total_tokens_out)
    return analysis


async def analyze_many(candidates: Iterable[selector.Candidate],
                       contexts: dict[str, data_loader.StockContext]
                       ) -> list[StockAnalysis]:
    """후보들을 종목 단위로 순차 처리 (한 종목 안에선 5개 병렬). Rate 부담 완화용."""
    out: list[StockAnalysis] = []
    for c in candidates:
        a = await analyze_stock(c, contexts.get(c.ticker))
        out.append(a)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _cli_single(ticker: str) -> None:
    # selector를 거치지 않고 단일 종목 직접 분석 (테스트용)
    rows = await selector.select_top_async(top_n=200)
    cand = next((c for c in rows if c.ticker == ticker), None)
    if not cand:
        print(f"{ticker}: 시총 상위 200에 없음. 임시 후보로 진행.")
        cand = selector.Candidate(
            ticker=ticker, name="?", market="?",
            close=0, market_cap_billion=0,
            weekly_return=0, value_surge=0, foreign_delta=0, score=0,
        )
    ctx = data_loader.load_context(ticker)
    analysis = await analyze_stock(cand, ctx)
    print(f"\n=== {analysis.name} ({analysis.ticker}) avg★={analysis.average_rating()} ===\n")
    for sub in SUB_AGENTS:
        r = analysis.sub_results[sub]
        print(f"--- {sub} ★{r.rating} ({r.elapsed_s}s, in={r.tokens_in} out={r.tokens_out}) ---")
        print(r.text[:500])
        if r.error:
            print("ERROR:", r.error)
        print()


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL,
                        format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python agents.py <ticker>")
        sys.exit(1)
    asyncio.run(_cli_single(sys.argv[1]))
