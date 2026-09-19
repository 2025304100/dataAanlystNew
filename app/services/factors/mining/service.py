"""挖掘域服务层：GA 逐代产物的落库（`service.py`；任务 T23）。

职责边界
========
- **只做持久化**（`factor_mining_generations` / `factor_mining_candidates` /
  `factor_mining_runs.total_trials`）与 GA 配置的装配；进化算法本体在
  `genetic_algorithm.py`（纯逻辑，离线可测）。
- **所有 MySQL 数值列写入必须过 `app.core.db_numeric`**（P0 规则，2026-09-17）：
  `NaN`/`±Inf` → `None`（绝不归 0），超界按列型夹取。
- 评估本体在 `evaluation_adapter.evaluate_short`（**只用 train 段**），
  本模块不实现评估。

`total_trials`（DSR 唯一输入源）落在 **`factor_mining_runs`** 上，
每代由 `sync_run_progress` 累加更新 —— 少记一代，DSR 就低估数据挖掘偏差。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select

from app.core import db_numeric as DN
from app.models.factor_model import FactorVersion
from app.models.factor_mining import (
    FactorMiningCandidate,
    FactorMiningGeneration,
    FactorMiningRun,
)

logger = logging.getLogger(__name__)

#: 候选表的 float 列（落库时逐列过 `to_db_float`，按**真实列型**夹取）
_CANDIDATE_FLOAT_COLUMNS: dict[str, Any] = {
    "generation_icir": FactorMiningCandidate.__table__.columns["generation_icir"],
    "generation_coverage": FactorMiningCandidate.__table__.columns["generation_coverage"],
    "generation_turnover": FactorMiningCandidate.__table__.columns["generation_turnover"],
    "similarity_score": FactorMiningCandidate.__table__.columns["similarity_score"],
    "latest_ic": FactorMiningCandidate.__table__.columns["latest_ic"],
}
#: 候选表的 int 列
_CANDIDATE_INT_COLUMNS = ("generation_complexity", "generation_rank")


def persist_generation(
    db: Any, *, run_id: str, generation: int, record: Mapping[str, Any],
) -> FactorMiningGeneration:
    """把一代统计写入/更新 `factor_mining_generations`（**过 `db_numeric`**）。

    `record` 键（来自 `genetic_algorithm.run_ga_loop` 的逐代 record）：
    `population_size / best_icir / avg_icir / median_icir / elite_count /
    mutation_count / crossover_count / random_count / total_trials /
    stall_count / eliminated_count`
    """
    row = db.execute(
        select(FactorMiningGeneration).where(
            FactorMiningGeneration.run_id == str(run_id),
            FactorMiningGeneration.generation == int(generation),
        )
    ).scalar_one_or_none()
    if row is None:
        row = FactorMiningGeneration(run_id=str(run_id), generation=int(generation))
        db.add(row)

    float_columns = {
        "best_icir": FactorMiningGeneration.__table__.columns["best_icir"],
        "avg_icir": FactorMiningGeneration.__table__.columns["avg_icir"],
        "median_icir": FactorMiningGeneration.__table__.columns["median_icir"],
    }
    int_keys = ("population_size", "elite_count", "mutation_count",
                "crossover_count", "random_count", "stall_count",
                "eliminated_count")
    for key in int_keys:
        if key in record:
            setattr(row, key, int(record[key] or 0))
    for key, column in float_columns.items():
        if key in record:
            # 🚨 P0：NaN/±Inf → None；绝不归 0（ICIR=0 是「无预测力」不是「没算」）
            setattr(row, key, DN.to_db_float(record[key], column=column))

    # ── 探针与 G2 校验列（T21 口径；同样必须过 db_numeric）──
    probe_float = {
        "probe_g2_hit_rate": FactorMiningGeneration.__table__.columns["probe_g2_hit_rate"],
        "cache_validation_max_diff": FactorMiningGeneration.__table__.columns[
            "cache_validation_max_diff"],
    }
    for key, value in record.items():
        if not (key.startswith("probe_") or key.startswith("cache_validation_")):
            continue
        if key in probe_float:
            setattr(row, key, DN.to_db_float(value, column=probe_float[key]))
        else:
            setattr(row, key, int(value or 0))

    db.flush()
    return row


def sync_run_progress(db: Any, *, run_id: str, generation: int,
                      total_trials: int, converged: bool = False) -> None:
    """更新 run 的进度字段（**`total_trials` 每代累加**，DSR 唯一输入源）。"""
    row = db.get(FactorMiningRun, str(run_id))
    if row is None:
        raise ValueError(f"run 不存在: {run_id!r}")
    row.current_generation = int(generation)
    row.total_trials = int(max(0, total_trials))       # 🚨 只增不减（DSR 唯一输入源）
    if converged:
        row.converged = 1
    db.flush()


def persist_candidates(
    db: Any, *, run_id: str, generation: int,
    ranked: Sequence[Mapping[str, Any]],
) -> int:
    """把一代的排序结果写入 `factor_mining_candidates`。

    每行：`formula_expr` / `canonical_formula` / `formula_hash` / `operation` /
    `generation` / 当代指标（**过 `db_numeric`**）/ `generation_rank`。

    **幂等**：同 (run_id, formula_hash, generation) 已存在则跳过
    （重跑/续跑不会产生重复行）。
    Returns:
        新增行数
    """
    existing = set(db.execute(
        select(FactorMiningCandidate.formula_hash).where(
            FactorMiningCandidate.run_id == str(run_id),
            FactorMiningCandidate.generation == int(generation),
        )
    ).scalars())
    inserted = 0
    for rank, entry in enumerate(ranked):
        digest = str(entry.get("formula_hash") or "")
        if not digest or digest in existing:
            continue
        fitness = entry.get("fitness") or {}
        row = FactorMiningCandidate(
            id=uuid.uuid4().hex,
            run_id=str(run_id),
            formula_expr=str(entry.get("formula") or ""),
            canonical_formula=str(entry.get("canonical_formula")
                                  or entry.get("formula") or ""),
            formula_hash=digest,
            generation=int(generation),
            operation=str(entry.get("operation") or "enumerated"),
            category=(str(entry.get("category")) if entry.get("category") else None),
            generation_rank=int(rank),
            economic_logic=(str(entry.get("economic_logic"))
                            if entry.get("economic_logic") else None),
            expected_direction=(str(entry.get("expected_direction"))
                                if entry.get("expected_direction") else None),
        )
        # 🚨 当代指标：NaN/±Inf → None（P0），按真实列型夹取
        for key, column in _CANDIDATE_FLOAT_COLUMNS.items():
            value = fitness.get(key.replace("generation_", "")) if fitness else None
            if value is None:
                value = entry.get(key)
            if value is not None:
                setattr(row, key, DN.to_db_float(value, column=column))
        if fitness and fitness.get("complexity") is not None:
            row.generation_complexity = int(fitness["complexity"])
        db.add(row)
        existing.add(digest)
        inserted += 1
    db.flush()
    return inserted


def create_run(db: Any, *, run_id: str | None = None, **fields: Any) -> FactorMiningRun:
    """创建 run 行（GA 入口；`total_trials` 从 0 起步）。"""
    row = FactorMiningRun(
        id=run_id or uuid.uuid4().hex,
        status="running",
        candidate_pool_snapshot_id=str(fields.pop("candidate_pool_snapshot_id")),
        data_cutoff_at=fields.pop("data_cutoff_at"),
        start_date=fields.pop("start_date"),
        end_date=fields.pop("end_date"),
        rebalance_frequency=str(fields.pop("rebalance_frequency", "weekly")),
        split_method=str(fields.pop("split_method", "ratio")),
        split_algorithm_version=str(fields.pop("split_algorithm_version",
                                               "split-1.0.0")),
        max_generation=int(fields.pop("max_generation", 20)),
        random_seed=int(fields.pop("random_seed", 42)),
        **fields,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def load_run(db: Any, run_id: str) -> FactorMiningRun | None:
    return db.get(FactorMiningRun, str(run_id))


def load_generation(db: Any, *, run_id: str, generation: int
                    ) -> FactorMiningGeneration | None:
    return db.execute(
        select(FactorMiningGeneration).where(
            FactorMiningGeneration.run_id == str(run_id),
            FactorMiningGeneration.generation == int(generation),
        )
    ).scalar_one_or_none()


def evolution_params_json(cfg: Mapping[str, Any]) -> str:
    """把 GA 配置序列化进 `factor_mining_runs.evolution_params_json`（复现用）。"""
    return json.dumps(dict(cfg), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def set_run_status(db: Any, run: FactorMiningRun, new_status: str) -> None:
    """run 状态迁移（**`succeeded` 不可回退**，任务卡坑 3）。"""
    if run.status == "succeeded":
        from app.schemas.errors import FactorSevenError

        raise FactorSevenError(
            "MINING_TEST_LOCKED",
            detail_zh=f"run 已处于 succeeded，禁止任何状态回退（当前请求 → {new_status}）。",
        )
    run.status = new_status
    db.flush()


def finalize_run(
    db: Any, *, ctx: Any, run_id: str, top_k: int = 50,
    factor_values: Any = None, forward_returns: Any = None,
    draft_creator: Callable[..., Any] | None = None,
    run_evaluator: Callable[..., Any] | None = None,
    created_by: str = "mining",
) -> dict[str, Any]:
    """最终验证（**test 段仅此一次**）+ 结果落库 + run 收官。

    流程：
        1. 状态守卫：`validating` / `succeeded` → `MINING_TEST_LOCKED`（二次调用被拒）
        2. `run.status → validating`
        3. 取候选 Top-K（按当代 ICIR 降序，None 沉底）
        4. 逐个 `evaluate_full`（C6 草稿链 + test 评估）；**单候选失败不中断整批**
        5. 回填候选：`factor_version_id` / `latest_evaluation_id` / `latest_ic`
        6. `run.status → succeeded`（**不可回退**）+ `converged=1`

    Returns:
        `{"evaluated": n, "failed": m, "outcomes": [...], "status": "succeeded"}`
    """
    from app.schemas.errors import FactorSevenError

    from app.services.factors.mining import evaluation_adapter as EA

    run = db.get(FactorMiningRun, str(run_id))
    if run is None:
        raise ValueError(f"run 不存在: {run_id!r}")
    if run.status in ("validating", "succeeded"):
        raise FactorSevenError(
            "MINING_TEST_LOCKED",
            detail_zh=f"run 已处于 {run.status}：test 段已评估过（或正在评估），"
                      "不可回炉重挖（防过拟合硬约束）。",
        )
    set_run_status(db, run, "validating")
    db.commit()

    rows = db.execute(
        select(FactorMiningCandidate).where(
            FactorMiningCandidate.run_id == str(run_id))
    ).scalars().all()
    pool = sorted(
        (r for r in rows if r.generation_icir is not None),
        key=lambda r: r.generation_icir, reverse=True)
    pool += [r for r in rows if r.generation_icir is None]
    pool = pool[: max(0, int(top_k))]

    outcomes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for cand in pool:
        item = {
            "formula": cand.formula_expr,
            "canonical_formula": cand.canonical_formula,
            "formula_hash": cand.formula_hash,
            "category": cand.category,
            "expected_direction": cand.expected_direction,
            "economic_logic": cand.economic_logic,
            "factor_version_id": cand.factor_version_id,
        }
        try:
            outcome = EA.evaluate_full(
                ctx, individual=item, db=db,
                factor_values=factor_values, forward_returns=forward_returns,
                draft_creator=draft_creator, run_evaluator=run_evaluator,
                created_by=created_by)
        except FactorSevenError:
            raise                       # 锁定类错误必须向上抛（不吞）
        except Exception as exc:  # noqa: BLE001 - 单候选失败不中断整批
            cand.latest_evaluation_status = "failed"
            failures.append({"formula_hash": cand.formula_hash,
                             "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue

        cand.factor_version_id = outcome.factor_version_id
        cand.latest_evaluation_id = outcome.evaluation_run_id
        cand.latest_evaluation_status = "done"
        cand.latest_ic = DN.to_db_float(
            outcome.metrics.get("ic"),
            column=FactorMiningCandidate.__table__.columns["latest_ic"])
        outcomes.append({
            "formula_hash": cand.formula_hash,
            "factor_version_id": outcome.factor_version_id,
            "evaluation_run_id": outcome.evaluation_run_id,
            "metrics": outcome.metrics,
            "gate_result": outcome.gate_result,
            "test_start": str(outcome.test_start) if outcome.test_start else None,
            "test_end": str(outcome.test_end) if outcome.test_end else None,
        })

    set_run_status(db, run, "succeeded")
    run.converged = 1
    db.commit()

    return {"evaluated": len(outcomes), "failed": len(failures),
            "outcomes": outcomes, "failures": failures, "status": "succeeded"}


def submit_candidate_as_factor(
    db: Any, *, candidate_id: str, created_by: str = "local_user",
) -> dict[str, Any]:
    """候选 → 正式因子草稿（红线 C6：走 factor_registry 写入口）。

    - 幂等：已带 `factor_version_id` 的候选直接返回 `already`
    - 草稿链 = `create_candidate_draft`（T24 引入，复用 C6 实现）
    - 回填候选：`factor_version_id` + `latest_evaluation_status="submitted"`
    """
    from app.schemas.errors import FactorSevenError

    from app.services.factors.mining import evaluation_adapter as EA

    cand = db.get(FactorMiningCandidate, str(candidate_id))
    if cand is None:
        raise ValueError(f"candidate_not_found:{candidate_id}")
    if cand.factor_version_id:
        return {"status": "already", "candidate_id": cand.id,
                "factor_version_id": cand.factor_version_id}

    digest = str(cand.formula_hash or "")
    if not digest:
        raise FactorSevenError(
            "MINING_SAMPLE_INSUFFICIENT",
            detail_zh=f"候选 {candidate_id} 缺少 formula_hash，无法提交。",
        )
    version_id, code = EA.create_candidate_draft(
        db, digest=digest, formula=str(cand.formula_expr or ""),
        category=cand.category,
        direction=str(cand.expected_direction or "positive"),
        logic=str(cand.economic_logic or ""), created_by=created_by,
    )
    cand.factor_version_id = int(version_id)
    cand.latest_evaluation_status = "submitted"
    db.commit()
    return {"status": "submitted", "candidate_id": cand.id,
            "factor_code": code, "factor_version_id": int(version_id),
            "formula_hash": digest}


def batch_review_candidates(
    db: Any, *, actions: Sequence[Mapping[str, Any]],
    created_by: str = "local_user",
) -> dict[str, Any]:
    """批量审核候选：approve（转因子草稿）/ reject（驳回）/ add_to_set（入 FactorSet）。

    每项独立处理（单项失败不中断批）；返回逐项状态供前端展示。
    `add_to_set` 前按候选是否已提交校验（未提交 → failed）；FactorSet 冻结等
    `FactorSetError` 按项记录（含 error_code），**frozen 的 set 不可写**（P0 规则）。
    """
    from app.schemas.factor_library import FactorSetMemberCreate
    from app.services.factors import factor_set_service as FSS

    results: list[dict[str, Any]] = []
    counts = {"approved": 0, "rejected": 0, "added": 0, "skipped": 0,
              "failed": 0}
    order_cursor: dict[str, int] = {}

    for action_item in actions:
        candidate_id = str(action_item.get("candidate_id") or "")
        action = str(action_item.get("action") or "").strip().lower()
        item: dict[str, Any] = {"candidate_id": candidate_id, "action": action}
        cand = db.get(FactorMiningCandidate, candidate_id) if candidate_id else None
        if cand is None:
            item.update(status="failed", error_code="candidate_not_found")
            counts["failed"] += 1
            results.append(item)
            continue

        if action == "approve":
            try:
                out = submit_candidate_as_factor(db, candidate_id=candidate_id,
                                                 created_by=created_by)
                item.update(status=out["status"],
                            factor_version_id=out.get("factor_version_id"))
                counts["approved" if out["status"] == "submitted" else "skipped"] += 1
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                item.update(status="failed",
                            error_code=type(exc).__name__,
                            error=str(exc)[:200])
                counts["failed"] += 1

        elif action == "reject":
            cand.elimination_status = "manual_rejected"
            cand.elimination_reason = str(action_item.get("reason")
                                          or "manual rejected")[:48]
            item["status"] = "rejected"
            counts["rejected"] += 1

        elif action == "add_to_set":
            factor_set_id = str(action_item.get("factor_set_id") or "")
            if not factor_set_id or not cand.factor_version_id:
                item.update(status="failed", error_code="not_submitted",
                            error="候选未提交为因子版本，无法加入 FactorSet")
                counts["failed"] += 1
                results.append(item)
                continue
            version = db.get(FactorVersion, int(cand.factor_version_id))
            if version is None:
                item.update(status="failed", error_code="version_not_found")
                counts["failed"] += 1
                results.append(item)
                continue
            order = order_cursor.setdefault(factor_set_id, 1)
            try:
                FSS.add_member(db, factor_set_id=factor_set_id,
                               payload=FactorSetMemberCreate(
                                   factor_id=version.factor_id,
                                   factor_version_id=version.id,
                                   display_order=order))
                order_cursor[factor_set_id] = order + 1
                item.update(status="added",
                            factor_id=version.factor_id,
                            factor_version_id=version.id)
                counts["added"] += 1
            except Exception as exc:  # noqa: BLE001 - 单项失败不中断批
                db.rollback()
                # FactorSetError 的属性名是 `code`（不是 error_code）
                code = getattr(exc, "code", None) or \
                    getattr(exc, "error_code", None) or type(exc).__name__
                item.update(status="failed", error_code=str(code),
                            error=str(exc)[:200])
                counts["failed"] += 1

        else:
            item.update(status="failed", error_code="unknown_action")
            counts["failed"] += 1
        results.append(item)

    return {"results": results, "counts": counts}


__all__ = [
    "persist_generation",
    "sync_run_progress",
    "persist_candidates",
    "create_run",
    "load_run",
    "load_generation",
    "evolution_params_json",
    "set_run_status",
    "finalize_run",
    "submit_candidate_as_factor",
    "batch_review_candidates",
]
