"""评分配置引擎。

负责：
1. 系统预设的内置定义（股票/ETF 各 4 套）。
2. 读取当前激活预设。
3. 按预设计算 symbol 的维度分、最终分。
4. 维度阈值过滤。

设计原则（与 docs/scoring-config-optimization-plan.md 对齐）：
- 当前激活预设只影响未来；历史 scores 保留原始快照。
- 股票/ETF 分开配置。
- 轻量版本化：每次保存用户预设生成新 version。
- 缺失数据策略：neutral=50 / ignore=剔除 / penalty=30。
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import date, datetime, timezone
from statistics import mean, pstdev
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.score import Score
from app.models.scoring_config import ScoringConfig
from app.models.symbol import Symbol


# ----------------------------------------------------------------------------
# 系统预设定义
# ----------------------------------------------------------------------------

def _stock_dimension(
    key: str, name: str, score_bucket: str, weight: float,
    factor_key: str, factor_source: str = "builtin", factor_weight: float = 1.0,
    direction: str = "higher_better", enabled: bool = True,
    filter_enabled: bool = False, filter_op: str = "gte", filter_value: float = 60.0,
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "enabled": enabled,
        "score_bucket": score_bucket,
        "weight": weight,
        "filter": {"enabled": filter_enabled, "operator": filter_op, "value": filter_value},
        "factors": [
            {"key": factor_key, "source": factor_source, "weight": factor_weight, "direction": direction}
        ],
    }


STOCK_SYSTEM_PRESETS: list[dict[str, Any]] = [
    {
        "preset_key": "balanced_opportunity",
        "name": "均衡机会",
        "description": "默认股票机会挖掘；趋势、时点、流动性较均衡，适合作为初始激活预设。",
        "asset_type": "stock",
        "final_weights": {"quality": 0.4, "timing": 0.5, "news": 0.1},
        "dimensions": [
            _stock_dimension("trend", "趋势", "quality", 0.22, "trend_score"),
            _stock_dimension("momentum", "动量", "timing", 0.18, "momentum_score"),
            _stock_dimension("volatility", "波动", "quality", 0.15, "volatility_score", direction="lower_better"),
            _stock_dimension("liquidity", "流动性", "quality", 0.15, "liquidity_score"),
            _stock_dimension("activity", "交易活跃", "timing", 0.20, "amount_activity", factor_source="daily_bar"),
            _stock_dimension("breadth", "题材", "quality", 0.10, "breadth_score"),
        ],
    },
    {
        "preset_key": "steady_value",
        "name": "稳健股质",
        "description": "中低频、偏稳健选股；更重视股质、波动控制、估值预留，降低追高倾向。",
        "asset_type": "stock",
        "final_weights": {"quality": 0.55, "timing": 0.35, "news": 0.10},
        "dimensions": [
            _stock_dimension("trend", "趋势", "quality", 0.18, "trend_score"),
            _stock_dimension("momentum", "动量", "timing", 0.12, "momentum_score"),
            _stock_dimension("volatility", "波动", "quality", 0.22, "volatility_score", direction="lower_better"),
            _stock_dimension("liquidity", "流动性", "quality", 0.18, "liquidity_score"),
            _stock_dimension("activity", "交易活跃", "timing", 0.10, "amount_activity", factor_source="daily_bar"),
            _stock_dimension("breadth", "题材", "quality", 0.12, "breadth_score"),
            _stock_dimension("valuation", "估值", "quality", 0.08, "pe_score", factor_source="fundamental", enabled=False, direction="lower_or_range_better"),
        ],
    },
    {
        "preset_key": "trend_growth",
        "name": "趋势成长",
        "description": "趋势跟随、强者恒强；更重视趋势、动量、突破，适合找强势票。",
        "asset_type": "stock",
        "final_weights": {"quality": 0.35, "timing": 0.55, "news": 0.10},
        "dimensions": [
            _stock_dimension("trend", "趋势", "quality", 0.28, "trend_score"),
            _stock_dimension("momentum", "动量", "timing", 0.25, "momentum_score"),
            _stock_dimension("volatility", "波动", "quality", 0.10, "volatility_score", direction="lower_better"),
            _stock_dimension("liquidity", "流动性", "quality", 0.12, "liquidity_score"),
            _stock_dimension("activity", "交易活跃", "timing", 0.15, "amount_activity", factor_source="daily_bar"),
            _stock_dimension("breadth", "题材", "quality", 0.10, "breadth_score"),
        ],
    },
    {
        "preset_key": "active_capital",
        "name": "活跃资金",
        "description": "短线活跃、资金异动；更重视成交活跃、换手、资金流预留，适合“大海捞针”。",
        "asset_type": "stock",
        "final_weights": {"quality": 0.30, "timing": 0.60, "news": 0.10},
        "dimensions": [
            _stock_dimension("trend", "趋势", "quality", 0.15, "trend_score"),
            _stock_dimension("momentum", "动量", "timing", 0.20, "momentum_score"),
            _stock_dimension("volatility", "波动", "quality", 0.10, "volatility_score", direction="lower_better"),
            _stock_dimension("liquidity", "流动性", "quality", 0.15, "liquidity_score"),
            _stock_dimension("activity", "交易活跃", "timing", 0.30, "amount_activity", factor_source="daily_bar"),
            _stock_dimension("breadth", "题材", "quality", 0.10, "breadth_score"),
            _stock_dimension("capital_flow", "资金流", "timing", 0.0, "main_net_inflow_score", factor_source="capital_flow", enabled=False),
        ],
    },
]


ETF_SYSTEM_PRESETS: list[dict[str, Any]] = [
    {
        "preset_key": "etf_balanced",
        "name": "ETF均衡机会",
        "description": "默认 ETF 机会挖掘；趋势、动量、流动性、波动较均衡。",
        "asset_type": "etf",
        "final_weights": {"quality": 0.35, "timing": 0.55, "news": 0.10},
        "dimensions": [
            _stock_dimension("trend", "趋势", "quality", 0.28, "trend_score"),
            _stock_dimension("momentum", "动量", "timing", 0.22, "momentum_score"),
            _stock_dimension("liquidity", "流动性", "quality", 0.20, "liquidity_score", filter_enabled=True, filter_value=60),
            _stock_dimension("drawdown_risk", "回撤风险", "quality", 0.15, "volatility_score", direction="lower_better"),
            _stock_dimension("activity", "成交活跃", "timing", 0.15, "amount_activity", factor_source="daily_bar"),
        ],
    },
    {
        "preset_key": "etf_trend_rotation",
        "name": "ETF趋势轮动",
        "description": "行业/主题 ETF 轮动；更重视趋势、动量和突破。",
        "asset_type": "etf",
        "final_weights": {"quality": 0.30, "timing": 0.60, "news": 0.10},
        "dimensions": [
            _stock_dimension("trend", "趋势", "quality", 0.32, "trend_score"),
            _stock_dimension("momentum", "动量", "timing", 0.28, "momentum_score"),
            _stock_dimension("liquidity", "流动性", "quality", 0.15, "liquidity_score"),
            _stock_dimension("drawdown_risk", "回撤风险", "quality", 0.10, "volatility_score", direction="lower_better"),
            _stock_dimension("activity", "成交活跃", "timing", 0.15, "amount_activity", factor_source="daily_bar"),
        ],
    },
    {
        "preset_key": "etf_low_volatility",
        "name": "ETF低波稳健",
        "description": "宽基、债券、低波 ETF；更重视波动、回撤和流动性。",
        "asset_type": "etf",
        "final_weights": {"quality": 0.55, "timing": 0.35, "news": 0.10},
        "dimensions": [
            _stock_dimension("trend", "趋势", "quality", 0.20, "trend_score"),
            _stock_dimension("momentum", "动量", "timing", 0.12, "momentum_score"),
            _stock_dimension("liquidity", "流动性", "quality", 0.25, "liquidity_score", filter_enabled=True, filter_value=60),
            _stock_dimension("drawdown_risk", "回撤风险", "quality", 0.28, "volatility_score", direction="lower_better", filter_enabled=True, filter_value=55),
            _stock_dimension("activity", "成交活跃", "timing", 0.15, "amount_activity", factor_source="daily_bar"),
        ],
    },
    {
        "preset_key": "etf_liquidity_first",
        "name": "ETF高流动",
        "description": "大资金进出友好；更重视成交额、流动性和价差风险预留。",
        "asset_type": "etf",
        "final_weights": {"quality": 0.45, "timing": 0.45, "news": 0.10},
        "dimensions": [
            _stock_dimension("trend", "趋势", "quality", 0.18, "trend_score"),
            _stock_dimension("momentum", "动量", "timing", 0.15, "momentum_score"),
            _stock_dimension("liquidity", "流动性", "quality", 0.35, "liquidity_score", filter_enabled=True, filter_value=65),
            _stock_dimension("drawdown_risk", "回撤风险", "quality", 0.15, "volatility_score", direction="lower_better"),
            _stock_dimension("activity", "成交活跃", "timing", 0.17, "amount_activity", factor_source="daily_bar"),
        ],
    },
    # P2：ETF 基本面焦点预设（接入溢价折价数据）
    {
        "preset_key": "etf_fundamental_focus",
        "name": "ETF基本面焦点",
        "description": "P2 新增；纳入溢价折价、基金规模等 ETF 特有指标，适合套利与基金筛选。",
        "asset_type": "etf",
        "final_weights": {"quality": 0.55, "timing": 0.35, "news": 0.10},
        "dimensions": [
            _stock_dimension("trend", "趋势", "quality", 0.20, "trend_score"),
            _stock_dimension("momentum", "动量", "timing", 0.15, "momentum_score"),
            _stock_dimension("liquidity", "流动性", "quality", 0.20, "liquidity_score", filter_enabled=True, filter_value=60),
            _stock_dimension("drawdown_risk", "回撤风险", "quality", 0.15, "volatility_score", direction="lower_better"),
            _stock_dimension("activity", "成交活跃", "timing", 0.10, "amount_activity", factor_source="daily_bar"),
            _stock_dimension("premium_discount", "溢价折价", "quality", 0.20, "premium_discount_score", factor_source="etf_basic", direction="range_better", filter_enabled=True, filter_value=40),
        ],
    },
]


ALL_SYSTEM_PRESETS: list[dict[str, Any]] = STOCK_SYSTEM_PRESETS + ETF_SYSTEM_PRESETS

# 默认激活预设
DEFAULT_ACTIVE_PRESET_KEYS: dict[str, str] = {
    "stock": "balanced_opportunity",
    "etf": "etf_balanced",
}


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------

def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def _grade(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def _safe_date(value):
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


# ----------------------------------------------------------------------------
# 激活预设读取
# ----------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# 进程级 TTL 缓存：消除批量评分/挖掘热路径上 N 次重复查询
# 缓存 key = asset_type，value = (config, fetched_at)
# TTL=30s 平衡"配置变更即时生效"与"批量调用零冗余"
_ACTIVE_CONFIG_CACHE: dict[str, tuple[ScoringConfig | None, float]] = {}
_ACTIVE_CONFIG_CACHE_TTL = 30.0  # 秒
_ACTIVE_CONFIG_CACHE_LOCK = threading.Lock()


def _snapshot_scoring_config(config: ScoringConfig | None) -> ScoringConfig | None:
    if config is None:
        return None
    return ScoringConfig(
        id=config.id,
        asset_type=config.asset_type,
        preset_key=config.preset_key,
        name=config.name,
        description=config.description,
        version=config.version,
        config_json=config.config_json,
        preset_source=config.preset_source,
        is_system=config.is_system,
        is_active=config.is_active,
        is_latest=config.is_latest,
        base_preset_key=config.base_preset_key,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


def get_active_scoring_config(db: Session, asset_type: str, *, use_cache: bool = True) -> ScoringConfig | None:
    """读取指定 asset_type 当前激活的评分预设。

    风控加固：默认走进程级 TTL 缓存（30s），消除批量评分 / 挖掘热路径的 N 次冗余查询。
    配置变更后 30s 内自动生效；如需立即使效，调用 invalidate_active_config_cache()。
    """
    if not use_cache:
        return _snapshot_scoring_config(_query_active_scoring_config(db, asset_type))
    now = time.time()
    with _ACTIVE_CONFIG_CACHE_LOCK:
        cached = _ACTIVE_CONFIG_CACHE.get(asset_type)
        if cached is not None and (now - cached[1]) < _ACTIVE_CONFIG_CACHE_TTL:
            return cached[0]
    # 缓存未命中或已过期，查 DB
    config = _snapshot_scoring_config(_query_active_scoring_config(db, asset_type))
    with _ACTIVE_CONFIG_CACHE_LOCK:
        _ACTIVE_CONFIG_CACHE[asset_type] = (config, now)
    return config


def _query_active_scoring_config(db: Session, asset_type: str) -> ScoringConfig | None:
    """实际查询 DB（无缓存）。"""
    return db.execute(
        select(ScoringConfig)
        .where(
            ScoringConfig.asset_type == asset_type,
            ScoringConfig.is_active == 1,
            ScoringConfig.is_latest == 1,
        )
        .limit(1)
    ).scalars().first()


def invalidate_active_config_cache() -> None:
    """失效激活预设缓存。配置写入/更新后调用。"""
    with _ACTIVE_CONFIG_CACHE_LOCK:
        _ACTIVE_CONFIG_CACHE.clear()


def get_scoring_config_by_id(db: Session, config_id: int) -> ScoringConfig | None:
    return db.get(ScoringConfig, config_id)


def parse_config_json(config: ScoringConfig) -> dict[str, Any]:
    """安全解析 config_json。"""
    try:
        return json.loads(config.config_json) if config.config_json else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# ----------------------------------------------------------------------------
# 种子数据初始化
# ----------------------------------------------------------------------------

def seed_system_scoring_configs(db: Session) -> None:
    """初始化系统内置评分预设。

    幂等：仅当 asset_type + preset_key 不存在时插入。
    每类资产首次初始化时，按 DEFAULT_ACTIVE_PRESET_KEYS 设置激活。
    """
    existing_keys = {
        (row.asset_type, row.preset_key)
        for row in db.execute(select(ScoringConfig)).scalars().all()
    }

    # 标记某 asset_type 是否首次初始化（无任何记录 -> 需要默认激活）
    stock_has_any = any(at == "stock" for at, _ in existing_keys)
    etf_has_any = any(at == "etf" for at, _ in existing_keys)
    need_default_active = {
        "stock": not stock_has_any,
        "etf": not etf_has_any,
    }

    for preset in ALL_SYSTEM_PRESETS:
        key = (preset["asset_type"], preset["preset_key"])
        if key in existing_keys:
            continue
        active = 1 if (need_default_active.get(preset["asset_type"]) and preset["preset_key"] == DEFAULT_ACTIVE_PRESET_KEYS.get(preset["asset_type"])) else 0
        cfg = ScoringConfig(
            asset_type=preset["asset_type"],
            preset_key=preset["preset_key"],
            name=preset["name"],
            description=preset.get("description"),
            version=1,
            config_json=json.dumps(preset, ensure_ascii=False),
            preset_source="system",
            is_system=1,
            is_active=active,
            is_latest=1,
            base_preset_key=None,
        )
        db.add(cfg)

    db.flush()


# ----------------------------------------------------------------------------
# 因子计算
# ----------------------------------------------------------------------------

def _compute_builtin_factors(bars: list[DailyBar], symbol: Symbol) -> dict[str, float | None]:
    """基于 K 线计算所有内置因子的原始 0-100 标准化分。

    返回字典包含：
        trend_score, momentum_score, volatility_score, liquidity_score,
        breadth_score, event_score, breakout_score, pullback_score,
        overheat_penalty, amount_activity, turnover_activity
    """
    if len(bars) < 5:
        return {
            "trend_score": 50.0,
            "momentum_score": 45.0,
            "volatility_score": 50.0,
            "liquidity_score": 45.0,
            "breadth_score": 55.0 if symbol.theme else 50.0,
            "event_score": 50.0,
            "breakout_score": 50.0,
            "pullback_score": 40.0,
            "overheat_penalty": 0.0,
            "amount_activity": 45.0,
            "turnover_activity": 45.0,
        }

    closes = [bar.close for bar in bars]
    amounts = [bar.amount or 0 for bar in bars]
    turnovers = [bar.turnover_rate or 0 for bar in bars]
    returns: list[float] = []
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

    # 交易活跃因子：amount_activity（成交额活跃度，对数缩放至 0-100）
    amount_activity = _clamp(20 + math.log10(max(avg_amount, 1)) * 10)
    # 换手活跃度：取近20日平均换手率，1%->50, 3%->70, 6%->85, 10%+->95
    avg_turnover = mean(turnovers[-20:]) if turnovers else 0
    if avg_turnover <= 0:
        turnover_activity = 45.0
    else:
        # 区间越好型：1-5% 较佳，过高或过低扣分
        if avg_turnover <= 1:
            turnover_activity = 40 + avg_turnover * 10  # 0->40, 1->50
        elif avg_turnover <= 5:
            turnover_activity = 50 + (avg_turnover - 1) * 7  # 1->50, 5->78
        elif avg_turnover <= 10:
            turnover_activity = 78 - (avg_turnover - 5) * 1.6  # 5->78, 10->70
        else:
            turnover_activity = max(40, 70 - (avg_turnover - 10) * 2)
        turnover_activity = _clamp(turnover_activity)

    breadth_score = 60.0 if symbol.theme else 50.0
    event_score = 50.0

    breakout_score = 70.0 if len(closes) >= 20 and last_close >= max(closes[-20:]) else 50.0
    pullback_score = 65.0 if ma20 and last_close >= ma20 * 0.98 else 40.0
    overheat_penalty = 20.0 if ma20 and last_close >= ma20 * 1.12 else 0.0

    return {
        "trend_score": _clamp(trend_component),
        "momentum_score": _clamp(momentum_component),
        "volatility_score": _clamp(volatility_component),
        "liquidity_score": _clamp(liquidity_component),
        "breadth_score": breadth_score,
        "event_score": event_score,
        "breakout_score": breakout_score,
        "pullback_score": pullback_score,
        "overheat_penalty": overheat_penalty,
        "amount_activity": amount_activity,
        "turnover_activity": turnover_activity,
    }


def _enabled_external_factor_keys(config_json: dict[str, Any]) -> set[str]:
    """Return external factor keys that are actually enabled by the config."""
    keys: set[str] = set()
    for dim in config_json.get("dimensions", []):
        if not dim.get("enabled", True):
            continue
        for factor in dim.get("factors", []):
            source = factor.get("source", "builtin")
            key = factor.get("key")
            if source not in ("builtin", "daily_bar") and key:
                keys.add(key)
    return keys


def _compute_external_factors(
    db: Session,
    symbol: Symbol,
    trade_date: date,
    required_factor_keys: set[str] | None = None,
) -> dict[str, float | None]:
    """P2：外部数据因子计算（PE/PB 估值、资金流、ETF 特有指标）。

    按需拉取：若 DB 无最新数据或数据过期，触发同步。
    返回 dict 仅包含已接入的因子；未接入数据源的因子不在此 dict 中。
    """
    required = required_factor_keys or set()
    if not required:
        return {}

    factors: dict[str, float | None] = {}
    try:
        if symbol.asset_type == "stock":
            # 股票估值
            if "pe_score" in required:
                from app.services.fundamental_data import get_pe_score
                pe_score = get_pe_score(db, symbol, trade_date)
                if pe_score is not None:
                    factors["pe_score"] = pe_score
            # 资金流
            if "main_net_inflow_score" in required:
                from app.services.capital_flow_data import get_main_net_inflow_score
                flow_score = get_main_net_inflow_score(db, symbol, trade_date)
                if flow_score is not None:
                    factors["main_net_inflow_score"] = flow_score
        elif symbol.asset_type == "etf":
            # ETF 特有指标
            if "premium_discount_score" in required:
                from app.services.etf_basic_data import get_premium_discount_score
                pd_score = get_premium_discount_score(db, symbol, trade_date)
                if pd_score is not None:
                    factors["premium_discount_score"] = pd_score
    except Exception as exc:
        # 外部数据失败不应阻断主评分流程
        import logging
        logging.getLogger(__name__).warning(
            "External factors failed for %s: %s", symbol.symbol, exc
        )
    return factors


def _data_credibility(bars: list[DailyBar], trade_date: date) -> float:
    bar_count = len(bars)
    if bar_count < 5:
        return 0.2
    bar_factor = min(1.0, 0.4 + (bar_count - 5) * 0.02)
    days_stale = (date.today() - _safe_date(trade_date)).days
    if days_stale <= 7:
        freshness_factor = max(0.3, 1.0 - days_stale * 0.1)
    else:
        freshness_factor = max(0.1, 0.5 - (days_stale - 7) * 0.05)
    return round(min(1.0, bar_factor * freshness_factor), 2)


# ----------------------------------------------------------------------------
# 维度评分
# ----------------------------------------------------------------------------

def _resolve_factor_score(factor_key: str, factor_scores: dict[str, float | None]) -> float | None:
    """从内置因子字典中取分；不在内置列表的（如 pe_score、main_net_inflow_score）返回 None。"""
    return factor_scores.get(factor_key)


def _apply_missing_policy(value: float | None, policy: str = "neutral") -> float:
    if value is not None:
        return value
    if policy == "neutral":
        return 50.0
    if policy == "penalty":
        return 30.0
    # ignore 由调用方处理
    return 50.0


def evaluate_dimension_scores(
    factor_scores: dict[str, float | None],
    config_json: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any]]:
    """按预设计算每个维度的得分。

    返回:
        dimension_scores: { dim_key: score }
        factor_detail:   { factor_key: { raw_value, normalized_value, weight, contribution } }
    """
    dimensions = config_json.get("dimensions", [])
    dim_scores: dict[str, float] = {}
    factor_detail: dict[str, Any] = {}

    for dim in dimensions:
        if not dim.get("enabled", True):
            continue
        dim_key = dim["key"]
        factors = dim.get("factors", [])
        if not factors:
            continue

        # 收集启用的因子
        active_factors = []
        for f in factors:
            fkey = f["key"]
            raw = _resolve_factor_score(fkey, factor_scores)
            normalized = _apply_missing_policy(raw, f.get("missing_policy", "neutral"))
            # 未接入数据源的因子（如 pe_score、main_net_inflow_score）按 ignore 处理
            if raw is None and f.get("source") not in ("builtin", "daily_bar"):
                # 跳过未接入数据源
                continue
            active_factors.append((fkey, normalized, float(f.get("weight", 1.0))))

        if not active_factors:
            continue

        total_w = sum(w for _, _, w in active_factors)
        if total_w <= 0:
            continue
        weighted = sum(n * w for _, n, w in active_factors) / total_w
        dim_scores[dim_key] = _clamp(weighted)

        for fkey, normalized, w in active_factors:
            factor_detail[fkey] = {
                "normalized_value": normalized,
                "weight": w / total_w,
                "contribution": round(normalized * (w / total_w), 2),
            }

    return dim_scores, factor_detail


def _aggregate_final_scores(
    dim_scores: dict[str, float],
    config_json: dict[str, Any],
    factor_scores: dict[str, float | None],
) -> tuple[float, float, float, str, str]:
    """根据 final_weights 和维度 score_bucket 聚合 quality/timing/priority。

    final_weights 形如: { "quality": 0.4, "timing": 0.5, "news": 0.1 }
    """
    final_weights = config_json.get("final_weights", {"quality": 0.4, "timing": 0.5, "news": 0.1})
    dimensions = config_json.get("dimensions", [])

    # 按 bucket 收集 (dim_score, dim_weight)
    bucket_dims: dict[str, list[tuple[float, float]]] = {"quality": [], "timing": [], "news": []}
    for dim in dimensions:
        if not dim.get("enabled", True):
            continue
        dkey = dim["key"]
        if dkey not in dim_scores:
            continue
        bucket = dim.get("score_bucket", "quality")
        bucket_dims.setdefault(bucket, []).append((dim_scores[dkey], float(dim.get("weight", 1.0))))

    def _weighted_avg(items: list[tuple[float, float]]) -> float:
        if not items:
            return 50.0
        total = sum(w for _, w in items)
        if total <= 0:
            return 50.0
        return sum(s * w for s, w in items) / total

    quality = _clamp(_weighted_avg(bucket_dims.get("quality", [])))
    timing = _clamp(_weighted_avg(bucket_dims.get("timing", [])))
    news = _clamp(_weighted_avg(bucket_dims.get("news", [50.0, 1.0])))  # news 默认 50

    q_w = float(final_weights.get("quality", 0.4))
    t_w = float(final_weights.get("timing", 0.5))
    n_w = float(final_weights.get("news", 0.1))
    total_w = q_w + t_w + n_w
    if total_w <= 0:
        total_w = 1.0
    priority = _clamp((quality * q_w + timing * t_w + news * n_w) / total_w)

    # stage/action 判定（沿用原逻辑）
    overheat = factor_scores.get("overheat_penalty", 0.0) or 0.0
    momentum = factor_scores.get("momentum_score", 50.0) or 50.0
    # ma 判定通过 trend_score 间接体现：trend_score >=60 视为上行结构
    trend = factor_scores.get("trend_score", 50.0) or 50.0
    breakout = factor_scores.get("breakout_score", 50.0) or 50.0

    if overheat >= 20:
        stage, action = "overheat", "reduce"
    elif trend >= 60 and momentum >= 60 and breakout >= 65:
        stage, action = "accel", "hold"
    elif trend >= 55 and momentum > 50:
        stage, action = "start", "open"
    else:
        stage, action = "cooldown", "hold"

    return quality, timing, priority, stage, action


def passes_dimension_filters(
    dim_scores: dict[str, float],
    config_json: dict[str, Any],
) -> tuple[bool, list[str]]:
    """检查是否通过预设中的维度硬性筛选阈值。

    返回 (passed, failed_dim_names)
    """
    failed: list[str] = []
    for dim in config_json.get("dimensions", []):
        if not dim.get("enabled", True):
            continue
        flt = dim.get("filter") or {}
        if not flt.get("enabled", False):
            continue
        dkey = dim["key"]
        score = dim_scores.get(dkey)
        if score is None:
            failed.append(dim.get("name", dkey))
            continue
        op = flt.get("operator", "gte")
        threshold = float(flt.get("value", 0))
        if op == "gte" and not (score >= threshold):
            failed.append(dim.get("name", dkey))
        elif op == "lte" and not (score <= threshold):
            failed.append(dim.get("name", dkey))
        elif op == "gt" and not (score > threshold):
            failed.append(dim.get("name", dkey))
        elif op == "lt" and not (score < threshold):
            failed.append(dim.get("name", dkey))
    return (len(failed) == 0, failed)


# ----------------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------------

def calculate_symbol_score_with_config(
    db: Session,
    symbol: Symbol,
    trade_date: date,
    config: ScoringConfig | None = None,
) -> Score:
    """按预设计算 symbol 评分，写入 scores 表（含配置快照）。

    Args:
        db: SQLAlchemy session
        symbol: Symbol 实例
        trade_date: 评分日
        config: 指定预设；为 None 时按 symbol.asset_type 读取当前激活预设。
    """
    asset_type = symbol.asset_type or "stock"
    if config is None:
        config = get_active_scoring_config(db, asset_type)
    # 兜底：没有任何激活预设时（不应发生），返回 None 配置走旧逻辑
    config_json = parse_config_json(config) if config is not None else {}

    # 取 K 线
    bars = db.execute(
        select(DailyBar)
        .where(DailyBar.symbol_id == symbol.id, DailyBar.trade_date <= trade_date)
        .order_by(DailyBar.trade_date.desc())
        .limit(80)
    ).scalars().all()
    bars = list(reversed(bars))

    # 因子
    factor_scores = _compute_builtin_factors(bars, symbol)
    credibility = _data_credibility(bars, trade_date)

    if config is None or not config_json:
        # 无配置兜底（理论上不会发生，因为 init_db 会种子系统预设）
        return _legacy_score_write(
            db, symbol, trade_date, factor_scores, credibility
        )

    # P2：只拉取当前配置实际启用的外部因子，避免默认预设全市场扫描时打爆外部接口。
    external_factor_keys = _enabled_external_factor_keys(config_json)
    external_factors = _compute_external_factors(db, symbol, trade_date, external_factor_keys)
    factor_scores.update(external_factors)

    # 维度评分
    dim_scores, factor_detail = evaluate_dimension_scores(factor_scores, config_json)
    quality, timing, priority, stage, action = _aggregate_final_scores(dim_scores, config_json, factor_scores)

    # 写库（含配置快照）
    date_key = trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
    calc_batch_id = f"sc-{config.id}-v{config.version}-{date_key}"

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

    existing.quality_score = quality
    existing.quality_grade = _grade(quality)
    existing.timing_score = timing
    existing.stage = stage
    existing.action = action
    existing.priority_score = priority
    # 保留内置分项写入，便于前端雷达图等沿用
    existing.trend_score = factor_scores.get("trend_score")
    existing.momentum_score = factor_scores.get("momentum_score")
    existing.volatility_score = factor_scores.get("volatility_score")
    existing.liquidity_score = factor_scores.get("liquidity_score")
    existing.breadth_score = factor_scores.get("breadth_score")
    existing.event_score = factor_scores.get("event_score")
    existing.breakout_score = factor_scores.get("breakout_score")
    existing.pullback_score = factor_scores.get("pullback_score")
    existing.overheat_penalty = factor_scores.get("overheat_penalty")
    existing.data_credibility = credibility
    # 配置快照字段
    existing.scoring_asset_type = asset_type
    existing.scoring_config_id = config.id
    existing.scoring_preset_key = config.preset_key
    existing.scoring_preset_name = config.name
    existing.scoring_config_version = config.version
    existing.scoring_config_snapshot_json = config.config_json
    existing.dimension_scores_json = json.dumps(dim_scores, ensure_ascii=False)
    existing.factor_scores_json = json.dumps(factor_detail, ensure_ascii=False)

    db.flush()
    return existing


def calculate_universe_symbol_score(
    db: Session,
    universe_symbol,
    symbol: Symbol,
    trade_date: date,
    config: ScoringConfig | None = None,
    *,
    prefetched_bars: list | None = None,
    existing_score_map: dict[int, Score] | None = None,
) -> Score:
    """从基础表 universe_daily_bars 读K线计算评分，写 scores 表。

    与 calculate_symbol_score_with_config 的区别：
    - K线数据来源：universe_daily_bars（基础数据层），而非 daily_bars（业务层）
    - 不调 akshare，纯 DB 读取，评分速度从 5-15s/标的 降至 <50ms/标的
    - 评分结果仍写入 scores 表（关联 symbol.id），后续 run_scan 等流程不变

    Args:
        db: SQLAlchemy session
        universe_symbol: UniverseSymbol 实例（用于读 universe_daily_bars）
        symbol: Symbol 实例（用于写 scores 表，需有 .id）
        trade_date: 评分日
        config: 指定预设；为 None 时按 symbol.asset_type 读取当前激活预设。
        prefetched_bars: P1.1 批量预载的K线列表（已按 trade_date 升序排列）。
            None=未预载，函数内部查 DB；非 None=直接使用（可能为空列表）。
        existing_score_map: P1.2 批量预查的已存在 Score 字典 {symbol_id: Score}。
            None=未预查，函数内部查 DB；非 None=从字典取（不存在则新建）。
    """
    from app.models.universe import UniverseDailyBar

    asset_type = symbol.asset_type or "stock"
    if config is None:
        config = get_active_scoring_config(db, asset_type)
    config_json = parse_config_json(config) if config is not None else {}

    # P1.1：K线读取——优先使用外部预载数据，避免每标的一次 DB 查询（N+1→1）
    if prefetched_bars is None:
        bars = db.execute(
            select(UniverseDailyBar)
            .where(
                UniverseDailyBar.universe_symbol_id == universe_symbol.id,
                UniverseDailyBar.trade_date <= trade_date,
            )
            .order_by(UniverseDailyBar.trade_date.desc())
            .limit(80)
        ).scalars().all()
        bars = list(reversed(bars))
    else:
        bars = prefetched_bars

    # 因子计算（_compute_builtin_factors 只用 bar.close/amount/turnover_rate，鸭子类型兼容）
    factor_scores = _compute_builtin_factors(bars, symbol)
    credibility = _data_credibility(bars, trade_date)

    if config is None or not config_json:
        return _legacy_score_write(db, symbol, trade_date, factor_scores, credibility)

    # 外部因子（PE/PB 等，按需拉取，失败不阻断）
    external_factor_keys = _enabled_external_factor_keys(config_json)
    external_factors = _compute_external_factors(db, symbol, trade_date, external_factor_keys)
    factor_scores.update(external_factors)

    # 维度评分 + 聚合
    dim_scores, factor_detail = evaluate_dimension_scores(factor_scores, config_json)
    quality, timing, priority, stage, action = _aggregate_final_scores(dim_scores, config_json, factor_scores)

    # 写 scores 表（与 calculate_symbol_score_with_config 完全一致）
    date_key = trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
    calc_batch_id = f"sc-{config.id}-v{config.version}-{date_key}"

    # P1.2：Score 去重——优先使用外部预查字典，避免每标的一次 DB 查询（N+1→1）
    if existing_score_map is None:
        existing = db.execute(
            select(Score).where(
                Score.symbol_id == symbol.id,
                Score.trade_date == trade_date,
                Score.calc_batch_id == calc_batch_id,
            )
        ).scalars().first()
    else:
        existing = existing_score_map.get(symbol.id)

    if existing is None:
        existing = Score(
            symbol_id=symbol.id,
            trade_date=trade_date,
            calc_batch_id=calc_batch_id,
        )
        db.add(existing)

    existing.quality_score = quality
    existing.quality_grade = _grade(quality)
    existing.timing_score = timing
    existing.stage = stage
    existing.action = action
    existing.priority_score = priority
    existing.trend_score = factor_scores.get("trend_score")
    existing.momentum_score = factor_scores.get("momentum_score")
    existing.volatility_score = factor_scores.get("volatility_score")
    existing.liquidity_score = factor_scores.get("liquidity_score")
    existing.breadth_score = factor_scores.get("breadth_score")
    existing.event_score = factor_scores.get("event_score")
    existing.breakout_score = factor_scores.get("breakout_score")
    existing.pullback_score = factor_scores.get("pullback_score")
    existing.overheat_penalty = factor_scores.get("overheat_penalty")
    existing.data_credibility = credibility
    existing.scoring_asset_type = asset_type
    existing.scoring_config_id = config.id
    existing.scoring_preset_key = config.preset_key
    existing.scoring_preset_name = config.name
    existing.scoring_config_version = config.version
    existing.scoring_config_snapshot_json = config.config_json
    existing.dimension_scores_json = json.dumps(dim_scores, ensure_ascii=False)
    existing.factor_scores_json = json.dumps(factor_detail, ensure_ascii=False)

    db.flush()
    return existing


def _legacy_score_write(
    db: Session,
    symbol: Symbol,
    trade_date: date,
    factor_scores: dict[str, float | None],
    credibility: float,
) -> Score:
    """无配置兜底（沿用旧硬编码权重，保持向后兼容）。"""
    trend = factor_scores.get("trend_score", 50.0) or 50.0
    momentum = factor_scores.get("momentum_score", 45.0) or 45.0
    volatility = factor_scores.get("volatility_score", 50.0) or 50.0
    liquidity = factor_scores.get("liquidity_score", 45.0) or 45.0
    breadth = factor_scores.get("breadth_score", 50.0) or 50.0
    event = factor_scores.get("event_score", 50.0) or 50.0
    breakout = factor_scores.get("breakout_score", 50.0) or 50.0
    pullback = factor_scores.get("pullback_score", 40.0) or 40.0
    overheat = factor_scores.get("overheat_penalty", 0.0) or 0.0

    quality = _clamp(
        trend * 0.25 + momentum * 0.20 + volatility * 0.15
        + liquidity * 0.15 + breadth * 0.15 + event * 0.10
    )
    timing = _clamp(
        breakout * 0.30 + momentum * 0.20 + liquidity * 0.15
        + pullback * 0.15 + event * 0.10 + (100 - overheat) * 0.10
    )
    if overheat >= 20:
        stage, action = "overheat", "reduce"
    elif trend >= 60 and momentum >= 60 and breakout >= 65:
        stage, action = "accel", "hold"
    elif trend >= 55 and momentum > 50:
        stage, action = "start", "open"
    else:
        stage, action = "cooldown", "hold"
    priority = _clamp(timing * 0.4 + quality * 0.3 + liquidity * 0.2 + breadth * 0.1)

    calc_batch_id = f"manual-{trade_date.isoformat() if hasattr(trade_date, 'isoformat') else str(trade_date)}"
    existing = db.execute(
        select(Score).where(
            Score.symbol_id == symbol.id,
            Score.trade_date == trade_date,
            Score.calc_batch_id == calc_batch_id,
        )
    ).scalars().first()
    if existing is None:
        existing = Score(symbol_id=symbol.id, trade_date=trade_date, calc_batch_id=calc_batch_id)
        db.add(existing)

    existing.quality_score = quality
    existing.quality_grade = _grade(quality)
    existing.timing_score = timing
    existing.stage = stage
    existing.action = action
    existing.priority_score = priority
    existing.trend_score = trend
    existing.momentum_score = momentum
    existing.volatility_score = volatility
    existing.liquidity_score = liquidity
    existing.breadth_score = breadth
    existing.event_score = event
    existing.breakout_score = breakout
    existing.pullback_score = pullback
    existing.overheat_penalty = overheat
    existing.data_credibility = credibility
    db.flush()
    return existing
