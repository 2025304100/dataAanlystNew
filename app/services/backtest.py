from __future__ import annotations

import ast
import json
import logging
import operator
from collections import Counter
from datetime import date, datetime, timezone
from math import floor, sqrt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.backtest import BacktestRun, BacktestTrade
from app.models.custom_indicator import CustomIndicator
from app.models.daily_bar import DailyBar
from app.models.factor_model import FactorModelRun
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.runtime import get_factor_runtime_snapshot
# 共享模块：成本模型、绩效指标。抽取自此文件原 _compute_cost/_compute_statistics，
# 供回测、模拟交易、（未来的）自动下单三方共用，避免重复实现。
from app.services.cost_model import DEFAULT_COST_CONFIG, compute_cost as _compute_cost
from app.services.metrics import (
    TRADING_DAYS_PER_YEAR,
    RISK_FREE_RATE,
    compute_statistics as _shared_compute_statistics,
)


EXECUTION_PRICE_FIELDS = {"open", "close"}
EXECUTION_TIMING_MODES = {"signal_open", "signal_close", "next_open"}

# 注：DEFAULT_COST_CONFIG / TRADING_DAYS_PER_YEAR / RISK_FREE_RATE
# 已从 app.services.cost_model / app.services.metrics 导入，
# 此处保留导入以维持向后兼容（外部模块可能 from app.services.backtest import DEFAULT_COST_CONFIG）。

logger = logging.getLogger(__name__)


def _execution_timing_mode(rule_config: dict, prefix: str, default: str = "signal_close") -> str:
    execution_config = rule_config.get("execution_config", {})
    if not isinstance(execution_config, dict):
        return default
    timing = execution_config.get(f"{prefix}_timing")
    if timing in EXECUTION_TIMING_MODES:
        return timing
    legacy_field = execution_config.get(f"{prefix}_price_field")
    if legacy_field == "open":
        return "signal_open"
    return default


def _execution_price_field_for_timing(timing: str) -> str:
    return "open" if timing in {"signal_open", "next_open"} else "close"


def _signal_price_field_for_timing(timing: str) -> str:
    return "open" if timing == "signal_open" else "close"


def _execution_price_field(rule_config: dict, key: str, default: str = "close") -> str:
    prefix = "entry" if key.startswith("entry") else "exit" if key.startswith("exit") else None
    if prefix is not None:
        return _execution_price_field_for_timing(_execution_timing_mode(rule_config, prefix, default="signal_close"))
    execution_config = rule_config.get("execution_config", {})
    if not isinstance(execution_config, dict):
        return default
    field = execution_config.get(key, default)
    return field if field in EXECUTION_PRICE_FIELDS else default


def _bar_price(bar: DailyBar, field: str) -> float:
    return float(getattr(bar, field if field in EXECUTION_PRICE_FIELDS else "close"))


def _safe_int(value, default: int, minimum: int = 1, maximum: int = 250) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _safe_float(value, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _daily_change_pct(history_bars: list[DailyBar]) -> float | None:
    if len(history_bars) < 2:
        return None
    prev_close = float(history_bars[-2].close)
    if prev_close <= 0:
        return None
    return (float(history_bars[-1].close) - prev_close) / prev_close


def _volume_ratio(history_bars: list[DailyBar], lookback_days: int) -> float | None:
    if len(history_bars) < 2:
        return None
    prior = history_bars[-lookback_days - 1:-1]
    avg_volume = _avg([float(bar.volume or 0) for bar in prior if float(bar.volume or 0) > 0])
    if not avg_volume:
        return None
    return float(history_bars[-1].volume or 0) / avg_volume


def _history_until(bars_by_symbol: dict[int, list[DailyBar]], bar_index: dict[tuple[int, date], int], symbol_id: int, trade_date: date) -> list[DailyBar]:
    index = bar_index.get((symbol_id, trade_date))
    if index is None:
        return []
    return bars_by_symbol.get(symbol_id, [])[:index + 1]


# 风控加固：批量预加载 Score 避免 N+1（回测场景下 N 可达数千~数万）
# 一次性 select(Score).where(symbol_id in (...), trade_date between start, end)
# 按 symbol_id 分组并按 trade_date 升序，循环内用 bisect 二分查找 <= current_date 的最新 score
def _build_score_map(
    db: Session,
    symbol_ids: list[int],
    start_date: date,
    end_date: date,
    *,
    score_weight_mode: str = 'manual',
    factor_model_run_id: str | None = None,
) -> dict[int, list[Score]]:
    """批量预加载 Score，返回 {symbol_id: [Score, ...]}，每个 list 按 trade_date 升序。"""
    if not symbol_ids:
        return {}
    stmt = select(Score).where(
            Score.symbol_id.in_(symbol_ids),
            Score.trade_date >= start_date,
            Score.trade_date <= end_date,
            Score.weight_mode == score_weight_mode,
        )
    if score_weight_mode == 'ridge':
        stmt = stmt.where(
            Score.factor_model_run_id == factor_model_run_id
        )
    rows = db.execute(
        stmt.order_by(Score.symbol_id, Score.trade_date, Score.id)
    ).scalars().all()
    score_map: dict[int, list[Score]] = {}
    for s in rows:
        score_map.setdefault(s.symbol_id, []).append(s)
    return score_map


def _latest_score_on_or_before(score_map: dict[int, list[Score]], symbol_id: int, target_date: date) -> Score | None:
    """用 bisect 在 score_map[symbol_id]（按 trade_date 升序）中查找 <= target_date 的最新 Score。

    等价于 select(Score).where(symbol_id=..., trade_date<=target_date).order_by(trade_date.desc()).first()
    但是 O(log n) 内存查找，无 DB 查询。
    """
    import bisect
    sym_scores = score_map.get(symbol_id)
    if not sym_scores:
        return None
    # bisect_right 返回第一个 > target_date 的索引，-1 即 <= target_date 的最后一个
    idx = bisect.bisect_right(sym_scores, target_date, key=lambda s: s.trade_date) - 1
    if idx < 0:
        return None
    return sym_scores[idx]


def _compute_price_context(bars: list[DailyBar], lookback_days: int) -> dict:
    current = bars[-1] if bars else None
    prior = bars[-lookback_days - 1:-1] if bars else []
    window = bars[-lookback_days:] if bars else []
    ma = _avg([float(bar.close) for bar in window])
    prior_high = max((float(bar.high) for bar in prior), default=None)
    prior_low = min((float(bar.low) for bar in prior), default=None)
    return {"current": current, "ma": ma, "prior_high": prior_high, "prior_low": prior_low, "has_window": len(window) >= lookback_days}


def _evaluate_buy_signal(
    symbol_id: int,
    trade_date: date,
    bar: DailyBar,
    score: Score | None,
    rule_config: dict,
    history_bars: list[DailyBar] | None = None,
    prev_bar: DailyBar | None = None,
) -> bool:
    """Doc."""
    if rule_config.get("version", 1) >= 2:
        return _buy_reject_reason_v2(bar, prev_bar, score, rule_config, history_bars) is None
    return _buy_reject_reason(symbol_id, trade_date, bar, score, rule_config, history_bars) is None


def _buy_reject_reason(
    symbol_id: int,
    trade_date: date,
    bar: DailyBar,
    score: Score | None,
    rule_config: dict,
    history_bars: list[DailyBar] | None = None,
) -> str | None:
    """Doc."""
    buy_conditions = rule_config.get("buy_conditions", {})
    
    # Score is required for standard-mode buy evaluation.
    if score is None:
        return "no_score"
    quality_min = buy_conditions.get("quality_score_min")
    if quality_min is not None and score.quality_score < quality_min:
        return "quality_score_min"
    
    timing_min = buy_conditions.get("timing_score_min")
    if timing_min is not None and score.timing_score < timing_min:
        return "timing_score_min"
    
    # Stage and action filters are optional guards in standard mode.
    allowed_stages = buy_conditions.get("stages")
    if allowed_stages and score.stage not in allowed_stages:
        return "stage"
    allowed_actions = buy_conditions.get("actions")
    if allowed_actions and score.action not in allowed_actions:
        return "action"

    history = history_bars or [bar]
    lookback_days = _safe_int(buy_conditions.get("lookback_days"), 20)
    price_rule = buy_conditions.get("price_rule", "none")
    if price_rule and price_rule != "none":
        context = _compute_price_context(history, lookback_days)
        current = context["current"]
        ma = context["ma"]
        if not current or not context["has_window"]:
            return "price_rule_data"
        close = float(current.close)
        if price_rule == "above_ma" and (ma is None or close <= ma):
            return "price_rule"
        if price_rule == "below_ma" and (ma is None or close >= ma):
            return "price_rule"
        if price_rule == "breakout_high":
            prior_high = context["prior_high"]
            if prior_high is None:
                return "price_rule_data"
            if close <= prior_high:
                return "price_rule"
        if price_rule == "pullback_ma":
            tolerance = _safe_float(buy_conditions.get("pullback_pct"), 0.02) or 0.02
            if ma is None or float(current.low) > ma * (1 + tolerance) or close < ma * (1 - tolerance):
                return "price_rule"

    min_daily_change = _safe_float(buy_conditions.get("min_daily_change_pct"))
    if min_daily_change is not None:
        change_pct = _daily_change_pct(history)
        if change_pct is None or change_pct < min_daily_change:
            return "daily_change_min"

    min_volume_ratio = _safe_float(buy_conditions.get("min_volume_ratio"))
    if min_volume_ratio is not None and min_volume_ratio > 0:
        ratio = _volume_ratio(history, lookback_days)
        if ratio is None or ratio < min_volume_ratio:
            return "volume_ratio"
    
    return None


def _evaluate_sell_signal(
    trade: BacktestTrade,
    current_bar: DailyBar,
    current_date: date,
    entry_date: date,
    rule_config: dict,
    score: Score | None = None,
    history_bars: list[DailyBar] | None = None,
    prev_bar: DailyBar | None = None,
    peak_price: float | None = None,
) -> tuple[bool, str | None]:
    """Doc."""
    if rule_config.get("version", 1) >= 2:
        return _evaluate_sell_v2(trade, current_bar, prev_bar, current_date, rule_config, score, history_bars, peak_price)
    sell_conditions = rule_config.get("sell_conditions", {})
    exit_price_field = _execution_price_field(rule_config, "exit_price_field")
    
    entry_price = trade.entry_price
    current_price = _bar_price(current_bar, exit_price_field)
    hold_days = (current_date - entry_date).days
    
    take_profit_pct = sell_conditions.get("take_profit_pct")
    if take_profit_pct is not None:
        profit_pct = (current_price - entry_price) / entry_price
        if profit_pct >= take_profit_pct:
            return True, "take_profit"
    
    stop_loss_pct = sell_conditions.get("stop_loss_pct")
    if stop_loss_pct is not None:
        loss_pct = (entry_price - current_price) / entry_price
        if loss_pct >= stop_loss_pct:
            return True, "stop_loss"

    score_actions = sell_conditions.get("score_actions")
    if score is not None and score_actions and score.action in score_actions:
        return True, "score_action"

    history = history_bars or [current_bar]
    lookback_days = _safe_int(sell_conditions.get("lookback_days"), 20)
    trailing_stop_pct = _safe_float(sell_conditions.get("trailing_stop_pct"))
    if trailing_stop_pct is not None and trailing_stop_pct > 0:
        trade_bars = [bar for bar in history if bar.trade_date >= entry_date]
        high_since_entry = max((float(bar.high) for bar in trade_bars), default=None)
        if high_since_entry is not None and current_price <= high_since_entry * (1 - trailing_stop_pct):
            return True, "trailing_stop"

    price_rule = sell_conditions.get("price_rule", "none")
    if price_rule and price_rule != "none":
        context = _compute_price_context(history, lookback_days)
        if context["has_window"]:
            if price_rule == "below_ma":
                ma = context["ma"]
                if ma is not None and current_price < ma:
                    return True, "price_rule"
            if price_rule == "breakdown_low":
                prior_low = context["prior_low"]
                if prior_low is not None and current_price < prior_low:
                    return True, "price_rule"

    max_hold_days = sell_conditions.get("max_hold_days")
    if max_hold_days is not None and hold_days >= max_hold_days:
        return True, "max_hold_days"
    
    return False, None


# ============================================================
# v2: 指标计算与信号判定
# ============================================================

def _score_attr(score, attr: str, default=None):
    """Doc."""
    if score is None:
        return default
    return getattr(score, attr, default)


def _history_with_current(history_bars: list[DailyBar] | None, bar: DailyBar) -> list[DailyBar]:
    bars = list(history_bars or [])
    if not bars or bars[-1].trade_date != bar.trade_date:
        bars.append(bar)
    return bars


def _safe_period(value, default: int, minimum: int = 1, maximum: int = 250) -> int:
    return _safe_int(value, default, minimum, maximum)


def _bar_series(history_bars: list[DailyBar] | None, bar: DailyBar, field: str = "close") -> list[float]:
    if field not in {"open", "high", "low", "close", "volume", "amount", "turnover_rate"}:
        field = "close"
    values: list[float] = []
    for item in _history_with_current(history_bars, bar):
        raw = getattr(item, field, None)
        if raw is not None:
            values.append(float(raw))
    return values


def _sma_value(values: list[float], period: int) -> float | None:
    period = _safe_period(period, 20)
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _ema_value(values: list[float], period: int) -> float | None:
    period = _safe_period(period, 20)
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = value * alpha + ema * (1.0 - alpha)
    return ema


def _ema_series(values: list[float], period: int) -> list[float | None]:
    period = _safe_period(period, 20)
    if len(values) < period:
        return [None] * len(values)
    result: list[float | None] = [None] * (period - 1)
    ema = sum(values[:period]) / period
    result.append(ema)
    alpha = 2.0 / (period + 1.0)
    for value in values[period:]:
        ema = value * alpha + ema * (1.0 - alpha)
        result.append(ema)
    return result


def _rsi_value(values: list[float], period: int) -> float | None:
    period = _safe_period(period, 14)
    if len(values) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for idx in range(len(values) - period, len(values)):
        change = values[idx] - values[idx - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _macd_values(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float | None, float | None, float | None]:
    fast = _safe_period(fast, 12)
    slow = _safe_period(slow, 26)
    signal = _safe_period(signal, 9)
    if len(values) < slow + signal:
        return None, None, None
    fast_series = _ema_series(values, fast)
    slow_series = _ema_series(values, slow)
    macd_line = [f - sl for f, sl in zip(fast_series, slow_series) if f is not None and sl is not None]
    if len(macd_line) < signal:
        return None, None, None
    signal_value = _ema_value(macd_line, signal)
    macd_value = macd_line[-1]
    if signal_value is None:
        return macd_value, None, None
    return macd_value, signal_value, macd_value - signal_value


def _boll_values(values: list[float], period: int = 20, width: float = 2.0) -> tuple[float | None, float | None, float | None]:
    period = _safe_period(period, 20)
    if len(values) < period:
        return None, None, None
    window = values[-period:]
    mid = sum(window) / period
    variance = sum((value - mid) ** 2 for value in window) / period
    band = float(width) * sqrt(variance)
    return mid + band, mid, mid - band


def _atr_value(bars: list[DailyBar], period: int = 14) -> float | None:
    period = _safe_period(period, 14)
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for idx in range(len(bars) - period, len(bars)):
        current = bars[idx]
        prev_close = float(bars[idx - 1].close)
        trs.append(max(float(current.high) - float(current.low), abs(float(current.high) - prev_close), abs(float(current.low) - prev_close)))
    return sum(trs) / len(trs)


def _kdj_values(bars: list[DailyBar], period: int = 9) -> tuple[float | None, float | None, float | None]:
    period = _safe_period(period, 9)
    if len(bars) < period:
        return None, None, None
    k = 50.0
    d = 50.0
    for idx in range(period - 1, len(bars)):
        window = bars[idx - period + 1:idx + 1]
        low = min(float(item.low) for item in window)
        high = max(float(item.high) for item in window)
        close = float(bars[idx].close)
        rsv = 50.0 if high == low else (close - low) / (high - low) * 100.0
        k = k * 2.0 / 3.0 + rsv / 3.0
        d = d * 2.0 / 3.0 + k / 3.0
    return k, d, 3.0 * k - 2.0 * d


def _resolve_sma(score, bar, prev_bar, history_bars, params) -> float | None:
    return _sma_value(_bar_series(history_bars, bar, str((params or {}).get("field", "close"))), int((params or {}).get("period", 20)))


def _resolve_ema(score, bar, prev_bar, history_bars, params) -> float | None:
    return _ema_value(_bar_series(history_bars, bar, str((params or {}).get("field", "close"))), int((params or {}).get("period", 20)))


def _resolve_rsi(score, bar, prev_bar, history_bars, params) -> float | None:
    return _rsi_value(_bar_series(history_bars, bar, "close"), int((params or {}).get("period", 14)))


def _resolve_macd(score, bar, prev_bar, history_bars, params) -> float | None:
    return _macd_values(_bar_series(history_bars, bar, "close"), int((params or {}).get("fast", 12)), int((params or {}).get("slow", 26)), int((params or {}).get("signal", 9)))[0]


def _resolve_macd_signal(score, bar, prev_bar, history_bars, params) -> float | None:
    return _macd_values(_bar_series(history_bars, bar, "close"), int((params or {}).get("fast", 12)), int((params or {}).get("slow", 26)), int((params or {}).get("signal", 9)))[1]


def _resolve_macd_hist(score, bar, prev_bar, history_bars, params) -> float | None:
    return _macd_values(_bar_series(history_bars, bar, "close"), int((params or {}).get("fast", 12)), int((params or {}).get("slow", 26)), int((params or {}).get("signal", 9)))[2]


def _resolve_boll(score, bar, prev_bar, history_bars, params, part: str) -> float | None:
    upper, mid, lower = _boll_values(_bar_series(history_bars, bar, "close"), int((params or {}).get("period", 20)), float((params or {}).get("width", 2.0)))
    return {"upper": upper, "mid": mid, "lower": lower}.get(part)


def _resolve_atr(score, bar, prev_bar, history_bars, params) -> float | None:
    return _atr_value(_history_with_current(history_bars, bar), int((params or {}).get("period", 14)))


def _resolve_kdj(score, bar, prev_bar, history_bars, params, part: str) -> float | None:
    k, d, j = _kdj_values(_history_with_current(history_bars, bar), int((params or {}).get("period", 9)))
    return {"k": k, "d": d, "j": j}.get(part)


def _resolve_volume_ma(score, bar, prev_bar, history_bars, params) -> float | None:
    return _sma_value(_bar_series(history_bars, bar, "volume"), int((params or {}).get("period", 20)))


def _resolve_turnover_rate(score, bar, prev_bar, history_bars, params) -> float | None:
    return float(bar.turnover_rate) if bar.turnover_rate is not None else None


def _resolve_price_vs_ma_pct(score, bar, prev_bar, history_bars, params) -> float | None:
    ma = _resolve_sma(score, bar, prev_bar, history_bars, params)
    if ma is None or ma == 0:
        return None
    return (float(bar.close) - ma) / ma


def _resolve_ma_cross(score, bar, prev_bar, history_bars, params, direction: str) -> bool:
    short = int((params or {}).get("short", 5))
    long = int((params or {}).get("long", 20))
    bars = _history_with_current(history_bars, bar)
    if len(bars) < long + 1:
        return False
    closes = [float(item.close) for item in bars]
    prev_closes = closes[:-1]
    short_now = _sma_value(closes, short)
    long_now = _sma_value(closes, long)
    short_prev = _sma_value(prev_closes, short)
    long_prev = _sma_value(prev_closes, long)
    if None in {short_now, long_now, short_prev, long_prev}:
        return False
    return short_prev <= long_prev and short_now > long_now if direction == "up" else short_prev >= long_prev and short_now < long_now


_ALLOWED_EXPR_NODES = (ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call, ast.Name, ast.Load, ast.Constant, ast.And, ast.Or, ast.Not, ast.USub, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _safe_pow(a, b):
    """受限的幂运算：防止 9**9**9 类 OOM 攻击。"""
    # 预检查：指数过大直接拒绝，避免 operator.pow 计算时耗尽内存
    if isinstance(b, int) and abs(b) > 1000:
        return None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        try:
            import math
            log_result = abs(b * math.log(abs(a) if a != 0 else 1))
            if log_result > 230:
                return None
        except (ValueError, TypeError):
            pass
    try:
        result = operator.pow(a, b)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError, MemoryError):
        return None
    if isinstance(result, (int, float)) and abs(result) > 1e100:
        return None
    return result


_BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: _safe_pow}
_UNARY_OPS = {ast.USub: operator.neg, ast.Not: operator.not_}
_COMPARE_OPS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def _eval_sandboxed(tree, variables, functions):
    """Doc."""
    # Evaluate a parsed expression tree against the sandboxed allow-list.
    def _ev(node):
        if isinstance(node, ast.Expression):
            return _ev(node.body)
        if isinstance(node, ast.BoolOp):
            values = (_ev(v) for v in node.values)
            if isinstance(node.op, ast.And):
                result = True
                for v in values:
                    result = v
                    if not result:
                        return result
                return result
            result = False
            for v in values:
                result = v
                if result:
                    return result
            return result
        if isinstance(node, ast.BinOp):
            left, right = _ev(node.left), _ev(node.right)
            op = _BIN_OPS.get(type(node.op))
            if op is None or None in (left, right):
                return None
            try:
                return op(left, right)
            except (TypeError, ZeroDivisionError, ValueError, OverflowError):
                return None
        if isinstance(node, ast.UnaryOp):
            operand = _ev(node.operand)
            op = _UNARY_OPS.get(type(node.op))
            if op is None or operand is None:
                return None
            try:
                return op(operand)
            except (TypeError, ValueError):
                return None
        if isinstance(node, ast.Compare):
            left = _ev(node.left)
            if left is None:
                return False
            for op_node, comparator in zip(node.ops, node.comparators):
                right = _ev(comparator)
                if right is None:
                    return False
                op = _COMPARE_OPS.get(type(op_node))
                if op is None:
                    return False
                try:
                    if not op(left, right):
                        return False
                except TypeError:
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            args = [_ev(a) for a in node.args]
            if any(a is None for a in args):
                return None
            kwargs = {kw.arg: _ev(kw.value) for kw in node.keywords}
            func = functions.get(node.func.id)
            if func is None:
                return None
            try:
                return func(*args, **kwargs)
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                return None
        if isinstance(node, ast.Name):
            if node.id == "True":
                return True
            if node.id == "False":
                return False
            return variables.get(node.id)
        if isinstance(node, ast.Constant):
            return node.value
        return None
    return _ev(tree)


def _value_from_token(token, bars: list[DailyBar], offset: int = 0):
    if isinstance(token, (int, float)):
        return float(token)
    if not isinstance(token, str):
        return None
    view = bars[:len(bars) - offset] if offset else bars
    if not view:
        return None
    key = token.strip().lower()
    fields = {"open", "high", "low", "close", "volume", "amount", "turnover_rate"}
    if key in fields:
        raw = getattr(view[-1], key, None)
        return float(raw) if raw is not None else None
    for prefix in ("sma", "ma", "ema", "rsi"):
        if key.startswith(prefix) and key[len(prefix):].isdigit():
            period = int(key[len(prefix):])
            closes = [float(item.close) for item in view]
            if prefix in {"sma", "ma"}:
                return _sma_value(closes, period)
            if prefix == "ema":
                return _ema_value(closes, period)
            return _rsi_value(closes, period)
    if key in {"macd", "macd_signal", "macd_hist"}:
        macd_value, signal_value, hist = _macd_values([float(item.close) for item in view])
        return {"macd": macd_value, "macd_signal": signal_value, "macd_hist": hist}[key]
    return None


def _resolve_formula_expr(score, bar, prev_bar, history_bars, expr: str) -> bool | float:
    expr = str(expr or "").strip()
    if not expr or len(expr) > 500:
        return False
    bars = _history_with_current(history_bars, bar)
    variables = {"open": float(bar.open), "high": float(bar.high), "low": float(bar.low), "close": float(bar.close), "volume": float(bar.volume or 0), "amount": float(bar.amount or 0), "turnover_rate": float(bar.turnover_rate or 0), "prev_close": float(prev_bar.close) if prev_bar else 0.0, "quality_score": float(_score_attr(score, "quality_score", 0.0) or 0.0), "timing_score": float(_score_attr(score, "timing_score", 0.0) or 0.0), "trend_score": float(_score_attr(score, "trend_score", 0.0) or 0.0), "momentum_score": float(_score_attr(score, "momentum_score", 0.0) or 0.0)}
    def sma(period=20, field="close"):
        return _sma_value(_bar_series(history_bars, bar, str(field)), int(period))
    def ema(period=20, field="close"):
        return _ema_value(_bar_series(history_bars, bar, str(field)), int(period))
    def rsi(period=14):
        return _rsi_value(_bar_series(history_bars, bar, "close"), int(period))
    def macd():
        return _macd_values(_bar_series(history_bars, bar, "close"))[0]
    def macd_signal():
        return _macd_values(_bar_series(history_bars, bar, "close"))[1]
    def macd_hist():
        return _macd_values(_bar_series(history_bars, bar, "close"))[2]
    def boll_upper(period=20, width=2.0):
        return _boll_values(_bar_series(history_bars, bar, "close"), int(period), float(width))[0]
    def boll_mid(period=20, width=2.0):
        return _boll_values(_bar_series(history_bars, bar, "close"), int(period), float(width))[1]
    def boll_lower(period=20, width=2.0):
        return _boll_values(_bar_series(history_bars, bar, "close"), int(period), float(width))[2]
    def atr(period=14):
        return _atr_value(bars, int(period))
    def kdj_k(period=9):
        return _kdj_values(bars, int(period))[0]
    def kdj_d(period=9):
        return _kdj_values(bars, int(period))[1]
    def kdj_j(period=9):
        return _kdj_values(bars, int(period))[2]
    def highest(period=20, field="high"):
        values = _bar_series(history_bars, bar, str(field)); period_int = _safe_period(period, 20)
        return max(values[-period_int:]) if len(values) >= period_int else None
    def lowest(period=20, field="low"):
        values = _bar_series(history_bars, bar, str(field)); period_int = _safe_period(period, 20)
        return min(values[-period_int:]) if len(values) >= period_int else None
    def ref(offset=1, field="close"):
        offset_int = _safe_period(offset, 1, 0); values = _bar_series(history_bars, bar, str(field))
        return values[-offset_int - 1] if len(values) > offset_int else None
    def pct_change(offset=1):
        base = ref(offset, "close")
        return None if base in {None, 0} else (float(bar.close) - float(base)) / float(base)
    def volume_ratio(period=20):
        return _volume_ratio(bars, int(period))
    def cross_over(left, right):
        lp, rp, ln, rn = _value_from_token(left, bars, 1), _value_from_token(right, bars, 1), _value_from_token(left, bars, 0), _value_from_token(right, bars, 0)
        return False if None in {lp, rp, ln, rn} else lp <= rp and ln > rn
    def cross_under(left, right):
        lp, rp, ln, rn = _value_from_token(left, bars, 1), _value_from_token(right, bars, 1), _value_from_token(left, bars, 0), _value_from_token(right, bars, 0)
        return False if None in {lp, rp, ln, rn} else lp >= rp and ln < rn
    functions = {"sma": sma, "ema": ema, "rsi": rsi, "macd": macd, "macd_signal": macd_signal, "macd_hist": macd_hist, "boll_upper": boll_upper, "boll_mid": boll_mid, "boll_lower": boll_lower, "atr": atr, "kdj_k": kdj_k, "kdj_d": kdj_d, "kdj_j": kdj_j, "highest": highest, "lowest": lowest, "ref": ref, "pct_change": pct_change, "volume_ratio": volume_ratio, "cross_over": cross_over, "cross_under": cross_under, "abs": abs, "min": min, "max": max, "round": round}
    allowed_names = set(variables) | set(functions) | {"True", "False"}
    try:
        tree = ast.parse(expr, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_EXPR_NODES):
                return False
            if isinstance(node, ast.Name) and node.id not in allowed_names:
                return False
            if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
                return False
            if isinstance(node, ast.Call) and node.func.id not in functions:
                return False
        result = _eval_sandboxed(tree, variables, functions)
        return False if result is None else result
    except Exception:
        return False


def _resolve_custom_expr(score, bar, prev_bar, history_bars, params) -> bool | float:
    return _resolve_formula_expr(score, bar, prev_bar, history_bars, str((params or {}).get("expr", "")))


def _resolve_custom_indicator(score, bar, prev_bar, history_bars, params) -> bool | float:
    indicator = (params or {}).get("indicator")
    if not isinstance(indicator, dict):
        return False
    return _resolve_formula_expr(score, bar, prev_bar, history_bars, str(indicator.get("formula", "")))


def _resolve_daily_change(score, bar, prev_bar, history_bars, params) -> float:
    if prev_bar is None or prev_bar.close == 0:
        return 0.0
    return (float(bar.close) - float(prev_bar.close)) / float(prev_bar.close)


def _resolve_volume_ratio(score, bar, prev_bar, history_bars, params) -> float:
    period = (params or {}).get("avg_period", (params or {}).get("period", 20))
    bars = _history_with_current(history_bars, bar)
    return _volume_ratio(bars, int(period)) or 1.0


def _resolve_ma_above(score, bar, prev_bar, history_bars, params) -> bool:
    period = (params or {}).get("period", 20)
    if not history_bars or len(history_bars) < int(period):
        return False
    window = history_bars[-int(period):]
    sma = sum(float(b.close) for b in window) / len(window)
    return float(bar.close) > sma


def _resolve_ma_below(score, bar, prev_bar, history_bars, params) -> bool:
    return not _resolve_ma_above(score, bar, prev_bar, history_bars, params)


def _resolve_breakout_high(score, bar, prev_bar, history_bars, params) -> bool:
    lookback = (params or {}).get("lookback", 20)
    if not history_bars or len(history_bars) < int(lookback) + 1:
        return False
    prior = history_bars[-int(lookback) - 1:-1]
    if not prior:
        return False
    max_high = max(float(b.high) for b in prior)
    return float(bar.close) >= max_high


def _resolve_pullback_ma(score, bar, prev_bar, history_bars, params) -> bool:
    period = (params or {}).get("period", 20)
    tolerance = (params or {}).get("tolerance_pct", 2.0) / 100.0
    if not history_bars or len(history_bars) < int(period):
        return False
    window = history_bars[-int(period):]
    sma = sum(float(b.close) for b in window) / len(window)
    if sma <= 0:
        return False
    return abs(float(bar.close) - sma) / sma <= tolerance


def _resolve_trailing_stop(score, bar, prev_bar, history_bars, params) -> bool:
    pct = (params or {}).get("pct", 10.0) / 100.0
    peak_price = (params or {}).get("peak_price")
    if peak_price is None:
        return False
    return float(bar.close) <= float(peak_price) * (1.0 - pct)


def _resolve_breakdown_low(score, bar, prev_bar, history_bars, params) -> bool:
    lookback = (params or {}).get("lookback", 20)
    if not history_bars or len(history_bars) < int(lookback) + 1:
        return False
    prior = history_bars[-int(lookback) - 1:-1]
    if not prior:
        return False
    min_low = min(float(b.low) for b in prior)
    return float(bar.close) <= min_low


def _resolve_stop_loss(score, bar, prev_bar, history_bars, params) -> bool:
    pct = (params or {}).get("pct", 8.0) / 100.0
    entry_price = (params or {}).get("entry_price")
    if entry_price is None or float(entry_price) <= 0:
        return False
    loss_pct = (float(entry_price) - float(bar.close)) / float(entry_price)
    return loss_pct >= pct


def _resolve_take_profit(score, bar, prev_bar, history_bars, params) -> bool:
    pct = (params or {}).get("pct", 15.0) / 100.0
    entry_price = (params or {}).get("entry_price")
    if entry_price is None or float(entry_price) <= 0:
        return False
    profit_pct = (float(bar.close) - float(entry_price)) / float(entry_price)
    return profit_pct >= pct


def _resolve_max_hold_days(score, bar, prev_bar, history_bars, params) -> bool:
    days = (params or {}).get("days", 30)
    hold_days = (params or {}).get("hold_days", 0)
    return int(hold_days) >= int(days)


FIELD_RESOLVERS = {
    # Score-derived fields.
    "quality_score": lambda s, b, p, h, pm: _score_attr(s, "quality_score", 0.0),
    "timing_score": lambda s, b, p, h, pm: _score_attr(s, "timing_score", 0.0),
    "stage": lambda s, b, p, h, pm: _score_attr(s, "stage", ""),
    "action": lambda s, b, p, h, pm: _score_attr(s, "action", ""),
    # Expanded score breakdown fields.
    "trend_score": lambda s, b, p, h, pm: _score_attr(s, "trend_score", 0.0),
    "momentum_score": lambda s, b, p, h, pm: _score_attr(s, "momentum_score", 0.0),
    "volatility_score": lambda s, b, p, h, pm: _score_attr(s, "volatility_score", 0.0),
    "liquidity_score": lambda s, b, p, h, pm: _score_attr(s, "liquidity_score", 0.0),
    "breadth_score": lambda s, b, p, h, pm: _score_attr(s, "breadth_score", 0.0),
    "breakout_score": lambda s, b, p, h, pm: _score_attr(s, "breakout_score", 0.0),
    "pullback_score": lambda s, b, p, h, pm: _score_attr(s, "pullback_score", 0.0),
    "overheat_penalty": lambda s, b, p, h, pm: _score_attr(s, "overheat_penalty", 0.0),
    # Price and technical fields.
    "open_price": lambda s, b, p, h, pm: float(b.open),
    "high_price": lambda s, b, p, h, pm: float(b.high),
    "low_price": lambda s, b, p, h, pm: float(b.low),
    "close_price": lambda s, b, p, h, pm: float(b.close),
    "daily_change": _resolve_daily_change,
    "turnover_rate": _resolve_turnover_rate,
    "volume_ratio": _resolve_volume_ratio,
    "volume_ma": _resolve_volume_ma,
    "sma": _resolve_sma,
    "ema": _resolve_ema,
    "rsi": _resolve_rsi,
    "macd": _resolve_macd,
    "macd_signal": _resolve_macd_signal,
    "macd_hist": _resolve_macd_hist,
    "boll_upper": lambda s, b, p, h, pm: _resolve_boll(s, b, p, h, pm, "upper"),
    "boll_mid": lambda s, b, p, h, pm: _resolve_boll(s, b, p, h, pm, "mid"),
    "boll_lower": lambda s, b, p, h, pm: _resolve_boll(s, b, p, h, pm, "lower"),
    "atr": _resolve_atr,
    "kdj_k": lambda s, b, p, h, pm: _resolve_kdj(s, b, p, h, pm, "k"),
    "kdj_d": lambda s, b, p, h, pm: _resolve_kdj(s, b, p, h, pm, "d"),
    "kdj_j": lambda s, b, p, h, pm: _resolve_kdj(s, b, p, h, pm, "j"),
    "price_vs_ma_pct": _resolve_price_vs_ma_pct,
    "ma_cross_up": lambda s, b, p, h, pm: _resolve_ma_cross(s, b, p, h, pm, "up"),
    "ma_cross_down": lambda s, b, p, h, pm: _resolve_ma_cross(s, b, p, h, pm, "down"),
    "ma_above": _resolve_ma_above,
    "ma_below": _resolve_ma_below,
    "breakout_high": _resolve_breakout_high,
    "pullback_ma": _resolve_pullback_ma,
    "custom_expr": _resolve_custom_expr,
    "custom_indicator": _resolve_custom_indicator,
    # 计算买卖信号
    "stop_loss_pct": _resolve_stop_loss,
    "take_profit_pct": _resolve_take_profit,
    "trailing_stop": _resolve_trailing_stop,
    "breakdown_low": _resolve_breakdown_low,
    "score_action": lambda s, b, p, h, pm: _score_attr(s, "action", ""),
    "max_hold_days": _resolve_max_hold_days,
}


def _compare(actual, operator: str, expected) -> bool:
    """Doc."""
    try:
        if operator == "gt":
            return float(actual) > float(expected)
        elif operator == "gte":
            return float(actual) >= float(expected)
        elif operator == "lt":
            return float(actual) < float(expected)
        elif operator == "lte":
            return float(actual) <= float(expected)
        elif operator == "eq":
            return actual == expected
        elif operator == "neq":
            return actual != expected
        elif operator == "in":
            return actual in (expected if isinstance(expected, list) else [expected])
        elif operator == "not_in":
            return actual not in (expected if isinstance(expected, list) else [expected])
    except (TypeError, ValueError):
        return False
    return False


def _evaluate_condition_tree(
    node: dict,
    score,
    bar: DailyBar,
    prev_bar: DailyBar | None,
    history_bars: list[DailyBar] | None,
    extra_params: dict | None = None,
) -> bool:
    """Doc."""
    if not node:
        return True

    if "logic" in node:
        logic = node["logic"]
        children = node.get("conditions", [])
        if not children:
            return True
        if logic == "AND":
            return all(
                _evaluate_condition_tree(c, score, bar, prev_bar, history_bars, extra_params)
                for c in children
            )
        else:  # OR
            return any(
                _evaluate_condition_tree(c, score, bar, prev_bar, history_bars, extra_params)
                for c in children
            )
    else:
        field = node.get("field", "")
        operator = node.get("operator", "eq")
        expected = node.get("value")
        params = {**(node.get("params") or {}), **(extra_params or {})}

        resolver = FIELD_RESOLVERS.get(field)
        if resolver is None:
            return False

        actual = resolver(score, bar, prev_bar, history_bars, params)
        return _compare(actual, operator, expected)


def _first_fail_reason_v2(
    node: dict,
    score,
    bar: DailyBar,
    prev_bar: DailyBar | None,
    history_bars: list[DailyBar] | None,
    extra_params: dict | None = None,
) -> str | None:
    """Doc."""
    if not node:
        return None

    if "logic" in node:
        logic = node["logic"]
        children = node.get("conditions", [])
        if logic == "AND":
            for c in children:
                reason = _first_fail_reason_v2(c, score, bar, prev_bar, history_bars, extra_params)
                if reason is not None:
                    return reason
            return None
        else:  # OR: any passing child is enough.
            for c in children:
                if _evaluate_condition_tree(c, score, bar, prev_bar, history_bars, extra_params):
                    return None
            # For OR groups, return the first leaf field as a fallback explanation.
            return _extract_first_leaf_field(node)
    else:
        field = node.get("field", "unknown")
        passed = _evaluate_condition_tree(node, score, bar, prev_bar, history_bars, extra_params)
        return None if passed else field

def _extract_first_leaf_field(node: dict) -> str:
    """Doc."""
    if "logic" not in node:
        return node.get("field", "unknown")
    for c in node.get("conditions", []):
        result = _extract_first_leaf_field(c)
        if result:
            return result
    return "unknown"


def _buy_reject_reason_v2(
    bar: DailyBar,
    prev_bar: DailyBar | None,
    score,
    rule_config: dict,
    history_bars: list[DailyBar] | None = None,
) -> str | None:
    """Doc."""
    if score is None:
        return "no_score"
    buy_tree = rule_config.get("buy_conditions", {})
    return _first_fail_reason_v2(buy_tree, score, bar, prev_bar, history_bars)


def _evaluate_sell_v2(
    trade: BacktestTrade,
    bar: DailyBar,
    prev_bar: DailyBar | None,
    current_date: date,
    rule_config: dict,
    score=None,
    history_bars: list[DailyBar] | None = None,
    peak_price: float | None = None,
) -> tuple[bool, str | None]:
    """Doc."""
    sell_tree = rule_config.get("sell_conditions", {})
    hold_days = (current_date - trade.entry_date).days
    extra = {
        "entry_price": trade.entry_price,
        "peak_price": peak_price or trade.entry_price,
        "hold_days": hold_days,
    }
    passed = _evaluate_condition_tree(sell_tree, score, bar, prev_bar, history_bars, extra)
    if passed:
        reason = _first_match_reason_v2(sell_tree, score, bar, prev_bar, history_bars, extra)
        return True, reason or "condition_met"
    return False, None


def _first_match_reason_v2(
    node: dict,
    score,
    bar: DailyBar,
    prev_bar: DailyBar | None,
    history_bars: list[DailyBar] | None,
    extra_params: dict | None = None,
) -> str | None:
    """Doc."""
    if not node:
        return None

    if "logic" in node:
        logic = node["logic"]
        children = node.get("conditions", [])
        if logic == "OR":
            for c in children:
                reason = _first_match_reason_v2(c, score, bar, prev_bar, history_bars, extra_params)
                if reason is not None:
                    return reason
            return None
        else:  # AND ?all must pass
            for c in children:
                if not _evaluate_condition_tree(c, score, bar, prev_bar, history_bars, extra_params):
                    return None
            # 条件字段映射
            return _extract_first_leaf_field(node)
    else:
        field = node.get("field", "unknown")
        passed = _evaluate_condition_tree(node, score, bar, prev_bar, history_bars, extra_params)
        return field if passed else None


def _compute_equity_curve(
    trades: list[BacktestTrade],
    initial_capital: float,
    date_range: list[date],
    close_prices: dict[tuple[int, date], float],
) -> list[dict]:
    """Doc."""
    equity_curve = []
    cash = initial_capital
    positions: dict[int, BacktestTrade] = {}
    
    for current_date in date_range:
        # Apply entries that fill on the current date.
        for trade in trades:
            if trade.entry_date == current_date:
                cash -= trade.entry_price * trade.quantity + trade.entry_cost
                positions[trade.id] = trade
        # 处理当日退出持仓
        for trade in trades:
            if trade.exit_date == current_date and trade.id in positions:
                cash += trade.exit_price * trade.quantity - (trade.exit_cost or 0)
                del positions[trade.id]
        
        position_value = 0.0
        for trade in positions.values():
            close_price = close_prices.get((trade.symbol_id, current_date), trade.entry_price)
            position_value += close_price * trade.quantity

        equity = cash + position_value
        equity_curve.append({
            "date": current_date.isoformat() if hasattr(current_date, 'isoformat') else str(current_date),
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "position_value": round(position_value, 2),
        })
    
    return equity_curve


def _compute_statistics(
    trades: list[BacktestTrade],
    initial_capital: float,
    equity_curve: list[dict],
) -> dict:
    """回测绩效统计 - 委托到 app.services.metrics 共享模块。

    保留原签名以维持 backtest.py 内部调用兼容（line 2090 等）。
    将 BacktestTrade ORM 对象转换为通用 dict 后调用共享实现，
    确保算法与原实现一致（回测结果可复现）。

    未来组合实盘绩效、自动下单效果评估可直接调用
    app.services.metrics.compute_statistics，无需经过此包装。
    """
    # 转换 BacktestTrade → dict，与 metrics.compute_statistics 期望的输入对齐
    trade_dicts = [
        {
            "pnl": t.pnl if t.exit_date is not None else None,
            "hold_days": t.hold_days,
        }
        for t in trades
    ]
    return _shared_compute_statistics(trade_dicts, initial_capital, equity_curve)


def _json_safe_trace_value(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    if isinstance(value, (list, tuple)):
        return [_json_safe_trace_value(item) for item in value]
    return str(value)



def _collect_condition_traces(
    node: dict,
    score,
    bar: DailyBar,
    prev_bar: DailyBar | None,
    history_bars: list[DailyBar] | None,
    extra_params: dict | None = None,
    field_filter: str | None = None,
) -> list[dict]:
    if not node:
        return []
    if "logic" in node:
        traces: list[dict] = []
        for child in node.get("conditions", []) or []:
            traces.extend(_collect_condition_traces(child, score, bar, prev_bar, history_bars, extra_params, field_filter))
        return traces

    field = node.get("field", "")
    if field_filter and field != field_filter:
        return []

    resolver = FIELD_RESOLVERS.get(field)
    if resolver is None:
        return []

    operator = node.get("operator", "eq")
    expected = node.get("value")
    params = {**(node.get("params") or {}), **(extra_params or {})}
    try:
        actual = resolver(score, bar, prev_bar, history_bars, params)
        matched = _compare(actual, operator, expected)
    except Exception:
        return []

    indicator = params.get("indicator") if isinstance(params.get("indicator"), dict) else {}
    trace = {
        "field": field,
        "operator": operator,
        "expected": _json_safe_trace_value(expected),
        "actual": _json_safe_trace_value(actual),
        "matched": matched,
    }
    if field == "custom_indicator":
        trace["indicator_key"] = params.get("indicator_key")
        trace["indicator_name"] = indicator.get("name") or params.get("indicator_key") or "custom_indicator"
        trace["value_type"] = indicator.get("value_type")
        trace["indicator_version"] = indicator.get("version")
    return [trace]



def _build_trade_trace_map(
    db: Session,
    trades: list[BacktestTrade],
    rule_config: dict,
    bars_by_sym: dict[int, list[DailyBar]],
    bar_idx: dict[tuple[int, date], int],
    *,
    score_weight_mode: str = 'manual',
    factor_model_run_id: str | None = None,
) -> dict[int, dict]:
    if not trades:
        return {}

    def _bar_context(symbol_id: int, trade_day: date, max_history: int = 250) -> tuple[DailyBar | None, DailyBar | None, list[DailyBar]]:
        idx = bar_idx.get((symbol_id, trade_day))
        if idx is None:
            return None, None, []
        sym_bars = bars_by_sym.get(symbol_id, [])
        current_bar = sym_bars[idx]
        prev_bar = sym_bars[idx - 1] if idx > 0 else None
        history = sym_bars[max(0, idx - max_history):idx]
        return current_bar, prev_bar, history

    def _signal_day(symbol_id: int, execution_day: date, timing: str) -> date | None:
        if timing != "next_open":
            return execution_day
        idx = bar_idx.get((symbol_id, execution_day))
        if idx is None or idx <= 0:
            return None
        sym_bars = bars_by_sym.get(symbol_id, [])
        return sym_bars[idx - 1].trade_date if idx - 1 < len(sym_bars) else None

    trade_annotations: dict[int, dict] = {}
    is_v2 = rule_config.get("version", 1) >= 2
    buy_tree = rule_config.get("buy_conditions", {})
    sell_tree = rule_config.get("sell_conditions", {})
    entry_timing = _execution_timing_mode(rule_config, "entry")
    exit_timing = _execution_timing_mode(rule_config, "exit")

    # 风控加固：批量预加载 Score 避免 N+1（原每笔 trade 查 2 次 Score = 2N 次 DB 查询）
    trade_symbol_ids = list({t.symbol_id for t in trades})
    trade_dates = [t.entry_date for t in trades] + [t.exit_date for t in trades if t.exit_date]
    if trade_dates:
        score_start = min(trade_dates)
        score_end = max(trade_dates)
    else:
        score_start = score_end = date.today()
    score_map = _build_score_map(
        db,
        trade_symbol_ids,
        score_start,
        score_end,
        score_weight_mode=score_weight_mode,
        factor_model_run_id=factor_model_run_id,
    )

    for trade in trades:
        entry_signal_day = _signal_day(trade.symbol_id, trade.entry_date, entry_timing)
        entry_signal_bar, entry_prev_bar, entry_history = _bar_context(trade.symbol_id, entry_signal_day) if entry_signal_day else (None, None, [])
        score = _latest_score_on_or_before(score_map, trade.symbol_id, (entry_signal_day or trade.entry_date))
        entry_traces = _collect_condition_traces(
            buy_tree,
            score,
            entry_signal_bar,
            entry_prev_bar,
            entry_history,
        ) if is_v2 and entry_signal_bar else []

        annotation = {
            "entry_signal_date": entry_signal_day,
            "entry_signal_price": _bar_price(entry_signal_bar, _signal_price_field_for_timing(entry_timing)) if entry_signal_bar else None,
            "entry_signal_price_field": _signal_price_field_for_timing(entry_timing) if entry_signal_bar else None,
            "entry_execution_timing": entry_timing,
            "entry_traces": entry_traces,
            "exit_traces": [],
            "exit_execution_timing": exit_timing,
            "exit_signal_date": None,
            "exit_signal_price": None,
            "exit_signal_price_field": None,
        }

        if trade.exit_date:
            exit_signal_day = _signal_day(trade.symbol_id, trade.exit_date, exit_timing)
            exit_score = _latest_score_on_or_before(score_map, trade.symbol_id, (exit_signal_day or trade.exit_date))
            exit_bar, exit_prev_bar, exit_history = _bar_context(trade.symbol_id, exit_signal_day) if exit_signal_day else (None, None, [])
            trade_window = [
                item for item in _history_with_current(exit_history, exit_bar)
                if item.trade_date >= trade.entry_date
            ] if exit_bar else []
            peak_price = max((float(item.high) for item in trade_window), default=trade.entry_price)
            exit_traces = _collect_condition_traces(
                sell_tree,
                exit_score,
                exit_bar,
                exit_prev_bar,
                exit_history,
                extra_params={
                    "entry_price": trade.entry_price,
                    "hold_days": ((exit_signal_day or trade.exit_date) - trade.entry_date).days,
                    "peak_price": peak_price,
                },
            ) if is_v2 and exit_bar else []
            annotation.update({
                "exit_signal_date": exit_signal_day,
                "exit_signal_price": _bar_price(exit_bar, _signal_price_field_for_timing(exit_timing)) if exit_bar else None,
                "exit_signal_price_field": _signal_price_field_for_timing(exit_timing) if exit_bar else None,
                "exit_traces": exit_traces,
            })

        trade_annotations[trade.id] = annotation

    return trade_annotations



def build_backtest_detail_context(db: Session, run: BacktestRun, trades: list[BacktestTrade] | None = None) -> dict:
    """Doc."""
    try:
        symbol_ids = json.loads(run.symbols_json or "[]")
    except json.JSONDecodeError:
        symbol_ids = []
    try:
        rule_config = json.loads(run.rule_config_json or "{}")
    except json.JSONDecodeError:
        rule_config = {}

    rule_config = _prepare_rule_config(db, rule_config)

    if not symbol_ids:
        return {"price_series": [], "diagnostics": {}, "trade_annotations": {}}

    bars = db.execute(
        select(DailyBar)
        .where(
            DailyBar.symbol_id.in_(symbol_ids),
            DailyBar.trade_date >= run.start_date,
            DailyBar.trade_date <= run.end_date,
        )
        .order_by(DailyBar.symbol_id, DailyBar.trade_date)
    ).scalars().all()

    price_series = [
        {
            "symbol_id": bar.symbol_id,
            "date": bar.trade_date.isoformat(),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
        }
        for bar in bars
    ]

    skipped = Counter()
    buy_signal_days = 0
    checked_days = 0
    sample_misses: list[dict] = []
    is_v2 = rule_config.get("version", 1) >= 2
    buy_tree = rule_config.get("buy_conditions", {})

    # bars 数据预处理
    bars_by_sym: dict[int, list] = {}
    bar_idx: dict[tuple[int, date], int] = {}
    for idx, b in enumerate(bars):
        bars_by_sym.setdefault(b.symbol_id, []).append(b)
        bar_idx[(b.symbol_id, b.trade_date)] = len(bars_by_sym[b.symbol_id]) - 1

    trade_annotations = _build_trade_trace_map(
        db,
        trades or [],
        rule_config,
        bars_by_sym,
        bar_idx,
        score_weight_mode=run.score_weight_mode,
        factor_model_run_id=run.factor_model_run_id,
    )

    # 风控加固：批量预加载 Score 避免 N+1（原循环内每条 bar 查一次 = O(N) DB 查询）
    # 改为一次性 select(Score).where(symbol_id in ..., trade_date between start, end)，
    # 循环内用 bisect 二分查找 O(log n) 内存查找
    score_map = _build_score_map(
        db,
        symbol_ids,
        run.start_date,
        run.end_date,
        score_weight_mode=run.score_weight_mode,
        factor_model_run_id=run.factor_model_run_id,
    )

    for bar in bars:
        checked_days += 1
        score = _latest_score_on_or_before(score_map, bar.symbol_id, bar.trade_date)

        # history_bars 与 prev_bar 对齐
        sym_bars = bars_by_sym.get(bar.symbol_id, [])
        b_idx = bar_idx.get((bar.symbol_id, bar.trade_date), 0)
        prev_bar = sym_bars[b_idx - 1] if b_idx > 0 else None
        diag_history = sym_bars[max(0, b_idx - 250):b_idx]

        if is_v2:
            reason = _buy_reject_reason_v2(bar, prev_bar, score, rule_config, diag_history)
        else:
            reason = _buy_reject_reason(bar.symbol_id, bar.trade_date, bar, score, rule_config)
        if reason is None:
            buy_signal_days += 1
        else:
            skipped[reason] += 1
            if len(sample_misses) < 8:
                failed_traces = []
                if is_v2:
                    failed_traces = [
                        trace
                        for trace in _collect_condition_traces(
                            buy_tree,
                            score,
                            bar,
                            prev_bar,
                            diag_history,
                        )
                        if not trace.get("matched")
                    ]
                sample_misses.append(
                    {
                        "date": bar.trade_date.isoformat(),
                        "symbol_id": bar.symbol_id,
                        "reason": reason,
                        "open": float(bar.open),
                        "close": float(bar.close),
                        "quality_score": float(score.quality_score) if score else None,
                        "timing_score": float(score.timing_score) if score else None,
                        "stage": score.stage if score else None,
                        "action": score.action if score else None,
                        "failed_traces": failed_traces,
                    }
                )

    diagnostics = {
        "checked_days": checked_days,
        "buy_signal_days": buy_signal_days,
        "trade_count": run.trade_count or 0,
        "skip_reasons": dict(skipped),
        "sample_misses": sample_misses,
        "execution": {
            "entry_timing": _execution_timing_mode(rule_config, "entry"),
            "exit_timing": _execution_timing_mode(rule_config, "exit"),
            "entry_price_field": _execution_price_field(rule_config, "entry_price_field"),
            "exit_price_field": _execution_price_field(rule_config, "exit_price_field"),
        },
        "buy_conditions": buy_tree,
        "sell_conditions": rule_config.get("sell_conditions", {}),
        "position_config": rule_config.get("position_config", {}),
    }
    if buy_signal_days > 0 and not (run.trade_count or 0):
        diagnostics["fill_warning"] = "buy_signal_passed_but_no_trade"

    return {"price_series": price_series, "diagnostics": diagnostics, "trade_annotations": trade_annotations}



def _collect_custom_indicator_keys(node: dict | list | None, keys: set[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _collect_custom_indicator_keys(item, keys)
        return
    if not isinstance(node, dict):
        return
    if node.get("field") == "custom_indicator":
        params = node.get("params") or {}
        key = params.get("indicator_key")
        if isinstance(key, str) and key:
            keys.add(key)
    for child in node.get("conditions", []) or []:
        _collect_custom_indicator_keys(child, keys)


def _load_custom_indicator_map(db: Session, rule_config: dict) -> dict[str, dict]:
    keys: set[str] = set()
    _collect_custom_indicator_keys(rule_config.get("buy_conditions"), keys)
    _collect_custom_indicator_keys(rule_config.get("sell_conditions"), keys)
    if not keys:
        return {}
    rows = db.execute(
        select(CustomIndicator).where(CustomIndicator.key.in_(keys), CustomIndicator.enabled == True)
    ).scalars().all()
    from app.models.custom_indicator import CustomIndicatorVersion
    from sqlalchemy import func as sa_func
    version_map: dict[int, int] = {}
    if rows:
        ids = [r.id for r in rows]
        ver_rows = db.execute(
            select(CustomIndicatorVersion.indicator_id, sa_func.max(CustomIndicatorVersion.version))
            .where(CustomIndicatorVersion.indicator_id.in_(ids))
            .group_by(CustomIndicatorVersion.indicator_id)
        ).all()
        version_map = {row[0]: int(row[1]) for row in ver_rows}
    return {
        row.key: {
            "key": row.key,
            "name": row.name,
            "formula": row.formula,
            "value_type": row.value_type,
            "version": version_map.get(row.id, 1),
        }
        for row in rows
    }


def _attach_custom_indicators(node: dict | list | None, indicator_map: dict[str, dict]) -> None:
    if isinstance(node, list):
        for item in node:
            _attach_custom_indicators(item, indicator_map)
        return
    if not isinstance(node, dict):
        return
    if node.get("field") == "custom_indicator":
        params = node.setdefault("params", {})
        key = params.get("indicator_key")
        # If indicator data is already snapshot in params (from stored rule_config_json), keep it
        if isinstance(params.get("indicator"), dict) and params["indicator"].get("formula"):
            pass
        elif isinstance(key, str) and key in indicator_map:
            params["indicator"] = indicator_map[key]
    for child in node.get("conditions", []) or []:
        _attach_custom_indicators(child, indicator_map)


def _prepare_rule_config(db: Session, rule_config: dict) -> dict:
    if not isinstance(rule_config, dict) or rule_config.get("version", 1) < 2:
        return rule_config
    indicator_map = _load_custom_indicator_map(db, rule_config)
    if not indicator_map:
        return rule_config
    _attach_custom_indicators(rule_config.get("buy_conditions"), indicator_map)
    _attach_custom_indicators(rule_config.get("sell_conditions"), indicator_map)
    return rule_config



def recommend_history_init_preset(start_date: date, end_date: date) -> str:
    days = max(1, (end_date - start_date).days + 1)
    if days <= 31:
        return "1m"
    if days <= 92:
        return "1q"
    if days <= 366:
        return "1y"
    return "3y"



def assess_backtest_score_coverage(
    db: Session,
    symbol_ids: list[int],
    start_date: date,
    end_date: date,
    *,
    score_weight_mode: str = 'manual',
    factor_model_run_id: str | None = None,
) -> dict:
    if not symbol_ids:
        return {
            "symbols_total": 0,
            "symbols_ready": 0,
            "symbols_missing": 0,
            "recommended_preset": recommend_history_init_preset(start_date, end_date),
            "issues": [],
        }

    bar_stats = {
        int(symbol_id): {
            "bar_days": int(bar_days),
            "bar_start": bar_start.isoformat() if hasattr(bar_start, "isoformat") else str(bar_start),
            "bar_end": bar_end.isoformat() if hasattr(bar_end, "isoformat") else str(bar_end),
        }
        for symbol_id, bar_days, bar_start, bar_end in db.execute(
            select(
                DailyBar.symbol_id,
                func.count(DailyBar.id),
                func.min(DailyBar.trade_date),
                func.max(DailyBar.trade_date),
            )
            .where(
                DailyBar.symbol_id.in_(symbol_ids),
                DailyBar.trade_date >= start_date,
                DailyBar.trade_date <= end_date,
            )
            .group_by(DailyBar.symbol_id)
        ).all()
    }
    score_stats_stmt = select(
        Score.symbol_id,
        func.count(func.distinct(Score.trade_date)),
        func.min(Score.trade_date),
        func.max(Score.trade_date),
    ).where(
        Score.symbol_id.in_(symbol_ids),
        Score.trade_date >= start_date,
        Score.trade_date <= end_date,
        Score.weight_mode == score_weight_mode,
    )
    if score_weight_mode == 'ridge':
        score_stats_stmt = score_stats_stmt.where(
            Score.factor_model_run_id == factor_model_run_id
        )
    score_stats_stmt = score_stats_stmt.group_by(Score.symbol_id)
    score_stats = {
        int(symbol_id): {
            "score_days": int(score_days),
            "score_start": score_start.isoformat() if hasattr(score_start, "isoformat") else str(score_start),
            "score_end": score_end.isoformat() if hasattr(score_end, "isoformat") else str(score_end),
        }
        for symbol_id, score_days, score_start, score_end in db.execute(
            score_stats_stmt
        ).all()
    }

    issues: list[dict] = []
    for symbol_id in symbol_ids:
        bars = bar_stats.get(symbol_id)
        if not bars or bars["bar_days"] <= 0:
            continue
        scores = score_stats.get(symbol_id, {"score_days": 0, "score_start": None, "score_end": None})
        bar_days = bars["bar_days"]
        score_days = int(scores.get("score_days") or 0)
        coverage_pct = round(score_days * 100 / bar_days, 2) if bar_days > 0 else 0.0
        if score_days < bar_days:
            issues.append(
                {
                    "symbol_id": symbol_id,
                    "bar_days": bar_days,
                    "score_days": score_days,
                    "coverage_pct": coverage_pct,
                    "missing_days": max(0, bar_days - score_days),
                    "bar_start": bars.get("bar_start"),
                    "bar_end": bars.get("bar_end"),
                    "score_start": scores.get("score_start"),
                    "score_end": scores.get("score_end"),
                }
            )

    return {
        "symbols_total": len(symbol_ids),
        "symbols_ready": len(symbol_ids) - len(issues),
        "symbols_missing": len(issues),
        "recommended_preset": recommend_history_init_preset(start_date, end_date),
        "issues": issues,
    }
def run_backtest(
    db: Session,
    portfolio_id: int,
    symbol_ids: list[int],
    start_date: date,
    end_date: date,
    rule_config: dict,
    cost_config: dict | None = None,
    run_name: str | None = None,
    score_weight_mode: str | None = None,
    factor_model_run_id: str | None = None,
) -> BacktestRun:
    """Doc."""
    # Execute a backtest run for the selected portfolio and symbols.
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")
    runtime = get_factor_runtime_snapshot(db)
    effective_score_mode = score_weight_mode or runtime.score_weight_mode
    if effective_score_mode not in {'manual', 'ridge'}:
        raise ValueError('score_weight_mode must be manual or ridge')
    effective_model_id = (
        factor_model_run_id or runtime.active_model_run_id
        if effective_score_mode == 'ridge'
        else None
    )
    factor_data_cutoff_at = None
    if effective_score_mode == 'ridge':
        if not effective_model_id:
            raise ValueError('factor_model_run_id is required for Ridge backtest')
        factor_model = db.get(FactorModelRun, effective_model_id)
        if factor_model is None or factor_model.status != 'validated':
            raise ValueError('Ridge backtest requires a validated factor model')
        factor_data_cutoff_at = factor_model.data_cutoff_at
    rule_config = _prepare_rule_config(db, rule_config)

    cost_config = cost_config or DEFAULT_COST_CONFIG

    initial_capital = float(portfolio.total_capital)

    run = BacktestRun(
        portfolio_id=portfolio_id,
        run_name=run_name or f"Backtest {datetime.now().strftime('%Y%m%d_%H%M%S')}",
        symbols_json=json.dumps(symbol_ids),
        rule_config_json=json.dumps(rule_config, ensure_ascii=False),
        cost_config_json=json.dumps(cost_config, ensure_ascii=False),
        score_weight_mode=effective_score_mode,
        factor_model_run_id=effective_model_id,
        factor_data_cutoff_at=factor_data_cutoff_at,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()

    try:
        date_bars = db.execute(
            select(DailyBar.trade_date)
            .where(
                DailyBar.symbol_id.in_(symbol_ids),
                DailyBar.trade_date >= start_date,
                DailyBar.trade_date <= end_date,
            )
            .distinct()
            .order_by(DailyBar.trade_date)
        ).scalars().all()

        if not date_bars:
            raise ValueError("No trading data found in date range")

        close_prices = {
            (row.symbol_id, row.trade_date): float(row.close)
            for row in db.execute(
                select(DailyBar.symbol_id, DailyBar.trade_date, DailyBar.close).where(
                    DailyBar.symbol_id.in_(symbol_ids),
                    DailyBar.trade_date >= start_date,
                    DailyBar.trade_date <= end_date,
                )
            ).all()
        }

        position_config = rule_config.get("position_config", {})
        position_type = position_config.get("type", "fixed_pct")
        position_value = float(position_config.get("value", 0.05))
        max_positions = int(position_config.get("max_positions", 10))
        entry_timing = _execution_timing_mode(rule_config, "entry")
        exit_timing = _execution_timing_mode(rule_config, "exit")
        entry_price_field = _execution_price_field_for_timing(entry_timing)
        exit_price_field = _execution_price_field_for_timing(exit_timing)

        cash = initial_capital
        open_trades: dict[int, BacktestTrade] = {}
        completed_trades: list[BacktestTrade] = []
        peak_prices: dict[int, float] = {}
        pending_entries: dict[int, dict] = {}
        pending_exits: dict[int, dict] = {}

        all_bars = db.execute(
            select(DailyBar)
            .where(
                DailyBar.symbol_id.in_(symbol_ids),
                DailyBar.trade_date >= start_date,
                DailyBar.trade_date <= end_date,
            )
            .order_by(DailyBar.symbol_id, DailyBar.trade_date)
        ).scalars().all()

        bars_by_symbol: dict[int, list[DailyBar]] = {}
        bar_index: dict[tuple[int, date], int] = {}
        for b in all_bars:
            bars_by_symbol.setdefault(b.symbol_id, []).append(b)
            bar_index[(b.symbol_id, b.trade_date)] = len(bars_by_symbol[b.symbol_id]) - 1

        # 风控加固：批量预加载 Score 避免 N+1（原日期×标的循环每组合查一次 = O(交易日×标的数) DB 查询）
        # 改为一次性 select(Score).where(symbol_id in ..., trade_date between start, end)，
        # 循环内用 bisect 二分查找 O(log n) 内存查找
        score_map = _build_score_map(
            db,
            symbol_ids,
            start_date,
            end_date,
            score_weight_mode=effective_score_mode,
            factor_model_run_id=effective_model_id,
        )

        def _get_history(symbol_id: int, trade_date: date, max_history: int = 250) -> tuple[DailyBar | None, DailyBar | None, list[DailyBar]]:
            idx = bar_index.get((symbol_id, trade_date))
            if idx is None:
                return None, None, []
            sym_bars = bars_by_symbol.get(symbol_id, [])
            current_bar = sym_bars[idx]
            prev_bar = sym_bars[idx - 1] if idx > 0 else None
            history = sym_bars[max(0, idx - max_history):idx]
            return current_bar, prev_bar, history

        def _get_next_bar(symbol_id: int, trade_date: date) -> DailyBar | None:
            idx = bar_index.get((symbol_id, trade_date))
            if idx is None:
                return None
            sym_bars = bars_by_symbol.get(symbol_id, [])
            next_idx = idx + 1
            return sym_bars[next_idx] if next_idx < len(sym_bars) else None

        def _compute_entry_quantity(symbol_id: int, entry_price: float) -> tuple[float, float, float] | None:
            if entry_price <= 0:
                return None
            if position_type == "fixed_pct":
                position_amount = initial_capital * position_value
            else:
                position_amount = position_value
            symbol = db.get(Symbol, symbol_id)
            lot_size = 100 if symbol and symbol.market in {"SH", "SZ", "BJ"} else 1
            quantity = floor(position_amount / entry_price / lot_size) * lot_size
            if quantity <= 0:
                return None
            entry_cost = _compute_cost(entry_price, quantity, "buy", cost_config)
            total_cost = entry_price * quantity + entry_cost
            return quantity, entry_cost, total_cost

        for current_date in date_bars:
            for symbol_id, pending in list(pending_exits.items()):
                if pending.get("execute_date") != current_date:
                    continue
                trade = open_trades.get(symbol_id)
                if trade is None:
                    del pending_exits[symbol_id]
                    continue
                bar, _, _ = _get_history(symbol_id, current_date)
                if bar is None:
                    continue
                exit_price = _bar_price(bar, pending.get("price_field", "open"))
                exit_cost = _compute_cost(exit_price, trade.quantity, "sell", cost_config)
                pnl = (exit_price - trade.entry_price) * trade.quantity - trade.entry_cost - exit_cost
                pnl_pct = pnl / (trade.entry_price * trade.quantity)
                hold_days = (current_date - trade.entry_date).days

                trade.exit_date = current_date
                trade.exit_price = exit_price
                trade.exit_reason = pending.get("reason")
                trade.exit_cost = exit_cost
                trade.pnl = round(pnl, 2)
                trade.pnl_pct = round(pnl_pct, 4)
                trade.hold_days = hold_days

                cash += exit_price * trade.quantity - exit_cost
                completed_trades.append(trade)
                del open_trades[symbol_id]
                del pending_exits[symbol_id]
                peak_prices.pop(symbol_id, None)

            for symbol_id, pending in list(pending_entries.items()):
                if pending.get("execute_date") != current_date:
                    continue
                if symbol_id in open_trades:
                    del pending_entries[symbol_id]
                    continue
                if len(open_trades) >= max_positions:
                    break
                bar, _, _ = _get_history(symbol_id, current_date)
                if bar is None:
                    continue
                entry_price = _bar_price(bar, pending.get("price_field", "open"))
                entry_plan = _compute_entry_quantity(symbol_id, entry_price)
                if entry_plan is None:
                    del pending_entries[symbol_id]
                    continue
                quantity, entry_cost, total_cost = entry_plan
                if total_cost > cash:
                    del pending_entries[symbol_id]
                    continue

                trade = BacktestTrade(
                    run_id=run.id,
                    symbol_id=symbol_id,
                    entry_date=current_date,
                    entry_price=entry_price,
                    quantity=quantity,
                    entry_cost=entry_cost,
                )
                db.add(trade)
                db.flush()

                cash -= total_cost
                open_trades[symbol_id] = trade
                peak_prices[symbol_id] = float(bar.high)
                del pending_entries[symbol_id]

            for symbol_id, trade in list(open_trades.items()):
                if symbol_id in pending_exits:
                    continue
                bar, prev_bar, history = _get_history(symbol_id, current_date)
                if bar is None:
                    continue

                peak_prices[symbol_id] = max(peak_prices.get(symbol_id, trade.entry_price), float(bar.high))

                # Bug fix: v1 _evaluate_sell_signal 也支持 score_actions 检查，
                # 但此前仅 v2 fetch score。统一 fetch 以让 v1 的 score_actions 生效。
                # score_map 是内存查找（O(log n) bisect），无 DB 性能影响。
                score = _latest_score_on_or_before(score_map, symbol_id, current_date)

                should_sell, exit_reason = _evaluate_sell_signal(
                    trade,
                    bar,
                    current_date,
                    trade.entry_date,
                    rule_config,
                    score=score,
                    history_bars=history,
                    prev_bar=prev_bar,
                    peak_price=peak_prices.get(symbol_id),
                )

                if not should_sell:
                    continue

                if exit_timing == "next_open":
                    next_bar = _get_next_bar(symbol_id, current_date)
                    if next_bar is None:
                        continue
                    pending_exits[symbol_id] = {
                        "signal_date": current_date,
                        "execute_date": next_bar.trade_date,
                        "price_field": exit_price_field,
                        "reason": exit_reason,
                    }
                    continue

                exit_price = _bar_price(bar, exit_price_field)
                exit_cost = _compute_cost(exit_price, trade.quantity, "sell", cost_config)
                pnl = (exit_price - trade.entry_price) * trade.quantity - trade.entry_cost - exit_cost
                pnl_pct = pnl / (trade.entry_price * trade.quantity)
                hold_days = (current_date - trade.entry_date).days

                trade.exit_date = current_date
                trade.exit_price = exit_price
                trade.exit_reason = exit_reason
                trade.exit_cost = exit_cost
                trade.pnl = round(pnl, 2)
                trade.pnl_pct = round(pnl_pct, 4)
                trade.hold_days = hold_days

                cash += exit_price * trade.quantity - exit_cost
                completed_trades.append(trade)
                del open_trades[symbol_id]
                peak_prices.pop(symbol_id, None)

            if len(open_trades) + len(pending_entries) >= max_positions:
                continue

            for symbol_id in symbol_ids:
                if symbol_id in open_trades or symbol_id in pending_entries:
                    continue
                if len(open_trades) + len(pending_entries) >= max_positions:
                    break

                bar, prev_bar, history = _get_history(symbol_id, current_date)
                if bar is None:
                    continue

                score = _latest_score_on_or_before(score_map, symbol_id, current_date)

                if not _evaluate_buy_signal(symbol_id, current_date, bar, score, rule_config, history_bars=history, prev_bar=prev_bar):
                    continue

                if entry_timing == "next_open":
                    next_bar = _get_next_bar(symbol_id, current_date)
                    if next_bar is None:
                        continue
                    pending_entries[symbol_id] = {
                        "signal_date": current_date,
                        "execute_date": next_bar.trade_date,
                        "price_field": entry_price_field,
                    }
                    continue

                entry_price = _bar_price(bar, entry_price_field)
                entry_plan = _compute_entry_quantity(symbol_id, entry_price)
                if entry_plan is None:
                    continue
                quantity, entry_cost, total_cost = entry_plan
                if total_cost > cash:
                    continue

                trade = BacktestTrade(
                    run_id=run.id,
                    symbol_id=symbol_id,
                    entry_date=current_date,
                    entry_price=entry_price,
                    quantity=quantity,
                    entry_cost=entry_cost,
                )
                db.add(trade)
                db.flush()

                cash -= total_cost
                open_trades[symbol_id] = trade
                peak_prices[symbol_id] = float(bar.high)

        all_trades = completed_trades + list(open_trades.values())
        equity_curve = _compute_equity_curve(all_trades, initial_capital, date_bars, close_prices)
        stats = _compute_statistics(all_trades, initial_capital, equity_curve)

        run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        run.total_return = stats["total_return"]
        run.total_return_pct = stats["total_return_pct"]
        run.max_drawdown = stats["max_drawdown"]
        run.max_drawdown_pct = stats["max_drawdown_pct"]
        run.sharpe_ratio = stats["sharpe_ratio"]
        run.win_rate = stats["win_rate"]
        run.profit_factor = stats["profit_factor"]
        run.trade_count = stats["trade_count"]
        run.avg_holding_days = stats["avg_holding_days"]
        # 风控：equity_curve 含 NaN/inf 时 json.dumps 写入非法 JSON，前端 JSON.parse 失败
        # 用 default 兜底 + allow_nan=False 严格校验
        try:
            run.equity_curve_json = json.dumps(equity_curve, ensure_ascii=False, allow_nan=False)
        except (ValueError, OverflowError):
            # NaN/inf 存在 → 清洗为 0.0 再写入
            sanitized = []
            for point in equity_curve:
                clean_point = dict(point)
                for k, v in clean_point.items():
                    if isinstance(v, float) and (v != v or v in (float('inf'), float('-inf'))):
                        clean_point[k] = 0.0
                sanitized.append(clean_point)
            run.equity_curve_json = json.dumps(sanitized, ensure_ascii=False, allow_nan=False)

        db.commit()
        return run

    except Exception as e:
        run.status = "failed"
        run.error_message = str(e)
        run.finished_at = datetime.now(timezone.utc)
        try:
            db.commit()
        except Exception:
            logger.error("回测异常分支提交失败", exc_info=True)
            db.rollback()
        raise




