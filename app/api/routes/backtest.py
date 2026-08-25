import json
import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, asc, desc, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.backtest import (
    BacktestExecutionFill,
    BacktestRun,
    BacktestRuleTemplate,
    BacktestTrade,
    BacktestValuationSnapshot,
)
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.models.daily_bar import DailyBar
from app.models.decision_engine import (
    DecisionEvidence,
    DecisionOrderPlanRecord,
    DecisionRun,
    StrategyExecutionSnapshot,
)
from app.schemas.backtest import (
    BacktestApplyRequest,
    BacktestApplyResult,
    BacktestRunDetail,
    BacktestRunRead,
    BacktestPositionPage,
    BacktestPositionRead,
    BacktestRunRequest,
    BacktestTradeRead,
    PortfolioBacktestCompareRequest,
    PortfolioBacktestCompareResult,
    PortfolioBacktestRequest,
    PortfolioBacktestResult,
    PortfolioBacktestSourceStatus,
    RuleTemplateCreate,
    RuleTemplateResponse,
    RuleTemplateUpdate,
)
from app.schemas.decision_engine import DecisionEvidenceRead
from app.services.backtest import (
    assess_backtest_score_coverage,
    build_backtest_detail_context,
    run_backtest,
)
from app.services.backtest_apply import apply_backtest_run_to_portfolio
from app.services.factors.runtime import get_factor_runtime_snapshot
from app.services.portfolio_backtest import compare_new_old_engine, run_portfolio_backtest
from app.services.portfolio_asset_scope import ensure_symbol_ids_in_scope


router = APIRouter()

# Keep response labels stable when a historical run only persisted the index
# code on its immutable strategy snapshot.
_BENCHMARK_CODE_TO_NAME = {
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "399006": "创业板指",
    "000001": "上证指数",
    "399001": "深证成指",
}


# The ledger exposes UI-oriented names.  Keep the mapping local and explicit so
# request values can never become dynamic SQL identifiers.
_BACKTEST_TRADE_SORT_COLUMNS: dict[str, Any] = {
    "signal_at": BacktestTrade.entry_date,
    "execution_at": func.coalesce(BacktestTrade.exit_date, BacktestTrade.entry_date),
    "symbol_id": BacktestTrade.symbol_id,
    "price": func.coalesce(BacktestTrade.exit_price, BacktestTrade.entry_price),
    "quantity": BacktestTrade.quantity,
    "cost": (
        func.coalesce(BacktestTrade.entry_cost, 0.0)
        + func.coalesce(BacktestTrade.exit_cost, 0.0)
    ),
}
_BACKTEST_TRADE_SORT_DIRECTIONS = frozenset({"asc", "desc"})


def _exact_evidence_decision_run_ids(
    db: Session,
    trades: list[BacktestTrade],
) -> dict[str, str]:
    """Return the DecisionRun for each evidence primary key referenced by a trade."""
    evidence_ids = {
        evidence_id
        for trade in trades
        for evidence_id in (trade.decision_evidence_id, trade.exit_evidence_id)
        if evidence_id
    }
    if not evidence_ids:
        return {}
    rows = db.execute(
        select(DecisionEvidence.id, DecisionEvidence.decision_run_id).where(
            DecisionEvidence.id.in_(evidence_ids)
        )
    ).all()
    return {str(evidence_id): str(decision_run_id) for evidence_id, decision_run_id in rows}


def _order_plan_fields_by_evidence(
    db: Session,
    trades: list[BacktestTrade],
) -> dict[str, dict[str, Any]]:
    """Project persisted execution fields without inferring a different leg."""
    evidence_ids = {
        evidence_id
        for trade in trades
        for evidence_id in (trade.decision_evidence_id, trade.exit_evidence_id)
        if evidence_id
    }
    if not evidence_ids:
        return {}
    rows = db.execute(
        select(DecisionEvidence.id, DecisionEvidence.versions_json).where(
            DecisionEvidence.id.in_(evidence_ids)
        )
    ).all()
    result: dict[str, dict[str, Any]] = {}
    for evidence_id, raw_versions in rows:
        if isinstance(raw_versions, dict):
            versions = raw_versions
        elif isinstance(raw_versions, str):
            try:
                versions = json.loads(raw_versions)
            except (TypeError, ValueError):
                versions = {}
        else:
            versions = {}
        if not isinstance(versions, dict):
            versions = {}
        result[str(evidence_id)] = {
            "requested_quantity": versions.get("order_plan_requested_quantity"),
            "filled_quantity": versions.get("order_plan_filled_quantity"),
            "remaining_quantity": versions.get("order_plan_remaining_quantity"),
            "order_plan_status": versions.get("order_plan_status"),
            "unfilled_reason": versions.get("order_plan_unfilled_reason"),
        }
    # New decision runs expose immutable intent in the relational ledger. Use
    # it for the requested quantity and plan identity while preserving the
    # Evidence JSON fallback for historical runs created before Stage 1.
    plan_rows = db.execute(
        select(DecisionOrderPlanRecord).where(
            DecisionOrderPlanRecord.evidence_id.in_(evidence_ids),
        )
    ).scalars().all()
    for plan in plan_rows:
        fields = result.setdefault(str(plan.evidence_id), {})
        fields["order_plan_id"] = plan.order_plan_id
        fields["requested_quantity"] = float(plan.target_quantity)
        fields["intended_price"] = plan.intended_price
        fields["direction"] = plan.direction
        fields["reason_code"] = plan.reason_code
    return result


def _order_plan_status_expression(db: Session) -> Any:
    """Return a portable SQL expression for Evidence order-plan status."""
    json_path = "$.order_plan_status"
    dialect_name = db.get_bind().dialect.name
    if dialect_name in {"mysql", "mariadb"}:
        return func.json_unquote(func.json_extract(DecisionEvidence.versions_json, json_path))
    return func.json_extract(DecisionEvidence.versions_json, json_path)


@router.post("/backtest/run", response_model=BacktestRunRead)
def create_backtest_run(payload: BacktestRunRequest, db: Session = Depends(get_db)):
    try:
        portfolio = db.get(Portfolio, payload.portfolio_id)
        if portfolio is None:
            raise HTTPException(status_code=404, detail=f"Portfolio {payload.portfolio_id} not found")
        try:
            ensure_symbol_ids_in_scope(db, portfolio, payload.symbol_ids)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime = get_factor_runtime_snapshot(db)
        score_weight_mode = (
            payload.score_weight_mode or runtime.score_weight_mode
        )
        factor_model_run_id = (
            payload.factor_model_run_id or runtime.active_model_run_id
            if score_weight_mode == 'ridge'
            else None
        )
        coverage = assess_backtest_score_coverage(
            db=db,
            symbol_ids=payload.symbol_ids,
            start_date=payload.start_date,
            end_date=payload.end_date,
            score_weight_mode=score_weight_mode,
            factor_model_run_id=factor_model_run_id,
        )
        if coverage["issues"]:
            symbol_rows = db.execute(select(Symbol).where(Symbol.id.in_(payload.symbol_ids))).scalars().all()
            symbol_map = {
                row.id: {
                    "symbol": row.symbol,
                    "name": row.name,
                }
                for row in symbol_rows
            }
            for issue in coverage["issues"]:
                issue.update(symbol_map.get(issue["symbol_id"], {}))

            min_coverage = min(issue.get("coverage_pct", 0) for issue in coverage["issues"]) if coverage["issues"] else 0
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "BACKTEST_SCORE_COVERAGE_INSUFFICIENT",
                    "message": "Historical score coverage is insufficient for this backtest range. Initialize history data in Settings before running the backtest.",
                    "summary": {
                        **coverage,
                        "min_coverage_pct": min_coverage,
                    },
                    "issues": coverage["issues"],
                },
            )

        result = run_backtest(
            db=db,
            portfolio_id=payload.portfolio_id,
            symbol_ids=payload.symbol_ids,
            start_date=payload.start_date,
            end_date=payload.end_date,
            rule_config=payload.rule_config.model_dump(),
            cost_config=payload.cost_config.model_dump() if payload.cost_config else None,
            run_name=payload.run_name,
            score_weight_mode=score_weight_mode,
            factor_model_run_id=factor_model_run_id,
        )
        payload_result = dict(result) if isinstance(result, dict) else dict(result.__dict__)
        result_id = payload_result.get("id")
        contract = _enrich_backtest_result_contract(db, {"run_id": result_id})
        payload_result.update({k: v for k, v in contract.items() if k != "run_id"})
        return BacktestRunRead(**payload_result)
    except HTTPException:
        raise
    except ValueError as exc:
        # 将可由用户修复的数据问题转换为明确的 4xx，避免被全局异常处理器
        # 包装成“服务暂时不可用”。前端可据 error_code 提供直达修复入口。
        if str(exc) == "No trading data found in date range":
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "BACKTEST_MARKET_DATA_MISSING",
                    "message": "该标的在回测区间内没有行情数据，请先初始化历史行情。",
                },
            ) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logging.getLogger(__name__).exception("回测执行失败")
        raise HTTPException(status_code=500, detail="回测执行失败，请检查配置或稍后重试") from exc


@router.get("/backtest/runs", response_model=list[BacktestRunRead])
def list_backtest_runs(
    portfolio_id: int = Query(...),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    runs = db.execute(
        select(BacktestRun)
        .where(BacktestRun.portfolio_id == portfolio_id)
        .order_by(desc(BacktestRun.created_at))
        .limit(limit)
    ).scalars().all()
    # Keep the history list on the same read contract as POST/detail.  In
    # particular, cost/PIT/snapshot/benchmark health must not disappear merely
    # because the caller opened the history view instead of a single run.
    items: list[BacktestRunRead] = []
    for run in runs:
        payload = dict(run.__dict__)
        contract = _enrich_backtest_result_contract(db, {"run_id": run.id})
        payload.update({k: v for k, v in contract.items() if k != "run_id"})
        items.append(BacktestRunRead(**payload))
    return items


def _backtest_decision_context(db: Session, run: BacktestRun) -> dict[str, Any]:
    """Return persisted DecisionRun/evidence linkage for a backtest.

    A historical backtest is allowed to have no decision run linkage. In that
    case the response is explicit (empty lists/zero counts) rather than
    creating synthetic evidence during a read.
    """
    try:
        raw_ids = json.loads(run.decision_run_ids_json or "[]")
    except (TypeError, ValueError):
        raw_ids = []
    run_ids = [str(x) for x in raw_ids if x]
    decision_runs = []
    if run_ids:
        decision_runs = db.execute(
            select(DecisionRun)
            .where(DecisionRun.id.in_(run_ids))
            .order_by(DecisionRun.trade_date, DecisionRun.created_at)
        ).scalars().all()
    evidence_summary: dict[str, Any] = {
        "total": 0,
        "by_action": {},
        "linked_trade_count": 0,
    }
    if run_ids:
        rows = db.execute(
            select(DecisionEvidence.action, func.count(DecisionEvidence.id))
            .where(DecisionEvidence.decision_run_id.in_(run_ids))
            .group_by(DecisionEvidence.action)
        ).all()
        by_action = {str(action): int(count) for action, count in rows}
        evidence_summary["by_action"] = by_action
        evidence_summary["total"] = sum(by_action.values())
    execution_rejected_count = 0
    if run_ids:
        execution_rejected_count = int(
            db.scalar(
                select(func.count()).select_from(DecisionEvidence).where(
                    DecisionEvidence.decision_run_id.in_(run_ids),
                    DecisionEvidence.action.in_(("BUY", "SELL")),
                    _order_plan_status_expression(db) == "REJECTED",
                )
            )
            or 0
        )
        evidence_summary["execution_rejected_count"] = execution_rejected_count
    linked_trade_count = int(db.scalar(
        select(func.count()).select_from(BacktestTrade).where(
            BacktestTrade.run_id == run.id,
            (BacktestTrade.decision_evidence_id.is_not(None))
            | (BacktestTrade.exit_evidence_id.is_not(None)),
        )
    ) or 0)
    evidence_summary["linked_trade_count"] = linked_trade_count
    snapshot = None
    if run.strategy_snapshot_id:
        snap = db.get(StrategyExecutionSnapshot, run.strategy_snapshot_id)
        if snap is not None:
            try:
                snapshot_versions = json.loads(snap.versions_json or "{}")
            except (TypeError, ValueError):
                snapshot_versions = {}
            snapshot_pit_mode = (
                snapshot_versions.get("pit_mode")
                if isinstance(snapshot_versions, dict)
                else None
            ) or run.pit_mode
            snapshot = {
                "id": snap.id,
                "snapshot_no": snap.snapshot_no,
                "portfolio_id": snap.portfolio_id,
                "factor_model_run_id": snap.factor_model_run_id,
                "factor_set_id": snap.factor_set_id,
                "rule_id": snap.rule_id,
                "rule_version": snap.rule_version,
                "snapshot_type": snap.snapshot_type,
                "snapshot_hash": snap.snapshot_hash,
                "effective_from": snap.effective_from,
                "pit_mode": snapshot_pit_mode,
                "versions_json": snap.versions_json,
            }
    return {
        "decision_run_ids": [str(r.id) for r in decision_runs] or run_ids,
        "decision_runs": decision_runs,
        "evidence_summary": evidence_summary,
        "rejected_count": int(
            evidence_summary.get("by_action", {}).get("REJECTED", 0)
            + evidence_summary.get("by_action", {}).get("DATA_BLOCKED", 0)
            + execution_rejected_count
        ),
        "decision_snapshot": snapshot,
    }


def _json_value(raw: Any, default: Any = None) -> Any:
    """Decode a JSON text column without making response reads fail closed."""
    if raw is None:
        return default
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default
    return default


def _enrich_backtest_result_contract(
    db: Session,
    result: dict[str, Any],
    *,
    benchmark_name: str | None = None,
) -> dict[str, Any]:
    """Project persisted cost/PIT/snapshot/gate fields onto a run response.

    ``run_portfolio_backtest`` deliberately focuses on execution and returns a
    compact dictionary.  This projection keeps POST and subsequent GET views
    identical without recomputing any decision or benchmark data.
    """
    run_id = result.get("run_id")
    run = db.get(BacktestRun, run_id) if run_id is not None else None
    if run is None:
        if benchmark_name is not None:
            result.setdefault("benchmark", benchmark_name)
        return result

    cost = _json_value(getattr(run, "cost_config_json", None), {})
    if not isinstance(cost, dict):
        cost = {}
    # The service uses stamp_duty_rate internally; expose both historical
    # spellings while keeping one canonical response key.
    result["cost_config"] = cost
    # Historical rows created before the snapshot-backed chain have no
    # reliable replay contract. Keep the marker explicit even if an old DB
    # predates the new nullable columns.
    result["reproducibility_status"] = getattr(
        run, "reproducibility_status", None
    ) or ("reproducible" if run.strategy_snapshot_id else "legacy/non_reproducible")
    result["reproducibility_reason"] = getattr(run, "reproducibility_reason", None)
    commission_value = result.get("commission_rate")
    if commission_value is None:
        commission_value = cost.get("commission_rate", 0.0003)
    stamp_value = result.get("stamp_tax_rate")
    if stamp_value is None:
        stamp_value = cost.get("stamp_tax_rate", cost.get("stamp_duty_rate", 0.001))
    slippage_value = result.get("slippage_bps")
    if slippage_value is None:
        slippage_value = cost.get("slippage_bps", cost.get("slippage", cost.get("slippage_buy_bps", 5)))
    result["commission_rate"] = float(commission_value)
    result["stamp_tax_rate"] = float(stamp_value)
    result["slippage_bps"] = int(slippage_value)
    for key in ("price_type", "volume_limit_pct", "rebalance_frequency"):
        if result.get(key) is None and cost.get(key) is not None:
            result[key] = cost[key]

    snapshot = db.get(StrategyExecutionSnapshot, run.strategy_snapshot_id) if run.strategy_snapshot_id else None
    snapshot_versions = _json_value(getattr(snapshot, "versions_json", None), {}) if snapshot else {}
    if not isinstance(snapshot_versions, dict):
        snapshot_versions = {}
    result["benchmark"] = benchmark_name if benchmark_name is not None else result.get("benchmark")
    result["strategy_snapshot_id"] = run.strategy_snapshot_id
    result["factor_model_run_id"] = run.factor_model_run_id or (
        getattr(snapshot, "factor_model_run_id", None) if snapshot else None
    )
    result["factor_set_id"] = getattr(run, "factor_set_id", None) or (
        getattr(snapshot, "factor_set_id", None) if snapshot else None
    )
    result["factor_data_cutoff_at"] = (
        run.factor_data_cutoff_at.isoformat() if run.factor_data_cutoff_at else None
    )
    result["data_cutoff_at"] = run.data_cutoff_at.isoformat() if run.data_cutoff_at else None
    result["pit_mode"] = run.pit_mode or snapshot_versions.get("pit_mode")
    result["match_mode"] = getattr(run, "match_mode", None) or result.get("price_type")
    result["benchmark_code"] = (
        getattr(snapshot, "benchmark_code", None) if snapshot else None
    ) or result.get("benchmark_code")
    if result.get("benchmark") is None and result.get("benchmark_code"):
        result["benchmark"] = _BENCHMARK_CODE_TO_NAME.get(
            str(result["benchmark_code"]), str(result["benchmark_code"])
        )
    result["snapshot_no"] = getattr(snapshot, "snapshot_no", None) if snapshot else None
    result["snapshot_hash"] = getattr(snapshot, "snapshot_hash", None) if snapshot else None
    result["member_snapshot_json"] = run.member_snapshot_json
    result["excluded_members_json"] = run.excluded_members_json
    result["benchmark_equity_json"] = getattr(run, "benchmark_equity_json", None)
    result["benchmark_equity"] = _json_value(result["benchmark_equity_json"], []) or []
    result["benchmark_status"] = getattr(run, "benchmark_status", None)
    result["benchmark_gap_days"] = getattr(run, "benchmark_gap_days", None)
    result["is_result_production_eligible"] = bool(
        getattr(run, "is_result_production_eligible", 1)
    )

    gate_json = _json_value(getattr(snapshot, "gate_result_json", None), None) if snapshot else None
    result["gate_result_json"] = gate_json
    result["gate_policy_version"] = getattr(snapshot, "gate_policy_version", None) if snapshot else None
    if isinstance(gate_json, dict) and "pass" in gate_json:
        result["gate_result"] = "passed" if bool(gate_json.get("pass")) else "blocked"
    else:
        result["gate_result"] = result.get("gate_result")

    # Keep a compact, immutable data snapshot for the result drawer.  The raw
    # member JSON remains available separately for audit/export.
    result["data_snapshot"] = {
        "strategy_snapshot_id": run.strategy_snapshot_id,
        "snapshot_no": result.get("snapshot_no"),
        "snapshot_hash": result.get("snapshot_hash"),
        "data_cutoff_at": result.get("data_cutoff_at"),
        "factor_data_cutoff_at": result.get("factor_data_cutoff_at"),
        "member_count": len(_json_value(run.member_snapshot_json, []) or []),
        "symbol_ids": _json_value(run.symbol_ids_json, []) or [],
        "pit_mode": result.get("pit_mode"),
    }

    # A run with any non-ready DecisionRun is a blocked result, not an empty
    # signal result.  Preserve the first blocking reason for the UI.
    decision_ids = _json_value(getattr(run, "decision_run_ids_json", None), []) or []
    if decision_ids:
        dr_rows = db.execute(select(DecisionRun).where(DecisionRun.id.in_([str(v) for v in decision_ids]))).scalars().all()
        blocked = next((dr for dr in dr_rows if getattr(dr, "blocking_status", "READY") != "READY"), None)
        if blocked is not None:
            result["blocking_status"] = blocked.blocking_status
            result["blocking_reasons"] = _json_value(blocked.blocking_reasons_json, []) or []
        result["is_result_production_eligible"] = all(
            bool(getattr(dr, "is_result_production_eligible", 1)) for dr in dr_rows
        )
    elif isinstance(gate_json, dict) and gate_json.get("pass") is False:
        result["blocking_status"] = "GATE_BLOCKED"
        result["blocking_reasons"] = [
            warning for warning in (gate_json.get("warnings") or [])
            if isinstance(warning, dict) and warning.get("severity") == "blocking"
        ]
        result["is_result_production_eligible"] = False
    return result


@router.get("/backtest/runs/{run_id}/trades")
def list_backtest_trades(
    run_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None),
    symbol_id: int | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    execution_status: str | None = Query(default=None, description="filled|open|rejected"),
    sort_by: str = Query(default="signal_at", description="signal_at|execution_at|symbol_id|price|quantity|cost"),
    sort_dir: str = Query(default="desc", description="asc|desc"),
    db: Session = Depends(get_db),
):
    """Server-side pagination/filtering for the backtest transaction ledger."""
    if limit is not None:
        page_size = limit
        page = (offset // page_size) + 1
    normalized_sort_by = sort_by.strip().lower()
    sort_column = _BACKTEST_TRADE_SORT_COLUMNS.get(normalized_sort_by)
    if sort_column is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BACKTEST_TRADE_SORT_INVALID",
                "field": "sort_by",
                "value": sort_by,
                "allowed": sorted(_BACKTEST_TRADE_SORT_COLUMNS),
            },
        )
    normalized_sort_dir = sort_dir.strip().lower()
    if normalized_sort_dir not in _BACKTEST_TRADE_SORT_DIRECTIONS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BACKTEST_TRADE_SORT_INVALID",
                "field": "sort_dir",
                "value": sort_dir,
                "allowed": sorted(_BACKTEST_TRADE_SORT_DIRECTIONS),
            },
        )
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    where = [BacktestTrade.run_id == run_id]
    if symbol_id is not None:
        where.append(BacktestTrade.symbol_id == symbol_id)
    if start_date is not None:
        where.append(BacktestTrade.entry_date >= start_date)
    if end_date is not None:
        where.append(BacktestTrade.entry_date <= end_date)
    normalized_action = action.strip().upper() if action and action.strip() else None
    if normalized_action:
        if normalized_action == "BUY":
            where.append(BacktestTrade.exit_date.is_(None))
        elif normalized_action == "SELL":
            where.append(BacktestTrade.exit_date.is_not(None))
        else:
            normalized_action = None
    normalized_execution_status = (
        execution_status.strip().lower()
        if execution_status and execution_status.strip()
        else None
    )
    if normalized_execution_status:
        if normalized_execution_status == "open":
            where.append(BacktestTrade.exit_date.is_(None))
        elif normalized_execution_status == "filled":
            where.append(BacktestTrade.entry_price.is_not(None))
        elif normalized_execution_status == "rejected":
            where.append(BacktestTrade.entry_rejection_reason.is_not(None))
        else:
            normalized_execution_status = None
    total = int(db.scalar(select(func.count()).select_from(BacktestTrade).where(and_(*where))) or 0)
    order_by = (
        (asc(sort_column), asc(BacktestTrade.id))
        if normalized_sort_dir == "asc"
        else (desc(sort_column), desc(BacktestTrade.id))
    )
    rows = db.execute(
        select(BacktestTrade).where(and_(*where))
        .order_by(*order_by)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    evidence_decision_run_ids = _exact_evidence_decision_run_ids(db, rows)
    evidence_order_plan_fields = _order_plan_fields_by_evidence(db, rows)
    items = []
    for row in rows:
        item = BacktestTradeRead.model_validate(row).model_dump(mode="json")
        # A trade carries the immutable evidence primary keys generated by the
        # decision-driven matcher.  Resolve only those keys; never infer a run
        # from a symbol, action, or date.
        item["entry_decision_run_id"] = evidence_decision_run_ids.get(
            row.decision_evidence_id
        )
        item["exit_decision_run_id"] = evidence_decision_run_ids.get(
            row.exit_evidence_id
        )
        for prefix, evidence_id in (
            ("entry", row.decision_evidence_id),
            ("exit", row.exit_evidence_id),
        ):
            fields = evidence_order_plan_fields.get(str(evidence_id), {}) if evidence_id else {}
            item[f"{prefix}_requested_quantity"] = fields.get("requested_quantity")
            item[f"{prefix}_filled_quantity"] = fields.get("filled_quantity")
            item[f"{prefix}_remaining_quantity"] = fields.get("remaining_quantity")
            item[f"{prefix}_order_plan_status"] = fields.get("order_plan_status")
            item[f"{prefix}_unfilled_reason"] = fields.get("unfilled_reason")
            if prefix == "entry" and fields.get("intended_price") is not None:
                item["intended_entry_price"] = fields.get("intended_price")
            elif prefix == "exit" and fields.get("intended_price") is not None:
                item["intended_exit_price"] = fields.get("intended_price")
        items.append(item)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "sort": {
            "field": normalized_sort_by,
            "direction": normalized_sort_dir,
        },
        "filters": {
            "action": normalized_action,
            "symbol_id": symbol_id,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "execution_status": normalized_execution_status,
        },
        "items": items,
    }


@router.get("/backtest/runs/{run_id}/positions", response_model=BacktestPositionPage)
def list_backtest_positions(
    run_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    as_of_date: date | None = Query(default=None),
    status: str | None = Query(default=None, description="OPEN|CLOSED"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return a read-only daily holding ledger for one immutable run.

    A result row is deliberately not a ``BacktestTrade`` copy.  It is the
    day/symbol state reconstructed from that run's persisted lots: opening
    quantity, buys, sells, closing quantity and end-of-day weight.  The
    current portfolio is never consulted, so historical runs remain stable.
    """
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    # Route functions are also used as the in-process read seam in backtest
    # contract tests.  FastAPI resolves Query defaults for HTTP requests, but
    # a direct Python call receives the Query descriptor itself.
    if not isinstance(page, int):
        page = 1
    if not isinstance(page_size, int):
        page_size = 20
    if not isinstance(as_of_date, date):
        as_of_date = None
    if not isinstance(status, str):
        status = None

    # A run's final date is its immutable valuation boundary.  Do not let a
    # later query date pull in prices that did not belong to this execution.
    valuation_date = min(as_of_date or run.end_date, run.end_date)
    normalized_status = status.strip().upper() if status and status.strip() else None
    if normalized_status not in {None, "OPEN", "CLOSED"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BACKTEST_POSITION_STATUS_INVALID",
                "field": "status",
                "value": status,
                "allowed": ["OPEN", "CLOSED"],
            },
        )

    fills = db.execute(
        select(BacktestExecutionFill)
        .where(
            BacktestExecutionFill.run_id == run_id,
            BacktestExecutionFill.execution_date <= valuation_date,
        )
        .order_by(
            BacktestExecutionFill.execution_date,
            BacktestExecutionFill.symbol_id,
            BacktestExecutionFill.id,
        )
    ).scalars().all()

    # Historical runs predate the immutable fill ledger.  Keep their existing
    # read path available, but label it as an approximation rather than
    # presenting aggregate entry/exit lots as an exact partial-fill history.
    trades = db.execute(
        select(BacktestTrade)
        .where(
            BacktestTrade.run_id == run_id,
            BacktestTrade.entry_date <= valuation_date,
        )
        .order_by(BacktestTrade.symbol_id, BacktestTrade.entry_date, BacktestTrade.id)
    ).scalars().all()

    # An empty new run has an exact empty event ledger.  Approximation is only
    # necessary when a historical aggregate trade exists without its original
    # per-session fills.
    ledger_mode = (
        "EXECUTION_EVENTS"
        if fills or not trades
        else "LEGACY_TRADE_APPROXIMATION"
    )

    symbol_ids = (
        {int(fill.symbol_id) for fill in fills}
        if fills else {int(trade.symbol_id) for trade in trades}
    )
    bars = []
    if symbol_ids:
        bars = db.execute(
            select(DailyBar)
            .where(
                DailyBar.symbol_id.in_(symbol_ids),
                DailyBar.trade_date >= run.start_date,
                DailyBar.trade_date <= valuation_date,
            )
            .order_by(DailyBar.trade_date, DailyBar.symbol_id)
        ).scalars().all()

    valuation_snapshots = db.execute(
        select(BacktestValuationSnapshot)
        .where(
            BacktestValuationSnapshot.run_id == run_id,
            BacktestValuationSnapshot.trade_date <= valuation_date,
        )
        .order_by(
            BacktestValuationSnapshot.trade_date,
            BacktestValuationSnapshot.symbol_id,
        )
    ).scalars().all()
    valuation_by_day_symbol = {
        (snapshot.trade_date, int(snapshot.symbol_id)): snapshot
        for snapshot in valuation_snapshots
    }

    # Use the actual sessions available to this run.  Execution dates are the
    # precise daily ledger boundary for new runs; legacy lot dates remain an
    # audit guard for historical rows that have no event ledger.
    ledger_dates = {bar.trade_date for bar in bars}
    if fills:
        ledger_dates.update(fill.execution_date for fill in fills)
    else:
        for trade in trades:
            ledger_dates.add(trade.entry_date)
            if trade.exit_date is not None and trade.exit_date <= valuation_date:
                ledger_dates.add(trade.exit_date)
    ledger_dates = {
        trade_date for trade_date in ledger_dates
        if run.start_date <= trade_date <= valuation_date
    }

    buy_changes: dict[tuple[date, int], float] = {}
    sell_changes: dict[tuple[date, int], float] = {}
    buy_evidence_ids: dict[tuple[date, int], list[str]] = {}
    sell_evidence_ids: dict[tuple[date, int], list[str]] = {}
    if fills:
        for fill in fills:
            symbol_id = int(fill.symbol_id)
            quantity = max(0.0, float(fill.quantity or 0.0))
            key = (fill.execution_date, symbol_id)
            if str(fill.side).upper() == "BUY":
                buy_changes[key] = buy_changes.get(key, 0.0) + quantity
                if fill.decision_evidence_id:
                    buy_evidence_ids.setdefault(key, []).append(
                        str(fill.decision_evidence_id)
                    )
            elif str(fill.side).upper() == "SELL":
                sell_changes[key] = sell_changes.get(key, 0.0) + quantity
                if fill.decision_evidence_id:
                    sell_evidence_ids.setdefault(key, []).append(
                        str(fill.decision_evidence_id)
                    )
    else:
        for trade in trades:
            symbol_id = int(trade.symbol_id)
            quantity = max(0.0, float(trade.quantity or 0.0))
            buy_key = (trade.entry_date, symbol_id)
            buy_changes[buy_key] = buy_changes.get(buy_key, 0.0) + quantity
            if trade.decision_evidence_id:
                buy_evidence_ids.setdefault(buy_key, []).append(
                    str(trade.decision_evidence_id)
                )
            if trade.exit_date is not None and trade.exit_date <= valuation_date:
                sell_key = (trade.exit_date, symbol_id)
                sell_changes[sell_key] = sell_changes.get(sell_key, 0.0) + quantity
                if trade.exit_evidence_id:
                    sell_evidence_ids.setdefault(sell_key, []).append(
                        str(trade.exit_evidence_id)
                    )

    close_by_day_symbol = {
        (bar.trade_date, int(bar.symbol_id)): float(bar.close)
        for bar in bars
        if bar.close is not None
    }
    try:
        equity_curve = json.loads(run.equity_curve_json or "[]")
    except (TypeError, json.JSONDecodeError):
        equity_curve = []
    equity_by_date: dict[str, float] = {}
    if isinstance(equity_curve, list):
        for point in equity_curve:
            if not isinstance(point, dict):
                continue
            raw_date = point.get("date")
            raw_equity = point.get("equity")
            try:
                equity = float(raw_equity)
            except (TypeError, ValueError):
                continue
            if isinstance(raw_date, str) and equity == equity and abs(equity) != float("inf"):
                equity_by_date[raw_date] = equity

    # The persisted equity curve is the canonical session calendar for a run.
    # Keep a holding row on a session where an individual symbol's bar is
    # missing, but leave its price/value/weight unknown instead of filling it
    # from a prior close.
    for raw_date in equity_by_date:
        try:
            equity_date = date.fromisoformat(raw_date)
        except (TypeError, ValueError):
            continue
        if run.start_date <= equity_date <= valuation_date:
            ledger_dates.add(equity_date)

    # A final BacktestTrade row represents a durable lot.  Partial exits are
    # split into a closed child lot and an open remainder, so summing entry and
    # exit events rebuilds each past session without touching live holdings.
    quantities: dict[int, float] = {symbol_id: 0.0 for symbol_id in symbol_ids}
    candidates: list[dict[str, Any]] = []
    for trade_date in sorted(ledger_dates):
        for symbol_id in sorted(symbol_ids):
            close = close_by_day_symbol.get((trade_date, symbol_id))
            opening_quantity = quantities.get(symbol_id, 0.0)
            buy_quantity = buy_changes.get((trade_date, symbol_id), 0.0)
            sell_quantity = sell_changes.get((trade_date, symbol_id), 0.0)
            closing_quantity = max(
                0.0,
                opening_quantity + buy_quantity - sell_quantity,
            )
            quantities[symbol_id] = closing_quantity
            if opening_quantity <= 0 and buy_quantity <= 0 and sell_quantity <= 0:
                continue
            valuation = valuation_by_day_symbol.get((trade_date, symbol_id))
            if valuation is not None:
                mark_price = valuation.mark_price
                market_value = valuation.market_value
                equity = valuation.portfolio_equity
                weight = valuation.weight
            else:
                mark_price = close
                market_value = (
                    round(mark_price * closing_quantity, 8)
                    if mark_price is not None and closing_quantity > 0
                    else (0.0 if closing_quantity <= 0 else None)
                )
                equity = equity_by_date.get(trade_date.isoformat())
                weight = (
                    round(market_value / equity, 8)
                    if market_value is not None and equity is not None
                    else None
                )
            row = BacktestPositionRead(
                run_id=int(run_id),
                symbol_id=symbol_id,
                trade_date=trade_date,
                opening_quantity=round(opening_quantity, 8),
                buy_quantity=round(buy_quantity, 8),
                sell_quantity=round(sell_quantity, 8),
                closing_quantity=round(closing_quantity, 8),
                status="OPEN" if closing_quantity > 0 else "CLOSED",
                as_of_date=valuation_date,
                mark_price=mark_price,
                market_value=market_value,
                portfolio_equity=equity,
                weight=weight,
                buy_evidence_ids=sorted(set(buy_evidence_ids.get((trade_date, symbol_id), []))),
                sell_evidence_ids=sorted(set(sell_evidence_ids.get((trade_date, symbol_id), []))),
            ).model_dump(mode="json")
            if normalized_status is None or row["status"] == normalized_status:
                candidates.append(row)

    candidates.sort(key=lambda item: (item["trade_date"], item["symbol_id"]), reverse=True)
    total = len(candidates)
    start = (page - 1) * page_size
    items = candidates[start : start + page_size]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "as_of_date": valuation_date.isoformat(),
        "status": normalized_status,
        "ledger_mode": ledger_mode,
        "items": items,
    }


@router.get("/backtest/runs/{run_id}/evidence")
def list_backtest_evidence(
    run_id: int,
    action: str | None = Query(default=None, description="REJECTED,DATA_BLOCKED 等逗号分隔动作"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Paginated DecisionEvidence restricted to the run's persisted links."""
    if limit is not None:
        page_size = limit
        page = (offset // page_size) + 1
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    ctx = _backtest_decision_context(db, run)
    run_ids = ctx["decision_run_ids"]
    if not run_ids:
        return {"total": 0, "page": page, "page_size": page_size, "items": [], "decision_run_ids": []}
    where = [DecisionEvidence.decision_run_id.in_(run_ids)]
    if action:
        tokens = [token.strip().upper() for token in action.split(",") if token.strip()]
        if tokens:
            action_predicates = [DecisionEvidence.action.in_(tokens)]
            if "REJECTED" in tokens:
                # A rejected order remains BUY/SELL at decision time.  It is
                # surfaced in the rejection tab from its execution status.
                action_predicates.append(and_(
                    DecisionEvidence.action.in_(("BUY", "SELL")),
                    _order_plan_status_expression(db) == "REJECTED",
                ))
            where.append(or_(*action_predicates))
    total = int(
        db.scalar(
            select(func.count()).select_from(DecisionEvidence).where(and_(*where))
        )
        or 0
    )
    rows = db.execute(
        select(DecisionEvidence).where(and_(*where))
        .order_by(DecisionEvidence.trade_date, DecisionEvidence.symbol_id)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "decision_run_ids": run_ids,
        "items": [DecisionEvidenceRead.model_validate(row).model_dump(mode="json") for row in rows],
    }


@router.get("/backtest/runs/{run_id}", response_model=BacktestRunDetail)
def get_backtest_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    # BT-UI-08: this endpoint is a run summary/read-only snapshot surface.
    # The transaction ledger must be read exclusively through the paginated
    # /trades endpoint so opening a historical run cannot load years of rows.
    extra = build_backtest_detail_context(db, run, [])
    extra.pop("trade_annotations", None)
    decision_ctx = _backtest_decision_context(db, run)
    contract = _enrich_backtest_result_contract(db, {"run_id": run.id})
    detail_payload = dict(run.__dict__)
    detail_payload.update(extra)
    detail_payload.update({k: v for k, v in contract.items() if k != "run_id"})
    detail_payload.update(
        decision_run_ids=decision_ctx["decision_run_ids"],
        evidence_summary=decision_ctx["evidence_summary"],
        rejected_count=decision_ctx["rejected_count"],
        decision_snapshot=decision_ctx["decision_snapshot"],
    )
    return BacktestRunDetail(**detail_payload)


@router.delete("/backtest/runs/{run_id}")
def delete_backtest_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    db.delete(run)
    db.commit()
    return {"success": True}


@router.post("/backtest/runs/{run_id}/apply-to-portfolio", response_model=BacktestApplyResult)
def apply_backtest_to_portfolio(
    run_id: int,
    payload: BacktestApplyRequest,
    db: Session = Depends(get_db),
):
    """P2-1：将回测结果应用到模拟组合。

    将回测的每笔交易重放为 SimOrder/SimTrade，重建组合的持仓、现金、交易历史。
    时间戳使用回测的 entry_date/exit_date，不污染最近交易统计。

    clear_existing=False 时若目标组合已有 sim orders，返回 409。
    """
    try:
        result = apply_backtest_run_to_portfolio(
            db=db,
            run_id=run_id,
            portfolio_id=payload.portfolio_id,
            clear_existing=payload.clear_existing,
        )
        return BacktestApplyResult(**result)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg or "not simulated" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "already has sim orders" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "status is" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as exc:
        logging.getLogger(__name__).exception("应用回测到组合失败")
        raise HTTPException(status_code=500, detail="应用回测到组合失败，请稍后重试") from exc



@router.post("/backtest/portfolio/run", response_model=PortfolioBacktestResult)
def create_portfolio_backtest_run(payload: PortfolioBacktestRequest, db: Session = Depends(get_db)):
    """P2-2：组合整体回测。

    自动从组合持仓 + 最新 scan executable 候选推导 symbol_ids，
    自动构造与 auto_trade 信号逻辑一致的 rule_config（基于 Score.action），
    复用 portfolio 的 active rule 限制仓位与持仓数。

    前提：portfolio 必须是 simulated 账户且 auto_trade_enabled=1。
    否则回测的信号源（Score.action）与实际执行逻辑不一致，结果无意义。

    WP7.3：接受 only_auto 参数，仅在 member 来源生效时跳过 manual/confirm 成员。
    """
    try:
        # WP0-5 Step 3：路由契约对齐（TR-05.3 8 参数传递；only_auto / current_universe 已从 schema 移除）
        result = run_portfolio_backtest(
            db=db,
            portfolio_id=payload.portfolio_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            run_name=payload.run_name,
            benchmark=payload.benchmark,
            strategy_snapshot_id=payload.strategy_snapshot_id,
            score_weight_mode=payload.score_weight_mode,
            factor_model_run_id=payload.factor_model_run_id,
            # WP0-5 C-05 契约参数 8 项（显式透传；run_portfolio_backtest 内部取默认值兜底）
            initial_capital=getattr(payload, "initial_capital", None),
            commission_rate=getattr(payload, "commission_rate", 0.0003),
            stamp_tax_rate=getattr(payload, "stamp_tax_rate", 0.001),
            slippage_bps=getattr(payload, "slippage_bps", 5),
            price_type=getattr(payload, "price_type", "NEXT_OPEN"),
            volume_limit_pct=getattr(payload, "volume_limit_pct", 0.10),
            rebalance_frequency=getattr(payload, "rebalance_frequency", "on_signal"),
            pit_mode=getattr(payload, "pit_mode", "legacy_research"),
        )
        # Re-project persisted run metadata so POST and historical GET expose
        # the same cost/benchmark/PIT/snapshot/gate contract.
        result = _enrich_backtest_result_contract(
            db,
            result,
            benchmark_name=payload.benchmark,
        )
        return PortfolioBacktestResult(**result)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "auto_trade_enabled is 0" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "is not simulated" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "total_capital is" in msg or "Cannot run whole-portfolio backtest" in msg:
            raise HTTPException(status_code=400, detail=msg)
        if "manual/confirm 成员" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as exc:
        logging.getLogger(__name__).exception("组合整体回测执行失败")
        raise HTTPException(status_code=500, detail="组合整体回测执行失败，请稍后重试") from exc


# ----------------------------------------------------------------------------
# WP7.4：组合回测来源状态 + 新旧引擎对比
# ----------------------------------------------------------------------------

_PORTFOLIO_BACKTEST_SOURCE_ENV_FLAG = "PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED"


def _get_portfolio_or_404(db: Session, portfolio_id: int) -> Portfolio:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id} not found")
    return portfolio


@router.get(
    "/portfolios/{portfolio_id}/backtest/source-status",
    response_model=PortfolioBacktestSourceStatus,
    tags=["backtest"],
)
def get_portfolio_backtest_source_status(
    portfolio_id: int,
    db: Session = Depends(get_db),
):
    """WP7.4 组合回测标的来源开关状态。

    返回 {enabled, env_flag, source_label}：
    - enabled：PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED 当前是否开启
    - env_flag：环境变量名（便于 UI 展示）
    - source_label：当前生效的来源标签 "legacy" / "members"
    """
    _get_portfolio_or_404(db, portfolio_id)
    enabled = bool(settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED)
    return PortfolioBacktestSourceStatus(
        enabled=enabled,
        env_flag=_PORTFOLIO_BACKTEST_SOURCE_ENV_FLAG,
        source_label="members" if enabled else "legacy",
    )


@router.post(
    "/portfolios/{portfolio_id}/backtest/compare",
    response_model=PortfolioBacktestCompareResult,
    tags=["backtest"],
)
def compare_portfolio_backtest_engines(
    portfolio_id: int,
    payload: PortfolioBacktestCompareRequest,
    db: Session = Depends(get_db),
):
    """WP7.4 新旧引擎对比。

    使用相同日期/资金/成本对比新旧来源回测结果：
    - 旧来源（legacy）：持仓 + 最新 scan 候选池
    - 新来源（members）：按有效日期读取历史成员

    返回 {old, new, diff}，详见 PortfolioBacktestCompareResult。
    """
    _get_portfolio_or_404(db, portfolio_id)
    if payload.portfolio_id != portfolio_id:
        raise HTTPException(
            status_code=400,
            detail="payload.portfolio_id must match path portfolio_id",
        )
    try:
        result = compare_new_old_engine(
            db=db,
            portfolio_id=portfolio_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            initial_capital=payload.initial_capital,
            run_name_prefix=payload.run_name_prefix,
        )
        return PortfolioBacktestCompareResult(**result)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "auto_trade_enabled is 0" in msg or "is not simulated" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "total_capital is" in msg or "Cannot run whole-portfolio backtest" in msg:
            raise HTTPException(status_code=400, detail=msg)
        if "manual/confirm 成员" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as exc:
        logging.getLogger(__name__).exception("组合回测新旧引擎对比失败")
        raise HTTPException(
            status_code=500,
            detail="组合回测新旧引擎对比失败，请稍后重试",
        ) from exc



def _format_template(template: BacktestRuleTemplate) -> dict:
    try:
        rule_config = json.loads(template.rule_config) if isinstance(template.rule_config, str) else template.rule_config
    except (json.JSONDecodeError, TypeError):
        rule_config = {}
    return {
        "id": template.id,
        "name": template.name,
        "description": template.description or "",
        "rule_config": rule_config,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


@router.get("/backtest/templates", response_model=list[RuleTemplateResponse])
def list_rule_templates(db: Session = Depends(get_db)):
    templates = db.execute(
        select(BacktestRuleTemplate).order_by(desc(BacktestRuleTemplate.updated_at))
    ).scalars().all()
    return [_format_template(template) for template in templates]


@router.post("/backtest/templates", response_model=RuleTemplateResponse, status_code=201)
def create_rule_template(payload: RuleTemplateCreate, db: Session = Depends(get_db)):
    existing = db.execute(
        select(BacktestRuleTemplate).where(BacktestRuleTemplate.name == payload.name)
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Template name already exists")
    template = BacktestRuleTemplate(
        name=payload.name,
        description=payload.description,
        rule_config=json.dumps(payload.rule_config, ensure_ascii=False),
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return _format_template(template)


@router.put("/backtest/templates/{template_id}", response_model=RuleTemplateResponse)
def update_rule_template(template_id: int, payload: RuleTemplateUpdate, db: Session = Depends(get_db)):
    template = db.get(BacktestRuleTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    if payload.name is not None:
        template.name = payload.name
    if payload.description is not None:
        template.description = payload.description
    if payload.rule_config is not None:
        template.rule_config = json.dumps(payload.rule_config, ensure_ascii=False)
    db.commit()
    db.refresh(template)
    return _format_template(template)


@router.delete("/backtest/templates/{template_id}")
def delete_rule_template(template_id: int, db: Session = Depends(get_db)):
    template = db.get(BacktestRuleTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    db.delete(template)
    db.commit()
    return {"success": True}
