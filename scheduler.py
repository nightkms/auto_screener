"""
S9: 상주 프로세스 entry point.

FastAPI 대시보드 + APScheduler를 한 프로세스에 띄운다.
매시각 정각에 다음 2단계를 차례로 수행:
  1) reset_failed_to_pending — 직전 라운드 'failed' 종목을 'pending'으로 되살림
     (attempts 보존 → next_queue_item 정렬에서 자연히 뒤로)
  2) enqueue_hot_picks(source='auto_hourly') — 핫 종목 5개 큐에 추가

큐 워커는 백그라운드에서 1개씩 처리한다. 보고서가 미생성된 'failed' 종목은
다음 정각까지 휴면 → 같은 종목을 즉시 재시도하지 않음.

실행:
    python scheduler.py
"""
from __future__ import annotations

import _silence_console  # noqa: F401  # 자식 콘솔 창 숨김 (첫 import)


import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import dashboard
import pipeline
import storage

log = logging.getLogger("scheduler")


async def _scheduled_run():
    """매시각 정각: failed 큐 복귀 → 핫 종목 enqueue."""
    # 일시정지 중이면 큐를 건드리지 않는다 (복귀·신규 enqueue 모두 스킵 →
    # 재개 시 큐가 폭증하지 않게). 재시작해도 paused는 영속 유지된다.
    if storage.is_queue_paused():
        log.info("스케줄 트리거 스킵: 큐 일시정지 상태")
        return
    log.info("스케줄 트리거: hourly reset + enqueue")
    try:
        rn = storage.reset_failed_to_pending()
        if rn:
            log.info("스케줄: failed %d건을 'pending'으로 복귀 (재시도 라운드)", rn)
    except Exception:
        log.exception("스케줄: reset_failed_to_pending 실패")
    try:
        added, _ = await pipeline.enqueue_hot_picks(
            config.TOP_N, source="auto_hourly",
        )
        log.info("스케줄: 핫 %d 종목 큐 추가됨 (워커가 1개씩 처리)", added)
    except Exception:
        log.exception("스케줄: enqueue_hot_picks 실패")


@asynccontextmanager
async def lifespan(app):
    storage.init_db()
    n = storage.cleanup_stale_runs()
    if n:
        log.info("이전 비정상 종료 run %d건을 'crashed'로 마킹", n)
    qn = storage.reset_stuck_queue()
    if qn:
        log.info("큐 stuck %d건을 'pending'으로 복귀 (재시도 예정)", qn)
    # 직전 종료 시점에 'failed'로 남아 있던 큐도 같이 복귀 → 부팅 즉시 재처리.
    rn = storage.reset_failed_to_pending()
    if rn:
        log.info("부팅: failed %d건을 'pending'으로 복귀", rn)
    sched = AsyncIOScheduler(timezone=config.TIMEZONE)
    # 매시각 정각: 부실 보고서 정리 + 자동 복구 + 핫 종목 큐 추가.
    # config.CRON은 더 이상 사용하지 않음 (시간당 단일 잡으로 통합).
    sched.add_job(
        _scheduled_run,
        trigger=CronTrigger(minute=0, timezone=config.TIMEZONE),
        id="hourly_pipeline",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=1800,
    )
    sched.start()
    log.info("스케줄러 시작: hourly (every :00) TZ=%s", config.TIMEZONE)
    # 백그라운드 워커들 (큐 + 가격 알림)
    queue_task = asyncio.create_task(dashboard.queue_worker())
    price_task = asyncio.create_task(dashboard.price_watch_worker())
    try:
        yield
    finally:
        for t in (queue_task, price_task):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        sched.shutdown()


# dashboard.app에 lifespan 부착 (단일 ASGI app으로 노출)
dashboard.app.router.lifespan_context = lifespan


def main():
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s %(name)s %(message)s",
    )
    uvicorn.run(
        "scheduler:dashboard.app",
        host="127.0.0.1",
        port=config.DASHBOARD_PORT,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
