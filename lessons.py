"""
회고 교훈 메모리 — TradingAgents의 reflection 메모리를 경량화한 것.

원작은 거래마다 실현 수익률로 각 에이전트(bull/bear/판정자 등)가 반성해 교훈을 벡터 DB에
쌓고, 다음 결정 때 비슷한 상황의 교훈을 꺼내 쓴다. 여기서는
- 쌓기: retrospect.py가 주말마다 등급 vs 이후 수익률 사례에서 역할별 교훈을 뽑아 `lesson` 테이블에 저장
- 꺼내기: 임베딩 검색 대신 역할별 활성 교훈을 전부(상한 MAX_ACTIVE_PER_ROLE) 주입하고,
  "지금 종목 상황에 해당하는 것만 적용하라"고 LLM에 맡긴다 (교훈 수가 적어 이걸로 충분).
"""
from __future__ import annotations

import storage

# 원작의 메모리 5종: bull / bear / trader / invest_judge(=synthesizer) / risk_manager(=portfolio_manager)
ROLES = ("bull", "bear", "synthesizer", "trader", "portfolio_manager")
MAX_ACTIVE_PER_ROLE = 10         # 넘치면 오래된 것부터 비활성 (프롬프트 비대화 방지)


def block(role: str) -> str:
    """분석 프롬프트에 붙일 '과거 회고 교훈' 블록. 교훈이 없으면 빈 문자열."""
    try:
        rows = storage.active_lessons(role)[:MAX_ACTIVE_PER_ROLE]
    except Exception:
        return ""
    if not rows:
        return ""
    lines = ["## 📚 과거 회고 교훈 (이전 분석의 등급과 이후 실제 흐름을 비교해 얻은 것)",
             "_지금 종목이 '상황'에 해당할 때만 적용한다. 해당하지 않으면 무시하라. "
             "교훈을 적용했다면 그 판단 근거 문장에 `[교훈#번호]`를 붙여라._", ""]
    for r in rows:
        lines.append(f"- [교훈#{r['id']}] 상황: {r['situation']}\n  → {r['lesson']}")
    return "\n".join(lines)


def enforce_cap(role: str) -> int:
    """역할별 활성 교훈이 상한을 넘으면 오래된 것부터 비활성. 비활성 건수 반환."""
    rows = storage.active_lessons(role)          # 최신 우선
    for r in rows[MAX_ACTIVE_PER_ROLE:]:
        storage.retire_lesson(r["id"], f"상한 {MAX_ACTIVE_PER_ROLE}개 초과(오래된 순)")
    return max(0, len(rows) - MAX_ACTIVE_PER_ROLE)
