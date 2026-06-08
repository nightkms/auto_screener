"""
S7: 전체 파이프라인.

selector → data_loader → agents (병렬) → synthesizer → storage + notifier.
1회 실행 단위. scheduler.py가 매주 토 08:00에 호출한다.

CLI:
    python pipeline.py                  # 즉시 1회 실행
    python pipeline.py --top 3          # 후보 수 변경
    python pipeline.py --dry-run        # 알림 전송 skip
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
import traceback
from datetime import date
from pathlib import Path

import agents
import config
import data_loader
import notifier
import selector
import storage
import synthesizer
import ticker_archive

log = logging.getLogger("pipeline")


def _week_label(d: date | None = None) -> str:
    d = d or date.today()
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def _save_markdown(run_label: str, report: synthesizer.FinalReport) -> str:
    week_dir = config.ANALYSIS_DIR / run_label
    week_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in report.name if c not in r'\/:*?"<>|').strip()
    path = week_dir / f"{safe}_{report.ticker}.md"
    path.write_text(report.markdown, encoding="utf-8")
    # ANALYSIS_DIR 기준 상대경로로 저장 → 폴더를 통째로 옮겨도 보고서가 열림.
    return path.relative_to(config.ANALYSIS_DIR).as_posix()


async def run_once(top_n: int = config.TOP_N,
                   dry_run: bool = False,
                   source: str = "manual") -> int:
    """파이프라인 1회 실행. run_id 반환.
    source: 'manual' (CLI/수동), 'auto_weekly' (스케줄러), 'telegram'."""
    storage.init_db()
    week_label = _week_label()
    run_id = storage.create_run(
        week_label=week_label, source=source,
        notes=f"top_n={top_n} dry_run={dry_run} source={source}",
    )
    log.info("=== run %d 시작 (%s, top=%d, src=%s) ===",
             run_id, week_label, top_n, source)

    started = time.time()
    status = "success"

    try:
        # 1) 종목 선정
        cands = await selector.select_top_async(top_n=top_n)
        if not cands:
            log.error("후보 0건")
            storage.finish_run(run_id, "failed")
            return run_id
        storage.save_candidates(run_id, [c.to_dict() for c in cands])
        log.info("후보 %d개: %s", len(cands), [c.ticker for c in cands])

        # 2) DART 컨텍스트 — 원자료는 매번 전체 새로 받음 (증분 수집 비활성).
        # 단 이전 회차 한 줄 결론은 LLM에 컨텍스트로 주입해 "등급 변동 사유" 분석 유도.
        contexts: dict[str, data_loader.StockContext] = {}
        prior_summaries: dict[str, str] = {}
        for c in cands:
            prior = ""
            if source in ("auto_weekly", "auto_hourly"):
                prior = ticker_archive.read_last_summary(c.ticker, c.name)
            prior_summaries[c.ticker] = prior
            try:
                ctx = data_loader.load_context(c.ticker)
                if ctx:
                    contexts[c.ticker] = ctx
            except Exception as e:
                log.warning("[%s] DART 실패: %s", c.ticker, e)

        # 3) + 4) 분석 + 종합 (종목 단위 순차, 내부 5개 병렬)
        for c in cands:
            try:
                prior = prior_summaries.get(c.ticker, "")
                analysis = await agents.analyze_stock(
                    c, contexts.get(c.ticker),
                    prior_summary=prior,
                )
                # 토큰 사용 로깅
                for r in analysis.sub_results.values():
                    storage.log_usage(run_id, config.CLAUDE_SUB_MODEL,
                                      r.tokens_in, r.tokens_out,
                                      f"sub:{r.name}:{c.ticker}")
                report = await synthesizer.synthesize(
                    c, analysis, prior_summary=prior,
                )
                storage.log_usage(run_id, config.CLAUDE_SYNTH_MODEL,
                                  report.tokens_in, report.tokens_out,
                                  f"synth:{c.ticker}")

                # 종합 실패/한도초과면 좀비 보고서를 저장하지 않고 이 종목은 건너뜀.
                if not report.ok:
                    log.warning("[%s] 종합 실패/한도초과 → 보고서 저장 skip", c.ticker)
                    continue

                md_path = _save_markdown(week_label, report)
                sub_info = {
                    name: {
                        "rating": sr.rating,
                        "tokens_in": sr.tokens_in,
                        "tokens_out": sr.tokens_out,
                        "elapsed_s": sr.elapsed_s,
                        "error": sr.error,
                    }
                    for name, sr in analysis.sub_results.items()
                }
                report_id = storage.save_report(
                    run_id=run_id, ticker=c.ticker, name=analysis.name,
                    grade=report.grade, avg_rating=report.avg_rating,
                    md_path=md_path,
                    tokens_in=analysis.total_tokens_in + report.tokens_in,
                    tokens_out=analysis.total_tokens_out + report.tokens_out,
                    elapsed_s=report.elapsed_s,
                    sub_ratings=sub_info,
                )
                # 종목별 폴더에 보고서 사본 + 메타(등급·요약) 갱신.
                # 원자료(공시/뉴스)는 누적 안 함 (매번 새로 받음).
                try:
                    ticker_archive.record_run_complete(
                        ticker=c.ticker, name=analysis.name,
                        week_label=week_label, report=report,
                        md_path=md_path, run_id=run_id,
                    )
                except Exception as e:
                    log.warning("[%s] ticker_archive 갱신 실패: %s", c.ticker, e)
                if report.grade in ("STRONG", "WATCH") and c.close:
                    storage.add_price_watch(
                        ticker=c.ticker, name=analysis.name,
                        base_price=float(c.close),
                        base_grade=report.grade,
                        report_id=report_id,
                    )
                # 종목별 즉시 텔레그램 전송 (source에 따라 등급 필터 적용)
                if not dry_run:
                    try:
                        await notifier.notify_single_report(report_id, source=source)
                    except Exception as e:
                        log.warning("[%s] 단일 알림 실패: %s", c.ticker, e)
                log.info("[%s] %s grade=%s avg=%.2f",
                         c.ticker, analysis.name, report.grade, report.avg_rating)
            except Exception as e:
                log.exception("[%s] 분석 실패: %s", c.ticker, e)

        # 종목별 알림은 위 루프에서 끝나는 즉시 전송됨. 요약 알림은 보내지 않음.

    except Exception:
        status = "failed"
        tb = traceback.format_exc()
        log.error("pipeline 예외:\n%s", tb)
        try:
            await notifier.notify_error(tb[-1500:], run_id=run_id,
                                         context="pipeline.run_once")
        except Exception:
            pass
    finally:
        storage.finish_run(run_id, status)
        elapsed = time.time() - started
        log.info("=== run %d 종료 (%s, %.1fs) ===", run_id, status, elapsed)

    return run_id


async def enqueue_hot_picks(top_n: int = config.TOP_N,
                             source: str = "auto_weekly") -> tuple[int, list[str]]:
    """selector로 핫 종목을 뽑아 큐에 추가만. 분석은 큐 워커가 처리.
    (추가된 수, 추가된 ticker 목록) 반환.
    스케줄러가 호출할 때 source='auto_weekly', 대시보드 수동 트리거도 동일 의미."""
    storage.init_db()
    cands = await selector.select_top_async(top_n=top_n)
    added: list[str] = []
    for c in cands:
        if storage.add_to_queue(c.ticker, name=c.name, market=c.market,
                                 source=source, pick_source=c.source_tag):
            added.append(c.ticker)
    log.info("hot picks 큐 추가: %d / %d종목 src=%s (선정 %s)",
             len(added), len(cands), source, [c.ticker for c in cands])
    return len(added), added


class RetryableAnalysisError(RuntimeError):
    """rate-limit/네트워크 등 일시적 실패. 큐 워커가 retry 처리."""


SUB_SUCCESS_THRESHOLD = 3  # 5개 sub 중 3개 미만 성공 → retry


def _should_retry_analysis(analysis: agents.StockAnalysis) -> str:
    """sub 성공이 SUB_SUCCESS_THRESHOLD 미만이면 retry 사유 메시지 반환.
    메시지 패턴(rate-limit 등) 무관 — 영구/일시 구분 없이 무한 retry (사용자 룰).
    그 외 빈 문자열 → 보고서 생성 진행."""
    success_count = 0
    errors: list[str] = []
    for r in analysis.sub_results.values():
        if r.rating is not None and r.text:
            success_count += 1
        elif r.error:
            errors.append(r.error)
    if success_count < SUB_SUCCESS_THRESHOLD:
        sample = errors[0][:200] if errors else "no error message"
        return f"sub 성공 {success_count}/5 < {SUB_SUCCESS_THRESHOLD}: {sample}"
    return ""


async def run_for_ticker(ticker: str, name: str = "",
                          dry_run: bool = False,
                          source: str = "manual",
                          pick_source: str = "manual") -> int:
    """단일 종목을 큐에서 받아 분석. selector 우회.
    source: 'manual'/'auto_weekly'/'telegram' — 알림 정책 분기에 사용.
    pick_source: 선정근거(search/upper/quant/...). 상한가·거래량급증으로 잡힌
    종목은 '오늘 왜 움직였는지'를 분석 프롬프트가 규명하도록 전달한다."""
    storage.init_db()
    week_label = _week_label()
    run_id = storage.create_run(
        week_label=week_label, source=source,
        notes=f"ticker={ticker} name={name} src={source}",
    )
    log.info("=== run %d for %s (%s) src=%s ===",
             run_id, ticker, name, source)

    started = time.time()
    status = "success"

    try:
        cand = await selector.fetch_single_candidate(
            ticker, name=name, pick_source=pick_source)
        if cand is None:
            log.error("[%s] 시세 컨텍스트를 못 받음", ticker)
            cand = selector.Candidate(
                ticker=ticker, name=name or ticker, market="?",
                close=0, market_cap_billion=0,
                weekly_return=0, value_surge=0, foreign_delta=0, score=0,
                source_tag=pick_source or "manual",
            )
        storage.save_candidates(run_id, [cand.to_dict()])

        # 원자료는 매번 전체 새로 받음. 이전 회차 한 줄 결론만 컨텍스트로 주입
        # (auto_weekly 일 때만 — 사용자가 직접 찍은 manual/telegram은 깨끗한 재분석).
        prior_summary = ""
        if source in ("auto_weekly", "auto_hourly"):
            prior_summary = ticker_archive.read_last_summary(ticker, name)
            if prior_summary:
                log.info("[%s] 이전 회차 결론 컨텍스트 주입 (%d자)",
                         ticker, len(prior_summary))

        try:
            ctx = data_loader.load_context(ticker)
        except Exception as e:
            log.warning("[%s] DART 실패: %s", ticker, e)
            ctx = None

        analysis = await agents.analyze_stock(
            cand, ctx, prior_summary=prior_summary,
        )
        for r in analysis.sub_results.values():
            storage.log_usage(run_id, config.CLAUDE_SUB_MODEL,
                              r.tokens_in, r.tokens_out,
                              f"sub:{r.name}:{ticker}")

        # sub 성공 < 3이면 보고서 저장하지 않고 큐에 재시도 떠넘김.
        retry_reason = _should_retry_analysis(analysis)
        if retry_reason:
            raise RetryableAnalysisError(retry_reason)

        report = await synthesizer.synthesize(
            cand, analysis, prior_summary=prior_summary,
        )
        storage.log_usage(run_id, config.CLAUDE_SYNTH_MODEL,
                          report.tokens_in, report.tokens_out,
                          f"synth:{ticker}")

        # 종합이 한도초과/실패면 점수만 있는 좀비 보고서를 저장하지 말고
        # 큐에 재시도를 떠넘긴다(다음 정각 reset_failed_to_pending에서 재개).
        if not report.ok:
            raise RetryableAnalysisError(f"종합 실패/한도초과 → 큐 재시도 ({ticker})")

        md_path = _save_markdown(week_label, report)
        sub_info = {
            n: {"rating": sr.rating, "tokens_in": sr.tokens_in,
                "tokens_out": sr.tokens_out, "elapsed_s": sr.elapsed_s,
                "error": sr.error}
            for n, sr in analysis.sub_results.items()
        }
        report_id = storage.save_report(
            run_id=run_id, ticker=ticker, name=analysis.name,
            grade=report.grade, avg_rating=report.avg_rating,
            md_path=md_path,
            tokens_in=analysis.total_tokens_in + report.tokens_in,
            tokens_out=analysis.total_tokens_out + report.tokens_out,
            elapsed_s=report.elapsed_s,
            sub_ratings=sub_info,
        )
        # 종목별 폴더에 보고서 사본 + 메타(등급·요약) 갱신.
        # 원자료(공시/뉴스)는 누적 안 함 (매번 새로 받음).
        try:
            ticker_archive.record_run_complete(
                ticker=ticker, name=analysis.name,
                week_label=week_label, report=report,
                md_path=md_path, run_id=run_id,
            )
        except Exception as e:
            log.warning("[%s] ticker_archive 갱신 실패: %s", ticker, e)
        if report.grade in ("STRONG", "WATCH") and cand.close:
            storage.add_price_watch(
                ticker=ticker, name=analysis.name,
                base_price=float(cand.close),
                base_grade=report.grade,
                report_id=report_id,
            )
        # 종목별 즉시 알림 (큐 항목 1개씩 끝날 때마다, source별 등급 필터 적용)
        if not dry_run:
            try:
                await notifier.notify_single_report(report_id, source=source)
            except Exception as e:
                log.warning("단일 알림 실패: %s", e)

    except RetryableAnalysisError:
        status = "failed"
        log.warning("[%s] 일시 실패 (rate-limit/네트워크) → 큐 재시도 대기", ticker)
        raise
    except Exception:
        status = "failed"
        tb = traceback.format_exc()
        log.error("run 예외:\n%s", tb)
        try:
            await notifier.notify_error(
                tb[-1500:], run_id=run_id,
                context=f"pipeline.run_for_ticker {ticker}",
            )
        except Exception:
            pass
        raise
    finally:
        storage.finish_run(run_id, status)
        log.info("=== run %d 종료 (%s, %.1fs) ===",
                 run_id, status, time.time() - started)

    return run_id


def _main():
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=config.TOP_N)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s %(name)s %(message)s",
    )
    asyncio.run(run_once(top_n=args.top, dry_run=args.dry_run))


if __name__ == "__main__":
    _main()
