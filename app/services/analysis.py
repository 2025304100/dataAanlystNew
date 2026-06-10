from __future__ import annotations

from datetime import date
import math
from statistics import mean, pstdev

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.score import Score
from app.models.symbol import Symbol


def _clamp_score(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def _grade(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def calculate_symbol_score(db: Session, symbol: Symbol, trade_date: date) -> Score:
    bars = db.execute(
        select(DailyBar)
        .where(DailyBar.symbol_id == symbol.id, DailyBar.trade_date <= trade_date)
        .order_by(DailyBar.trade_date.desc())
        .limit(80)
    ).scalars().all()
    bars = list(reversed(bars))

    if len(bars) < 5:
        quality_score = 50.0
        timing_score = 45.0
        trend_score = 50.0
        momentum_score = 45.0
        volatility_score = 50.0
        liquidity_score = 45.0
        breadth_score = 55.0 if symbol.theme else 50.0
        event_score = 50.0
        stage = "cooldown"
        action = "hold"
    else:
        closes = [bar.close for bar in bars]
        amounts = [bar.amount or 0 for bar in bars]
        returns = []
        for prev, current in zip(closes, closes[1:]):
            if prev:
                returns.append((current - prev) / prev)
        last_close = closes[-1]
        ma20_window = closes[-20:] if len(closes) >= 20 else closes
        ma50_window = closes[-50:] if len(closes) >= 50 else closes
        ma20 = mean(ma20_window)
        ma50 = mean(ma50_window)
        momentum20 = ((last_close / closes[-20]) - 1) if len(closes) >= 20 and closes[-20] else 0.0
        trend_component = 50 + ((last_close - ma50) / ma50 * 180 if ma50 else 0)
        momentum_component = 50 + momentum20 * 250
        volatility = pstdev(returns[-20:]) if len(returns) >= 2 else 0.0
        volatility_component = 70 - volatility * 600
        avg_amount = mean(amounts[-20:]) if amounts else 0
        liquidity_component = 35 + min(40, math.log10(max(avg_amount, 1)) * 4)
        breadth_score = 60.0 if symbol.theme else 50.0
        event_score = 50.0

        trend_score = _clamp_score(trend_component)
        momentum_score = _clamp_score(momentum_component)
        volatility_score = _clamp_score(volatility_component)
        liquidity_score = _clamp_score(liquidity_component)

        quality_score = _clamp_score(
            trend_score * 0.25
            + momentum_score * 0.20
            + volatility_score * 0.15
            + liquidity_score * 0.15
            + breadth_score * 0.15
            + event_score * 0.10
        )

        breakout = 70.0 if len(closes) >= 20 and last_close >= max(closes[-20:]) else 50.0
        pullback = 65.0 if ma20 and last_close >= ma20 * 0.98 else 40.0
        overheat_penalty = 20.0 if ma20 and last_close >= ma20 * 1.12 else 0.0
        timing_score = _clamp_score(
            breakout * 0.30
            + momentum_score * 0.20
            + liquidity_score * 0.15
            + pullback * 0.15
            + event_score * 0.10
            + (100 - overheat_penalty) * 0.10
        )

        if overheat_penalty >= 20:
            stage = "overheat"
            action = "reduce"
        elif last_close > ma20 > ma50 and momentum20 > 0.08:
            stage = "accel"
            action = "hold"
        elif last_close > ma20 and momentum20 > 0:
            stage = "start"
            action = "open"
        else:
            stage = "cooldown"
            action = "hold"

    priority_score = round(timing_score * 0.4 + quality_score * 0.3 + liquidity_score * 0.2 + breadth_score * 0.1, 2)
    calc_batch_id = f"manual-{trade_date.isoformat()}"

    existing = db.execute(
        select(Score).where(
            Score.symbol_id == symbol.id,
            Score.trade_date == trade_date,
            Score.calc_batch_id == calc_batch_id,
        )
    ).scalars().first()

    if existing is None:
        existing = Score(
            symbol_id=symbol.id,
            trade_date=trade_date,
            calc_batch_id=calc_batch_id,
        )
        db.add(existing)

    existing.quality_score = quality_score
    existing.quality_grade = _grade(quality_score)
    existing.timing_score = timing_score
    existing.stage = stage
    existing.action = action
    existing.priority_score = priority_score
    existing.trend_score = trend_score
    existing.momentum_score = momentum_score
    existing.volatility_score = volatility_score
    existing.liquidity_score = liquidity_score
    existing.breadth_score = breadth_score
    existing.event_score = event_score
    db.flush()
    return existing

