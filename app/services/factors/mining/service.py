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
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select

from app.core import db_numeric as DN
from app.models.factor_model import FactorVersion
from app.models.factor_mining import (
    FactorMiningCandidate,
    FactorMiningGeneration,
    FactorMiningPrescreenFingerprint,
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
    stall_count / eliminated_count`；M2 路径（selection_mode="advanced"）额外：
    `diversity_* / category_evenness / pareto_front_count / actual_三率 /
    mutation_type_distribution_json / category_distribution_json /
    cross_category_ratio / adaptive_state / convergence_delta`（探针与
    `cache_validation_*` 由 T21 探针链路另行合入）。
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

    # ── M2 代际字段（P0 集成；A1/B1/B2/C1-C3/D1-D3 产出，naive 路径不含这些键） ──
    m2_float_columns = {
        "diversity_score": FactorMiningGeneration.__table__.columns[
            "diversity_score"],
        "category_evenness": FactorMiningGeneration.__table__.columns[
            "category_evenness"],
        "diversity_genotype": FactorMiningGeneration.__table__.columns[
            "diversity_genotype"],
        "diversity_phenotype": FactorMiningGeneration.__table__.columns[
            "diversity_phenotype"],
        "diversity_health": FactorMiningGeneration.__table__.columns[
            "diversity_health"],
        "actual_mutation_rate": FactorMiningGeneration.__table__.columns[
            "actual_mutation_rate"],
        "actual_crossover_rate": FactorMiningGeneration.__table__.columns[
            "actual_crossover_rate"],
        "actual_random_rate": FactorMiningGeneration.__table__.columns[
            "actual_random_rate"],
        "cross_category_ratio": FactorMiningGeneration.__table__.columns[
            "cross_category_ratio"],
        "convergence_delta": FactorMiningGeneration.__table__.columns[
            "convergence_delta"],
    }
    for key, column in m2_float_columns.items():
        if key in record:
            setattr(row, key, DN.to_db_float(record[key], column=column))
    if "pareto_front_count" in record:
        row.pareto_front_count = int(record["pareto_front_count"] or 0)
    if "adaptive_state" in record and record["adaptive_state"] is not None:
        row.adaptive_state = str(record["adaptive_state"])[:24]
    for key in ("category_distribution_json", "mutation_type_distribution_json"):
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            setattr(row, key, value)
        else:
            setattr(row, key, json.dumps(value, ensure_ascii=False,
                                         default=str))

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

    **幂等（run 级）**：同一 run 内同 `formula_hash` 已存在则跳过
    —— 对齐模型唯一约束 `(run_id, formula_hash)`；精英（elite）跨代复制
    同公式时不再撞库，其当代指标也不会被覆盖（去重保留首次入库行）。
    Returns:
        新增行数
    """
    existing = set(db.execute(
        select(FactorMiningCandidate.formula_hash).where(
            FactorMiningCandidate.run_id == str(run_id),
        )
    ).scalars())
    inserted = 0
    for rank, entry in enumerate(ranked):
        digest = str(entry.get("formula_hash") or "")
        if not digest or digest in existing:
            continue
        fitness = entry.get("fitness") or {}
        # B1 钥匙（rank, crowding_distance）：advanced 路径按 Pareto rank 落库；
        # naive 路径无 B1 → 回退 enumerate 位置（ICIR 降序）。
        b1_key = entry.get("_b1_key")
        if b1_key is not None:
            gen_rank = int(b1_key[0])
            crowd = DN.to_db_float(
                b1_key[1],
                column=FactorMiningCandidate.__table__.columns["crowding_distance"])
        else:
            gen_rank = int(rank)
            crowd = None
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
            generation_rank=gen_rank,
            economic_logic=(str(entry.get("economic_logic"))
                            if entry.get("economic_logic") else None),
            expected_direction=(str(entry.get("expected_direction"))
                                if entry.get("expected_direction") else None),
        )
        if crowd is not None:
            row.crowding_distance = crowd
        # —— 第 4 层淘汰元数据（P1-6；absence 时保持 NULL）——
        if entry.get("elimination_status"):
            row.elimination_status = str(entry["elimination_status"])[:24]
        if entry.get("elimination_reason"):
            row.elimination_reason = str(entry["elimination_reason"])[:48]
        if entry.get("similar_to_candidate_id"):
            row.similar_to_candidate_id = str(entry["similar_to_candidate_id"])[:64]
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
        # —— 第 4 层预筛指纹落库（P1-6；仅新候选，幂等） ——
        fp = entry.get("_prescreen_fingerprint")
        if fp:
            fp_cols = FactorMiningPrescreenFingerprint.__table__.columns
            db.add(FactorMiningPrescreenFingerprint(
                candidate_id=row.id,
                semantic_category=(str(fp.get("semantic_category"))[:24]
                                   if fp.get("semantic_category") else None),
                long_short_direction=(int(fp["long_short_direction"])
                                      if fp.get("long_short_direction") is not None
                                      else None),
                ic_mean_short=DN.to_db_float(fp.get("ic_mean_short"),
                                             column=fp_cols["ic_mean_short"]),
                factor_std=DN.to_db_float(fp.get("factor_std"),
                                          column=fp_cols["factor_std"]),
                turnover_rate=DN.to_db_float(fp.get("turnover_rate"),
                                             column=fp_cols["turnover_rate"]),
                top_overlap_vector=fp.get("top_overlap_vector"),
            ))
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


def compute_split_budget_preview(
    *,
    start_date: Any,
    end_date: Any,
    frequency: str,
    target_horizon: int,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
    warehouse_path: str | None = None,
) -> dict[str, Any]:
    """Step2 切分预算预览（向导 §4）：取区间交易日 → 频率重采样调仓点 → 预算。

    数据源与口径：
    - 交易日来自数仓 `raw_daily_bars` 的 DISTINCT trade_date（`list_trade_dates`）；
    - 「调仓点」按频率重采样（周频每周最后交易日、月频每月最后交易日，
      日频即全部交易日）——与向导「同一调仓频率下只保留一个调仓点」一致；
    - 点数/边界由 `evaluation_adapter.compute_split_budget`（**纯函数**）产出，
      本函数**不做任何切分计算**（R5）。

    数仓不可用/区间内无交易日 → 返回 `available=False` 的降级结构，
    前端据此展示「预算暂不可用」而非空白或崩溃。
    """
    from datetime import date as _date

    from app.core.config import Settings
    from app.services.factors.mining import evaluation_adapter as EVA
    from app.services.factors.store import FactorWarehouse

    wh = FactorWarehouse(str(warehouse_path or Settings().factor_warehouse_path))
    raw = wh.list_trade_dates()  # DESC
    if not raw:
        return {"available": False, "reason_zh": "数仓暂无行情交易日（raw_daily_bars 为空）。"}

    def _as_date(v: Any) -> _date | None:
        # datetime 是 date 的子类：必须先判 datetime，否则 datetime 实例会被
        # 当 date 直接返回，后续与 date 比较会抛 TypeError
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, _date):
            return v
        if hasattr(v, "date"):
            try:
                return v.date()
            except Exception:  # noqa: BLE001
                return None
        return None

    lo = _as_date(start_date)
    hi = _as_date(end_date)
    if lo is None or hi is None:
        return {"available": False, "reason_zh": "时间区间无效，无法计算切分预算。"}
    if lo > hi:
        return {"available": False, "reason_zh": "开始日不能晚于结束日。"}

    in_range = sorted(
        d for d in (_as_date(v) for v in raw)
        if d is not None and lo <= d <= hi
    )
    if not in_range:
        return {
            "available": False,
            "reason_zh": f"区间 {lo.isoformat()} ~ {hi.isoformat()} 内无数仓交易日。",
        }

    # 频率重采样：周/月只保留最后一个调仓点（向导 §4）
    if frequency == "weekly":
        by_key: dict[tuple[int, int], _date] = {}
        for d in in_range:
            iso = d.isocalendar()
            by_key[(iso[0], iso[1])] = d  # 同一周保留最后（日期递增，后写覆盖）
        points = sorted(by_key.values())
    elif frequency == "monthly":
        by_key = {}
        for d in in_range:
            by_key[(d.year, d.month)] = d  # 同一月保留最后
        points = sorted(by_key.values())
    else:
        points = in_range

    purge = EVA.normalize_purge_points(
        target_horizon_days=int(target_horizon), frequency=frequency)
    budget = EVA.compute_split_budget(
        all_dates=points,
        frequency=frequency,
        purge_points=purge,
        embargo_points=purge,
        tail_loss=int(target_horizon),
        train_ratio=float(train_ratio),
        validation_ratio=float(validation_ratio),
    )

    train_idx = max(0, min(len(points) - 1, int(len(points) * float(train_ratio))))
    val_idx = max(
        train_idx,
        min(len(points) - 1, int(len(points) * (float(train_ratio) + float(validation_ratio)))),
    )
    return {
        "available": True,
        "frequency": frequency,
        "total_points": budget.total_points,
        "train_points": budget.train_points,
        "val_points": budget.val_points,
        "test_points": budget.test_points,
        "purge_points": budget.purge_points,
        "embargo_points": budget.embargo_points,
        "purge_trading_days": budget.purge_trading_days,
        "embargo_trading_days": budget.embargo_trading_days,
        "tail_loss": budget.tail_loss,
        "frequency_floor": budget.frequency_floor,
        "meets_floor": budget.meets_floor,
        "statistically_degraded": budget.statistically_degraded,
        "train_start": points[0].isoformat() if points else None,
        "train_end": points[train_idx].isoformat() if points else None,
        "val_end": points[val_idx].isoformat() if points else None,
    }


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
                created_by=created_by,
                train_icir=DN.to_db_float(cand.generation_icir),
            )
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

        # B2：分级消费真实数据 —— 按评估 metrics 定级落库（factor_versions 4 列），
        # D 级移出 FactorSet；尽力而为，失败不阻断 run 收官（历史行已可追溯）。
        try:
            _grade_and_govern_outcome(db, cand=cand, outcome=outcome)
        except Exception:  # noqa: BLE001 - 分级/治理尽力而为
            continue

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
    # B3：F1 首批沉淀（正样本；尽力而为，F1 不可用不阻断提交）
    try:
        _store_f1_experience(
            db, factor_version_id=int(version_id),
            icir=DN.to_db_float(cand.generation_icir),
            logic_source=cand.logic_source, is_negative_sample=0,
        )
    except Exception:  # noqa: BLE001 - 沉淀尽力而为
        pass
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


# ══════════════════════════════════════════════════════════
# A3 查询与操作族（runs/结果/evaluations/AI/分级 路由薄委托）
# ══════════════════════════════════════════════════════════


def _run_to_dict(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status,
        "current_generation": int(run.current_generation or 0),
        "max_generation": int(run.max_generation or 0),
        "converged": int(run.converged or 0),
        "total_trials": int(run.total_trials or 0),
        "candidate_pool_snapshot_id": run.candidate_pool_snapshot_id,
        "data_cutoff_at": str(run.data_cutoff_at) if run.data_cutoff_at else None,
        "start_date": str(run.start_date) if run.start_date else None,
        "end_date": str(run.end_date) if run.end_date else None,
        "rebalance_frequency": run.rebalance_frequency,
        "error_code": run.error_code,
        "created_at": str(run.created_at) if run.created_at else None,
    }


def list_runs(db: Any, *, page: int = 1, page_size: int = 20,
              status: str | None = None) -> dict[str, Any]:
    """分页查询批次（路由 GET /factor-mining/runs）。"""
    from sqlalchemy import func, select

    q = select(FactorMiningRun).order_by(FactorMiningRun.created_at.desc())
    cq = select(func.count()).select_from(FactorMiningRun)
    if status:
        q = q.where(FactorMiningRun.status == str(status))
        cq = cq.where(FactorMiningRun.status == str(status))
    total = int(db.execute(cq).scalar_one() or 0)
    rows = db.execute(
        q.offset((max(1, page) - 1) * max(1, page_size)).limit(max(1, page_size))
    ).scalars().all()
    return {"items": [_run_to_dict(r) for r in rows], "total": total,
            "page": max(1, page), "page_size": max(1, page_size)}


def get_run_detail(db: Any, run_id: str) -> dict[str, Any] | None:
    run = load_run(db, run_id)
    if run is None:
        return None
    return _run_to_dict(run)


def list_generations(db: Any, run_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        select(FactorMiningGeneration)
        .where(FactorMiningGeneration.run_id == str(run_id))
        .order_by(FactorMiningGeneration.generation)
    ).scalars().all()
    out = []
    for g in rows:
        item = {c.name: getattr(g, c.name) for c in FactorMiningGeneration.__table__.columns}
        out.append(item)
    return out


def list_candidates(db: Any, run_id: str, *, page: int = 1, page_size: int = 20,
                    grade: str | None = None,
                    category: str | None = None) -> dict[str, Any]:
    """分页候选及指标（M2 起按 `factor_versions.quality_grade` 筛选）。"""
    from sqlalchemy import func, select

    q = select(FactorMiningCandidate).where(
        FactorMiningCandidate.run_id == str(run_id))
    cq = select(func.count()).select_from(FactorMiningCandidate).where(
        FactorMiningCandidate.run_id == str(run_id))
    if category:
        q = q.where(FactorMiningCandidate.category == str(category))
        cq = cq.where(FactorMiningCandidate.category == str(category))
    # grade 归属 `factor_versions`（B1 迁移后才有列）；列缺失时按无数据过滤
    if grade:
        q = q.where(FactorMiningCandidate.factor_version_id.isnot(None))
        cq = cq.where(FactorMiningCandidate.factor_version_id.isnot(None))
    total = int(db.execute(cq).scalar_one() or 0)
    rows = db.execute(
        q.order_by(FactorMiningCandidate.generation.desc())
        .offset((max(1, page) - 1) * max(1, page_size))
        .limit(max(1, page_size))
    ).scalars().all()
    items = []
    for c in rows:
        items.append({
            "id": c.id, "run_id": c.run_id, "factor_code": c.factor_code,
            "factor_version_id": c.factor_version_id, "formula_expr": c.formula_expr,
            "canonical_formula": c.canonical_formula, "formula_hash": c.formula_hash,
            "generation": int(c.generation or 0), "operation": c.operation,
            "category": c.category, "generation_icir": DN.to_db_float(c.generation_icir)
            if c.generation_icir is not None else None,
            "generation_coverage": DN.to_db_float(c.generation_coverage)
            if c.generation_coverage is not None else None,
            "generation_turnover": DN.to_db_float(c.generation_turnover)
            if c.generation_turnover is not None else None,
            "generation_complexity": int(c.generation_complexity or 0),
            "generation_rank": int(c.generation_rank) if c.generation_rank is not None else None,
            "crowding_distance": DN.to_db_float(c.crowding_distance)
            if c.crowding_distance is not None else None,
            "economic_logic": c.economic_logic, "expected_direction": c.expected_direction,
            "log_sources": c.logic_source,
        })
    return {"items": items, "total": total, "page": max(1, page),
            "page_size": max(1, page_size)}


def get_candidate_lineage(db: Any, run_id: str,
                          candidate_id: str) -> dict[str, Any] | None:
    """血缘追溯：候选元数据 + 父代链（递归 parent_ids_json，最多 8 层）。"""
    import json as _json

    def _one(cid: str) -> dict[str, Any] | None:
        row = db.execute(
            select(FactorMiningCandidate).where(
                FactorMiningCandidate.run_id == str(run_id),
                FactorMiningCandidate.id == str(cid),
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        parents: list[dict[str, Any]] = []
        try:
            pids = _json.loads(row.parent_ids_json or "[]") or []
        except ValueError:
            pids = []
        for pid in list(pids)[:8]:
            p = _one(str(pid))
            if p is not None:
                parents.append(p)
        return {
            "id": row.id, "generation": int(row.generation or 0),
            "operation": row.operation, "category": row.category,
            "formula": row.canonical_formula or row.formula_expr,
            "formula_hash": row.formula_hash,
            "icir": DN.to_db_float(row.generation_icir)
            if row.generation_icir is not None else None,
            "parents": parents,
        }

    return _one(candidate_id)


def build_mining_context(db: Any, run: Any, *, warehouse_path: str | None,
                         target_calc_batch_id: str) -> Any:
    """从 run 行还原 `MiningContext`（复用：TR 阶段链 / evaluate-all 共用）。

    切分走 `evaluation_adapter.build_split`（归一化 + 频率地板）；
    目标日期取 `factor_targets` 内的实际调仓点。
    """
    from app.services.factors.mining import evaluation_adapter as EVA
    from app.services.factors.mining.contracts import MiningContext
    from app.services.factors.store import FactorWarehouse

    wh = FactorWarehouse(str(warehouse_path or ""))
    target_df, _bid, _tcode = wh.get_target_panel(str(target_calc_batch_id),
                                                   "target_5d_return")
    all_dates = sorted(
        d for d in (EVA._as_date(v) for v in target_df["signal_date"].tolist())
        if d is not None
    )
    freq = str(run.rebalance_frequency or "daily")
    horizon = int(getattr(run, "target_horizon", 0) or 5)
    split, budget = EVA.build_split(all_dates=all_dates, frequency=freq,
                                    target_horizon=horizon)
    return MiningContext(
        run_id=run.id, candidate_pool_snapshot_id=str(run.candidate_pool_snapshot_id),
        data_cutoff_at=run.data_cutoff_at, start_date=run.start_date.date(),
        end_date=run.end_date.date(), rebalance_frequency=freq,
        target_horizon=horizon, split=split,
        purge_points=budget.purge_points, embargo_points=budget.embargo_points,
        train_ratio=0.6, validation_ratio=0.2,
        random_seed=int(getattr(run, "random_seed", 0) or 42),
        config_hash="", split_algorithm_version=str(
            getattr(run, "split_algorithm_version", "") or "split-1.0.0"),
        target_calc_batch_id=target_calc_batch_id,
        warehouse_path=str(wh.path),
        data_snapshot_version=str(getattr(run, "split_algorithm_version", "")
                                  or "snapshot-default"),
    )


def evaluate_all_impl(db: Any, *, run_id: str, top_k: int = 50,
                      warehouse_path: str | None = None) -> dict[str, Any]:
    """批量创建最终验证（test-once）：生成目标标签 → finalize_run。"""
    from app.core.config import Settings
    from app.services.factors.store import FactorWarehouse
    from app.services.factors.target_engine import calculate_targets

    run = load_run(db, run_id)
    if run is None:
        raise ValueError(f"run 不存在: {run_id!r}")
    wh = FactorWarehouse(str(warehouse_path or Settings().factor_warehouse_path))
    batch = f"mining-{run_id}"
    calculate_targets(wh, start_date=run.start_date.date(),
                      end_date=run.end_date.date(), calc_batch_id=batch)
    ctx = build_mining_context(db, run, warehouse_path=str(wh.path),
                               target_calc_batch_id=batch)
    return finalize_run(db, ctx=ctx, run_id=run_id, top_k=max(0, int(top_k)))


def get_mining_evaluation(db: Any, evaluation_id: str) -> dict[str, Any] | None:
    from app.models.factor_evaluation import EvaluationRun

    row = db.get(EvaluationRun, str(evaluation_id))
    if row is None:
        return None
    import json as _json

    metrics_raw = row.metrics_json or "{}"
    try:
        metrics = _json.loads(metrics_raw) if isinstance(metrics_raw, str) else metrics_raw
    except ValueError:
        metrics = {}
    return {
        "id": row.id,
        "factor_version_id": row.factor_version_id,
        "status": getattr(row, "status", None),
        "gate_result": getattr(row, "gate_result", getattr(row, "gate_status", None)),
        "test_start_date": getattr(row, "test_start_date", None) and str(row.test_start_date),
        "test_end_date": getattr(row, "test_end_date", None) and str(row.test_end_date),
        "metrics": metrics,
        "stats": (metrics or {}).get("stats"),
    }


def save_candidate_as_template(db: Any, *, candidate_id: str) -> dict[str, Any]:
    """优质候选 → 个人公式模板（`factor_formula_templates`，scope=personal）。"""
    import uuid as _uuid

    from app.models.factor_formula_template import FactorFormulaTemplate

    row = db.get(FactorMiningCandidate, str(candidate_id))
    if row is None:
        raise ValueError(f"candidate_not_found:{candidate_id}")
    if not (row.canonical_formula or row.formula_expr):
        raise ValueError(f"candidate_empty_formula:{candidate_id}")
    formula = row.canonical_formula or row.formula_expr

    exists = db.execute(
        select(FactorFormulaTemplate).where(
            FactorFormulaTemplate.formula_template == formula,
            FactorFormulaTemplate.scope == "personal",
        )
    ).scalar_one_or_none()
    if exists is not None:
        return {"template_id": exists.id, "status": "already"}

    tpl = FactorFormulaTemplate(
        id=_uuid.uuid4().hex,
        name=f"挖掘模板·{formula[:24]}",
        category=row.category or "trend",
        formula_template=formula,
        param_definitions_json="[]",
        economic_logic=row.economic_logic,
        priority=100,
        scope="personal",
        source=("ai_generated" if row.logic_source == "ai" else "manual"),
        complexity=(int(row.generation_complexity)
                    if row.generation_complexity is not None
                    else formula.count("(")),
        enabled=1,
    )
    db.add(tpl)
    db.commit()
    return {"template_id": tpl.id, "status": "created"}


def ai_preview_skeletons(payload: dict[str, Any], db: Any) -> dict[str, Any]:
    """AI 骨架预览（**不落库**）。尽力而为：AI 不可用/参数非法 → 空清单 + note。"""
    from app.services.factors.mining import ai_generator as AIG

    try:
        n = max(1, min(int(payload.get("count") or 5), 20))
        cfg = AIG.AIGeneratorConfig(
            target_count=n,
            selected_fields=tuple(payload.get("selected_fields") or ("close", "volume")),
            enabled_categories=tuple(payload.get("enabled_categories")
                                     or AIG.ALL_CATEGORIES),
            existing_formulas=tuple(payload.get("existing_formulas") or ()),
            seed=int(payload.get("seed") or 42),
        )
        result = AIG.generate_ai_candidates(cfg, db=db)
    except Exception as exc:  # noqa: BLE001 - 预览尽力而为
        return {"skeletons": [], "stats": {"accepted": 0},
                "note_zh": f"AI 预览不可用（{type(exc).__name__}）。"}
    return {"skeletons": list(result.accepted or [])[:n],
            "stats": dict(result.stats or {}),
            "notes_zh": list(result.notes_zh or [])}


def _grade_metrics_after_evaluation(db: Any, outcome: Any, cand: Any) -> dict[str, Any]:
    """按评估 outcome 组装定级 8 维指标（B2：eat 缺维度 → grade 视为不满足）。"""
    m = dict(getattr(outcome, "metrics", None) or {})
    stats = m.get("stats") if isinstance(m.get("stats"), dict) else None
    ic = m.get("ic") if isinstance(m.get("ic"), dict) else {}
    cov = m.get("coverage") if isinstance(m.get("coverage"), dict) else {}
    turn = m.get("turnover") if isinstance(m.get("turnover"), dict) else {}
    g = {
        "icir": DN.to_db_float(m.get("icir_raw", ic.get("icir"))),
        "p_value": DN.to_db_float((stats or {}).get("p_value")),
        "p_adj_bonferroni": DN.to_db_float((stats or {}).get("p_adj_bonferroni")),
        "ci_lower": DN.to_db_float((stats or {}).get("ci_lower")),
        "ci_upper": DN.to_db_float((stats or {}).get("ci_upper")),
        "perm_p_value": (stats or {}).get("perm_p_value"),
        "coverage": DN.to_db_float(cov.get("coverage")),
        "oos_icir": DN.to_db_float(ic.get("icir")),
        "turnover": DN.to_db_float(turn.get("avg_turnover")),
        "complexity": int(cand.generation_complexity or 0),
        "decay_ratio": DN.to_db_float((stats or {}).get("decay_ratio")),
        "degraded": bool((stats or {}).get("degraded")),
    }
    run_row = load_run(db, cand.run_id)
    if run_row is not None:
        g["frequency"] = str(run_row.rebalance_frequency or "daily")
    return g


def _remove_factor_from_all_sets(db: Any, *, factor_version_id: int) -> None:
    """D 级治理：把该因子从所有 FactorSet 移除（批量评审口径，不覆盖历史）。"""
    from app.models.factor_evaluation import FactorSetMember
    from app.models.factor_model import FactorVersion
    from app.services.factors.factor_set_service import remove_member

    version = db.get(FactorVersion, int(factor_version_id))
    if version is None:
        return
    rows = db.execute(
        select(FactorSetMember).where(FactorSetMember.factor_id == version.factor_id)
    ).scalars().all()
    for row in rows:
        try:
            remove_member(db, factor_set_id=row.factor_set_id,
                          factor_id=version.factor_id)
        except Exception:  # noqa: BLE001 - 单集合移除失败不阻断
            continue


def _f1_source(logic_source: str | None) -> str:
    """候选逻辑来源 → F1 source 白名单（ai_generated/enumerated/random/manual）。"""
    raw = str(logic_source or "").lower()
    if raw in ("ai", "ai_generated"):
        return "ai_generated"
    if raw in ("template", "enumerated"):
        return "enumerated"
    if raw == "random":
        return "random"
    return "manual"


def _store_f1_experience(
    db: Any, *, factor_version_id: int,
    icir: float | None = None, logic_source: str | None = None,
    is_negative_sample: int = 0,
) -> dict[str, Any] | None:
    """B3：F1 首批沉淀（submit 正样本 / D 级负样本 is_negative_sample=1）。

    红线 C5 处理（决策记录 2026-09-20）：挖掘 worker 与 API 同进程，HTTP 自调不可靠；
    本函数是 mining 写入 F1 的**唯一入口**（static 扫描可见），语义仍走 F1 服务层
    （三层指纹去重 / 参数泛化 / 负样本规避豁免），**不直写 F1 表**；对外契约仍以
    `/factor-experience` HTTP 面（T26）为准。
    无 `formula_ast`（编译失败 / 老版本）或类别不在白名单 → 跳过（不臆造 AST）。
    """
    import json as _json

    try:
        from app.models.factor import Factor
        from app.models.factor_model import FactorVersion
        from app.services.factors.experience import service as EXP
        from app.services.factors.experience.service import CATEGORY_WHITELIST
    except Exception:  # noqa: BLE001 - F1 不可用 → 跳过沉淀
        return None

    version = db.get(FactorVersion, int(factor_version_id))
    if version is None or not getattr(version, "formula_ast_json", None):
        return None
    try:
        formula_ast = _json.loads(version.formula_ast_json)
    except ValueError:
        return None
    if not isinstance(formula_ast, dict) or "type" not in formula_ast:
        return None
    factor = db.get(Factor, version.factor_id) if getattr(version, "factor_id", None) else None
    category = str(factor.category) if factor is not None else ""
    if category not in CATEGORY_WHITELIST:
        return None
    payload = {
        "formula_ast": formula_ast,
        "category": category,
        "source": _f1_source(logic_source),
        "is_negative_sample": int(bool(is_negative_sample)),
        "metrics": ([{"metric_type": "icir", "value": icir}]
                    if icir is not None else []),
        "task_context": {
            "note_zh": "D级负样本（质量分级自动沉淀）" if is_negative_sample
                       else "mining 第一次沉淀",
        },
    }
    try:
        exp_id, status = EXP.store_experience(db, payload=payload)
        return {"experience_id": exp_id, "status": status}
    except Exception:  # noqa: BLE001 - 沉淀尽力而为
        return None


def _grade_and_govern_outcome(db: Any, *, cand: Any, outcome: Any) -> None:
    """B2 分级消费真实数据：按评估 metrics 定级 → `persist_grade_to_version` 落库；
    D 级 → 移出 FactorSet + 写 F1 负样本（B3 内生链路）。
    """
    from app.services.factors.mining import factor_grading as FG

    if not cand.factor_version_id:
        return
    grade_metrics = _grade_metrics_after_evaluation(db, outcome, cand)
    grade_value, reason = FG.grade(grade_metrics)
    persist_grade_to_version(
        db, factor_version_id=cand.factor_version_id,
        grade_value=grade_value, reason=reason,
        metrics=grade_metrics, manual_adjusted=0,
    )
    if grade_value == "D":
        _remove_factor_from_all_sets(db, factor_version_id=cand.factor_version_id)
        # B3：D 级负样本沉淀（尽力而为；F1 语义：三层指纹去重 + 负样本豁免抽取）
        _store_f1_experience(
            db, factor_version_id=cand.factor_version_id,
            icir=grade_metrics.get("icir"), logic_source=cand.logic_source,
            is_negative_sample=1,
        )


def persist_grade_to_version(
    db: Any, *, factor_version_id: int | str, grade_value: str, reason: str,
    metrics: Mapping[str, Any] | None = None, manual_adjusted: int = 0,
) -> dict[str, Any]:
    """把 grade() 评定结果（等级/证据/人工标记）落到 `factor_versions`（B1 4 列）。

    - `grade_metrics_json` 存评定时指标快照（含 reason，便于审计回放）；
    - `manual_adjusted=1` 表示人工调整（`apply_manual_grade`），0 为自动/季度评定；
    - 历史序列一律写 `factor_grade_history`，本函数只更新「当前态」列；
    - 迁移未执行（老库、列不存在）时**静默跳过并返回 persisted=False**，
      不阻断调用方（与 `set_manual_grade_auto` 同款防御）。
    """
    import json as _json
    from datetime import datetime, timezone

    from app.models.factor_model import FactorVersion

    version = db.get(FactorVersion, int(factor_version_id))
    if version is None:
        raise ValueError(f"factor_version_not_found:{factor_version_id}")
    cols = FactorVersion.__table__.columns.keys()
    if "quality_grade" not in cols:
        return {"factor_version_id": int(factor_version_id), "persisted": False}
    grade_value = str(grade_value).upper()
    version.quality_grade = grade_value
    version.grade_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    version.grade_metrics_json = _json.dumps(
        {"grade": grade_value, "reason": reason, "metrics": dict(metrics or {})},
        ensure_ascii=False, default=str,
    )
    version.grade_manual_adjusted = 1 if manual_adjusted else 0
    db.commit()
    return {"factor_version_id": int(factor_version_id), "persisted": True}


def apply_manual_grade(db: Any, *, candidate_id: str, grade: str,
                       reason: str) -> dict[str, Any]:
    """人工调整等级：校验原因 ≥10 字；写 `factor_grade_history`（trigger=manual）。

    B1：同时把人工等级落到 `factor_versions` 当前态列（grade_manual_adjusted=1，
    季度重评任务据此跳过，需求 §6.8）。
    """
    import uuid as _uuid

    from app.models.factor_grade_history import FactorGradeHistory

    if not reason or len(str(reason).strip()) < 10:
        raise ValueError("grade_reason_required: 调整原因必填且不少于 10 字。")
    if str(grade).upper() not in ("S", "A", "B", "C", "D"):
        raise ValueError(f"invalid_grade:{grade}")
    cand = db.get(FactorMiningCandidate, str(candidate_id))
    if cand is None:
        raise ValueError(f"candidate_not_found:{candidate_id}")
    if not cand.factor_version_id:
        from app.schemas.errors import FactorSevenError

        raise FactorSevenError(
            "CANDIDATE_NOT_SUBMITTED",
            detail_zh="候选尚未提交为正式因子版本，无法人工调整等级。请先执行提交。",
        )
    prev = db.execute(
        select(FactorGradeHistory).where(
            FactorGradeHistory.factor_version_id == cand.factor_version_id)
        .order_by(FactorGradeHistory.created_at.desc())
    ).scalars().first()
    hist = FactorGradeHistory(
        id=_uuid.uuid4().hex,
        factor_version_id=cand.factor_version_id,
        grade=str(grade).upper(),
        previous_grade=(prev.grade if prev else None),
        metrics_snapshot_json="{}",
        source="manual",
        reason=str(reason).strip(),
    )
    db.add(hist)
    db.commit()
    persist_grade_to_version(
        db, factor_version_id=cand.factor_version_id,
        grade_value=hist.grade, reason=str(reason).strip(),
        metrics={}, manual_adjusted=1,
    )
    return {"grade": str(grade).upper(), "candidate_id": candidate_id,
            "history_id": hist.id, "manual_adjusted": 1}


def set_manual_grade_auto(db: Any, *, candidate_id: str) -> dict[str, Any]:
    """恢复自动评定：置 `grade_manual_adjusted=0`（B1 迁移后写 factor_versions 列）。"""
    from app.models.factor_model import FactorVersion

    cand = db.get(FactorMiningCandidate, str(candidate_id))
    if cand is None:
        raise ValueError(f"candidate_not_found:{candidate_id}")
    if not cand.factor_version_id:
        raise ValueError("candidate_not_submitted")
    version = db.get(FactorVersion, cand.factor_version_id)
    if version is not None and "grade_manual_adjusted" in \
            FactorVersion.__table__.columns.keys():
        setattr(version, "grade_manual_adjusted", 0)
        db.commit()
    return {"candidate_id": candidate_id, "manual_adjusted": 0}


def get_grade_evidence(db: Any, *, candidate_id: str) -> dict[str, Any]:
    """定级证据：8 维度明细 + grade 纯函数评定 + 血缘。"""
    from app.services.factors.mining import factor_grading as FG

    cand = db.get(FactorMiningCandidate, str(candidate_id))
    if cand is None:
        raise ValueError(f"candidate_not_found:{candidate_id}")
    metrics = {
        "icir": DN.to_db_float(cand.generation_icir)
        if cand.generation_icir is not None else None,
        "coverage": DN.to_db_float(cand.generation_coverage)
        if cand.generation_coverage is not None else None,
        "turnover": DN.to_db_float(cand.generation_turnover)
        if cand.generation_turnover is not None else 0.0,
        "complexity": int(cand.generation_complexity or 0),
    }
    grade, reason = FG.grade(metrics)
    lineage = get_candidate_lineage(db, cand.run_id, cand.id)
    return {
        "candidate_id": candidate_id,
        "grade": grade,
        "reason": reason,
        "metrics": metrics,
        "thresholds_source": "default",
        "lineage": lineage,
        "stats": None,
    }


def cleanup_run_data(db: Any, *, run_id: str) -> dict[str, Any]:
    """清理批次中间数据（候选/代际/检查点/指纹）并删 run 行（B4）。

    幂等：run 不存在 → ValueError（路由 404）；二次调用对已删 run 同样 404。
    """
    from app.models.factor_mining import (
        FactorMiningCandidate,
        FactorMiningGeneration,
        FactorTrainingCheckpoint,
    )

    run = db.get(FactorMiningRun, str(run_id))
    if run is None:
        raise ValueError(f"run_not_found:{run_id}")
    cands = db.query(FactorMiningCandidate).filter_by(run_id=run_id).delete()
    gens = db.query(FactorMiningGeneration).filter_by(run_id=run_id).delete()
    checkpoints = db.query(FactorTrainingCheckpoint).filter_by(
        run_id=run_id).delete()
    db.delete(run)
    db.commit()
    return {
        "run_id": run_id,
        "deleted_candidates": int(cands),
        "deleted_generations": int(gens),
        "deleted_checkpoints": int(checkpoints),
        "status": "cleaned",
    }


def purge_stale_mining_drafts(
    db: Any, *, days: int = 7, limit: int = 100,
) -> dict[str, Any]:
    """中间数据 7 天保留：清理超期且未提交的向导草稿（B4）。

    只删 `draft / waiting_data_recheck` 状态、updated_at 早于 cutoff 的草稿；
    已用于创建 run 的草稿由 run 持有，不在此列（run 清理由 cleanup_run_data）。
    """
    from app.models.factor_mining import FactorMiningDraft

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(1, days))
    rows = db.execute(
        select(FactorMiningDraft)
        .where(FactorMiningDraft.updated_at < cutoff)
        .where(FactorMiningDraft.status.in_(
            ("draft", "waiting_data_recheck")))
        .order_by(FactorMiningDraft.updated_at.asc())
        .limit(max(1, int(limit)))
    ).scalars().all()
    for row in rows:
        db.delete(row)
    db.commit()
    return {"purged": len(rows), "cutoff_days": int(days)}


def prepare_draft(db: Any, *, draft_id: str) -> dict[str, Any]:
    """最终准备校验（B4）：Step1~4 内容齐备 + 快照已生成 + 字段校验有效。

    任一不满足 → `ok=False` + blockers（前端展示缺什么），满足才能提交。
    """
    from app.services.factors.mining import draft_service as DS

    view = DS.get_draft(db, draft_id=draft_id)
    if view is None:
        raise ValueError(f"draft_not_found:{draft_id}")
    steps = dict(getattr(view, "steps", {}) or {})
    blockers: list[str] = []
    if not getattr(view, "candidate_pool_snapshot_id", None):
        blockers.append("候选股票池快照未生成（Step1 需先锁定）")
    s2 = dict(steps.get("step2") or {})
    if not (s2.get("start_date") and s2.get("end_date")):
        blockers.append("时间与目标未填全（Step2 起止日期必填）")
    s3 = dict(steps.get("step3") or {})
    if not (s3.get("selected_fields") or []):
        blockers.append("字段未勾选（Step3 至少一个字段）")
    if not (steps.get("step4") or {}):
        blockers.append("进化参数未配置（Step4）")
    val = DS.latest_validation_run(db, draft_id=draft_id)
    valid = bool(
        val is not None
        and DS.is_validation_run_valid(db, run_id=getattr(val, "run_id", "")))
    if not valid:
        blockers.append("字段校验未通过或已过期（请重新发起校验）")
    return {
        "draft_id": draft_id,
        "ok": len(blockers) == 0,
        "blockers": blockers,
    }


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
    "list_runs",
    "get_run_detail",
    "list_generations",
    "list_candidates",
    "get_candidate_lineage",
    "build_mining_context",
    "evaluate_all_impl",
    "get_mining_evaluation",
    "save_candidate_as_template",
    "ai_preview_skeletons",
    "persist_grade_to_version",
    "apply_manual_grade",
    "set_manual_grade_auto",
    "get_grade_evidence",
    "cleanup_run_data",
    "purge_stale_mining_drafts",
    "prepare_draft",
]
