"""종목별 자료 누적 (analysis/by_ticker/<ticker>/).

자동 스크리닝(주간 5개)이 같은 종목을 반복 분석할 때 토큰을 아끼기 위해,
이전 회차에서 모은 자료를 보존하고 증분만 다시 수집·분석한다.

폴더 구조:
    analysis/by_ticker/<ticker>_<safe_name>/
    ├── runs/
    │   ├── 2026-W20.md         각 회차 종합 보고서 복사본
    │   └── 2026-W22.md
    ├── dart_disclosures.jsonl   누적 공시 (dedup by rcept_no, append-only)
    ├── financials.json          최신 재무 스냅샷 (period → {계정: 값})
    ├── metadata.json            마지막 수집 시각·등급·요약
    └── last_summary.txt         이전 회차 한 줄 요약 (증분 분석 시 컨텍스트로 주입)

DB의 ticker_state 테이블에도 핵심 메타를 미러링한다 (파일 손상 대비 + 빠른 조회).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

import config
import storage

log = logging.getLogger("ticker_archive")


_SAFE_NAME_PAT = re.compile(r'[\\/:*?"<>|\s]+')


def _safe_name(name: str) -> str:
    """폴더명에 쓸 수 있도록 특수문자 제거."""
    return _SAFE_NAME_PAT.sub("_", (name or "").strip()) or "unknown"


def ticker_dir(ticker: str, name: str = "") -> Path:
    """analysis/by_ticker/<ticker>_<safe_name>/ 보장 생성."""
    # 기존 폴더가 있으면 (이름이 달라도) 그대로 사용
    existing = list(config.BY_TICKER_DIR.glob(f"{ticker}_*"))
    if existing:
        return existing[0]
    folder = config.BY_TICKER_DIR / f"{ticker}_{_safe_name(name)}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _meta_path(folder: Path) -> Path:
    return folder / "metadata.json"


def read_metadata(ticker: str, name: str = "") -> dict:
    p = _meta_path(ticker_dir(ticker, name))
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("metadata.json 손상 (%s): %s", ticker, e)
        return {}


def write_metadata(ticker: str, name: str = "", **updates) -> dict:
    """metadata.json을 부분 갱신해서 저장. 갱신 후 dict 반환."""
    folder = ticker_dir(ticker, name)
    meta = read_metadata(ticker, name)
    meta.update({k: v for k, v in updates.items() if v is not None})
    _meta_path(folder).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return meta


def save_run_report(ticker: str, name: str, week_label: str,
                     md_source_path: str | Path) -> Path | None:
    """주차별 보고서 마크다운을 종목별 폴더로 복사. runs/<week>.md."""
    src = Path(md_source_path) if md_source_path else None
    if src and not src.is_absolute():            # 신규는 ANALYSIS_DIR 기준 상대경로
        src = config.resolve_report_md(str(md_source_path))
    if not src or not src.exists():
        return None
    folder = ticker_dir(ticker, name) / "runs"
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / f"{week_label}.md"
    shutil.copyfile(src, dst)
    return dst


def append_disclosures(ticker: str, name: str,
                        disclosures: list[dict]) -> tuple[int, str | None, str | None]:
    """공시를 jsonl에 dedup append.
    반환: (신규 건수, 가장 최근 rcept_no, 가장 최근 date YYYYMMDD)."""
    folder = ticker_dir(ticker, name)
    jsonl = folder / "dart_disclosures.jsonl"
    seen: set[str] = set()
    if jsonl.exists():
        with jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if "rcept_no" in row:
                        seen.add(row["rcept_no"])
                except Exception:
                    continue

    added = 0
    latest_no: str | None = None
    latest_date: str | None = None
    with jsonl.open("a", encoding="utf-8") as f:
        for d in disclosures:
            no = d.get("rcept_no")
            if not no or no in seen:
                continue
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
            seen.add(no)
            added += 1
            dt = d.get("date") or ""
            if not latest_date or dt > latest_date:
                latest_date = dt
                latest_no = no
    return added, latest_no, latest_date


def read_disclosure_summaries(ticker: str, name: str = "") -> dict[str, dict]:
    """누적 공시 jsonl에서 rcept_no → row 맵을 읽는다.
    이미 본문 요약(summary)이 붙은 공시는 재방문 시 LLM 재호출 없이 재사용한다.
    summary 없는 옛 row도 포함해 반환하므로, 호출부에서 summary 유무로 판단할 것."""
    folder = ticker_dir(ticker, name)
    jsonl = folder / "dart_disclosures.jsonl"
    out: dict[str, dict] = {}
    if not jsonl.exists():
        return out
    with jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            no = row.get("rcept_no")
            if no:
                out[no] = row      # 같은 rcept_no면 뒤(최신 append)가 이김
    return out


def cache_disclosure_summaries(ticker: str, name: str,
                                rows: list[dict]) -> int:
    """본문 요약(summary)이 붙은 공시 row를 jsonl에 append.
    append_disclosures와 달리 rcept_no 중복을 막지 않는다 — 옛 요약-미보유 row가
    이미 있어도 새 요약 row를 덧붙여 read_disclosure_summaries(뒤가 이김)가 최신
    요약을 집어가도록 한다. 같은 요약이 중복 누적되는 일은 호출부(캐시 히트 시 skip)가
    막는다. 반환: 실제 append한 건수."""
    rows = [r for r in rows if r.get("summary") and r.get("rcept_no")]
    if not rows:
        return 0
    jsonl = ticker_dir(ticker, name) / "dart_disclosures.jsonl"
    with jsonl.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def save_financials_snapshot(ticker: str, name: str,
                              financials: dict[str, dict[str, float]]) -> None:
    """최신 재무 스냅샷을 단일 파일로 저장 (덮어쓰기)."""
    if not financials:
        return
    folder = ticker_dir(ticker, name)
    (folder / "financials.json").write_text(
        json.dumps(financials, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_financials_snapshot(ticker: str, name: str = "") -> dict:
    p = ticker_dir(ticker, name) / "financials.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_last_summary(ticker: str, name: str, summary: str) -> None:
    folder = ticker_dir(ticker, name)
    (folder / "last_summary.txt").write_text(summary.strip(), encoding="utf-8")


def read_last_summary(ticker: str, name: str = "") -> str:
    p = ticker_dir(ticker, name) / "last_summary.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def extract_summary_from_markdown(md: str, max_lines: int = 8) -> str:
    """보고서 마크다운에서 '한 줄 결론' / '요약' 섹션의 처음 몇 줄을 추출.
    실패하면 본문 첫 max_lines줄."""
    if not md:
        return ""
    # 한 줄 결론 / 결론 / 요약 / TL;DR 헤더 찾기
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^#+\s*(한\s*줄\s*결론|결론|요약|TL;DR|핵심)", line.strip()):
            chunk = []
            for j in range(i + 1, min(i + 1 + max_lines * 3, len(lines))):
                t = lines[j].strip()
                if t.startswith("#"):
                    break
                if t:
                    chunk.append(t)
                if len(chunk) >= max_lines:
                    break
            if chunk:
                return "\n".join(chunk)
    # fallback: 본문 첫 줄들
    head = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    return "\n".join(head[:max_lines])


def record_run_complete(ticker: str, name: str, week_label: str,
                         report, md_path: str | Path,
                         run_id: int,
                         disclosures: list[dict] | None = None,
                         financials: dict | None = None) -> dict:
    """파이프라인이 한 종목 분석을 끝낸 직후 호출.
    종목별 폴더 + DB ticker_state를 한 번에 갱신.

    report: synthesizer.FinalReport (grade, avg_rating, markdown 사용).
    반환: 갱신된 metadata.
    """
    save_run_report(ticker, name, week_label, md_path)

    latest_no = None
    latest_date = None
    added_n = 0
    if disclosures:
        added_n, latest_no, latest_date = append_disclosures(
            ticker, name, disclosures,
        )
    if financials:
        save_financials_snapshot(ticker, name, financials)

    summary = extract_summary_from_markdown(getattr(report, "markdown", ""))
    if summary:
        save_last_summary(ticker, name, summary)

    now = datetime.now().isoformat(timespec="seconds")
    meta = write_metadata(
        ticker, name=name,
        last_collected_at=now,
        last_run_id=run_id,
        last_run_week=week_label,
        last_grade=getattr(report, "grade", None),
        last_avg_rating=getattr(report, "avg_rating", None),
        last_disclosure_rcept_no=latest_no
            or read_metadata(ticker, name).get("last_disclosure_rcept_no"),
        last_disclosure_date=latest_date
            or read_metadata(ticker, name).get("last_disclosure_date"),
        last_summary=summary or read_metadata(ticker, name).get("last_summary"),
    )

    # DB 미러
    storage.upsert_ticker_state(
        ticker, name=name,
        last_collected_at=now,
        last_disclosure_date=meta.get("last_disclosure_date"),
        last_disclosure_rcept_no=meta.get("last_disclosure_rcept_no"),
        last_run_id=run_id, last_run_week=week_label,
        last_grade=meta.get("last_grade"),
        last_avg_rating=meta.get("last_avg_rating"),
        last_summary=summary or None,
    )

    log.info("[%s] archive 갱신 week=%s 신규공시=%d grade=%s",
             ticker, week_label, added_n, meta.get("last_grade"))
    return meta
