from __future__ import annotations

import csv
import io
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.dialect import days_since
from app.db.manager import DatabaseManager
from app.db.session import get_db
from app.models.daily_bar import DailyBar
from app.models.async_task import AsyncTaskRecord
from app.models.discovery import DiscoveryTaskRecord
from app.models.journal_entry import JournalEntry
from app.models.macro_data import MacroIndicatorValue, MacroSnapshot
from app.models.market_event import MarketEvent
from app.models.scan import ScanResult
from app.models.score import Score
from app.models.symbol import Symbol


router = APIRouter()
BAR_ISSUE_SAMPLE_LIMIT = 8


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_datetime(value) -> datetime | None:
    """将可能是字符串的 datetime 值安全转为 datetime 对象。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _parse_date(value) -> date | None:
    """将可能是字符串的 date 值安全转为 date 对象。"""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return None


def _age_days(value) -> int | None:
    if value is None:
        return None
    value = _parse_date(value) if not isinstance(value, (date, datetime)) else value
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        return None
    today = _now().date()
    return max(0, (today - value).days)


def _status_from_score(score: int) -> str:
    if score >= 82:
        return "ok"
    if score >= 58:
        return "warn"
    return "error"


def _format_bar_issue_row(row) -> dict:
    age_days = _age_days(row.latest_trade_date)
    return {
        "symbol_id": row.id,
        "symbol": row.symbol,
        "name": row.name,
        "asset_type": row.asset_type,
        "market": row.market,
        "theme": row.theme,
        "latest_trade_date": row.latest_trade_date,
        "latest_age_days": age_days,
        "reason": "missing_bars" if row.latest_trade_date is None else "stale_bars",
    }


@router.get("/system/capabilities")
def get_capabilities(db: Session = Depends(get_db)):
    """聚合查询所有功能的就绪状态、前置条件、推荐操作（WP-S.7）。

    返回 `CapabilitiesResponse`，包含 8 个域的就绪状态：
    基础数据采集 / 评分配置激活 / Ridge 因子仓库 / 机会扫描快照 /
    组合操作前 / 自动交易前 / AI 配置 / 外部消息渠道。

    每项返回 `status`（ready/degraded/blocked）、`reason_code`、`user_message`、
    `prerequisites`、`recommended_actions`、`data_cutoff_at`。
    前端可基于此做按钮门禁（禁用时显示原因 + "去完成前置条件"入口）。
    """
    from app.services.capability_gates import get_all_capabilities
    return get_all_capabilities(db).model_dump(mode="json")


@router.get("/system/data-health")
def get_data_health(db: Session = Depends(get_db)):
    today = _now().date()
    stale_bar_cutoff = today - timedelta(days=7)
    recent_event_cutoff = _now() - timedelta(days=7)

    region_expr = case(
        (Symbol.market.in_(("sh", "sz", "bj", "cn", "SH", "SZ", "BJ", "CN")), "cn"),
        (Symbol.market.in_(("us", "nasdaq", "nyse", "amex", "US", "NASDAQ", "NYSE", "AMEX")), "us"),
        else_="other",
    )

    total_symbols = db.execute(select(func.count(Symbol.id)).where(Symbol.is_active == 1)).scalar_one()
    symbol_rows = db.execute(
        select(region_expr.label("region"), Symbol.asset_type, func.count(Symbol.id))
        .where(Symbol.is_active == 1)
        .group_by(region_expr, Symbol.asset_type)
    ).all()
    by_region: dict[str, int] = {"cn": 0, "us": 0, "other": 0}
    by_asset: dict[str, int] = {}
    for region, asset_type, count in symbol_rows:
        by_region[str(region)] = by_region.get(str(region), 0) + int(count)
        by_asset[str(asset_type)] = by_asset.get(str(asset_type), 0) + int(count)

    latest_bar_subq = (
        select(DailyBar.symbol_id, func.max(DailyBar.trade_date).label("latest_trade_date"))
        .group_by(DailyBar.symbol_id)
        .subquery()
    )
    latest_bar_date = _parse_date(db.execute(select(func.max(DailyBar.trade_date))).scalar_one())
    bars_total = db.execute(select(func.count(DailyBar.id))).scalar_one()
    covered_symbols = db.execute(
        select(func.count(Symbol.id))
        .join(latest_bar_subq, latest_bar_subq.c.symbol_id == Symbol.id)
        .where(Symbol.is_active == 1)
    ).scalar_one()
    missing_bar_symbols = db.execute(
        select(func.count(Symbol.id))
        .outerjoin(latest_bar_subq, latest_bar_subq.c.symbol_id == Symbol.id)
        .where(Symbol.is_active == 1, latest_bar_subq.c.latest_trade_date.is_(None))
    ).scalar_one()
    outdated_bar_symbols = db.execute(
        select(func.count(Symbol.id))
        .join(latest_bar_subq, latest_bar_subq.c.symbol_id == Symbol.id)
        .where(Symbol.is_active == 1, latest_bar_subq.c.latest_trade_date < stale_bar_cutoff)
    ).scalar_one()
    stale_symbols = missing_bar_symbols + outdated_bar_symbols
    coverage_pct = round((covered_symbols / total_symbols) * 100, 2) if total_symbols else 0.0
    stale_pct = round((stale_symbols / total_symbols) * 100, 2) if total_symbols else 0.0
    missing_samples = db.execute(
        select(
            Symbol.id,
            Symbol.symbol,
            Symbol.name,
            Symbol.asset_type,
            Symbol.market,
            Symbol.theme,
            latest_bar_subq.c.latest_trade_date,
        )
        .outerjoin(latest_bar_subq, latest_bar_subq.c.symbol_id == Symbol.id)
        .where(Symbol.is_active == 1, latest_bar_subq.c.latest_trade_date.is_(None))
        .order_by(Symbol.asset_type.asc(), Symbol.symbol.asc())
        .limit(BAR_ISSUE_SAMPLE_LIMIT)
    ).all()
    stale_samples = db.execute(
        select(
            Symbol.id,
            Symbol.symbol,
            Symbol.name,
            Symbol.asset_type,
            Symbol.market,
            Symbol.theme,
            latest_bar_subq.c.latest_trade_date,
        )
        .join(latest_bar_subq, latest_bar_subq.c.symbol_id == Symbol.id)
        .where(Symbol.is_active == 1, latest_bar_subq.c.latest_trade_date < stale_bar_cutoff)
        .order_by(latest_bar_subq.c.latest_trade_date.asc(), Symbol.symbol.asc())
        .limit(BAR_ISSUE_SAMPLE_LIMIT)
    ).all()
    latest_score_date = _parse_date(db.execute(select(func.max(Score.trade_date))).scalar_one())
    # 只统计当前 is_active=1 标的的评分数量，避免已清理的僵尸标的评分记录污染覆盖率诊断
    scored_symbols = db.execute(
        select(func.count(func.distinct(Score.symbol_id)))
        .join(Symbol, Symbol.id == Score.symbol_id)
        .where(Symbol.is_active == 1)
    ).scalar_one()

    latest_macro_at = _parse_datetime(db.execute(select(func.max(MacroIndicatorValue.updated_at))).scalar_one())
    latest_macro_snapshot = db.execute(select(MacroSnapshot).order_by(MacroSnapshot.id.desc())).scalars().first()
    macro_indicator_total = db.execute(select(func.count(MacroIndicatorValue.id))).scalar_one()

    latest_event_at = _parse_datetime(db.execute(select(func.max(func.coalesce(MarketEvent.published_at, MarketEvent.created_at)))).scalar_one())
    events_7d = db.execute(
        select(func.count(MarketEvent.id)).where(func.coalesce(MarketEvent.published_at, MarketEvent.created_at) >= recent_event_cutoff)
    ).scalar_one()
    important_events_7d = db.execute(
        select(func.count(MarketEvent.id)).where(
            func.coalesce(MarketEvent.published_at, MarketEvent.created_at) >= recent_event_cutoff,
            MarketEvent.importance_level >= 4,
        )
    ).scalar_one()

    latest_task = db.execute(select(DiscoveryTaskRecord).order_by(DiscoveryTaskRecord.updated_at.desc())).scalars().first()
    frozen_results = db.execute(select(func.count(ScanResult.id)).where(ScanResult.is_frozen == 1)).scalar_one()
    warning_results = db.execute(
        select(func.count(ScanResult.id)).where(
            ScanResult.is_frozen == 0,
            days_since(ScanResult.created_at) >= ScanResult.warning_days,
            days_since(ScanResult.created_at) < ScanResult.valid_days,
        )
    ).scalar_one()
    expired_results = db.execute(
        select(func.count(ScanResult.id)).where(
            ScanResult.is_frozen == 0,
            days_since(ScanResult.created_at) >= ScanResult.valid_days,
        )
    ).scalar_one()

    issues: list[dict[str, str]] = []
    score = 100
    if total_symbols == 0:
        issues.append({"level": "error", "message": "标的库为空，机会挖掘和同步都没有基础范围。"})
        score -= 45
    if total_symbols and coverage_pct < 60:
        issues.append({"level": "warn", "message": f"行情覆盖率 {coverage_pct:.1f}%，部分标的缺少K线。"})
        score -= 18
    if total_symbols and stale_pct > 35:
        issues.append({"level": "warn", "message": f"{stale_pct:.1f}% 标的行情超过7天未更新。"})
        score -= 16
    if latest_macro_at is None:
        issues.append({"level": "warn", "message": "宏观数据还没有同步。"})
        score -= 10
    elif (_age_days(latest_macro_at) or 0) > 35:
        issues.append({"level": "warn", "message": "宏观数据超过35天未更新。"})
        score -= 8
    if latest_event_at is None or events_7d == 0:
        issues.append({"level": "warn", "message": "近7天没有行情消息，消息面评分可信度会降低。"})
        score -= 8
    if expired_results:
        issues.append({"level": "warn", "message": f"{expired_results} 条机会结果已过有效期，建议清理或重新扫描。"})
        score -= min(12, int(expired_results))
    if latest_task and latest_task.status in {"failed", "expired"}:
        issues.append({"level": "warn", "message": f"最近一次机会挖掘状态为 {latest_task.status}。"})
        score -= 8

    score = max(0, min(100, score))

    return {
        "status": _status_from_score(score),
        "score": score,
        "updated_at": _now(),
        "issues": issues[:6],
        "symbols": {
            "total": total_symbols,
            "by_region": by_region,
            "by_asset_type": by_asset,
        },
        "bars": {
            "total": bars_total,
            "covered_symbols": covered_symbols,
            "coverage_pct": coverage_pct,
            "latest_trade_date": latest_bar_date,
            "latest_age_days": _age_days(latest_bar_date),
            "missing_symbols": missing_bar_symbols,
            "outdated_symbols": outdated_bar_symbols,
            "stale_symbols": stale_symbols,
            "stale_pct": stale_pct,
            "stale_cutoff": stale_bar_cutoff,
            "repair_hint": "优先补拉 missing_samples 和 stale_samples 中的标的，再重新运行机会扫描。",
            "missing_samples": [_format_bar_issue_row(row) for row in missing_samples],
            "stale_samples": [_format_bar_issue_row(row) for row in stale_samples],
        },
        "scores": {
            "scored_symbols": scored_symbols,
            "latest_trade_date": latest_score_date,
            "latest_age_days": _age_days(latest_score_date),
        },
        "macro": {
            "indicators_total": macro_indicator_total,
            "latest_updated_at": latest_macro_at,
            "latest_age_days": _age_days(latest_macro_at),
            "market_score": latest_macro_snapshot.market_score if latest_macro_snapshot else None,
            "failed_total": latest_macro_snapshot.failed_total if latest_macro_snapshot else 0,
        },
        "market_events": {
            "latest_at": latest_event_at,
            "latest_age_days": _age_days(latest_event_at),
            "events_7d": events_7d,
            "important_events_7d": important_events_7d,
        },
        "discovery": {
            "latest_task": None
            if latest_task is None
            else {
                "id": latest_task.id,
                "status": latest_task.status,
                "stage": latest_task.stage,
                "percent": latest_task.percent,
                "total": latest_task.total,
                "processed": latest_task.processed,
                "updated_at": latest_task.updated_at,
            },
            "warning_results": warning_results,
            "expired_results": expired_results,
            "frozen_results": frozen_results,
        },
    }


@router.get("/system/data-health/symbols/{symbol_id}")
def get_symbol_data_health(symbol_id: int, db: Session = Depends(get_db)):
    """标的维度的数据覆盖诊断：按 1月/1季/1年/3年 展示覆盖率，区分问题类型，给出修复建议。"""
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Symbol not found")

    today = _now().date()
    stale_cutoff = today - timedelta(days=7)

    periods = {
        "1m": today - timedelta(days=30),
        "1q": today - timedelta(days=90),
        "1y": today - timedelta(days=365),
        "3y": today - timedelta(days=1095),
    }

    bar_stats = db.execute(
        select(
            func.count(DailyBar.id),
            func.min(DailyBar.trade_date),
            func.max(DailyBar.trade_date),
        ).where(DailyBar.symbol_id == symbol_id)
    ).one()
    total_bars = int(bar_stats[0] or 0)
    earliest_bar = _parse_date(bar_stats[1])
    latest_bar = _parse_date(bar_stats[2])

    coverage: dict[str, dict] = {}
    for label, start_date in periods.items():
        end_date = today
        bar_count = db.execute(
            select(func.count(DailyBar.id)).where(
                DailyBar.symbol_id == symbol_id,
                DailyBar.trade_date >= start_date,
                DailyBar.trade_date <= end_date,
            )
        ).scalar_one()
        trading_days_approx = min((today - start_date).days, (today - (earliest_bar or today)).days) if earliest_bar and earliest_bar > start_date else (today - start_date).days
        trading_days_approx = max(1, int(trading_days_approx * 5 / 7))
        actual = int(bar_count)
        coverage_pct = round(min(100, (actual / trading_days_approx) * 100), 1) if trading_days_approx > 0 else 0.0
        coverage[label] = {
            "bar_count": actual,
            "expected_bars": trading_days_approx,
            "coverage_pct": coverage_pct,
        }

    score_stats = db.execute(
        select(
            func.count(Score.id),
            func.max(Score.trade_date),
        ).where(Score.symbol_id == symbol_id)
    ).one()
    total_scores = int(score_stats[0] or 0)
    latest_score = _parse_date(score_stats[1])

    score_coverage: dict[str, dict] = {}
    for label, start_date in periods.items():
        end_date = today
        score_count = db.execute(
            select(func.count(Score.id)).where(
                Score.symbol_id == symbol_id,
                Score.trade_date >= start_date,
                Score.trade_date <= end_date,
            )
        ).scalar_one()
        bar_count = coverage[label]["bar_count"]
        score_coverage[label] = {
            "score_count": int(score_count),
            "bar_count": bar_count,
            "coverage_pct": round((int(score_count) / bar_count) * 100, 1) if bar_count > 0 else 0.0,
        }

    issues: list[dict] = []
    if total_bars == 0:
        issues.append({"type": "missing_bars", "severity": "error", "message": "没有任何K线数据", "period": "all"})
    else:
        if latest_bar and latest_bar < stale_cutoff:
            age = (today - latest_bar).days
            issues.append({"type": "stale_bars", "severity": "warn", "message": f"K线数据已过期，最后更新距今 {age} 天", "period": "all", "latest_date": latest_bar.isoformat()})
        for label, cov in coverage.items():
            if cov["coverage_pct"] < 50:
                issues.append({"type": "low_coverage", "severity": "warn", "message": f"近{label}K线覆盖率仅 {cov['coverage_pct']}%", "period": label})

    if total_scores == 0 and total_bars > 0:
        issues.append({"type": "missing_scores", "severity": "warn", "message": "有K线但没有任何评分数据", "period": "all"})
    else:
        for label, sc in score_coverage.items():
            if sc["bar_count"] > 0 and sc["coverage_pct"] < 60:
                issues.append({"type": "low_score_coverage", "severity": "warn", "message": f"近{label}评分覆盖率仅 {sc['coverage_pct']}%", "period": label})

    suggestions: list[str] = []
    if total_bars == 0:
        suggestions.append("建议先初始化该标的的历史K线数据")
    elif any(i["type"] == "stale_bars" for i in issues):
        suggestions.append("建议补拉最近7天的K线数据")
    if any(i["type"] in ("missing_scores", "low_score_coverage") for i in issues):
        suggestions.append("建议重新计算评分")

    return {
        "symbol_id": symbol.id,
        "symbol": symbol.symbol,
        "name": symbol.name,
        "asset_type": symbol.asset_type,
        "market": symbol.market,
        "summary": {
            "total_bars": total_bars,
            "earliest_bar": earliest_bar.isoformat() if earliest_bar else None,
            "latest_bar": latest_bar.isoformat() if latest_bar else None,
            "latest_age_days": _age_days(latest_bar),
            "total_scores": total_scores,
            "latest_score": latest_score.isoformat() if latest_score else None,
            "score_age_days": _age_days(latest_score),
        },
        "coverage": coverage,
        "score_coverage": score_coverage,
        "issues": issues,
        "suggestions": suggestions,
    }


@router.get("/system/tasks")
def list_unified_tasks(
    task_type: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """统一任务历史：聚合 async_tasks 和 discovery_tasks，按时间倒序。"""
    import json as _json

    items: list[dict] = []

    # --- async_tasks (market_data_sync / history_initialization) ---
    stmt_async = select(AsyncTaskRecord).order_by(AsyncTaskRecord.created_at.desc()).limit(limit)
    for row in db.execute(stmt_async).scalars().all():
        errors = []
        if row.errors_json:
            try:
                errors = _json.loads(row.errors_json)
            except Exception:
                pass
        result = {}
        if row.result_json:
            try:
                result = _json.loads(row.result_json)
            except Exception:
                pass
        payload = {}
        if row.payload_json:
            try:
                payload = _json.loads(row.payload_json)
            except Exception:
                pass

        duration_sec = None
        if row.started_at and row.finished_at:
            duration_sec = round((row.finished_at - row.started_at).total_seconds(), 1)

        items.append({
            "id": row.id,
            "source": "async",
            "task_type": row.task_type,
            "status": row.status,
            "stage": row.stage,
            "percent": round(row.percent, 1),
            "message": row.message or "",
            "total": row.total,
            "processed": row.processed,
            "ok_count": row.ok_count,
            "failed_count": row.failed_count,
            "current_item": row.current_item,
            "payload": payload,
            "result": result,
            "errors": errors[:10],
            "duration_sec": duration_sec,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        })

    # --- discovery_tasks ---
    stmt_disc = select(DiscoveryTaskRecord).order_by(DiscoveryTaskRecord.created_at.desc()).limit(limit)
    for row in db.execute(stmt_disc).scalars().all():
        errors = []
        if row.errors_json:
            try:
                errors = _json.loads(row.errors_json)
            except Exception:
                pass
        payload = {}
        if row.payload_json:
            try:
                payload = _json.loads(row.payload_json)
            except Exception:
                pass

        duration_sec = None
        if row.started_at and row.finished_at:
            duration_sec = round((row.finished_at - row.started_at).total_seconds(), 1)

        items.append({
            "id": row.id,
            "source": "discovery",
            "task_type": "discovery_mining",
            "status": row.status,
            "stage": row.stage,
            "percent": round(row.percent, 1),
            "message": row.message or "",
            "total": row.total,
            "processed": row.processed,
            "ok_count": row.ok_count,
            "failed_count": row.failed_count,
            "current_item": row.current_symbol,
            "payload": {
                "scope": row.scope,
                "min_score": row.min_score,
                "batch_size": row.batch_size,
                "include_news": bool(row.include_news),
                **payload,
            },
            "result": {
                "executable_count": row.executable_count,
                "scored_count": row.scored_count,
                "empty_count": row.empty_count,
                "news_symbols_total": row.news_symbols_total,
            },
            "errors": errors[:10],
            "duration_sec": duration_sec,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        })

    # Sort all items by created_at descending
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    # Filter by task_type if specified
    if task_type:
        items = [i for i in items if i["task_type"] == task_type]

    return {"tasks": items[:limit]}


@router.post("/system/backup")
def backup_database():
    """一键备份 SQLite 数据库到临时目录（仅 SQLite 模式可用）"""
    mgr = DatabaseManager.get()
    if mgr.is_mysql:
        raise HTTPException(400, "MySQL 模式下暂不支持文件备份，请使用 mysqldump 工具")
    db_path = settings.database_url.replace("sqlite:///", "")
    backup_dir = Path(tempfile.gettempdir()) / "quant_backups"
    backup_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"quant_workbench_backup_{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    return {
        "status": "ok",
        "backup_path": str(backup_path),
        "timestamp": timestamp,
        "size_mb": round(backup_path.stat().st_size / 1024 / 1024, 2),
    }


@router.get("/system/backups")
def list_backups():
    """列出所有备份"""
    backup_dir = Path(tempfile.gettempdir()) / "quant_backups"
    if not backup_dir.exists():
        return {"backups": []}
    backups = []
    for f in sorted(backup_dir.glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
        stat = f.stat()
        backups.append(
            {
                "filename": f.name,
                "path": str(f),
                "size_mb": round(stat.st_size / 1024 / 1024, 2),
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )
    return {"backups": backups}


@router.post("/system/restore")
def restore_database(backup_path: str = Query(...)):
    """从备份恢复数据库（仅 SQLite 模式可用）"""
    mgr = DatabaseManager.get()
    if mgr.is_mysql:
        raise HTTPException(400, "MySQL 模式下暂不支持文件恢复，请使用 mysql 客户端导入")
    db_path = settings.database_url.replace("sqlite:///", "")
    if not Path(backup_path).exists():
        raise HTTPException(status_code=404, detail="Backup file not found")
    shutil.copy2(backup_path, db_path)
    return {"status": "ok", "restored_from": backup_path}


@router.get("/system/export/{data_type}")
def export_data(data_type: str, portfolio_id: int = Query(default=1), db: Session = Depends(get_db)):
    """导出数据为 CSV
    data_type: journals | positions | scan_results | trade_setups
    """
    output = io.StringIO()
    writer = csv.writer(output)

    if data_type == "journals":
        rows = db.execute(select(JournalEntry).where(JournalEntry.portfolio_id == portfolio_id)).scalars().all()
        writer.writerow(["id", "symbol_id", "title", "entry_type", "stage", "action", "actual_action", "outcome", "review_note", "created_at"])
        for r in rows:
            writer.writerow([r.id, r.symbol_id, r.title, r.entry_type, r.stage, r.action, r.actual_action, r.outcome, r.review_note, r.created_at])
    elif data_type == "positions":
        from app.models.portfolio import Position
        rows = db.execute(select(Position).where(Position.portfolio_id == portfolio_id)).scalars().all()
        writer.writerow(["id", "symbol_id", "quantity", "avg_cost", "latest_price", "market_value", "position_pct", "asset_type", "theme"])
        for r in rows:
            writer.writerow([r.id, r.symbol_id, r.quantity, r.avg_cost, r.latest_price, r.market_value, r.position_pct, r.asset_type, r.theme])
    elif data_type == "scan_results":
        rows = db.execute(select(ScanResult)).scalars().all()
        writer.writerow(["id", "symbol_id", "quality_score", "timing_score", "priority_score", "stage", "action", "is_frozen", "created_at"])
        for r in rows:
            writer.writerow([r.id, r.symbol_id, getattr(r, "quality_score", ""), getattr(r, "timing_score", ""), getattr(r, "priority_score", ""), getattr(r, "stage", ""), getattr(r, "action", ""), r.is_frozen, r.created_at])
    elif data_type == "trade_setups":
        from app.models.trade_setup import TradeSetup
        rows = db.execute(select(TradeSetup).where(TradeSetup.portfolio_id == portfolio_id)).scalars().all()
        writer.writerow(["id", "symbol_id", "stage", "action", "entry_min", "entry_max", "stop_loss", "target_price", "recommended_position_pct", "risk_reward_ratio", "created_at"])
        for r in rows:
            writer.writerow([r.id, r.symbol_id, r.stage, r.action, r.entry_min, r.entry_max, r.stop_loss, r.target_price, r.recommended_position_pct, r.risk_reward_ratio, r.created_at])
    else:
        raise HTTPException(status_code=400, detail="Invalid data_type. Use: journals, positions, scan_results, trade_setups")

    content = output.getvalue()
    output.close()
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={data_type}_export.csv"},
    )
