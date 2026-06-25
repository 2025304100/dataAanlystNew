from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.daily_bar import DailyBar
from app.models.discovery import DiscoveryTaskRecord
from app.models.macro_data import MacroIndicatorValue, MacroSnapshot
from app.models.market_event import MarketEvent
from app.models.scan import ScanResult
from app.models.score import Score
from app.models.symbol import Symbol


router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _age_days(value: date | datetime | None) -> int | None:
    if value is None:
        return None
    today = _now().date()
    if isinstance(value, datetime):
        value = value.date()
    return max(0, (today - value).days)


def _status_from_score(score: int) -> str:
    if score >= 82:
        return "ok"
    if score >= 58:
        return "warn"
    return "error"


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
    latest_bar_date = db.execute(select(func.max(DailyBar.trade_date))).scalar_one()
    bars_total = db.execute(select(func.count(DailyBar.id))).scalar_one()
    covered_symbols = db.execute(select(func.count()).select_from(latest_bar_subq)).scalar_one()
    stale_symbols = db.execute(
        select(func.count(Symbol.id))
        .outerjoin(latest_bar_subq, latest_bar_subq.c.symbol_id == Symbol.id)
        .where(
            Symbol.is_active == 1,
            (latest_bar_subq.c.latest_trade_date.is_(None)) | (latest_bar_subq.c.latest_trade_date < stale_bar_cutoff),
        )
    ).scalar_one()
    coverage_pct = round((covered_symbols / total_symbols) * 100, 2) if total_symbols else 0.0
    stale_pct = round((stale_symbols / total_symbols) * 100, 2) if total_symbols else 0.0

    latest_score_date = db.execute(select(func.max(Score.trade_date))).scalar_one()
    scored_symbols = db.execute(select(func.count(func.distinct(Score.symbol_id)))).scalar_one()

    latest_macro_at = db.execute(select(func.max(MacroIndicatorValue.updated_at))).scalar_one()
    latest_macro_snapshot = db.execute(select(MacroSnapshot).order_by(MacroSnapshot.id.desc())).scalars().first()
    macro_indicator_total = db.execute(select(func.count(MacroIndicatorValue.id))).scalar_one()

    latest_event_at = db.execute(select(func.max(func.coalesce(MarketEvent.published_at, MarketEvent.created_at)))).scalar_one()
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
            func.julianday(func.current_timestamp()) - func.julianday(ScanResult.created_at) >= ScanResult.warning_days,
            func.julianday(func.current_timestamp()) - func.julianday(ScanResult.created_at) < ScanResult.valid_days,
        )
    ).scalar_one()
    expired_results = db.execute(
        select(func.count(ScanResult.id)).where(
            ScanResult.is_frozen == 0,
            func.julianday(func.current_timestamp()) - func.julianday(ScanResult.created_at) >= ScanResult.valid_days,
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
            "stale_symbols": stale_symbols,
            "stale_pct": stale_pct,
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
