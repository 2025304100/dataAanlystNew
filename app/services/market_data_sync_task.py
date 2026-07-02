"""市场数据异步同步任务。

将 sync_market_data 拆分为分阶段异步任务，
后台线程执行，前端通过轮询获取进度。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.async_task import AsyncTaskRecord
from app.models.daily_bar import DailyBar
from app.models.scan import ScanResult
from app.schemas.async_task import MarketDataSyncCreate
from app.services.async_tasks import (
    _append_error,
    _now,
    _set_task,
    _start_worker,
    create_async_task,
    get_async_task,
)
from app.services.allocation import get_active_rule, get_default_portfolio
from app.services.analysis import calculate_symbol_score
from app.services.market_data import (
    _resolve_sync_symbols,
    sync_symbol_daily_bars,
)
from app.services.scans import run_scan
from app.services.symbol_names import refresh_symbol_name
from app.services.trade_plans import upsert_trade_setup
from app.db.session import get_session_local

logger = logging.getLogger(__name__)

TASK_TYPE = "market_data_sync"

# 阶段 → 百分比区间（评分内联在 sync 中，sync 阶段范围扩展到 90%）
STAGE_PERCENT = {
    "prepare": (0, 5),
    "sync": (5, 90),   # sync + score 内联
    "scan": (90, 98),
    "done": (98, 100),
}


def _calc_percent(stage: str, processed: int, total: int) -> float:
    """根据阶段和进度计算总百分比。"""
    start, end = STAGE_PERCENT.get(stage, (0, 100))
    if total <= 0:
        return float(start)
    ratio = min(processed / total, 1.0)
    return round(start + (end - start) * ratio, 1)


def _check_cancelled(db: Session, task_id: str) -> bool:
    """检查任务是否已被取消。"""
    task = db.get(AsyncTaskRecord, task_id)
    return task is not None and task.status == "cancelled"


def create_market_data_sync_task(payload: MarketDataSyncCreate) -> dict:
    """创建市场数据异步同步任务，启动后台 worker。
    
    如果已有同类型任务处于 queued/running 状态，则拒绝创建并返回已有任务。
    """
    from app.services.async_tasks import list_async_tasks

    # 检查是否有运行中的同类型任务
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in ("queued", "running"):
        return existing[0].model_dump()

    task_read = create_async_task(TASK_TYPE, payload.model_dump())
    _start_worker(task_read.id, _run_market_data_sync)
    return task_read.model_dump()


def _run_market_data_sync(task_id: str) -> None:
    """后台线程执行的市场数据同步逻辑。"""
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return

        payload = MarketDataSyncCreate.model_validate(
            json.loads(task.payload_json or "{}")
        )

        # ── stage: prepare ─────────────────────────────
        _set_task(db, task_id,
                  status="running", stage="prepare",
                  percent=2, message="解析标的列表...",
                  started_at=_now())

        if _check_cancelled(db, task_id):
            return

        symbols = _resolve_sync_symbols(
            db=db,
            scope=payload.scope,
            watchlist_id=payload.watchlist_id,
            symbol_ids=payload.symbol_ids,
            asset_types=payload.asset_types,
        )

        resolved_portfolio_id = payload.portfolio_id
        resolved_portfolio_rule_id = payload.portfolio_rule_id
        if resolved_portfolio_id is None:
            default_portfolio = get_default_portfolio(db)
            if default_portfolio is not None:
                resolved_portfolio_id = default_portfolio.id
        if resolved_portfolio_id is not None and resolved_portfolio_rule_id is None:
            active_rule = get_active_rule(db, resolved_portfolio_id)
            if active_rule is not None:
                resolved_portfolio_rule_id = active_rule.id

        total = len(symbols)
        if total == 0:
            _set_task(db, task_id,
                      status="done", stage="done", percent=100,
                      total=0, processed=0, ok_count=0, failed_count=0,
                      message="无需同步的标的",
                      result_json=json.dumps({"scope": payload.scope, "symbols_total": 0, "ok_count": 0, "failed_count": 0, "scored_count": 0}, ensure_ascii=False),
                      finished_at=_now())
            return

        _set_task(db, task_id,
                  total=total, processed=0,
                  ok_count=0, failed_count=0,
                  stage="sync", percent=_calc_percent("sync", 0, total),
                  message=f"开始同步行情 (0/{total})")

        # ── stage: sync + score ─────────────────────────
        ok_count = 0
        failed_count = 0
        scored_count = 0
        synced_symbol_ids: list[int] = []

        for i, symbol in enumerate(symbols, start=1):
            if _check_cancelled(db, task_id):
                return

            # 保存 symbol 信息，避免 rollback 后懒加载失败
            symbol_code = symbol.symbol
            symbol_id = symbol.id

            try:
                refresh_symbol_name(symbol)
                result = sync_symbol_daily_bars(
                    db=db,
                    symbol=symbol,
                    start_date=payload.start_date,
                    end_date=payload.end_date,
                    adjust=payload.adjust,
                )

                if result["status"] == "ok":
                    # 先提交行情数据，确保不被后续评分失败回滚
                    db.commit()

                    # 评分和交易计划单独 try，失败不影响已保存的行情
                    try:
                        latest_bar = db.execute(
                            select(DailyBar)
                            .where(DailyBar.symbol_id == symbol.id)
                            .order_by(DailyBar.trade_date.desc())
                        ).scalars().first()

                        if latest_bar is not None:
                            score = calculate_symbol_score(
                                db=db, symbol=symbol,
                                trade_date=latest_bar.trade_date,
                            )
                            scored_count += 1
                            if resolved_portfolio_id is not None:
                                upsert_trade_setup(
                                    db=db,
                                    portfolio_id=resolved_portfolio_id,
                                    symbol=symbol,
                                    score=score,
                                )
                            db.commit()
                    except Exception as score_exc:
                        db.rollback()
                        logger.warning("Score/trade-plan failed for %s: %s", symbol_code, score_exc)

                    ok_count += 1
                    synced_symbol_ids.append(symbol_id)
                else:
                    ok_count += 1  # empty 也算 ok
                    db.commit()

            except Exception as exc:
                db.rollback()
                failed_count += 1
                task_record = db.get(AsyncTaskRecord, task_id)
                if task_record is not None:
                    _append_error(task_record, {
                        "symbol": symbol_code,
                        "symbol_id": symbol_id,
                        "error": str(exc),
                    })
                logger.warning("Sync failed for %s: %s", symbol_code, exc)

            # 更新进度
            _set_task(db, task_id,
                      processed=i,
                      ok_count=ok_count,
                      failed_count=failed_count,
                      current_item=symbol.symbol,
                      percent=_calc_percent("sync", i, total),
                      message=f"同步行情 ({i}/{total})")

        # ── stage: scan ─────────────────────────────────
        auto_scan_result = None
        if _check_cancelled(db, task_id):
            return

        if payload.auto_scan and synced_symbol_ids:
            _set_task(db, task_id,
                      stage="scan",
                      percent=_calc_percent("scan", 0, 1),
                      current_item=None,
                      message="正在执行自动扫描...")

            try:
                scope_snapshot = {
                    "asset_types": payload.asset_types,
                    "symbol_ids": synced_symbol_ids,
                }
                if payload.scope == "watchlist" and payload.watchlist_id is not None:
                    scope_snapshot["watchlist_id"] = payload.watchlist_id

                scan_run = run_scan(
                    db=db,
                    scope_snapshot=scope_snapshot,
                    filters_snapshot={
                        "source": "market-data.sync-task",
                        "start_date": payload.start_date.isoformat() if payload.start_date else None,
                        "end_date": payload.end_date.isoformat() if payload.end_date else None,
                    },
                    portfolio_id=resolved_portfolio_id,
                    portfolio_rule_id=resolved_portfolio_rule_id,
                    run_name="post-sync-auto-scan",
                    preset_id=None,
                )
                executable_count = db.execute(
                    select(ScanResult).where(
                        ScanResult.scan_run_id == scan_run.id,
                        ScanResult.result_type == "executable",
                    )
                ).scalars().all()
                auto_scan_result = {
                    "triggered": True,
                    "scan_run_id": scan_run.id,
                    "executable_count": len(executable_count),
                }
                db.commit()
            except Exception as exc:
                db.rollback()
                logger.warning("Auto-scan failed: %s", exc, exc_info=True)
                auto_scan_result = {"triggered": False, "error": str(exc)}
        elif payload.auto_scan:
            auto_scan_result = {"triggered": False, "reason": "no_symbols_synced"}

        # ── stage: done ─────────────────────────────────
        result_summary = {
            "scope": payload.scope,
            "symbols_total": total,
            "ok_count": ok_count,
            "failed_count": failed_count,
            "scored_count": scored_count,
            "auto_scan": auto_scan_result,
        }

        _set_task(db, task_id,
                  status="done", stage="done",
                  percent=100,
                  message=f"同步完成: {ok_count} 成功, {failed_count} 失败",
                  current_item=None,
                  result_json=json.dumps(result_summary, ensure_ascii=False, default=str),
                  finished_at=_now())

    except Exception as exc:
        logger.exception("Market data sync task %s failed", task_id)
        try:
            _set_task(db, task_id,
                      status="failed", stage="failed",
                      message=f"同步任务失败: {exc}",
                      finished_at=_now())
        except Exception:
            logger.exception("Failed to mark task %s as failed", task_id)
    finally:
        db.close()
