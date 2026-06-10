from __future__ import annotations

from statistics import mean

from sqlalchemy import asc, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.score import Score
from app.models.symbol import Symbol
from app.schemas.signal_rule import SignalRuleUpsert
from app.services.regions import markets_for_region, region_from_market
from app.services.signal_rules import get_active_signal_rule


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 4) if values else None


def _rate(values: list[bool]) -> float | None:
    return round(sum(1 for item in values if item) / len(values), 4) if values else None


def _load_forward_bars(db: Session, symbol_id: int, trade_date, horizon: int = 20) -> list[DailyBar]:
    return (
        db.execute(
            select(DailyBar)
            .where(DailyBar.symbol_id == symbol_id, DailyBar.trade_date >= trade_date)
            .order_by(asc(DailyBar.trade_date))
            .limit(horizon + 1)
        )
        .scalars()
        .all()
    )


def _score_similarity(row: Score, latest_score: Score) -> float:
    quality_gap = abs(row.quality_score - latest_score.quality_score)
    timing_gap = abs(row.timing_score - latest_score.timing_score)
    return quality_gap * 0.55 + timing_gap * 0.45


def build_similar_signal_stats(
    db: Session,
    symbol: Symbol,
    latest_score: Score | None,
    portfolio_id: int | None = None,
    rule_override: SignalRuleUpsert | None = None,
    *,
    max_samples: int = 60,
) -> dict | None:
    if latest_score is None:
        return None

    rule = rule_override if rule_override is not None else get_active_signal_rule(db, portfolio_id) if portfolio_id is not None else None
    quality_tolerance = rule.quality_tolerance if rule is not None else 12
    timing_tolerance = rule.timing_tolerance if rule is not None else 12
    max_samples = rule.max_samples if rule is not None else max_samples
    min_sample_count = rule.min_sample_count if rule is not None else 3

    stmt = select(Score, Symbol).join(Symbol, Symbol.id == Score.symbol_id).where(Score.id != latest_score.id)
    if rule is None or rule.same_stage:
        stmt = stmt.where(Score.stage == latest_score.stage)
    if rule is None or rule.same_action:
        stmt = stmt.where(Score.action == latest_score.action)
    if rule is None or rule.same_asset_type:
        stmt = stmt.where(Symbol.asset_type == symbol.asset_type)

    market_codes = markets_for_region(region_from_market(symbol.market))
    if (rule is None or rule.same_region) and market_codes:
        stmt = stmt.where(Symbol.market.in_(market_codes))
    stmt = stmt.order_by(Score.trade_date.desc(), Score.id.desc()).limit(240)

    rows = db.execute(stmt).all()
    similar_rows = [
        (row, row_symbol)
        for row, row_symbol in rows
        if abs(row.quality_score - latest_score.quality_score) <= quality_tolerance
        and abs(row.timing_score - latest_score.timing_score) <= timing_tolerance
    ]
    similar_rows.sort(key=lambda item: _score_similarity(item[0], latest_score))

    samples = []
    for row, row_symbol in similar_rows[:max_samples]:
        bars = _load_forward_bars(db, row_symbol.id, row.trade_date, horizon=20)
        if len(bars) < 2:
            continue

        entry = bars[0].close
        if entry <= 0:
            continue

        forward = bars[1:]
        horizon5 = forward[:5]
        horizon20 = forward[:20]
        sample = {
            "symbol_id": row_symbol.id,
            "symbol": row_symbol.symbol,
            "trade_date": row.trade_date.isoformat(),
            "entry_price": round(entry, 2),
            "return_5d": None,
            "return_20d": None,
            "max_gain_20d": None,
            "max_drawdown_20d": None,
        }
        if horizon5:
            sample["return_5d"] = round((horizon5[-1].close - entry) / entry, 4)
        if horizon20:
            sample["return_20d"] = round((horizon20[-1].close - entry) / entry, 4)
            sample["max_gain_20d"] = round((max(item.high for item in horizon20) - entry) / entry, 4)
            sample["max_drawdown_20d"] = round((min(item.low for item in horizon20) - entry) / entry, 4)
        samples.append(sample)

    returns_5d = [item["return_5d"] for item in samples if item["return_5d"] is not None]
    returns_20d = [item["return_20d"] for item in samples if item["return_20d"] is not None]
    max_gains_20d = [item["max_gain_20d"] for item in samples if item["max_gain_20d"] is not None]
    max_drawdowns_20d = [item["max_drawdown_20d"] for item in samples if item["max_drawdown_20d"] is not None]

    return {
        "sample_count": len(samples),
        "matched_count": len(similar_rows),
        "scope": {
            "region": region_from_market(symbol.market),
            "asset_type": symbol.asset_type,
            "stage": latest_score.stage,
            "action": latest_score.action,
            "rule_name": rule.rule_name if rule is not None else "balanced",
            "mode": rule.mode if rule is not None else "balanced",
            "quality_tolerance": quality_tolerance,
            "timing_tolerance": timing_tolerance,
            "min_sample_count": min_sample_count,
            "max_samples": max_samples,
            "same_region": True if rule is None else rule.same_region,
            "same_asset_type": True if rule is None else rule.same_asset_type,
            "same_stage": True if rule is None else rule.same_stage,
            "same_action": True if rule is None else rule.same_action,
        },
        "min_sample_count": min_sample_count,
        "win_rate_5d": _rate([value > 0 for value in returns_5d]),
        "win_rate_20d": _rate([value > 0 for value in returns_20d]),
        "avg_return_5d": _avg(returns_5d),
        "avg_return_20d": _avg(returns_20d),
        "avg_max_gain_20d": _avg(max_gains_20d),
        "avg_max_drawdown_20d": _avg(max_drawdowns_20d),
        "best_return_20d": round(max(returns_20d), 4) if returns_20d else None,
        "worst_return_20d": round(min(returns_20d), 4) if returns_20d else None,
        "recent_samples": samples[:5],
    }
