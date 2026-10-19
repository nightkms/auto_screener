"""
S5: 트레이더 → 리스크 토론 → 포트폴리오 매니저 — TradingAgents의 마지막 세 단계.

1) 트레이더(Sonnet, prompts/trader.txt): 분석가 보고 + 강세/약세 토론 + 리서치 매니저(synthesizer)
   보고서·투자 계획을 받아 거래 제안. `FINAL TRANSACTION PROPOSAL: BUY|HOLD|SELL`.
2) 리스크 토론(Sonnet × 3): 공격적 → 보수적 → 중립적 순으로 RISK_DEBATE_ROUNDS 라운드 발언.
   각자 세션을 resume으로 이어가서 둘째 라운드부터는 다른 두 명의 직전 발언만 새로 받는다.
3) 포트폴리오 매니저(Opus, prompts/portfolio_manager.txt): 리스크 토론을 판정하고 트레이더 제안을
   승인·수정해 최종 `DECISION: BUY|HOLD|SELL`.

검색 도구는 주지 않는다(원작과 같이 앞 단계 보고로만 판단). 어느 단계든 실패하면 거기서 멈추고
결정 없이(decision="") 보고서를 그대로 내보낸다 — 분석 자체를 실패시키지 않는다.

CLI:
    python trading.py <보고서.md>    # 저장된 종합 보고서 1건으로 트레이더~최종 결정만 (저장·알림 없음)
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field

import agents
import config
import debate
import lessons
import selector

log = logging.getLogger("trading")

STEP_TIMEOUT_S = 300
DECISIONS = ("BUY", "HOLD", "SELL")
DECISION_KO = {"BUY": "매수", "HOLD": "보유", "SELL": "매도"}
_PROPOSAL_PAT = re.compile(r"FINAL TRANSACTION PROPOSAL:\s*\**\s*(BUY|HOLD|SELL)", re.I)
_DECISION_PAT = re.compile(r"DECISION:\s*\**\s*(BUY|HOLD|SELL)", re.I)

RISK_ROLES = (("risk_aggressive", "🔥 공격적"), ("risk_conservative", "🛡 보수적"),
              ("risk_neutral", "⚖️ 중립적"))


@dataclass
class Trading:
    trader_text: str = ""
    proposal: str = ""                                  # 트레이더 BUY/HOLD/SELL
    risk_turns: list[tuple[str, str]] = field(default_factory=list)   # (역할 라벨, 발언)
    pm_text: str = ""
    decision: str = ""                                  # 최종 BUY/HOLD/SELL, 실패면 ""
    sub_tokens: list[int] = field(default_factory=lambda: [0, 0])     # Sonnet in/out
    pm_tokens: list[int] = field(default_factory=lambda: [0, 0])      # Opus in/out
    error: str = ""

    def risk_transcript(self) -> str:
        return "\n\n".join(f"#### {label}\n{text}" for label, text in self.risk_turns)

    def sections(self) -> str:
        """보고서 끝에 붙일 섹션들. 리스크 토론 전문은 텔레그램에서 빠진다(notifier _SKIP_SECTIONS)."""
        parts = []
        if self.trader_text:
            parts.append(f"## 💼 트레이더 거래 제안\n{self.trader_text}")
        if self.risk_turns:
            parts.append(f"## 🗣 리스크 토론 전문\n{self.risk_transcript()}")
        if self.pm_text:
            # 포트폴리오 매니저 출력은 자체 `## ✅ 최종 결정` 제목으로 시작한다.
            body = self.pm_text
            parts.append(body if body.lstrip().startswith("## ✅") else f"## ✅ 최종 결정\n{body}")
        return "\n\n".join(parts)


def _strip_token(text: str, pat: re.Pattern) -> str:
    return "\n".join(ln for ln in text.splitlines() if not pat.search(ln)).strip()


async def _speak(name: str, prompt: str, out: Trading, resume: str | None = None,
                 model: str | None = None) -> agents.SubAgentResult | None:
    res = await agents._run_one(name, prompt, model=model, timeout_s=STEP_TIMEOUT_S,
                                resume=resume, tools=[])
    bucket = out.pm_tokens if model == config.CLAUDE_SYNTH_MODEL else out.sub_tokens
    bucket[0] += res.tokens_in
    bucket[1] += res.tokens_out
    if not res.text or res.error:
        out.error = f"{name} 실패: {res.error or '빈 응답'}"
        return None
    return res


async def run(candidate: selector.Candidate, analysis: agents.StockAnalysis,
              report_md: str, debate_text: str = "") -> Trading:
    out = Trading()
    base = debate._materials(candidate, analysis)
    if debate_text:
        base += f"\n## [debate] 강세/약세 연구원 토론\n{debate_text}\n"
    base += f"\n## [research_manager] 종합 보고서 · 투자 계획\n{report_md}\n"

    # 1) 트레이더
    memo = lessons.block("trader")
    res = await _speak("trader", base + (f"\n{memo}\n" if memo else "")
                       + "\n---\n위 자료로 거래 제안을 작성하라.", out)
    if not res:
        log.warning("[%s] %s", candidate.ticker, out.error)
        return out
    m = _PROPOSAL_PAT.search(res.text)
    out.proposal = m.group(1).upper() if m else ""
    out.trader_text = _strip_token(res.text, _PROPOSAL_PAT)
    trader_block = f"\n## [trader] 거래 제안 (판단: {out.proposal or '미상'})\n{out.trader_text}\n"

    # 2) 리스크 토론 — 공격 → 보수 → 중립, 라운드 반복
    sessions: dict[str, str] = {}
    latest: dict[str, str] = {}
    for rnd in range(1, max(config.RISK_DEBATE_ROUNDS, 0) + 1):
        for name, label in RISK_ROLES:
            others = "\n\n".join(f"### {lb} 분석가의 직전 발언\n{latest[n]}"
                                 for n, lb in RISK_ROLES if n != name and n in latest)
            if name not in sessions:
                prompt = (base + trader_block + f"\n---\n리스크 토론 {rnd}라운드 발언을 하라."
                          + (f"\n\n{others}" if others else ""))
            else:
                prompt = (f"리스크 토론 {rnd}라운드다. 다른 분석가들의 직전 발언에 반박하며 같은 "
                          f"출력 형식으로 발언하라.\n\n{others}")
            r = await _speak(name, prompt, out, resume=sessions.get(name))
            if not r:
                log.warning("[%s] %s → 리스크 토론 중단, 지금까지 발언으로 판정", candidate.ticker, out.error)
                break
            sessions[name] = r.session_id or sessions.get(name, "")
            latest[name] = r.text.strip()
            out.risk_turns.append((f"{label} 분석가 — {rnd}라운드", latest[name]))
        else:
            continue
        break

    # 3) 포트폴리오 매니저 (Opus)
    memo = lessons.block("portfolio_manager")
    pm_prompt = (base + trader_block
                 + f"\n## [risk_debate] 리스크 토론 기록\n{out.risk_transcript() or '(토론 없음)'}\n"
                 + (f"\n{memo}\n" if memo else "")
                 + "\n---\n최종 결정을 내려라.")
    res = await _speak("portfolio_manager", pm_prompt, out, model=config.CLAUDE_SYNTH_MODEL)
    if not res:
        log.warning("[%s] %s → 최종 결정 없음", candidate.ticker, out.error)
        return out
    m = _DECISION_PAT.search(res.text)
    out.decision = m.group(1).upper() if m else ""
    out.pm_text = _strip_token(res.text, _DECISION_PAT)
    if not out.decision:
        out.error = "최종 결정 토큰(DECISION:) 없음"
    log.info("[%s] 트레이더 %s → 최종 %s (리스크 발언 %d)", candidate.ticker,
             out.proposal or "?", out.decision or "?", len(out.risk_turns))
    return out


async def _cli(path: str) -> None:
    from pathlib import Path
    md = Path(path).read_text(encoding="utf-8")
    m = re.search(r"_(\d{6})\.md$", path)
    ticker = m.group(1) if m else "?"
    cand = selector.Candidate(ticker=ticker, name="?", market="?", close=0,
                              market_cap_billion=0, weekly_return=0,
                              value_surge=0, foreign_delta=0, score=0)
    analysis = agents.StockAnalysis(ticker=ticker, name=Path(path).stem)
    t = await run(cand, analysis, md)
    print(t.sections())
    print(f"\n제안={t.proposal} 결정={t.decision} Sonnet={t.sub_tokens} Opus={t.pm_tokens} err={t.error}")


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python trading.py <보고서.md>")
        sys.exit(1)
    asyncio.run(_cli(sys.argv[1]))
