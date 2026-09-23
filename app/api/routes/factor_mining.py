"""挖掘域 HTTP 路由（设计文档 §8.2，模块 M5/M7，任务卡 A3）。

⚠️ 本项目的路由范式（设计文档 §2.1 第 27 条）：
   `router = APIRouter()` **无 prefix**，装饰器里写**全路径**。
   前缀 `/api/v1` 由 `app/api/router.py:7` 统一挂载。

⚠️ 裸眼可见的分层纪律（R5）：本文件只做 DTO 转换与入参校验，
   **不得**出现阈值、切分、指标计算（业务全部委托 service/task_runner）。

A3 完成度：19 处路由占位全部清零；drafts/templates/cleanup 族属 B4（not_do）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.async_task import AsyncTaskRecord
from app.schemas.errors import FactorSevenError, factor_structured_error
from app.services.factors.mining import draft_service as mining_draft_service
from app.services.factors.mining import service as mining_service
from app.services.factors.mining import task_lock as mining_lock
from app.services.factors.mining import task_runner as mining_runner
from app.services.factors.mining import template_service as mining_template_service

router = APIRouter()


def _factor_seven_http(exc: FactorSevenError) -> HTTPException:
    """`FactorSevenError` 转 HTTP（沿用 mining_candidate_pool 的状态码映射）。"""
    from app.api.routes.mining_candidate_pool import _http_status

    # 注意：`return X from Y` 不是合法语法（`from` 只跟在 raise 后），
    # 异常链由调用方 `raise _factor_seven_http(exc) from exc` 建立
    return HTTPException(status_code=_http_status(exc.error_code),
                         detail=exc.to_dict())


def _not_found(kind: str, object_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail={
        "error_code": "NOT_FOUND",
        "title_zh": "资源不存在",
        "detail_zh": f"未找到{kind} {object_id}。",
        "impact": "本次查询无结果",
        "fix_link": "/settings/factor-mining",
        "retryable": False,
    })


# ══════════════════════════════════════════════════════════
# 请求/响应 DTO
# ══════════════════════════════════════════════════════════


class MiningRunCreate(BaseModel):
    draft_id: str | None = None
    template_id: str | None = None
    candidate_pool_snapshot_id: str = Field(..., min_length=1)
    data_cutoff_at: datetime
    start_date: datetime
    end_date: datetime
    rebalance_frequency: Literal["daily", "weekly", "monthly"]
    target_horizon: int = Field(default=5, ge=1, le=20)
    train_ratio: float = Field(default=0.6, gt=0, lt=1)
    validation_ratio: float = Field(default=0.2, gt=0, lt=1)
    random_seed: int = 42
    evolution_params: dict[str, Any] = Field(default_factory=dict)
    filter_config: dict[str, Any] = Field(default_factory=dict)


class MiningRunCreated(BaseModel):
    run_id: str
    task_id: str
    queue_position: int = 0
    eta_seconds: int | None = None
    #: 切分预算（Step2/Step4 展示）；由候选池/校验接口提供，本接口不重复计算（R5）
    split_budget: dict[str, Any] | None = None


class Page(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


class ManualGradeIn(BaseModel):
    grade: Literal["S", "A", "B", "C", "D"]
    reason: str = Field(..., min_length=10, max_length=500)


class EvaluateAllIn(BaseModel):
    top_n: int = Field(default=50, ge=1, le=200)


# ══════════════════════════════════════════════════════════
# 错误辅助
# ══════════════════════════════════════════════════════════


def _busy_error(exc: mining_lock.MiningDomainBusy) -> FactorSevenError:
    """mining_domain 冲突 → 409。带当前任务 ID/状态/代数（需求 §3.1）。"""
    return factor_structured_error(
        "MINING_DOMAIN_BUSY",
        title_zh="已有挖掘任务进行中",
        detail_zh=(
            f"当前任务 {exc.owner_task_id}"
            + (f"（{exc.owner_status}，第 {exc.owner_generation} 代）" if exc.owner_status else "")
            + "，挖掘域同一时间只允许一个任务。请等待其结束或先取消。"
        ),
        impact="本次提交被拒绝，未创建任务（挖掘域不排队）",
        fix_link="/settings/factor-mining",
        retryable=True,
        extras={"owner_task_id": exc.owner_task_id, "owner_run_id": exc.owner_run_id},
    )


def _task_for_run(db: Session, run_id: str) -> AsyncTaskRecord | None:
    """按 `run_id` 反查挖掘任务（任务 payload 里带 run_id）。"""
    rows = db.execute(
        select(AsyncTaskRecord).where(
            AsyncTaskRecord.task_type == mining_runner.TASK_TYPE)
    ).scalars().all()
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}") or {}
        except ValueError:
            continue
        if payload.get("run_id") == run_id:
            return row
    return None


# ══════════════════════════════════════════════════════════
# 批次
# ══════════════════════════════════════════════════════════


@router.post("/factor-mining/runs", status_code=201)
def create_factor_mining_run(payload: MiningRunCreate,
                             db: Session = Depends(get_db)):
    """创建挖掘批次（含双锁检查：mining_domain 冲突 409 / duckdb_write 排队）。

    R5：本接口只做 DTO 转换与双锁语义映射；真实阶段链在
    `task_runner._default_stage_runner`（A2 已落地）。
    A5：提交前先落 `factor_mining_runs` 行（worker 阶段链按 run_id 加载；
    此前缺此行 → 真实 worker 直接报「run 不存在」，闭环 E2E 首代即断）。
    """
    run_id = uuid.uuid4().hex
    evolution = dict(payload.evolution_params or {})
    mining_service.create_run(
        db,
        run_id=run_id,
        candidate_pool_snapshot_id=payload.candidate_pool_snapshot_id,
        data_cutoff_at=payload.data_cutoff_at,
        start_date=payload.start_date,
        end_date=payload.end_date,
        rebalance_frequency=payload.rebalance_frequency,
        target_horizon=payload.target_horizon,
        random_seed=payload.random_seed,
        max_generation=max(1, int(evolution.get("max_generations", 20) or 20)),
    )
    task_payload = {
        "run_id": run_id,
        "candidate_pool_snapshot_id": payload.candidate_pool_snapshot_id,
        "data_cutoff_at": str(payload.data_cutoff_at),
        "start_date": str(payload.start_date),
        "end_date": str(payload.end_date),
        "rebalance_frequency": payload.rebalance_frequency,
        "target_horizon": payload.target_horizon,
        "train_ratio": payload.train_ratio,
        "validation_ratio": payload.validation_ratio,
        "random_seed": payload.random_seed,
        "evolution_params": dict(payload.evolution_params or {}),
        "selected_fields": list((payload.filter_config or {}).get("selected_fields") or [
            "close", "open", "high", "low", "volume", "amount", "turnover_rate"]),
        "finalize_top_k": int((payload.evolution_params or {}).get("finalize_top_k", 0) or 0),
        "warehouse_path": (payload.filter_config or {}).get("warehouse_path"),
    }
    try:
        result = mining_runner.submit_mining_run(task_payload, run_id=run_id)
    except mining_lock.MiningDomainBusy as exc:
        # 拒绝时把刚建的 run 行标 failed 留痕（审计可见），响应仍是 409
        run_row = mining_service.load_run(db, run_id)
        if run_row is not None:
            mining_service.set_run_status(db, run_row, "failed")
            db.commit()
        raise _factor_seven_http(_busy_error(exc)) from exc
    return MiningRunCreated(
        run_id=run_id,
        task_id=result.task_id,
        queue_position=result.queue_position,
        eta_seconds=None,
    ).model_dump()


@router.get("/factor-mining/runs", response_model=Page)
def list_factor_mining_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """分页查询批次。"""
    return mining_service.list_runs(db, page=page, page_size=page_size, status=status)


@router.get("/factor-mining/runs/{run_id}")
def get_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """状态/当前代数/进度/排队位次（合并锁状态）。"""
    detail = mining_service.get_run_detail(db, run_id)
    if detail is None:
        raise _not_found("挖掘批次", run_id)
    locks = mining_lock.get_lock_status(db).to_dict()
    detail["lock"] = {
        "mining_domain_busy": bool(
            (locks.get("miningDomain") or {}).get("busy")),
        "duckdb_write_busy": bool(
            (locks.get("duckdbWrite") or {}).get("busy")),
        "queue": list((locks.get("duckdbWrite") or {}).get("queue") or []),
    }
    return detail


@router.post("/factor-mining/runs/{run_id}/cancel")
def cancel_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """取消（保留结果与 checkpoint）。"""
    run = mining_service.load_run(db, run_id)
    if run is None:
        raise _not_found("挖掘批次", run_id)
    task = _task_for_run(db, run_id)
    if task is not None and task.status not in ("done", "failed", "cancelled"):
        from app.services.async_tasks import cancel_async_task

        cancel_async_task(task.id)
    mining_service.set_run_status(db, run, "cancelled")
    db.commit()
    return {"run_id": run_id, "status": "cancelled"}


@router.post("/factor-mining/runs/{run_id}/pause")
def pause_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """中断：保存 checkpoint，状态 paused，释放计算资源。"""
    run = mining_service.load_run(db, run_id)
    if run is None:
        raise _not_found("挖掘批次", run_id)
    task = _task_for_run(db, run_id)
    if task is not None and task.status not in ("done", "failed", "cancelled"):
        from app.services.async_tasks import cancel_async_task

        cancel_async_task(task.id)
    mining_service.set_run_status(db, run, "paused")
    db.commit()
    return {"run_id": run_id, "status": "paused"}


@router.post("/factor-mining/runs/{run_id}/resume")
def resume_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """从断点恢复：以既有 run 配置重新提交（checkpoint 由断言-恢复接管）。"""
    run = mining_service.load_run(db, run_id)
    if run is None:
        raise _not_found("挖掘批次", run_id)
    task_payload = {
        "run_id": run_id,
        "candidate_pool_snapshot_id": str(run.candidate_pool_snapshot_id),
        "data_cutoff_at": str(run.data_cutoff_at),
        "start_date": str(run.start_date),
        "end_date": str(run.end_date),
        "rebalance_frequency": str(run.rebalance_frequency or "daily"),
        "target_horizon": int(getattr(run, "target_horizon", 0) or 5),
        "train_ratio": 0.6, "validation_ratio": 0.2,
        "random_seed": int(getattr(run, "random_seed", 0) or 42),
        "finalize_top_k": 0,
    }
    try:
        result = mining_runner.submit_mining_run(task_payload, run_id=run_id)
    except mining_lock.MiningDomainBusy as exc:
        raise _factor_seven_http(_busy_error(exc)) from exc
    return {"run_id": run_id, "status": "queued", "task_id": result.task_id,
            "queue_position": result.queue_position}


@router.post("/factor-mining/runs/{run_id}/stop")
def stop_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """提前停止：保留已进化结果，状态 converged（最终验证由 evaluate-all 触发）。"""
    run = mining_service.load_run(db, run_id)
    if run is None:
        raise _not_found("挖掘批次", run_id)
    if run.status not in ("succeeded",):
        mining_service.set_run_status(db, run, "converged")
    run.converged = 1
    db.commit()
    return {"run_id": run_id, "status": "converged"}


@router.post("/factor-mining/runs/{run_id}/discard")
def discard_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """完全放弃：终止任务并置 cancelled（中间数据清理归 B4）。"""
    run = mining_service.load_run(db, run_id)
    if run is None:
        raise _not_found("挖掘批次", run_id)
    task = _task_for_run(db, run_id)
    if task is not None and task.status not in ("done", "failed", "cancelled"):
        from app.services.async_tasks import cancel_async_task

        cancel_async_task(task.id)
    mining_service.set_run_status(db, run, "cancelled")
    db.commit()
    return {"run_id": run_id, "status": "cancelled"}


# ══════════════════════════════════════════════════════════
# 代际与候选
# ══════════════════════════════════════════════════════════


@router.get("/factor-mining/runs/{run_id}/generations")
def list_generations(run_id: str, db: Session = Depends(get_db)):
    """每代汇总（含性能探针与 cache_validation_*）。"""
    if mining_service.load_run(db, run_id) is None:
        raise _not_found("挖掘批次", run_id)
    return mining_service.list_generations(db, run_id)


@router.get("/factor-mining/runs/{run_id}/candidates")
def list_candidates(
    run_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    grade: str | None = Query(default=None, description="S/A/B/C/D"),
    category: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """分页候选及指标（支持按等级/类型筛选；等级数据随 B1 迁移后可用）。"""
    if mining_service.load_run(db, run_id) is None:
        raise _not_found("挖掘批次", run_id)
    return mining_service.list_candidates(
        db, run_id, page=page, page_size=page_size, grade=grade, category=category)


@router.get("/factor-mining/runs/{run_id}/candidates/{candidate_id}/lineage")
def get_candidate_lineage(run_id: str, candidate_id: str,
                          db: Session = Depends(get_db)):
    """血缘追溯（代数/父代 ID/操作类型）。"""
    lineage = mining_service.get_candidate_lineage(db, run_id, candidate_id)
    if lineage is None:
        raise _not_found("候选", candidate_id)
    return lineage


# ══════════════════════════════════════════════════════════
# AI 预览
# ══════════════════════════════════════════════════════════


@router.post("/factor-mining/ai/generate-preview")
def ai_generate_preview(payload: dict[str, Any], db: Session = Depends(get_db)):
    """AI 骨架生成预览（不落库；尽力而为，AI 不可用返回空清单）。"""
    return mining_service.ai_preview_skeletons(payload, db)


# ══════════════════════════════════════════════════════════
# 锁状态（前端 Step4/Step5 依赖）
# ══════════════════════════════════════════════════════════


@router.get("/factor-mining/locks/status")
def get_locks_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    """双锁状态：占用者 / 排队位次。**已实现**，前端可在 Step4 提交前直接调用。

    查询前先回收心跳超时的过期锁（best-effort、幂等）：此前 expired 锁只能等
    task_runner patrol 清理，worker 重启/崩溃后残留锁会让 Step4 提交按钮
    永久受益于 `mining_domain busy` 而被禁用，用户无路可走。此处每次查询
    顺带回收，前端打开资源确认弹窗即触发。
    """
    mining_lock.expire_stale_locks(db)
    return mining_lock.get_lock_status(db).to_dict()


@router.get("/factor-mining/fields")
def list_mining_fields() -> dict[str, Any]:
    """Step3 字段目录（设计 §5）：DSL 已注册且可用于挖掘的字段。

    ⚠️ 与候选池筛选字段（`filter-fields`）**不同**：筛选字段用于 Step1 建池
    （如 avg_amount/board 等）；挖掘字段是公式/模板实际引用的 DSL 字段。
    此前前端误用筛选字段目录作为 Step3 目录，用户勾选 `avg_amount` 后经典
    模板全部因字段依赖不满足而跳过 → 初始种群为空 → worker crash。本接口
    返回 `factor_compiler.FIELD_CATALOG`（单一事实源）的挖掘字段目录。
    """
    from app.services.factors.factor_compiler import DSL_VERSION, FIELD_CATALOG

    # 来源分组（向导 §5：行情 / 估值 / 财报 / 资金流 / 事件快照）
    GROUP_BY_TABLE: dict[str, tuple[str, str]] = {
        "raw_daily_bars": ("quote", "行情"),
        "raw_valuation_snapshots": ("valuation", "估值"),
        "raw_financial_reports": ("financial", "财报"),
    }
    FLOW_FIELDS = ("main_net_inflow", "lhb_institution_net")
    EVENT_FIELDS = ("hot_rank_pct", "proxy_score", "etf_premium_discount",
                    "etf_tracking_error", "etf_fund_size")
    # 已知数据缺口（实测，未采集）→ blocked（补齐后自动恢复，不写死永久阻断）。
    # 文案两层（2026-09-22）：user 句进界面正文，detail 句进悬浮提示 —— 表名/物理列
    # 不得出现在用户界面上（哨兵 tests/.../candidate_pool/test_blocked_reason_copy.py）。
    BLOCKED_BY_DATA: dict[str, tuple[str, str]] = {
        "dividend_yield": (
            "股息率该列已存在，但当前估值快照里全为空值（未采集），补齐采集前不可用。",
            "实测 `raw_valuation_snapshots.dividend_yield` 全为空值（未采集）。",
        ),
        "proxy_score": (
            "尾盘分钟代理当前不可用，缺少可评价的分钟历史数据。",
            "尾盘分钟代理当前 maintained blocked，未接入可评价的分钟历史。",
        ),
    }

    fields: list[dict[str, Any]] = []
    for code, spec in FIELD_CATALOG.items():
        table = str(getattr(spec, "source_table", "") or "")
        if table in GROUP_BY_TABLE:
            group, group_label_zh = GROUP_BY_TABLE[table]
        elif code in FLOW_FIELDS:
            group, group_label_zh = "flow", "资金流"
        else:
            group, group_label_zh = "event", "事件/快照"
        policy_blocked = getattr(spec, "data_mode", None) == "blocked"
        blocked = policy_blocked or code in BLOCKED_BY_DATA
        fields.append({
            "field": code,
            "label_zh": str(getattr(spec, "label_zh", code) or code),
            "group": group,
            "group_label_zh": group_label_zh,
            "source_table": table or None,
            "data_mode": "blocked" if blocked else (
                "PIT" if getattr(spec, "point_in_time", False) else "continuous"),
            "availability": "blocked" if blocked else "available",
            "blocked_reason_zh": (
                BLOCKED_BY_DATA[code][0] if code in BLOCKED_BY_DATA
                else ("字段当前标记为不可用，暂不能勾选。" if policy_blocked else None)
            ),
            "blocked_detail_zh": (
                BLOCKED_BY_DATA[code][1] if code in BLOCKED_BY_DATA
                else (f"编译器 `FIELD_CATALOG['{code}'].data_mode == 'blocked'`"
                      if policy_blocked else None)
            ),
        })
    return {
        "dsl_version": DSL_VERSION,
        "fields": fields,
        "groups": [
            {"group": "quote", "label_zh": "行情"},
            {"group": "valuation", "label_zh": "估值"},
            {"group": "financial", "label_zh": "财报"},
            {"group": "flow", "label_zh": "资金流"},
            {"group": "event", "label_zh": "事件/快照"},
        ],
    }


class SplitBudgetRequest(BaseModel):
    start_date: datetime
    end_date: datetime
    frequency: Literal["daily", "weekly", "monthly"] = "daily"
    target_horizon: int = Field(default=5, ge=1, le=20)
    train_ratio: float = Field(default=0.6, gt=0, lt=1)
    validation_ratio: float = Field(default=0.2, gt=0, lt=1)


@router.post("/factor-mining/split-budget")
def get_split_budget(payload: SplitBudgetRequest,
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    """Step2 切分预算（向导 §4）：区间交易日 → 频率重采样 → 纯函数预算。

    数仓不可用/区间无交易日 → `available=False` + `reason_zh`（前端降级展示）。
    """
    return mining_service.compute_split_budget_preview(
        start_date=payload.start_date,
        end_date=payload.end_date,
        frequency=payload.frequency,
        target_horizon=payload.target_horizon,
        train_ratio=payload.train_ratio,
        validation_ratio=payload.validation_ratio,
    )


# ══════════════════════════════════════════════════════════
# 评估
# ══════════════════════════════════════════════════════════


@router.post("/factor-mining/evaluations")
def create_mining_evaluation(payload: dict[str, Any],
                             db: Session = Depends(get_db)):
    """创建评估（单候选）：候选须已提交为正式因子且已有评估记录，否则结构化 409。"""
    candidate_id = str((payload or {}).get("candidate_id") or "")
    if not candidate_id:
        raise HTTPException(status_code=400, detail={
            "error_code": "VALIDATION_ERROR",
            "title_zh": "缺少 candidate_id",
            "detail_zh": "请提供 candidate_id（单候选评估）。",
        })
    from app.models.factor_mining import FactorMiningCandidate

    cand = db.get(FactorMiningCandidate, candidate_id)
    if cand is None:
        raise _not_found("候选", candidate_id)
    if not cand.factor_version_id:
        raise HTTPException(status_code=409, detail={
            "error_code": "CANDIDATE_NOT_SUBMITTED",
            "title_zh": "候选未提交",
            "detail_zh": "候选尚未提交为正式因子版本；请先提交，再使用 evaluate-all 触发最终验证。",
        })
    return {"evaluation_id": cand.latest_evaluation_id,
            "factor_version_id": cand.factor_version_id, "status": "existing"}


@router.post("/factor-mining/runs/{run_id}/evaluate-all")
def evaluate_all(run_id: str, payload: EvaluateAllIn | None = None,
                 db: Session = Depends(get_db)):
    """批量创建最终验证（Top N，test-once；同步执行 finalize_run）。"""
    top_n = (payload.top_n if payload else 50)
    try:
        return mining_service.evaluate_all_impl(db, run_id=run_id, top_k=top_n)
    except ValueError as exc:
        raise _not_found("挖掘批次", run_id) from exc
    except FactorSevenError as exc:
        raise _factor_seven_http(exc) from exc


@router.get("/factor-mining/evaluations/{evaluation_id}")
def get_mining_evaluation(evaluation_id: str, db: Session = Depends(get_db)):
    """评估结果（含 metrics_json.stats 的 8 项统计，M2 统计层消费）。"""
    row = mining_service.get_mining_evaluation(db, evaluation_id)
    if row is None:
        raise _not_found("评估记录", evaluation_id)
    return row


# ══════════════════════════════════════════════════════════
# B4：草稿 / 实验模板 / 清理
# ══════════════════════════════════════════════════════════


@router.get("/factor-mining/drafts")
def list_mining_drafts(
    owner: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """多份并存草稿列表（B4）。"""
    return mining_draft_service.list_drafts(db, owner=owner, limit=limit)


@router.post("/factor-mining/drafts", status_code=201)
def save_mining_draft(payload: dict[str, Any], db: Session = Depends(get_db)):
    """暂存/更新草稿（无 draft_id 则新建，多份并存）。"""
    try:
        view = mining_draft_service.save_draft(db, payload=dict(payload or {}))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return view.to_dict()


@router.get("/factor-mining/drafts/{draft_id}")
def get_mining_draft(draft_id: str, db: Session = Depends(get_db)):
    view = mining_draft_service.get_draft(db, draft_id=draft_id)
    if view is None:
        raise _not_found("草稿", draft_id)
    return view.to_dict()


@router.post("/factor-mining/drafts/{draft_id}/prepare")
def prepare_mining_draft(draft_id: str, db: Session = Depends(get_db)):
    """最终准备校验（Step1~4 + 快照 + 字段校验有效；不满足给出 blockers）。"""
    try:
        return mining_service.prepare_draft(db, draft_id=draft_id)
    except ValueError as exc:
        raise _not_found("草稿", draft_id) from exc


@router.post("/factor-mining/drafts/{draft_id}/status")
def set_mining_draft_status(draft_id: str, payload: dict[str, Any],
                            db: Session = Depends(get_db)):
    try:
        view = mining_draft_service.set_draft_status(
            db, draft_id=draft_id, status=str((payload or {}).get("status") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return view.to_dict()


@router.post("/factor-mining/templates/seed")
def seed_mining_templates(db: Session = Depends(get_db)):
    """25 系统经典模板入表（幂等，与 GA 初始种群同源）。"""
    return mining_template_service.seed_system_templates(db)


@router.get("/factor-mining/templates")
def list_mining_templates(
    scope: str | None = Query(default=None),
    enabled: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return mining_template_service.list_templates(
        db, scope=scope, enabled=enabled, limit=limit)


@router.get("/factor-mining/templates/{template_id}")
def get_mining_template(template_id: str, db: Session = Depends(get_db)):
    view = mining_template_service.get_template(db, template_id)
    if view is None:
        raise _not_found("实验模板", template_id)
    return view


@router.post("/factor-mining/templates", status_code=201)
def create_mining_template(payload: dict[str, Any], db: Session = Depends(get_db)):
    """个人模板创建（rule_config 必须含 formula）。"""
    try:
        return mining_template_service.create_personal_template(
            db,
            name=str((payload or {}).get("name") or ""),
            description=(payload or {}).get("description"),
            rule_config=dict((payload or {}).get("rule_config") or {}),
            owner=str((payload or {}).get("owner") or "local_user"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/factor-mining/templates/{template_id}/copy")
def copy_mining_template(template_id: str, payload: dict[str, Any] | None = None,
                         db: Session = Depends(get_db)):
    """复制为个人模板（副本启停/版本独立）。"""
    try:
        return mining_template_service.copy_template(
            db, template_id=template_id,
            owner=str((payload or {}).get("owner") or "local_user"))
    except ValueError as exc:
        raise _not_found("实验模板", template_id) from exc


@router.post("/factor-mining/templates/{template_id}/enabled")
def toggle_mining_template(template_id: str, payload: dict[str, Any],
                           db: Session = Depends(get_db)):
    """启停模板（enabled: 0/1）。"""
    try:
        return mining_template_service.set_template_enabled(
            db, template_id=template_id,
            enabled=int((payload or {}).get("enabled", 1)))
    except ValueError as exc:
        raise _not_found("实验模板", template_id) from exc


@router.post("/factor-mining/runs/{run_id}/cleanup")
def cleanup_mining_run(run_id: str, db: Session = Depends(get_db)):
    """清理批次中间数据并删 run 行（候选/代际/检查点/指纹）。"""
    try:
        return mining_service.cleanup_run_data(db, run_id=run_id)
    except ValueError as exc:
        raise _not_found("挖掘批次", run_id) from exc


@router.post("/factor-mining/cleanup/stale")
def purge_stale_mining_data(
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """中间数据 7 天保留：清理超期未提交的草稿。"""
    return mining_service.purge_stale_mining_drafts(db, days=days, limit=limit)


# ══════════════════════════════════════════════════════════
# 候选 → 正式因子 / 批量操作
# ══════════════════════════════════════════════════════════


@router.post("/factor-mining/candidates/{candidate_id}/submit")
def submit_candidate(candidate_id: str, db: Session = Depends(get_db)):
    """提交为正式因子版本（草稿）。

    红线 C6：**必须**走 factor_registry.create_factor_draft / create_factor_version，
    挖掘域不得绕过 /factors 写入口（实现 = `mining.service.submit_candidate_as_factor`）。
    幂等：已提交的候选返回 `already`。
    """
    try:
        return mining_service.submit_candidate_as_factor(db, candidate_id=candidate_id)
    except FactorSevenError as exc:
        raise _factor_seven_http(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error_code": "candidate_not_found",
                                                     "detail_zh": str(exc)}) from exc


@router.post("/factor-mining/candidates/batch-review")
def batch_review(payload: dict[str, Any], db: Session = Depends(get_db)):
    """批量审核候选：approve（转因子草稿）/ reject（驳回）/ add_to_set（入 draft FactorSet）。

    - 逐项独立处理，单项失败不中断批；逐项返回 `status` / `error_code`
    - add_to_set 前候选必须已提交（有 `factor_version_id`）
    - frozen FactorSet 不可写：`set_not_mutable` 按项返回
    """
    actions = payload.get("actions") or []
    if not isinstance(actions, list) or not actions:
        raise HTTPException(status_code=400, detail={
            "error_code": "VALIDATION_ERROR",
            "title_zh": "缺少 actions",
            "detail_zh": "请提供非空的 actions 列表",
        })
    return mining_service.batch_review_candidates(
        db, actions=actions,
        created_by=str(payload.get("created_by") or "local_user"))


@router.post("/factor-mining/candidates/{candidate_id}/save-as-template")
def save_candidate_as_template(candidate_id: str, db: Session = Depends(get_db)):
    """保存为个人公式模板（scope=personal）。"""
    try:
        return mining_service.save_candidate_as_template(db, candidate_id=candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={
            "error_code": "candidate_not_found", "detail_zh": str(exc)}) from exc


# ══════════════════════════════════════════════════════════
# 质量分级（M2；factor_versions 分级列随 B1 迁移）
# ══════════════════════════════════════════════════════════


@router.post("/factor-mining/candidates/{candidate_id}/grade/manual")
def manual_grade(candidate_id: str, payload: ManualGradeIn,
                 db: Session = Depends(get_db)):
    """人工调整等级。原因必填 ≥10 字；写 factor_grade_history（trigger=manual）。"""
    try:
        return mining_service.apply_manual_grade(
            db, candidate_id=candidate_id, grade=payload.grade, reason=payload.reason)
    except FactorSevenError as exc:
        # CANDIDATE_NOT_SUBMITTED 未被全局状态码表收录 → 显式 409
        status = 409 if exc.error_code == "CANDIDATE_NOT_SUBMITTED" \
            else _factor_seven_http(exc).status_code
        raise HTTPException(status_code=status, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "error_code": "VALIDATION_ERROR", "detail_zh": str(exc)}) from exc


@router.post("/factor-mining/candidates/{candidate_id}/grade/restore-auto")
def restore_auto_grade(candidate_id: str, db: Session = Depends(get_db)):
    """恢复自动评定：置 grade_manual_adjusted=0（B1 迁移后写 factor_versions 列）。"""
    try:
        return mining_service.set_manual_grade_auto(db, candidate_id=candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "error_code": "VALIDATION_ERROR", "detail_zh": str(exc)}) from exc


@router.get("/factor-mining/candidates/{candidate_id}/grade/evidence")
def grade_evidence(candidate_id: str, db: Session = Depends(get_db)):
    """定级证据：8 维度 + 定级理由 + 血缘（M2，证据抽屉数据源）。"""
    try:
        return mining_service.get_grade_evidence(db, candidate_id=candidate_id)
    except ValueError as exc:
        raise _not_found("候选", candidate_id) from exc