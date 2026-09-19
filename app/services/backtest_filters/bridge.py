"""回测主循环与 BFG 过滤治理的桥接层。

现有 backtest.py 在每日加载 t 日数据后，只需调用：
    from app.services.backtest_filters.bridge import pre_rebalance_pipeline
    pipeline_out = pre_rebalance_pipeline(db=..., run_id=..., trade_date=..., raw_candidates=..., positions=..., cfg=...)
然后使用：
    pipeline_out.eligible_candidates    # 过滤后的候选池（替换原 candidate_pool）
    pipeline_out.frozen_skip_set        # 传入 match_engine.execute(skip_symbol_ids=...)
    pipeline_out.liquidations           # 退市清算结果（用于写入 BacktestExecutionFill、净值）
    pipeline_out.filter_events          # 批量写入审计（bulk_write_filter_events）
    pipeline_out.errors                 # 阻断错误信息列表（如有 StatusUnknownBlockingError 等）

保持纯函数契约，不 commit 不 rollback。

Task 32 兼容：PipelineRebalanceInput 同时接受
  - 老字段：db / run_id / positions / cfg / data_batch_id
  - 新字段：filter_config / pit_service / liquidation_service / portfolio_id
并在 engine_compat_version == "legacy_baseline" 时立即 short-circuit 返回原始
candidates（不调用 PIT / 过滤 / 清算）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Optional, Sequence, Set

from sqlalchemy.orm import Session

from app.services.backtest_filters.config import BacktestFilterConfig, compute_config_hash
from app.services.backtest_filters.engine import apply_daily_filters
from app.services.backtest_filters.audit_writer import bulk_write_filter_events
from app.services.backtest_filters.liquidation import (
    HoldingInfo, LiquidationResult, process_delisting_liquidations,
)
from app.services.backtest_filters.rules import (
    DelistingCandidate, FilterEventDTO, FilterOutcome, FrozenPositionInfo,
    StatusUnknownBlockingError,
)
from app.services.backtest_filters.freeze import frozen_symbols_skip_set

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineRebalanceInput:
    """pre_rebalance_pipeline 的入参（集中管理）。

    新旧双签名兼容：
      * 老调用：db + run_id + positions + cfg + raw_candidate_symbol_ids
      * 新签名（Task 32 / pytest）：trade_date + raw_candidate_symbol_ids +
        filter_config + enable_filters (+ pit_service/liquidation_service/portfolio_id)
    """
    # 老字段：可空（为了新签名不用传）
    db: Optional[Session] = None
    run_id: Optional[int] = None
    positions: Mapping[int, HoldingInfo] = field(default_factory=dict)
    cfg: Optional[BacktestFilterConfig] = None
    data_batch_id: Optional[str] = None
    # 核心必填（两种签名都要有）
    trade_date: Optional[date] = None
    raw_candidate_symbol_ids: Sequence[int] = field(default_factory=list)
    # Task 32 新字段：
    filter_config: Optional[BacktestFilterConfig] = None
    enable_filters: bool = True
    pit_service: Any = None
    liquidation_service: Any = None
    portfolio_id: Optional[int] = None


@dataclass(frozen=True)
class PipelineRebalanceOutput:
    """pre_rebalance_pipeline 输出（供主循环按字段消费）。

    新旧双签名兼容：
      * 老字段：filter_outcome, eligible_candidates(list), frozen_skip_set,
        liquidations, filter_events, config_hash, blocking_errors, should_persist_events
      * 新字段（Task 32 spec）：eligible_candidates (set 也可通过属性读),
        audit_events, liquidation_orders, effective_config_hash
    """
    # 老字段（默认值给 legacy short-circuit 用）
    filter_outcome: Optional[FilterOutcome] = None
    eligible_candidates: Any = field(default_factory=list)
    frozen_skip_set: Set[int] = field(default_factory=set)
    liquidations: Sequence[LiquidationResult] = field(default_factory=list)
    filter_events: Sequence[FilterEventDTO] = field(default_factory=list)
    config_hash: str = ""
    blocking_errors: list[str] = field(default_factory=list)
    should_persist_events: bool = True
    # Task 32 spec 新字段
    audit_events: Sequence[FilterEventDTO] = field(default_factory=list)
    liquidation_orders: Sequence[Any] = field(default_factory=list)
    effective_config_hash: str = ""


def _resolve_cfg(p: PipelineRebalanceInput) -> Optional[BacktestFilterConfig]:
    """优先用 filter_config，回退 cfg。"""
    return p.filter_config if p.filter_config is not None else p.cfg


def pre_rebalance_pipeline(p: PipelineRebalanceInput) -> PipelineRebalanceOutput:
    """调仓前统一执行 BFG 管线。

    顺序（严格按 PRD）：
      0) Task 32 legacy_baseline short-circuit（不调用任何过滤/清算/PIT）
      1) 批量查表 PIT 状态
      2) apply_daily_filters（UNKNOWN -> 次新股 -> ST -> 停牌 -> 整理期 -> 清算候选）
      3) 退市清算执行（取收盘价、生成 LiquidationResult + 对应事件）
      4) 生成 frozen_skip_set（供 match_engine）
      5) 返回所有 outputs + 事件列表（主循环调用 bulk_write）

    阻断时不抛异常，改为把错误消息放进 blocking_errors，主循环可集中处理。
    """
    cfg = _resolve_cfg(p)
    cfg_hash = compute_config_hash(cfg) if cfg is not None else ""
    blocking_errors: list[str] = []

    # --- Task 32.2: legacy_baseline 顶层 short-circuit -------------------
    # 完全不调用过滤/清算/PIT，直接返回原始 candidates
    if cfg is not None and cfg.engine_compat_version == "legacy_baseline":
        audit_events = [
            FilterEventDTO(
                symbol_id=sid,
                trade_date=p.trade_date if p.trade_date is not None else date.today(),
                rule_code="LEGACY_BASELINE_COMPAT_MODE",
                action="include",
                reason="engine_compat_version=legacy_baseline: no filters applied, exact replay",
                evidence={"compat": "legacy_baseline"},
            ) for sid in sorted(set(p.raw_candidate_symbol_ids))
        ]
        eligible_set: Set[int] = set(p.raw_candidate_symbol_ids)
        return PipelineRebalanceOutput(
            # 新字段（按 spec）
            eligible_candidates=eligible_set,
            liquidation_orders=[],
            frozen_skip_set=set(),
            audit_events=audit_events,
            blocking_errors=[],
            effective_config_hash="legacy_baseline",
            # 老字段（兼容）
            filter_outcome=FilterOutcome(
                eligible_candidates=list(p.raw_candidate_symbol_ids),
                frozen_positions={}, delisting_candidates=[], filter_events=audit_events,
            ),
            liquidations=[],
            filter_events=audit_events,
            config_hash="legacy_baseline",
            should_persist_events=False,
        )

    # 1) enable_filters=False 时的老兼容分支（跳过全部过滤）
    if not p.enable_filters:
        return PipelineRebalanceOutput(
            filter_outcome=FilterOutcome(
                eligible_candidates=list(p.raw_candidate_symbol_ids),
                frozen_positions={}, delisting_candidates=[], filter_events=[],
            ),
            eligible_candidates=list(p.raw_candidate_symbol_ids),
            frozen_skip_set=set(),
            liquidations=[],
            filter_events=[],
            config_hash=cfg_hash,
            blocking_errors=[],
            should_persist_events=False,
            # Task 32 新字段镜像
            audit_events=[],
            liquidation_orders=[],
            effective_config_hash=cfg_hash,
        )

    # 正常路径：db 必须可用
    if p.db is None:
        raise RuntimeError(
            "pre_rebalance_pipeline: db is required when enable_filters=True "
            "and engine_compat_version != legacy_baseline"
        )
    if p.trade_date is None:
        raise RuntimeError("pre_rebalance_pipeline: trade_date is required")

    all_symbol_ids = list(set(
        list(p.raw_candidate_symbol_ids) + list(p.positions.keys())
    ))
    # PIT 查询：优先用传入的 pit_service，否则用全局 SecurityStatusPitService
    if p.pit_service is not None and hasattr(p.pit_service, "status_batch"):
        pit_map = p.pit_service.status_batch(p.db, all_symbol_ids, p.trade_date)
    else:
        from app.services.security_status.pit_service import SecurityStatusPitService
        pit_map = SecurityStatusPitService.status_batch(
            p.db, all_symbol_ids, p.trade_date
        )

    # 2) apply_daily_filters（UNKNOWN 生产保真阻断：捕获异常->blocking_errors）
    filter_outcome: Optional[FilterOutcome] = None
    try:
        filter_outcome = apply_daily_filters(
            trade_date=p.trade_date,
            candidate_symbol_ids=list(p.raw_candidate_symbol_ids),
            position_map={sid: h.quantity for sid, h in p.positions.items()},
            pit_status_map=pit_map,
            config=cfg,
            data_batch_id=p.data_batch_id,
            run_id=p.run_id,
        )
    except StatusUnknownBlockingError as e:
        blocking_errors.append(str(e))
        filter_outcome = FilterOutcome(
            eligible_candidates=[],
            frozen_positions={}, delisting_candidates=[],
            filter_events=[
                FilterEventDTO(
                    run_id=p.run_id, trade_date=p.trade_date, symbol_id=sid,
                    action="exclude_candidate",
                    rule_code=e.rule_code, reason=e.detail,
                    raw_status_json="{}", effective_status="UNKNOWN",
                    config_hash=cfg_hash, data_batch_id=p.data_batch_id,
                ) for sid in e.symbol_ids
            ],
        )

    eligible = list(filter_outcome.eligible_candidates)
    frozen_map: dict[int, FrozenPositionInfo] = dict(filter_outcome.frozen_positions)
    frozen_skips = frozen_symbols_skip_set(frozen_map)
    delisting_cands: list[DelistingCandidate] = list(filter_outcome.delisting_candidates)
    events: list[FilterEventDTO] = list(filter_outcome.filter_events)

    # 3) 退市清算（若阻断已存在，跳过，避免写入半状态）
    liquidations: list[LiquidationResult] = []
    extra_events: list[FilterEventDTO] = []
    if not blocking_errors and delisting_cands:
        try:
            # 如果传入了 liquidation_service 优先用它；否则走 process_delisting_liquidations
            if (
                p.liquidation_service is not None
                and hasattr(p.liquidation_service, "process_delisting_liquidations")
            ):
                liquidations, extra_events = p.liquidation_service.process_delisting_liquidations(
                    db=p.db,
                    run_id=p.run_id,
                    trade_date=p.trade_date,
                    positions=p.positions,
                    pit_status_map=pit_map,
                    delisting_candidates=delisting_cands,
                    price_resolver=None,
                    config_hash=cfg_hash,
                    data_batch_id=p.data_batch_id,
                )
            else:
                liquidations, extra_events = process_delisting_liquidations(
                    db=p.db,
                    run_id=p.run_id,
                    trade_date=p.trade_date,
                    positions=p.positions,
                    pit_status_map=pit_map,
                    delisting_candidates=delisting_cands,
                    price_resolver=None,  # 使用默认 DailyBar resolver
                    config_hash=cfg_hash,
                    data_batch_id=p.data_batch_id,
                )
        except Exception as e:  # DelistingPriceMissingError 或其他
            blocking_errors.append(f"[LIQUIDATION] {e!r}")

    events = events + list(extra_events)

    # diagnostic_only：把 eligible_candidates 还原为 raw_candidates（仅写事件，不影响实际交易）
    if cfg is not None and cfg.diagnostic_only:
        eligible = list(p.raw_candidate_symbol_ids)
        # 追加差异事件
        diff = len(set(p.raw_candidate_symbol_ids) - set(filter_outcome.eligible_candidates))
        if diff:
            events.append(FilterEventDTO(
                run_id=p.run_id, trade_date=p.trade_date,
                symbol_id=min(p.raw_candidate_symbol_ids) if p.raw_candidate_symbol_ids else 0,
                action="include",
                rule_code="DIAGNOSTIC_ONLY_OVERRIDE",
                reason=f"[diagnostic_only] 还原候选池：{diff} 条被排除的股票重新纳入（审计用，不改交易结果）",
                raw_status_json="{}", effective_status="LISTED",
                config_hash=cfg_hash, data_batch_id=p.data_batch_id,
            ))

    return PipelineRebalanceOutput(
        filter_outcome=filter_outcome,
        eligible_candidates=eligible,
        frozen_skip_set=frozen_skips,
        liquidations=liquidations,
        filter_events=events,
        config_hash=cfg_hash,
        blocking_errors=blocking_errors,
        should_persist_events=True,
        # Task 32 新字段镜像
        audit_events=events,
        liquidation_orders=liquidations,
        effective_config_hash=cfg_hash,
    )


# ---------------------------------------------------------------------------
# 接入指南（现有 backtest.py 主循环每日位置）：
#
#   from app.services.backtest_filters.bridge import (
#       pre_rebalance_pipeline, PipelineRebalanceInput, PipelineRebalanceOutput,
#   )
#   from app.services.backtest_filters.liquidation import HoldingInfo
#   from app.services.backtest_filters.audit_writer import bulk_write_filter_events
#
#   positions_map = {sid: HoldingInfo(qty, avg_cost) for ...}  # 构造持仓
#   raw_cands = [sid for sid, in original_candidates]
#   pipe = pre_rebalance_pipeline(PipelineRebalanceInput(
#       db=session, run_id=run.id, trade_date=td,
#       raw_candidate_symbol_ids=raw_cands, positions=positions_map,
#       cfg=backtest_filter_config, data_batch_id=state_batch_id,
#   ))
#   if pipe.blocking_errors:
#       run.status = 'failed'; run.error_message = '; '.join(pipe.blocking_errors); raise RuntimeError(...)
#   # 替换候选池 + 清算了结 + 撮合（冻结 skip_set）
#   candidate_pool = pipe.eligible_candidates
#   liquidate_backtest_positions(pipe.liquidations)
#   match_result = match_engine.execute(..., skip_symbol_ids=pipe.frozen_skip_set)
#   # 审计事件批量落库
#   bulk_write_filter_events(session, pipe.filter_events)
#   # 校验冻结契约（__debug__ 断言）
#   if __debug__:
#       from app.services.backtest_filters.freeze import validate_freeze_contract
#       viols = validate_freeze_contract(trade_date=td, frozen_ids=pipe.frozen_skip_set, ...)
#       assert not viols, f'FREEZE CONTRACT VIOLATED: {viols}'
# ---------------------------------------------------------------------------