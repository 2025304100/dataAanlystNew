from pathlib import Path
import re

path = Path('app/services/trade_plans.py')
text = path.read_text(encoding='utf-8')

start = text.index('def upsert_trade_setup(')
end = text.index('\ndef build_trade_setup_view(', start)
new_func = r'''def upsert_trade_setup(
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

'''
text = text[:start] + new_func + text[end + 1:]

if '_load_json_map' not in text:
    text = text.replace(
        '    return {\n        "id": setup.id,\n',
        '    def _load_json_map(value: str | None) -> dict:\n        if not value:\n            return {}\n        try:\n            parsed = json.loads(value)\n            return parsed if isinstance(parsed, dict) else {}\n        except (TypeError, ValueError):\n            return {}\n\n    manual_overrides = _load_json_map(getattr(setup, "manual_overrides_json", None))\n    field_sources = _load_json_map(getattr(setup, "field_sources_json", None))\n\n    return {\n        "id": setup.id,\n',
        1,
    )

text = text.replace(
    '        "setup_reason": setup.setup_reason,\n        "created_at": setup.created_at.isoformat(),\n',
    '        "setup_reason": setup.setup_reason,\n        "manual_overrides_json": getattr(setup, "manual_overrides_json", None),\n        "field_sources_json": getattr(setup, "field_sources_json", None),\n        "manual_overrides": manual_overrides,\n        "field_sources": field_sources,\n        "created_at": setup.created_at.isoformat(),\n',
)
path.write_text(text, encoding='utf-8')
