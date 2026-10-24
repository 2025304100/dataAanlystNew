"""G5 归档核验 API；无真实执行器时兼容启动入口明确拒绝。

路由（挂在 api_prefix 下，路径前缀 /portfolios/{pid}）：

  POST /portfolios/{pid}/g5-dual-run
    保留旧请求结构用于兼容；当前固定返回 503 G5_REAL_RUNNER_UNAVAILABLE。
    不解析任意版本字符串、不创建异步任务、不生成模拟报告。

  GET  /portfolios/{pid}/g5-dual-run/{replay_id}
    → 查询真实任务状态或核验现有归档；当前证据范围不支持正式引擎链准入。

说明：查询只返回经真实执行证据复核的归档报告；当前没有正式对比执行器，启动入口明确拒绝。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.portfolio import Portfolio
from app.services.g5_dual_run_audit import validate_g5_report
from app.services.g5_dual_run_replay import ACTION_TYPES, get_g5_acceptance_policy

router = APIRouter(tags=["g5-dual-run"])


# ──────────────────────────────────────────────────────────── Schemas


class G5DualRunLaunchRequest(BaseModel):
    start_date: date = Field(..., description="起始交易日 T1")
    end_date: date = Field(..., description="兼容区间元数据；当前无执行器，不据此启动回放")
    old_engine_version: str = Field(..., description="兼容旧版本元数据；不是可解析执行方案，当前入口拒绝启动", deprecated=True)
    new_engine_version: str = Field(..., description="兼容候选版本元数据；手填字符串不构成真实执行器证明", deprecated=True)
    run_async: bool = Field(default=True, description="仅保留旧请求兼容；不承诺异步能力，当前不会创建任务", deprecated=True)
    strict_match_p0_threshold: float | None = Field(default=None, description="仅保留旧请求兼容；验收阈值由后端版本化策略决定", deprecated=True)


class G5DualRunLaunchResponse(BaseModel):
    replay_id: str
    portfolio_id: int
    start_date: date
    end_date: date
    expected_trade_days: int
    async_task_id: str | None = None
    correlation_id: str
    is_terminal_locked: bool = False
    terminal_lock_reason: str | None = None
    status: str = "SUBMITTED"


class DailyDualRunReportResponse(BaseModel):
    trade_date: date
    action_match_rate: float | None
    universe_jaccard: float | None
    p0_unexplained_count: int
    p1_hold_noaction_flip: int
    # 与逐日报告和前端一致：matrix[旧动作][新动作]。
    action_confusion_matrix: dict[str, dict[str, int]]
    summary_notes: list[str] = Field(default_factory=list)
    universe_union_count: int
    universe_intersection_count: int
    action_denominator: int
    action_match_count: int
    valid_symbol_days: int
    chain_a_checked_count: int
    chain_b_checked_count: int
    chain_a_coverage_pct: float
    chain_b_coverage_pct: float
    sample_status: str
    metric_status: str
    chain_a_meta: dict[str, Any]
    chain_b_meta: dict[str, Any]
    input_manifest: list[dict[str, Any]]
    symbol_diffs: list[dict[str, Any]] = Field(default_factory=list)
    attributions: list[dict[str, Any]] = Field(default_factory=list)


class G5DualRunSummaryResponse(BaseModel):
    portfolio_id: int
    replay_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    total_days: int | None = None
    days_replayed: int | None = None
    skipped_days: list[date] = Field(default_factory=list)
    daily_reports: list[DailyDualRunReportResponse] = Field(default_factory=list)
    avg_action_match_rate: float | None = None
    avg_universe_jaccard: float | None = None
    total_p0_unexplained: int | None = None
    total_p1_hold_noaction_flip: int | None = None
    failing_days_p0: list[date] = Field(default_factory=list)
    failing_days_p1: list[date] = Field(default_factory=list)
    g5_eligible_for_g6: bool
    evidence_verified: bool = False
    summary_notes: list[str] = Field(default_factory=list)
    is_terminal_locked: bool = False
    terminal_lock_reason: str | None = None
    status: str
    job_status: str
    verdict: str
    g5_validation_errors: list[str] = Field(default_factory=list)
    storage_errors: list[dict] = Field(default_factory=list)
    acceptance_policy_version: str | None = None
    acceptance_policy: dict | None = None
    applicable_scope: dict | None = None
    valid_symbol_days: int | None = None
    evidence_scope: str = "decision_actions_only"
    acceptance_blockers: list[str] = Field(default_factory=list)
    acceptance_blocker_codes: list[str] = Field(default_factory=list)
    provenance: dict | None = None
    async_task_id: str | None = None
    audit_report_id: int | None = None
    audit_report_hash: str | None = None
    action_order: list[str] = Field(default_factory=lambda: list(ACTION_TYPES))
    matrix_scope: str = "all_days"
    aggregate_action_confusion_matrix: dict[str, dict[str, int]] | None = None


# ──────────────────────────────────────────────────────────── Helpers


def _load_portfolio(db: Session, portfolio_id: int) -> Portfolio:
    p = db.get(Portfolio, int(portfolio_id))
    if p is None:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id} not found")
    if p.account_type != "simulated":
        raise HTTPException(status_code=400, detail=f"Portfolio {portfolio_id} is not simulated")
    return p


def _result_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={
        "error_code": "G5_RESULT_UNAVAILABLE", "title_zh": "暂时读不到对比报告",
        "detail_zh": "报告存储读取失败，还不能确认结果。请稍后重新查询。",
    })


def _dict_to_summary_response(data: dict[str, Any], *, replay_id: str) -> G5DualRunSummaryResponse:
    """Serialize only checked original data; missing fields are errors."""
    matrix = {a: {b: sum(day.get("action_confusion", day.get("action_confusion_matrix"))[a][b]
                              for day in data["daily_reports"]) for b in ACTION_TYPES} for a in ACTION_TYPES}
    return G5DualRunSummaryResponse.model_validate({**data, "replay_id": replay_id,
        "aggregate_action_confusion_matrix": matrix, "daily_reports": [
        {**day, "action_confusion_matrix": day.get("action_confusion", day.get("action_confusion_matrix")),
         "summary_notes": day.get("summary_notes", day.get("warnings", [])),
         "attributions": [{"category": item["reason_category"], "symbol_id": item["symbol_id"],
             "summary": f"{item['chain_a']['action']} → {item['chain_b']['action']}",
             "review_status": "REQUIRES_REVIEW", "change_note": None} for item in day.get("symbol_diffs", [])]}
        for day in data["daily_reports"]
    ]})


@router.get("/portfolios/{portfolio_id}/g5-dual-run/capabilities")
def get_g5_capabilities(portfolio_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    _load_portfolio(db, portfolio_id)
    return {"real_runner_available": False, "old_chain": None, "candidate_chains": [],
            "comparison_kind": "engine_source", "acceptance_policy": get_g5_acceptance_policy(),
            "acceptance_policy_version": get_g5_acceptance_policy()["version"],
            "precheck_available": True, "history_available": True,
            "message": "当前还不能实际运行新旧方案对比，暂不能据此切换方案。"}


class G5PrecheckRequest(BaseModel):
    start_date: date
    end_date: date
    baseline_snapshot_id: str | None = None
    candidate_snapshot_id: str | None = None
    acceptance_policy_version: str


@router.post("/portfolios/{portfolio_id}/g5-dual-run/precheck")
def precheck_g5_dual_run(portfolio_id: int, payload: G5PrecheckRequest, db: Session = Depends(get_db)) -> dict:
    _load_portfolio(db, portfolio_id)
    from app.models.market_data import MarketCalendar
    from app.models.decision_engine import StrategyExecutionSnapshot
    from app.services.portfolio_binding_review import _last_accepted_binding
    from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
    with db.no_autoflush:
        calendar = resolve_inspection_calendar(db)
        dates = list(db.scalars(select(MarketCalendar.trade_date).where(
            MarketCalendar.trade_date >= payload.start_date, MarketCalendar.trade_date <= payload.end_date,
            MarketCalendar.is_trading_day == 1).order_by(MarketCalendar.trade_date))) if calendar["status"] == "PASSED" else []
        accepted = _last_accepted_binding(db, portfolio_id)
        baseline = db.get(StrategyExecutionSnapshot, payload.baseline_snapshot_id) if payload.baseline_snapshot_id else None
        candidate = db.scalar(select(StrategyExecutionSnapshot).where(StrategyExecutionSnapshot.portfolio_id == portfolio_id,
            StrategyExecutionSnapshot.snapshot_type == "save_and_apply").order_by(StrategyExecutionSnapshot.snapshot_no.desc(),
            StrategyExecutionSnapshot.created_at.desc(), StrategyExecutionSnapshot.id.desc()).limit(1))
    checks = [{"key": "runner", "passed": False, "reason_code": "G5_REAL_RUNNER_UNAVAILABLE",
               "reason": "当前没有接通真实双链执行器；预检不能替代真实执行"},
              {"key": "calendar", "passed": calendar["status"] == "PASSED", "actual": calendar.get("version")},
              {"key": "policy", "passed": payload.acceptance_policy_version == get_g5_acceptance_policy()["version"],
               "required": get_g5_acceptance_policy()},
              {"key": "interval", "passed": payload.end_date >= payload.start_date and len(dates) >= get_g5_acceptance_policy()["min_trade_days"],
               "actual": len(dates) if calendar["status"] == "PASSED" else None,
               "required": get_g5_acceptance_policy()["min_trade_days"]},
              {"key": "baseline", "passed": bool(baseline and baseline.portfolio_id == portfolio_id and accepted
                  and accepted.get("snapshot_id") == baseline.id), "actual": baseline.id if baseline and baseline.portfolio_id == portfolio_id else None},
              {"key": "candidate", "passed": bool(candidate and candidate.id == payload.candidate_snapshot_id),
               "actual": candidate.id if candidate else None},
              {"key": "input_evidence", "passed": None, "reason": "尚无真实预执行器，不能确认双侧实际输入与有效样本"}]
    return {"portfolio_id": portfolio_id, "can_run": False, "precheck_snapshot_id": None,
            "checks": checks, "expected_trade_dates": [day.isoformat() for day in dates],
            "calendar_version": calendar.get("version"), "acceptance_policy": get_g5_acceptance_policy(),
            "baseline_snapshot_id": payload.baseline_snapshot_id, "candidate_snapshot_id": payload.candidate_snapshot_id}


@router.get("/portfolios/{portfolio_id}/g5-dual-run/reports")
def list_g5_reports(portfolio_id: int, limit: int = 20, db: Session = Depends(get_db)) -> dict:
    from app.models.g5_dual_run import G5DualRunReport
    _load_portfolio(db, portfolio_id)
    items = []
    with db.no_autoflush:
        rows = db.scalars(select(G5DualRunReport).where(G5DualRunReport.portfolio_id == portfolio_id)
            .order_by(G5DualRunReport.created_at.desc(), G5DualRunReport.id.desc()).limit(max(1, min(limit, 100))))
        for row in rows:
            checked = validate_g5_report(db, row, portfolio_id=portfolio_id)
            items.append({"report_id": row.id, "replay_id": checked.get("replay_id") or f"report:{row.id}",
                "original_hash": row.report_hash,
                "start_date": row.start_date, "end_date": row.end_date, "created_at": row.created_at,
                "job_status": checked["job_status"], "verdict": checked["verdict"],
                "evidence_verified": checked["evidence_verified"], "g5_eligible_for_g6": checked["g5_eligible_for_g6"],
                "g5_validation_errors": checked["g5_validation_errors"], "storage_errors": checked["storage_errors"],
                "evidence_scope": checked["evidence_scope"], "acceptance_blockers": checked["acceptance_blockers"],
                "acceptance_blocker_codes": checked["acceptance_blocker_codes"],
                "applicable_scope": checked.get("applicable_scope"), "valid_symbol_days": checked.get("valid_symbol_days")})
    return {"portfolio_id": portfolio_id, "reports": items}


# ──────────────────────────────────────────────────────────── POST /portfolios/{pid}/g5-dual-run


@router.post(
    "/portfolios/{portfolio_id}/g5-dual-run",
    response_model=G5DualRunLaunchResponse,
    status_code=200,
    summary="G5：兼容启动入口（无真实执行器，明确拒绝）",
)
def launch_g5_dual_run(
    portfolio_id: int,
    payload: G5DualRunLaunchRequest,
    db: Session = Depends(get_db),
) -> G5DualRunLaunchResponse:
    _load_portfolio(db, portfolio_id)
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="结束日期不能早于开始日期")
    if not payload.old_engine_version.strip() or not payload.new_engine_version.strip():
        raise HTTPException(status_code=400, detail="请填写新旧方案版本")
    raise HTTPException(status_code=503, detail={
        "error_code": "G5_REAL_RUNNER_UNAVAILABLE", "title_zh": "暂不能运行新旧方案对比",
        "detail_zh": "当前还不能实际运行新旧方案对比，不会生成示意报告，也不能据此切换方案。",
    })


# ──────────────────────────────────────────────────────────── GET /portfolios/{pid}/g5-dual-run/{replay_id}


@router.get(
    "/portfolios/{portfolio_id}/g5-dual-run/{replay_id}",
    response_model=G5DualRunSummaryResponse,
    status_code=200,
    summary="G5：查询双跑对账结果汇总",
)
def query_g5_dual_run_result(
    portfolio_id: int,
    replay_id: str,
    db: Session = Depends(get_db),
) -> G5DualRunSummaryResponse:
    from app.models.g5_dual_run import G5DualRunReport

    p = _load_portfolio(db, portfolio_id)
    # replay_id 存储在 report_json 中（模型表未单独建列），遍历匹配
    try:
        records = db.execute(
            select(G5DualRunReport)
            .where(G5DualRunReport.portfolio_id == portfolio_id)
            .order_by(G5DualRunReport.created_at.desc(), G5DualRunReport.id.desc())
        ).scalars().all()
    except SQLAlchemyError as exc:
        raise _result_unavailable() from exc

    target = None
    payload_data: dict[str, Any] | None = None
    for r in records:
        try:
            data = json.loads(r.report_json) if r.report_json else {}
        except (TypeError, ValueError):
            data = {}
        if isinstance(data, dict) and (data.get("replay_id") == replay_id or replay_id == f"report:{r.id}"):
            target = r
            payload_data = data
            break

    if target is None:
        from app.models.async_task import AsyncTaskRecord
        try:
            with db.no_autoflush:
                tasks = db.scalars(select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == "g5_dual_run")
                    .order_by(AsyncTaskRecord.created_at.desc(), AsyncTaskRecord.id.desc()))
                for task in tasks:
                    try:
                        request = json.loads(task.payload_json or "{}")
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(request, dict) or request.get("portfolio_id") != portfolio_id or request.get("replay_id") != replay_id:
                        continue
                    if task.status in {"done", "partial"}:
                        raise HTTPException(409, detail={"error_code": "G5_REPORT_INVALID", "job_status": "COMPLETED",
                            "detail_zh": "任务已结束但没有完整报告，不能作为准入依据"})
                    job_status = {"queued": "SUBMITTED", "running": "RUNNING", "failed": "FAILED", "cancelled": "CANCELLED"}.get(task.status, "INVALID")
                    return G5DualRunSummaryResponse(portfolio_id=portfolio_id, replay_id=replay_id,
                        status=job_status, job_status=job_status, verdict="NOT_COMPLETED", async_task_id=task.id,
                        g5_eligible_for_g6=False, evidence_verified=False,
                        summary_notes=["当前显示真实任务状态；尚无可用于准入的完整报告"])
        except SQLAlchemyError as exc:
            raise _result_unavailable() from exc
        raise HTTPException(status_code=404, detail={
            "error_code": "G5_REPORT_NOT_FOUND", "title_zh": "报告不存在或已清理",
            "detail_zh": "没有找到当前组合的这份对比报告，不能据此切换方案。",
        })

    # 从持久化 JSON 重建响应（直接用 dict 构造，跳过不存在的 from_dict）
    try:
        if payload_data:
            if payload_data.get("status") in {"SUBMITTED", "RUNNING", "FAILED", "CANCELLED"} and payload_data.get("execution_mode") == "async":
                from app.models.async_task import AsyncTaskRecord
                task = db.get(AsyncTaskRecord, payload_data.get("async_task_id")) if payload_data.get("async_task_id") else None
                request = json.loads(task.payload_json or "{}") if task else {}
                if task and isinstance(request, dict) and request.get("portfolio_id") == portfolio_id and request.get("replay_id") == replay_id and task.status in {"queued", "running", "failed", "cancelled"}:
                    job_status = {"queued": "SUBMITTED", "running": "RUNNING", "failed": "FAILED", "cancelled": "CANCELLED"}[task.status]
                    return G5DualRunSummaryResponse(portfolio_id=portfolio_id, replay_id=replay_id,
                        status=job_status, job_status=job_status, verdict="NOT_COMPLETED", async_task_id=task.id,
                        g5_eligible_for_g6=False, evidence_verified=False,
                        summary_notes=["当前显示真实任务状态；归档占位记录尚不构成可审核的完整报告"])
            _logger = logging.getLogger(__name__)
            _logger.info("G5 GET payload: %s", json.dumps({
                'g5_eligible_for_g6': payload_data.get("g5_eligible_for_g6"),
                'notes': payload_data.get("summary_notes"),
                'total_days': payload_data.get("total_days"),
                'days_replayed': payload_data.get("days_replayed"),
                'skipped_days_len': len(payload_data.get("skipped_days") or []),
                'p0': payload_data.get("total_p0_unexplained"),
                'p1': payload_data.get("total_p1_hold_noaction_flip"),
            }, ensure_ascii=False))
            checked = validate_g5_report(db, target, portfolio_id=portfolio_id)
            if checked["storage_errors"]:
                raise _result_unavailable()
            if not checked["evidence_verified"]:
                raise HTTPException(status_code=409, detail={
                    "error_code": "G5_REPORT_INVALID", "title_zh": "报告缺少可核验的真实执行证据",
                    "detail_zh": "不能据此切换方案。原始报告已保留。",
                    "reasons": checked["g5_validation_errors"],
                })
            resp = _dict_to_summary_response(checked, replay_id=replay_id)
            resp.evidence_verified = True
            _logger.info("G5 GET response: g5_eligible_for_g6=%s", resp.g5_eligible_for_g6)
            return resp
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise _result_unavailable() from exc
    except Exception:
        logging.getLogger(__name__).exception("G5 _dict_to_summary_response failed")

    raise HTTPException(status_code=409, detail={
        "error_code": "G5_REPORT_INVALID", "title_zh": "报告无法读取",
        "detail_zh": "这份报告内容不完整或已损坏，不能据此切换方案。原始记录已保留。",
    })
