"""挖掘域 HTTP 路由（设计文档 §8.2，模块 M5/M7）。

⚠️ 本项目的路由范式（设计文档 §2.1 第 27 条）：
   `router = APIRouter()` **无 prefix**，装饰器里写**全路径**。
   前缀 `/api/v1` 由 `app/api/router.py:7` 统一挂载。

⚠️ 裸眼可见的分层纪律（R5）：本文件只做 DTO 转换与入参校验，
   **不得**出现阈值、切分、指标计算。

⚠️ 落位后必须在 `app/api/router.py` 补两处：
   ① L3 import 列表加 `factor_mining`
   ② L75 之前加 `api_router.include_router(factor_mining.router, tags=["factor-mining"])`
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.errors import FactorSevenError, factor_structured_error
from app.services.factors.mining import task_lock as mining_lock

router = APIRouter()


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
    #: 切分预算（Step2/Step4 展示；purge 同时给出调仓点数与折算交易日数）
    split_budget: dict[str, Any] | None = None


class Page(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


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


# ══════════════════════════════════════════════════════════
# 批次
# ══════════════════════════════════════════════════════════


@router.post("/factor-mining/runs")
def create_factor_mining_run(payload: MiningRunCreate, db: Session = Depends(get_db)):
    """创建挖掘批次（含双锁检查）。

    TODO(M5/M7): 委托 mining.service.create_mining_run。
    """
    raise NotImplementedError("M5: create_factor_mining_run")


@router.get("/factor-mining/runs", response_model=Page)
def list_factor_mining_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """分页查询批次。TODO(M5)"""
    raise NotImplementedError("M5: list_factor_mining_runs")


@router.get("/factor-mining/runs/{run_id}")
def get_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """状态/当前代数/进度/排队位次。TODO(M5)"""
    raise NotImplementedError("M5: get_factor_mining_run")


@router.post("/factor-mining/runs/{run_id}/cancel")
def cancel_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """取消（保留结果与 checkpoint）。TODO(M7)"""
    raise NotImplementedError("M7: cancel_factor_mining_run")


@router.post("/factor-mining/runs/{run_id}/pause")
def pause_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """中断：保存 checkpoint，状态 paused，释放计算资源。TODO(M7)"""
    raise NotImplementedError("M7: pause_factor_mining_run")


@router.post("/factor-mining/runs/{run_id}/resume")
def resume_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """从断点恢复。TODO(M7)"""
    raise NotImplementedError("M7: resume_factor_mining_run")


@router.post("/factor-mining/runs/{run_id}/stop")
def stop_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """提前停止：保留已进化结果，直接进入最终验证。TODO(M7)"""
    raise NotImplementedError("M7: stop_factor_mining_run")


@router.post("/factor-mining/runs/{run_id}/discard")
def discard_factor_mining_run(run_id: str, db: Session = Depends(get_db)):
    """完全放弃：终止并清理中间数据（不可恢复）。TODO(M7/M15)"""
    raise NotImplementedError("M7: discard_factor_mining_run")


# ══════════════════════════════════════════════════════════
# 代际与候选
# ══════════════════════════════════════════════════════════


@router.get("/factor-mining/runs/{run_id}/generations")
def list_generations(run_id: str, db: Session = Depends(get_db)):
    """每代汇总（含性能探针与 cache_validation_*）。TODO(M7)"""
    raise NotImplementedError("M7: list_generations")


@router.get("/factor-mining/runs/{run_id}/candidates")
def list_candidates(
    run_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    grade: str | None = Query(default=None, description="S/A/B/C/D"),
    category: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """分页候选及指标（支持按等级/类型筛选，M2）。TODO(M7/M13)"""
    raise NotImplementedError("M7: list_candidates")


@router.get("/factor-mining/runs/{run_id}/candidates/{candidate_id}/lineage")
def get_candidate_lineage(run_id: str, candidate_id: str, db: Session = Depends(get_db)):
    """血缘追溯（代数/父代 ID/操作类型）。TODO(M9)"""
    raise NotImplementedError("M9: get_candidate_lineage")


# ══════════════════════════════════════════════════════════
# AI 预览
# ══════════════════════════════════════════════════════════


@router.post("/factor-mining/ai/generate-preview")
def ai_generate_preview(payload: dict[str, Any], db: Session = Depends(get_db)):
    """AI 骨架生成预览（不落库）。

    强制校验：缺 formula_ast / category / economic_logic / expected_direction 任一即丢弃。
    TODO(M12): 委托 mining.ai_generator。
    """
    raise NotImplementedError("M12: ai_generate_preview")


# ══════════════════════════════════════════════════════════
# 锁状态（前端 Step4/Step5 依赖）
# ══════════════════════════════════════════════════════════


@router.get("/factor-mining/locks/status")
def get_locks_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    """双锁状态：占用者 / 排队位次。**已实现**，前端可在 Step4 提交前直接调用。"""
    return mining_lock.get_lock_status(db).to_dict()


# ══════════════════════════════════════════════════════════
# 评估
# ══════════════════════════════════════════════════════════


@router.post("/factor-mining/evaluations")
def create_mining_evaluation(payload: dict[str, Any], db: Session = Depends(get_db)):
    """创建评估（先走 /scoring/evaluations/precheck）。TODO(M10)"""
    raise NotImplementedError("M10: create_mining_evaluation")


@router.post("/factor-mining/runs/{run_id}/evaluate-all")
def evaluate_all(run_id: str, db: Session = Depends(get_db)):
    """批量创建最终验证（Top N）。TODO(M10)"""
    raise NotImplementedError("M10: evaluate_all")


@router.get("/factor-mining/evaluations/{evaluation_id}")
def get_mining_evaluation(evaluation_id: str, db: Session = Depends(get_db)):
    """评估结果（含 metrics_json.stats 的 8 项统计）。TODO(M10/M13)"""
    raise NotImplementedError("M10: get_mining_evaluation")


# ══════════════════════════════════════════════════════════
# 候选 → 正式因子 / 批量操作
# ══════════════════════════════════════════════════════════


@router.post("/factor-mining/candidates/{candidate_id}/submit")
def submit_candidate(candidate_id: str, db: Session = Depends(get_db)):
    """提交为正式因子版本。

    红线 C6：**必须**走 factor_registry.create_factor_draft / create_factor_version，
    挖掘域不得绕过 /factors 写入口。
    TODO(M7): 委托 mining.service.submit_candidate_as_factor。
    """
    raise NotImplementedError("M7: submit_candidate")


@router.post("/factor-mining/candidates/batch-review")
def batch_review(payload: dict[str, Any], db: Session = Depends(get_db)):
    """批量通过/驳回/加入 draft FactorSet。

    注意：factor_set_service.add_member 要求成员版本状态 ∈ testing/shadow/active；
    加入前按 formula_hash 查重，已存在跳过并提示。
    TODO(M7)
    """
    raise NotImplementedError("M7: batch_review")


@router.post("/factor-mining/candidates/{candidate_id}/save-as-template")
def save_candidate_as_template(candidate_id: str, db: Session = Depends(get_db)):
    """保存为个人模板。TODO(M15)"""
    raise NotImplementedError("M15: save_candidate_as_template")


# ══════════════════════════════════════════════════════════
# 质量分级（M2）
# ══════════════════════════════════════════════════════════


@router.post("/factor-mining/candidates/{candidate_id}/grade/manual")
def manual_grade(candidate_id: str, payload: dict[str, Any], db: Session = Depends(get_db)):
    """人工调整等级。原因必填 ≥10 字；置 grade_manual_adjusted=1（M2）。TODO(M13)"""
    raise NotImplementedError("M13: manual_grade")


@router.post("/factor-mining/candidates/{candidate_id}/grade/restore-auto")
def restore_auto_grade(candidate_id: str, db: Session = Depends(get_db)):
    """恢复自动评定：置 grade_manual_adjusted=0（M2）。TODO(M13)"""
    raise NotImplementedError("M13: restore_auto_grade")


@router.get("/factor-mining/candidates/{candidate_id}/grade/evidence")
def grade_evidence(candidate_id: str, db: Session = Depends(get_db)):
    """定级证据：8 维度 + 统计 8 项 + 血缘（M2，证据抽屉数据源）。TODO(M13)"""
    raise NotImplementedError("M13: grade_evidence")
