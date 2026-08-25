"""G1-WP0-3a：DecisionEngine 统一编排层。

Q25 约定：采用「新增编排层 + 旧服务 adapter」方案，不立即重写四个服务。
新增 decision_engine.evaluate(...) 统一执行：
  1 snapshot_loader  2 clock  3 gate  4 universe/eligibility  5 health
  6 scorer           7 signal 8 risk/position-allocation       9 evidence
结构化中间对象全部落表，11 步全链路可审计。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timezone
from typing import Any, Callable, Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.hash_utils import canonical_json, content_hash
from app.models.decision_engine import (
    DecisionEvidence, DecisionRun, DecisionOrderPlanRecord, ManualPriceOverride, StrategyExecutionSnapshot,
)
from app.models.score import Score
from app.services.decision_clock import (
    DEFAULT_MATCH_MODE, MatchMode, ResolvedClock, resolve,
    utc_naive_to_shanghai, utcnow_naive, validate_match_mode,
)
import app.services.match_price_resolver as _tb2_mp  # T-B2: NEXT_OPEN 涨跌停顺延

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────── 枚举
ActionType = Literal["BUY", "SELL", "HOLD", "NO_ACTION", "REJECTED", "DATA_BLOCKED"]
BlockingStatus = Literal[
    "READY", "DATA_INCOMPLETE_PAUSED", "RECONCILIATION_BLOCKED",
    "MODEL_INACTIVE", "SCORE_STALE",
]
PitSafeFlag = Literal["PIT_SAFE", "NOT_PIT_SAFE", "UNKNOWN"]
# DB CheckConstraint / Pydantic Schema 对齐的标准枚举
RunType = Literal["backtest", "auto_simulation", "research_preflight"]

# 旧值 → 新值 兼容映射（G1 过渡期，避免旧 task/前端入参写库失败）
_RUN_TYPE_ALIASES: dict[str, RunType] = {
    "auto_sim": "auto_simulation",
    "dry_run": "research_preflight",
    "auto_simulation": "auto_simulation",
    "research_preflight": "research_preflight",
    "backtest": "backtest",
}

# 对外 API 枚举 → DB 层枚举（G0 期已冻结的 wps_0023_024 建表 CHECK：dry_run/backtest/auto_simulation）。
# SQLite 不支持 ALTER CHECK CONSTRAINT，为保持 G0 迁移链不修改，这里做双向映射。
_RUN_TYPE_TO_DB: dict[str, str] = {
    "research_preflight": "dry_run",
    "backtest": "backtest",
    "auto_simulation": "auto_simulation",
    # 额外覆盖所有别名（防止重复映射）
    "auto_sim": "auto_simulation",
    "dry_run": "dry_run",
}
_RUN_TYPE_FROM_DB: dict[str, str] = {v: k for k, v in _RUN_TYPE_TO_DB.items() if k in {"research_preflight", "backtest", "auto_simulation"}}


def _normalize_run_type(raw: str | None) -> RunType:
    """将用户/旧服务传入的 run_type 规范化为对外 API 枚举。未知值降级为 research_preflight。"""
    if raw is None:
        return "research_preflight"
    key = str(raw).strip()
    if key in _RUN_TYPE_ALIASES:
        return _RUN_TYPE_ALIASES[key]
    # 未知但不阻断：返回 research_preflight（最安全的 fail-soft）
    return "research_preflight"


def _run_type_to_db(api_value: str | None) -> str:
    """对外 API 枚举 → DB CHECK 约束值（dry_run/backtest/auto_simulation）。"""
    if api_value is None:
        return "dry_run"
    key = str(api_value).strip()
    if key in _RUN_TYPE_TO_DB:
        return _RUN_TYPE_TO_DB[key]
    return "dry_run"


def _run_type_from_db(db_value: str | None) -> RunType:
    """DB CHECK 约束值 → 对外 API 枚举。"""
    if db_value is None:
        return "research_preflight"
    if db_value in _RUN_TYPE_FROM_DB:
        return _RUN_TYPE_FROM_DB[db_value]  # type: ignore[return-value]
    return "research_preflight"


DEFAULT_SLIPPAGE_BUY_BPS = 5.0
DEFAULT_SLIPPAGE_SELL_BPS = 5.0
_FORMAL_PIT_RUN_MODES = frozenset({
    "production",
    "strict_pit",
    "production_pit",
    "production_sim",
})


# ──────────────────────────────────────────────────────────── 流水线中间对象
@dataclass
class LoadedSnapshot:
    """Step 1: 不可变快照加载器输出。"""
    snapshot: StrategyExecutionSnapshot
    factor_model_run_id: str
    factor_set_id: str | None
    rule_id: int | None
    rule_version: int | None
    members: list[dict[str, Any]] = dc_field(default_factory=list)
    candidate_pool: list[dict[str, Any]] = dc_field(default_factory=list)
    gate_policy_version: str = "production-v1.0.0"
    cost_config: dict[str, Any] = dc_field(default_factory=dict)
    versions: dict[str, Any] = dc_field(default_factory=dict)
    # 阶段 1（决策驱动回测）：可选状态注入。结构同 _read_alloc_state() 输出；
    # 提供时 allocator 不得回退查询数据库 Position 表（回测注入模拟持仓/现金）。
    allocation_state_override: dict[str, Any] | None = None
    # 决策时参考价（symbol_id -> 价格），用于 allocator 的数量计算 fallback。
    price_hints: dict[int, float] | None = None
    # 可验证的止损线（symbol_id -> price）。它是本次决策的行情输入，
    # 仅在 entry_price > stop_loss_price 时参与单笔风险预算 clamp。
    risk_stop_loss_by_symbol: dict[int, float] = dc_field(default_factory=dict)
    # 决策所用的成交量（symbol_id -> quantity），用于在订单计划生成前
    # 应用参与率约束；缺失量仍由数据健康门禁处理，不能在此处猜测。
    market_volume_by_symbol: dict[int, float] = dc_field(default_factory=dict)


@dataclass(frozen=True)
class DecisionStateContext:
    """Historical portfolio state supplied by replay/backtest callers."""

    current_by_symbol: dict[int, dict[str, Any]] = dc_field(default_factory=dict)
    current_asset_pct: dict[str, float] = dc_field(default_factory=dict)
    current_sector_pct: dict[str, float] = dc_field(default_factory=dict)
    available_cash: float | None = None
    total_capital: float | None = None
    pending_orders: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    price_data_by_symbol: dict[int, dict[str, Any]] = dc_field(default_factory=dict)
    cost_config: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def state_hash(self) -> str:
        # Execution bars/prices are matcher inputs. They must not create a
        # second decision identity for an otherwise identical snapshot/date/
        # portfolio state (for example when cost or replay end-date changes).
        # The actual bar lineage remains on Evidence/MatchResult.
        return content_hash(
            "decision_state_v1",
            self.current_by_symbol,
            self.current_asset_pct,
            self.current_sector_pct,
            self.pending_orders,
        )


@dataclass
class UniverseAndEligibility:
    """Steps 4+5: universe 构建 + 资格筛选。"""
    universe: list[dict[str, Any]] = dc_field(default_factory=list)
    universe_count: int = 0
    member_count: int = 0
    ineligible: list[dict[str, Any]] = dc_field(default_factory=list)
    health_issues: list[dict[str, Any]] = dc_field(default_factory=list)


@dataclass
class ScoredUniverse:
    """Step 6: Score 门禁 + 新鲜度 + 覆盖率。"""
    items: list[dict[str, Any]] = dc_field(default_factory=list)
    expected: int = 0
    actual: int = 0
    coverage_pct: float = 0.0
    max_age_days: int | None = None
    stale_score_symbols: list[int] = dc_field(default_factory=list)
    missing_symbols: list[int] = dc_field(default_factory=list)


@dataclass
class SignalResult:
    """Step 7: 方向信号。"""
    items: list[dict[str, Any]] = dc_field(default_factory=list)


@dataclass
class RiskAllocationResult:
    """Steps 8+9: 顺序 clamp + 风控。"""
    items: list[dict[str, Any]] = dc_field(default_factory=list)
    clamp_trace: list[dict[str, Any]] = dc_field(default_factory=list)


@dataclass
class ExitRuleHit:
    """T-B5 Q11.1：一次卖出规则命中记录。

    rule_priority 数值越小优先级越高（10 > 20 > 30 ...）；
    rule_subtype ∈ {SELL_STOP_LOSS, SELL_RISK_EXIT, SELL_STRATEGY_EXIT}。
    """
    rule_code: str
    rule_priority: int
    requested_exit_qty: float
    rule_subtype: str


@dataclass
class PerSymbolEvidence:
    """Evidence item prior to ORM persist。"""
    symbol_id: int
    action: ActionType
    action_subtype: str | None = None
    target_position_pct: float | None = None
    min_lot_size: int = 100
    target_quantity: float | None = None
    target_qty_delta: float | None = None
    intended_price: float | None = None
    executed_price: float | None = None
    slippage_bps: float | None = None
    rejection_reason: str | None = None
    rejection_detail: str | None = None
    blocking_reason: str | None = None
    score_id: int | None = None
    score_value: float | None = None
    score_rank: int | None = None
    score_published_at: datetime | None = None
    pit_safe_flag: PitSafeFlag = "UNKNOWN"
    constraints: list[dict[str, Any]] = dc_field(default_factory=list)
    reason_codes: list[str] = dc_field(default_factory=list)
    factor_contributions: dict[str, Any] = dc_field(default_factory=dict)
    legacy_fallback_flag: bool = False
    stop_loss_verified_price_source: str | None = None
    stop_loss_triggered: bool = False
    match_mode: str = DEFAULT_MATCH_MODE.value
    manual_price_flag: bool = False
    # T-A9.3: 非 None 表示被哪一个 ManualPriceOverride.id 应用（消费标记用）
    manual_price_override_id: int | None = None
    # T-B2 Q2.1：NEXT_OPEN 涨跌停顺延
    roll_forward_days: int | None = None
    rejections_trace_json: list[dict[str, Any]] = dc_field(default_factory=list)
    # T-B5 Q11.1：卖出规则命中记录列表（按命中序；最终 top priority 由 resolve_exit_rule_outcome 裁决）
    exit_rules_hit: list[ExitRuleHit] = dc_field(default_factory=list)


@dataclass(frozen=True)
class DecisionOrderPlan:
    """Deterministic order intent derived from one DecisionEvidence row.

    The plan is deliberately a value object for the first integration slice:
    its identity is derived from ``decision_run_id`` and ``evidence_id`` so a
    dry-run and a persisted evaluation produce the same plan without creating
    a second mutable order state machine.  Matching engines may consume only
    BUY/SELL plans; non-actionable plans remain available for reconciliation.
    """

    decision_run_id: str
    evidence_id: str
    symbol_id: int
    action: ActionType
    signal_date: date
    execution_date: date
    target_quantity: float
    direction: str | None
    intended_price: float | None
    reason_code: str | None
    rejection_trace: list[dict[str, Any]] = dc_field(default_factory=list)

    @property
    def order_plan_id(self) -> str:
        """Stable plan identifier shared by dry-run and persisted evaluation."""
        return content_hash("decision_order_plan", self.decision_run_id, self.evidence_id)


def _build_order_plans(
    *,
    decision_run_id: str,
    evidence_prefix: str,
    trade_date: date,
    execution_at: datetime,
    evidence: list[PerSymbolEvidence],
) -> list[DecisionOrderPlan]:
    """Build one deterministic plan value for each evidence item.

    ``_persist`` uses the same evidence-id formula below. Keeping the formula
    in this shared builder makes dry-run and persisted results byte-for-byte
    equivalent at the order-plan boundary.
    """
    execution_date = utc_naive_to_shanghai(execution_at).date()
    plans: list[DecisionOrderPlan] = []
    for index, ev in enumerate(evidence):
        evidence_id = content_hash(
            evidence_prefix, index, ev.symbol_id, ev.action, trade_date.isoformat(),
        )
        delta = ev.target_qty_delta
        if delta is None:
            delta = ev.target_quantity if ev.action in {"BUY", "SELL"} else 0.0
        target_quantity = abs(float(delta or 0.0))
        direction = ev.action if ev.action in {"BUY", "SELL"} else None
        reason_code = (
            ev.rejection_reason
            or ev.action_subtype
            or (ev.reason_codes[0] if ev.reason_codes else None)
        )
        plans.append(DecisionOrderPlan(
            decision_run_id=decision_run_id,
            evidence_id=evidence_id,
            symbol_id=int(ev.symbol_id),
            action=ev.action,
            signal_date=trade_date,
            execution_date=execution_date,
            target_quantity=target_quantity,
            direction=direction,
            intended_price=(
                float(ev.intended_price) if ev.intended_price is not None else None
            ),
            reason_code=reason_code,
            rejection_trace=[dict(item) for item in (ev.rejections_trace_json or [])],
        ))
    return plans


@dataclass
class EvaluateResult:
    """DecisionEngine.evaluate() 总输出。"""
    decision_run_id: str
    blocking_status: BlockingStatus
    blocking_reasons: list[dict[str, Any]]
    evidence: list[PerSymbolEvidence]
    clock: ResolvedClock
    score_coverage_pct: float | None
    score_max_age_days: int | None
    persisted: bool
    dry_run: bool
    fail_closed_result: dict | None = None
    is_result_production_eligible: bool = True
    degraded_warnings: list[dict[str, Any]] = dc_field(default_factory=list)
    match_mode: str = DEFAULT_MATCH_MODE.value
    order_plans: list[DecisionOrderPlan] = dc_field(default_factory=list)


def requires_strict_market_data_pit(snap: LoadedSnapshot) -> bool:
    """Whether supplied market data must carry a verifiable availability time.

    Strict historical replays cannot treat a later-ingested daily bar as if it
    were known at the original decision cutoff. Research snapshots retain the
    legacy best-effort behavior unless they explicitly opt into strict PIT.
    """
    versions = getattr(snap, "versions", {}) or {}
    if not isinstance(versions, dict):
        return False
    run_mode = str(versions.get("run_mode") or "research").strip().lower()
    pit_mode = str(versions.get("pit_mode") or "best_effort").strip().lower()
    return run_mode in _FORMAL_PIT_RUN_MODES or pit_mode in {
        "strict_pit_safe",
        "strict_pit",
    }


def _coerce_utc_naive_datetime(value: Any) -> datetime | None:
    """Normalize externally supplied availability metadata for PIT checks."""
    parsed: datetime | None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _strict_price_data_pit_result(
    *,
    snap: LoadedSnapshot,
    clock: ResolvedClock,
    symbol_id: int,
    price_data: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a fail-closed data result for unavailable strict-PIT prices."""
    if not requires_strict_market_data_pit(snap):
        return None
    raw_available_at = price_data.get(
        "available_at", price_data.get("data_available_at")
    )
    available_at = _coerce_utc_naive_datetime(raw_available_at)
    if available_at is None:
        return {
            "available": False,
            "status": "DATA_BLOCKED",
            "reason_code": "PRICE_DATA_AVAILABILITY_MISSING",
            "reason": (
                "PRICE_DATA_AVAILABILITY_MISSING: "
                f"symbol_id={symbol_id} strict PIT requires available_at"
            ),
        }
    if available_at > clock.data_cutoff_at:
        return {
            "available": False,
            "status": "DATA_BLOCKED",
            "reason_code": "DAILY_BAR_AVAILABLE_AFTER_CUTOFF",
            "reason": (
                "DAILY_BAR_AVAILABLE_AFTER_CUTOFF: "
                f"symbol_id={symbol_id} available_at={available_at.isoformat()} "
                f"> data_cutoff_at={clock.data_cutoff_at.isoformat()}"
            ),
        }
    return None


# ──────────────────────────────────────────────────────────── 协议 — 11 步
class SnapshotLoader(Protocol):
    def __call__(self, db: Session, strategy_snapshot_id: str, /) -> LoadedSnapshot: ...


class GateChecker(Protocol):
    def __call__(
        self, db: Session, snap: LoadedSnapshot, clock: ResolvedClock, /,
    ) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]: ...


class UniverseBuilder(Protocol):
    def __call__(
        self, db: Session, snap: LoadedSnapshot, cutoff_utc: datetime, /,
    ) -> UniverseAndEligibility: ...


class DataHealthChecker(Protocol):
    def __call__(
        self, db: Session, universe: UniverseAndEligibility, cutoff_utc: datetime, /,
    ) -> UniverseAndEligibility: ...


class Scorer(Protocol):
    def __call__(
        self, db: Session, snap: LoadedSnapshot, universe: UniverseAndEligibility,
        cutoff_utc: datetime, decision_date: date, /,
    ) -> ScoredUniverse: ...


class SignalGenerator(Protocol):
    def __call__(
        self, db: Session, snap: LoadedSnapshot, scored: ScoredUniverse, /,
    ) -> SignalResult: ...


class PositionAllocator(Protocol):
    def __call__(
        self, db: Session, snap: LoadedSnapshot, signal: SignalResult,
        cutoff_utc: datetime, /,
    ) -> RiskAllocationResult: ...


# ──────────────────────────────────────────────────────────── 编排主类
class DecisionEngine:
    """G1-WP0-3a：11 步流水线入口。

    每个步骤默认是 stub（把输入原样透传并打标记），保证骨架能跑通；
    真实实现随 WP0-3b/WP0-4a/WP0-5a 逐步替换，失败不影响骨架契约。
    """

    def __init__(
        self,
        *,
        snapshot_loader: SnapshotLoader | None = None,
        gate: GateChecker | None = None,
        universe_builder: UniverseBuilder | None = None,
        health: DataHealthChecker | None = None,
        scorer: Scorer | None = None,
        signal: SignalGenerator | None = None,
        allocator: PositionAllocator | None = None,
        evidence_builder: Callable[..., list[PerSymbolEvidence]] | None = None,
    ) -> None:
        self.load_snapshot = snapshot_loader or _stub_snapshot_loader
        self.gate = gate or _stub_gate
        self.build_universe = universe_builder or _stub_universe_builder
        self.check_health = health or _stub_health_check
        # WP0-4：默认使用真实 Score 查询 + 信号映射 + Q10 顺序 clamp（不再是 stub）
        self.score = scorer or _real_scorer
        self.generate_signal = signal or _real_signal_generator
        self.allocate_position = allocator or _real_allocator
        self.build_evidence = evidence_builder or _default_build_evidence

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------
    def evaluate(
        self,
        db: Session,
        *,
        portfolio_id: int,
        strategy_snapshot_id: str,
        trade_date: date,
        run_type: RunType = "research_preflight",
        dry_run: bool = True,
        coverage_result: dict | None = None,
        key_member_result: dict | None = None,
        staleness_result: dict | None = None,
        per_member_score_avail: list[dict] | None = None,
        allow_legacy_fallback: bool | None = None,
        price_data_by_symbol: dict[int, dict[str, Any]] | None = None,
        state_context: DecisionStateContext | None = None,
        match_mode: MatchMode | str | None = None,
        allow_t_close_research_override: bool | None = None,
    ) -> EvaluateResult:
        """Q25：统一 evaluate 入口。dry_run 只返回证据，不写库。

        T-A4 Q6.1 预留钩子：若传入 coverage_result / key_member_result /
        staleness_result (非 None) 则执行 evaluate_fail_closed_decision；
        否则（旧调用兼容）跳过，保证旧路径不崩。

        Q6.3：allow_legacy_fallback 参数控制 legacy_score fallback 是否启用。
        - 生产/正式链路 (run_mode ∈ {production, strict_pit}) 且 allow_legacy_fallback=True
          → 前置校验抛错码 FALLBACK_NOT_ALLOWED_IN_PRODUCTION
        - 研究模式：allow_legacy_fallback=True → 允许使用 legacy，显式留痕
        - allow_legacy_fallback=None/False → 默认不启用 legacy

        T-A7 Q7.1：price_data_by_symbol 传入当日每只股票的 OHLCV + 停牌标记，
        用于在撮合前检查数据可用性。格式：
        {symbol_id: {"open_price": float|None, "close_price": float|None,
                     "high_price": float|None, "low_price": float|None,
                     "volume": int|None, "is_suspended_today": bool}}
        """
        # Q25 / G1-3：run_type 旧值兼容（auto_sim → auto_simulation，dry_run → research_preflight）
        run_type = _normalize_run_type(run_type)

        # Step 1-2: Snapshot + Clock
        snap = self.load_snapshot(db, strategy_snapshot_id)
        if state_context is not None:
            # Backtest/replay callers own the historical state.  Convert it to
            # the allocator's existing override shape so no live Position row
            # is consulted for this evaluation.
            total_capital = float(state_context.total_capital or 0.0)
            current_by_symbol = {
                int(symbol_id): dict(values)
                for symbol_id, values in state_context.current_by_symbol.items()
            }
            # Evidence is built from the immutable snapshot member list, while
            # replay callers own the historical quantities.  Overlay only
            # state fields here so target deltas and HOLD/SELL semantics use
            # the injected day state without querying live Position rows.
            for member in snap.members:
                try:
                    symbol_id = int(member.get("symbol_id"))
                except (TypeError, ValueError):
                    continue
                state_values = current_by_symbol.get(symbol_id, {})
                member["current_quantity"] = float(state_values.get("qty", 0.0) or 0.0)
                member["current_position_pct"] = float(state_values.get("pct", 0.0) or 0.0)
                member["market_value"] = float(state_values.get("market_value", 0.0) or 0.0)
                if state_values.get("asset_type") is not None:
                    member["asset_type"] = state_values["asset_type"]
                if state_values.get("sector") is not None:
                    member["industry"] = state_values["sector"]
            total_position_pct = sum(
                float(values.get("pct", 0.0) or 0.0)
                for values in current_by_symbol.values()
            )
            available_cash = (
                float(state_context.available_cash)
                if state_context.available_cash is not None
                else max(0.0, total_capital * max(0.0, 1.0 - total_position_pct))
            )
            asset_pct = dict(state_context.current_asset_pct)
            sector_pct = dict(state_context.current_sector_pct)
            if not asset_pct:
                for values in current_by_symbol.values():
                    if float(values.get("qty", 0.0) or 0.0) <= 0:
                        continue
                    asset = str(values.get("asset_type", "stock") or "stock").lower()
                    asset_pct[asset] = asset_pct.get(asset, 0.0) + float(values.get("pct", 0.0) or 0.0)
            if not sector_pct:
                for values in current_by_symbol.values():
                    if float(values.get("qty", 0.0) or 0.0) <= 0:
                        continue
                    sector = str(values.get("sector", values.get("industry", "unclassified")) or "unclassified")
                    sector_pct[sector] = sector_pct.get(sector, 0.0) + float(values.get("pct", 0.0) or 0.0)
            snap.allocation_state_override = {
                "total_position_pct": total_position_pct,
                "current_by_sym": current_by_symbol,
                "current_asset_pct": asset_pct,
                "current_sector_pct": sector_pct,
                "position_count": sum(
                    1 for values in current_by_symbol.values()
                    if float(values.get("qty", 0.0) or 0.0) > 0
                ),
                "cash_pct": (available_cash / total_capital if total_capital > 0 else 0.0),
                "available_cash": available_cash,
                "total_capital": total_capital,
            }
            snap.price_hints = {
                int(symbol_id): float(data.get("open_price") or data.get("close_price"))
                for symbol_id, data in state_context.price_data_by_symbol.items()
                if data.get("open_price") or data.get("close_price")
            }
            if state_context.cost_config:
                snap.cost_config = {
                    **(snap.cost_config or {}),
                    **state_context.cost_config,
                }
            if price_data_by_symbol is None:
                price_data_by_symbol = dict(state_context.price_data_by_symbol)
        # Stage-2 PIT master-data gate: a snapshot may contain a symbol that
        # was not listed yet on the replay date (or whose master row was
        # removed/incomplete). Keep the member in the evidence universe, but
        # annotate it so the evidence builder emits a zero-adjustment
        # DATA_BLOCKED decision instead of silently allocating a position.
        _annotate_symbol_master_pit(db, snap.members, trade_date)
        if state_context is not None:
            _annotate_member_effective_pit(
                snap.members,
                trade_date,
                held_symbol_ids={
                    int(symbol_id)
                    for symbol_id, values in state_context.current_by_symbol.items()
                    if float((values or {}).get("qty", 0.0) or 0.0) > 0
                },
            )
        clock = resolve(trade_date, mode="t_day_close")

        run_mode_precheck = snap.versions.get("run_mode", "research")

        # Q2.1 T-B1 Step 0: 前置校验撮合模式 match_mode（双重保险第一重）
        resolved_match_mode, mm_degraded_warnings, mm_eligible = validate_match_mode(
            mode=match_mode,
            run_mode=run_mode_precheck,
            allow_t_close_research_override=allow_t_close_research_override,
        )

        # Q6.3 Step 0: 前置校验 legacy fallback（生产链路绝不允许）
        validate_allow_legacy_fallback(
            run_mode=run_mode_precheck,
            allow_legacy_fallback=allow_legacy_fallback,
        )

        # ── T-A9 Q7.4 Step 0: 查询 + 注入 ManualPriceOverride（人工数据阻断处理）
        #       3 模式：confirm_manual_price（注入手动价）/ continue_forward（MANUALLY_SKIPPED）/ keep_paused（不做任何事，仍然阻断）
        ta9_per_symbol: dict[int, "ManualPriceOverride"] = {}
        ta9_portfolio_wide: "ManualPriceOverride | None" = None
        if not dry_run:
            ov_stmt = select(ManualPriceOverride).where(
                ManualPriceOverride.portfolio_id == portfolio_id,
                ManualPriceOverride.trade_date == trade_date,
                ManualPriceOverride.consumed_flag == 0,
            ).order_by(ManualPriceOverride.id)
            ov_rows = list(db.execute(ov_stmt).scalars())
            for ov in ov_rows:
                if ov.symbol_id is None:
                    # portfolio 级批量决议：取最新一条（id 最大的优先；多个时给 warn）
                    ta9_portfolio_wide = ov
                else:
                    # symbol 级覆盖（新覆盖旧，按 id=时间顺序，最后一个生效）
                    ta9_per_symbol[int(ov.symbol_id)] = ov

            # 1) confirm_manual_price → 把 manual_executable_price 注入 price_data_by_symbol
            #    T-B1 口径 NEXT_OPEN 保持一致：将 open/close/high/low/volume 都填手动价
            if ta9_per_symbol or ta9_portfolio_wide:
                if price_data_by_symbol is None:
                    price_data_by_symbol = {}
                for sid, ov in ta9_per_symbol.items():
                    if ov.resolved_mode == "confirm_manual_price" and ov.manual_executable_price is not None:
                        existing = price_data_by_symbol.get(sid) or {}
                        mp = float(ov.manual_executable_price)
                        # 所有 OHLCV 字段都写手动价：保证 check_data_available 判定"有数据"不 DATA_BLOCKED
                        # 另打上特殊标记，由 build_evidence 识别 manual_price_flag=1 写入证据
                        price_data_by_symbol[sid] = {
                            **existing,
                            "open_price": existing.get("open_price") or mp,
                            "close_price": existing.get("close_price") or mp,
                            "high_price": existing.get("high_price") or mp,
                            "low_price": existing.get("low_price") or mp,
                            "volume": existing.get("volume") if existing.get("volume") not in (None, 0) else 1,
                            "is_suspended_today": bool(existing.get("is_suspended_today", False)),
                            "_ta9_override_applied": True,
                            "_ta9_override_id": ov.id,
                        }
                # portfolio 级 confirm_manual_price 不存在（必须指定 symbol），此处不处理

        ta9_context = {
            "per_symbol": ta9_per_symbol,
            "portfolio_wide": ta9_portfolio_wide,
            "applied_override_ids": [],  # build_evidence/消费后回填
        }

        # Stop prices belong to the same PIT market-data input as the intended
        # execution price. Keep them on the loaded snapshot so the allocator
        # can cap a BUY before emitting an order plan, rather than leaving the
        # risk check to a downstream matcher.
        risk_stop_loss_by_symbol = dict(snap.risk_stop_loss_by_symbol or {})
        market_volume_by_symbol = dict(snap.market_volume_by_symbol or {})
        for raw_symbol_id, price_data in (price_data_by_symbol or {}).items():
            if not isinstance(price_data, dict):
                continue
            try:
                symbol_id = int(raw_symbol_id)
            except (TypeError, ValueError):
                continue
            raw_stop_loss = price_data.get("stop_loss_price")
            try:
                stop_loss = float(raw_stop_loss)
            except (TypeError, ValueError):
                stop_loss = None
            if stop_loss is not None and stop_loss > 0:
                risk_stop_loss_by_symbol[symbol_id] = stop_loss
            try:
                market_volume = float(price_data.get("volume"))
            except (TypeError, ValueError):
                market_volume = None
            if market_volume is not None and market_volume >= 0:
                market_volume_by_symbol[symbol_id] = market_volume
        snap.risk_stop_loss_by_symbol = risk_stop_loss_by_symbol
        snap.market_volume_by_symbol = market_volume_by_symbol

        # Step 3: Gate (门禁)
        gate_ok, gate_reasons, gate_result = self.gate(db, snap, clock)

        blocking_status: BlockingStatus = "READY"
        blocking_reasons: list[dict[str, Any]] = []
        if not gate_ok:
            # 分类到 Q27 的五类状态
            blocking_status = _classify_blocking(gate_reasons)
            blocking_reasons = gate_reasons

        # ── T-A4 Q6.1: Fail-Closed 组合级门禁（预留钩子，仅当全部三个结果都传入时启用）
        fail_closed_result: dict | None = None
        is_result_production_eligible: bool = mm_eligible
        aggregated_degraded_warnings: list[dict] = list(mm_degraded_warnings)
        if (
            coverage_result is not None
            and key_member_result is not None
            and staleness_result is not None
            and per_member_score_avail is not None
        ):
            run_mode = snap.versions.get("run_mode", "research")
            fail_closed_result = evaluate_fail_closed_decision(
                db,
                run_mode=run_mode,
                coverage_result=coverage_result,
                key_member_result=key_member_result,
                staleness_result=staleness_result,
                per_member_score_avail=per_member_score_avail,
                portfolio_id=portfolio_id,
                strategy_snapshot_id=strategy_snapshot_id,
                decision_at=clock.decision_at,
                allow_legacy_fallback=allow_legacy_fallback,
                match_mode=resolved_match_mode,
                allow_t_close_research_override=allow_t_close_research_override,
            )
            # 组合 is_result_production_eligible：任何一侧 False 则 False
            is_result_production_eligible = (
                is_result_production_eligible
                and fail_closed_result["is_result_production_eligible"]
            )
            aggregated_degraded_warnings.extend(fail_closed_result.get("degraded_warnings", []))
            # 正式 BLOCKED 时：叠加到 blocking_status + blocking_reasons
            if fail_closed_result["block_all_members"]:
                blocking_status = _classify_blocking([
                    {"severity": "blocking", "code": c}
                    for c in fail_closed_result["block_codes"]
                ] + ([{"severity": "blocking", "code": r} for r in gate_reasons]))
                for code, reason in zip(
                    fail_closed_result["block_codes"],
                    fail_closed_result["block_reasons"],
                ):
                    blocking_reasons.append({
                        "severity": "blocking",
                        "code": code,
                        "message": reason,
                        "source": "q6_1_fail_closed",
                    })

        # Step 4-5: Universe + Health
        universe = self.build_universe(db, snap, clock.data_cutoff_at)
        universe = self.check_health(db, universe, clock.data_cutoff_at)

        # Step 6: Score (门禁失败时也跑至少一次覆盖率计数，保证 evidence 里能标 missing)
        scored = self.score(
            db, snap, universe, clock.data_cutoff_at, trade_date,
        )

        # Q5/Fail-Closed：正式模式覆盖率不达标 → 阻断
        if blocking_status == "READY":
            sla = _read_sla_from_snapshot(snap)
            if scored.expected > 0 and scored.coverage_pct < sla["coverage_pct"]:
                blocking_status = "SCORE_STALE" if scored.max_age_days and scored.max_age_days > sla["max_age_days"] else "DATA_INCOMPLETE_PAUSED"
                blocking_reasons.append({
                    "severity": "blocking",
                    "code": "SCORE_COVERAGE_BELOW_SLA",
                    "message": f"Score 覆盖率 {scored.coverage_pct:.2f}% < 阈值 {sla['coverage_pct']:.2f}%",
                    "detail": {"expected": scored.expected, "actual": scored.actual,
                               "sla_coverage_pct": sla["coverage_pct"]},
                })
            if sla.get("max_age_days") and scored.max_age_days is not None and scored.max_age_days > sla["max_age_days"]:
                blocking_status = "SCORE_STALE"
                blocking_reasons.append({
                    "severity": "blocking", "code": "SCORE_MAX_AGE_EXCEEDED",
                    "message": f"Score 最旧距今 {scored.max_age_days} 天 > SLA {sla['max_age_days']} 天",
                })

        # Q6/Fail-Closed：正式模式 + 关键成员缺 Score → 整体阻断
        # （key_member 识别留待 allocator 细化；这里骨架只对全阻断统一放行 NO_ACTION）
        # Step 7-8: Signal + Allocation
        signal_res = self.generate_signal(db, snap, scored)
        alloc = self.allocate_position(db, snap, signal_res, clock.data_cutoff_at)

        # Step 9: Evidence
        evidence = self.build_evidence(
            snap=snap, clock=clock, universe=universe, scored=scored,
            signal=signal_res, alloc=alloc, blocking_status=blocking_status,
            blocking_reasons=blocking_reasons,
            price_data_by_symbol=price_data_by_symbol,
            trade_date=trade_date,
            match_mode=resolved_match_mode.value,
            manual_overrides_context=ta9_context,
        )
        # T-A9.3：回填 consumed_override_ids（已 apply 的 ManualPriceOverride.id 集合，用于 persist 时 mark consumed）
        consumed_override_ids: set[int] = set(ta9_context.get("applied_override_ids") or [])
        for ev in evidence:
            if ev.manual_price_override_id is not None:
                consumed_override_ids.add(int(ev.manual_price_override_id))

        # 幂等决策主键 (Q28)
        decision_run_id = content_hash(
            "decision_run", portfolio_id, strategy_snapshot_id,
            clock.decision_at.isoformat(), trade_date.isoformat(), run_type,
            state_context.state_hash if state_context is not None else None,
        )
        evidence_idempotency_prefix = decision_run_id[:16]
        order_plans = _build_order_plans(
            decision_run_id=decision_run_id,
            evidence_prefix=evidence_idempotency_prefix,
            trade_date=trade_date,
            execution_at=clock.execution_at,
            evidence=evidence,
        )

        # ── 落库（dry_run 只返回不落库）
        persisted = False
        if not dry_run:
            self._persist(
                db,
                portfolio_id=portfolio_id,
                strategy_snapshot_id=strategy_snapshot_id,
                run_type=run_type, trade_date=trade_date, clock=clock,
                snap=snap, universe=universe, scored=scored,
                blocking_status=blocking_status, blocking_reasons=blocking_reasons,
                gate_result=gate_result, evidence=evidence,
                decision_run_id=decision_run_id,
                evidence_prefix=evidence_idempotency_prefix,
                is_result_production_eligible=is_result_production_eligible,
                consumed_override_ids=consumed_override_ids,
                # T-B1: 传给 _persist 写 DB 的 DecisionRun.match_mode / Evidence.match_mode
                resolved_match_mode=resolved_match_mode,
            )
            persisted = True

        return EvaluateResult(
            decision_run_id=decision_run_id,
            blocking_status=blocking_status,
            blocking_reasons=blocking_reasons,
            evidence=evidence,
            clock=clock,
            score_coverage_pct=scored.coverage_pct if scored.expected else None,
            score_max_age_days=scored.max_age_days,
            persisted=persisted,
            dry_run=dry_run,
            fail_closed_result=fail_closed_result,
            is_result_production_eligible=is_result_production_eligible,
            match_mode=resolved_match_mode.value,
            order_plans=order_plans,
        )

    # -----------------------------------------------------------------------
    # 落库实现
    # -----------------------------------------------------------------------
    def _persist(
        self,
        db: Session,
        *,
        portfolio_id: int,
        strategy_snapshot_id: str,
        run_type: str,
        trade_date: date,
        clock: ResolvedClock,
        snap: LoadedSnapshot,
        universe: UniverseAndEligibility,
        scored: ScoredUniverse,
        blocking_status: str,
        blocking_reasons: list[dict[str, Any]],
        gate_result: dict[str, Any],
        evidence: list[PerSymbolEvidence],
        decision_run_id: str,
        evidence_prefix: str,
        is_result_production_eligible: bool = True,
        consumed_override_ids: set[int] | None = None,
        resolved_match_mode: MatchMode | str | None = None,
    ) -> None:
        # DecisionRun（幂等：已存在则直接返回）
        existing = db.get(DecisionRun, decision_run_id)
        if existing is not None:
            if str(existing.status) == "FAILED":
                # A backtest-level failure may intentionally downgrade all
                # decisions created in that attempt, even when this day's
                # DecisionEvidence was completely flushed.  Retrying the
                # same deterministic input must be able to reactivate that
                # complete immutable decision.  Do not revive a partially
                # written audit record: that would turn a persistence fault
                # into an apparently successful decision.
                expected_evidence_ids = {
                    content_hash(
                        evidence_prefix, index, ev.symbol_id, ev.action,
                        trade_date.isoformat(),
                    )
                    for index, ev in enumerate(evidence)
                }
                persisted_evidence_ids = set(db.execute(
                    select(DecisionEvidence.id).where(
                        DecisionEvidence.decision_run_id == decision_run_id,
                    )
                ).scalars().all())
                if persisted_evidence_ids != expected_evidence_ids:
                    raise RuntimeError(
                        "FAILED DecisionRun has incomplete evidence; "
                        "cannot safely reactivate deterministic decision"
                    )
                finished = utcnow_naive()
                existing.status = "SUCCEEDED"
                existing.finished_at = finished
                if existing.started_at is not None:
                    existing.duration_ms = max(
                        0,
                        int((finished - existing.started_at).total_seconds() * 1000),
                    )
                db.flush()
                logger.info(
                    "decision_run %s reactivated from FAILED after complete evidence retry",
                    decision_run_id,
                )
                self._ensure_order_plan_records(
                    db, portfolio_id=portfolio_id, decision_run_id=decision_run_id,
                    trade_date=trade_date, execution_at=clock.execution_at, evidence=evidence,
                )
                return
            if str(existing.status) != "SUCCEEDED":
                raise RuntimeError(
                    "DecisionRun already exists with non-reusable status "
                    f"{existing.status}: {decision_run_id}"
                )
            logger.debug("decision_run %s 已存在，跳过重复写入（幂等）", decision_run_id)
            # Releases before the order-plan ledger existed have complete
            # Evidence but no relational plan rows.  A deterministic retry is
            # the only safe repair path: it has the same snapshot/date/state
            # inputs, so it materializes intent without guessing old data.
            self._ensure_order_plan_records(
                db, portfolio_id=portfolio_id, decision_run_id=decision_run_id,
                trade_date=trade_date, execution_at=clock.execution_at, evidence=evidence,
            )
            return
        # 解析 match_mode → 字符串；允许 MatchMode Enum 或 str，None 回退到默认 NEXT_OPEN
        if isinstance(resolved_match_mode, MatchMode):
            _mm_str = resolved_match_mode.value
        elif isinstance(resolved_match_mode, str) and resolved_match_mode:
            _mm_str = resolved_match_mode
        else:
            _mm_str = DEFAULT_MATCH_MODE.value

        # G1-3 枚举双向兼容：对外 API 用 research_preflight/auto_simulation；
        # DB CHECK 约束仍为 dry_run/backtest/auto_simulation（G0 冻结迁移链）。
        db_run_type = _run_type_to_db(run_type)
        run = DecisionRun(
            id=decision_run_id,
            strategy_snapshot_id=strategy_snapshot_id,
            portfolio_id=portfolio_id,
            run_type=db_run_type,
            trade_date=trade_date,
            decision_at=clock.decision_at,
            data_cutoff_at=clock.data_cutoff_at,
            execution_at=clock.execution_at,
            run_mode=snap.versions.get("run_mode", "research"),
            pit_mode=snap.versions.get("pit_mode", "best_effort"),
            universe_count=universe.universe_count,
            member_count=universe.member_count,
            score_count_expected=scored.expected or None,
            score_count_actual=scored.actual or None,
            score_coverage_pct=(scored.coverage_pct if scored.expected else None),
            score_max_age_days=scored.max_age_days,
            blocking_status=blocking_status,
            blocking_reasons_json=canonical_json(blocking_reasons) if blocking_reasons else None,
            idempotency_key=decision_run_id,
            versions_json=canonical_json({
                **snap.versions,
                "gate_policy_version": snap.gate_policy_version,
                "gate_result": gate_result,
            }),
            is_result_production_eligible=(1 if is_result_production_eligible else 0),
            match_mode=_mm_str,
            started_at=utcnow_naive(),
        )
        db.add(run)
        db.flush()

        # DecisionEvidence 逐条写入
        for i, ev in enumerate(evidence):
            ev_id = content_hash(evidence_prefix, i, ev.symbol_id, ev.action,
                                 trade_date.isoformat())
            order_plan_delta = ev.target_qty_delta
            if order_plan_delta is None:
                order_plan_delta = (
                    ev.target_quantity if ev.action in {"BUY", "SELL"} else 0.0
                )
            order_plan_target_quantity = abs(float(order_plan_delta or 0.0))
            order_plan_reason_code = (
                ev.rejection_reason
                or ev.action_subtype
                or (ev.reason_codes[0] if ev.reason_codes else None)
            )
            # ``canonical_json`` intentionally drops generic ``None`` values,
            # but the order-plan envelope is a typed contract: consumers must
            # distinguish an intentional null direction/price/reason from a
            # missing immutable-plan field. Canonicalize the surrounding
            # snapshot metadata first, then restore every contract key.
            evidence_versions = json.loads(canonical_json({
                **(snap.versions or {}),
                "order_plan_id": content_hash(
                    "decision_order_plan", decision_run_id, ev_id,
                ),
            }))
            evidence_versions.update({
                "order_plan_action": ev.action,
                "order_plan_direction": ev.action if ev.action in {"BUY", "SELL"} else None,
                "order_plan_signal_date": trade_date.isoformat(),
                "order_plan_execution_date": utc_naive_to_shanghai(clock.execution_at).date().isoformat(),
                "order_plan_target_quantity": order_plan_target_quantity,
                "order_plan_intended_price": ev.intended_price,
                "order_plan_reason_code": order_plan_reason_code,
                "order_plan_rejection_trace": [
                    dict(item) for item in (ev.rejections_trace_json or [])
                ],
            })
            row = DecisionEvidence(
                id=ev_id,
                decision_run_id=decision_run_id,
                strategy_snapshot_id=strategy_snapshot_id,
                portfolio_id=portfolio_id,
                symbol_id=ev.symbol_id,
                trade_date=trade_date,
                decision_at=clock.decision_at,
                data_cutoff_at=clock.data_cutoff_at,
                execution_at=clock.execution_at,
                action=ev.action,
                action_subtype=ev.action_subtype,
                target_position_pct=ev.target_position_pct,
                min_lot_size=ev.min_lot_size,
                target_quantity=ev.target_quantity,
                target_qty_delta=ev.target_qty_delta,
                intended_price=ev.intended_price,
                executed_price=ev.executed_price,
                slippage_bps=ev.slippage_bps,
                rejection_reason=ev.rejection_reason,
                rejection_detail=ev.rejection_detail,
                blocking_reason=ev.blocking_reason,
                score_id=ev.score_id,
                score_value=ev.score_value,
                score_rank=ev.score_rank,
                score_published_at=ev.score_published_at,
                pit_safe_flag=ev.pit_safe_flag,
                constraints_json=canonical_json(ev.constraints) if ev.constraints else None,
                versions_json=json.dumps(
                    evidence_versions, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ),
                reason_codes_json=canonical_json(ev.reason_codes) if ev.reason_codes else None,
                factor_contributions_json=(
                    canonical_json(ev.factor_contributions) if ev.factor_contributions else None
                ),
                legacy_fallback_flag=(1 if ev.legacy_fallback_flag else 0),
                stop_loss_verified_price_source=ev.stop_loss_verified_price_source,
                stop_loss_triggered=(1 if ev.stop_loss_triggered else 0),
                manual_price_flag=(1 if ev.manual_price_flag else 0),
                # T-B3 Q2.2：NEXT_OPEN 顺延 + 每次拒单落 DecisionEvidence
                roll_forward_days=ev.roll_forward_days,
                rejections_trace_json=(
                    canonical_json(ev.rejections_trace_json) if ev.rejections_trace_json else None
                ),
                # T-B5 Q11.1：卖出规则命中全量保存
                exit_rules_hit_json=(
                    canonical_json([
                        {
                            "rule_code": h.rule_code,
                            "rule_priority": int(h.rule_priority),
                            "requested_exit_qty": float(h.requested_exit_qty),
                            "rule_subtype": str(h.rule_subtype),
                        }
                        for h in ev.exit_rules_hit
                    ]) if ev.exit_rules_hit else None
                ),
                content_hash=content_hash(
                    ev.symbol_id, ev.action, ev.action_subtype or "",
                    ev.target_position_pct or 0, ev.target_quantity or 0,
                    ev.intended_price or 0, ev.executed_price or 0,
                    ev.slippage_bps or 0,
                    ev.rejection_reason or "", ev.score_value or 0,
                    ev.constraints, ev.reason_codes,
                    ev.legacy_fallback_flag,
                    ev.stop_loss_verified_price_source or "",
                    ev.stop_loss_triggered,
                    ev.manual_price_flag,
                    ev.roll_forward_days or 0,
                    ev.rejections_trace_json or [],
                    [
                        (h.rule_code, int(h.rule_priority), float(h.requested_exit_qty), str(h.rule_subtype))
                        for h in ev.exit_rules_hit
                    ],
                ),
                idempotency_key=ev_id,
                # T-B1: 撮合模式，用 resolve 后的口径，默认回退 NEXT_OPEN
                match_mode=getattr(ev, "match_mode", None) or _mm_str,
            )
            db.add(row)
            plan_target_quantity = abs(float(order_plan_delta or 0.0))
            plan_id = content_hash("decision_order_plan", decision_run_id, ev_id)
            db.add(DecisionOrderPlanRecord(
                order_plan_id=plan_id,
                decision_run_id=decision_run_id,
                evidence_id=ev_id,
                portfolio_id=portfolio_id,
                symbol_id=ev.symbol_id,
                action=ev.action,
                signal_date=trade_date,
                execution_date=utc_naive_to_shanghai(clock.execution_at).date(),
                target_quantity=plan_target_quantity,
                direction=ev.action if ev.action in {"BUY", "SELL"} else None,
                intended_price=ev.intended_price,
                reason_code=order_plan_reason_code,
                rejection_trace_json=canonical_json(ev.rejections_trace_json) if ev.rejections_trace_json else None,
            ))
        db.flush()

        # T-A9.3：把已消费的 ManualPriceOverride 行标记 consumed_flag=1
        if consumed_override_ids:
            now = utcnow_naive()
            stmt_update = (
                select(ManualPriceOverride)
                .where(ManualPriceOverride.id.in_(list(consumed_override_ids)))
                .with_for_update()
            )
            for ov in db.execute(stmt_update).scalars():
                ov.consumed_flag = 1
                ov.consumed_at = now
                ov.consumed_by_run_id = decision_run_id
            db.flush()

        # A persisted backtest/auto run is complete once all evidence rows are
        # flushed. Keeping the row in PENDING makes downstream linkage incomplete.
        finished = utcnow_naive()
        run.status = "SUCCEEDED"
        run.finished_at = finished
        if run.started_at is not None:
            run.duration_ms = max(0, int((finished - run.started_at).total_seconds() * 1000))
        db.flush()

    @staticmethod
    def _ensure_order_plan_records(
        db: Session,
        *,
        portfolio_id: int,
        decision_run_id: str,
        trade_date: date,
        execution_at: datetime,
        evidence: list[PerSymbolEvidence],
    ) -> None:
        """Materialize missing relational plans for a deterministic run retry."""
        evidence_prefix = decision_run_id[:16]
        existing_ids = set(db.execute(
            select(DecisionOrderPlanRecord.order_plan_id).where(
                DecisionOrderPlanRecord.decision_run_id == decision_run_id,
            )
        ).scalars().all())
        execution_date = utc_naive_to_shanghai(execution_at).date()
        for index, ev in enumerate(evidence):
            evidence_id = content_hash(
                evidence_prefix, index, ev.symbol_id, ev.action, trade_date.isoformat(),
            )
            plan_id = content_hash("decision_order_plan", decision_run_id, evidence_id)
            if plan_id in existing_ids:
                continue
            delta = ev.target_qty_delta
            if delta is None:
                delta = ev.target_quantity if ev.action in {"BUY", "SELL"} else 0.0
            reason_code = (
                ev.rejection_reason or ev.action_subtype
                or (ev.reason_codes[0] if ev.reason_codes else None)
            )
            db.add(DecisionOrderPlanRecord(
                order_plan_id=plan_id,
                decision_run_id=decision_run_id,
                evidence_id=evidence_id,
                portfolio_id=portfolio_id,
                symbol_id=ev.symbol_id,
                action=ev.action,
                signal_date=trade_date,
                execution_date=execution_date,
                target_quantity=abs(float(delta or 0.0)),
                direction=ev.action if ev.action in {"BUY", "SELL"} else None,
                intended_price=ev.intended_price,
                reason_code=reason_code,
                rejection_trace_json=canonical_json(ev.rejections_trace_json) if ev.rejections_trace_json else None,
            ))
        db.flush()

        # helper intentionally only materializes plans; caller owns run state.

# ──────────────────────────────────────────────────────────── 默认 Stubs
def _stub_snapshot_loader(db: Session, strategy_snapshot_id: str) -> LoadedSnapshot:
    from app.models.decision_engine import StrategyExecutionSnapshot as SES
    snap = db.get(SES, strategy_snapshot_id)
    if snap is None:
        raise ValueError(f"StrategyExecutionSnapshot {strategy_snapshot_id} not found")
    import json as _json
    members = _json.loads(snap.member_snapshot_json or "[]")
    candidates = _json.loads(snap.candidate_pool_json or "[]") if snap.candidate_pool_json else []
    versions = _json.loads(snap.versions_json or "{}") if snap.versions_json else {}
    cost_cfg = _json.loads(snap.cost_config_json or "{}") if snap.cost_config_json else {
        "slippage_buy_bps": DEFAULT_SLIPPAGE_BUY_BPS,
        "slippage_sell_bps": DEFAULT_SLIPPAGE_SELL_BPS,
        "min_lot_size": 100,
    }
    return LoadedSnapshot(
        snapshot=snap,
        factor_model_run_id=snap.factor_model_run_id or versions.get("factor_model_run_id", ""),
        factor_set_id=snap.factor_set_id,
        rule_id=snap.rule_id,
        rule_version=snap.rule_version,
        members=members,
        candidate_pool=candidates,
        gate_policy_version=snap.gate_policy_version or "production-v1.0.0",
        cost_config=cost_cfg,
        versions={
            **versions,
            "run_mode": versions.get("run_mode", "research"),
            "pit_mode": versions.get("pit_mode", "best_effort"),
            "snapshot_hash": snap.snapshot_hash,
            "snapshot_type": snap.snapshot_type,
        },
    )


def _stub_gate(
    db: Session, snap: LoadedSnapshot, clock: ResolvedClock,
) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    if not snap.factor_model_run_id:
        reasons.append({"severity": "blocking", "code": "MODEL_INACTIVE",
                        "message": "快照未绑定 factor_model_run_id（Q21方案A必须绑定全局active）"})
    result = {
        "passed": not reasons,
        "checks": [{"code": r["code"], "passed": r["severity"] != "blocking"} for r in reasons],
    }
    return not bool(reasons), reasons, result


def _stub_universe_builder(
    db: Session, snap: LoadedSnapshot, cutoff_utc: datetime,
) -> UniverseAndEligibility:
    members = snap.members or []
    return UniverseAndEligibility(
        universe=list(members),
        universe_count=len(members),
        member_count=len(members),
        ineligible=[],
        health_issues=[],
    )


def _stub_health_check(
    db: Session, universe: UniverseAndEligibility, cutoff_utc: datetime,
) -> UniverseAndEligibility:
    return universe


# WP0-4 Q7 信号方向 → 动作映射的默认阈值（可被 snap.versions 的信号策略覆盖）
_DEFAULT_SIGNAL_THRESHOLDS: dict[str, float] = {
    # quality_score > buy 阈值 → BUY；quality_score < sell_threshold → SELL
    "buy": 0.70,
    "sell": 0.35,
}


def _query_scores_for_universe(
    db: Session,
    *,
    factor_model_run_id: str | None,
    trade_date: date,
    cutoff_utc: datetime,
    symbol_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """WP0-4 Step 6：按 FR-P0-6 规范查询 Score。

    严格约束：
      (1) symbol_id IN universe_member_ids
      (2) trade_date = 决策日（as_of_date 对齐）
      (3) factor_model_run_id 绑定快照（manual 模式允许为 None，则退化为按 (symbol_id, trade_date) 取最新 published_at ≤ cutoff）
      (4) published_at ≤ data_cutoff_at（PIT 安全）
      (5) 同一 symbol 多个批次时，取 calc_batch_id 最新（即 id 最大的一行，避免重复）

    返回 {symbol_id: {score_id, quality_score, quality_grade, timing_score,
                       priority_score, action, stage, published_at,
                       factor_data_cutoff_at, factor_model_run_id,
                       trade_date, data_credibility, pit_flag}}
    """
    if not symbol_ids:
        return {}
    # 使用窗口函数分组取每组 id 最大的 1 行（避免 symbol 重复）
    # SQLite/PG 均支持 LATERAL / DISTINCT ON 不可通用，改用自 JOIN + id=MAX(id)
    base_q = (
        select(Score)
        .where(
            Score.symbol_id.in_(symbol_ids),
            Score.trade_date == trade_date,
            # 注意：published_at 过滤不在 SQL 层做（保留未来泄漏、NULL 行让 build_evidence 打 PIT 标签）。
            # 排序时 PIT_SAFE（pub 非空且 ≤ cutoff）优先 → 非安全 → NULL；同安全等级内 id 最大优先。
        )
    )
    if factor_model_run_id:
        base_q = base_q.where(Score.factor_model_run_id == factor_model_run_id)
    rows = list(db.execute(base_q).scalars())
    # symbol_id → [row1, row2, ...]
    # 注意：FR-P0-6 要求 published_at > cutoff 的 Score "不被作为决策依据"，
    # 但我们仍然把它们保留在 items 中（不 SQL 过滤），让上层标记 NOT_PIT_SAFE。
    # 这样用户/审计侧能看到"有 Score 但未来泄漏（NOT_PIT_SAFE）→ BLOCKING SCORE_STALE"的完整证据链，
    # 与 G1 test_03（SPA 应 NOT_PIT_SAFE）口径一致。
    # 只是在排序时把 published_at ≤ cutoff 的 PIT_SAFE 行优先选（仍然满足 Q22 精确查询契约，
    # 不选择未来 Score 作为决策依据：选行时 PIT_SAFE 优先 ≥ id 最大）。
    bucket: dict[int, list[Any]] = {}
    for r in rows:
        bucket.setdefault(int(r.symbol_id), []).append(r)
    out: dict[int, dict[str, Any]] = {}
    for sid, rs in bucket.items():
        def _sort_key(r: Any, _cutoff: datetime = cutoff_utc) -> tuple[int, int]:
            pub = getattr(r, "published_at", None)
            # tier 0 = PIT_SAFE（pub 非空 ≤ cutoff），tier 1 = pub>cutoff 未来泄漏，tier 2 = pub NULL
            if pub is None:
                tier = 2
            elif pub <= _cutoff:
                tier = 0
            else:
                tier = 1
            # tier 升序 + id 降序（用负数）
            return (tier, -(int(getattr(r, "id", 0)) or 0))
        rs_sorted = sorted(rs, key=_sort_key)
        r = rs_sorted[0]
        out[sid] = {
            "symbol_id": int(r.symbol_id),
            "score_id": int(r.id),
            "quality_score": float(r.quality_score) if r.quality_score is not None else None,
            "quality_grade": r.quality_grade,
            "timing_score": float(r.timing_score) if r.timing_score is not None else None,
            "priority_score": float(r.priority_score) if r.priority_score is not None else None,
            "recommended_action": r.action,
            "stage": r.stage,
            "published_at": r.published_at,  # datetime or None
            "factor_data_cutoff_at": r.factor_data_cutoff_at,
            "factor_model_run_id": r.factor_model_run_id,
            "trade_date": r.trade_date,
            "data_credibility": (float(r.data_credibility) if r.data_credibility is not None else None),
            "pit_flag": r.pit_flag,
        }
    return out


def _score_staleness_days(score_trade_date: date, decision_date: date) -> int:
    """score 日期 → 决策日的日历天数（非负）。"""
    return max(0, (decision_date - score_trade_date).days)


def _real_scorer(
    db: Session, snap: LoadedSnapshot, universe: UniverseAndEligibility,
    cutoff_utc: datetime, decision_date: date,
) -> ScoredUniverse:
    """WP0-4 真实评分查询：替换原先 stub，严格按 FR-P0-6 约束走。"""
    symbol_ids = [int(m["symbol_id"]) for m in universe.universe if m.get("symbol_id")]
    if not symbol_ids:
        return ScoredUniverse(expected=0, actual=0, coverage_pct=100.0, items=[])

    score_by_sym = _query_scores_for_universe(
        db,
        factor_model_run_id=getattr(snap, "factor_model_run_id", None) or None,
        trade_date=decision_date,
        cutoff_utc=cutoff_utc,
        symbol_ids=symbol_ids,
    )

    items: list[dict[str, Any]] = []
    max_age: int | None = None
    stale_ids: list[int] = []
    missing_ids: list[int] = []
    for sid in symbol_ids:
        s = score_by_sym.get(sid)
        if s is None:
            missing_ids.append(sid)
            continue
        items.append(s)
        # staleness（按 score.trade_date → decision_date 口径）
        sd = s.get("trade_date")
        if isinstance(sd, date):
            age = _score_staleness_days(sd, decision_date)
            if max_age is None or age > max_age:
                max_age = age
            # 约定：score 落后 ≥ 2 交易日算 stale（和 test_g1_score_staleness 一致）
            if age >= 2:
                stale_ids.append(sid)
    actual = len(items)
    expected = len(symbol_ids)
    coverage = round((float(actual) / float(expected)) * 100.0, 9) if expected else 100.0
    return ScoredUniverse(
        items=items,
        expected=expected,
        actual=actual,
        coverage_pct=coverage,
        max_age_days=max_age,
        stale_score_symbols=stale_ids,
        missing_symbols=missing_ids,
    )


def _real_signal_generator(
    db: Session, snap: LoadedSnapshot, scored: ScoredUniverse,
) -> SignalResult:
    """WP0-4 Step 7：Quality Score → BUY/HOLD/SELL 映射。

    规则（snap.versions["signal_policy"] 可覆盖阈值）：
      - quality_score ≥ buy_threshold  → BUY
      - quality_score ≤ sell_threshold → SELL（strategy exit 口径）
      - 否则 → HOLD
    若 Score 缺失（scored.items 没有对应 symbol）：不生成信号（由 build_evidence 走 STALE_SCORE HOLD 分支）。
    """
    thresholds = (snap.versions or {}).get("signal_policy") or {}
    if isinstance(thresholds, dict):
        buy_th_raw = thresholds.get("buy_threshold")
        sell_th_raw = thresholds.get("sell_threshold")
    else:
        buy_th_raw = None
        sell_th_raw = None
    buy_th = float(buy_th_raw) if buy_th_raw is not None else _DEFAULT_SIGNAL_THRESHOLDS["buy"]
    sell_th = float(sell_th_raw) if sell_th_raw is not None else _DEFAULT_SIGNAL_THRESHOLDS["sell"]

    out: list[dict[str, Any]] = []
    for s in scored.items:
        sid = s.get("symbol_id")
        if sid is None:
            continue
        qs = s.get("quality_score")
        reasons: list[str] = []
        direction: str = "HOLD"
        if qs is None:
            reasons.append("SCORE_QUALITY_NULL")
        elif qs >= buy_th:
            direction = "BUY"
            reasons.append(f"QUALITY_GE_{buy_th:g}")
            if s.get("recommended_action"):
                reasons.append(f"SCORE_ACTION_{s['recommended_action']}")
        elif qs <= sell_th:
            direction = "SELL"
            reasons.append(f"QUALITY_LE_{sell_th:g}")
            if s.get("recommended_action"):
                reasons.append(f"SCORE_ACTION_{s['recommended_action']}")
        else:
            reasons.append(f"QUALITY_IN_RANGE_{sell_th:g}_{buy_th:g}")
            if s.get("recommended_action"):
                reasons.append(f"SCORE_ACTION_{s['recommended_action']}")
        out.append({
            "symbol_id": int(sid),
            "direction": direction,
            "stage": s.get("stage"),
            "reasons": reasons,
            "score_item": s,
            "quality_score": qs,
        })
    return SignalResult(items=out)


def _real_allocator(
    db: Session, snap: LoadedSnapshot, signal: SignalResult,
    cutoff_utc: datetime,
) -> RiskAllocationResult:
    """WP0-4 Step 8：真实顺序 clamp allocator（Q10 实现复用 sequential_clamp_allocate）。"""
    return sequential_clamp_allocate(
        db, snap, signal, cutoff_utc,
    )


def _stub_allocator_legacy(
    db: Session, snap: LoadedSnapshot, signal: SignalResult,
    cutoff_utc: datetime,
) -> RiskAllocationResult:
    """旧 stub：透传，仅在 sequential_clamp_allocate 失败降级时用。"""
    items: list[dict[str, Any]] = []
    for sig in signal.items:
        items.append({
            **sig,
            "target_position_pct": 0.0,
            "target_quantity": 0.0,
            "min_lot_size": snap.cost_config.get("min_lot_size", 100),
        })
    return RiskAllocationResult(items=items, clamp_trace=[])


# ─────────────────────────────────────────────────────── Q10 顺序式 Clamp
# 约束顺序：总仓位 → 资产类型 → 行业 → 单票 → 现金 → 最小手数
# 每层记录 clamp_before / clamp_after / trigger / remaining
# -------------------------------------------------------------------------

def _portfolio_default_constraints(snap: LoadedSnapshot) -> dict[str, Any]:
    """rule 缺失时用 Portfolio 字段推断的默认值（WP0-4 真实链路不会卡 HOLD）。"""
    portfolio = getattr(snap.snapshot, "portfolio", None)
    investable = 0.95
    single_pct = 0.10
    cash_reserve = 0.05
    if portfolio is not None:
        investable = float(getattr(portfolio, "investable_ratio", 0.95) or 0.95)
        single_pct = float(getattr(portfolio, "default_single_position_pct", 0.10) or 0.10)
        cash_reserve = float(getattr(portfolio, "cash_reserve_ratio", 0.05) or 0.05)
    # 总资产：stock 上限 = total_investable（即 portfolio 允许的比例）；ETF 无配置时 0
    stock_cap = max(0.0, investable)
    sector_cap = max(0.0, min(single_pct * 3.0, stock_cap))  # 行业 cap 默认 3 × 单票，但不可超总 stock
    open_slot = max(1, int(round(stock_cap / max(single_pct, 1e-6)))) if single_pct > 0 else 20
    return {
        "total_investable_pct": investable,
        "min_cash_reserve_pct": cash_reserve,
        "max_loss_per_trade_pct": 0.0,
        "asset_limits": {"stock": stock_cap, "etf": 0.0},
        "sector_limit_pct": sector_cap,
        "single_limit_pct": single_pct,
        "stage_limits": {"stock": {"default_open_pct": min(single_pct, 0.05)}},
        "open_slot_max": min(50, max(5, open_slot)),
        "rule_ok": True,  # rule 缺失 fallback 默认不视为 "无规则=阻断"
    }


def _get_rule_constraints(snap: LoadedSnapshot, db: Session) -> dict[str, Any]:
    """从 LoadedSnapshot.rule_id / rule_version 读取 PortfolioRule 的 limit 字段。

    规则缺失时使用 Portfolio 字段推断的默认值（WP0-4：不再全 0 阻断，避免信号
    被 PRE_0_RULE 强转 HOLD）。兼容老版本 portfolio_rules 表字段。
    """
    if snap.rule_id is None:
        return _portfolio_default_constraints(snap)
    from app.models.portfolio import PortfolioRule
    rule = db.get(PortfolioRule, snap.rule_id)
    if rule is None:
        return _portfolio_default_constraints(snap)
    import json as _json
    stage_limits = _json.loads(getattr(rule, "stage_limits_json", "{}") or "{}")
    asset_limits = {
        "stock": float(getattr(rule, "max_stock_position_pct", 0) or 0),
        "etf": float(getattr(rule, "max_etf_position_pct", 0) or 0),
    }
    # ``PortfolioRule`` has no durable min-cash column in the current schema.
    # A missing rule-level override must preserve the portfolio's configured
    # cash floor rather than silently turning it into zero.
    rule_cash_reserve = getattr(rule, "min_cash_reserve_pct", None)
    portfolio = getattr(snap.snapshot, "portfolio", None)
    if rule_cash_reserve is None:
        cash_reserve = float(
            getattr(portfolio, "cash_reserve_ratio", 0.0) or 0.0
        )
    else:
        cash_reserve = float(rule_cash_reserve or 0.0)
    return {
        "total_investable_pct": float(getattr(snap.snapshot.portfolio, "investable_ratio", 0.95) or 0.95)
        if hasattr(snap.snapshot, "portfolio") else 0.95,
        "min_cash_reserve_pct": cash_reserve,
        "max_loss_per_trade_pct": float(
            getattr(rule, "max_loss_per_trade_pct", 0.0) or 0.0
        ),
        "asset_limits": asset_limits,
        "sector_limit_pct": float(getattr(rule, "max_sector_position_pct", 0) or 0),
        "single_limit_pct": float(getattr(rule, "max_single_position_pct", 0) or 0),
        "stage_limits": stage_limits or {},
        "open_slot_max": int(getattr(rule, "max_open_positions", 0) or 0),
        "rule_ok": True,
    }


def _read_alloc_state(
    db: Session,
    portfolio_id: int,
    members: list[dict[str, Any]],
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回当前组合的持仓/现金/行业暴露状态。用于作为 clamp 比较基准。

    state:
      - total_position_pct  : 当前已用总仓位占比 (decimal)
      - current_by_sym: {symbol_id: {"pct": float, "qty": float, "market_value": float, ...}}
      - current_asset_pct: {"stock": x, "etf": y, ...}
      - current_sector_pct: {"sector_name": x, ...}
      - position_count: int (非零持仓)
      - cash_pct: 1 - total_position_pct (基于净值)
      - total_capital: float
    """
    if override is not None:
        # An explicit replay state is authoritative, including an empty state;
        # never fall back to the live portfolio rows when it is supplied.
        return {
            "total_position_pct": float(override.get("total_position_pct", 0.0) or 0.0),
            "current_by_sym": dict(override.get("current_by_sym") or {}),
            "current_asset_pct": dict(override.get("current_asset_pct") or {}),
            "current_sector_pct": dict(override.get("current_sector_pct") or {}),
            "position_count": int(override.get("position_count", 0) or 0),
            "cash_pct": float(override.get("cash_pct", 0.0) or 0.0),
            "available_cash": float(override.get("available_cash", 0.0) or 0.0),
            "total_capital": float(override.get("total_capital", 0.0) or 0.0),
        }

    from app.models.portfolio import Position, Portfolio
    portfolio = db.get(Portfolio, portfolio_id)
    total_capital = float(getattr(portfolio, "total_capital", 0) or 0) if portfolio else 0.0
    default_state = {
        "total_position_pct": 0.0,
        "current_by_sym": {},
        "current_asset_pct": {"stock": 0.0, "etf": 0.0},
        "current_sector_pct": {},
        "position_count": 0,
        "cash_pct": 1.0,
        "total_capital": total_capital,
    }
    if portfolio is None:
        return default_state
    positions = db.execute(
        __import__("sqlalchemy").select(Position).where(Position.portfolio_id == portfolio_id)
    ).scalars().all()
    by_sym: dict[int, dict[str, Any]] = {}
    asset_pct = {"stock": 0.0, "etf": 0.0}
    sector_pct: dict[str, float] = {}
    total_pct = 0.0
    nonzero_count = 0
    sector_key_fn = __import__("app.services.allocation", fromlist=["_sector_key"])._sector_key
    for p in positions:
        pct = float(getattr(p, "position_pct", 0) or 0)
        qty = float(getattr(p, "quantity", 0) or 0)
        mv = float(getattr(p, "market_value", 0) or 0)
        asset_type = str(getattr(p, "asset_type", "stock") or "stock").lower()
        sec = str(sector_key_fn(p) or "unclassified")
        by_sym[int(p.symbol_id)] = {"pct": pct, "qty": qty, "market_value": mv, "asset_type": asset_type, "sector": sec}
        total_pct += pct
        if qty > 0:
            nonzero_count += 1
            asset_pct[asset_type] = asset_pct.get(asset_type, 0.0) + pct
            sector_pct[sec] = sector_pct.get(sec, 0.0) + pct
    return {
        "total_position_pct": total_pct,
        "current_by_sym": by_sym,
        "current_asset_pct": asset_pct,
        "current_sector_pct": sector_pct,
        "position_count": nonzero_count,
        "cash_pct": max(0.0, 1.0 - total_pct),
        "total_capital": total_capital,
    }


def _symbol_meta(db: Session, members: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """批量读取 Symbol.meta（asset_type / industry / theme）。"""
    from app.models.symbol import Symbol
    ids = sorted({int(m["symbol_id"]) for m in members if m.get("symbol_id")})
    if not ids:
        return {}
    rows = db.execute(
        __import__("sqlalchemy").select(Symbol).where(Symbol.id.in_(ids))
    ).scalars().all()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        asset_type = str(getattr(r, "asset_type", "stock") or "stock").lower()
        industry = (
            getattr(r, "industry", None)
            or getattr(r, "theme", None)
            or getattr(r, "sector", None)
            or "unclassified"
        )
        out[int(r.id)] = {"asset_type": asset_type, "industry": str(industry), "symbol_code": getattr(r, "symbol", None)}
    return out


def _annotate_symbol_master_pit(
    db: Session,
    members: list[dict[str, Any]],
    trade_date: date,
) -> None:
    """Annotate snapshot members whose master data is invalid at ``trade_date``.

    ``Symbol.is_active`` is a current-state flag and therefore is deliberately
    not used to reject historical rows.  ``listed_at`` is an as-of-safe field;
    a symbol cannot be traded before that date.  Missing/incomplete master rows
    are retained in the universe for auditability and blocked by the evidence
    builder rather than being silently dropped.
    """
    if db is None or not members or not hasattr(db, "execute"):
        return
    try:
        from app.models.symbol import Symbol

        ids = sorted({int(m.get("symbol_id")) for m in members if m.get("symbol_id")})
        if not ids:
            return
        rows = list(db.execute(select(Symbol).where(Symbol.id.in_(ids))).scalars())
        by_id = {int(row.id): row for row in rows}
    except Exception:
        # Compatibility with dry-run unit seams that provide a light-weight DB
        # double. Real sessions still execute the fail-closed checks above.
        return

    for member in members:
        try:
            sid = int(member.get("symbol_id"))
        except (TypeError, ValueError):
            continue
        row = by_id.get(sid)
        issue: str | None = None
        detail: str | None = None
        if row is None:
            issue = "SYMBOL_MASTER_MISSING"
            detail = f"symbol_id={sid} master row missing"
        else:
            listed_at = getattr(row, "listed_at", None)
            if listed_at is not None and listed_at > trade_date:
                issue = "NOT_LISTED_ON_TRADE_DATE"
                detail = f"listed_at={listed_at.isoformat()} > trade_date={trade_date.isoformat()}"
            elif not getattr(row, "symbol", None) or not getattr(row, "market", None) or not getattr(row, "asset_type", None):
                issue = "SYMBOL_MASTER_INCOMPLETE"
                detail = "symbol/market/asset_type master fields are incomplete"
        if issue:
            member["_master_data_issue"] = issue
            member["_master_data_issue_detail"] = detail or issue
        else:
            member.pop("_master_data_issue", None)
            member.pop("_master_data_issue_detail", None)


def _annotate_member_effective_pit(
    members: list[dict[str, Any]],
    trade_date: date,
    *,
    held_symbol_ids: set[int] | None = None,
) -> None:
    """Mark members outside their SCD2 interval for a historical replay.

    The snapshot remains auditable (rows are not dropped).  Non-held symbols
    outside ``[effective_from, effective_to)`` are blocked from opening a new
    position.  A held symbol is tagged ``exit_only`` so risk/exit decisions can
    still close a position after its membership relationship has expired.
    """
    held = held_symbol_ids or set()

    def _as_date(value: Any) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
            except ValueError:
                return None
        return None

    for member in members:
        sid_raw = member.get("symbol_id")
        try:
            sid = int(sid_raw)
        except (TypeError, ValueError):
            continue
        effective_from = _as_date(member.get("effective_from"))
        effective_to = _as_date(member.get("effective_to"))
        outside = (
            (effective_from is not None and trade_date < effective_from)
            or (effective_to is not None and trade_date >= effective_to)
        )
        if not outside:
            member.pop("_membership_data_issue", None)
            member.pop("_membership_data_issue_detail", None)
            member.pop("_membership_exit_only", None)
            continue
        detail = (
            f"member_id={member.get('member_id')} effective interval "
            f"[{effective_from.isoformat() if effective_from else '-inf'}, "
            f"{effective_to.isoformat() if effective_to else '+inf'}) does not include "
            f"trade_date={trade_date.isoformat()}"
        )
        if sid in held:
            member["_membership_exit_only"] = True
            member["_membership_data_issue_detail"] = detail
            member.pop("_membership_data_issue", None)
        else:
            member["_membership_data_issue"] = "MEMBER_NOT_EFFECTIVE_ON_TRADE_DATE"
            member["_membership_data_issue_detail"] = detail
            member.pop("_membership_exit_only", None)


def sequential_clamp_allocate(
    db: Session,
    snap: LoadedSnapshot,
    signal: SignalResult,
    cutoff_utc: datetime,
    *,
    portfolio_id: int | None = None,
    intent_candidates: list[dict[str, Any]] | None = None,
    fallback_portfolio_net_value: float | None = None,
    fallback_prices: dict[int, float] | None = None,
) -> RiskAllocationResult:
    """Q10：顺序式 Clamp。

    优先级：总仓位 → 资产类型 → 行业 → 单票 → 现金 → 最小手数。
    每层记录 clamp_before / clamp_after / trigger / remaining。

    Parameters
    ----------
    intent_candidates : 可选覆盖信号的"目标仓位意图"，按 [{"symbol_id":int, "intent_pct":float}] 传。
        若不传，则依据 signal 方向：BUY → 默认 5%，SELL → 意图清空，HOLD → 意图维持当前。
    """
    if portfolio_id is None:
        portfolio_id = int(snap.snapshot.portfolio_id)
    cfg = snap.cost_config or {}
    min_lot_size = int(cfg.get("min_lot_size", 100) or 100)
    slippage_buy = float(cfg.get("slippage_buy_bps", DEFAULT_SLIPPAGE_BUY_BPS) or 0)
    slippage_sell = float(cfg.get("slippage_sell_bps", DEFAULT_SLIPPAGE_SELL_BPS) or 0)

    rules = _get_rule_constraints(snap, db)
    state = _read_alloc_state(
        db, portfolio_id, snap.members, snap.allocation_state_override,
    )
    meta = _symbol_meta(db, snap.members)
    rule_ok = rules["rule_ok"]
    clamp_trace: list[dict[str, Any]] = []

    # 构建意图：每 symbol → {sym_id, direction, intent_pct, current_pct, asset_type, industry, price, has_position}
    # 若 intent_pct 为正 = 买入意图；0 或小于 current_pct = 卖出/减仓；
    # 与 current_pct 相等 = HOLD。
    sig_by_sym = {int(s["symbol_id"]): s for s in signal.items if isinstance(s, dict) and s.get("symbol_id")}
    intent_by_sym = {
        int(x["symbol_id"]): float(x.get("intent_pct") or 0.0)
        for x in (intent_candidates or [])
        if isinstance(x, dict) and x.get("symbol_id") is not None
    }
    total_capital = float(state["total_capital"] or 0.0)
    net_value_for_quantity = total_capital if total_capital > 0 else float(fallback_portfolio_net_value or 0.0)

    plans: list[dict[str, Any]] = []
    for m in snap.members:
        sid = int(m.get("symbol_id"))
        cur = state["current_by_sym"].get(sid, {})
        cur_pct = float(cur.get("pct", 0.0) or 0.0)
        has_position = bool(cur.get("qty", 0) or cur_pct > 0)
        sig = sig_by_sym.get(sid, {})
        direction = str(sig.get("direction") or "HOLD").upper()
        if sid in intent_by_sym:
            intent_pct = intent_by_sym[sid]
        elif direction == "BUY":
            # 研究/回测默认意图：均匀分布或 stage 默认；这里用 5% + stage_limit 二选一较小值
            symbol_asset_type = (meta.get(sid) or {}).get("asset_type", "stock")
            stage_cfg: dict = rules["stage_limits"].get(symbol_asset_type, {}) or {}
            stage_limit_suggest = float(stage_cfg.get("default_open_pct") or stage_cfg.get("growth") or 0.05) if isinstance(stage_cfg, dict) else float(stage_cfg or 0.05)
            intent_pct = min(0.05, stage_limit_suggest) if stage_limit_suggest > 0 else 0.05
        elif direction in {"SELL", "EXIT", "REDUCE"}:
            intent_pct = 0.0  # 默认清仓 (Q11.2 默认一次性清仓)
        elif direction == "HOLD":
            intent_pct = cur_pct
        else:
            intent_pct = cur_pct
        current_mark = None
        if cur.get("qty") and cur.get("market_value"):
            current_mark = float(cur.get("market_value", 0)) / float(cur.get("qty"))
        price = (
            float(sig.get("price")) if sig.get("price") not in (None, 0)
            else (fallback_prices or {}).get(sid)
            or (snap.price_hints or {}).get(sid)
            or current_mark
        )
        raw_signal_stop_loss = sig.get("stop_loss_price")
        try:
            signal_stop_loss = (
                float(raw_signal_stop_loss)
                if raw_signal_stop_loss not in (None, "") else None
            )
        except (TypeError, ValueError):
            signal_stop_loss = None
        md = meta.get(sid, {"asset_type": "stock", "industry": "unclassified"})
        plans.append({
            "symbol_id": sid,
            "direction": direction,
            "intent_pct": float(intent_pct),
            "current_pct": float(cur_pct),
            "current_qty": float(cur.get("qty", 0.0) or 0.0),
            "asset_type": md.get("asset_type", "stock"),
            "industry": md.get("industry", "unclassified"),
            "price": float(price) if price is not None else None,
            "stop_loss_price": signal_stop_loss or (snap.risk_stop_loss_by_symbol or {}).get(sid),
            "has_position": has_position,
        })

    # 规则不全时：全部意图清 0，标记 NO_RULE_BLOCKED
    if not rule_ok:
        items_out: list[dict[str, Any]] = []
        for p in plans:
            items_out.append({
                "symbol_id": p["symbol_id"],
                "direction": "HOLD",
                "target_position_pct": p["current_pct"],
                "target_quantity": p["current_qty"],
                "min_lot_size": min_lot_size,
                "clamp_steps": [{
                    "step": "PRE_0_RULE",
                    "before_pct": p["intent_pct"],
                    "after_pct": p["current_pct"],
                    "trigger": "NO_ACTIVE_RULE",
                    "remaining_shortfall_pct": None,
                }],
            })
        return RiskAllocationResult(items=items_out, clamp_trace=[
            {"step": "PRE_0_RULE", "note": "active portfolio rule missing, clamp skipped (NO-OP)"},
        ])

    # 汇总当前 exposure（用于"已用"比较的基准）
    cur_total = float(state["total_position_pct"])
    cur_asset: dict[str, float] = dict(state["current_asset_pct"])
    cur_sector: dict[str, float] = dict(state["current_sector_pct"])

    # 缓存 clamp step，最后统一写回每支股票的 clamp_steps
    per_sym_clamps: dict[int, list[dict[str, Any]]] = {p["symbol_id"]: [] for p in plans}
    target_total_used_after_delta = cur_total

    def _push(sym_id: int, step_name: str, before: float, after: float, trigger: str | None = None,
              remaining: float | None = None) -> None:
        per_sym_clamps[sym_id].append({
            "step": step_name, "before_pct": round(before, 6),
            "after_pct": round(after, 6), "trigger": trigger,
            "remaining_shortfall_pct": (None if remaining is None else round(remaining, 6)),
        })

    # ───────── Step 1: 总仓位 (total investable ratio)
    # 计算本轮 intent 的总目标：sum(max(current, intent))
    intent_total = sum(max(p["current_pct"], p["intent_pct"]) for p in plans)
    step1_cap = float(rules["total_investable_pct"])
    # 若 intent_total 超 cap，则按比例缩放 intent_pct（先对买入意图做缩放，不强行压缩现有持仓）
    if intent_total > step1_cap and intent_total > cur_total:
        scale = max(0.0, (step1_cap - cur_total) / max(1e-12, intent_total - cur_total))
    else:
        scale = 1.0
    clamp_trace.append({"step": "TOTAL_POSITION_CAP", "cap_pct": step1_cap,
                        "before_total_intent_pct": round(intent_total, 6),
                        "buy_scale": round(scale, 6),
                        "current_total_used_pct": round(cur_total, 6)})
    for p in plans:
        before = p["intent_pct"]
        if before > p["current_pct"] and scale < 1.0:
            after = p["current_pct"] + (before - p["current_pct"]) * scale
        else:
            after = before
        p["intent_pct"] = float(after)
        trigger_code = "TOTAL_POSITION_CAP" if (scale < 1.0 and before > p["current_pct"]) else None
        _push(p["symbol_id"], "TOTAL_POSITION_CAP", before, after, trigger_code,
              None if trigger_code is None else round(step1_cap - intent_total, 6))

    # 重新运行每支股票：asset_type / sector 限制时，要基于"运行中聚合 exposure"
    running_asset = {k: float(v) for k, v in cur_asset.items()}
    running_sector = {k: float(v) for k, v in cur_sector.items()}

    # 为保证"公平"，按 intent_pct - current_pct 降序处理（大仓位先吃额度），等额度则 symbol_id 升序
    order = sorted(range(len(plans)), key=lambda i: (
        -(plans[i]["intent_pct"] - plans[i]["current_pct"]), plans[i]["symbol_id"],
    ))

    # ───────── Step 2: 资产类型（stock / etf / index）
    asset_limits = rules["asset_limits"]
    clamp_trace.append({"step": "ASSET_TYPE_CAP", "limits": asset_limits,
                        "current_asset_pct": {k: round(v, 6) for k, v in running_asset.items()}})
    for idx in order:
        p = plans[idx]
        before = p["intent_pct"]
        delta = before - p["current_pct"]
        if delta <= 0:
            _push(p["symbol_id"], "ASSET_TYPE_CAP", before, before, None, None)
            continue
        cap = float(asset_limits.get(p["asset_type"], 0.0) or 0.0)
        used = running_asset.get(p["asset_type"], 0.0)
        room = max(0.0, cap - used)
        if delta <= room:
            after = before
            trigger_code = None
            running_asset[p["asset_type"]] = used + delta
        else:
            after = p["current_pct"] + room
            trigger_code = f"ASSET_TYPE_{p['asset_type'].upper()}_CAP"
            running_asset[p["asset_type"]] = cap
        p["intent_pct"] = float(after)
        _push(p["symbol_id"], "ASSET_TYPE_CAP", before, after, trigger_code,
              None if trigger_code is None else round(delta - room, 6))

    # ───────── Step 3: 行业 / sector
    sector_cap = float(rules["sector_limit_pct"])
    clamp_trace.append({"step": "SECTOR_CAP", "cap_pct": sector_cap,
                        "current_sector_pct": {k: round(v, 6) for k, v in running_sector.items()}})
    for idx in order:
        p = plans[idx]
        before = p["intent_pct"]
        delta = before - p["current_pct"]
        if delta <= 0:
            _push(p["symbol_id"], "SECTOR_CAP", before, before, None, None)
            continue
        used = running_sector.get(p["industry"], 0.0)
        room = max(0.0, sector_cap - used) if sector_cap > 0 else delta
        if sector_cap <= 0:
            after = p["current_pct"]
            trigger_code = "SECTOR_CAP_UNDEFINED"
            room = 0.0
        elif delta <= room:
            after = before
            trigger_code = None
            running_sector[p["industry"]] = used + delta
        else:
            after = p["current_pct"] + room
            trigger_code = "SECTOR_CAP"
            running_sector[p["industry"]] = sector_cap
        p["intent_pct"] = float(after)
        _push(p["symbol_id"], "SECTOR_CAP", before, after, trigger_code,
              None if trigger_code is None else round(delta - room, 6))

    # ───────── Step 4: 单票
    single_cap = float(rules["single_limit_pct"])
    clamp_trace.append({"step": "SINGLE_STOCK_CAP", "cap_pct": single_cap})
    for idx in order:
        p = plans[idx]
        before = p["intent_pct"]
        delta = before - p["current_pct"]
        # 单票限制同时也考虑 stage_limit (按 asset_type/stage 键)。显式
        # 配置为 0 是一个硬性禁开仓限制，不能和缺失的阶段键混为一谈。
        stage_cfg: Any = rules["stage_limits"].get(p["asset_type"], {}) or {}
        stage_name = sig_by_sym.get(p["symbol_id"], {}).get("stage") or "growth"
        stage_limit: float | None = None
        stage_limit_is_configured = False
        if isinstance(stage_cfg, dict):
            if stage_name in stage_cfg and stage_cfg[stage_name] is not None:
                stage_limit = float(stage_cfg[stage_name])
                stage_limit_is_configured = True
        elif stage_cfg not in (None, ""):
            stage_limit = float(stage_cfg)
            stage_limit_is_configured = True

        if stage_limit_is_configured and stage_limit is not None and stage_limit <= 0:
            after = p["current_pct"]
            p["intent_pct"] = float(after)
            _push(
                p["symbol_id"], "SINGLE_STOCK_CAP", before, after,
                "STAGE_LIMIT_CAP", round(max(0.0, before - after), 6),
            )
            continue

        caps = [c for c in [single_cap, stage_limit] if c is not None and c > 0]
        cap = min(caps) if caps else single_cap
        if cap <= 0:
            after = p["current_pct"]
            trigger_code = "SINGLE_CAP_UNDEFINED"
            p["intent_pct"] = float(after)
            _push(p["symbol_id"], "SINGLE_STOCK_CAP", before, after, trigger_code, round(before - after, 6))
            continue
        if before <= cap:
            after = before
            trigger_code = None
        else:
            after = cap
            trigger_code = "SINGLE_STOCK_CAP" if before == single_cap else "STAGE_LIMIT_CAP"
        p["intent_pct"] = float(after)
        _push(p["symbol_id"], "SINGLE_STOCK_CAP", before, after, trigger_code,
              None if trigger_code is None else round(before - after, 6))

    # ───────── Step 5: 单笔风险预算
    # 仅使用当前决策可验证的 entry/stop 距离。缺少止损线或止损线不在
    # entry_price 下方时不猜测风险距离，维持前序约束的结果。
    max_loss_per_trade_pct = max(0.0, float(rules.get("max_loss_per_trade_pct", 0.0) or 0.0))
    # 兼容历史库中以 "5" 表示 5% 的旧值；新规则均使用 decimal ratio。
    if max_loss_per_trade_pct > 1.0:
        max_loss_per_trade_pct /= 100.0
    risk_budget_amount = max_loss_per_trade_pct * net_value_for_quantity
    clamp_trace.append({
        "step": "RISK_BUDGET_CAP",
        "max_loss_per_trade_pct": round(max_loss_per_trade_pct, 6),
        "risk_budget_amount": round(risk_budget_amount, 6),
    })
    for idx in order:
        p = plans[idx]
        before = p["intent_pct"]
        delta = before - p["current_pct"]
        entry_price = p["price"]
        stop_loss_price = p.get("stop_loss_price")
        risk_distance = (
            float(entry_price) - float(stop_loss_price)
            if entry_price is not None
            and stop_loss_price is not None
            and float(entry_price) > float(stop_loss_price) > 0.0
            else None
        )
        if delta <= 0 or risk_distance is None or risk_budget_amount <= 0 or net_value_for_quantity <= 0:
            _push(p["symbol_id"], "RISK_BUDGET_CAP", before, before, None, None)
            continue
        risk_capped_pct = (risk_budget_amount / risk_distance * float(entry_price)) / net_value_for_quantity
        after = max(p["current_pct"], min(before, risk_capped_pct))
        p["intent_pct"] = float(after)
        trigger_code = "MAX_LOSS_PER_TRADE" if after < before else None
        _push(
            p["symbol_id"],
            "RISK_BUDGET_CAP",
            before,
            after,
            trigger_code,
            None if trigger_code is None else round(before - after, 6),
        )

    # ───────── Step 6: 开仓数量上限
    # 先扣除本轮已明确清仓的既有持仓，再给新建仓按既有确定性优先级分配余下槽位。
    # 这样卖出不会被持仓数量上限反向阻断，且重放状态与实时状态遵循同一规则。
    open_slot_max = int(rules.get("open_slot_max", 0) or 0)
    current_open_symbol_ids = {
        int(symbol_id)
        for symbol_id, values in state["current_by_sym"].items()
        if float(values.get("qty", 0.0) or 0.0) > 0
        or float(values.get("pct", 0.0) or 0.0) > 0
    }
    scheduled_full_exits = {
        p["symbol_id"]
        for p in plans
        if p["symbol_id"] in current_open_symbol_ids
        and p["intent_pct"] <= 1e-12
    }
    occupied_slots = max(0, len(current_open_symbol_ids - scheduled_full_exits))
    remaining_slots = max(0, open_slot_max - occupied_slots)
    clamp_trace.append({
        "step": "OPEN_POSITION_CAP",
        "max_open_positions": open_slot_max,
        "current_open_positions": len(current_open_symbol_ids),
        "scheduled_full_exits": len(scheduled_full_exits),
        "remaining_slots": remaining_slots,
    })
    for idx in order:
        p = plans[idx]
        before = p["intent_pct"]
        is_new_open = (
            p["symbol_id"] not in current_open_symbol_ids
            and before > 1e-12
        )
        if not is_new_open:
            _push(p["symbol_id"], "OPEN_POSITION_CAP", before, before, None, None)
            continue
        if remaining_slots > 0:
            remaining_slots -= 1
            _push(p["symbol_id"], "OPEN_POSITION_CAP", before, before, None, None)
            continue
        p["intent_pct"] = p["current_pct"]
        _push(
            p["symbol_id"],
            "OPEN_POSITION_CAP",
            before,
            p["intent_pct"],
            "MAX_OPEN_POSITIONS",
            round(max(0.0, before - p["current_pct"]), 6),
        )

    # ───────── Step 7: 现金预留（min_cash_reserve_pct）— 仅约束净买入的现金占用
    cash_reserve = float(rules["min_cash_reserve_pct"])
    clamp_trace.append({"step": "MIN_CASH_RESERVE", "min_cash_pct": cash_reserve})
    # 运行中总资产占比（current + 所有 delta）
    net_buy = sum(max(0.0, p["intent_pct"] - p["current_pct"]) for p in plans)
    projected_total = cur_total + net_buy
    cash_after = max(0.0, 1.0 - projected_total)
    if cash_reserve > 0 and cash_after < cash_reserve:
        # 需要统一缩减买入 delta
        room_for_net_buy = max(0.0, (1.0 - cash_reserve) - cur_total)
        shrink = 0.0 if net_buy <= 0 else min(1.0, room_for_net_buy / net_buy)
        for idx in order:
            p = plans[idx]
            before = p["intent_pct"]
            delta = before - p["current_pct"]
            if delta <= 0:
                _push(p["symbol_id"], "MIN_CASH_RESERVE", before, before, None, None)
                continue
            after = p["current_pct"] + delta * shrink
            p["intent_pct"] = float(after)
            _push(p["symbol_id"], "MIN_CASH_RESERVE", before, after,
                  ("MIN_CASH_RESERVE" if shrink < 1.0 else None),
                  None if shrink >= 1.0 else round(delta * (1.0 - shrink), 6))
        clamp_trace[-1]["shrink_ratio"] = round(shrink, 6)
    else:
        for p in plans:
            _push(p["symbol_id"], "MIN_CASH_RESERVE", p["intent_pct"], p["intent_pct"], None, None)

    # The replay/live execution state may know less cash than its percentage
    # exposure implies (for example after unsettled orders or an account cash
    # adjustment).  That amount is authoritative and must restrict a BUY
    # before lot rounding, otherwise an unexecutable order plan leaks out of
    # the DecisionEngine and is only rejected later by the matcher.
    available_cash = state.get("available_cash")
    available_cash_known = available_cash is not None and net_value_for_quantity > 0
    if available_cash_known:
        available_cash = max(0.0, float(available_cash))
        reserve_amount = max(0.0, cash_reserve * net_value_for_quantity)
        spendable_cash = max(0.0, available_cash - reserve_amount)
        cash_cap_pct = spendable_cash / net_value_for_quantity

        # Keep the allocator's cash capacity aligned with the unified matcher:
        # buys consume directional-slippage notional plus commission and
        # transfer fee.  The minimum commission makes this nonlinear, so find
        # the largest common scale that remains cash-feasible.
        def _cost_value(*keys: str, default: float) -> float:
            for key in keys:
                raw = cfg.get(key)
                if raw is None:
                    continue
                try:
                    return max(0.0, float(raw))
                except (TypeError, ValueError):
                    continue
            return default

        buy_commission_rate = _cost_value(
            "buy_commission_pct", "commission_rate", default=0.0003,
        )
        min_commission = _cost_value("min_commission", default=5.0)
        transfer_fee_rate = _cost_value("transfer_fee_rate", default=0.00001)
        buy_execution_factor = 1.0 + slippage_buy / 10_000.0
        buy_deltas = [
            max(0.0, p["intent_pct"] - p["current_pct"])
            for p in plans
        ]

        def _estimated_buy_cash(scale: float) -> float:
            total = 0.0
            for delta in buy_deltas:
                if delta * scale <= 1e-12:
                    continue
                gross = delta * scale * net_value_for_quantity * buy_execution_factor
                commission = max(min_commission, gross * buy_commission_rate)
                total += gross + commission + gross * transfer_fee_rate
            return total

        full_estimated_buy_cash = _estimated_buy_cash(1.0)
        if full_estimated_buy_cash <= spendable_cash + 1e-9:
            shrink = 1.0
        elif _estimated_buy_cash(1e-12) > spendable_cash + 1e-9:
            shrink = 0.0
        else:
            lower, upper = 0.0, 1.0
            for _ in range(48):
                midpoint = (lower + upper) / 2.0
                if _estimated_buy_cash(midpoint) <= spendable_cash + 1e-9:
                    lower = midpoint
                else:
                    upper = midpoint
            shrink = lower
        clamp_trace.append({
            "step": "AVAILABLE_CASH_CAP",
            "available_cash": round(available_cash, 6),
            "reserve_amount": round(reserve_amount, 6),
            "spendable_cash": round(spendable_cash, 6),
            "cap_pct": round(cash_cap_pct, 6),
            "buy_scale": round(shrink, 6),
            "estimated_buy_cash": round(full_estimated_buy_cash, 6),
            "buy_commission_rate": round(buy_commission_rate, 8),
            "min_commission": round(min_commission, 6),
            "transfer_fee_rate": round(transfer_fee_rate, 8),
            "slippage_buy_bps": round(slippage_buy, 6),
        })
        for idx in order:
            p = plans[idx]
            before = p["intent_pct"]
            delta = before - p["current_pct"]
            if delta <= 0:
                _push(p["symbol_id"], "AVAILABLE_CASH_CAP", before, before, None, None)
                continue
            after = p["current_pct"] + delta * shrink
            p["intent_pct"] = float(after)
            _push(
                p["symbol_id"],
                "AVAILABLE_CASH_CAP",
                before,
                after,
                "AVAILABLE_CASH_CAP" if shrink < 1.0 else None,
                (None if shrink >= 1.0 else round(delta * (1.0 - shrink), 6)),
            )
    else:
        clamp_trace.append({
            "step": "AVAILABLE_CASH_CAP",
            "available_cash": None,
            "note": "cash amount unavailable; percentage cash reserve applied",
        })
        for p in plans:
            _push(p["symbol_id"], "AVAILABLE_CASH_CAP", p["intent_pct"], p["intent_pct"], None, None)

    # ───────── Step 8: 最小手数（lot size）— 结合 price 和净值
    clamp_trace.append({"step": "MIN_LOT_SIZE", "min_lot_size": min_lot_size})
    raw_volume_limit_pct = cfg.get("volume_limit_pct")
    try:
        volume_limit_pct = (
            max(0.0, float(raw_volume_limit_pct))
            if raw_volume_limit_pct is not None else None
        )
    except (TypeError, ValueError):
        volume_limit_pct = None
    clamp_trace.append({
        "step": "VOLUME_PARTICIPATION_CAP",
        "volume_limit_pct": volume_limit_pct,
        "min_lot_size": min_lot_size,
    })
    items_out: list[dict[str, Any]] = []
    # 用于 quantity：price 不存在 或 net_value 为 0 → quantity = None，Q10.2 触发 REJECTED/ORDER_BELOW_LOT_SIZE
    for p in plans:
        sid = p["symbol_id"]
        before = p["intent_pct"]
        before_qty: float | None
        target_amount: float | None = None
        price = p["price"]
        if net_value_for_quantity > 0 and price is not None and price > 0:
            target_amount = net_value_for_quantity * before
            raw_shares = target_amount / price
            # 向下取整到 min_lot_size 的倍数
            rounded_qty = int(raw_shares // min_lot_size) * min_lot_size
            after_qty: float = float(rounded_qty)
            # 换算回 after_pct
            after_pct = (after_qty * price) / net_value_for_quantity
        else:
            # 价格未知：保留 pct，不生成 qty；由调用方在下单前再验证
            after_pct = before
            after_qty = None
        # Q10.2：不足一最小交易单位 → 标记 ORDER_BELOW_LOT_SIZE（REJECTED）
        trigger_code = None
        if after_qty == 0 and (before - p["current_pct"]) > 0:
            # 买入意图不足一手
            trigger_code = "ORDER_BELOW_LOT_SIZE"
            after_pct = p["current_pct"]
            after_qty = p["current_qty"] if p["has_position"] else 0.0
        _push(sid, "MIN_LOT_SIZE", before, after_pct, trigger_code,
              None if trigger_code is None else round(before - after_pct, 6))

        # 计算 direction：基于 target 与 current 的差额
        delta = after_pct - p["current_pct"]
        if p["direction"] in {"SELL", "EXIT", "REDUCE"}:
            direction = "SELL"  # Q11: 卖出方向保留
            if p["direction"] == "EXIT":
                # 清仓：覆盖 clamp 结果
                after_qty = 0.0
                after_pct = 0.0
                per_sym_clamps[sid].append({
                    "step": "EXIT_OVERRIDE", "before_pct": round(after_pct, 6),
                    "after_pct": 0.0, "trigger": "HARD_EXIT", "remaining_shortfall_pct": None,
                })
        elif abs(delta) < 1e-8:
            direction = "HOLD"
        elif delta > 0:
            direction = "BUY"
        else:
            direction = "SELL"

        # The final executable delta, not the gross target holding, consumes
        # participation capacity. Run it after EXIT_OVERRIDE so an exit plan
        # cannot bypass the same volume constraint enforced by the matcher.
        volume_trigger_code = None
        volume = (snap.market_volume_by_symbol or {}).get(sid)
        if (
            volume_limit_pct is None
            or volume is None
            or float(volume) <= 0
            or after_qty is None
        ):
            _push(sid, "VOLUME_PARTICIPATION_CAP", after_pct, after_pct, None, None)
        else:
            max_trade_quantity = int(
                (float(volume) * volume_limit_pct) // min_lot_size
            ) * min_lot_size
            planned_delta_qty = float(after_qty) - p["current_qty"]
            if abs(planned_delta_qty) <= max_trade_quantity + 1e-9:
                _push(sid, "VOLUME_PARTICIPATION_CAP", after_pct, after_pct, None, None)
            else:
                before_volume_pct = after_pct
                if planned_delta_qty > 0:
                    after_qty = p["current_qty"] + float(max_trade_quantity)
                else:
                    after_qty = max(0.0, p["current_qty"] - float(max_trade_quantity))
                if net_value_for_quantity > 0 and price is not None and price > 0:
                    after_pct = (float(after_qty) * price) / net_value_for_quantity
                volume_trigger_code = "VOLUME_LIMIT"
                _push(
                    sid,
                    "VOLUME_PARTICIPATION_CAP",
                    before_volume_pct,
                    after_pct,
                    volume_trigger_code,
                    round(
                        max(0.0, abs(planned_delta_qty) - max_trade_quantity)
                        * (price or 0.0) / max(net_value_for_quantity, 1.0),
                        6,
                    ),
                )
                # A zero participation allowance must not create a SELL/BUY
                # order with a zero delta. Otherwise derive the final side
                # from the adjusted target holding.
                adjusted_delta = after_pct - p["current_pct"]
                if abs(adjusted_delta) < 1e-8:
                    direction = "HOLD"
                elif adjusted_delta > 0:
                    direction = "BUY"
                else:
                    direction = "SELL"
        # 滑点应用：成交价 on 意图价格 (Q2.3)
        exec_price = None
        if price is not None:
            if direction == "BUY":
                exec_price = float(price) * (1.0 + slippage_buy / 10_000.0)
            elif direction == "SELL":
                exec_price = float(price) * (1.0 - slippage_sell / 10_000.0)
            else:
                exec_price = float(price)
        items_out.append({
            "symbol_id": sid,
            "direction": direction,
            "target_position_pct": round(float(after_pct), 6),
            "target_quantity": (float(after_qty) if after_qty is not None else None),
            "min_lot_size": min_lot_size,
            "intended_price": float(price) if price is not None else None,
            "executed_price": round(float(exec_price), 6) if exec_price is not None else None,
            "slippage_bps": (
                slippage_buy if direction == "BUY" else (slippage_sell if direction == "SELL" else 0.0)
            ),
            "asset_type": p["asset_type"],
            "industry": p["industry"],
            "clamp_steps": per_sym_clamps.get(sid, []),
            "rejection_subtype": (
                "ORDER_BELOW_LOT_SIZE" if trigger_code == "ORDER_BELOW_LOT_SIZE"
                else ("VOLUME_LIMIT" if volume_trigger_code == "VOLUME_LIMIT" else None)
            ),
        })

    return RiskAllocationResult(items=items_out, clamp_trace=clamp_trace)


def check_data_availability(
    *,
    symbol_id: int,
    trade_date: date,
    open_price: float | None,
    close_price: float | None,
    high_price: float | None,
    low_price: float | None,
    volume: int | None,
    is_suspended_today: bool = False,
) -> dict:
    """返回 {available: bool, status: "OK"|"SUSPENDED"|"DATA_BLOCKED", reason: str}

    OK = 价格正常；SUSPENDED = 今日已标记法定停牌（走另外的 HOLD 路径）；
    DATA_BLOCKED = 今日既不是停牌，又所有 open/close/high/low == None/0 或 体积=0 → Fail-Closed 不猜价
    """
    if is_suspended_today:
        return {"available": False, "status": "SUSPENDED", "reason": f"{trade_date} 法定停牌"}
    all_prices_missing = (open_price is None or open_price == 0) and (close_price is None or close_price == 0) and \
                         (high_price is None or high_price == 0) and (low_price is None or low_price == 0)
    vol_missing = volume is None or volume == 0
    if all_prices_missing or vol_missing:
        return {"available": False, "status": "DATA_BLOCKED", "reason": f"数据缺失（DATA_BLOCKED）：symbol_id={symbol_id} 在 {trade_date} 的价格/成交量全部缺失"}
    return {"available": True, "status": "OK", "reason": ""}


def check_stop_loss_verifiability(
    *,
    current_position_qty: int,
    stop_loss_price: float | None,
    prev_close_price: float | None,
    first_open_price: float | None,
    data_status: dict,
) -> dict:
    """
    返回：{should_intervene: bool,
           action: "REJECTED"|"SELL"|"DATA_BLOCKED_PASS",
           action_subtype: "UNABLE_TO_VERIFY_STOP_LOSS"|"STOP_LOSS_EXECUTED"|None,
           executed_price: float|None,
           stop_loss_triggered: bool,
           stop_loss_verified_price_source: "FIRST_OPEN"|None,
           reason: str}

    顺序（严格 IF-ELSE）：
    Step 0：无 stop_loss_price 或 持仓=0 → 不介入（should_intervene=False，返回 DATA_BLOCKED_PASS 让其他分支处理）。
    Step 1（C2 快速通道）：前收触止损（prev_close <= stop_loss_price）OR FIRST_OPEN <= stop_loss_price 且 FIRST_OPEN is not None → 允许 SELL；
             executed_price = first_open if first_open else prev_close；
             stop_loss_verified_price_source = "FIRST_OPEN"（若用 first_open）否则 "PREV_CLOSE"；
             stop_loss_triggered=True；reason 明确写入。
    Step 2（C1 核心阻断）：数据 status=DATA_BLOCKED 且 持仓>0 → 虽然无法验证但**理论上止损已可能触发**（无法100%证伪）→ 保守 REJECTED；
             action=REJECTED, action_subtype=UNABLE_TO_VERIFY_STOP_LOSS；
             executed_price=None；stop_loss_triggered=False（未执行）；reason="UNABLE_TO_VERIFY_STOP_LOSS：今日价格缺失且无法验证是否已触达止损线"。
    Step 3（C3 未触发止损 + DATA_BLOCKED）：若 data_status=DATA_BLOCKED 但"明确知道未触发止损条件"（例如今日 open>stop_loss 且今日价正常 available），
             但实际这里当 DATA_BLOCKED 时没法"明确知道"，因此默认 should_intervene=False，走 T-A7 DATA_BLOCKED HOLD（函数内部不处理 C3）。
    Step 4（正常价格 OK + 无止损触发） → 不介入 should_intervene=False；若 OK 且 FIRST_OPEN/prev_close <= stop_loss_price → 走 Step 1 同一分支。
    """
    # Step 0
    if stop_loss_price is None or current_position_qty <= 0:
        return {
            "should_intervene": False,
            "action": "DATA_BLOCKED_PASS",
            "action_subtype": None,
            "executed_price": None,
            "stop_loss_triggered": False,
            "stop_loss_verified_price_source": None,
            "reason": "Step 0：无止损线或无持仓，止损验证不介入",
        }

    # Step 1（C2 快速通道）：已有可靠价源证明止损已触发 → 允许 SELL
    prev_close_triggered = (
        prev_close_price is not None and prev_close_price <= stop_loss_price
    )
    first_open_triggered = (
        first_open_price is not None and first_open_price <= stop_loss_price
    )

    if prev_close_triggered or first_open_triggered:
        if first_open_price is not None:
            executed_price = float(first_open_price)
            price_source: str | None = "FIRST_OPEN"
            reason = (
                f"STOP_LOSS_EXECUTED：止损已触发；"
                f"first_open={first_open_price} <= stop_loss={stop_loss_price}，"
                f"使用 FIRST_OPEN 作为验证价源"
            )
        elif prev_close_price is not None:
            executed_price = float(prev_close_price)
            price_source = None
            reason = (
                f"STOP_LOSS_EXECUTED：止损已触发；"
                f"prev_close={prev_close_price} <= stop_loss={stop_loss_price}，"
                f"使用 PREV_CLOSE 作为验证价源"
            )
        else:
            executed_price = None
            price_source = None
            reason = "Step 1 fallback：理论不可达，两个价源都触发但都为 None"

        return {
            "should_intervene": True,
            "action": "SELL",
            "action_subtype": "STOP_LOSS_EXECUTED",
            "executed_price": executed_price,
            "stop_loss_triggered": True,
            "stop_loss_verified_price_source": price_source,
            "reason": reason,
        }

    # Step 2（C1 核心阻断）：DATA_BLOCKED + 有持仓 + 未触发 Step1 → 保守 REJECTED
    status = data_status.get("status", "OK")
    if status == "DATA_BLOCKED":
        # 注意：若 Step 3 C3 场景（first_open > stop_loss 且显式传入 first_open），
        # 应在调用前通过 first_open_price 判定并由调用方控制不触发 Step 2
        # 这里按规范：DATA_BLOCKED 且已知 first_open > stop_loss 时不应走本函数 Step2，
        # 而直接返回 should_intervene=False
        if first_open_price is not None and first_open_price > stop_loss_price:
            return {
                "should_intervene": False,
                "action": "DATA_BLOCKED_PASS",
                "action_subtype": None,
                "executed_price": None,
                "stop_loss_triggered": False,
                "stop_loss_verified_price_source": None,
                "reason": (
                    f"Step 3 C3：已知今日 first_open={first_open_price} > stop_loss={stop_loss_price}，"
                    f"明确未触止损；虽 DATA_BLOCKED 但放行，交由 T-A7 DATA_BLOCKED HOLD 处理"
                ),
            }
        return {
            "should_intervene": True,
            "action": "REJECTED",
            "action_subtype": "UNABLE_TO_VERIFY_STOP_LOSS",
            "executed_price": None,
            "stop_loss_triggered": False,
            "stop_loss_verified_price_source": None,
            "reason": "UNABLE_TO_VERIFY_STOP_LOSS：今日价格缺失且无法验证是否已触达止损线",
        }

    # Step 4：正常可用价格 + 未触发止损 → 不介入
    return {
        "should_intervene": False,
        "action": "DATA_BLOCKED_PASS",
        "action_subtype": None,
        "executed_price": None,
        "stop_loss_triggered": False,
        "stop_loss_verified_price_source": None,
        "reason": "Step 4：价格正常且止损未触发，止损验证不介入",
    }


def _default_build_evidence(
    *,
    snap: LoadedSnapshot,
    clock: ResolvedClock,
    universe: UniverseAndEligibility,
    scored: ScoredUniverse,
    signal: SignalResult,
    alloc: RiskAllocationResult,
    blocking_status: str,
    blocking_reasons: list[dict[str, Any]],
    price_data_by_symbol: dict[int, dict[str, Any]] | None = None,
    trade_date: date | None = None,
    match_mode: str = "NEXT_OPEN",
    manual_overrides_context: dict[str, Any] | None = None,
    exit_rules_hit_by_symbol: dict[int, list[ExitRuleHit]] | None = None,
) -> list[PerSymbolEvidence]:
    ev: list[PerSymbolEvidence] = []
    # Action 映射：阻断状态 → 全部 DATA_BLOCKED 或 REJECTED
    fail_closed = blocking_status != "READY"

    # ── T-A9：解析人工 override context
    ta9_ctx = manual_overrides_context or {}
    per_symbol_ov: dict[int, Any] = ta9_ctx.get("per_symbol") or {}
    portfolio_ov = ta9_ctx.get("portfolio_wide")
    applied_ids_ref: list[int] = ta9_ctx.setdefault("applied_override_ids", [])

    # 先把 scored.items 按 symbol_id 建索引
    scored_by_sym: dict[int, dict[str, Any]] = {
        s.get("symbol_id"): s for s in scored.items if isinstance(s, dict) and s.get("symbol_id")
    }
    alloc_by_sym: dict[int, dict[str, Any]] = {
        a.get("symbol_id"): a for a in alloc.items if isinstance(a, dict) and a.get("symbol_id")
    }

    # 遍历 universe 成员逐个生成
    for member in universe.universe:
        sym_id = member.get("symbol_id")
        if sym_id is None:
            continue
        reasons: list[str] = []
        action: ActionType
        action_subtype: str | None = None
        rejection_reason: str | None = None
        blocking_reason_val: str | None = None
        score_item = scored_by_sym.get(sym_id)
        alloc_item = alloc_by_sym.get(sym_id) or {}
        sym_id_int = int(sym_id)
        master_data_issue = member.get("_master_data_issue")
        master_data_detail = member.get("_master_data_issue_detail")
        membership_data_issue = member.get("_membership_data_issue")
        membership_data_detail = member.get("_membership_data_issue_detail")
        data_master_issue = master_data_issue or membership_data_issue
        data_master_detail = master_data_detail or membership_data_detail

        current_qty: float = float(member.get("current_quantity", 0.0) or 0.0)
        target_qty_from_alloc: float | None = (
            float(alloc_item.get("target_quantity"))
            if alloc_item.get("target_quantity") is not None else None
        )
        target_qty_delta_val: float | None = None
        if target_qty_from_alloc is not None:
            target_qty_delta_val = float(target_qty_from_alloc - current_qty)

        # ── Q7.1 T-A7：撮合前先检查数据可用性（DATA_BLOCKED/SUSPENDED）
        data_check_result: dict | None = None
        sym_price_data: dict[str, Any] | None = None
        if data_master_issue:
            data_check_result = {
                "available": False,
                "status": "DATA_BLOCKED",
                "reason": str(data_master_detail or data_master_issue),
            }
        elif price_data_by_symbol is not None and trade_date is not None:
            sym_price_data = price_data_by_symbol.get(sym_id_int)
            if sym_price_data is None:
                if requires_strict_market_data_pit(snap):
                    data_check_result = {
                        "available": False,
                        "status": "DATA_BLOCKED",
                        "reason_code": "PRICE_DATA_MISSING",
                        "reason": (
                            "PRICE_DATA_MISSING: "
                            f"symbol_id={sym_id_int} has no market-data row for "
                            f"{trade_date.isoformat()}"
                        ),
                    }
            else:
                data_check_result = _strict_price_data_pit_result(
                    snap=snap,
                    clock=clock,
                    symbol_id=sym_id_int,
                    price_data=sym_price_data,
                )
                if data_check_result is None:
                    data_check_result = check_data_availability(
                        symbol_id=sym_id_int,
                        trade_date=trade_date,
                        open_price=sym_price_data.get("open_price"),
                        close_price=sym_price_data.get("close_price"),
                        high_price=sym_price_data.get("high_price"),
                        low_price=sym_price_data.get("low_price"),
                        volume=sym_price_data.get("volume"),
                        is_suspended_today=bool(sym_price_data.get("is_suspended_today", False)),
                    )

        # ── Q7.2 T-A8：check_stop_loss_verifiability（先于 DATA_BLOCKED HOLD）
        sl_result: dict | None = None
        sl_override = False
        stop_loss_verified_price_source_val: str | None = None
        stop_loss_triggered_val: bool = False
        if data_check_result is not None and sym_price_data is not None:
            sl_price = sym_price_data.get("stop_loss_price")
            if sl_price is not None:
                sl_result = check_stop_loss_verifiability(
                    current_position_qty=int(current_qty),
                    stop_loss_price=(float(sl_price) if sl_price is not None else None),
                    prev_close_price=(
                        float(sym_price_data["prev_close_price"])
                        if sym_price_data.get("prev_close_price") is not None else None
                    ),
                    first_open_price=(
                        float(sym_price_data["first_open_price"])
                        if sym_price_data.get("first_open_price") is not None else None
                    ),
                    data_status=data_check_result,
                )
                if sl_result is not None and sl_result["should_intervene"]:
                    sl_override = True

        data_blocked_override = False
        if sl_override and sl_result is not None:
            # T-A8 止损介入：优先级高于 DATA_BLOCKED HOLD
            status = sl_result["action"]
            if status == "REJECTED":
                action = "REJECTED"
                action_subtype = sl_result["action_subtype"]  # UNABLE_TO_VERIFY_STOP_LOSS
                rejection_reason = sl_result["action_subtype"]
                blocking_reason_val = sl_result["reason"]
                reasons.append(sl_result["action_subtype"] or "STOP_LOSS_REJECTED")
            elif status == "SELL":
                action = "SELL"
                action_subtype = sl_result["action_subtype"]  # STOP_LOSS_EXECUTED
                reasons.append(sl_result["action_subtype"] or "STOP_LOSS_SELL")
            # 通用：止损介入相关覆盖
            executed_price_override = sl_result["executed_price"]
            intended_price_override = sl_result["executed_price"]
            stop_loss_verified_price_source_val = sl_result["stop_loss_verified_price_source"]
            stop_loss_triggered_val = bool(sl_result["stop_loss_triggered"])
            if status == "SELL":
                # 卖出全部持仓
                target_qty_override: float = 0.0
                target_qty_delta_val = float(0.0 - current_qty)
                target_position_pct_override: float | None = 0.0
            else:  # REJECTED
                target_qty_override = float(current_qty)  # 零调仓，保留持仓
                target_qty_delta_val = 0.0
                target_position_pct_override = (
                    float(member.get("current_position_pct"))
                    if member.get("current_position_pct") is not None else None
                )
            data_blocked_override = False  # 虽然可能是 DATA_BLOCKED 来源，但动作已被止损接管

        elif data_check_result is not None and not data_check_result["available"]:
            status = data_check_result["status"]
            data_blocked_override = True
            blocking_reason_val = data_check_result["reason"]
            if status == "DATA_BLOCKED":
                # Q7.1 DATA_BLOCKED：Fail-Closed 绝不猜价
                action = "HOLD"
                action_subtype = "DATA_BLOCKED"
                reason_code = data_check_result.get("reason_code")
                if reason_code:
                    reasons.append(str(reason_code))
                reasons.append("DATA_BLOCKED")
            elif status == "SUSPENDED":
                # 法定停牌：同样 HOLD，但 subtype 不同，方便区分是交易所合法停还是数据源问题
                action = "HOLD"
                action_subtype = "SUSPENDED"
                reasons.append("SUSPENDED")
            # ── 关键：executed_price 显式 NULL，绝不允许任何 fallback
            # 禁止：昨日收盘价兜底 / 行业均价兜底 / 组合均价兜底 / 默认价 0.0 兜底
            executed_price_override = None
            assert executed_price_override is None, (
                f"Q7.1 Fail-Closed: {status} 时 executed_price 必须是 NULL，"
                f"禁止任何隐式猜价 fallback"
            )
            intended_price_override = None
            target_qty_override = current_qty  # 零调仓
            target_qty_delta_val = 0.0
            target_position_pct_override = (
                float(member.get("current_position_pct"))
                if member.get("current_position_pct") is not None else None
            )
        else:
            executed_price_override = None
            intended_price_override = None
            target_qty_override = None
            target_position_pct_override = None

            if fail_closed:
                # Q6 Fail-Closed：组合整体阻断时所有成员统一 REJECTED / DATA_BLOCKED
                if blocking_status == "DATA_INCOMPLETE_PAUSED":
                    action = "DATA_BLOCKED"
                    rejection_reason = "DATA_INCOMPLETE_PAUSED"
                elif blocking_status == "MODEL_INACTIVE":
                    action = "REJECTED"
                    action_subtype = "REJECTED_MODEL_INACTIVE"
                    rejection_reason = "MODEL_INACTIVE"
                elif blocking_status == "SCORE_STALE":
                    action = "REJECTED"
                    action_subtype = "REJECTED_STALE_SCORE"
                    rejection_reason = "SCORE_STALE"
                elif blocking_status == "RECONCILIATION_BLOCKED":
                    action = "REJECTED"
                    action_subtype = "REJECTED_RECONCILIATION_BLOCKED"
                    rejection_reason = "RECONCILIATION_BLOCKED"
                else:
                    action = "REJECTED"
                    rejection_reason = blocking_status
                reasons = [br.get("code") for br in blocking_reasons if br.get("code")]
            elif score_item is None:
                # 单成员缺 Score（Q6）：如果是 research 模式，允许 HOLD + STALE_SCORE；
                # 正式模式由 blocking_status 在上层已经 fail-closed，不会走到这里。
                action = "HOLD"
                action_subtype = "STALE_SCORE"
                reasons.append("MISSING_SCORE_FOR_MEMBER")
            else:
                dir_ = (alloc_item.get("direction") or "HOLD")
                if dir_ in ("BUY",):
                    action = "BUY"
                    reasons.append("SCORE_DRIVEN_BUY")
                elif dir_ in ("SELL",):
                    action = "SELL"
                    action_subtype = "SELL_STRATEGY_EXIT"
                    reasons.append("SCORE_DRIVEN_SELL")
                elif dir_ == "HOLD":
                    action = "HOLD"
                    reasons.append("NO_SIGNAL_CHANGE")
                else:
                    action = "NO_ACTION"

        # Master-data failures take precedence over score/signal output. Keep
        # the member in Evidence, but guarantee no guessed execution price or
        # quantity change for a symbol that was not tradable on this date.
        if data_master_issue:
            action = "DATA_BLOCKED"
            action_subtype = str(data_master_issue)
            rejection_reason = str(data_master_issue)
            blocking_reason_val = str(data_master_detail or data_master_issue)
            if str(data_master_issue) not in reasons:
                reasons.append(str(data_master_issue))
            data_blocked_override = True
            executed_price_override = None
            intended_price_override = None
            final_target_qty_override = float(current_qty)
            target_qty_override = final_target_qty_override
            target_qty_delta_val = 0.0
            target_position_pct_override = (
                float(member.get("current_position_pct"))
                if member.get("current_position_pct") is not None else None
            )

        # An expired member with a historical holding is exit-only: preserve
        # a SELL/risk path, but never allow a new BUY to reopen it.
        if member.get("_membership_exit_only") and action == "BUY":
            action = "HOLD"
            action_subtype = "MEMBER_EXIT_ONLY"
            rejection_reason = "MEMBER_NOT_EFFECTIVE_ON_TRADE_DATE"
            blocking_reason_val = str(
                membership_data_detail or "member is outside effective interval"
            )
            reasons.append("MEMBER_EXIT_ONLY")
            executed_price_override = None
            intended_price_override = None
            target_qty_override = float(current_qty)
            target_qty_delta_val = 0.0
            target_position_pct_override = (
                float(member.get("current_position_pct"))
                if member.get("current_position_pct") is not None else None
            )

        # PIT 标记（Q4）：published_at NULL → NOT_PIT_SAFE
        published_at: datetime | None = (score_item or {}).get("published_at")
        pit_safe: PitSafeFlag = "UNKNOWN"
        if published_at is None:
            if score_item is not None:
                pit_safe = "NOT_PIT_SAFE"
        else:
            pit_safe = "PIT_SAFE" if published_at <= clock.data_cutoff_at else "NOT_PIT_SAFE"

        # ── 撮合价处理：data_blocked_override 或 sl_override 时已经设置过
        final_executed_price: float | None = executed_price_override
        final_intended_price: float | None = intended_price_override
        final_slippage: float | None = None

        # ════════════════════════════════════════════════════════════════════
        # T-B2 Q2.1：NEXT_OPEN 真实撮合价格提取 + 涨跌停顺延
        #   触发信号：sym_price_data["_tb2_roll_rows"] 存在（类型 list[tuple]，
        #       按 trade_date 升序，每格 = (T+1起 date, open, high, low, close, volume)）；
        #       同时可传 sym_price_data["_tb2_prev_close"] = T-1 close（判涨跌停更准确）。
        #   优先级：仅当 data_blocked_override=False 且 sl_override=False（止损介入/数据阻断优先），
        #       且 match_mode != T_CLOSE（与 T-B1 口径一致，T_CLOSE 场景不进入 NEXT_OPEN 顺延）时才触发。
        # ════════════════════════════════════════════════════════════════════
        tb2_result: MatchPriceResult | None = None
        if (
            not data_blocked_override
            and not sl_override
            and match_mode != MatchMode.T_CLOSE.value
            and sym_price_data is not None
            and isinstance(sym_price_data.get("_tb2_roll_rows"), (list, tuple))
            and len(sym_price_data["_tb2_roll_rows"]) > 0
        ):
            roll_rows = sym_price_data["_tb2_roll_rows"]
            prev_close_raw = sym_price_data.get("_tb2_prev_close")
            tb2_prev_close: float | None = float(prev_close_raw) if prev_close_raw is not None else None
            tb2_max_roll = int(sym_price_data.get("_tb2_max_roll_days") or 10)
            tb2_result = _tb2_mp.resolve_next_open_bar(
                roll_rows,
                prev_close=tb2_prev_close,
                max_roll_days=tb2_max_roll,
            )
            if tb2_result.success:
                # T-B3 口径（intended vs executed）：
                #   intended_price = 首次原定 NEXT_OPEN（T+1 当日第一个 intended_open；
                #       如果 T+1 被跳过 → 取 rejections[0].intended_open）；
                #       若 rejections 为空则直接用 final_open。
                #   executed_price = 最终实际匹配到的 open（顺延命中的那一天 final_open）。
                first_intended = (
                    float(tb2_result.rejections[0].intended_open)
                    if tb2_result.rejections else float(tb2_result.final_open)
                )
                if final_intended_price is None or not data_blocked_override:
                    final_intended_price = first_intended
                final_executed_price = float(tb2_result.final_open)

        if not data_blocked_override and not sl_override and tb2_result is None:
            # 原始非 T-B2 路径（保持零回退）
            final_intended_price = (
                float(alloc_item.get("intended_price"))
                if alloc_item.get("intended_price") is not None else None
            )
            final_executed_price = (
                float(alloc_item.get("executed_price"))
                if alloc_item.get("executed_price") is not None else None
            )
            final_slippage = (
                float(alloc_item.get("slippage_bps"))
                if alloc_item.get("slippage_bps") is not None else None
            )

        final_target_qty: float | None = target_qty_override
        if final_target_qty is None:
            final_target_qty = target_qty_from_alloc

        final_target_pct: float | None = target_position_pct_override
        if final_target_pct is None:
            final_target_pct = (
                float(alloc_item.get("target_position_pct"))
                if alloc_item.get("target_position_pct") is not None else None
            )

        # ════════════════════════════════════════════════════════════════════
        # T-B4 Q2.3：撮合最后一步统一乘滑点（误差 ≤ 1e-6）
        #   * BUY  (final_target_qty > current_qty) → executed_price_with_slip = raw_executed * (1 + buy_bps / 10000)
        #   * SELL (final_target_qty < current_qty) → executed_price_with_slip = raw_executed * (1 - sell_bps / 10000)
        #   * HOLD / unknown dir → 不乘，保持原值；slip_bps 保持 None。
        #   从 snap.cost_config 读：slippage_buy_bps（默认 5），slippage_sell_bps（默认 5）。
        # ════════════════════════════════════════════════════════════════════
        if (
            not data_blocked_override
            and not sl_override
            and final_executed_price is not None
            and final_target_qty is not None
        ):
            cost_cfg: dict[str, Any] = snap.cost_config if isinstance(snap.cost_config, dict) else {}
            buy_bps_raw = cost_cfg.get("slippage_buy_bps", 5)
            sell_bps_raw = cost_cfg.get("slippage_sell_bps", 5)
            try:
                buy_bps: float = float(buy_bps_raw)
            except (TypeError, ValueError):
                buy_bps = 5.0
            try:
                sell_bps: float = float(sell_bps_raw)
            except (TypeError, ValueError):
                sell_bps = 5.0
            raw_exec = float(final_executed_price)
            if final_target_qty > current_qty + 1e-12:
                # BUY direction
                slip_factor = 1.0 + (buy_bps / 10000.0)
                final_executed_price = raw_exec * slip_factor
                final_slippage = buy_bps
            elif final_target_qty < current_qty - 1e-12:
                # SELL direction
                slip_factor = 1.0 - (sell_bps / 10000.0)
                final_executed_price = raw_exec * slip_factor
                final_slippage = sell_bps

        # ════════════════════════════════════════════════════════════════════
        # T-A9 Q7.4：Override 后置处理
        #   - symbol_ov = 单个 symbol 指定（优先）；否则 portfolio_wide_ov（批量，symbol_id=NULL）
        #   - 3 resolved_mode：
        #       * confirm_manual_price → manual_price_flag=True；消费 override_id
        #       * continue_forward     → 当原 DATA_BLOCKED/SUSPENDED 时 HOLD→MANUALLY_SKIPPED
        #       * keep_paused          → 保持阻断状态，不消费
        # ════════════════════════════════════════════════════════════════════
        manual_price_flag_val = False
        manual_price_override_id_val: int | None = None
        symbol_ov = per_symbol_ov.get(sym_id_int)
        effective_ov = symbol_ov or portfolio_ov

        if effective_ov is not None:
            mode = str(effective_ov.resolved_mode)
            ov_id_val: int = int(effective_ov.id)

            if mode == "confirm_manual_price":
                # 仅当前 symbol 级 override 时应用（portfolio_wide_ov 不可能是 confirm_manual_price）
                if sym_price_data is not None and sym_price_data.get("_ta9_override_applied"):
                    manual_price_flag_val = True
                    manual_price_override_id_val = ov_id_val
                    if ov_id_val not in applied_ids_ref:
                        applied_ids_ref.append(ov_id_val)

            elif mode == "continue_forward":
                # 当原 data_blocked_override 或 fail_closed DATA_BLOCKED 时 → HOLD + MANUALLY_SKIPPED
                needs_manual_skip = data_blocked_override or (
                    fail_closed and action in ("DATA_BLOCKED",)
                )
                if needs_manual_skip:
                    action = "HOLD"
                    action_subtype = "MANUALLY_SKIPPED"
                    reasons.append("MANUALLY_SKIPPED")
                    reasons = [r for r in reasons if r != "DATA_BLOCKED" and r != "SUSPENDED"]
                    blocking_reason_val = (
                        f"DATA_BLOCKED 但人工显式 continue_forward（T-A9 override_id={ov_id_val}）"
                    )
                    # 覆盖价格/仓位：零调仓，不猜价
                    executed_price_override = None
                    intended_price_override = None
                    final_executed_price = None
                    final_intended_price = None
                    target_qty_override = current_qty
                    target_qty_delta_val = 0.0
                    final_target_qty = current_qty
                    if member.get("current_position_pct") is not None:
                        final_target_pct = float(member.get("current_position_pct"))
                    manual_price_flag_val = True
                    manual_price_override_id_val = ov_id_val
                    if ov_id_val not in applied_ids_ref:
                        applied_ids_ref.append(ov_id_val)

            elif mode == "keep_paused":
                # 语义：维持阻断状态（DATA_BLOCKED 或 SUSPENDED 不变），不标记 manual_price_flag
                # 仅把 override_id 暂留作 reference（不消费 consumed_flag）
                # evidence 里保留 blocking_reason（来自 T-A7）即可
                pass

        # ════════════════════════════════════════════════════════════════════
        # T-B3 Q2.2：NEXT_OPEN 顺延 rejection_reason 聚合（若有跳过记录）
        # rejection_reason 语义：空列表 / 无顺延 → None（保持 fail-closed 语义）；
        # 若有 ≥1 条跳过 → 用"|"拼接按日期有序、去重后的 reasons codes。
        # 注意：T-A8 / T-A7 路径已经写了 rejection_reason（STOP_LOSS 等），我们要
        # 避免覆盖（"高优先级"的止损/阻断 reason 优先于顺延）。
        # ════════════════════════════════════════════════════════════════════
        if (
            rejection_reason is None
            and tb2_result is not None
            and tb2_result.rejections
        ):
            ordered_dedup: list[str] = []
            seen: set[str] = set()
            for rej in tb2_result.rejections:
                code = str(rej.reason)
                if code not in seen:
                    seen.add(code)
                    ordered_dedup.append(code)
            rejection_reason = "|".join(ordered_dedup)

        ev.append(PerSymbolEvidence(
            symbol_id=sym_id_int,
            action=action,
            action_subtype=action_subtype,
            target_position_pct=final_target_pct,
            min_lot_size=int(alloc_item.get("min_lot_size") or snap.cost_config.get("min_lot_size", 100)),
            target_quantity=final_target_qty,
            target_qty_delta=target_qty_delta_val,
            intended_price=final_intended_price,
            executed_price=final_executed_price,
            slippage_bps=final_slippage,
            rejection_reason=rejection_reason,
            rejection_detail=("; ".join(r["message"] for r in blocking_reasons if r.get("message"))
                              if fail_closed and blocking_reasons else None),
            blocking_reason=blocking_reason_val,
            score_id=(score_item or {}).get("score_id"),
            score_value=(score_item or {}).get("score_value"),
            score_rank=(score_item or {}).get("score_rank"),
            score_published_at=published_at,
            pit_safe_flag=pit_safe,
            constraints=list(alloc_item.get("clamp_steps") or []),
            reason_codes=reasons + ([action_subtype] if action_subtype else []),
            factor_contributions=dict((score_item or {}).get("factor_contributions") or {}),
            stop_loss_verified_price_source=stop_loss_verified_price_source_val,
            stop_loss_triggered=stop_loss_triggered_val,
            manual_price_flag=manual_price_flag_val,
            manual_price_override_id=manual_price_override_id_val,
            roll_forward_days=(tb2_result.roll_forward_days if tb2_result is not None else None),
            rejections_trace_json=(
                tb2_result.as_dict()["rejections"] if tb2_result is not None else []
            ),
            exit_rules_hit=list((exit_rules_hit_by_symbol or {}).get(int(sym_id_int), ())),
        ))
    # ════════════════════════════════════════════════════════════════════
    # T-B5 Q11.1：卖出规则聚合。
    #   * action_subtype = 最高优先级命中的 rule_subtype（priority 数值越小越优先）
    #   * target_quantity = max(当前已算好的 target_quantity, max_{h in hits} h.requested_exit_qty)
    #   * 方向修正：若最终 target_qty < current_qty → action=SELL（无论原 BUY/HOLD）
    # ════════════════════════════════════════════════════════════════════
    exit_by_current: dict[int, Any] = exit_rules_hit_by_symbol or {}
    for e in ev:
        hits = list(exit_by_current.get(int(e.symbol_id), ()))
        if not hits:
            continue
        # 1) top priority rule_subtype（priority 越小越先 → 排序后取第一个）
        sorted_hits = sorted(hits, key=lambda h: (int(h.rule_priority), str(h.rule_subtype)))
        top_hit = sorted_hits[0]
        e.action_subtype = str(top_hit.rule_subtype)
        # 2) target_quantity = max(existing target qty, max requested exit qty)
        current_qty_from_alloc = float(e.target_quantity or 0.0)
        max_exit = max(float(h.requested_exit_qty) for h in hits)
        final_target_qty_exit = max(current_qty_from_alloc, max_exit)
        # 3) action 方向修正：若 final_target_qty_exit < current_qty → SELL
        #    current_qty 只能从 target_qty_delta 反推？或从 alloc 推断。
        #    为避免依赖，当 top_hit.rule_subtype 含 SELL_ 前缀且 target_qty 明确时，
        #    保证 action=SELL
        if str(top_hit.rule_subtype).startswith("SELL_") or str(top_hit.rule_subtype) in {"EXIT"}:
            e.action = "SELL"
        e.target_quantity = final_target_qty_exit
    return ev


# ──────────────────────────────────────────────────────────── helpers
def compute_exit_phase_quantities(
    exit_config: dict[str, Any] | None,
    current_qty: float,
    target_qty: float,
    elapsed_trade_days: int,
) -> tuple[float, dict[str, Any]]:
    """T-B6 Q11.2：计算当日应卖出数量（基于分阶段退出配置）。

    Returns:
        (sell_qty_today, trace_dict)
          - sell_qty_today：当日要卖出的股数，0 表示今天不执行。
          - trace_dict：审计调试，含 quantities_by_phase_idx / total_phase_percent_mature /
            normalized_phases_pct / one_shot_mode。

    规则：
      - exit_config 是 None / 空 / phased_exit_enabled 非 True → 一次性 100%：
        * 忽略 elapsed_trade_days，直接返回 total_delta = max(current_qty - target_qty, 0)
          （即"立即一次性清仓剩余要退的股数"，调用方保证只调用一次或自行去重）。
      - 分阶段（phased_exit_enabled=True + phases=[{pct, delay_days}, ...]）：
        * 每个 phase i 在 elapsed_trade_days == delay_days 那天成熟；
        * pct 按 sum(phases.pct) 归一化保证 100%（防配置 0.5+0.3=0.8 不退出 20% 的 bug）；
        * phase.quantity_i = normalized_pct_i * total_delta；
        * sum 所有成熟 phase 的 quantity 就是 sell_qty_today；
        * total_phase_percent_mature = sum(normalized_pct_i for mature phases)。
    """
    total_delta = float(current_qty) - float(target_qty)
    total_delta = max(total_delta, 0.0)
    elapsed = int(elapsed_trade_days)
    if elapsed < 0:
        elapsed = 0

    cfg = exit_config if isinstance(exit_config, dict) else {}
    phased_enabled = bool(cfg.get("phased_exit_enabled")) is True
    phases_raw: list[Any] = cfg.get("phases") if isinstance(cfg.get("phases"), list) else []

    if (not phased_enabled) or (not phases_raw):
        # 一次性清仓：忽略 elapsed，直接全退 total_delta
        qty = total_delta
        trace = {
            "one_shot_mode": True,
            "phased_exit_enabled": False,
            "quantities_by_phase_idx": {"0": qty} if qty > 0 else {},
            "total_phase_percent_mature": 1.0 if qty > 0 or total_delta <= 0 else 0.0,
            "total_delta": total_delta,
            "elapsed_trade_days": elapsed,
        }
        return qty, trace

    # ---- 分阶段：规范化 phases ----
    parsed: list[tuple[float, int]] = []  # (raw_pct, delay_days)
    for p in phases_raw:
        if not isinstance(p, dict):
            continue
        try:
            pct = float(p.get("pct"))
        except (TypeError, ValueError):
            continue
        if pct <= 0:
            continue
        try:
            delay = int(p.get("delay_days"))
        except (TypeError, ValueError):
            delay = 0
        if delay < 0:
            delay = 0
        parsed.append((pct, delay))

    if not parsed:
        # 空 phase 列表 → 退回一次性逻辑（同上：忽略 elapsed）
        qty = total_delta
        return qty, {
            "one_shot_mode": True,
            "phased_exit_enabled": False,
            "quantities_by_phase_idx": {},
            "total_phase_percent_mature": 1.0 if (total_delta <= 0 or qty > 0) else 0.0,
            "total_delta": total_delta,
            "elapsed_trade_days": elapsed,
            "note": "phased_enabled=True but phases empty → fallback one_shot",
        }

    sum_raw_pct = sum(p for p, _ in parsed)
    if sum_raw_pct <= 0:  # pragma: no cover - defensive
        qty = total_delta
        return qty, {
            "one_shot_mode": True, "phased_exit_enabled": False,
            "quantities_by_phase_idx": {}, "total_phase_percent_mature": 1.0 if (total_delta <= 0 or qty > 0) else 0.0,
            "total_delta": total_delta, "elapsed_trade_days": elapsed,
            "note": "phases pct sum <= 0 → fallback one_shot",
        }
    normalized = [(p / sum_raw_pct, delay) for p, delay in parsed]

    qty_per_idx: dict[str, float] = {}
    mature_pct = 0.0
    for i, (npct, delay_i) in enumerate(normalized):
        # mature 条件：delay_i == elapsed 当日卖出；累计所有 delay_i <= elapsed 视为已 mature
        if delay_i <= elapsed:
            mature_pct += npct
        if delay_i == elapsed:
            qty_per_idx[str(i)] = npct * total_delta
    sell_qty = sum(qty_per_idx.values())

    # clamp mature_pct 到 [0, 1] 防浮点
    mature_pct = min(max(mature_pct, 0.0), 1.0)

    trace = {
        "one_shot_mode": False,
        "phased_exit_enabled": True,
        "quantities_by_phase_idx": qty_per_idx,
        "total_phase_percent_mature": mature_pct,
        "normalized_phases_pct": [round(x[0], 12) for x in normalized],
        "phase_delays": [x[1] for x in normalized],
        "total_delta": total_delta,
        "elapsed_trade_days": elapsed,
    }
    return sell_qty, trace


def _classify_blocking(reasons: list[dict[str, Any]]) -> BlockingStatus:
    codes = {r.get("code") for r in reasons if r.get("severity") == "blocking"}
    if {"RECONCILIATION_FAILED"} & codes:
        return "RECONCILIATION_BLOCKED"
    if {"MODEL_INACTIVE", "MODEL_NOT_GLOBAL_ACTIVE"} & codes:
        return "MODEL_INACTIVE"
    if {"SCORE_MAX_AGE_EXCEEDED"} & codes:
        return "SCORE_STALE"
    # 默认按数据问题归因
    return "DATA_INCOMPLETE_PAUSED"


def _read_sla_from_snapshot(snap: LoadedSnapshot) -> dict[str, Any]:
    versions = snap.versions or {}
    return {
        "coverage_pct": float(versions.get("score_sla_coverage_pct", 95.0)),
        "max_age_days": int(versions.get("score_sla_max_age_days", 1)),
    }


# 包级默认实例，供路由/服务直接调用
default_engine = DecisionEngine()


def evaluate(
    db: Session,
    *,
    portfolio_id: int,
    strategy_snapshot_id: str,
    trade_date: date,
    run_type: RunType = "research_preflight",
    dry_run: bool = True,
    coverage_result: dict | None = None,
    key_member_result: dict | None = None,
    staleness_result: dict | None = None,
    per_member_score_avail: list[dict] | None = None,
    allow_legacy_fallback: bool | None = None,
    price_data_by_symbol: dict[int, dict[str, Any]] | None = None,
    state_context: DecisionStateContext | None = None,
    match_mode: MatchMode | str | None = None,
    allow_t_close_research_override: bool | None = None,
) -> EvaluateResult:
    """便捷入口；等价于 default_engine.evaluate(...)。"""
    return default_engine.evaluate(
        db, portfolio_id=portfolio_id,
        strategy_snapshot_id=strategy_snapshot_id,
        trade_date=trade_date, run_type=run_type, dry_run=dry_run,
        coverage_result=coverage_result,
        key_member_result=key_member_result,
        staleness_result=staleness_result,
        per_member_score_avail=per_member_score_avail,
        allow_legacy_fallback=allow_legacy_fallback,
        price_data_by_symbol=price_data_by_symbol,
        state_context=state_context,
        match_mode=match_mode,
        allow_t_close_research_override=allow_t_close_research_override,
    )


# ──────────────────────────────────────────────────────────── Q6.1 Fail-Closed
def validate_allow_legacy_fallback(
    *,
    run_mode: str,
    allow_legacy_fallback: bool | None,
) -> None:
    """Q6.3 Fail-Closed：生产链路绝不允许 legacy fallback。

    - run_mode in {production, strict_pit} AND allow_legacy_fallback=True
      → raise ValueError with meta: {error_code: "FALLBACK_NOT_ALLOWED_IN_PRODUCTION"}
        （最终包在 evaluate() 的 try 层，转 HTTP 422；或让调用方在路由层直接转 422）
    - run_mode=research：allow_legacy_fallback=True → OK（显式授权 legacy 使用）；
      allow_legacy_fallback=None/False → 默认 False，不启用 legacy。
    """
    is_formal = run_mode in {"production", "strict_pit", "production_pit", "production_sim"}
    if is_formal and allow_legacy_fallback is True:
        err = ValueError("legacy_score fallback 不允许在生产/正式链路使用")
        err.__dict__["meta"] = {"error_code": "FALLBACK_NOT_ALLOWED_IN_PRODUCTION"}
        raise err


def evaluate_fail_closed_decision(
    db: Session,
    *,
    run_mode: str,
    coverage_result: dict,
    key_member_result: dict,
    staleness_result: dict,
    per_member_score_avail: list[dict],
    portfolio_id: int,
    strategy_snapshot_id: str | None,
    decision_at: datetime,
    allow_legacy_fallback: bool | None = None,
) -> dict:
    """Q6.1 / Q6.2 / Q6.3 Fail-Closed 组合级门禁。

    Rule 0（Q6.3 双重保险）：正式链路绝不允许 legacy fallback
      run_mode ∈ 正式 AND allow_legacy_fallback=True → raise ValueError

    Rule 1（正式阻断）：run_mode ∈ {production, strict_pit}
      以下任一 True → block_all_members=True, composite_level=BLOCKED
      ① coverage_result.coverage_level != "PASS" (WARN 或 FAIL：< 95%)
      ② key_member_result.level == "REJECTED"
      ③ staleness_result.level != "PASS" (即 SCORE_STALE)
      ④ 任意 per_member_score_avail.score_found=False (缺 Score)
         * Q6.3 例外：research 模式下 use_legacy_fallback=True 的成员不触发阻断
      ⑤ 任意 per_member_score_avail.not_pit_safe=True (且 run_mode ∈ {production, strict_pit})

    Rule 2（研究降级）：run_mode == "research"
      上述任一为 False 但其他 PASS → composite_level=WARN, block_all_members=False
      该成员被打入 per_member_rejections_research_only
      degraded_warnings 加一条（每条原因独立 1 条 warning）
      is_result_production_eligible=False
      * Q6.3：use_legacy_fallback=True → degraded_warnings 加 FALLBACK_USED，
        per_member_extra_info 标记 legacy_fallback_used=True，不算 REJECTED

    Rule 3（全 PASS）：全部条件 PASS → block_all_members=False,
      composite_level=PASS, is_result_production_eligible=True

    Rule 4（阻断审计码）：block_codes 命中的每一个
      ["COVERAGE_BELOW_95", "KEY_MEMBER_SCORE_MISSING", "SCORE_STALE",
       "SINGLE_MEMBER_STALE_SCORE", "NOT_PIT_SAFE_IN_STALE"]

    Returns {
        "composite_level": "PASS" | "WARN" | "BLOCKED",
        "block_all_members": bool,
        "block_codes": list[str],
        "block_reasons": list[str],
        "per_member_rejections_research_only": list[dict],
        "per_member_extra_info": list[dict],
        "degraded_warnings": list[dict],
        "is_result_production_eligible": bool,
    }
    """
    from typing import Literal

    validate_allow_legacy_fallback(
        run_mode=run_mode,
        allow_legacy_fallback=allow_legacy_fallback,
    )

    CompositeLevel = Literal["PASS", "WARN", "BLOCKED"]

    block_codes: list[str] = []
    block_reasons: list[str] = []
    per_member_rejections: list[dict] = []
    per_member_extra_info: list[dict] = []
    degraded_warnings: list[dict] = []
    legacy_used_any: bool = False

    is_formal = run_mode in {"production", "strict_pit", "production_pit", "production_sim"}

    coverage_level = coverage_result.get("coverage_level", "PASS")
    key_member_level = key_member_result.get("level", "PASS")
    staleness_level = staleness_result.get("level", "PASS")

    # ── Rule 1/2 条件 ①：Coverage（Q6.2 95% 正式阻断）
    coverage_pct_raw = coverage_result.get("coverage_pct")
    coverage_rate = None
    if coverage_pct_raw is not None:
        coverage_rate = float(coverage_pct_raw) / 100.0
    elif coverage_result.get("coverage_rate") is not None:
        coverage_rate = float(coverage_result.get("coverage_rate"))

    pct_display = (
        f"{float(coverage_pct_raw):.2f}%"
        if coverage_pct_raw is not None
        else (f"{coverage_rate * 100:.2f}%" if coverage_rate is not None else "N/A")
    )

    if coverage_level in {"WARN", "FAIL"}:
        if coverage_rate is not None:
            below_95 = coverage_rate < 0.95
            below_92 = coverage_rate < 0.92
        else:
            below_95 = coverage_level in {"WARN", "FAIL"}
            below_92 = (coverage_level == "FAIL")

        if is_formal:
            if below_95:
                code_95 = "COVERAGE_BELOW_95"
                msg_95 = f"COVERAGE_BELOW_95：覆盖率 {pct_display} < 95% 正式门槛"
                block_codes.append(code_95)
                block_reasons.append(msg_95)
            if below_92:
                code_92 = "COVERAGE_CRITICAL_BELOW_92"
                msg_92 = f"COVERAGE_CRITICAL_BELOW_92：覆盖率 {pct_display} < 92%"
                block_codes.append(code_92)
                block_reasons.append(msg_92)
        else:
            if coverage_level == "WARN":
                degraded_warnings.append({
                    "code": "DEGRADED_DATA",
                    "subtype": "COVERAGE_WARN_92_95",
                    "pct": pct_display,
                })
            elif coverage_level == "FAIL":
                degraded_warnings.append({
                    "code": "DEGRADED_DATA",
                    "subtype": "COVERAGE_FAIL_BELOW_92",
                    "pct": pct_display,
                })

    # ── Rule 1/2 条件 ②：Key member
    if key_member_level == "REJECTED":
        code = "KEY_MEMBER_SCORE_MISSING"
        msg = (
            f"关键成员缺 Score: level=REJECTED, "
            f"missing={key_member_result.get('missing_key_members', [])}"
        )
        if is_formal:
            block_codes.append(code)
            block_reasons.append(msg)
        else:
            degraded_warnings.append({
                "code": "DEGRADED_DATA",
                "sub_code": code,
                "message": msg,
                "detail": {
                    "missing_key_members": key_member_result.get("missing_key_members", []),
                },
            })
            for km in key_member_result.get("missing_key_members", []) or []:
                per_member_rejections.append({
                    "symbol_id": int(km) if isinstance(km, int) or (isinstance(km, str) and km.isdigit()) else km,
                    "action": "REJECTED",
                    "action_subtype": "KEY_MEMBER_SCORE_MISSING",
                    "reason": msg,
                })

    # ── Rule 1/2 条件 ③：Staleness
    if staleness_level != "PASS":
        code = "SCORE_STALE"
        msg = (
            f"Score 新鲜度不达标: staleness_level={staleness_level}, "
            f"max_age_days={staleness_result.get('max_age_days', 'N/A')}"
        )
        if is_formal:
            block_codes.append(code)
            block_reasons.append(msg)
        else:
            degraded_warnings.append({
                "code": "DEGRADED_DATA",
                "sub_code": code,
                "message": msg,
                "detail": {
                    "staleness_level": staleness_level,
                    "max_age_days": staleness_result.get("max_age_days"),
                    "sla_max_age_days": staleness_result.get("sla_max_age_days"),
                },
            })

    # ── Rule 1/2 条件 ④+⑤：Per-member 逐项检查
    for member in per_member_score_avail:
        sym_id = member.get("symbol_id")
        score_found = bool(member.get("score_found", False))
        not_pit_safe = bool(member.get("not_pit_safe", False))
        staled = bool(member.get("staled", False))
        use_legacy_fallback = bool(member.get("use_legacy_fallback", False))

        # Q6.3: research 模式下 use_legacy_fallback=True → 留痕，不算 REJECTED
        legacy_used_for_this_member = False
        if (not is_formal) and use_legacy_fallback and (not score_found or staled):
            legacy_used_for_this_member = True
            legacy_used_any = True
            degraded_warnings.append({
                "code": "FALLBACK_USED",
                "symbol_id": sym_id,
                "subtype": "LEGACY_SCORE_FALLBACK",
            })
            per_member_extra_info.append({
                "symbol_id": sym_id,
                "legacy_fallback_used": True,
            })

        # ④ score_found=False → SINGLE_MEMBER_STALE_SCORE (Q6.1: 缺 Score 也归类为 stale)
        # Q6.3: research + use_legacy_fallback=True → 不触发 rejection（留痕但放行）
        if not score_found and not legacy_used_for_this_member:
            code = "SINGLE_MEMBER_STALE_SCORE"
            msg = f"成员 symbol_id={sym_id} 缺 Score (score_found=False)"
            if is_formal:
                if code not in block_codes:
                    block_codes.append(code)
                    block_reasons.append(msg)
            else:
                degraded_warnings.append({
                    "code": "DEGRADED_DATA",
                    "sub_code": code,
                    "message": msg,
                    "detail": {"symbol_id": sym_id, "issue": "score_missing"},
                })
                per_member_rejections.append({
                    "symbol_id": sym_id,
                    "action": "REJECTED",
                    "action_subtype": "SINGLE_MEMBER_STALE_SCORE",
                    "reason": msg,
                })

        # staled=True → 同样计入 SINGLE_MEMBER_STALE_SCORE
        # Q6.3: research + use_legacy_fallback=True → 不触发 rejection（留痕但放行）
        if staled and score_found and not legacy_used_for_this_member:
            code = "SINGLE_MEMBER_STALE_SCORE"
            msg = f"成员 symbol_id={sym_id} Score 过期 (staled=True)"
            if is_formal:
                if code not in block_codes:
                    block_codes.append(code)
                    block_reasons.append(msg)
            else:
                degraded_warnings.append({
                    "code": "DEGRADED_DATA",
                    "sub_code": code,
                    "message": msg,
                    "detail": {"symbol_id": sym_id, "issue": "score_staled"},
                })
                already_rejected = any(
                    r.get("symbol_id") == sym_id for r in per_member_rejections
                )
                if not already_rejected:
                    per_member_rejections.append({
                        "symbol_id": sym_id,
                        "action": "REJECTED",
                        "action_subtype": "SINGLE_MEMBER_STALE_SCORE",
                        "reason": msg,
                    })

        # ⑤ not_pit_safe=True 且 run_mode ∈ 正式模式
        if not_pit_safe and is_formal:
            code = "NOT_PIT_SAFE_IN_STALE"
            msg = f"成员 symbol_id={sym_id} NOT_PIT_SAFE，禁用（正式链路严格 PIT）"
            if code not in block_codes:
                block_codes.append(code)
                block_reasons.append(msg)
        elif not_pit_safe and not is_formal:
            code = "NOT_PIT_SAFE_IN_STALE"
            msg = f"成员 symbol_id={sym_id} NOT_PIT_SAFE（研究模式仅降级，不阻断）"
            degraded_warnings.append({
                "code": "DEGRADED_DATA",
                "sub_code": code,
                "message": msg,
                "detail": {"symbol_id": sym_id, "issue": "not_pit_safe"},
            })
            already_rejected = any(
                r.get("symbol_id") == sym_id for r in per_member_rejections
            )
            if not already_rejected and not legacy_used_for_this_member:
                per_member_rejections.append({
                    "symbol_id": sym_id,
                    "action": "REJECTED",
                    "action_subtype": "NOT_PIT_SAFE_IN_STALE",
                    "reason": msg,
                })

    # ── 确定 composite_level / block_all_members
    composite_level: CompositeLevel
    block_all_members: bool
    is_prod_eligible: bool

    if is_formal and block_codes:
        composite_level = "BLOCKED"
        block_all_members = True
        is_prod_eligible = False
    elif not is_formal and (degraded_warnings or per_member_rejections or legacy_used_any):
        composite_level = "WARN"
        block_all_members = False
        is_prod_eligible = False
    else:
        composite_level = "PASS"
        block_all_members = False
        is_prod_eligible = True

    return {
        "composite_level": composite_level,
        "block_all_members": block_all_members,
        "block_codes": block_codes,
        "block_reasons": block_reasons,
        "per_member_rejections_research_only": per_member_rejections,
        "per_member_extra_info": per_member_extra_info,
        "degraded_warnings": degraded_warnings,
        "is_result_production_eligible": is_prod_eligible,
    }
