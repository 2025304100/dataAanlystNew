"""因子数据 readiness 评估（WPD-04）。

按因子类型返回 available / degraded / blocked 及结构化证据，避免 0 覆盖因子
被推入评估流水线后浪费 1900 万 factor_values 扫描。

层级分类（对齐计划 §9.1）：
- A_continuous：日线技术类（turnover_z20），唯一可立即评估的因子
- B_event：事件/状态类（lhb_institution_net_ratio、hot_rank_attention），
  按事件日样本评估
- C_blocked：数据阻断类（ep_ttm、negative_pb、roe_yoy_growth、
  main_inflow_5d_ratio、tail_accumulation_proxy），覆盖不足时直接阻断

设计约束：
- 只读模块，不修改 schema、FACTOR_DEFINITIONS、_SOURCE_MAPPINGS 等
- 复用 health.py 的 FactorCoverage 输出和 trade_calendar.py 的完整交易日证据
- 阻断原因使用稳定英文枚举，便于 i18n
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal

from app.services.factors.definitions import (
    FACTOR_BY_CODE,
    FACTOR_DEFINITIONS,
    _SOURCE_MAPPINGS,
)
from app.services.factors.trade_calendar import (
    EPOCH_DATE,
    CompleteTradeDayEvidence,
    latest_complete_trade_date,
)

ReadinessStatus = Literal["available", "degraded", "blocked"]
FactorLayer = Literal["A_continuous", "B_event", "C_blocked"]

# ── 因子层级映射 ──────────────────────────────────────────

FACTOR_LAYER_MAP: dict[str, FactorLayer] = {
    "turnover_z20": "A_continuous",
    "lhb_institution_net_ratio": "B_event",
    "hot_rank_attention": "B_event",
    "ep_ttm": "C_blocked",
    "negative_pb": "C_blocked",
    "roe_yoy_growth": "C_blocked",
    "main_inflow_5d_ratio": "C_blocked",
    "tail_accumulation_proxy": "C_blocked",
}

# ── 阻断原因枚举（稳定英文，便于 i18n）──────────────────

BLOCKING_REASONS = {
    "source_table_empty": "数据源表为空",
    "source_table_insufficient": "数据源表行数不足",
    "coverage_zero": "因子覆盖率为 0",
    "coverage_below_threshold": "因子覆盖率低于阈值",
    "no_complete_trade_day": "无完整交易日",
    "warehouse_unavailable": "因子仓库不可用",
    "factor_batch_missing": "因子批次缺失",
    "historical_depth_insufficient": "历史深度不足",
    "point_in_time_missing": "缺少 point-in-time 数据",
    "snapshot_only": "仅快照数据，无历史连续性",
}

# ── 推荐动作枚举 ──────────────────────────────────────────

RECOMMENDED_ACTIONS = {
    "evaluate": "可立即评估",
    "await_data": "等待数据补齐",
    "event_only": "仅按事件日样本评估",
    "snapshot_only": "仅做快照观察",
    "blocked": "数据阻断，不可评估",
}

# B 层事件因子的推荐动作映射
_EVENT_ACTION_MAP: dict[str, str] = {
    "lhb_institution_net_ratio": "event_only",
    "hot_rank_attention": "snapshot_only",
}

# readiness 关注的源表及其日期列
_READINESS_SOURCE_TABLES: dict[str, str] = {
    "raw_daily_bars": "trade_date",
    "raw_valuation_snapshots": "trade_date",
    "raw_financial_reports": "announcement_date",
    "raw_fund_flows": "trade_date",
    "raw_sentiment": "trade_date",
    "raw_tail_proxy": "trade_date",
}

# A 层覆盖率阈值
_A_MINIMUM_COVERAGE = 0.7
_A_HEALTHY_COVERAGE = 0.9
# C 层覆盖率阈值（任何缺失都阻断）
_C_BLOCKED_COVERAGE = 1.0


# ── 数据类 ────────────────────────────────────────────────

@dataclass(frozen=True)
class FactorReadiness:
    """单个因子的 readiness 评估结果。"""

    factor_code: str
    factor_name: str
    category: str
    layer: FactorLayer
    status: ReadinessStatus
    coverage: float
    latest_factor_date: str | None
    latest_source_date: str | None
    universe_symbols: int
    eligible_symbols: int
    evidence: dict[str, Any]
    blocking_reasons: list[str]
    recommended_action: str
    complete_trade_day_evidence: dict[str, Any] | None
    data_source_policy: dict[str, Any] | None  # WPD-07: 数据源补齐路线策略

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


@dataclass(frozen=True)
class FactorReadinessReport:
    """全部因子 readiness 报告。"""

    generated_at: str
    warehouse_available: bool
    warehouse_path: str
    complete_trade_day: date
    complete_trade_day_evidence: dict[str, Any]
    factors: list[FactorReadiness]
    summary: dict[str, int]
    source_table_stats: dict[str, dict]

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))

    def get_factor(self, code: str) -> FactorReadiness | None:
        for factor in self.factors:
            if factor.factor_code == code:
                return factor
        return None


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


def _classify_factor_layer(definition) -> FactorLayer:
    """根据因子定义返回 A/B/C 层级。"""
    return FACTOR_LAYER_MAP.get(definition.code, "C_blocked")


def _extract_source_tables(source_mapping: dict) -> list[str]:
    """从 _SOURCE_MAPPINGS 条目提取依赖的源表列表。"""
    tables: list[str] = []
    if "table" in source_mapping:
        tables.append(source_mapping["table"])
    if "tables" in source_mapping:
        tables.extend(source_mapping["tables"])
    seen: set[str] = set()
    result: list[str] = []
    for table in tables:
        if table not in seen:
            seen.add(table)
            result.append(table)
    return result


def _get_source_table_stats(warehouse_conn) -> dict[str, dict]:
    """查询各源表的行数和最近日期。"""
    result: dict[str, dict] = {}
    for table, date_col in _READINESS_SOURCE_TABLES.items():
        try:
            row = warehouse_conn.execute(
                f"SELECT COUNT(*), MAX({date_col}) FROM {table}"
            ).fetchone()
            count = int(row[0] or 0)
            latest = row[1]
            result[table] = {
                "rows": count,
                "latest_date": str(latest) if latest is not None else None,
                "date_column": date_col,
            }
        except Exception:
            result[table] = {
                "rows": 0,
                "latest_date": None,
                "date_column": date_col,
            }
    return result


def _compute_coverage_threshold(*, layer: FactorLayer) -> float:
    """按层级返回覆盖率阈值。

    - A_continuous: 0.7（最低）/0.9（健康）
    - B_event: 0.0（事件日覆盖率不适用）
    - C_blocked: 1.0（任何缺失都阻断）
    """
    if layer == "A_continuous":
        return _A_MINIMUM_COVERAGE
    if layer == "B_event":
        return 0.0
    return _C_BLOCKED_COVERAGE


def _evidence_to_dict(evidence: CompleteTradeDayEvidence) -> dict[str, Any]:
    """将 CompleteTradeDayEvidence 转为 JSON 安全的 dict。"""
    return _json_safe(asdict(evidence))


def _no_ctd_evidence() -> CompleteTradeDayEvidence:
    """构造无数据时的完整交易日证据。"""
    return CompleteTradeDayEvidence(
        selected_trade_date=EPOCH_DATE,
        observed_symbols=0,
        expected_symbols=0,
        completeness_ratio=0.0,
        fallback_reason="no_universe_data",
        median_baseline=0,
        evaluated_candidate_dates=[],
        candidate_ratios={},
    )


def _build_readiness(
    definition,
    *,
    layer: FactorLayer,
    status: ReadinessStatus,
    coverage: float,
    latest_factor_date: str | None,
    latest_source_date: str | None,
    universe_symbols: int,
    eligible_symbols: int,
    evidence: dict[str, Any],
    blocking_reasons: list[str],
    recommended_action: str,
    complete_trade_day_evidence: dict[str, Any] | None,
    data_source_policy: dict[str, Any] | None = None,
) -> FactorReadiness:
    """构造 FactorReadiness 的统一工厂。"""
    return FactorReadiness(
        factor_code=definition.code,
        factor_name=definition.name,
        category=definition.category,
        layer=layer,
        status=status,
        coverage=round(coverage, 6),
        latest_factor_date=latest_factor_date,
        latest_source_date=latest_source_date,
        universe_symbols=universe_symbols,
        eligible_symbols=eligible_symbols,
        evidence=evidence,
        blocking_reasons=blocking_reasons,
        recommended_action=recommended_action,
        complete_trade_day_evidence=complete_trade_day_evidence,
        data_source_policy=data_source_policy,
    )


# ── 核心评估函数 ──────────────────────────────────────────

def evaluate_factor_readiness(
    factor_code: str,
    *,
    db_session,
    warehouse,
    complete_trade_day_evidence,
    source_table_stats: dict[str, dict],
    factor_coverage: dict | None = None,
) -> FactorReadiness:
    """评估单个因子 readiness。

    算法：
    1. 从 FACTOR_BY_CODE 获取定义
    2. 从 _SOURCE_MAPPINGS 获取依赖表
    3. 检查源表行数和最近日期（用 source_table_stats）
    4. 检查 factor_values 覆盖率（用 factor_coverage 或查询 DuckDB）
    5. 按层级应用阈值
    6. 生成 evidence 和 recommended_action
    """
    definition = FACTOR_BY_CODE[factor_code]
    layer = _classify_factor_layer(definition)
    source_mapping = _SOURCE_MAPPINGS.get(factor_code, {})
    source_tables = _extract_source_tables(source_mapping)

    blocking_reasons: list[str] = []
    evidence: dict[str, Any] = {
        "layer": layer,
        "source_tables": source_tables,
        "source_mapping": source_mapping,
    }
    # WPD-07: 数据源补齐路线策略（在主逻辑结束后填充，提前初始化以便早期返回透传）
    data_source_policy_dict: dict[str, Any] | None = None

    # universe 来自完整交易日证据
    universe_symbols = (
        complete_trade_day_evidence.expected_symbols
        or complete_trade_day_evidence.observed_symbols
        or 0
    )

    # 因子覆盖率
    coverage = 0.0
    latest_factor_date: str | None = None
    eligible_symbols = 0
    if factor_coverage:
        coverage = float(factor_coverage.get("coverage", 0.0) or 0.0)
        latest_factor_date = factor_coverage.get("latest_trade_date")
        if latest_factor_date is not None:
            latest_factor_date = str(latest_factor_date)
        eligible_symbols = int(factor_coverage.get("eligible_symbols", 0) or 0)
        if factor_coverage.get("universe_symbols"):
            universe_symbols = int(factor_coverage["universe_symbols"])

    evidence["coverage"] = coverage
    evidence["eligible_symbols"] = eligible_symbols
    evidence["universe_symbols"] = universe_symbols

    # 源表统计与最近日期
    latest_source_date: str | None = None
    source_rows: dict[str, int] = {}
    for table in source_tables:
        stats = source_table_stats.get(table, {})
        rows = int(stats.get("rows", 0) or 0)
        latest = stats.get("latest_date")
        source_rows[table] = rows
        if latest and (
            latest_source_date is None or str(latest) > str(latest_source_date)
        ):
            latest_source_date = str(latest)
    evidence["source_table_rows"] = source_rows
    evidence["source_table_stats"] = {
        table: source_table_stats.get(table, {}) for table in source_tables
    }

    # ── 1. 仓库可用性 ──
    warehouse_available = True
    try:
        wh_health = warehouse.health()
        warehouse_available = wh_health.available
        evidence["warehouse_available"] = wh_health.available
        evidence["warehouse_path"] = wh_health.path
    except Exception as exc:
        warehouse_available = False
        evidence["warehouse_available"] = False
        evidence["warehouse_error"] = str(exc)

    if not warehouse_available:
        blocking_reasons.append("warehouse_unavailable")
        return _build_readiness(
            definition,
            layer=layer,
            status="blocked",
            coverage=coverage,
            latest_factor_date=latest_factor_date,
            latest_source_date=latest_source_date,
            universe_symbols=universe_symbols,
            eligible_symbols=eligible_symbols,
            evidence=evidence,
            blocking_reasons=blocking_reasons,
            recommended_action="blocked",
            complete_trade_day_evidence=None,
            data_source_policy=None,
        )

    # ── 2. 完整交易日 ──
    ctd_fallback = complete_trade_day_evidence.fallback_reason
    ctd_dict = _evidence_to_dict(complete_trade_day_evidence)
    evidence["complete_trade_day"] = ctd_dict
    if ctd_fallback == "no_universe_data":
        blocking_reasons.append("no_complete_trade_day")
        return _build_readiness(
            definition,
            layer=layer,
            status="blocked",
            coverage=coverage,
            latest_factor_date=latest_factor_date,
            latest_source_date=latest_source_date,
            universe_symbols=universe_symbols,
            eligible_symbols=eligible_symbols,
            evidence=evidence,
            blocking_reasons=blocking_reasons,
            recommended_action="blocked",
            complete_trade_day_evidence=None,
            data_source_policy=None,
        )

    # ── 3. 源表行数检查 ──
    for table in source_tables:
        rows = source_rows.get(table, 0)
        if rows == 0:
            if "source_table_empty" not in blocking_reasons:
                blocking_reasons.append("source_table_empty")
        elif layer != "B_event":
            # B_event 层：事件稀疏，不按 universe 阈值判定行数不足
            minimum = max(universe_symbols, 1)
            if rows < minimum:
                if "source_table_insufficient" not in blocking_reasons:
                    blocking_reasons.append("source_table_insufficient")

    # ── 4. 覆盖率检查 ──
    threshold = _compute_coverage_threshold(layer=layer)
    evidence["coverage_threshold"] = threshold
    if coverage == 0.0:
        if "coverage_zero" not in blocking_reasons:
            blocking_reasons.append("coverage_zero")
    elif layer == "C_blocked" and coverage < threshold:
        if "coverage_below_threshold" not in blocking_reasons:
            blocking_reasons.append("coverage_below_threshold")

    # ── 5. 按层级决定最终状态 ──
    ctd_evidence_dict: dict[str, Any] | None = None

    if layer == "A_continuous":
        if not blocking_reasons and coverage >= _A_HEALTHY_COVERAGE:
            status: ReadinessStatus = "available"
            action = "evaluate"
            ctd_evidence_dict = ctd_dict
        elif not blocking_reasons and coverage >= _A_MINIMUM_COVERAGE:
            status = "degraded"
            action = "evaluate"
        else:
            status = "blocked"
            action = "await_data"
            if not blocking_reasons:
                blocking_reasons.append("coverage_below_threshold")

    elif layer == "B_event":
        if blocking_reasons:
            status = "blocked"
            action = "await_data"
        elif eligible_symbols > 0:
            status = "degraded"
            action = _EVENT_ACTION_MAP.get(factor_code, "event_only")
        else:
            status = "blocked"
            action = "await_data"
            if "coverage_zero" not in blocking_reasons:
                blocking_reasons.append("coverage_zero")

    else:  # C_blocked
        if blocking_reasons:
            status = "blocked"
            action = "await_data"
        elif coverage >= _C_BLOCKED_COVERAGE:
            status = "available"
            action = "evaluate"
            ctd_evidence_dict = ctd_dict
        else:
            status = "blocked"
            action = "await_data"
            if "coverage_below_threshold" not in blocking_reasons:
                blocking_reasons.append("coverage_below_threshold")

    # WPD-07: 数据源补齐路线策略
    data_source_policy_dict: dict[str, Any] | None = None
    try:
        from app.services.factors.data_source_roadmap import (
            DATA_SOURCE_POLICIES,
            FACTOR_TO_SOURCE_MAP,
            is_factor_blocked_by_roadmap,
        )
        source_key = FACTOR_TO_SOURCE_MAP.get(factor_code)
        if source_key and source_key in DATA_SOURCE_POLICIES:
            policy = DATA_SOURCE_POLICIES[source_key]
            policy_dict = _json_safe({
                "source_key": policy.source_key,
                "source_name": policy.source_name,
                "layer": policy.layer.value,
                "completion_strategy": policy.completion_strategy.value,
                "completion_start_date": policy.completion_start_date.isoformat() if policy.completion_start_date else None,
                "allowed_uses": policy.allowed_uses,
                "forbidden_uses": policy.forbidden_uses,
                "reachable_coverage": _json_safe(policy.reachable_coverage),
                "boundary_notes": policy.boundary_notes,
            })
            # Check if roadmap adds additional blocking
            roadmap_block = is_factor_blocked_by_roadmap(factor_code)
            policy_dict["roadmap_block_check"] = roadmap_block
            evidence["roadmap_block_check"] = roadmap_block
            # If roadmap blocks and current status is not already blocked, add reason
            if roadmap_block.get("is_blocked") and status != "blocked":
                for reason in roadmap_block.get("blocking_reasons", []):
                    if reason not in blocking_reasons:
                        blocking_reasons.append(reason)
            data_source_policy_dict = policy_dict
    except Exception:
        pass

    return _build_readiness(
        definition,
        layer=layer,
        status=status,
        coverage=coverage,
        latest_factor_date=latest_factor_date,
        latest_source_date=latest_source_date,
        universe_symbols=universe_symbols,
        eligible_symbols=eligible_symbols,
        evidence=evidence,
        blocking_reasons=blocking_reasons,
        recommended_action=action,
        complete_trade_day_evidence=ctd_evidence_dict,
        data_source_policy=data_source_policy_dict,
    )


def get_factor_readiness_report(
    db_session,
    *,
    warehouse=None,
    adjust: str = "qfq",
) -> FactorReadinessReport:
    """生成全部 8 因子的 readiness 报告。

    流程：
    1. 获取或创建 FactorWarehouse
    2. 调用 WPD-02 latest_complete_trade_date 获取完整交易日证据
    3. 调用 _get_source_table_stats 获取源表统计
    4. 调用 get_factor_health 获取现有 FactorCoverage
    5. 对每个 FACTOR_DEFINITIONS 调用 evaluate_factor_readiness
    6. 汇总 summary 和 source_table_stats
    """
    from app.services.factors.config import get_factor_system_config
    from app.services.factors.health import get_factor_health
    from app.services.factors.store import FactorWarehouse

    # 1. 获取或创建 warehouse
    if warehouse is None:
        config = get_factor_system_config(db_session)
        warehouse = FactorWarehouse(config.warehouse_path)

    wh_health = warehouse.health()
    warehouse_available = wh_health.available
    warehouse_path = wh_health.path

    # 2. 完整交易日证据（WPD-02）
    try:
        ctd_evidence = latest_complete_trade_date(db_session)
    except Exception:
        ctd_evidence = _no_ctd_evidence()

    # 3. 源表统计
    source_table_stats: dict[str, dict] = {}
    if warehouse_available:
        try:
            with warehouse.connection(read_only=True) as conn:
                source_table_stats = _get_source_table_stats(conn)
        except Exception:
            source_table_stats = {}

    # 4. 因子覆盖率（来自 health.py）
    factor_coverage_map: dict[str, dict] = {}
    if warehouse_available:
        try:
            health_report = get_factor_health(warehouse, adjust=adjust)
            for fc in health_report.factors:
                factor_coverage_map[fc.factor_code] = asdict(fc)
        except Exception:
            pass

    # 5. 逐因子评估
    factors: list[FactorReadiness] = []
    for definition in FACTOR_DEFINITIONS:
        fc = factor_coverage_map.get(definition.code)
        readiness = evaluate_factor_readiness(
            definition.code,
            db_session=db_session,
            warehouse=warehouse,
            complete_trade_day_evidence=ctd_evidence,
            source_table_stats=source_table_stats,
            factor_coverage=fc,
        )
        factors.append(readiness)

    # 6. 汇总
    summary = {"available": 0, "degraded": 0, "blocked": 0}
    for factor in factors:
        summary[factor.status] += 1

    return FactorReadinessReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        warehouse_available=warehouse_available,
        warehouse_path=warehouse_path,
        complete_trade_day=ctd_evidence.selected_trade_date,
        complete_trade_day_evidence=_evidence_to_dict(ctd_evidence),
        factors=factors,
        summary=summary,
        source_table_stats=source_table_stats,
    )


__all__ = [
    "BLOCKING_REASONS",
    "FACTOR_LAYER_MAP",
    "FactorLayer",
    "FactorReadiness",
    "FactorReadinessReport",
    "RECOMMENDED_ACTIONS",
    "ReadinessStatus",
    "evaluate_factor_readiness",
    "get_factor_readiness_report",
]
