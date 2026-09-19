"""组合每日净值快照服务（P0-9 / P0-10）。

职责：
1. 由 scheduled_tasks 每日收盘后触发，遍历所有 account_type="simulated" 的组合
   调用 build_sim_account_summary 获取当前 cash/market_value/total_equity/realized_pnl/unrealized_pnl，
   查询前一日 snapshot 计算 daily_return，写入 PortfolioEquitySnapshot（Upsert 语义）
2. 提供 list_portfolio_equity_snapshots 供 API 查询时序净值

设计要点：
- 历史不可回溯：snapshot_date 取系统当前 UTC 日期，不支持补写历史
- Upsert：同 portfolio_id + snapshot_date 重复则更新（防止定时任务重跑或手动触发重复）
- daily_return：相对前一日 total_equity；首日或前一日缺失为 0
- 进度平滑：queued=0% → fetch=10% → loop 按比例 30→90% → done=100%
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.models.portfolio import Portfolio
from app.models.portfolio_equity_snapshot import PortfolioEquitySnapshot
from app.services.async_tasks import (
    _append_error,
    _now,
    _set_task,
    _start_worker,
    create_async_task,
    list_async_tasks,
)
from app.services.sim_accounts import build_sim_account_summary
from app.services.portfolio_risk_notifications import evaluate_snapshot_risk_notifications


logger = logging.getLogger(__name__)
TASK_TYPE = "portfolio_equity_snapshot"


def _round_money(value: float) -> float:
    """统一保留 4 位小数，与 sim_accounts 一致。"""
    return round(float(value or 0.0), 4)


def _snapshot_date_for(now_utc: datetime | None = None) -> date:
    """快照日期取 UTC 日期（与 DailyBar.trade_date 对齐）。"""
    return (now_utc or datetime.now(timezone.utc)).date()


def _find_latest_snapshot_before(
    db: Session, portfolio_id: int, before_date: date
) -> PortfolioEquitySnapshot | None:
    """查询指定日期之前最近的一条快照（用于计算 daily_return）。"""
    return db.execute(
        select(PortfolioEquitySnapshot)
        .where(
            PortfolioEquitySnapshot.portfolio_id == portfolio_id,
            PortfolioEquitySnapshot.snapshot_date < before_date,
        )
        .order_by(desc(PortfolioEquitySnapshot.snapshot_date))
        .limit(1)
    ).scalars().first()


def upsert_snapshot(
    db: Session,
    *,
    portfolio_id: int,
    snapshot_date: date,
    cash_balance: float,
    market_value: float,
    total_equity: float,
    realized_pnl: float,
    unrealized_pnl: float,
    position_count: int,
) -> PortfolioEquitySnapshot:
    """写入或更新一条快照。返回最终落库的实例。

    daily_return = (今日 total_equity - 前一日 total_equity) / 前一日 total_equity
    若前一日无快照或前一日 total_equity 为 0，则 daily_return = 0。
    """
    prev = _find_latest_snapshot_before(db, portfolio_id, snapshot_date)
    if prev and prev.total_equity:
        daily_return = (total_equity - prev.total_equity) / prev.total_equity
    else:
        daily_return = 0.0
    daily_return = _round_money(daily_return)

    existing = db.execute(
        select(PortfolioEquitySnapshot).where(
            PortfolioEquitySnapshot.portfolio_id == portfolio_id,
            PortfolioEquitySnapshot.snapshot_date == snapshot_date,
        )
    ).scalars().first()

    if existing is not None:
        # Upsert：同日重复则更新（保留 id，避免唯一约束冲突）
        existing.cash_balance = _round_money(cash_balance)
        existing.market_value = _round_money(market_value)
        existing.total_equity = _round_money(total_equity)
        existing.realized_pnl = _round_money(realized_pnl)
        existing.unrealized_pnl = _round_money(unrealized_pnl)
        existing.daily_return = daily_return
        existing.position_count = int(position_count)
        db.flush()
        return existing

    snap = PortfolioEquitySnapshot(
        portfolio_id=portfolio_id,
        snapshot_date=snapshot_date,
        cash_balance=_round_money(cash_balance),
        market_value=_round_money(market_value),
        total_equity=_round_money(total_equity),
        realized_pnl=_round_money(realized_pnl),
        unrealized_pnl=_round_money(unrealized_pnl),
        daily_return=daily_return,
        position_count=int(position_count),
    )
    db.add(snap)
    db.flush()
    return snap


def snapshot_all_simulated_portfolios(db: Session) -> dict:
    """遍历所有模拟组合写入当日快照。返回统计信息。"""
    portfolios = db.execute(
        select(Portfolio).where(Portfolio.account_type == "simulated")
    ).scalars().all()

    today = _snapshot_date_for()
    written = 0
    updated = 0
    skipped = 0
    errors: list[dict] = []

    for portfolio in portfolios:
        try:
            summary = build_sim_account_summary(db, portfolio)
            # 是否已存在（用于统计 written vs updated）
            existed = db.execute(
                select(PortfolioEquitySnapshot.id).where(
                    PortfolioEquitySnapshot.portfolio_id == portfolio.id,
                    PortfolioEquitySnapshot.snapshot_date == today,
                )
            ).scalar_one_or_none()
            snapshot = upsert_snapshot(
                db,
                portfolio_id=portfolio.id,
                snapshot_date=today,
                cash_balance=summary["cash_balance"],
                market_value=summary["market_value"],
                total_equity=summary["total_equity"],
                realized_pnl=summary["realized_pnl"],
                unrealized_pnl=summary["unrealized_pnl"],
                position_count=summary["position_count"],
            )
            try:
                evaluate_snapshot_risk_notifications(
                    db, portfolio=portfolio, snapshot=snapshot
                )
            except Exception:
                logger.warning(
                    "Risk notification evaluation failed for portfolio %s",
                    portfolio.id,
                    exc_info=True,
                )
            if existed:
                updated += 1
            else:
                written += 1
        except Exception as exc:
            # 单组合失败不影响其他组合
            logger.exception("Snapshot failed for portfolio %s", portfolio.id)
            errors.append({"portfolio_id": portfolio.id, "error": str(exc)})
            skipped += 1

    db.commit()
    return {
        "snapshot_date": today.isoformat(),
        "total": len(portfolios),
        "written": written,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }


def create_portfolio_equity_snapshot_task():
    """调度入口：去重后启动后台 worker。"""
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in {"queued", "running"}:
        return existing[0]
    task = create_async_task(TASK_TYPE, {})
    _start_worker(task.id, _run_snapshot_task)
    return task


def _run_snapshot_task(task_id: str) -> None:
    """worker：实际执行快照写入，带进度跟踪与终态保护。"""
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return
        _set_task(
            db,
            task_id,
            status="running",
            stage="fetch",
            percent=10,
            message="Fetching simulated portfolios",
            started_at=_now(),
        )

        portfolios = db.execute(
            select(Portfolio).where(Portfolio.account_type == "simulated")
        ).scalars().all()
        total = len(portfolios)
        today = _snapshot_date_for()

        if total == 0:
            _set_task(
                db,
                task_id,
                status="done",
                stage="done",
                percent=100,
                total=0,
                processed=0,
                ok_count=0,
                failed_count=0,
                message="No simulated portfolios to snapshot",
                result_json=json.dumps(
                    {"snapshot_date": today.isoformat(), "total": 0, "written": 0, "updated": 0, "skipped": 0},
                    ensure_ascii=False,
                ),
                finished_at=_now(),
            )
            return

        written = 0
        updated = 0
        skipped = 0
        errors: list[dict] = []
        # 进度区间 30% → 90%，按组合数线性
        for idx, portfolio in enumerate(portfolios):
            # 每次循环前重新检查取消信号
            task = db.get(AsyncTaskRecord, task_id)
            if task is None or task.status == "cancelled":
                return
            try:
                summary = build_sim_account_summary(db, portfolio)
                existed = db.execute(
                    select(PortfolioEquitySnapshot.id).where(
                        PortfolioEquitySnapshot.portfolio_id == portfolio.id,
                        PortfolioEquitySnapshot.snapshot_date == today,
                    )
                ).scalar_one_or_none()
                snapshot = upsert_snapshot(
                    db,
                    portfolio_id=portfolio.id,
                    snapshot_date=today,
                    cash_balance=summary["cash_balance"],
                    market_value=summary["market_value"],
                    total_equity=summary["total_equity"],
                    realized_pnl=summary["realized_pnl"],
                    unrealized_pnl=summary["unrealized_pnl"],
                    position_count=summary["position_count"],
                )
                try:
                    evaluate_snapshot_risk_notifications(
                        db, portfolio=portfolio, snapshot=snapshot
                    )
                except Exception:
                    logger.warning(
                        "Risk notification evaluation failed for portfolio %s",
                        portfolio.id,
                        exc_info=True,
                    )
                if existed:
                    updated += 1
                else:
                    written += 1
            except Exception as exc:
                db.rollback()
                logger.exception("Snapshot failed for portfolio %s", portfolio.id)
                errors.append({"portfolio_id": portfolio.id, "error": str(exc)})
                skipped += 1
                # rollback 后 portfolio 对象可能 detached，重新获取避免后续循环引用问题
                continue

            # 进度平滑：避免单组合大幅跳跃
            percent = 30 + int((idx + 1) / total * 60)
            _set_task(
                db,
                task_id,
                stage="snapshot",
                percent=min(percent, 90),
                total=total,
                processed=idx + 1,
                ok_count=written + updated,
                failed_count=skipped,
                message=f"Snapshotted {idx + 1}/{total} portfolios",
            )

        db.commit()
        # 终态检查（防止取消信号被覆盖）
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return
        _set_task(
            db,
            task_id,
            status="done",
            stage="done",
            percent=100,
            total=total,
            processed=total,
            ok_count=written + updated,
            failed_count=skipped,
            message="Portfolio equity snapshot completed",
            result_json=json.dumps(
                {
                    "snapshot_date": today.isoformat(),
                    "total": total,
                    "written": written,
                    "updated": updated,
                    "skipped": skipped,
                    "errors": errors[:20],
                },
                ensure_ascii=False,
                default=str,
            ),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Portfolio equity snapshot task failed: %s", task_id)
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None:
            _append_error(task, {"scope": "portfolio_equity_snapshot", "error": str(exc)})
            task.status = "failed"
            task.stage = "failed"
            task.message = str(exc)
            task.finished_at = _now()
            task.updated_at = _now()
            db.commit()
    finally:
        db.close()


def list_portfolio_equity_snapshots(
    db: Session,
    portfolio_id: int,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 400,
) -> list[PortfolioEquitySnapshot]:
    """按时间升序返回组合的净值快照（供 API 与绩效统计使用）。"""
    stmt = (
        select(PortfolioEquitySnapshot)
        .where(PortfolioEquitySnapshot.portfolio_id == portfolio_id)
        .order_by(PortfolioEquitySnapshot.snapshot_date.asc())
    )
    if start_date is not None:
        stmt = stmt.where(PortfolioEquitySnapshot.snapshot_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(PortfolioEquitySnapshot.snapshot_date <= end_date)
    if limit and limit > 0:
        stmt = stmt.limit(int(limit))
    return list(db.execute(stmt).scalars().all())


def snapshot_to_dict(snap: PortfolioEquitySnapshot) -> dict:
    """序列化为 API 响应。"""
    return {
        "id": snap.id,
        "portfolio_id": snap.portfolio_id,
        "snapshot_date": snap.snapshot_date.isoformat() if snap.snapshot_date else None,
        "cash_balance": float(snap.cash_balance or 0),
        "market_value": float(snap.market_value or 0),
        "total_equity": float(snap.total_equity or 0),
        "realized_pnl": float(snap.realized_pnl or 0),
        "unrealized_pnl": float(snap.unrealized_pnl or 0),
        "daily_return": float(snap.daily_return or 0),
        "position_count": int(snap.position_count or 0),
        "created_at": snap.created_at.isoformat() if snap.created_at else None,
    }


__all__ = [
    "TASK_TYPE",
    "create_portfolio_equity_snapshot_task",
    "list_portfolio_equity_snapshots",
    "snapshot_all_simulated_portfolios",
    "snapshot_to_dict",
    "upsert_snapshot",
]
