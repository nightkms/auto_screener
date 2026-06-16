"""
S4: 종합 + 등급 판정.

5개 서브에이전트 결과를 Opus(또는 설정 모델)로 묶어 매트릭스 + 마크다운 보고서 생성.
GRADE 토큰을 파싱해 등급(STRONG/WATCH/INTEREST/SKIP)을 확정한다.

CLI:
    python synthesizer.py 260970
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

import agents
import config
import data_loader
import selector

log = logging.getLogger("synthesizer")

Grade = Literal["STRONG", "WATCH", "INTEREST", "SKIP"]
GRADE_PAT = re.compile(r"GRADE:\s*(STRONG|WATCH|INTEREST|SKIP)")
GRADE_RANK = {"STRONG": 3, "WATCH": 2, "INTEREST": 1, "SKIP": 0}

# Claude Max 구독 한도 초과 등 "회복 가능한" 실패 신호. 종합 응답이 분석이 아니라
# 이런 안내문이면 보고서로 저장하면 안 되고(좀비 보고서), 큐가 다음에 재시도해야 한다.
_LIMIT_PAT = re.compile(r"hit your limit|usage limit|rate limit|resets?\s+\d", re.I)


def is_usage_limit(text: str) -> bool:
    """한도 초과/리셋 안내문이 섞였는지. 종합·서브 결과 공통 판정에 쓴다."""
    return bool(text and _LIMIT_PAT.search(text))


@dataclass
class FinalReport:
    ticker: str
    name: str
    grade: Grade
    avg_rating: float
    sub_ratings: dict[str, float | None]
    markdown: str
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_s: float = 0.0
    ok: bool = True           # False면 종합 실패(한도초과 등) → 저장 금지·큐 재시도


# ---------------------------------------------------------------------------
# Fallback 등급 판정 (LLM이 GRADE 미출력 시 규칙으로 보정)
# ---------------------------------------------------------------------------
def _rule_grade(sub_ratings: dict[str, float | None]) -> Grade:
    avg = _avg(sub_ratings)
    risk = sub_ratings.get("risk") or 0
    has_catalyst = (sub_ratings.get("catalyst") or 0) >= 3.5
    if avg >= 4.0 and risk >= 3.5 and has_catalyst:
        return "STRONG"
    if avg >= 3.5 and risk >= 3.0:
        return "WATCH"
    if avg >= 3.0:
        return "INTEREST"
    return "SKIP"


def _avg(ratings: dict[str, float | None]) -> float:
    vals = [v for v in ratings.values() if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else 0.0


# ---------------------------------------------------------------------------
# 종합 호출
# ---------------------------------------------------------------------------
def _build_synth_prompt(candidate: selector.Candidate,
                        analysis: agents.StockAnalysis,
                        prior_summary: str = "") -> str:
    lines: list[str] = []
    lines.append(f"# 종합 보고 요청: {analysis.name} ({analysis.ticker})")
    lines.append(f"- 시장: {candidate.market}, 종가: {candidate.close:,}원")
    lines.append(f"- 주간 등락률: {candidate.weekly_return:+.2f}% / 거래대금 급증: {candidate.value_surge:.2f}배")
    lines.append(f"- 외인 보유율 변화: {candidate.foreign_delta:+.3f}%p")
    lines.append(f"- 분석일: {date.today().isoformat()}")
    mover = {"upper": "상한가", "quant": "거래량 급증"}.get(candidate.source_tag or "")
    if mover:
        lines.append(f"- **선정 사유: 오늘 {mover}** → 카탈리스트 보고의 '당일 급등 "
                     f"트리거'를 보고서에 `## 📈 오늘 {mover} 사유` 섹션으로 명시할 것.")
    lines.append("")
    if prior_summary:
        lines.append("## 📌 이전 회차 종합 요약 (변동 비교용)")
        lines.append(prior_summary)
        lines.append("")
        lines.append("_위는 직전 회차 결론이다. 이번 회차 5명 보고(아래)와 비교해 "
                     "`## 이전 회차 대비 변동` 섹션을 작성하라._")
        lines.append("")
    for sub_name in agents.SUB_AGENTS:
        r = analysis.sub_results.get(sub_name)
        if not r:
            continue
        lines.append(f"## [{sub_name}] 별점={r.rating}")
        lines.append(r.text or "(빈 결과)")
        lines.append("")
    return "\n".join(lines)


async def synthesize(candidate: selector.Candidate,
                     analysis: agents.StockAnalysis,
                     prior_summary: str = "") -> FinalReport:
    system = (config.PROMPTS_DIR / "synthesizer.txt").read_text(encoding="utf-8")
    opts = ClaudeAgentOptions(
        system_prompt=system,
        model=config.CLAUDE_SYNTH_MODEL,
        permission_mode="bypassPermissions",
        allowed_tools=[],
        max_turns=2,
        env=config.sdk_env(),          # 홈 ~/.claude.json 동시 write 경합 회피
    )
    user_prompt = _build_synth_prompt(candidate, analysis, prior_summary)

    log.info("[%s] 종합 시작", candidate.ticker)
    started = time.time()
    pieces: list[str] = []
    tokens_in = tokens_out = 0
    synth_failed = False
    try:
        async for msg in query(prompt=user_prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        pieces.append(block.text)
            elif isinstance(msg, ResultMessage):
                usage = getattr(msg, "usage", None)
                if usage:
                    tokens_in = (usage.get("input_tokens") or 0) + \
                                (usage.get("cache_read_input_tokens") or 0)
                    tokens_out = usage.get("output_tokens") or 0
    except Exception as e:
        log.exception("[%s] 종합 실패", candidate.ticker)
        synth_failed = True
        pieces.append(f"\n\n_종합 호출 실패: {e}_\n")

    markdown = "\n".join(pieces).strip()
    sub_ratings = {n: r.rating for n, r in analysis.sub_results.items()}
    avg = _avg(sub_ratings)

    m = GRADE_PAT.search(markdown)
    grade: Grade = m.group(1) if m else _rule_grade(sub_ratings)  # type: ignore

    # 종합이 예외로 끊겼거나, 본문이 비었거나, 한도초과 안내문이면 실패로 본다.
    # → pipeline이 보고서를 저장하지 않고 큐에 재시도를 떠넘긴다(좀비 보고서 방지).
    ok = not synth_failed and bool(markdown) and not is_usage_limit(markdown)
    if not ok:
        log.warning("[%s] 종합 결과 불완전(한도초과/실패) → 저장 보류", candidate.ticker)

    return FinalReport(
        ticker=candidate.ticker, name=analysis.name, grade=grade,
        avg_rating=avg, sub_ratings=sub_ratings,
        markdown=markdown,
        tokens_in=tokens_in, tokens_out=tokens_out,
        elapsed_s=round(time.time() - started, 1),
        ok=ok,
    )


# ---------------------------------------------------------------------------
# CLI (selector → data_loader → agents → synthesizer)
# ---------------------------------------------------------------------------
async def _cli_single(ticker: str) -> None:
    rows = await selector.select_top_async(top_n=200)
    cand = next((c for c in rows if c.ticker == ticker), None)
    if not cand:
        cand = selector.Candidate(
            ticker=ticker, name="?", market="?", close=0,
            market_cap_billion=0, weekly_return=0,
            value_surge=0, foreign_delta=0, score=0,
        )
    ctx = data_loader.load_context(ticker)
    analysis = await agents.analyze_stock(cand, ctx)
    report = await synthesize(cand, analysis)
    print("\n" + "=" * 70)
    print(f"GRADE: {report.grade}   avg★={report.avg_rating}")
    print("=" * 70)
    print(report.markdown[:3000])


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL,
                        format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python synthesizer.py <ticker>")
        sys.exit(1)
    asyncio.run(_cli_single(sys.argv[1]))
