from __future__ import annotations

import json
from math import floor
from statistics import mean

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.trade_setup import TradeSetup
from app.services.allocation import compute_position_budget, get_active_rule


def _round_price(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _rolling_mean(values: list[float], window: int) -> float | None:
    if not values:
        return None
    subset = values[-window:] if len(values) >= window else values
    return round(mean(subset), 2)


def _lot_size(symbol: Symbol) -> int:
    return 100 if symbol.market in {"SH", "SZ", "BJ"} else 1


def _build_return_scenarios(
    symbol: Symbol,
    score: Score,
    setup: TradeSetup,
    last_close: float,
    high20: float | None,
) -> dict | None:
    if last_close <= 0:
        return None

    if setup.entry_min is not None and setup.entry_max is not None:
        reference_price = _round_price((setup.entry_min + setup.entry_max) / 2)
    else:
        reference_price = _round_price(setup.entry_max or setup.entry_min or last_close)
    if reference_price is None or reference_price <= 0:
        return None

    risk_unit = max(reference_price * 0.015, 0.01)
    if setup.stop_loss is not None:
        risk_unit = max(risk_unit, reference_price - setup.stop_loss)
    pessimistic_price = _round_price(setup.stop_loss or (reference_price - risk_unit))

    baseline_price = _round_price(
        setup.target_price
        or (reference_price + risk_unit * max(setup.risk_reward_ratio or 1.6, 1.0))
    )
    optimistic_anchor = (baseline_price or reference_price) + max(risk_unit * 0.8, reference_price * 0.03)
    if high20 is not None:
        optimistic_anchor = max(optimistic_anchor, high20 * 1.03)
    optimistic_price = _round_price(optimistic_anchor)

    quality_edge = (score.quality_score - 50) / 50
    timing_edge = (score.timing_score - 50) / 50
    stage_edge = {
        "start": 0.01,
        "accel": 0.05,
        "cooldown": -0.03,
        "overheat": -0.08,
    }.get(score.stage, 0.0)
    action_edge = {
        "open": 0.04,
        "hold": 0.01,
        "buy_dip": 0.02,
        "reduce": -0.04,
        "exit": -0.08,
    }.get(score.action, 0.0)
    confidence_pct = round(
        _clamp(56 + quality_edge * 12 + timing_edge * 16 + stage_edge * 100 + action_edge * 100, 35, 82),
        1,
    )
    confidence = confidence_pct / 100
    expected_price = _round_price((baseline_price or reference_price) * confidence + (pessimistic_price or reference_price) * (1 - confidence))
    if expected_price is None:
        expected_price = reference_price

    lot_size = _lot_size(symbol)
    planned_quantity = 0
    if setup.recommended_position_amount > 0:
        planned_quantity = floor(setup.recommended_position_amount / reference_price / lot_size) * lot_size
    planned_amount = round(planned_quantity * reference_price, 2)

    def build_case(name: str, exit_price: float | None) -> dict:
        exit_value = _round_price(exit_price or reference_price) or reference_price
        profit_per_share = round(exit_value - reference_price, 2)
        return_pct = round((profit_per_share / reference_price) if reference_price else 0.0, 4)
        return {
            "name": name,
            "exit_price": exit_value,
            "profit_per_share": profit_per_share,
            "return_pct": return_pct,
            "profit_amount": round(profit_per_share * planned_quantity, 2),
        }

    horizon_days = {
        "start": 25,
        "accel": 18,
        "cooldown": 15,
        "overheat": 10,
    }.get(score.stage, 20)

    return {
        "reference_price": reference_price,
        "confidence_pct": confidence_pct,
        "horizon_days": horizon_days,
        "planned_order": {
            "quantity": planned_quantity,
            "amount": planned_amount,
            "lot_size": lot_size,
        },
        "expected": build_case("expected", expected_price),
        "optimistic": build_case("optimistic", optimistic_price),
        "pessimistic": build_case("pessimistic", pessimistic_price),
    }


def get_latest_score(db: Session, symbol_id: int) -> Score | None:
    return db.execute(
        select(Score).where(Score.symbol_id == symbol_id).order_by(desc(Score.trade_date), desc(Score.id))
    ).scalars().first()


def load_recent_bars(db: Session, symbol_id: int, limit: int = 60) -> list[DailyBar]:
    bars = db.execute(
        select(DailyBar).where(DailyBar.symbol_id == symbol_id).order_by(desc(DailyBar.trade_date)).limit(limit)
    ).scalars().all()
    return list(reversed(bars))


def _build_tranches(
    stage: str,
    action: str,
    recommended_pct: float,
    recommended_amount: float,
    entry_min: float | None,
    entry_max: float | None,
    ma10: float | None,
    ma20: float | None,
    high20: float | None,
) -> list[dict]:
    if recommended_pct <= 0 or action not in {"open", "hold", "buy_dip"}:
        return []

    if stage == "start":
        plan = [
            ("Probe", 0.4, f"Break and hold above {entry_max or '-'}"),
            ("Confirm", 0.35, f"Retest near {ma10 or entry_min or '-'} and hold"),
            ("Expand", 0.25, f"Push through {high20 or entry_max or '-'} with follow-through"),
        ]
    elif stage == "accel":
        plan = [
            ("Starter", 0.35, f"Buy on pullback into {entry_min or ma10 or '-'} - {entry_max or '-'}"),
            ("Trend", 0.3, f"Add after close back above MA10 {ma10 or '-'}"),
            ("Momentum", 0.35, f"Add only if breakout extends above {high20 or entry_max or '-'}"),
        ]
    elif stage == "cooldown":
        plan = [
            ("Scout", 0.6, f"Only nibble near lower buy zone {entry_min or '-'}"),
            ("Rebuild", 0.4, f"Add only after reclaiming MA20 {ma20 or '-'}"),
        ]
    else:
        plan = [("Single", 1.0, f"Wait for valid risk/reward near {entry_min or '-'} - {entry_max or '-'}")]

    items = []
    for label, ratio, trigger in plan:
        items.append(
            {
                "label": label,
                "ratio": round(ratio, 2),
                "position_pct": round(recommended_pct * ratio, 4),
                "amount": round(recommended_amount * ratio, 2),
                "trigger": trigger,
            }
        )
    return items


def _build_future_buy_plan(
    stage: str,
    action: str,
    last_close: float,
    recommended_pct: float,
    recommended_amount: float,
    entry_min: float | None,
    entry_max: float | None,
    stop_loss: float | None,
    ma10: float | None,
    ma20: float | None,
    high20: float | None,
    low20: float | None,
) -> list[dict]:
    if last_close <= 0:
        return []

    def add_item(
        label: str,
        horizon_days: int,
        zone_min: float | None,
        zone_max: float | None,
        trigger: str,
        ratio: float,
        priority: str,
    ) -> dict:
        return {
            "label": label,
            "horizon_days": horizon_days,
            "zone_min": _round_price(zone_min),
            "zone_max": _round_price(zone_max),
            "trigger": trigger,
            "position_pct": round(recommended_pct * ratio, 4),
            "amount": round(recommended_amount * ratio, 2),
            "priority": priority,
        }

    if action in {"exit", "reduce"} or stage == "overheat":
        wait_anchor = ma20 or entry_min or last_close
        return [
            add_item(
                label="wait_cooling",
                horizon_days=10,
                zone_min=wait_anchor * 0.98,
                zone_max=wait_anchor * 1.01,
                trigger="avoid chasing; only revisit after price cools near MA20 and risk/reward improves",
                ratio=0.0,
                priority="avoid",
            )
        ]

    plan: list[dict] = []
    if entry_min is not None and entry_max is not None:
        current_in_zone = entry_min <= last_close <= entry_max
        plan.append(
            add_item(
                label="current_buy_zone" if current_in_zone else "pullback_buy_zone",
                horizon_days=3 if current_in_zone else 5,
                zone_min=entry_min,
                zone_max=entry_max,
                trigger=(
                    "price is already inside the planned buy zone; use only starter size"
                    if current_in_zone
                    else "wait for pullback into the planned buy zone without breaking the stop"
                ),
                ratio=0.35 if current_in_zone else 0.4,
                priority="high" if current_in_zone else "normal",
            )
        )

    if stage == "start":
        breakout_anchor = entry_max or high20 or last_close
        plan.append(
            add_item(
                label="breakout_retest",
                horizon_days=5,
                zone_min=breakout_anchor * 0.995,
                zone_max=breakout_anchor * 1.015,
                trigger="buy only after breakout and retest hold; skip if it gaps far above the zone",
                ratio=0.35,
                priority="normal",
            )
        )
    elif stage == "accel":
        trend_anchor = ma10 or entry_min or last_close
        plan.append(
            add_item(
                label="trend_pullback",
                horizon_days=5,
                zone_min=trend_anchor * 0.985,
                zone_max=trend_anchor * 1.005,
                trigger="add only if price pulls back near MA10 and closes back above it",
                ratio=0.35,
                priority="normal",
            )
        )
    elif stage == "cooldown":
        rebuild_anchor = ma20 or low20 or entry_min or last_close
        plan.append(
            add_item(
                label="rebuild_after_reclaim",
                horizon_days=8,
                zone_min=rebuild_anchor * 0.99,
                zone_max=rebuild_anchor * 1.02,
                trigger="only rebuild after reclaiming MA20; otherwise keep observing",
                ratio=0.25,
                priority="low",
            )
        )

    if stop_loss is not None:
        plan.append(
            add_item(
                label="invalid_below_stop",
                horizon_days=1,
                zone_min=stop_loss,
                zone_max=stop_loss,
                trigger="no buy if daily close breaks this level",
                ratio=0.0,
                priority="avoid",
            )
        )

    return plan


def upsert_trade_setup(
    db: Session,
    portfolio_id: int,
    symbol: Symbol,
    score: Score,
    scan_run_id: int | None = None,
    overrides: dict | None = None,
) -> TradeSetup:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")

    bars = load_recent_bars(db, symbol.id, limit=60)
    if not bars:
        raise ValueError("Daily bars not found")

    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    last_bar = bars[-1]
    last_close = last_bar.close
    ma10 = _rolling_mean(closes, 10) or last_close
    ma20 = _rolling_mean(closes, 20) or last_close
    high20 = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    low20 = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    range20 = max(high20 - low20, last_close * 0.04)
    stage_multiplier = {
        "start": 0.35,
        "accel": 0.2,
        "cooldown": -0.1,
        "overheat": -0.2,
    }.get(score.stage, 0.0)

    entry_anchor = max(min(last_close + range20 * stage_multiplier, high20), low20)
    entry_min = _round_price(max(low20, min(entry_anchor, ma10, last_close) * 0.99))
    entry_max = _round_price(min(high20 * 1.01, max(entry_anchor, ma10, last_close) * 1.01))
    stop_anchor = min(low20, ma20 * 0.96, last_close * 0.92)
    stop_loss = _round_price(max(stop_anchor, (entry_min or last_close) * 0.94))

    stage_rr = {
        "start": 2.2,
        "accel": 2.6,
        "cooldown": 1.4,
        "overheat": 1.1,
    }.get(score.stage, 1.8)
    risk_unit = max((entry_min or last_close) - (stop_loss or last_close * 0.96), last_close * 0.015)
    target_price = _round_price((entry_max or last_close) + risk_unit * stage_rr)

    manual_overrides: dict[str, float] = {}
    raw_overrides = overrides or {}
    for field in (
        "entry_min",
        "entry_max",
        "stop_loss",
        "target_price",
        "recommended_position_pct",
        "recommended_position_amount",
    ):
        if field not in raw_overrides or raw_overrides[field] is None:
            continue
        value = float(raw_overrides[field])
        if field == "recommended_position_pct":
            manual_overrides[field] = round(_clamp(value, 0.0, 1.0), 4)
        elif field == "recommended_position_amount":
            manual_overrides[field] = round(max(0.0, value), 2)
        else:
            manual_overrides[field] = _round_price(max(0.0, value)) or 0.0

    entry_min = manual_overrides.get("entry_min", entry_min)
    entry_max = manual_overrides.get("entry_max", entry_max)
    stop_loss = manual_overrides.get("stop_loss", stop_loss)
    target_price = manual_overrides.get("target_price", target_price)
    if entry_min is not None and entry_max is not None and entry_min > entry_max:
        entry_min, entry_max = entry_max, entry_min
        if "entry_min" in manual_overrides or "entry_max" in manual_overrides:
            manual_overrides["entry_min"] = entry_min
            manual_overrides["entry_max"] = entry_max

    risk_reward_ratio = None
    if entry_max and stop_loss and target_price and entry_max > stop_loss:
        risk_reward_ratio = round((target_price - entry_max) / (entry_max - stop_loss), 2)

    position_budget = compute_position_budget(
        db=db,
        portfolio_id=portfolio_id,
        symbol=symbol,
        stage=score.stage,
        action=score.action,
        entry_price=entry_max or last_close,
        stop_loss=stop_loss,
    )
    recommended_pct = float(position_budget["recommended_pct"])
    recommended_amount = float(position_budget["recommended_amount"])
    if "recommended_position_pct" in manual_overrides:
        recommended_pct = float(manual_overrides["recommended_position_pct"])
        if "recommended_position_amount" not in manual_overrides:
            recommended_amount = round(portfolio.total_capital * recommended_pct, 2)
    if "recommended_position_amount" in manual_overrides:
        recommended_amount = float(manual_overrides["recommended_position_amount"])
        if "recommended_position_pct" not in manual_overrides:
            recommended_pct = round(recommended_amount / portfolio.total_capital, 4) if portfolio.total_capital else 0.0

    sector_flag = bool(position_budget["is_sector_overweight"])
    asset_flag = bool(position_budget["is_asset_overweight"])
    allow_add_position = int(score.stage in {"start", "accel"} and score.action in {"open", "hold"} and position_budget["can_open"])
    setup_reason = (
        f"grade={score.quality_grade}; quality={score.quality_score}; timing={score.timing_score}; "
        f"decision={position_budget['decision']}; ma10={ma10:.2f}; ma20={ma20:.2f}; "
        f"high20={high20:.2f}; low20={low20:.2f}"
    )

    setup = db.execute(
        select(TradeSetup).where(
            TradeSetup.portfolio_id == portfolio_id,
            TradeSetup.symbol_id == symbol.id,
            TradeSetup.score_id == score.id,
        )
    ).scalars().first()
    if setup is None:
        setup = TradeSetup(
            portfolio_id=portfolio_id,
            symbol_id=symbol.id,
            score_id=score.id,
        )
        db.add(setup)

    setup.scan_run_id = scan_run_id
    setup.stage = score.stage
    setup.action = score.action
    setup.entry_min = entry_min
    setup.entry_max = entry_max
    setup.stop_loss = stop_loss
    setup.target_price = target_price
    setup.recommended_position_pct = recommended_pct
    setup.recommended_position_amount = recommended_amount
    setup.risk_reward_ratio = risk_reward_ratio
    setup.allow_add_position = allow_add_position
    setup.is_sector_overweight = int(sector_flag)
    setup.is_asset_overweight = int(asset_flag)
    setup.setup_reason = setup_reason
    field_sources = {
        "entry_min": "manual" if "entry_min" in manual_overrides else "system",
        "entry_max": "manual" if "entry_max" in manual_overrides else "system",
        "stop_loss": "manual" if "stop_loss" in manual_overrides else "system",
        "target_price": "manual" if "target_price" in manual_overrides else "system",
        "recommended_position_pct": "manual" if "recommended_position_pct" in manual_overrides else "system",
        "recommended_position_amount": "manual" if "recommended_position_amount" in manual_overrides else "system",
    }
    setup.manual_overrides_json = json.dumps(manual_overrides, ensure_ascii=False) if manual_overrides else None
    setup.field_sources_json = json.dumps(field_sources, ensure_ascii=False)
    db.flush()
    return setup

def build_trade_setup_view(
    db: Session,
    portfolio_id: int,
    symbol: Symbol,
    score: Score,
    setup: TradeSetup,
    bars: list[DailyBar] | None = None,
) -> dict:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")

    bars = bars or load_recent_bars(db, symbol.id, limit=60)
    if not bars:
        raise ValueError("Daily bars not found")

    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    last_bar = bars[-1]
    ma10 = _rolling_mean(closes, 10)
    ma20 = _rolling_mean(closes, 20)
    high20 = _round_price(max(highs[-20:]) if len(highs) >= 20 else max(highs))
    low20 = _round_price(min(lows[-20:]) if len(lows) >= 20 else min(lows))

    rule = get_active_rule(db, portfolio_id)
    stage_cap_pct = 0.0
    max_loss_per_trade_pct = None
    stage_limits = {}
    if rule is not None:
        stage_limits = json.loads(rule.stage_limits_json)
        stage_cap_pct = float(stage_limits.get(symbol.asset_type, {}).get(score.stage, 0.0))
        max_loss_per_trade_pct = float(rule.max_loss_per_trade_pct)

    position_budget = compute_position_budget(
        db=db,
        portfolio_id=portfolio_id,
        symbol=symbol,
        stage=score.stage,
        action=score.action,
        entry_price=setup.entry_max or setup.entry_min or last_bar.close,
        stop_loss=setup.stop_loss,
    )
    stage_cap_pct = float(position_budget.get("stage_limit_pct", stage_cap_pct))
    stage_cap_amount = round(portfolio.total_capital * stage_cap_pct, 2)
    risk_budget_amount = position_budget.get("risk_budget_amount")
    risk_per_share = position_budget.get("risk_per_share")
    risk_capped_shares = position_budget.get("risk_capped_shares")
    risk_capped_amount = position_budget.get("risk_capped_amount")
    position_constraints = position_budget.get("constraints", [])
    blocked_reasons = position_budget.get("blocked_reasons", [])
    current_position_pct = round(float(position_budget.get("current_position_pct", 0.0)), 4)
    current_position_amount = round(float(position_budget.get("current_position_amount", 0.0)), 2)
    remaining_stage_pct = round(max(0.0, stage_cap_pct - current_position_pct), 4)
    remaining_stage_amount = round(max(0.0, stage_cap_amount - current_position_amount), 2)

    tranches = _build_tranches(
        stage=score.stage,
        action=score.action,
        recommended_pct=setup.recommended_position_pct,
        recommended_amount=setup.recommended_position_amount,
        entry_min=setup.entry_min,
        entry_max=setup.entry_max,
        ma10=ma10,
        ma20=ma20,
        high20=high20,
    )
    future_buy_plan = _build_future_buy_plan(
        stage=score.stage,
        action=score.action,
        last_close=last_bar.close,
        recommended_pct=setup.recommended_position_pct,
        recommended_amount=setup.recommended_position_amount,
        entry_min=setup.entry_min,
        entry_max=setup.entry_max,
        stop_loss=setup.stop_loss,
        ma10=ma10,
        ma20=ma20,
        high20=high20,
        low20=low20,
    )

    add_trigger = None
    if setup.allow_add_position:
        add_trigger = (
            f"Close above MA10 {ma10} and keep risk/reward > 1.5"
            if score.stage == "accel"
            else f"Break above {high20} with follow-through"
        )

    stop_trigger = f"Any daily close below {setup.stop_loss}" if setup.stop_loss is not None else None
    trim_trigger = f"Trim into {setup.target_price} or if price stretches too far above MA20 {ma20}" if setup.target_price else None
    opening_trigger = (
        f"Open only inside {setup.entry_min} - {setup.entry_max}"
        if setup.entry_min is not None and setup.entry_max is not None
        else None
    )

    chart_signals = []
    if setup.entry_min is not None:
        chart_signals.append({"kind": "buy-zone", "label": "Buy min", "price": setup.entry_min})
    if setup.entry_max is not None:
        chart_signals.append({"kind": "buy-zone", "label": "Buy max", "price": setup.entry_max})
    if setup.stop_loss is not None:
        chart_signals.append({"kind": "stop", "label": "Stop", "price": setup.stop_loss})
    if setup.target_price is not None:
        chart_signals.append({"kind": "target", "label": "Target", "price": setup.target_price})
    if ma10 is not None:
        chart_signals.append({"kind": "moving-average", "label": "MA10", "price": ma10})
    if ma20 is not None:
        chart_signals.append({"kind": "moving-average", "label": "MA20", "price": ma20})

    execution_notes = [
        f"Stage cap {round(stage_cap_pct * 100, 1)}%, remaining {round(remaining_stage_pct * 100, 1)}%",
        f"Current position {round(current_position_pct * 100, 1)}%, current market value {current_position_amount:.2f}",
    ]
    if risk_budget_amount is not None and risk_capped_shares is not None:
        execution_notes.append(
            f"Risk budget {risk_budget_amount:.2f}, about {risk_capped_shares} shares before hitting max loss budget"
        )
    if blocked_reasons:
        execution_notes.append(f"Blocked by: {', '.join(blocked_reasons)}")

    return_scenarios = _build_return_scenarios(
        symbol=symbol,
        score=score,
        setup=setup,
        last_close=last_bar.close,
        high20=high20,
    )

    def _load_json_map(value: str | None) -> dict:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _load_json_list(value: str | None) -> list:
        if not value:
            return []
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []

    manual_overrides = _load_json_map(getattr(setup, "manual_overrides_json", None))
    field_sources = _load_json_map(getattr(setup, "field_sources_json", None))
    manual_tranches = _load_json_list(getattr(setup, "manual_tranche_plan_json", None))
    if manual_tranches:
        tranches = manual_tranches

    return {
        "id": setup.id,
        "entry_min": setup.entry_min,
        "entry_max": setup.entry_max,
        "stop_loss": setup.stop_loss,
        "target_price": setup.target_price,
        "recommended_position_pct": setup.recommended_position_pct,
        "recommended_position_amount": setup.recommended_position_amount,
        "suggested_buy_pct": position_budget.get("recommended_pct", setup.recommended_position_pct),
        "suggested_buy_amount": position_budget.get("recommended_amount", setup.recommended_position_amount),
        "risk_reward_ratio": setup.risk_reward_ratio,
        "allow_add_position": setup.allow_add_position,
        "is_sector_overweight": setup.is_sector_overweight,
        "is_asset_overweight": setup.is_asset_overweight,
        "can_open": position_budget.get("can_open", False),
        "decision": position_budget.get("decision", "wait"),
        "blocked_reasons": blocked_reasons,
        "position_constraints": position_constraints,
        "open_slots_remaining": position_budget.get("open_slots_remaining"),
        "allocation_snapshot": position_budget.get("allocation"),
        "action": setup.action,
        "stage": setup.stage,
        "setup_reason": setup.setup_reason,
        "manual_overrides_json": getattr(setup, "manual_overrides_json", None),
        "field_sources_json": getattr(setup, "field_sources_json", None),
        "manual_overrides": manual_overrides,
        "field_sources": field_sources,
        "created_at": setup.created_at.isoformat() if hasattr(setup.created_at, 'isoformat') else str(setup.created_at),
        "current_position_pct": current_position_pct,
        "current_position_amount": current_position_amount,
        "stage_cap_pct": round(stage_cap_pct, 4),
        "stage_cap_amount": stage_cap_amount,
        "remaining_stage_pct": remaining_stage_pct,
        "remaining_stage_amount": remaining_stage_amount,
        "risk_budget_amount": risk_budget_amount,
        "risk_per_share": risk_per_share,
        "risk_capped_shares": risk_capped_shares,
        "risk_capped_amount": risk_capped_amount,
        "max_loss_per_trade_pct": max_loss_per_trade_pct,
        "opening_trigger": opening_trigger,
        "add_trigger": add_trigger,
        "stop_trigger": stop_trigger,
        "trim_trigger": trim_trigger,
        "tranche_plan": tranches,
        "future_buy_plan": future_buy_plan,
        "execution_notes": execution_notes,
        "chart_signals": chart_signals,
        "moving_averages": {"ma10": ma10, "ma20": ma20},
        "return_scenarios": return_scenarios,
    }
