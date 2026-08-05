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
from datetime import datetime, timedelta, timezone as _dt_timezone

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

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
# ── 홈 토큰 keepalive ─────────────────────────────────────────────────────
# 갱신의 주 경로는 '만료 _KEEPALIVE_LEAD_S 전'에 발화하는 원샷 타이머다
# (_schedule_keepalive_timer). 정각 격자에만 의존하면 만료 시각이 정각 사이에
# 떨어질 때 갱신 기회를 통째로 놓친다 — CLI는 만료가 실제로 임박했을 때만 refresh를
# 발동하므로, 잔여가 넉넉한 시점의 ping은 성공해도 토큰이 그대로다.
# (2026-08-06 사고: 만료 05:38 → 05:00 정각 ping은 잔여 39분이라 refresh 미발동,
#  다음 기회인 06:00엔 이미 만료 → 리프레시 토큰 회전으로 자력 복구 불가)
_KEEPALIVE_LEAD_S = 6 * 60
# 타이머 발화 후 갱신이 확인될 때까지 이 간격으로 계속 재시도한다. CLI가 실제로
# refresh를 발동하는 임계를 알 수 없으므로 만료 직전까지 반복해 확실히 걸리게 한다.
_KEEPALIVE_TIGHT_GAP_S = 60
# 만료 직후 이 시간까지는 한 번 더 시도한다(시계 오차·직전 시도 실패 보정).
_KEEPALIVE_TAIL_S = 30
# 타이머를 놓쳐도(sleep/wake) 깨어난 뒤 실행되도록 두는 유예.
_KEEPALIVE_MISFIRE_GRACE_S = 3600

_sched: AsyncIOScheduler | None = None


async def _keepalive_push_until_refreshed() -> bool:
    """홈 토큰이 실제로 갱신될 때까지 짧은 간격으로 ping을 반복한다.

    ping 1회는 성공해도 CLI가 refresh를 발동하지 않으면 만료 시각이 그대로다 —
    그래서 '호출 성공'이 아니라 **만료 시각이 밀렸는지**를 성공 기준으로 삼고,
    밀릴 때까지(또는 만료될 때까지) 재시도한다. 반드시 홈을 갱신해야 대화형 세션과
    리프레시 토큰 회전 충돌이 나지 않는다(agents.keepalive_refresh가 env={}로 홈 사용).
    """
    before = config.home_credential_seconds_left()
    if before is None:
        return False
    if before <= 0:
        # 만료 후엔 리프레시 토큰이 회전돼 CLI가 자력으로 살리지 못한다(/login만이 답).
        log.warning("홈 토큰 이미 만료(%.0f분 경과) → keepalive로 복구 불가, /login 필요",
                    -before / 60)
        return False
    deadline = time.time() + before + _KEEPALIVE_TAIL_S
    attempt = 0
    while True:
        attempt += 1
        try:
            await agents.keepalive_refresh()
        except Exception:
            # 인증 실패는 SDK에서 'error result: success'로 위장돼 예외로 올라온다.
            log.warning("홈 토큰 keepalive 시도 %d 예외", attempt, exc_info=True)
        left = config.home_credential_seconds_left()
        if left is not None and left > before + 60:
            log.info("홈 토큰 keepalive 갱신 성공 (만료까지 %.1fh, %d회째)",
                     left / 3600, attempt)
            config.sdk_env()                    # 격리본에 즉시 반영
            return True
        if time.time() >= deadline or (left is not None and left <= 0):
            break
        await asyncio.sleep(_KEEPALIVE_TIGHT_GAP_S)
    log.warning("홈 토큰 keepalive %d회 후에도 갱신 안 됨 (리프레시 토큰 만료 의심)",
                attempt)
    return False


async def _keepalive_timer_fire():
    """만료 직전에 발화하는 원샷 잡. 갱신 성패와 무관하게 다음 타이머를 재예약한다."""
    left = config.home_credential_seconds_left()
    log.info("keepalive 타이머 발화 (만료까지 %s)",
             f"{left / 60:.0f}분" if left is not None else "미상")
    try:
        await _keepalive_push_until_refreshed()
    finally:
        _schedule_keepalive_timer()


def _schedule_keepalive_timer():
    """홈 토큰 만료 _KEEPALIVE_LEAD_S 전에 원샷 keepalive 잡을 건다(재호출 시 교체).

    만료 시각이 정각과 어떻게 어긋나도 갱신 기회를 반드시 갖게 하는 주 경로다.
    정각 점검·타이머 발화 때마다 다시 불려 만료 시각 변화(대화형 세션이 갱신한 경우
    등)에 재동기화된다. 이미 만료됐으면 걸지 않는다 — 자력 복구가 불가능해 재시도
    루프만 돌기 때문이고, /login 후 다음 정각 점검이 다시 타이머를 세운다."""
    if _sched is None:
        return
    try:
        left = config.home_credential_seconds_left()
    except Exception:
        return
    if left is None or left <= 0:
        return
    # LEAD 안쪽이면 곧바로(수 초 뒤) 발화 — 정각 잡을 블록하지 않도록 잡으로 넘긴다.
    run_at = datetime.now(_dt_timezone.utc) + timedelta(
        seconds=max(left - _KEEPALIVE_LEAD_S, 5.0))
    try:
        _sched.add_job(
            _keepalive_timer_fire,
            trigger=DateTrigger(run_date=run_at),
            id="keepalive_timer",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_KEEPALIVE_MISFIRE_GRACE_S,
        )
    except Exception:
        log.exception("keepalive 타이머 예약 실패")
        return
    log.info("keepalive 타이머 예약: %s (홈 토큰 만료 %.0f분 전)",
             run_at.astimezone().strftime("%m-%d %H:%M:%S"), _KEEPALIVE_LEAD_S / 60)


async def _check_credentials_and_alert():
    """서브프로세스가 쓸 OAuth 토큰을 홈에서 갱신·점검하고, 만료면 텔레그램으로
    알린다(dedup). 사용자가 원격 재로그인하면 다음 정각에 자동 복구된다."""
    _schedule_keepalive_timer()                # 만료 시각 기준 타이머 재동기화
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
    global _sched
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
    _sched = sched
    log.info("스케줄러 시작: hourly (every :00) TZ=%s", config.TIMEZONE)
    _schedule_keepalive_timer()                # 부팅 직후 만료 타이머 장전
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
        _sched = None


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
