"""数据源补齐路线（WPD-07）。

为 7 类数据源（估值/财报/资金流/龙虎榜/热度/尾盘/宏观）定义补齐策略、
限流预算、失败重试和可达覆盖说明，确保：
- 0 覆盖因子不通过填 0 获得假样本
- 估值无历史 point-in-time 时不做伪历史 IC
- 资金流表为空时不运行 main_inflow_5d_ratio
- 龙虎榜只在事件样本内评估
- 热度不承诺历史连续性
- 尾盘代理本期维持 blocked
- 宏观作为 regime 条件，不按个股横截面覆盖评估

对齐 docs/专业因子库开发计划.md §2A.5 数据补齐边界。

设计约束：
- 只读模块，不修改 schema、FACTOR_DEFINITIONS、_SOURCE_MAPPINGS 等
- DuckDB 查询均包裹 try/except，缺表/缺库时返回空状态而非抛错
- 复用 readiness._READINESS_SOURCE_TABLES 的源表日期列映射并补充 raw_macro
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from app.services.factors.readiness import _READINESS_SOURCE_TABLES


# ── 枚举 ──────────────────────────────────────────────────

class DataSourceLayer(str, Enum):
    """数据源层级（对齐 readiness 的 A/B/C 分层并扩展 D 宏观层）。"""

    A_CONTINUOUS = "A_continuous"   # 日线技术类
    B_EVENT = "B_event"             # 事件/状态类
    C_BLOCKED = "C_blocked"         # 数据阻断类
    D_REGIME = "D_regime"           # 宏观 regime 条件类


class CompletionStrategy(str, Enum):
    """数据源补齐策略。"""

    INCREMENTAL_FROM_DATE = "incremental_from_date"       # 估值：从补齐日期增量积累
    HIGH_LIQUIDITY_FIRST = "high_liquidity_first"         # 财报：先覆盖高流动性池
    VALIDATE_AND_CONTINUITY = "validate_and_continuity"   # 资金流：先验证再补60日连续
    EVENT_SAMPLE_ONLY = "event_sample_only"               # 龙虎榜：事件日样本
    SNAPSHOT_ONLY = "snapshot_only"                       # 热度：快照不承诺连续
    MAINTAIN_BLOCKED = "maintain_blocked"                 # 尾盘：本期维持阻断
    REGIME_CONDITION = "regime_condition"                 # 宏观：regime 条件


# ── 常量 ──────────────────────────────────────────────────

VALUATION_COMPLETION_START_DATE = date(2026, 8, 1)

# 因子代码 -> 数据源 key
FACTOR_TO_SOURCE_MAP: dict[str, str] = {
    "ep_ttm": "valuation",
    "negative_pb": "valuation",
    "roe_yoy_growth": "financial",
    "main_inflow_5d_ratio": "capital_flow",
    "lhb_institution_net_ratio": "lhb",
    "turnover_z20": "daily_bars",  # A 层，已可用
    "hot_rank_attention": "hot_rank",
    "tail_accumulation_proxy": "tail_proxy",
}

# 已可用、不纳入补齐路线的数据源（无对应 policy，不阻断）
_ALREADY_AVAILABLE_SOURCES = frozenset({"daily_bars"})

# 源表 -> 日期列（复用 readiness._READINESS_SOURCE_TABLES 并补充 raw_macro）
_ROADMAP_SOURCE_TABLES: dict[str, str] = {
    **_READINESS_SOURCE_TABLES,
    "raw_macro": "period",
}

# 源表 -> 实体列（用于 distinct_symbols 统计；macro 用 indicator_key）
_SOURCE_SYMBOL_COLUMNS: dict[str, str] = {
    "raw_daily_bars": "symbol",
    "raw_valuation_snapshots": "symbol",
    "raw_financial_reports": "symbol",
    "raw_fund_flows": "symbol",
    "raw_sentiment": "symbol",
    "raw_tail_proxy": "symbol",
    "raw_macro": "indicator_key",
}


# ── 数据类 ────────────────────────────────────────────────

@dataclass(frozen=True)
class RateLimitBudget:
    """数据采集的限流与重试预算。"""

    calls_per_minute: int
    calls_per_hour: int
    batch_size: int
    delay_between_calls_seconds: float
    offpeak_window_only: bool
    retry_max_attempts: int
    retry_backoff_base_seconds: float


@dataclass(frozen=True)
class ReachableCoverage:
    """数据源可达覆盖说明。"""

    description: str
    expected_universe_coverage: float        # 0.0-1.0，当前可达到的覆盖率
    expected_date_range: str                 # 如 "from 2026-08-01 forward"
    requires_point_in_time: bool
    fake_history_forbidden: bool             # True 表示禁止构造伪历史 IC


@dataclass(frozen=True)
class DataSourcePolicy:
    """单个数据源的补齐策略与边界。"""

    source_key: str                          # 如 "valuation"
    source_name: str                         # 中文名称
    duckdb_table: str                        # 如 "raw_valuation_snapshots"
    factor_codes: list[str]                  # 依赖此源的因子代码
    layer: DataSourceLayer
    completion_strategy: CompletionStrategy
    completion_start_date: date | None       # 估值：补齐起始日
    allowed_uses: list[str]                  # 允许的用途
    forbidden_uses: list[str]                # 禁止的用途
    rate_limit_budget: RateLimitBudget
    reachable_coverage: ReachableCoverage
    validation_checks: list[str]             # 需要验证的检查项
    boundary_notes: str                      # 边界说明

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


@dataclass(frozen=True)
class DataSourceState:
    """数据源在 DuckDB 中的当前状态快照。"""

    source_key: str
    duckdb_table: str
    row_count: int
    latest_date: str | None
    date_column: str
    is_empty: bool
    continuity_days: int | None              # 连续交易日数（资金流需 60 日）
    distinct_symbols: int | None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


@dataclass(frozen=True)
class DataSourceRoadmapReport:
    """全部 7 类数据源的补齐路线报告。"""

    generated_at: str
    valuation_completion_start_date: date
    policies: list[DataSourcePolicy]
    source_states: dict[str, DataSourceState]
    factor_to_source_map: dict[str, str]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))

    def get_policy(self, source_key: str) -> DataSourcePolicy | None:
        for policy in self.policies:
            if policy.source_key == source_key:
                return policy
        return None


# ── 策略定义 ──────────────────────────────────────────────

DATA_SOURCE_POLICIES: dict[str, DataSourcePolicy] = {
    "valuation": DataSourcePolicy(
        source_key="valuation",
        source_name="估值",
        duckdb_table="raw_valuation_snapshots",
        factor_codes=["ep_ttm", "negative_pb"],
        layer=DataSourceLayer.C_BLOCKED,
        completion_strategy=CompletionStrategy.INCREMENTAL_FROM_DATE,
        completion_start_date=VALUATION_COMPLETION_START_DATE,
        allowed_uses=["从补齐日期开始的增量评估", "未来 point-in-time 积累"],
        forbidden_uses=["伪历史 IC", "补齐日期前的回测"],
        rate_limit_budget=RateLimitBudget(
            calls_per_minute=60,
            calls_per_hour=600,
            batch_size=50,
            delay_between_calls_seconds=1.0,
            offpeak_window_only=True,
            retry_max_attempts=3,
            retry_backoff_base_seconds=1.0,
        ),
        reachable_coverage=ReachableCoverage(
            description="从 2026-08-01 起增量积累，不补历史",
            expected_universe_coverage=0.0,
            expected_date_range="from 2026-08-01 forward",
            requires_point_in_time=True,
            fake_history_forbidden=True,
        ),
        validation_checks=[
            "补齐日期后的数据连续性",
            "PE/PB正值过滤",
            "市值字段完整性",
        ],
        boundary_notes="估值数据从补齐日期开始增量积累，无历史 point-in-time 时不做伪历史 IC",
    ),
    "financial": DataSourcePolicy(
        source_key="financial",
        source_name="财报",
        duckdb_table="raw_financial_reports",
        factor_codes=["roe_yoy_growth"],
        layer=DataSourceLayer.C_BLOCKED,
        completion_strategy=CompletionStrategy.HIGH_LIQUIDITY_FIRST,
        completion_start_date=None,
        allowed_uses=["按 announcement_date 评估", "高流动性训练池优先"],
        forbidden_uses=["使用未公告的财报数据", "全市场一次性覆盖"],
        rate_limit_budget=RateLimitBudget(
            calls_per_minute=30,
            calls_per_hour=300,
            batch_size=100,
            delay_between_calls_seconds=2.0,
            offpeak_window_only=True,
            retry_max_attempts=3,
            retry_backoff_base_seconds=1.5,
        ),
        reachable_coverage=ReachableCoverage(
            description="先覆盖 300-500 高流动性标的，再扩全市场",
            expected_universe_coverage=0.1,
            expected_date_range="high liquidity pool first, then full market",
            requires_point_in_time=True,
            fake_history_forbidden=True,
        ),
        validation_checks=[
            "announcement_date 不可缺失",
            "report_period 与公告日一致性",
            "roe_ttm 字段非空率",
        ],
        boundary_notes="财报按 announcement_date 使用，先覆盖 300~500 高流动性训练池",
    ),
    "capital_flow": DataSourcePolicy(
        source_key="capital_flow",
        source_name="主力资金",
        duckdb_table="raw_fund_flows",
        factor_codes=["main_inflow_5d_ratio"],
        layer=DataSourceLayer.C_BLOCKED,
        completion_strategy=CompletionStrategy.VALIDATE_AND_CONTINUITY,
        completion_start_date=None,
        allowed_uses=["验证接口可用性后增量补数", "60日连续性达标后评估"],
        forbidden_uses=[
            "表为空时运行 main_inflow_5d_ratio",
            "未验证复权口径直接计算",
        ],
        rate_limit_budget=RateLimitBudget(
            calls_per_minute=120,
            calls_per_hour=1200,
            batch_size=200,
            delay_between_calls_seconds=0.5,
            offpeak_window_only=True,
            retry_max_attempts=4,
            retry_backoff_base_seconds=1.0,
        ),
        reachable_coverage=ReachableCoverage(
            description="验证接口/复权/限流后补 60 日连续数据",
            expected_universe_coverage=0.0,
            expected_date_range="after validation, 60-day continuous",
            requires_point_in_time=False,
            fake_history_forbidden=True,
        ),
        validation_checks=[
            "接口可用性验证",
            "复权口径一致性",
            "60日连续交易日数",
            "main_net_inflow 字段非空率",
        ],
        boundary_notes=(
            "资金流表为空时不运行 main_inflow_5d_ratio，"
            "先验证接口/复权/限流/60 日连续性"
        ),
    ),
    "lhb": DataSourcePolicy(
        source_key="lhb",
        source_name="龙虎榜",
        duckdb_table="raw_sentiment",
        factor_codes=["lhb_institution_net_ratio"],
        layer=DataSourceLayer.B_EVENT,
        completion_strategy=CompletionStrategy.EVENT_SAMPLE_ONLY,
        completion_start_date=None,
        allowed_uses=["事件日样本内评估", "机构席位净买额比率"],
        forbidden_uses=["非事件日填0", "全市场连续覆盖率评估"],
        rate_limit_budget=RateLimitBudget(
            calls_per_minute=30,
            calls_per_hour=300,
            batch_size=50,
            delay_between_calls_seconds=2.0,
            offpeak_window_only=True,
            retry_max_attempts=3,
            retry_backoff_base_seconds=1.5,
        ),
        reachable_coverage=ReachableCoverage(
            description="仅事件日样本，不承诺全市场连续覆盖",
            expected_universe_coverage=0.0,
            expected_date_range="event days only",
            requires_point_in_time=False,
            fake_history_forbidden=False,
        ),
        validation_checks=[
            "has_lhb 事件标记准确性",
            "lhb_institution_net 字段完整性",
            "事件日与非事件日区分",
        ],
        boundary_notes="龙虎榜只在事件样本内评估，不把非事件日填 0",
    ),
    "hot_rank": DataSourcePolicy(
        source_key="hot_rank",
        source_name="热度",
        duckdb_table="raw_sentiment",
        factor_codes=["hot_rank_attention"],
        layer=DataSourceLayer.B_EVENT,
        completion_strategy=CompletionStrategy.SNAPSHOT_ONLY,
        completion_start_date=None,
        allowed_uses=["当前快照解释", "候选池增强", "人气度横向比较"],
        forbidden_uses=["历史连续性承诺", "时间序列 IC 评估", "回测"],
        rate_limit_budget=RateLimitBudget(
            calls_per_minute=60,
            calls_per_hour=600,
            batch_size=100,
            delay_between_calls_seconds=1.0,
            offpeak_window_only=False,
            retry_max_attempts=2,
            retry_backoff_base_seconds=1.0,
        ),
        reachable_coverage=ReachableCoverage(
            description="仅当前快照，不补历史",
            expected_universe_coverage=0.0,
            expected_date_range="current snapshot only",
            requires_point_in_time=False,
            fake_history_forbidden=True,
        ),
        validation_checks=[
            "hot_rank_pct 字段完整性",
            "快照时间戳",
            "top-100 标的范围",
        ],
        boundary_notes="热度快照适合解释/候选增强，不承诺历史连续性",
    ),
    "tail_proxy": DataSourcePolicy(
        source_key="tail_proxy",
        source_name="尾盘代理",
        duckdb_table="raw_tail_proxy",
        factor_codes=["tail_accumulation_proxy"],
        layer=DataSourceLayer.C_BLOCKED,
        completion_strategy=CompletionStrategy.MAINTAIN_BLOCKED,
        completion_start_date=None,
        allowed_uses=["保存定义", "显示 readiness 阻断"],
        forbidden_uses=[
            "testing",
            "Shadow",
            "Active",
            "Ridge",
            "引入大规模分钟库",
        ],
        rate_limit_budget=RateLimitBudget(
            calls_per_minute=0,
            calls_per_hour=0,
            batch_size=0,
            delay_between_calls_seconds=0.0,
            offpeak_window_only=True,
            retry_max_attempts=0,
            retry_backoff_base_seconds=0.0,
        ),
        reachable_coverage=ReachableCoverage(
            description="本期维持 blocked，不引入分钟库",
            expected_universe_coverage=0.0,
            expected_date_range="blocked this cycle",
            requires_point_in_time=False,
            fake_history_forbidden=True,
        ),
        validation_checks=["本期不验证"],
        boundary_notes="尾盘代理维持 blocked，本期不引入大规模分钟库",
    ),
    "macro": DataSourcePolicy(
        source_key="macro",
        source_name="宏观",
        duckdb_table="raw_macro",
        factor_codes=[],
        layer=DataSourceLayer.D_REGIME,
        completion_strategy=CompletionStrategy.REGIME_CONDITION,
        completion_start_date=None,
        allowed_uses=["regime 条件分类", "市场状态判断", "宏观因子解释"],
        forbidden_uses=["按股票横截面覆盖率评估", "个股连续因子门禁"],
        rate_limit_budget=RateLimitBudget(
            calls_per_minute=10,
            calls_per_hour=100,
            batch_size=20,
            delay_between_calls_seconds=3.0,
            offpeak_window_only=True,
            retry_max_attempts=3,
            retry_backoff_base_seconds=2.0,
        ),
        reachable_coverage=ReachableCoverage(
            description="作为 regime 条件，不按个股覆盖评估",
            expected_universe_coverage=1.0,
            expected_date_range="regime condition, not cross-sectional",
            requires_point_in_time=False,
            fake_history_forbidden=False,
        ),
        validation_checks=[
            "indicator_key 完整性",
            "period 连续性",
            "value 非空率",
        ],
        boundary_notes="宏观作为 regime 条件使用，不按股票横截面覆盖率评估",
    ),
}


# ── 工具函数 ──────────────────────────────────────────────

def _json_safe(obj: Any) -> Any:
    """递归将 date/datetime 转为 ISO 字符串，确保 JSON 可序列化。"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(item) for item in obj]
    return obj


def _query_table_aggregates(
    conn, table: str, date_col: str
) -> tuple[int, str | None, int, int]:
    """查询单表的行数、最近日期、distinct 实体数、distinct 日期数。

    返回 (row_count, latest_date, distinct_symbols, continuity_days)。
    任何异常（缺表、缺列）返回全零，由调用方决定如何处理。
    """
    symbol_col = _SOURCE_SYMBOL_COLUMNS.get(table, "symbol")
    try:
        row = conn.execute(
            f"SELECT COUNT(*), MAX({date_col}), "
            f"COUNT(DISTINCT {symbol_col}), COUNT(DISTINCT {date_col}) "
            f"FROM {table}"
        ).fetchone()
    except Exception:
        return (0, None, 0, 0)
    row_count = int(row[0] or 0)
    latest = row[1]
    latest_date = str(latest) if latest is not None else None
    distinct_symbols = int(row[2] or 0)
    continuity_days = int(row[3] or 0)
    return (row_count, latest_date, distinct_symbols, continuity_days)


def _build_state(
    source_key: str,
    table: str,
    date_col: str,
    *,
    row_count: int,
    latest_date: str | None,
    distinct_symbols: int,
    continuity_days: int,
) -> DataSourceState:
    """构造 DataSourceState 的统一工厂。"""
    return DataSourceState(
        source_key=source_key,
        duckdb_table=table,
        row_count=row_count,
        latest_date=latest_date,
        date_column=date_col,
        is_empty=row_count == 0,
        continuity_days=continuity_days,
        distinct_symbols=distinct_symbols,
    )


def _empty_state(source_key: str, table: str, date_col: str) -> DataSourceState:
    """构造空表状态。"""
    return _build_state(
        source_key,
        table,
        date_col,
        row_count=0,
        latest_date=None,
        distinct_symbols=0,
        continuity_days=0,
    )


# ── 核心函数 ──────────────────────────────────────────────

def _query_source_table_stats(warehouse_conn) -> dict[str, DataSourceState]:
    """查询全部源表统计，返回 source_key -> DataSourceState。

    复用 readiness._READINESS_SOURCE_TABLES 的表名映射并补充 raw_macro。
    每个物理表只查询一次，再按 source_key 组装状态
    （lhb 与 hot_rank 共享 raw_sentiment）。
    """
    # table -> (row_count, latest_date, distinct_symbols, continuity_days)
    table_cache: dict[str, tuple[int, str | None, int, int]] = {}
    for table, date_col in _ROADMAP_SOURCE_TABLES.items():
        if table in table_cache:
            continue
        table_cache[table] = _query_table_aggregates(warehouse_conn, table, date_col)

    result: dict[str, DataSourceState] = {}
    for source_key, policy in DATA_SOURCE_POLICIES.items():
        table = policy.duckdb_table
        date_col = _ROADMAP_SOURCE_TABLES.get(table, "trade_date")
        row_count, latest_date, distinct_symbols, continuity_days = table_cache.get(
            table, (0, None, 0, 0)
        )
        result[source_key] = _build_state(
            source_key,
            table,
            date_col,
            row_count=row_count,
            latest_date=latest_date,
            distinct_symbols=distinct_symbols,
            continuity_days=continuity_days,
        )
    return result


def get_source_state(warehouse, source_key: str) -> DataSourceState:
    """查询单个数据源的当前 DuckDB 状态。"""
    policy = DATA_SOURCE_POLICIES.get(source_key)
    if policy is None:
        return _empty_state(source_key, "", "")
    table = policy.duckdb_table
    date_col = _ROADMAP_SOURCE_TABLES.get(table, "trade_date")
    try:
        with warehouse.connection(read_only=True) as conn:
            row_count, latest_date, distinct_symbols, continuity_days = (
                _query_table_aggregates(conn, table, date_col)
            )
    except Exception:
        return _empty_state(source_key, table, date_col)
    return _build_state(
        source_key,
        table,
        date_col,
        row_count=row_count,
        latest_date=latest_date,
        distinct_symbols=distinct_symbols,
        continuity_days=continuity_days,
    )


def get_data_source_roadmap(
    db_session,
    *,
    warehouse=None,
) -> DataSourceRoadmapReport:
    """生成全部 7 类数据源的补齐路线报告。

    流程：
    1. 获取或创建 FactorWarehouse
    2. 查询各源表的当前状态（行数、最近日期、连续性、标的数）
    3. 组装 DataSourcePolicy + DataSourceState
    4. 汇总 summary（含各因子是否被 roadmap 策略阻断）
    """
    from app.services.factors.config import get_factor_system_config
    from app.services.factors.store import FactorWarehouse

    # 1. 获取或创建 warehouse
    if warehouse is None:
        config = get_factor_system_config(db_session)
        warehouse = FactorWarehouse(config.warehouse_path)

    wh_health = warehouse.health()
    warehouse_available = wh_health.available

    # 2. 源表统计
    source_states: dict[str, DataSourceState] = {}
    if warehouse_available:
        try:
            with warehouse.connection(read_only=True) as conn:
                source_states = _query_source_table_stats(conn)
        except Exception:
            source_states = {}

    # 仓库不可用时，为每个源生成空状态
    if not source_states:
        for source_key, policy in DATA_SOURCE_POLICIES.items():
            table = policy.duckdb_table
            date_col = _ROADMAP_SOURCE_TABLES.get(table, "trade_date")
            source_states[source_key] = _empty_state(source_key, table, date_col)

    policies = list(DATA_SOURCE_POLICIES.values())

    # 3. 各因子是否被 roadmap 策略阻断
    blocked_factors = 0
    unblocked_factors = 0
    for factor_code in FACTOR_TO_SOURCE_MAP:
        result = is_factor_blocked_by_roadmap(factor_code, warehouse=warehouse)
        if result["is_blocked"]:
            blocked_factors += 1
        else:
            unblocked_factors += 1

    empty_tables = sum(1 for state in source_states.values() if state.is_empty)

    # 4. 汇总
    layer_counts: dict[str, int] = {}
    strategy_counts: dict[str, int] = {}
    for policy in policies:
        layer_counts[policy.layer.value] = (
            layer_counts.get(policy.layer.value, 0) + 1
        )
        strategy_counts[policy.completion_strategy.value] = (
            strategy_counts.get(policy.completion_strategy.value, 0) + 1
        )

    summary: dict[str, Any] = {
        "total_sources": len(policies),
        "total_factors": len(FACTOR_TO_SOURCE_MAP),
        "empty_source_tables": empty_tables,
        "sources_with_data": len(source_states) - empty_tables,
        "roadmap_blocked_factors": blocked_factors,
        "roadmap_unblocked_factors": unblocked_factors,
        "warehouse_available": warehouse_available,
        "layers": layer_counts,
        "strategies": strategy_counts,
    }

    return DataSourceRoadmapReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        valuation_completion_start_date=VALUATION_COMPLETION_START_DATE,
        policies=policies,
        source_states=source_states,
        factor_to_source_map=dict(FACTOR_TO_SOURCE_MAP),
        summary=summary,
    )


def check_capital_flow_continuity(
    warehouse, *, min_days: int = 60
) -> dict[str, Any]:
    """检查资金流表的连续交易日数是否达到 min_days。

    表为空或 distinct trade_date 不足 min_days 时视为阻断。
    """
    try:
        with warehouse.connection(read_only=True) as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT trade_date), MAX(trade_date) "
                "FROM raw_fund_flows"
            ).fetchone()
    except Exception:
        return {
            "is_continuous": False,
            "continuous_days": 0,
            "required_days": min_days,
            "latest_date": None,
            "is_blocked": True,
            "reason": "warehouse_or_table_unavailable",
        }

    distinct_days = int(row[0] or 0)
    latest = row[1]
    latest_date = str(latest) if latest is not None else None

    if distinct_days == 0:
        return {
            "is_continuous": False,
            "continuous_days": 0,
            "required_days": min_days,
            "latest_date": latest_date,
            "is_blocked": True,
            "reason": "source_table_empty",
        }

    is_continuous = distinct_days >= min_days
    return {
        "is_continuous": is_continuous,
        "continuous_days": distinct_days,
        "required_days": min_days,
        "latest_date": latest_date,
        "is_blocked": not is_continuous,
        "reason": (
            "continuity_sufficient"
            if is_continuous
            else f"continuous_days_below_{min_days}"
        ),
    }


def check_valuation_point_in_time(
    warehouse, *, as_of_date: date
) -> dict[str, Any]:
    """检查估值数据在指定日期是否有 point-in-time 数据。

    - as_of_date 早于 VALUATION_COMPLETION_START_DATE 时禁止（伪历史）。
    - 达到补齐起始日后，检查是否已积累该日及以后的 PIT 数据。
    """
    available_from = VALUATION_COMPLETION_START_DATE.isoformat()
    requested = as_of_date.isoformat()
    is_before = as_of_date < VALUATION_COMPLETION_START_DATE

    if is_before:
        return {
            "has_pit_data": False,
            "available_from": available_from,
            "requested_date": requested,
            "is_before_completion_start": True,
            "fake_history_forbidden": True,
            "reason": "requested_date_before_completion_start_date",
        }

    try:
        with warehouse.connection(read_only=True) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM raw_valuation_snapshots "
                "WHERE trade_date >= ?",
                [as_of_date],
            ).fetchone()
            count = int(row[0] or 0)
    except Exception:
        return {
            "has_pit_data": False,
            "available_from": available_from,
            "requested_date": requested,
            "is_before_completion_start": False,
            "fake_history_forbidden": True,
            "reason": "warehouse_or_table_unavailable",
        }

    has_pit = count > 0
    return {
        "has_pit_data": has_pit,
        "available_from": available_from,
        "requested_date": requested,
        "is_before_completion_start": False,
        "fake_history_forbidden": True,
        "reason": "pit_data_available" if has_pit else "no_data_accumulated_yet",
    }


def get_lhb_event_sample(
    warehouse, *, start_date: date, end_date: date
) -> dict[str, Any]:
    """获取龙虎榜事件日样本统计。

    查询 raw_sentiment WHERE has_lhb = TRUE 在 [start_date, end_date] 范围内
    的 distinct trade_date 与 distinct symbol。
    """
    try:
        with warehouse.connection(read_only=True) as conn:
            day_rows = conn.execute(
                "SELECT DISTINCT trade_date FROM raw_sentiment "
                "WHERE has_lhb = TRUE AND trade_date >= ? AND trade_date <= ? "
                "ORDER BY trade_date",
                [start_date, end_date],
            ).fetchall()
            event_dates = [str(r[0]) for r in day_rows]
            sym_row = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM raw_sentiment "
                "WHERE has_lhb = TRUE AND trade_date >= ? AND trade_date <= ?",
                [start_date, end_date],
            ).fetchone()
            total_symbols = int(sym_row[0] or 0) if sym_row else 0
    except Exception:
        return {
            "event_days": 0,
            "total_symbols": 0,
            "event_dates": [],
            "non_event_policy": "missing",
            "reason": "warehouse_or_table_unavailable",
        }

    return {
        "event_days": len(event_dates),
        "total_symbols": total_symbols,
        "event_dates": event_dates[:100],
        "non_event_policy": "missing",
        "reason": (
            "event_sample_collected"
            if event_dates
            else "no_lhb_events_in_range"
        ),
    }


def is_factor_blocked_by_roadmap(
    factor_code: str, warehouse=None
) -> dict[str, Any]:
    """根据 roadmap 策略判断因子是否被阻断。

    阻断规则：
    - MAINTAIN_BLOCKED：始终阻断
    - INCREMENTAL_FROM_DATE：早于补齐起始日或尚未积累 PIT 数据时阻断
    - VALIDATE_AND_CONTINUITY：资金流连续性不足时阻断
      （warehouse 不可用时无法验证，按安全策略阻断）
    - HIGH_LIQUIDITY_FIRST / EVENT_SAMPLE_ONLY / SNAPSHOT_ONLY /
      REGIME_CONDITION：roadmap 策略本身不阻断（数据层阻断由 readiness 负责）
    - daily_bars（已可用源）：不阻断
    """
    source_key = FACTOR_TO_SOURCE_MAP.get(factor_code)

    if source_key is None:
        return {
            "factor_code": factor_code,
            "source_key": "",
            "is_blocked": True,
            "blocking_reasons": ["factor_source_mapping_missing"],
            "recommended_action": "blocked",
            "completion_strategy": "",
        }

    # 已可用的数据源（如 daily_bars）不纳入补齐路线，不阻断
    if source_key in _ALREADY_AVAILABLE_SOURCES:
        return {
            "factor_code": factor_code,
            "source_key": source_key,
            "is_blocked": False,
            "blocking_reasons": [],
            "recommended_action": "evaluate",
            "completion_strategy": "",
        }

    policy = DATA_SOURCE_POLICIES.get(source_key)
    if policy is None:
        return {
            "factor_code": factor_code,
            "source_key": source_key,
            "is_blocked": True,
            "blocking_reasons": ["source_policy_missing"],
            "recommended_action": "blocked",
            "completion_strategy": "",
        }

    strategy = policy.completion_strategy
    blocking_reasons: list[str] = []
    recommended_action = "evaluate"

    if strategy == CompletionStrategy.MAINTAIN_BLOCKED:
        blocking_reasons.append("maintain_blocked_strategy")
        recommended_action = "blocked"

    elif strategy == CompletionStrategy.INCREMENTAL_FROM_DATE:
        # 估值：早于补齐起始日直接阻断；达到起始日后检查是否已积累 PIT 数据
        start = policy.completion_start_date
        today = date.today()
        if start is not None and today < start:
            blocking_reasons.append("before_completion_start_date")
            recommended_action = "await_data"
        elif warehouse is not None and start is not None:
            pit = check_valuation_point_in_time(warehouse, as_of_date=start)
            if not pit["has_pit_data"]:
                blocking_reasons.append(
                    "no_accumulated_data_since_completion_start"
                )
                recommended_action = "await_data"

    elif strategy == CompletionStrategy.VALIDATE_AND_CONTINUITY:
        # 资金流：连续性不足时阻断；warehouse 不可用时无法验证，安全阻断
        if warehouse is not None:
            cont = check_capital_flow_continuity(warehouse)
            if cont["is_blocked"]:
                blocking_reasons.append(cont["reason"])
                recommended_action = "await_data"
        else:
            blocking_reasons.append("continuity_not_verified")
            recommended_action = "await_data"

    # HIGH_LIQUIDITY_FIRST / EVENT_SAMPLE_ONLY / SNAPSHOT_ONLY /
    # REGIME_CONDITION：roadmap 策略不阻断，数据层门禁由 readiness 负责

    is_blocked = len(blocking_reasons) > 0
    return {
        "factor_code": factor_code,
        "source_key": source_key,
        "is_blocked": is_blocked,
        "blocking_reasons": blocking_reasons,
        "recommended_action": recommended_action,
        "completion_strategy": strategy.value,
    }


__all__ = [
    "VALUATION_COMPLETION_START_DATE",
    "FACTOR_TO_SOURCE_MAP",
    "DATA_SOURCE_POLICIES",
    "CompletionStrategy",
    "DataSourceLayer",
    "DataSourcePolicy",
    "DataSourceRoadmapReport",
    "DataSourceState",
    "RateLimitBudget",
    "ReachableCoverage",
    "check_capital_flow_continuity",
    "check_valuation_point_in_time",
    "get_data_source_roadmap",
    "get_lhb_event_sample",
    "get_source_state",
    "is_factor_blocked_by_roadmap",
]
