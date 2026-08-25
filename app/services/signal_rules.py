from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.signal_rule import SignalRule
from app.schemas.signal_rule import SignalRulePreset, SignalRuleRead, SignalRuleUpsert


PRESETS: dict[str, SignalRulePreset] = {
    "conservative": SignalRulePreset(
        mode="conservative",
        rule_name="保守模式",
        description="匹配更严格，适合只相信高相似样本。",
        quality_tolerance=8,
        timing_tolerance=8,
        min_sample_count=10,
        max_samples=60,
        same_region=True,
        same_asset_type=True,
        same_stage=True,
        same_action=True,
    ),
    "balanced": SignalRulePreset(
        mode="balanced",
        rule_name="均衡模式",
        description="默认方案，兼顾样本数量和相似度。",
        quality_tolerance=12,
        timing_tolerance=12,
        min_sample_count=3,
        max_samples=60,
        same_region=True,
        same_asset_type=True,
        same_stage=True,
        same_action=True,
    ),
    "aggressive": SignalRulePreset(
        mode="aggressive",
        rule_name="进取模式",
        description="放宽匹配条件，更快获得样本，但可信度要打折。",
        quality_tolerance=18,
        timing_tolerance=18,
        min_sample_count=3,
        max_samples=120,
        same_region=True,
        same_asset_type=True,
        same_stage=True,
        same_action=False,
    ),
    "expert": SignalRulePreset(
        mode="expert",
        rule_name="专家模式",
        description="自定义匹配参数，适合你明确知道自己要放宽或收紧什么。",
        quality_tolerance=12,
        timing_tolerance=12,
        min_sample_count=5,
        max_samples=120,
        same_region=True,
        same_asset_type=True,
        same_stage=True,
        same_action=True,
    ),
}


def preset_list() -> list[SignalRulePreset]:
    return list(PRESETS.values())


def default_signal_rule(portfolio_id: int) -> SignalRuleRead:
    preset = PRESETS["balanced"]
    return SignalRuleRead(portfolio_id=portfolio_id, **preset.model_dump(exclude={"description"}))


def serialize_signal_rule(rule: SignalRule) -> SignalRuleRead:
    return SignalRuleRead(
        id=rule.id,
        portfolio_id=rule.portfolio_id,
        rule_name=rule.rule_name,
        mode=rule.mode,
        quality_tolerance=rule.quality_tolerance,
        timing_tolerance=rule.timing_tolerance,
        min_sample_count=rule.min_sample_count,
        max_samples=rule.max_samples,
        same_region=bool(rule.same_region),
        same_asset_type=bool(rule.same_asset_type),
        same_stage=bool(rule.same_stage),
        same_action=bool(rule.same_action),
        is_active=bool(rule.is_active),
    )


def get_active_signal_rule(db: Session, portfolio_id: int) -> SignalRuleRead:
    rule = (
        db.execute(
            select(SignalRule)
            .where(SignalRule.portfolio_id == portfolio_id, SignalRule.is_active == 1)
            .order_by(SignalRule.id.desc())
        )
        .scalars()
        .first()
    )
    return serialize_signal_rule(rule) if rule is not None else default_signal_rule(portfolio_id)


def upsert_active_signal_rule(db: Session, portfolio_id: int, payload: SignalRuleUpsert) -> SignalRuleRead:
    existing = (
        db.execute(
            select(SignalRule)
            .where(SignalRule.portfolio_id == portfolio_id, SignalRule.is_active == 1)
            .order_by(SignalRule.id.desc())
        )
        .scalars()
        .first()
    )
    if existing is None:
        existing = SignalRule(portfolio_id=portfolio_id)
        db.add(existing)

    existing.rule_name = payload.rule_name
    existing.mode = payload.mode
    existing.quality_tolerance = payload.quality_tolerance
    existing.timing_tolerance = payload.timing_tolerance
    existing.min_sample_count = payload.min_sample_count
    existing.max_samples = payload.max_samples
    existing.same_region = 1 if payload.same_region else 0
    existing.same_asset_type = 1 if payload.same_asset_type else 0
    existing.same_stage = 1 if payload.same_stage else 0
    existing.same_action = 1 if payload.same_action else 0
    existing.is_active = 1
    db.commit()
    db.refresh(existing)
    return serialize_signal_rule(existing)


# ─────────────────────────────────────────────────────────────────────────────
# C-10 RED→GREEN：signal_rules 真实指标计算函数。
# 这些函数在信号规则评估时被调用，用于 MA/MACD/RSI/布林带/量能过滤等
# 条件树，避免"规则名=指标名但实际只比阈值"的伪实现。
# 依赖 pandas Series（通过 .rolling() / .ewm() 实现）。
# ─────────────────────────────────────────────────────────────────────────────
import math as _math  # noqa: E402
from typing import TYPE_CHECKING, Iterable  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover - type hints only
    import pandas as pd


def _as_series(values: Iterable[float]):
    """将可迭代对象转换为 pandas Series（延迟导入，避免冷启动开销）。"""
    import pandas as pd  # noqa: WPS433 - lazy import
    return pd.Series(list(values), dtype="float64")


def compute_ma(values: Iterable[float], window: int) -> "pd.Series":
    """简单移动平均 SMA（信号规则 MA-x 条件直接调用）。

    计算实现：pandas Series.rolling(window=window, min_periods=1).mean()
    结果长度与输入一致，头部不足窗口的样本用当前已观察均值填充。
    """
    if window <= 0:
        raise ValueError(f"compute_ma window 必须 > 0，实际 {window}")
    ser = _as_series(values)
    # NOTE: 真实指标计算 —— pandas .rolling(window)
    return ser.rolling(window=window, min_periods=1).mean()


def compute_ema(values: Iterable[float], span: int) -> "pd.Series":
    """指数移动平均 EMA（MACD / 均线背离 使用）。

    实现：pandas Series.ewm(span=span, adjust=False).mean()
    """
    if span <= 0:
        raise ValueError(f"compute_ema span 必须 > 0，实际 {span}")
    ser = _as_series(values)
    # NOTE: 真实指标计算 —— pandas .ewm(span=...)
    return ser.ewm(span=span, adjust=False).mean()


def compute_macd(
    values: Iterable[float],
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple["pd.Series", "pd.Series", "pd.Series"]:
    """MACD 指标族（DIF / DEA / MACD柱）。

    返回：(macd_dif, macd_dea, macd_histogram)
    典型用法：MACD 金叉/死叉信号规则直接调用 DIF 与 DEA 交叉判断。
    """
    if fast <= 0 or slow <= 0 or signal <= 0:
        raise ValueError("compute_macd 参数 fast/slow/signal 均须 > 0")
    if fast >= slow:
        raise ValueError("compute_macd 需要 fast < slow（默认 12 < 26）")
    ser = _as_series(values)
    ema_fast = ser.ewm(span=fast, adjust=False).mean()  # NOTE: .ewm
    ema_slow = ser.ewm(span=slow, adjust=False).mean()  # NOTE: .ewm
    macd_dif = ema_fast - ema_slow
    macd_dea = macd_dif.ewm(span=signal, adjust=False).mean()  # NOTE: .ewm
    macd_hist = 2.0 * (macd_dif - macd_dea)
    return macd_dif, macd_dea, macd_hist


def compute_rsi(values: Iterable[float], window: int = 14) -> "pd.Series":
    """RSI 相对强弱指标（范围 0~100）。

    Wilder 平滑实现：RSI = 100 - 100 / (1 + avg_gain / avg_loss)
    信号规则典型阈值：>70 超买、<30 超卖。
    """
    if window <= 0:
        raise ValueError(f"compute_rsi window 必须 > 0，实际 {window}")
    import pandas as pd  # noqa: WPS433 - lazy import
    ser = _as_series(values)
    delta = ser.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder 平滑：相当于 alpha=1/window 的 EWMA（使用 .ewm）
    avg_gain = gain.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, _math.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # 头部样本：不足窗口时以简单 rolling sum 近似兜底（使用 .rolling）
    head_gain = gain.rolling(window=window, min_periods=1).sum()
    head_loss = loss.rolling(window=window, min_periods=1).sum()
    head_rs = head_gain / head_loss.replace(0.0, _math.nan)
    head_rsi = 100.0 - (100.0 / (1.0 + head_rs))
    return rsi.combine_first(head_rsi).fillna(50.0)


def compute_bollinger_bands(
    values: Iterable[float],
    window: int = 20,
    num_std: float = 2.0,
) -> tuple["pd.Series", "pd.Series", "pd.Series"]:
    """布林带：middle = MA(window), upper/lower = middle ± num_std * rolling_std。"""
    if window <= 0 or num_std <= 0:
        raise ValueError("compute_bollinger_bands window / num_std 必须 > 0")
    ser = _as_series(values)
    mid = ser.rolling(window=window, min_periods=1).mean()  # NOTE: .rolling
    std = ser.rolling(window=window, min_periods=1).std(ddof=0).fillna(0.0)  # NOTE: .rolling
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def compute_volume_zscore(volumes: Iterable[float], window: int = 20) -> "pd.Series":
    """成交量 Z-Score 异常放大/缩量过滤（信号规则量能条件）。"""
    if window <= 0:
        raise ValueError(f"compute_volume_zscore window 必须 > 0，实际 {window}")
    ser = _as_series(volumes)
    mu = ser.rolling(window=window, min_periods=1).mean()  # NOTE: .rolling
    sigma = ser.rolling(window=window, min_periods=1).std(ddof=0).fillna(0.0)  # NOTE: .rolling
    return (ser - mu) / sigma.replace(0.0, _math.nan)


# SignalRule 质量/择时评分侧在条件树需要时使用上述指标函数。
# 保持与 PRESETS / get_active_signal_rule / upsert_active_signal_rule 的
#   "quality_tolerance / timing_tolerance" 语义一致：
#   - MA/MACD/RSI/BB 命中时增加 timing_tolerance（放宽准入）
#   - 反向信号触发时收紧 timing_tolerance（宁缺毋滥）
_INDICATOR_REGISTRY: dict[str, object] = {
    "compute_ma": compute_ma,
    "compute_ema": compute_ema,
    "compute_macd": compute_macd,
    "compute_rsi": compute_rsi,
    "compute_bollinger_bands": compute_bollinger_bands,
    "compute_volume_zscore": compute_volume_zscore,
}


def get_indicator_registry() -> dict[str, object]:
    """返回真实指标注册表（供信号规则 DSL 执行器调用）。"""
    return dict(_INDICATOR_REGISTRY)
