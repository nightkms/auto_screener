"""
S8: FastAPI 대시보드.

- /            전체 실행 이력 + 토큰 누계
- /run/{id}    선정 후보 + 분석 결과 + 서브에이전트 점수
- /report/{id} 종목 보고서 마크다운 뷰
- POST /trigger  수동으로 pipeline.run_once() 즉시 실행 (별도 task)

scheduler.py가 이 앱과 같은 프로세스에서 BackgroundScheduler를 띄운다.
단독으로도 실행 가능:
    python dashboard.py
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import config
import data_loader
import pipeline
import report_chat
import storage

log = logging.getLogger("dashboard")

app = FastAPI(title="AutoScreener")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# /trigger 중복 실행 방지
_trigger_running = False


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------
def _enrich_run(r: dict) -> dict:
    """run row에 best_grade 추가."""
    reps = storage.reports_for_run(r["id"])
    grades = [x["grade"] for x in reps if x["grade"]]
    rank = {"STRONG": 3, "WATCH": 2, "INTEREST": 1, "SKIP": 0}
    best = max(grades, key=lambda g: rank.get(g, -1)) if grades else None
    return {**r, "best_grade": best}


def _enrich_report(r: dict) -> dict:
    """report row에 sub_ratings 펼침."""
    import sqlite3
    with storage._connect() as c:
        rows = c.execute(
            "SELECT sub_name, rating FROM sub_ratings WHERE report_id=?",
            (r["id"],),
        ).fetchall()
    sub = {row["sub_name"]: row["rating"] for row in rows}
    return {**r, "sub": sub}


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    storage.init_db()
    runs = [_enrich_run(r) for r in storage.recent_runs(limit=30)]
    all_reps: list[dict] = []
    for r in runs:
        all_reps.extend(storage.reports_for_run(r["id"]))
    usage = storage.usage_summary(run_ids=[r["id"] for r in runs])
    total_in = sum(v["in"] or 0 for v in usage.values())
    total_out = sum(v["out"] or 0 for v in usage.values())
    stats = {
        "runs": len(runs),
        "strong": sum(1 for r in all_reps if r["grade"] == "STRONG"),
        "watch": sum(1 for r in all_reps if r["grade"] == "WATCH"),
        "tokens_total": f"{(total_in + total_out):,}",
    }
    strong = storage.strong_reports(limit=30)
    queue_active = storage.queue_items(
        statuses=("pending", "processing", "failed"), limit=30)

    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    per_page = 10

    # 등급 필터 (?grade=STRONG&grade=WATCH...). 비어있으면 전체.
    VALID_GRADES = ("STRONG", "WATCH", "INTEREST", "SKIP")
    grade_filter = tuple(
        g for g in request.query_params.getlist("grade")
        if g in VALID_GRADES
    )
    # 종목명/코드 검색 (?q=삼성). 부분 일치.
    q_raw = request.query_params.get("q", "").strip()
    recent_items, recent_total = storage.recent_reports_paged(
        page=page, per_page=per_page,
        grades=grade_filter if grade_filter else None,
        q=q_raw or None,
    )
    recent_total_pages = max(1, (recent_total + per_page - 1) // per_page)

    return templates.TemplateResponse(
        request, "index.html",
        {"runs": runs[:5], "stats": stats,
         "strong_reports": strong,
         "recent_items": recent_items,
         "recent_page": page,
         "recent_total_pages": recent_total_pages,
         "recent_total": recent_total,
         "grade_filter": list(grade_filter),
         "all_grades": list(VALID_GRADES),
         "search_q": q_raw,
         "queue_items": queue_active,
         "is_running": _trigger_running,
         "busy_flash": request.query_params.get("busy") == "1",
         "started_flash": request.query_params.get("started") == "1",
         "queued_flash": request.query_params.get("queued"),
         "dup_flash": request.query_params.get("dup"),
         "hot_flash": request.query_params.get("hot"),
         "err_flash": request.query_params.get("err")},
    )


@app.get("/api/search")
async def api_search(q: str = "") -> list[dict]:
    """종목 검색 (자동완성용). 종목명 또는 6자리 코드로 부분 일치."""
    return data_loader.search_stocks(q, limit=20)


@app.post("/queue/add")
async def queue_add(ticker: str = Form(...), name: str = Form("")):
    ticker = ticker.strip()
    if not (ticker.isdigit() and len(ticker) == 6):
        return RedirectResponse(url=f"/?dup={ticker}", status_code=303)
    # 대시보드에서 검색 → 추가하는 경로는 source='manual' (등급 무관 알림)
    added = storage.add_to_queue(ticker, name=name.strip(),
                                 source="manual", pick_source="manual")
    flash = "queued" if added else "dup"
    return RedirectResponse(
        url=f"/?{flash}={ticker}", status_code=303)


@app.post("/queue/{qid}/delete")
async def queue_delete(qid: int):
    storage.remove_queue_item(qid)
    return RedirectResponse(url="/", status_code=303)


@app.post("/report/{report_id}/delete")
async def report_delete(report_id: int, request: Request):
    """보고서 카스케이드 삭제. 별점 0 부실 보고서가 분석 목록에 남아
    selector dedup(`recently_analyzed_tickers`)에 걸려 자동 재분석이
    안 되는 케이스를 사용자가 직접 청소할 수 있게 함."""
    info = storage.delete_report(report_id)
    if not info:
        raise HTTPException(404, "Report not found")
    next_url = request.query_params.get("next") or "/"
    if not next_url.startswith("/"):
        next_url = "/"
    return RedirectResponse(url=next_url, status_code=303)


@app.get("/run/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int):
    runs = storage.recent_runs(limit=200)
    run = next((r for r in runs if r["id"] == run_id), None)
    if not run:
        raise HTTPException(404, "Run not found")
    reports = [_enrich_report(r) for r in storage.reports_for_run(run_id)]
    candidates = storage.candidates_for_run(run_id)
    return templates.TemplateResponse(
        request, "run.html",
        {"run": run, "reports": reports, "candidates": candidates},
    )


@app.get("/ticker/{ticker}", response_class=HTMLResponse)
async def ticker_history(request: Request, ticker: str):
    """동일 종목의 보고서 이력 — 등급/★/서브 별점이 어떻게 변했는지 추적."""
    ticker = ticker.strip()
    if not (ticker.isdigit() and len(ticker) == 6):
        raise HTTPException(400, "ticker must be 6 digits")
    reports = storage.reports_for_ticker(ticker, limit=100)
    name = reports[0]["name"] if reports else ticker
    # 모든 보고서에 등장한 sub_name 합집합 (컬럼 헤더용, 정렬은 표준 순서)
    SUB_ORDER = ["valuation", "industry", "price_flow", "catalyst", "risk"]
    seen = {s for r in reports for s in r["sub"].keys()}
    sub_names = [s for s in SUB_ORDER if s in seen] + \
                sorted(s for s in seen if s not in SUB_ORDER)
    rank = {"STRONG": 3, "WATCH": 2, "INTEREST": 1, "SKIP": 0}
    grades = [r["grade"] for r in reports if r.get("grade")]
    best_grade = max(grades, key=lambda g: rank.get(g, -1)) if grades else None
    ratings = [r["avg_rating"] for r in reports if r.get("avg_rating") is not None]
    return templates.TemplateResponse(
        request, "ticker.html",
        {"ticker": ticker, "name": name, "reports": reports,
         "sub_names": sub_names,
         "stats": {
             "count": len(reports),
             "best_grade": best_grade,
             "max_rating": max(ratings) if ratings else None,
             "latest_rating": ratings[0] if ratings else None,
         }},
    )


@app.get("/report/{report_id}", response_class=HTMLResponse)
async def report_view(request: Request, report_id: int):
    with storage._connect() as c:
        row = c.execute(
            "SELECT * FROM reports WHERE id=?", (report_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Report not found")
    rep = dict(row)
    md = ""
    if rep.get("md_path") and Path(rep["md_path"]).exists():
        md = Path(rep["md_path"]).read_text(encoding="utf-8")
    qa_history = storage.list_qa_messages(report_id)
    return templates.TemplateResponse(
        request, "report.html",
        {"report": rep, "markdown": md, "qa_history": qa_history},
    )


@app.get("/report/{report_id}/qa")
async def report_qa_list(report_id: int) -> JSONResponse:
    """이 보고서의 Q&A 이력을 JSON으로. UI 리프레시·디버깅용."""
    with storage._connect() as c:
        exists = c.execute("SELECT 1 FROM reports WHERE id=?", (report_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "Report not found")
    return JSONResponse({"history": storage.list_qa_messages(report_id)})


@app.post("/report/{report_id}/ask")
async def report_ask(report_id: int, payload: dict = Body(...)) -> JSONResponse:
    """보고서에 추가 질문. Claude Sonnet + WebSearch 허용.
    Request: {"question": "..."}
    Response: {"answer", "tokens_in", "tokens_out", "elapsed_s", "sources", "error"}"""
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question is empty")
    if len(question) > 4000:
        raise HTTPException(400, "question too long (>4000 chars)")
    with storage._connect() as c:
        exists = c.execute("SELECT 1 FROM reports WHERE id=?", (report_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "Report not found")

    res = await report_chat.ask(report_id, question)
    if res.error and not res.answer:
        return JSONResponse(
            {"answer": "", "error": res.error,
             "tokens_in": 0, "tokens_out": 0, "elapsed_s": res.elapsed_s,
             "sources": []},
            status_code=500,
        )
    return JSONResponse({
        "answer": res.answer,
        "tokens_in": res.tokens_in,
        "tokens_out": res.tokens_out,
        "elapsed_s": res.elapsed_s,
        "sources": res.sources,
        "error": res.error,
    })


def is_trigger_running() -> bool:
    return _trigger_running


async def _guarded_run(top_n: int):
    global _trigger_running
    _trigger_running = True
    try:
        await pipeline.run_once(top_n=top_n, dry_run=False)
    finally:
        _trigger_running = False


async def _guarded_run_ticker(ticker: str, name: str = "",
                               source: str = "manual"):
    global _trigger_running
    _trigger_running = True
    try:
        return await pipeline.run_for_ticker(
            ticker, name=name, dry_run=False, source=source,
        )
    finally:
        _trigger_running = False


PRICE_ALERT_THRESHOLD_PCT = 5.0          # 기준가 대비 ±N% 변동 시 알림
PRICE_WATCH_TTL_DAYS = 14                # 자동 만료
PRICE_CHECK_HOUR = 18                     # 장 마감(시간외 단일가 포함)이라 그날 종가 확정 시각
PRICE_CHECK_POLL_SEC = 3600              # 게이트 평가 주기(실제 체크는 평일 1회)


async def _fetch_current_price(ticker: str) -> float | None:
    import aiohttp
    url = f"https://m.stock.naver.com/api/stock/{ticker}/basic"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
            async with s.get(url, timeout=10) as r:
                if r.status != 200:
                    return None
                j = await r.json()
                price = j.get("closePrice")
                if isinstance(price, str):
                    price = price.replace(",", "")
                return float(price) if price else None
    except Exception:
        return None


async def price_watch_worker():
    """STRONG/WATCH 종목의 기준가 대비 ±5% 변동 시 텔레그램 알림.

    주가는 평일 장 마감(시간외 단일가 포함, ~18시) 후에야 그날 종가가 확정되므로
    **평일 하루 1회, 18시 이후**에만 체크한다. 매시간 polling은 불필요.

    고정 시각 cron은 쓰지 않는다(호스트가 18시에 sleep이면 그 잡을 놓침,
    runtime-assumptions 참고) — 대신 주기적으로 깨어나 "평일 & 18시 이후 &
    오늘 아직 안 함"을 게이트로 검사한다. 18시에 꺼져 있다 늦게 깨어나도 그날
    첫 평가에서 실행되므로 sleep/wake에 안전하다.
    알림 발생 시 last_alert_pct 갱신해 같은 임계 반복 방지."""
    import notifier
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(config.TIMEZONE)
    log.info("price_watch_worker 시작 (평일 %d시 이후 1회)", PRICE_CHECK_HOUR)
    last_run_date = None
    while True:
        try:
            now = datetime.now(tz)
            # 게이트: 평일(월~금) & 마감 이후 & 오늘 아직 안 함
            if not (now.weekday() < 5
                    and now.hour >= PRICE_CHECK_HOUR
                    and last_run_date != now.date()):
                await asyncio.sleep(PRICE_CHECK_POLL_SEC)
                continue
            log.info("주가 워치 점검 (%s)", now.date())
            expired = storage.expire_old_watches(days=PRICE_WATCH_TTL_DAYS)
            if expired:
                log.info("주가 워치 %d건 자동 만료 (>%d일)", expired, PRICE_WATCH_TTL_DAYS)
            watches = storage.active_watches()
            for w in watches:
                # 등급 가드: WATCH 이상(STRONG/WATCH)만 주가 알림.
                # 과거 등록된 INTEREST/SKIP active=1 워치가 남아있을 수 있어
                # storage.add_price_watch와 별개로 워커에서 한 번 더 거른다.
                if w.get("base_grade") not in ("STRONG", "WATCH"):
                    storage.update_watch(w["id"])
                    continue
                price = await _fetch_current_price(w["ticker"])
                if not price or not w["base_price"]:
                    continue
                base = float(w["base_price"])
                pct = (price - base) / base * 100
                last = w.get("last_alert_pct") or 0
                # ±5%를 새로 넘었을 때만 알림 (반복 방지)
                threshold = PRICE_ALERT_THRESHOLD_PCT
                if abs(pct) >= threshold and abs(pct - last) >= threshold:
                    try:
                        await notifier.notify_price_alert(
                            ticker=w["ticker"], name=w["name"] or w["ticker"],
                            base_price=base, current_price=price,
                            change_pct=pct, base_grade=w.get("base_grade"),
                            base_date=w.get("base_date"),
                        )
                        storage.update_watch(w["id"], last_alert_pct=pct)
                    except Exception as e:
                        log.warning("price alert send fail: %s", e)
                else:
                    storage.update_watch(w["id"])
            last_run_date = now.date()       # 오늘 체크 완료 표시
            await asyncio.sleep(PRICE_CHECK_POLL_SEC)
        except asyncio.CancelledError:
            log.info("price_watch_worker 취소됨")
            raise
        except Exception:
            log.exception("price_watch_worker 루프 예외")
            await asyncio.sleep(600)


async def queue_worker():
    """큐를 처리하는 백그라운드 워커. lifespan에서 띄움.
    분석 락이 잡혀있으면 대기, 풀리면 큐에서 다음 항목 꺼내 실행.

    실패 정책 (영구/일시 구분 없음):
    - 어떤 실패든 mark_queue_failed_retry → status='failed' + attempts++
    - 'failed'는 next_queue_item이 안 잡음 → 다음 정각 reset_failed_to_pending까지 휴면
    - 부팅 시 reset_stuck_queue가 processing→pending 자동 복구."""
    log.info("queue_worker 시작")
    while True:
        try:
            if _trigger_running:
                await asyncio.sleep(5)
                continue
            item = storage.next_queue_item()
            if not item:
                await asyncio.sleep(10)
                continue
            qid = item["id"]
            ticker = item["ticker"]
            name = item.get("name") or ""
            source = item.get("source") or "manual"
            log.info("queue: 처리 시작 qid=%d %s(%s) src=%s attempts=%d",
                     qid, name, ticker, source, item["attempts"])
            storage.mark_queue_processing(qid)
            try:
                run_id = await _guarded_run_ticker(
                    ticker, name=name, source=source,
                )
                storage.mark_queue_done(qid, run_id=run_id)
                log.info("queue: 완료 qid=%d run_id=%d", qid, run_id)
            except pipeline.RetryableAnalysisError as e:
                log.warning("queue: 실패 qid=%d: %s", qid, e)
                storage.mark_queue_failed_retry(qid, str(e))
                await asyncio.sleep(30)
            except Exception as e:
                err_str = f"{type(e).__name__}: {e}"
                log.exception("queue: 실패 qid=%d: %s", qid, e)
                storage.mark_queue_failed_retry(qid, err_str)
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            log.info("queue_worker 취소됨")
            raise
        except Exception:
            log.exception("queue_worker 루프 예외")
            await asyncio.sleep(30)


@app.post("/trigger")
async def trigger():
    """핫 종목 N개를 선정해 큐에 추가. 분석은 큐 워커가 1개씩 진행.
    버튼으로 트리거해도 자동 스크리닝과 동일한 정책 (STRONG만 알림)."""
    try:
        added, _ = await pipeline.enqueue_hot_picks(
            config.TOP_N, source="auto_weekly",
        )
    except Exception:
        log.exception("hot picks 큐 추가 실패")
        return RedirectResponse(url="/?err=enqueue", status_code=303)
    return RedirectResponse(url=f"/?hot={added}", status_code=303)


# ---------------------------------------------------------------------------
# 단독 실행
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=config.LOG_LEVEL,
                        format="%(asctime)s %(name)s %(message)s")
    storage.init_db()
    uvicorn.run("dashboard:app", host="127.0.0.1",
                port=config.DASHBOARD_PORT, log_level="info")
