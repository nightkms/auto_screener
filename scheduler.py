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
import time
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import agents
import config
import dashboard
import notifier
import pipeline
import storage

log = logging.getLogger("scheduler")

# 인증 만료 알림: 만료가 지속되는 동안 이 간격으로만 재알림(스팸 방지).
_AUTH_ALERT_KEY = "auth_alert_sent_at"
_AUTH_ALERT_GAP_S = 6 * 3600
# 홈 토큰 만료 이 시간 전부터 keepalive 선제 갱신 시도(>1h: 매시각 점검이 한 번은 잡음).
_KEEPALIVE_THRESHOLD_S = 75 * 60


async def _keepalive_refresh_home_if_needed():
    """홈 OAuth 토큰이 곧 만료(또는 이미 만료)면 claude를 직접 1회 돌려 갱신한다.
    성공하면 무인 상태에서도 인증이 자동 연장된다(다음 copy로 격리본 반영). 실패하면
    이어지는 만료 점검이 텔레그램으로 알린다. 반드시 홈을 갱신해 대화형 세션과
    리프레시 토큰 회전 충돌을 피한다(agents.keepalive_refresh가 env={}로 홈 사용)."""
    try:
        home_left = config.home_credential_seconds_left()
    except Exception:
        return
    if home_left is None or home_left > _KEEPALIVE_THRESHOLD_S:
        return
    log.info("홈 토큰 만료 임박(%.0f분 남음) → keepalive 갱신 시도", home_left / 60)
    try:
        await agents.keepalive_refresh()
        new_left = config.home_credential_seconds_left()
        if new_left is not None and new_left > home_left + 60:
            log.info("홈 토큰 keepalive 갱신 성공 (만료까지 %.1fh)", new_left / 3600)
        else:
            log.warning("홈 토큰 keepalive 후에도 갱신 안 됨 (리프레시 토큰 만료 의심)")
    except Exception:
        log.exception("홈 토큰 keepalive 갱신 실패")


async def _check_credentials_and_alert():
    """서브프로세스가 쓸 OAuth 토큰을 홈에서 갱신·점검하고, 만료면 텔레그램으로
    알린다(dedup). 사용자가 원격 재로그인하면 다음 정각에 자동 복구된다."""
    await _keepalive_refresh_home_if_needed()   # 만료 임박 시 홈 토큰 선제 자가 갱신
    try:
        config.sdk_env()                       # 홈의 신선본을 격리본으로 끌어옴
        left = config.credential_seconds_left()
    except Exception:
        log.exception("인증 점검 실패")
        return
    if left is None:
        return
    if left <= 0:
        try:
            last_t = float(storage.get_state(_AUTH_ALERT_KEY) or "0")
        except ValueError:
            last_t = 0.0
        if time.time() - last_t >= _AUTH_ALERT_GAP_S:
            log.warning("Claude 인증 토큰 만료 감지 → 텔레그램 알림")
            try:
                await notifier.notify_auth_expired(
                    "만료 시각이 지났고 홈 토큰도 갱신되지 않았습니다."
                )
            except Exception:
                log.exception("인증 만료 알림 전송 실패")
            storage.set_state(_AUTH_ALERT_KEY, str(time.time()))
    elif storage.get_state(_AUTH_ALERT_KEY):
        storage.set_state(_AUTH_ALERT_KEY, "")  # 복구 → 다음 만료 때 즉시 알림


async def _scheduled_run():
    """매시각 정각: 인증 점검 → failed 큐 복귀 → 핫 종목 enqueue."""
    await _check_credentials_and_alert()       # 토큰 갱신·만료 알림 (paused와 무관)
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
    await _check_credentials_and_alert()       # 부팅 즉시 인증 상태 점검
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
