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
from app.services.external_data_gateway import (
    is_within_offpeak_window,
    record_failed_batch,
    trigger_auto_recovery,
)

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
                # P1-08：记录失败批次到网关失败批次注册表，不阻塞其他标的的补数
                # 失败批次可通过 retry_failed_batch() 或 /market-data/sync-tasks/{id}/retry-failed 重跑
                try:
                    record_failed_batch(
                        interface_key="akshare.daily_bars",
                        symbols=[symbol_code],
                        reason=str(exc),
                        error_code="sync_failed",
                    )
                except Exception:
                    logger.debug("record_failed_batch failed for %s", symbol_code, exc_info=True)
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

        # P1-08：数据就绪后自动恢复——补数完成后触发评分/扫描更新
        # 调用方可通过 register_auto_recovery_callback 注册自定义恢复逻辑
        # （如重新计算因子覆盖率、刷新数据健康度、更新扫描快照）
        if synced_symbol_ids:
            synced_symbol_codes = [
                s.symbol for s in symbols if s.id in set(synced_symbol_ids)
            ]
            try:
                import asyncio
                asyncio.run(trigger_auto_recovery(synced_symbol_codes))
            except Exception:
                logger.debug("trigger_auto_recovery failed", exc_info=True)

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


def retry_sync_failed_symbols(task_id: str) -> dict:
    """重试指定同步任务中失败的标的（P1-08 失败批次续跑）。

    从原任务的 errors_json 中提取失败标的列表，创建一个新的同步任务仅同步这些标的。
    失败批次不阻塞其他标的：原任务已完成的标的不会重复同步。

    Args:
        task_id: 原同步任务 ID

    Returns:
        新创建的重试任务字典；原任务不存在或无失败标的时返回错误信息

    Raises:
        ValueError: 原任务不存在
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            raise ValueError(f"Sync task not found: {task_id}")
        if task.task_type != TASK_TYPE:
            raise ValueError(f"Task {task_id} is not a market_data_sync task")

        # 从 errors_json 提取失败标的
        errors = []
        try:
            errors = json.loads(task.errors_json or "[]")
        except (json.JSONDecodeError, TypeError):
            errors = []

        failed_symbol_ids: list[int] = []
        for err in errors:
            sid = err.get("symbol_id")
            if sid is not None and sid not in failed_symbol_ids:
                failed_symbol_ids.append(sid)

        if not failed_symbol_ids:
            return {
                "task_id": task_id,
                "retried": False,
                "reason": "no_failed_symbols",
                "message": "原任务无失败标的记录，无需重试",
            }

        # 加载原 payload 以保留 portfolio_id / adjust 等配置
        try:
            original_payload = MarketDataSyncCreate.model_validate(
                json.loads(task.payload_json or "{}")
            )
        except Exception:
            original_payload = None

        # 构造仅含失败标的的新 payload
        from app.models.symbol import Symbol
        symbols = db.execute(
            select(Symbol).where(Symbol.id.in_(failed_symbol_ids))
        ).scalars().all()

        if not symbols:
            return {
                "task_id": task_id,
                "retried": False,
                "reason": "symbols_not_found",
                "message": "失败标的已不存在",
            }

        retry_payload = MarketDataSyncCreate(
            scope="symbols",
            symbol_ids=[s.id for s in symbols],
            asset_types=(
                list({s.asset_type for s in symbols})
                if original_payload is None
                else original_payload.asset_types
            ),
            adjust=original_payload.adjust if original_payload else "qfq",
            start_date=original_payload.start_date if original_payload else None,
            end_date=original_payload.end_date if original_payload else None,
            auto_scan=original_payload.auto_scan if original_payload else False,
            portfolio_id=original_payload.portfolio_id if original_payload else None,
            portfolio_rule_id=original_payload.portfolio_rule_id if original_payload else None,
            watchlist_id=None,
        )

        retry_task = create_market_data_sync_task(retry_payload)
        return {
            "task_id": task_id,
            "retried": True,
            "retry_task_id": retry_task.get("id"),
            "failed_count": len(failed_symbol_ids),
            "message": f"已创建重试任务，重新同步 {len(failed_symbol_ids)} 个失败标的",
        }
    finally:
        db.close()
