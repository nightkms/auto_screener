"""
대시보드 보고서 페이지의 Q&A.

이미 작성된 보고서 마크다운을 컨텍스트로 사용자 추가 질문에 답한다.
claude_agent_sdk → Claude Code CLI 서브프로세스 → Max 구독 한도 안에서 동작.

dashboard.py에서 POST /report/{id}/ask 가 호출.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

import config
import storage

log = logging.getLogger("report_chat")

MAX_TURNS = 14  # 검색·페치를 여러 번 도는 무거운 질문도 8턴이면 답을 못 내고 끊겼음
MAX_HISTORY_TURNS = 12  # 직전 Q&A를 컨텍스트로 몇 쌍까지 넣을지


@dataclass
class ChatResult:
    answer: str
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_s: float = 0.0
    sources: list[dict] = field(default_factory=list)
    error: str = ""


def _load_system_prompt() -> str:
    return (config.PROMPTS_DIR / "report_chat.txt").read_text(encoding="utf-8")


def _build_user_message(report: dict, report_md: str,
                        history: list[dict], question: str) -> str:
    """Claude에 넘길 단일 사용자 메시지. 컨텍스트 전체를 한 번에 패킹."""
    lines: list[str] = []
    lines.append("# 분석 대상")
    lines.append(f"- 종목명: {report.get('name','?')}")
    lines.append(f"- 종목코드: {report.get('ticker','?')}")
    lines.append(f"- 등급: {report.get('grade','?')} (★{report.get('avg_rating','?')})")
    lines.append("")
    lines.append("# 기존 분석 보고서 (마크다운 원본)")
    lines.append(report_md or "(보고서 파일을 찾을 수 없음)")
    lines.append("")

    if history:
        lines.append("# 이전 Q&A 이력 (시간순)")
        # 너무 길어지지 않게 최근 N쌍만
        recent = history[-MAX_HISTORY_TURNS * 2:]
        for h in recent:
            role_kr = "Q" if h["role"] == "user" else "A"
            lines.append(f"## {role_kr} ({h['created_at']})")
            lines.append(h["content"])
            lines.append("")

    lines.append("# 사용자의 새 질문")
    lines.append(question.strip())
    lines.append("")
    lines.append("위 보고서·이력을 기반으로 답하되, 필요하면 WebSearch/WebFetch로 보강하라. "
                 "도구 결과는 답변 끝 '**출처**' 섹션에 URL과 함께 명시할 것.")
    return "\n".join(lines)


def _extract_sources_from_tool_use(block: ToolUseBlock) -> list[dict]:
    """ToolUseBlock에서 출처 정보 추출.
    - WebSearch: input의 query
    - WebFetch: input의 url
    실제 결과 본문은 ToolResultBlock에 오지만, query/URL만 있어도 사후 추적 가능."""
    name = getattr(block, "name", "")
    inp = getattr(block, "input", {}) or {}
    if name == "WebSearch":
        return [{"tool": "WebSearch", "query": inp.get("query", "")}]
    if name == "WebFetch":
        return [{"tool": "WebFetch", "url": inp.get("url", "")}]
    return []


async def ask(report_id: int, question: str) -> ChatResult:
    """보고서 1개에 대한 사용자 질문 처리.

    1. DB에서 보고서 row + 이력 로드
    2. md 파일 읽기
    3. claude_agent_sdk로 Sonnet 호출 (WebSearch/WebFetch 허용)
    4. 답변 + 사용된 도구 출처 수집
    5. DB에 user/assistant 두 행 저장
    """
    storage.init_db()
    # 보고서 메타
    with storage._connect() as c:
        row = c.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if not row:
        return ChatResult(answer="", error=f"report {report_id} not found")
    report = dict(row)
    md_path = config.resolve_report_md(report["md_path"]) if report.get("md_path") else None
    report_md = ""
    if md_path and md_path.exists():
        report_md = md_path.read_text(encoding="utf-8")

    history = storage.list_qa_messages(report_id)
    user_msg = _build_user_message(report, report_md, history, question)
    system = _load_system_prompt()

    opts = ClaudeAgentOptions(
        system_prompt=system,
        model=config.CLAUDE_SUB_MODEL,  # Sonnet 4.6
        permission_mode="bypassPermissions",
        allowed_tools=["WebSearch", "WebFetch"],
        max_turns=MAX_TURNS,
    )

    started = time.time()
    pieces: list[str] = []
    sources: list[dict] = []
    tokens_in = tokens_out = 0
    err = ""
    try:
        async for msg in query(prompt=user_msg, options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        pieces.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        sources.extend(_extract_sources_from_tool_use(block))
            elif isinstance(msg, ResultMessage):
                usage = getattr(msg, "usage", None)
                if usage:
                    tokens_in = (usage.get("input_tokens") or 0) + \
                                (usage.get("cache_read_input_tokens") or 0)
                    tokens_out = usage.get("output_tokens") or 0
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log.exception("[report_chat %d] 실패", report_id)

    answer = "\n".join(pieces).strip()
    elapsed = round(time.time() - started, 1)

    # max_turns 초과로 예외가 나도, 그때까지 모은 텍스트가 있으면 부분 답변으로 살린다.
    # (없으면 빈 답이 저장 안 돼 사용자에게 아무 응답도 안 보이던 문제 보완)
    if err and "maximum number of turns" in err.lower():
        note = (f"\n\n---\n*⚠️ 검색이 길어져 {MAX_TURNS}턴 한도에서 중단됐습니다. "
                f"위 내용은 부분 답변일 수 있으니, 질문을 더 좁혀 다시 물어봐 주세요.*")
        if answer:
            answer += note
        else:
            answer = (f"검색·자료 수집이 {MAX_TURNS}턴 한도를 넘겨 답변을 완성하지 못했습니다. "
                      f"질문을 더 구체적으로 좁혀서 다시 시도해 주세요.")

    # 성공/실패 무관 user 질문은 기록 (실패해도 재시도 가능하게)
    storage.save_qa_message(report_id, "user", question, elapsed_s=0)
    if answer:
        storage.save_qa_message(
            report_id, "assistant", answer,
            tokens_in=tokens_in, tokens_out=tokens_out,
            elapsed_s=elapsed, sources=sources,
        )
        storage.log_usage(None, config.CLAUDE_SUB_MODEL,
                          tokens_in, tokens_out,
                          context=f"chat:report:{report_id}")

    return ChatResult(
        answer=answer, tokens_in=tokens_in, tokens_out=tokens_out,
        elapsed_s=elapsed, sources=sources, error=err,
    )


# ---------------------------------------------------------------------------
# CLI 테스트
# ---------------------------------------------------------------------------
async def _cli(report_id: int, question: str) -> None:
    res = await ask(report_id, question)
    print(f"=== answer ({res.elapsed_s}s, {res.tokens_in}/{res.tokens_out} tokens) ===")
    print(res.answer)
    if res.sources:
        print("\n--- sources ---")
        for s in res.sources:
            print(s)
    if res.error:
        print("\nERROR:", res.error)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python report_chat.py <report_id> <question>")
        sys.exit(1)
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(_cli(int(sys.argv[1]), sys.argv[2]))
