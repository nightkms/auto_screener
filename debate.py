"""
S3.5: 강세/약세 연구원 토론 — TradingAgents의 Researcher Team(Bull vs Bear).

5명 분석가 보고 + 사전 리서치 브리프를 근거로 강세·약세 연구원이 DEBATE_ROUNDS 라운드
번갈아 주장·반박한다(강세 먼저). 각 연구원은 자기 세션을 resume으로 이어가서, 둘째 라운드부터는
상대의 직전 발언만 새로 받는다(근거 자료를 다시 보내지 않아 입력 토큰 절약).
판정은 synthesizer(Opus)가 토론 기록을 받아 한다 — 원작의 Research Manager 역할.

검색 도구는 주지 않는다(원작과 같이 분석가 보고로만 토론). 토론이 실패해도 종합은 진행된다.

CLI:
    python debate.py 260970        # 분석가 5명 → 토론까지 실행해 기록 출력 (종합·저장 없음)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field

import agents
import config
import lessons
import selector

log = logging.getLogger("debate")

DEBATE_TIMEOUT_S = 300


@dataclass
class Debate:
    turns: list[tuple[str, str]] = field(default_factory=list)   # (bull|bear, 발언)
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ""

    def transcript(self) -> str:
        label = {"bull": "🐂 강세 연구원", "bear": "🐻 약세 연구원"}
        return "\n\n".join(f"#### {label[side]} — {i // 2 + 1}라운드\n{text}"
                           for i, (side, text) in enumerate(self.turns))


def _materials(candidate: selector.Candidate, analysis: agents.StockAnalysis) -> str:
    lines = [f"# 토론 대상: {analysis.name} ({analysis.ticker})",
             f"- 시장: {candidate.market}, 종가: {candidate.close:,}원, "
             f"주간 등락률: {candidate.weekly_return:+.2f}%"]
    for k, v in (analysis.valuation or {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    if analysis.research and analysis.research.text:
        lines += ["## [research] 사전 리서치 브리프", analysis.research.text.strip(), ""]
    for name in agents.SUB_AGENTS:
        r = analysis.sub_results.get(name)
        if r and r.text:
            star = f"별점={r.rating}" if r.rating is not None else "별점=미산정"
            lines += [f"## [{name}] {star}", r.text.strip(), ""]
    return "\n".join(lines)


async def run(candidate: selector.Candidate, analysis: agents.StockAnalysis,
              rounds: int | None = None) -> Debate:
    rounds = config.DEBATE_ROUNDS if rounds is None else rounds
    deb = Debate()
    if rounds <= 0:
        return deb
    base = _materials(candidate, analysis)
    sessions: dict[str, str] = {}
    last: dict[str, str] = {}
    for rnd in range(1, rounds + 1):
        for side, other in (("bull", "bear"), ("bear", "bull")):
            other_name = "약세" if other == "bear" else "강세"
            if side not in sessions:
                memo = lessons.block(side)
                prompt = base + (f"\n{memo}\n" if memo else "") + f"\n---\n{rnd}라운드 발언을 하라."
                if last.get(other):
                    prompt += f"\n\n## {other_name} 연구원의 직전 발언\n{last[other]}"
            else:
                prompt = (f"{rnd}라운드다. {other_name} 연구원의 직전 발언에 반박하며 주장을 보강하라. "
                          f"같은 출력 형식으로.\n\n## {other_name} 연구원의 직전 발언\n{last.get(other, '')}")
            res = await agents._run_one(side, prompt, timeout_s=DEBATE_TIMEOUT_S,
                                        resume=sessions.get(side), tools=[])
            deb.tokens_in += res.tokens_in
            deb.tokens_out += res.tokens_out
            if not res.text or res.error:
                deb.error = f"{side} {rnd}라운드 실패: {res.error or '빈 응답'}"
                log.warning("[%s] 토론 중단 — %s", analysis.ticker, deb.error)
                return deb
            sessions[side] = res.session_id or sessions.get(side, "")
            last[side] = res.text.strip()
            deb.turns.append((side, last[side]))
    log.info("[%s] 토론 %d라운드 완료 (발언 %d개)", analysis.ticker, rounds, len(deb.turns))
    return deb


async def _cli(ticker: str) -> None:
    import data_loader
    cand = await selector.fetch_single_candidate(ticker) or selector.Candidate(
        ticker=ticker, name="?", market="?", close=0, market_cap_billion=0,
        weekly_return=0, value_surge=0, foreign_delta=0, score=0)
    analysis = await agents.analyze_stock(cand, data_loader.load_context(ticker))
    deb = await run(cand, analysis)
    print(deb.transcript())
    print(f"\n토큰 in={deb.tokens_in} out={deb.tokens_out} error={deb.error}")


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python debate.py <ticker>")
        sys.exit(1)
    asyncio.run(_cli(sys.argv[1]))
