from __future__ import annotations

from datetime import date, datetime, timezone
import math
from statistics import mean, pstdev

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.score import Score
from app.models.symbol import Symbol


def _safe_date(value):
    """安全地将值转换为 date 类型，处理 MySQL 返回的字符串。"""
    from datetime import date, datetime
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return None


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
    """计算 symbol 评分。

    P0 改造：委托给 scoring_config_engine.calculate_symbol_score_with_config，
    按 symbol.asset_type 自动读取当前激活预设。
    若 scoring_configs 表不存在或无激活预设，则回退到旧的硬编码逻辑。
    """
    try:
        from app.services.scoring_config_engine import calculate_symbol_score_with_config
        return calculate_symbol_score_with_config(db, symbol, trade_date, config=None)
    except Exception:
        # 兜底：表未建/无激活预设时走旧硬编码逻辑，保证向后兼容
        return _legacy_calculate_symbol_score(db, symbol, trade_date)


def _legacy_calculate_symbol_score(db: Session, symbol: Symbol, trade_date: date) -> Score:
    """旧的硬编码评分逻辑（向后兼容兜底）。"""
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
        breakout_score = 50.0
        pullback_score = 40.0
        overheat_penalty = 0.0
        data_credibility = 0.2  # 数据极少，可信度极低
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

        breakout_score = 70.0 if len(closes) >= 20 and last_close >= max(closes[-20:]) else 50.0
        pullback_score = 65.0 if ma20 and last_close >= ma20 * 0.98 else 40.0
        overheat_penalty = 20.0 if ma20 and last_close >= ma20 * 1.12 else 0.0
        timing_score = _clamp_score(
            breakout_score * 0.30
            + momentum_score * 0.20
            + liquidity_score * 0.15
            + pullback_score * 0.15
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

    # 数据可信度计算（P0-4.3）
    # 基于 K 线数量 + 行情时效综合评估
    # 注意：<5 分支已在上方 if 块中赋值 data_credibility = 0.2，此处不可无条件重置
    bar_count = len(bars)
    if bar_count >= 5:
        # K 线数量因子: 5根=0.4, 20根=0.7, 50+=1.0
        bar_factor = min(1.0, 0.4 + (bar_count - 5) * 0.02)
        # 行情时效因子: 当天=1.0, 1天前=0.95, 3天前=0.8, 7天前=0.5
        days_stale = (date.today() - _safe_date(trade_date)).days
        freshness_factor = max(0.3, 1.0 - days_stale * 0.1) if days_stale <= 7 else max(0.1, 0.5 - (days_stale - 7) * 0.05)
        data_credibility = round(min(1.0, bar_factor * freshness_factor), 2)
    # bar_count < 5 时 data_credibility 保持 if 块中赋的 0.2

    priority_score = round(timing_score * 0.4 + quality_score * 0.3 + liquidity_score * 0.2 + breadth_score * 0.1, 2)
    calc_batch_id = f"manual-{trade_date.isoformat() if hasattr(trade_date, 'isoformat') else str(trade_date)}"

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
    # 股质评分分项
    existing.trend_score = trend_score
    existing.momentum_score = momentum_score
    existing.volatility_score = volatility_score
    existing.liquidity_score = liquidity_score
    existing.breadth_score = breadth_score
    existing.event_score = event_score
    # 时点评分分项（新增）
    existing.breakout_score = breakout_score
    existing.pullback_score = pullback_score
    existing.overheat_penalty = overheat_penalty
    # 数据可信度（P0-4.3）
    existing.data_credibility = data_credibility
    db.flush()
    return existing
