# 큐와 자동 복구

분석은 큐 기반으로 1종목씩 처리되며, 일시적 실패는 자동으로 복구된다.

## hourly 잡 (`scheduler.py`)

매시각 정각(`CronTrigger(minute=0)`)에 다음을 차례로 수행한다.

1. `reset_failed_to_pending()` — 직전 라운드 `failed` 종목을 `pending`으로 되살림
   (attempts 보존 → `next_queue_item` 정렬에서 자연히 뒤로 밀려 같은 종목 즉시 재시도 방지).
2. `enqueue_hot_picks(source="auto_hourly")` — selector 결과 핫 종목 5개를 큐에 추가.

큐 워커는 백그라운드에서 1개씩 처리한다. 보고서가 미생성된 `failed` 종목은 다음 정각까지 휴면한다.

## 부팅 시 복구 (`lifespan`)

프로세스 시작 시:

- `cleanup_stale_runs()` — 비정상 종료된 run을 `crashed`로 마킹
- `reset_stuck_queue()` — `processing`에 멈춘 큐를 `pending`으로 복귀
- `reset_failed_to_pending()` — 종료 시점에 `failed`로 남은 큐도 복귀 → 부팅 즉시 재처리

## 부실 보고서 복구 정책

일부 서브에이전트가 토큰 리미트 등 **일시적(retryable) 에러**로 실패해 결과가 부실한 보고서는
자동 복구 대상이다.

- **재추가 대상**: `RETRYABLE_ERROR_PATTERNS`(rate-limit, max_tokens, context_length_exceeded,
  timeout, 503/504 등)가 `sub_ratings`에 1개 이상인 보고서.
- **제외**: 영구 실패(invalid ticker, 데이터 없음) 또는 retryable이 아닌 일반 예외.
- **별점 0 보고서**(5개 sub 전부 실패)는 row까지 카스케이드 삭제(이력 노이즈 제거).
  부분 성공 보고서는 **보존**하고 큐만 재추가.
- **윈도우**: 최근 24시간(그 이전은 토큰 폭발 방지로 자동 복구 안 함).
- **중복 방지**: 같은 ticker가 이미 pending/processing이면 `add_to_queue`가 skip.
- **소스 보존**: 원래 보고서의 source를 그대로 큐에 재추가. 종목당 개별 큐 항목으로 처리.

구현 위치:

- `storage.purge_failed_reports(hours=24)` — 별점 0 보고서 카스케이드 삭제, 반환 ticker는 호출자가 재추가
- `storage.recover_failed_reports(hours=24)` — retryable 에러 보고서의 ticker를 큐에 재추가(옛 row 보존)

> weekly cron(예: `0 8 * * SAT`)은 폐기됨. 시간당 단일 잡으로 통합했고, 구
> `SCREENER_CRON` 환경변수는 더 이상 참조하지 않는다. 이유는 [런타임 가정](runtime-assumptions.md) 참고.

관련: [종목 선정](selector.md), [출처 추적](source-tracking.md)
