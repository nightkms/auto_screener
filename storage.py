"""
S6: SQLite 저장소.

- runs:        파이프라인 실행 단위 (start/end/status/총비용)
- candidates:  실행별 후보 + 선정 지표
- reports:     종목별 최종 보고서 (등급, 평균★, 마크다운 경로)
- sub_ratings: 서브에이전트별 점수
- usage:       토큰 사용량 (모델별 누계)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import config

log = logging.getLogger("storage")


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    status       TEXT NOT NULL DEFAULT 'running',  -- running/success/failed
    week_label   TEXT NOT NULL,                    -- 'YYYY-W##'
    source       TEXT NOT NULL DEFAULT 'manual',   -- manual/auto_weekly/telegram
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    ticker       TEXT NOT NULL,
    name         TEXT,
    market       TEXT,
    weekly_return REAL,
    value_surge  REAL,
    foreign_delta REAL,
    select_score REAL,
    rank_in_run  INTEGER
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    ticker       TEXT NOT NULL,
    name         TEXT,
    grade        TEXT,                              -- STRONG/WATCH/INTEREST/SKIP
    avg_rating   REAL,
    md_path      TEXT,                              -- analysis/auto/YYYY-W##/...
    tokens_in    INTEGER DEFAULT 0,
    tokens_out   INTEGER DEFAULT 0,
    elapsed_s    REAL
);

CREATE TABLE IF NOT EXISTS sub_ratings (
    report_id    INTEGER NOT NULL REFERENCES reports(id),
    sub_name     TEXT NOT NULL,
    rating       REAL,
    tokens_in    INTEGER DEFAULT 0,
    tokens_out   INTEGER DEFAULT 0,
    elapsed_s    REAL,
    error        TEXT,
    PRIMARY KEY (report_id, sub_name)
);

CREATE TABLE IF NOT EXISTS usage_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER REFERENCES runs(id),
    timestamp    TEXT NOT NULL,
    model        TEXT,
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    context      TEXT                                -- e.g. 'sub:valuation:260970'
);

CREATE INDEX IF NOT EXISTS idx_reports_run ON reports(run_id);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id);

CREATE TABLE IF NOT EXISTS analysis_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL,
    name         TEXT,
    market       TEXT,
    source       TEXT NOT NULL DEFAULT 'manual',   -- manual/auto_weekly/telegram
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending/processing/done/failed
    queued_at    TEXT NOT NULL,
    started_at   TEXT,
    ended_at     TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    run_id       INTEGER REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON analysis_queue(status);

CREATE TABLE IF NOT EXISTS ticker_state (
    ticker                    TEXT PRIMARY KEY,
    name                      TEXT,
    last_collected_at         TEXT,                 -- ISO datetime of last DART/news pull
    last_disclosure_date      TEXT,                 -- YYYYMMDD of most recent disclosure seen
    last_disclosure_rcept_no  TEXT,
    last_run_id               INTEGER,              -- runs.id 참조 (FK 없음; run 삭제돼도 메타는 보존)
    last_run_week             TEXT,
    last_grade                TEXT,
    last_avg_rating           REAL,
    last_summary              TEXT                  -- 이전 회차 한 줄 요약 (증분 분석 시 컨텍스트)
);

CREATE TABLE IF NOT EXISTS price_watch (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL,
    name         TEXT,
    base_price   REAL NOT NULL,
    base_date    TEXT NOT NULL,
    base_grade   TEXT,
    last_alert_pct REAL DEFAULT 0,
    last_checked TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    report_id    INTEGER REFERENCES reports(id)
);
CREATE INDEX IF NOT EXISTS idx_watch_active ON price_watch(active);

CREATE TABLE IF NOT EXISTS report_qa (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id    INTEGER NOT NULL REFERENCES reports(id),
    role         TEXT NOT NULL,        -- 'user' | 'assistant'
    content      TEXT NOT NULL,
    tokens_in    INTEGER DEFAULT 0,
    tokens_out   INTEGER DEFAULT 0,
    elapsed_s    REAL DEFAULT 0,
    sources      TEXT,                 -- JSON: 도구 결과 출처(WebSearch URL, rcept_no 등)
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_report ON report_qa(report_id);
"""

# 실패 분류는 사용하지 않는다 (모든 실패는 무한 retry, 정렬로만 우선순위 결정).
# 큐 정책: 5/5 또는 sub 성공<3이면 reports 미생성 + 큐 'failed'로 attempts++,
# 다음 정각에 reset_failed_to_pending으로 일괄 'pending' 복귀.


# ---------------------------------------------------------------------------
# 연결
# ---------------------------------------------------------------------------
def _migrate(c: sqlite3.Connection) -> None:
    """기존 DB에 신규 컬럼 추가 (IF NOT EXISTS 우회)."""
    def cols(table: str) -> set[str]:
        rows = c.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}

    if "source" not in cols("runs"):
        c.execute("ALTER TABLE runs ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        log.info("migrate: runs.source 추가")
    if "source" not in cols("analysis_queue"):
        c.execute("ALTER TABLE analysis_queue ADD COLUMN source "
                  "TEXT NOT NULL DEFAULT 'manual'")
        log.info("migrate: analysis_queue.source 추가")

    # ticker_state에 last_run_id FK가 살아있으면 (이전 스키마) 테이블 재생성.
    # 이 테이블은 미러 캐시라 데이터 보존 안 해도 무방.
    fks = c.execute("PRAGMA foreign_key_list(ticker_state)").fetchall()
    if fks:
        log.info("migrate: ticker_state FK 제거 위해 재생성")
        c.execute("DROP TABLE ticker_state")
        c.executescript(SCHEMA)  # ticker_state만 다시 만듦 (IF NOT EXISTS라 다른 건 영향 없음)

    # 신 큐 정책: 'failed_fatal' 폐기 → 'failed' + attempts=0 으로 되살려 무한 retry.
    cur = c.execute(
        "UPDATE analysis_queue SET status='failed', attempts=0 "
        "WHERE status='failed_fatal'"
    )
    if cur.rowcount:
        log.info("migrate: failed_fatal %d건을 'failed'로 되살림", cur.rowcount)


def init_db() -> None:
    with _connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)
    log.info("DB 초기화: %s", config.DB_PATH)


@contextmanager
def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def create_run(week_label: str, notes: str = "",
               source: str = "manual") -> int:
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO runs (started_at, week_label, source, notes) "
            "VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"),
             week_label, source, notes),
        )
        return cur.lastrowid


def finish_run(run_id: int, status: str = "success") -> None:
    with _connect() as c:
        c.execute(
            "UPDATE runs SET ended_at=?, status=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), status, run_id),
        )


def cleanup_stale_runs() -> int:
    """프로세스 시작 시 호출. 이전 실행이 비정상 종료해서
    status='running' 으로 남은 run들을 'crashed'로 마킹."""
    with _connect() as c:
        cur = c.execute(
            "UPDATE runs SET status='crashed', ended_at=? "
            "WHERE status='running' AND ended_at IS NULL",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------
def save_candidates(run_id: int, candidates: list[dict]) -> None:
    with _connect() as c:
        for i, cand in enumerate(candidates, 1):
            c.execute(
                """INSERT INTO candidates
                   (run_id, ticker, name, market, weekly_return,
                    value_surge, foreign_delta, select_score, rank_in_run)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (run_id, cand["ticker"], cand["name"], cand["market"],
                 cand["weekly_return"], cand["value_surge"],
                 cand["foreign_delta"], cand["score"], i),
            )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def save_report(run_id: int, ticker: str, name: str, grade: str,
                avg_rating: float, md_path: str, tokens_in: int,
                tokens_out: int, elapsed_s: float,
                sub_ratings: dict[str, dict]) -> int:
    with _connect() as c:
        cur = c.execute(
            """INSERT INTO reports
               (run_id, ticker, name, grade, avg_rating, md_path,
                tokens_in, tokens_out, elapsed_s)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, ticker, name, grade, avg_rating, md_path,
             tokens_in, tokens_out, elapsed_s),
        )
        rid = cur.lastrowid
        for sub, info in sub_ratings.items():
            c.execute(
                """INSERT INTO sub_ratings
                   (report_id, sub_name, rating, tokens_in, tokens_out, elapsed_s, error)
                   VALUES (?,?,?,?,?,?,?)""",
                (rid, sub, info.get("rating"), info.get("tokens_in", 0),
                 info.get("tokens_out", 0), info.get("elapsed_s", 0),
                 info.get("error", "")),
            )
        return rid


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
def log_usage(run_id: int | None, model: str, tokens_in: int,
              tokens_out: int, context: str = "") -> None:
    with _connect() as c:
        c.execute(
            """INSERT INTO usage_log (run_id, timestamp, model, tokens_in, tokens_out, context)
               VALUES (?,?,?,?,?,?)""",
            (run_id, datetime.now().isoformat(timespec="seconds"),
             model, tokens_in, tokens_out, context),
        )


# ---------------------------------------------------------------------------
# 조회 (대시보드용)
# ---------------------------------------------------------------------------
def recent_runs(limit: int = 20) -> list[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def reports_for_run(run_id: int) -> list[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM reports WHERE run_id=? ORDER BY avg_rating DESC",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def candidates_for_run(run_id: int) -> list[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM candidates WHERE run_id=? ORDER BY rank_in_run",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Analysis queue
# ---------------------------------------------------------------------------
def add_to_queue(ticker: str, name: str = "", market: str = "",
                 source: str = "manual") -> bool:
    """pending/processing/failed 인 같은 ticker가 이미 있으면 추가하지 않음.
    'failed'까지 막아야 정각 hot pick이 동일 retry 종목을 중복 INSERT하지 않음.
    source: 'manual' (사용자 수동 추가) / 'auto_weekly' / 'auto_hourly' / 'telegram'."""
    with _connect() as c:
        exists = c.execute(
            "SELECT 1 FROM analysis_queue WHERE ticker=? "
            "AND status IN ('pending','processing','failed')",
            (ticker,),
        ).fetchone()
        if exists:
            return False
        c.execute(
            """INSERT INTO analysis_queue (ticker, name, market, source, queued_at)
               VALUES (?, ?, ?, ?, ?)""",
            (ticker, name, market, source,
             datetime.now().isoformat(timespec="seconds")),
        )
        return True


def queue_items(statuses: tuple[str, ...] | None = None,
                limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM analysis_queue"
    params: tuple = ()
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        sql += f" WHERE status IN ({placeholders})"
        params = statuses
    sql += " ORDER BY id DESC LIMIT ?"
    params = params + (limit,)
    with _connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def next_queue_item() -> dict | None:
    """다음 처리할 항목. 'pending'만 잡음. 'failed'는 다음 정각의
    reset_failed_to_pending까지 휴면 → 즉시 재시도 방지.
    정렬: attempts ASC → 재시도 적은 신규가 먼저, retry 종목은 자연히 뒤로."""
    with _connect() as c:
        row = c.execute(
            """SELECT * FROM analysis_queue
               WHERE status='pending'
               ORDER BY attempts ASC, id ASC LIMIT 1"""
        ).fetchone()
        return dict(row) if row else None


def mark_queue_processing(qid: int) -> None:
    """워커가 항목을 잡음. attempts는 여기서 올리지 않고,
    실제 영구 실패(fatal) 시에만 카운트 → rate-limit이 반복돼도 무한 retry."""
    with _connect() as c:
        c.execute(
            """UPDATE analysis_queue
               SET status='processing', started_at=?
               WHERE id=?""",
            (datetime.now().isoformat(timespec="seconds"), qid),
        )


def mark_queue_done(qid: int, run_id: int | None = None) -> None:
    with _connect() as c:
        c.execute(
            """UPDATE analysis_queue
               SET status='done', ended_at=?, run_id=?, last_error=NULL
               WHERE id=?""",
            (datetime.now().isoformat(timespec="seconds"), run_id, qid),
        )


def mark_queue_failed_retry(qid: int, err: str) -> None:
    """실패 처리. status='failed' + attempts++ → 다음 정각의 reset 전까지 휴면.
    영속/일시 구분 없이 무한 retry (사용자 룰). 큐는 보존."""
    with _connect() as c:
        c.execute(
            """UPDATE analysis_queue
               SET status='failed',
                   attempts=attempts+1,
                   last_error=?, ended_at=?
               WHERE id=?""",
            (err[:500],
             datetime.now().isoformat(timespec="seconds"), qid),
        )


def reset_failed_to_pending() -> int:
    """정각 hourly 잡 + 부팅 시 호출. 'failed' → 'pending'으로 일괄 전환.
    attempts는 보존 → next_queue_item 정렬에서 자연히 뒤로 감."""
    with _connect() as c:
        cur = c.execute(
            "UPDATE analysis_queue SET status='pending' WHERE status='failed'"
        )
        return cur.rowcount


def reset_stuck_queue() -> int:
    """부팅 시: status='processing'인 항목을 'pending'으로 복귀.
    이전 실행이 토큰 한도 등으로 중단된 경우 자동 재시도."""
    with _connect() as c:
        cur = c.execute(
            "UPDATE analysis_queue SET status='pending' WHERE status='processing'"
        )
        return cur.rowcount


def delete_report(report_id: int, delete_md: bool = True) -> dict | None:
    """단일 보고서를 카스케이드 삭제. UI 수동 삭제와 시간당 purge 공용.

    - reports / sub_ratings / report_qa / price_watch row 삭제
    - delete_md=True 면 reports.md_path 파일도 삭제 (best effort)
    - runs / ticker_state / ticker_archive 사본은 보존 (이력 유지)

    반환: 삭제된 {"ticker","name","source","report_id"} (없으면 None)
    """
    with _connect() as c:
        row = c.execute(
            """SELECT r.id AS report_id, r.ticker, r.name, r.md_path,
                      runs.source
               FROM reports r
               JOIN runs ON r.run_id = runs.id
               WHERE r.id=?""",
            (report_id,),
        ).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM sub_ratings WHERE report_id=?", (report_id,))
        c.execute("DELETE FROM report_qa WHERE report_id=?", (report_id,))
        c.execute("DELETE FROM price_watch WHERE report_id=?", (report_id,))
        c.execute("DELETE FROM reports WHERE id=?", (report_id,))
        info = {
            "ticker": row["ticker"],
            "name": row["name"] or "",
            "source": row["source"] or "manual",
            "report_id": report_id,
        }
    if delete_md and row["md_path"]:
        try:
            Path(row["md_path"]).unlink(missing_ok=True)
        except Exception as e:
            log.warning("md_path 삭제 실패: %s — %s", row["md_path"], e)
    log.info("보고서 삭제: report=%d %s(%s)",
             report_id, info["name"], info["ticker"])
    return info


def remove_queue_item(qid: int) -> None:
    with _connect() as c:
        c.execute("DELETE FROM analysis_queue WHERE id=?", (qid,))


# ---------------------------------------------------------------------------
# Price watch list
# ---------------------------------------------------------------------------
def add_price_watch(ticker: str, name: str, base_price: float,
                     base_grade: str, report_id: int | None = None) -> bool:
    """이미 active 워치 있으면 추가하지 않음."""
    with _connect() as c:
        exists = c.execute(
            "SELECT 1 FROM price_watch WHERE ticker=? AND active=1",
            (ticker,),
        ).fetchone()
        if exists:
            return False
        c.execute(
            """INSERT INTO price_watch
               (ticker, name, base_price, base_date, base_grade, report_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ticker, name, base_price,
             datetime.now().isoformat(timespec="seconds"),
             base_grade, report_id),
        )
        return True


def active_watches() -> list[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM price_watch WHERE active=1 ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_watch(watch_id: int, last_alert_pct: float | None = None,
                 last_checked: str | None = None,
                 deactivate: bool = False) -> None:
    with _connect() as c:
        if deactivate:
            c.execute(
                "UPDATE price_watch SET active=0, last_checked=? WHERE id=?",
                (last_checked or datetime.now().isoformat(timespec="seconds"),
                 watch_id),
            )
            return
        sets = ["last_checked=?"]
        params: list = [last_checked or datetime.now().isoformat(timespec="seconds")]
        if last_alert_pct is not None:
            sets.append("last_alert_pct=?")
            params.append(last_alert_pct)
        params.append(watch_id)
        c.execute(f"UPDATE price_watch SET {','.join(sets)} WHERE id=?",
                  tuple(params))


def expire_old_watches(days: int = 14) -> int:
    threshold = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _connect() as c:
        cur = c.execute(
            "UPDATE price_watch SET active=0 WHERE active=1 AND base_date<?",
            (threshold,),
        )
        return cur.rowcount


def recently_analyzed_tickers(days: int = 30) -> set[str]:
    """최근 N일 안에 (선정→분석된) 종목 ticker 집합. selector dedup용."""
    threshold = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _connect() as c:
        rows = c.execute(
            """SELECT DISTINCT r.ticker FROM reports r
               JOIN runs ON r.run_id = runs.id
               WHERE runs.started_at >= ?""",
            (threshold,),
        ).fetchall()
    return {r["ticker"] for r in rows}


def strong_reports(limit: int = 50) -> list[dict]:
    """STRONG 등급 종목 누적 (대시보드 '강력매수 추천' 섹션)."""
    with _connect() as c:
        rows = c.execute(
            """SELECT r.*, runs.week_label, runs.started_at as run_started
               FROM reports r JOIN runs ON r.run_id = runs.id
               WHERE r.grade='STRONG' ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def recent_reports(limit: int = 20) -> list[dict]:
    """최근 분석된 종목 (run 무관, 시간 역순)."""
    with _connect() as c:
        rows = c.execute(
            """SELECT r.*, runs.week_label, runs.started_at as run_started
               FROM reports r JOIN runs ON r.run_id = runs.id
               ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def reports_for_ticker(ticker: str, limit: int = 100) -> list[dict]:
    """동일 종목의 보고서 이력 (최신순). 각 row에 sub_ratings(dict)도 inline.
    종목 페이지에서 등급/★ 변화 추적용."""
    with _connect() as c:
        rows = c.execute(
            """SELECT r.*, runs.week_label, runs.started_at as run_started,
                      runs.source as run_source
               FROM reports r JOIN runs ON r.run_id = runs.id
               WHERE r.ticker=? ORDER BY r.id DESC LIMIT ?""",
            (ticker, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            sub_rows = c.execute(
                "SELECT sub_name, rating FROM sub_ratings WHERE report_id=?",
                (d["id"],),
            ).fetchall()
            d["sub"] = {x["sub_name"]: x["rating"] for x in sub_rows}
            out.append(d)
        return out


def recent_reports_paged(page: int = 1, per_page: int = 10,
                          grades: tuple[str, ...] | None = None,
                          q: str | None = None,
                          ) -> tuple[list[dict], int]:
    """(items, total_count) — 최근 분석 페이지네이션.
    grades: 등급 필터. q: 종목명 또는 ticker 부분 일치 (공백 제거 후 LIKE)."""
    page = max(1, page)
    offset = (page - 1) * per_page
    where_parts: list[str] = []
    params: tuple = ()
    if grades:
        placeholders = ",".join("?" for _ in grades)
        where_parts.append(f"r.grade IN ({placeholders})")
        params = params + grades
    if q and q.strip():
        like = f"%{q.strip()}%"
        where_parts.append("(r.name LIKE ? OR r.ticker LIKE ?)")
        params = params + (like, like)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    with _connect() as c:
        total = c.execute(
            f"SELECT COUNT(*) FROM reports r {where}", params,
        ).fetchone()[0]
        rows = c.execute(
            f"""SELECT r.*, runs.week_label, runs.started_at as run_started
               FROM reports r JOIN runs ON r.run_id = runs.id
               {where}
               ORDER BY r.id DESC LIMIT ? OFFSET ?""",
            params + (per_page, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


# ---------------------------------------------------------------------------
# ticker_state (종목별 메타: 증분 수집용)
# ---------------------------------------------------------------------------
def get_ticker_state(ticker: str) -> dict | None:
    with _connect() as c:
        row = c.execute(
            "SELECT * FROM ticker_state WHERE ticker=?", (ticker,)
        ).fetchone()
        return dict(row) if row else None


def upsert_ticker_state(ticker: str, **fields) -> None:
    """fields 중 None 아닌 값만 갱신. 없으면 row 생성."""
    if not fields:
        return
    allowed = {"name", "last_collected_at", "last_disclosure_date",
               "last_disclosure_rcept_no", "last_run_id", "last_run_week",
               "last_grade", "last_avg_rating", "last_summary"}
    upd = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not upd:
        return
    with _connect() as c:
        exists = c.execute(
            "SELECT 1 FROM ticker_state WHERE ticker=?", (ticker,),
        ).fetchone()
        if exists:
            sets = ",".join(f"{k}=?" for k in upd)
            c.execute(
                f"UPDATE ticker_state SET {sets} WHERE ticker=?",
                tuple(upd.values()) + (ticker,),
            )
        else:
            cols = ["ticker"] + list(upd.keys())
            qs = ",".join("?" for _ in cols)
            c.execute(
                f"INSERT INTO ticker_state ({','.join(cols)}) VALUES ({qs})",
                (ticker,) + tuple(upd.values()),
            )


def usage_summary(run_id: int | None = None,
                  run_ids: list[int] | None = None) -> dict:
    with _connect() as c:
        if run_id is not None:
            row = c.execute(
                """SELECT model, SUM(tokens_in) ti, SUM(tokens_out) to_
                   FROM usage_log WHERE run_id=? GROUP BY model""",
                (run_id,),
            ).fetchall()
        elif run_ids is not None:
            if not run_ids:
                return {}
            placeholders = ",".join("?" * len(run_ids))
            row = c.execute(
                f"""SELECT model, SUM(tokens_in) ti, SUM(tokens_out) to_
                    FROM usage_log WHERE run_id IN ({placeholders})
                    GROUP BY model""",
                tuple(run_ids),
            ).fetchall()
        else:
            row = c.execute(
                """SELECT model, SUM(tokens_in) ti, SUM(tokens_out) to_
                   FROM usage_log GROUP BY model""",
            ).fetchall()
    return {r["model"]: {"in": r["ti"], "out": r["to_"]} for r in row}


# ---------------------------------------------------------------------------
# Report Q&A (대시보드 보고서 페이지 채팅)
# ---------------------------------------------------------------------------
def save_qa_message(report_id: int, role: str, content: str,
                    tokens_in: int = 0, tokens_out: int = 0,
                    elapsed_s: float = 0.0,
                    sources: list[dict] | None = None) -> int:
    """role: 'user' | 'assistant'. sources는 도구 호출 결과(URL/rcept_no 등) JSON."""
    if role not in ("user", "assistant"):
        raise ValueError(f"role must be user|assistant, got {role!r}")
    with _connect() as c:
        cur = c.execute(
            """INSERT INTO report_qa
               (report_id, role, content, tokens_in, tokens_out, elapsed_s, sources, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (report_id, role, content, tokens_in, tokens_out, elapsed_s,
             json.dumps(sources or [], ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def list_qa_messages(report_id: int, limit: int = 200) -> list[dict]:
    """보고서별 Q&A 이력. created_at 오름차순."""
    with _connect() as c:
        rows = c.execute(
            """SELECT * FROM report_qa
               WHERE report_id=? ORDER BY id ASC LIMIT ?""",
            (report_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["sources"] = json.loads(d.get("sources") or "[]")
        except json.JSONDecodeError:
            d["sources"] = []
        out.append(d)
    return out


if __name__ == "__main__":
    init_db()
    print("DB:", config.DB_PATH)
