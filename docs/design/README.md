# 설계 노트

auto_screener의 동작·정책 설계 문서. 코드만으로는 드러나지 않는 "왜 이렇게 했는가"를 기록한다.

- [종목 선정 정책 (selector)](selector.md) — 검색 상위 → dedup → 부족분만 시총 보강, 0건 허용
- [큐와 자동 복구](queue-and-recovery.md) — hourly 잡, 부팅 복구, 부실 보고서 재큐잉
- [이벤트 시간/시제 룰](event-time-rule.md) — time decay·시제·가격 반영 검증, 키워드 자동매칭 금지
- [출처 추적 원칙](source-tracking.md) — 수집 자료의 출처 보존(현재 한계 + 개선 TODO)
- [런타임 가정](runtime-assumptions.md) — 24/7 아닌 호스트 전제 → hourly + 부팅 복구

보안·비밀값 취급은 [../SECURITY.md](../SECURITY.md) 참고.
