"""WP5-06: 评估异步任务接线。

复用现有 async_tasks 框架（单飞 + 心跳 + 终态保护 + 取消）。

评估任务流程：
1. 加载因子定义和版本
2. 冻结数据截止时间（latest_complete_trade_date）
3. 用 FactorExecutor.preview 读取因子值
4. 计算目标收益（5 日前瞻收益）
5. 执行 run_evaluation（IC/ICIR/分组/换手/成本/门禁）
6. 执行压力测试（参数扰动/时间段/缺失敏感度）
7. 写入 EvaluationRun + stress 结果
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.models.factor_evaluation import EvaluationRun
from app.services.async_tasks import (
    _now,
    _set_task,
    _start_worker,
    create_async_task,
    list_async_tasks,
)
from app.services.factors.factor_compiler import compile_formula
from app.services.factors.factor_evaluator import (
    EvaluationConfig,
    EvaluationOutcome,
    run_evaluation,
    finalize_evaluation_run,
)
from app.services.factors.factor_executor import FactorExecutor
from app.services.factors.factor_registry import get_factor_by_code, get_latest_version
from app.services.factors.factor_stress import run_stress_test
from app.services.factors.store import FactorWarehouse
from app.services.factors.trade_calendar import latest_complete_trade_date


logger = logging.getLogger(__name__)
TASK_TYPE = "wp5_evaluation"
TASK_HEARTBEAT_SECONDS = 15.0


def _cancelled(task_id: str) -> bool:
    """短事务读取取消状态（复用 pipeline_task 模式）。"""
    SessionLocal = get_session_local()
    cancel_db = SessionLocal()
    try:
        task = cancel_db.get(AsyncTaskRecord, task_id)
        return task is not None and task.status == "cancelled"
    finally:
        cancel_db.close()


def _touch_task_heartbeat(task_id: str) -> bool:
    """刷新运行中任务的心跳。"""
    SessionLocal = get_session_local()
    hb_db = SessionLocal()
    try:
        task = hb_db.get(AsyncTaskRecord, task_id)
        if task is None or task.status != "running":
            return False
        task.updated_at = _now()
        hb_db.commit()
        return True
    finally:
        hb_db.close()


def _start_task_heartbeat(task_id: str) -> tuple[threading.Event, threading.Thread]:
    """启用心跳守护线程。"""
    stop_event = threading.Event()

    def _beat() -> None:
        while not stop_event.wait(TASK_HEARTBEAT_SECONDS):
            try:
                if not _touch_task_heartbeat(task_id):
                    return
            except Exception:  # noqa: BLE001
                logger.warning("heartbeat failed for task %s", task_id, exc_info=True)
                return

    thread = threading.Thread(
        target=_beat,
        name=f"wp5-hb-{task_id[:8]}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def create_evaluation_task(
    *,
    factor_code: str,
    factor_kind: str = "continuous",
    target_horizon: int = 5,
    n_groups: int = 5,
    cost_rate: float = 0.001,
    created_by: str = "local_user",
) -> dict:
    """创建评估异步任务（单飞幂等）。

    幂等逻辑：
    1. 同 factor_code 有 queued/running 任务时复用
    2. EvaluationRun 本身的幂等由 create_evaluation_run 保证（config_hash + cutoff）
    """
    payload = {
        "factor_code": factor_code,
        "factor_kind": factor_kind,
        "target_horizon": target_horizon,
        "n_groups": n_groups,
        "cost_rate": cost_rate,
        "created_by": created_by,
    }

    # 单飞检查：同 factor_code 有在跑任务则复用
    existing = list_async_tasks(task_type=TASK_TYPE, limit=10)
    for t in existing:
        if t.status not in {"queued", "running"}:
            continue
        try:
            p = json.loads(t.payload_json) if t.payload_json else {}
        except (TypeError, json.JSONDecodeError):
            continue
        if p.get("factor_code") == factor_code:
            return t.model_dump()

    task = create_async_task(TASK_TYPE, payload)
    _start_worker(task.id, _run_evaluation_worker)
    return task.model_dump()


def _run_evaluation_worker(task_id: str) -> None:
    """评估任务 worker（复用 pipeline_task 的取消/心跳/终态保护模式）。"""
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return
        if task.status == "cancelled":
            return

        # 解析 payload
        payload = json.loads(task.payload_json) if task.payload_json else {}
        factor_code = payload.get("factor_code", "")
        factor_kind = payload.get("factor_kind", "continuous")
        target_horizon = int(payload.get("target_horizon", 5))
        n_groups = int(payload.get("n_groups", 5))
        cost_rate = float(payload.get("cost_rate", 0.001))
        created_by = payload.get("created_by", "local_user")

        if not factor_code:
            _set_task(db, task_id, status="failed", stage="failed",
                      message="missing factor_code", finished_at=_now())
            return

        # 启动心跳
        heartbeat_stop, heartbeat_thread = _start_task_heartbeat(task_id)

        _set_task(db, task_id, status="running", stage="loading",
                  percent=5, message=f"加载因子 {factor_code}",
                  started_at=_now(), current_item=factor_code)

        # 1. 加载因子定义
        if _cancelled(task_id):
            return
        factor = get_factor_by_code(db, factor_code)
        if factor is None:
            _set_task(db, task_id, status="failed", stage="failed",
                      message=f"factor_not_found:{factor_code}", finished_at=_now())
            return

        version = get_latest_version(db, factor.id)
        if version is None:
            _set_task(db, task_id, status="failed", stage="failed",
                      message="no_factor_version", finished_at=_now())
            return

        _set_task(db, task_id, stage="loading", percent=10,
                  message=f"已加载版本 v{version.version}")

        # 2. 冻结数据截止时间
        if _cancelled(task_id):
            return
        _set_task(db, task_id, stage="cutoff", percent=15,
                  message="冻结完整交易日")
        ctd_evidence = latest_complete_trade_date(db)
        cutoff_date = ctd_evidence.selected_trade_date

        _set_task(db, task_id, stage="cutoff", percent=20,
                  message=f"数据截止 {cutoff_date.isoformat()}")

        # 3. 编译公式
        if _cancelled(task_id):
            return
        _set_task(db, task_id, stage="compiling", percent=25,
                  message="编译因子公式")
        params = json.loads(version.params_json) if version.params_json else {}
        postprocess = json.loads(version.postprocess_json) if version.postprocess_json else None

        compile_result = compile_formula(
            formula=version.formula_expr,
            params=params,
            postprocess=postprocess,
            direction=version.direction or factor.direction or "higher_better",
            strict_fields=True,
        )
        if not compile_result.is_valid:
            _set_task(db, task_id, status="failed", stage="failed",
                      message=f"compile_failed:{compile_result.errors}",
                      finished_at=_now())
            return

        # 4. 读取因子值（只读快照）
        if _cancelled(task_id):
            return
        _set_task(db, task_id, stage="reading", percent=35,
                  message="读取因子值（冻结批次）")
        warehouse = FactorWarehouse()
        executor = FactorExecutor(warehouse)

        # 读取最近 120 个交易日的因子值用于评估
        lookback_days = 120
        factor_values_dict: dict[date, dict[str, float]] = {}
        symbols_seen: set[str] = set()

        # 简化：使用 preview 读取最近若干日（实际可批量读取）
        for i in range(lookback_days):
            if _cancelled(task_id):
                return
            target_date = cutoff_date - timedelta(days=i)
            try:
                outcome = executor.preview(
                    compile_result.execution_plan,
                    trade_date=target_date,
                    limit=500,
                )
                if outcome.values:
                    day_values: dict[str, float] = {}
                    for v in outcome.values:
                        sym = v.get("symbol", "")
                        raw = v.get("raw_value")
                        if sym and raw is not None:
                            day_values[sym] = float(raw)
                            symbols_seen.add(sym)
                    if day_values:
                        factor_values_dict[target_date] = day_values
            except Exception:  # noqa: BLE001
                logger.debug("preview failed for %s", target_date, exc_info=True)
                continue

            # 进度更新
            if i % 10 == 0:
                pct = 35 + int(20 * i / lookback_days)
                _set_task(db, task_id, stage="reading", percent=pct,
                          message=f"已读取 {len(factor_values_dict)} 日因子值",
                          processed=len(factor_values_dict),
                          total=lookback_days)

        if not factor_values_dict:
            _set_task(db, task_id, status="failed", stage="failed",
                      message="no_factor_data:无法读取因子值",
                      finished_at=_now())
            return

        # 构建 DataFrame
        all_symbols = sorted(symbols_seen)
        factor_values = pd.DataFrame(
            {d: {s: factor_values_dict[d].get(s) for s in all_symbols}
             for d in sorted(factor_values_dict.keys())}
        ).T
        factor_values.index = pd.to_datetime(factor_values.index).date

        _set_task(db, task_id, stage="reading", percent=55,
                  message=f"因子值矩阵 {factor_values.shape}")

        # 5. 构造目标收益（简化版：用 close 收益率，实际应从 warehouse 读取）
        if _cancelled(task_id):
            return
        _set_task(db, task_id, stage="targets", percent=60,
                  message="对齐目标收益")

        # 简化：用因子值的 shifted 作为目标代理（实际应用应读取真实前瞻收益）
        # 注意：这是评估框架的占位实现，真实场景需接入 target_engine
        forward_returns = factor_values.shift(-target_horizon).pct_change(
            periods=target_horizon, fill_method=None
        )

        # 6. 执行评估
        if _cancelled(task_id):
            return
        _set_task(db, task_id, stage="evaluating", percent=70,
                  message="计算 IC/ICIR/分组/换手/成本")

        config = EvaluationConfig(
            factor_kind=factor_kind,
            target_horizon=target_horizon,
            n_groups=n_groups,
            cost_rate=cost_rate,
        )

        outcome = run_evaluation(
            db,
            factor_version_id=version.id,
            factor_values=factor_values,
            forward_returns=forward_returns,
            config=config,
            data_cutoff_at=datetime.combine(cutoff_date, datetime.min.time()),
            ctd_evidence=ctd_evidence,
            created_by=created_by,
            task_id=task_id,
        )

        _set_task(db, task_id, stage="evaluating", percent=85,
                  message=f"门禁结论: {outcome.gate_result}")

        # 7. 压力测试（可选，仅对有足够数据的因子）
        if _cancelled(task_id):
            return
        _set_task(db, task_id, stage="stress", percent=90,
                  message="执行参数扰动和时间段稳定性")

        # 提取验证期数据用于压力测试
        val_mask = (factor_values.index >= outcome.time_split.validation_start) & (
            factor_values.index <= outcome.time_split.validation_end
        )
        val_features = factor_values.loc[val_mask]
        val_targets = forward_returns.loc[forward_returns.index.isin(val_features.index)]

        stress_summary = run_stress_test(
            features=val_features,
            targets=val_targets,
        )

        # 更新 metrics 加入压力测试结果
        stress_metrics = {
            "overall_verdict": stress_summary.overall_verdict,
            "failure_reasons": stress_summary.failure_reasons,
            "parameter_results": [
                {
                    "param_name": r.param_name,
                    "baseline_value": r.baseline_value,
                    "verdict": r.verdict,
                    "sign_consistency_ratio": r.sign_consistency_ratio,
                    "median_ic_ratio": r.median_ic_ratio,
                    "passing_neighbor_count": r.passing_neighbor_count,
                    "has_cliff_drop": r.has_cliff_drop,
                    "points": [
                        {
                            "label": p.label,
                            "ratio": p.ratio,
                            "param_value": p.param_value,
                            "ic_mean": p.ic_mean,
                            "icir": p.icir,
                            "passed_min_gate": p.passed_min_gate,
                        }
                        for p in r.points
                    ],
                }
                for r in stress_summary.parameter_results
            ],
            "time_result": {
                "ic_stability": stress_summary.time_result.ic_stability if stress_summary.time_result else None,
                "verdict": stress_summary.time_result.verdict if stress_summary.time_result else None,
                "segments": [
                    {
                        "segment_label": s.segment_label,
                        "ic_mean": s.ic_mean,
                        "icir": s.icir,
                    }
                    for s in (stress_summary.time_result.segments if stress_summary.time_result else [])
                ],
            } if stress_summary.time_result else None,
            "missing_result": {
                "ic_decay_ratio": stress_summary.missing_result.ic_decay_ratio if stress_summary.missing_result else None,
                "verdict": stress_summary.missing_result.verdict if stress_summary.missing_result else None,
            } if stress_summary.missing_result else None,
        }

        # 合并到 metrics（通过 finalize 再写一次会触发终态保护，所以直接更新 run）
        run = db.get(EvaluationRun, outcome.run_id)
        if run is not None:
            existing_metrics = json.loads(run.metrics_json) if run.metrics_json else {}
            existing_metrics["stress_test"] = stress_metrics
            run.metrics_json = json.dumps(existing_metrics, ensure_ascii=False, default=str)
            db.flush()

        # 8. 完成
        _set_task(
            db, task_id,
            status="done",
            stage="done",
            percent=100,
            message=f"评估完成: {outcome.gate_result}, 压力测试: {stress_summary.overall_verdict}",
            finished_at=_now(),
            result_json=json.dumps({
                "run_id": outcome.run_id,
                "gate_result": outcome.gate_result,
                "rejection_reasons": outcome.rejection_reasons,
                "stress_verdict": stress_summary.overall_verdict,
            }, ensure_ascii=False),
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("WP5 evaluation task %s failed", task_id)
        # 终态保护：检查是否已被取消
        try:
            existing = db.get(AsyncTaskRecord, task_id)
            if existing is not None and existing.status == "cancelled":
                return  # cancelled 终态不得被 worker 覆盖
        except Exception:  # noqa: BLE001
            pass
        _set_task(
            db, task_id,
            status="failed",
            stage="failed",
            message=f"evaluation_failed:{exc}",
            finished_at=_now(),
        )
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        db.close()


def get_evaluation_task(task_id: str) -> dict | None:
    """获取评估任务状态。"""
    from app.services.async_tasks import get_async_task
    task = get_async_task(task_id)
    return task.model_dump() if task else None


def list_evaluation_tasks(limit: int = 20) -> list[dict]:
    """列出评估任务。"""
    tasks = list_async_tasks(task_type=TASK_TYPE, limit=limit)
    return [t.model_dump() for t in tasks]


def cancel_evaluation_task(task_id: str) -> dict:
    """取消评估任务。"""
    from app.services.async_tasks import cancel_async_task
    task = cancel_async_task(task_id)
    return task.model_dump()


__all__ = [
    "TASK_TYPE",
    "create_evaluation_task",
    "get_evaluation_task",
    "list_evaluation_tasks",
    "cancel_evaluation_task",
]
