"""WP5-06: 评估异步任务接线。

复用现有 async_tasks 框架（单飞 + 心跳 + 终态保护 + 取消）。

评估任务流程（修正后状态机顺序）：
1. 加载因子定义和版本
2. 冻结数据截止时间（latest_complete_trade_date）
3. 用 FactorExecutor.execute_panel 批量计算因子面板
4. 构造 raw/winsorized/normalized 三个 pivot 矩阵
5. 计算目标收益（5 日前瞻收益）
6. 对齐因子-目标 → 方向统一（apply_direction_alignment）
7. 执行 run_evaluation_phase1（IC/ICIR/分组/换手/成本，不写终态）
8. 执行压力测试（参数扰动/时间段/缺失敏感度）
9. 压力通过 → finalize(passed/rejected)，压力不通过 → finalize(warn)
10. 写入 EvaluationRun + stress 结果
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any, Literal

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
from app.services.factors.factor_compiler import FactorCompiler, compile_formula
from app.services.factors.factor_evaluator import (
    EvaluationConfig,
    EvaluationOutcome,
    GateResult,
    run_evaluation,
    finalize_evaluation_run,
    TERMINAL_GATE_RESULTS,
)
from app.services.factors.factor_executor import FactorExecutor
from app.services.factors import target_engine
from app.services.factors.factor_registry import get_factor_by_code, get_latest_version
from app.services.factors.factor_stress import run_stress_test
from app.services.factors.store import FactorWarehouse
from app.services.factors.trade_calendar import latest_complete_trade_date


logger = logging.getLogger(__name__)
TASK_TYPE = "wp5_evaluation"
TASK_HEARTBEAT_SECONDS = 15.0


# ══════════════════════════════════════════════════════════════════════════════
# 结构化 Blocker 约定：让前端能"按维度列出问题 + 给超链接修复"
# ══════════════════════════════════════════════════════════════════════════════
# severity:  error  = 阻止评估（必须修），warning = 不阻止但有提示
# category:  data    = 缺失/不足（→ 数据覆盖诊断 / 初始化补数）
#            formula = 公式本身（→ 因子编辑器）
#            version = 版本问题（→ 因子编辑器-版本区）
#            config  = 参数不合理（→ 评估实验室页面本身）
#            universe= 股票池/范围（→ 设置/基础数据）
# fix_link.tab：对应前端侧边栏菜单的激活 key（AppContext 用）
_BLOCKER_FIX_DATA_COVERAGE = {"tab": "data-health", "label_zh": "去查看数据覆盖诊断"}
_BLOCKER_FIX_INIT_BACKFILL = {"tab": "init-backfill", "label_zh": "去执行初始化补数"}
_BLOCKER_FIX_FACTOR_EDITOR = {"tab": "factors", "subtab": "editor", "label_zh": "去因子编辑器修复公式/版本"}
_BLOCKER_FIX_EXTERNAL_DATA = {"tab": "external-data", "label_zh": "去外部数据配置并同步"}


def _blocker(
    code: str,
    *,
    severity: str = "error",
    category: str = "data",
    title_zh: str,
    detail_zh: str | None = None,
    fix_link: dict | None = None,
    retryable: bool = False,
    evidence: dict | None = None,
    correlation_id: str | None = None,
) -> dict:
    corr = correlation_id or uuid.uuid4().hex[:8]
    b = {
        "code": code,
        "severity": severity,
        "category": category,
        "title_zh": title_zh,
        "detail_zh": detail_zh or title_zh,
        "correlation_id": corr,
    }
    # correlation_id 也放进 evidence 便于查询（前端+DB SQL 都能取）
    ev: dict = dict(evidence or {})
    ev.setdefault("correlation_id", corr)
    b["evidence"] = ev
    if fix_link:
        b["fix_link"] = fix_link
    if retryable:
        b["retryable"] = retryable
    return b


# ══════════════════════════════════════════════════════════════════════════════
# Part B-1: 配置哈希幂等（idempotency fingerprint）
# ══════════════════════════════════════════════════════════════════════════════

_IDEMPOTENCY_KEYS = (
    "factor_code",
    "factor_version_id",
    "universe",
    "start_date",
    "end_date",
    "target_horizon",
    "n_groups",
    "cost_rate",
    "direction",
)


def compute_payload_fingerprint(payload: dict[str, Any]) -> str:
    """规范化 payload → canonical JSON → SHA256 → 十六进制 fingerprint。

    用于 create_evaluation_task 的单飞幂等检查：
    - key 排序
    - 移除 None 字段
    - 日期/数字统一转为字符串规范化
    """
    normalized: dict[str, Any] = {}
    for k in sorted(payload.keys()):
        if k not in _IDEMPOTENCY_KEYS:
            continue
        v = payload.get(k)
        if v is None:
            continue
        if isinstance(v, date):
            normalized[k] = v.isoformat()
        elif isinstance(v, datetime):
            normalized[k] = v.date().isoformat()
        else:
            normalized[k] = v
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# Part B-2: 评价口径 selection（resolve_evaluation_column）
# ══════════════════════════════════════════════════════════════════════════════


def resolve_evaluation_column(
    factor_version_postprocess: dict | None,
) -> Literal["raw_value", "winsorized_value", "normalized_value"]:
    """根据 FactorVersion.postprocess 配置决定使用哪一列因子值。

    规则（纯函数，无副作用）：
    - 若 postprocess 中存在 zscore OR rank → normalized_value
    - 否则若存在 winsorize（且没 zscore/rank）→ winsorized_value
    - 否则 → raw_value
    """
    if not factor_version_postprocess:
        return "raw_value"
    if not isinstance(factor_version_postprocess, dict):
        return "raw_value"
    steps = factor_version_postprocess.get("steps") or []
    names = {
        (s.get("name") if isinstance(s, dict) else None)
        for s in steps
    }
    # 兼容直接写顶层键的旧格式：{"winsorize": {...}, "zscore": true}
    names.update(
        k for k in ("zscore", "rank", "winsorize")
        if factor_version_postprocess.get(k) not in (None, False)
    )
    if "zscore" in names or "rank" in names:
        return "normalized_value"
    if "winsorize" in names:
        return "winsorized_value"
    return "raw_value"


# ══════════════════════════════════════════════════════════════════════════════
# Part C-1: 因子方向统一（apply_direction_alignment）
# ══════════════════════════════════════════════════════════════════════════════


def apply_direction_alignment(
    aligned_features: pd.DataFrame,
    direction: str,
) -> tuple[pd.DataFrame, list[dict]]:
    """对齐后统一因子方向为 higher_better（IC 计算前调用）。

    返回 (adjusted_features, blockers, meta)。
    - higher_better：不变，空 blockers
    - lower_better：取反 -aligned_features，空 blockers
    - nonlinear：不变，追加 blocker（warn 级门禁降级）
    - unknown：不变，追加 warn blocker
    """
    blockers: list[dict] = []
    direction = (direction or "higher_better").strip().lower()

    if direction == "higher_better":
        return aligned_features.copy(), blockers

    if direction == "lower_better":
        negated = -aligned_features
        return negated, blockers

    if direction == "nonlinear":
        blockers.append(_blocker(
            "eval.direction.nonlinear_not_supported",
            severity="warn",
            category="config",
            title_zh="非线性因子方向标记：IC/分组解释降级为 warn",
            detail_zh=(
                "direction=nonlinear 表示因子与收益非单调关系，"
                "Rank IC / 分组多空等基于线性单调的指标解释力下降。"
                "门禁不会直接失败，但 gate_result 会降级为 warn。"
            ),
            evidence={"direction": "nonlinear"},
            fix_link=_BLOCKER_FIX_FACTOR_EDITOR,
            retryable=True,
        ))
        return aligned_features.copy(), blockers

    blockers.append(_blocker(
        "eval.direction.unknown",
        severity="warn",
        category="config",
        title_zh=f"未知因子方向标记：{direction}",
        detail_zh=(
            "因子 direction 字段未设置为合法值（higher_better/lower_better/nonlinear），"
            "默认按 higher_better 处理，门禁降级为 warn。"
        ),
        evidence={"direction": direction, "expected": ["higher_better", "lower_better", "nonlinear"]},
        fix_link=_BLOCKER_FIX_FACTOR_EDITOR,
        retryable=True,
    ))
    return aligned_features.copy(), blockers


def resolve_forward_returns(
    warehouse,
    factor_values: pd.DataFrame,
    target_horizon: int,
    *,
    _target_engine_module=None,
) -> tuple[pd.DataFrame, list[dict], dict]:
    """解析目标收益（真实标签或 fallback）。

    返回 (forward_returns, blockers, run_context)。
    - forward_returns: 对齐后的前瞻收益 DataFrame
    - blockers: 本步骤产生的 blocker 列表
    - run_context: dict(target_code, target_horizon, latest_batch_id, fallback_used)
    """
    import pandas as pd

    te = _target_engine_module if _target_engine_module is not None else target_engine
    target_code = f"target_{target_horizon}d_return"
    latest_batch_id = warehouse.get_latest_target_batch_id(target_code)
    fallback_used = False
    blockers: list[dict] = []

    def _fallback_shift_pct() -> pd.DataFrame:
        return factor_values.shift(-target_horizon).pct_change(
            periods=target_horizon, fill_method=None
        )

    forward_returns: pd.DataFrame | None = None

    if latest_batch_id is None:
        if target_horizon == 5 and hasattr(te, "calculate_targets"):
            try:
                batch_result = te.calculate_targets(warehouse=warehouse)
                latest_batch_id = (
                    batch_result.calc_batch_id
                    if batch_result and getattr(batch_result, "tradable_rows", 0) > 0
                    else None
                )
            except Exception as e:
                blockers.append(_blocker(
                    "eval.data.target_engine_exception",
                    severity="warn",
                    category="data",
                    title_zh="即时生成目标标签失败",
                    detail_zh=(
                        f"尝试即时调用 target_engine 生成 {target_code} 失败："
                        f"{type(e).__name__}: {e}。将回退至占位同源收益率。"
                    ),
                    fix_link={"tab": "factor-laboratory", "subtab": "targets",
                              "label_zh": "目标标签管理"},
                ))
        if latest_batch_id is None:
            if target_horizon != 5:
                blockers.append(_blocker(
                    "eval.data.target_horizon_unavailable",
                    severity="error",
                    category="data",
                    title_zh=f"目标标签不存在：{target_code}",
                    detail_zh=(
                        f"当前 target_horizon={target_horizon}，仓库中未找到 "
                        f"{target_code} 的可交易冻结批次，且无即时计算引擎支持。"
                        "评价无法继续，请先在标签中心生成并冻结对应 horizon 的目标标签。"
                    ),
                    evidence={"target_code": target_code,
                              "target_horizon": target_horizon},
                    fix_link={"tab": "factor-laboratory", "subtab": "targets",
                              "label_zh": "目标标签管理"},
                    retryable=True,
                ))
                return (
                    pd.DataFrame(),
                    blockers,
                    {
                        "target_code": target_code,
                        "target_horizon": target_horizon,
                        "latest_batch_id": None,
                        "fallback_used": False,
                    },
                )
            fallback_used = True
            blockers.append(_blocker(
                "eval.data.target_fallback_used",
                severity="warn",
                category="data",
                title_zh="目标收益使用占位同源收益率",
                detail_zh=(
                    f"未找到 {target_code} 的可交易冻结批次，IC 不具备业务含义。"
                    f"请先在标签中心生成并冻结 {target_code}。"
                ),
                evidence={"target_code": target_code},
                fix_link={"tab": "factor-laboratory", "subtab": "targets",
                          "label_zh": "目标标签管理"},
                retryable=True,
            ))
            forward_returns = _fallback_shift_pct()
    else:
        try:
            target_panel, _, _ = warehouse.get_target_panel(
                latest_batch_id, target_code
            )
            if target_panel is not None and not target_panel.empty:
                if target_panel["signal_date"].dtype != object:
                    target_panel["signal_date"] = pd.to_datetime(
                        target_panel["signal_date"],
                        errors="coerce",
                    ).dt.strftime("%Y-%m-%d")
                pivoted = target_panel.pivot_table(
                    index="signal_date",
                    columns="symbol",
                    values="target_value",
                    aggfunc="first",
                )
                pivoted.index = pd.to_datetime(
                    pivoted.index, errors="coerce"
                ).date
                pivoted = pivoted.loc[
                    [pd.notna(x) for x in pivoted.index]
                ]
                if pivoted.empty:
                    raise ValueError("pivoted panel is empty after date coerce")
                forward_returns = pivoted
            else:
                raise ValueError("target_panel empty")
        except Exception as panel_exc:
            fallback_used = True
            blockers.append(_blocker(
                "eval.data.target_panel_empty",
                severity="warn",
                category="data",
                title_zh="目标标签面板为空或解析失败，回退至占位同源收益率",
                detail_zh=(
                    f"批次 {latest_batch_id} 下 {target_code} 面板为空/无可交易行/日期解析失败："
                    f"{type(panel_exc).__name__}: {panel_exc}。"
                    "IC 不具备业务含义，请重新冻结一批有效目标标签。"
                ),
                evidence={"target_code": target_code,
                          "calc_batch_id": latest_batch_id},
                fix_link={"tab": "factor-laboratory", "subtab": "targets",
                          "label_zh": "目标标签管理"},
                retryable=True,
            ))
            forward_returns = _fallback_shift_pct()

    if forward_returns is None:
        forward_returns = _fallback_shift_pct()

    common_idx = factor_values.index.intersection(forward_returns.index)
    common_cols = factor_values.columns.intersection(forward_returns.columns)
    forward_returns = forward_returns.loc[common_idx, common_cols]

    return (
        forward_returns,
        blockers,
        {
            "target_code": target_code,
            "target_horizon": target_horizon,
            "latest_batch_id": latest_batch_id,
            "fallback_used": fallback_used,
        },
    )


def select_evaluation_date_range(
    warehouse,
    cutoff_date: date,
    *,
    min_validation_days: int = 50,
    purge_days: int = 0,
    embargo_days: int = 5,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
    user_start_date: date | None = None,
    user_end_date: date | None = None,
) -> tuple[date, date, list[date], list[dict]]:
    """选择评价用的日期区间（真实交易日）。

    - 用户指定 start_date/end_date 时 → 裁剪到真实交易日集合
    - 用户未指定时 → 按门禁要求倒推 275 个交易日（保证验证期≥55日）

    Returns:
        (start_date, end_date, all_trade_dates_asc, blockers)
    """
    blockers: list[dict] = []

    all_trade_dates_desc = warehouse.list_trade_dates()
    all_trade_dates_asc = sorted(all_trade_dates_desc)
    # 剔除今日及之后（仅保留已完成历史交易日）
    all_trade_dates_asc = [d for d in all_trade_dates_asc if d < cutoff_date]

    if not all_trade_dates_asc:
        blockers.append(_blocker(
            "eval.data.historical_data_insufficient",
            severity="error",
            category="data",
            title_zh="仓库中没有任何历史交易日快照",
            detail_zh=(
                "无法构造评价区间。请先执行初始化补数导入一段历史行情后再评估。"
            ),
            fix_link=_BLOCKER_FIX_INIT_BACKFILL,
            retryable=True,
        ))
        return cutoff_date, cutoff_date, [], blockers

    # 用户指定日期区间
    if user_start_date is not None or user_end_date is not None:
        start_d = user_start_date if user_start_date else all_trade_dates_asc[0]
        end_d = user_end_date if user_end_date else all_trade_dates_asc[-1]
        filtered = [d for d in all_trade_dates_asc if start_d <= d <= end_d]
        if len(filtered) < 2:
            blockers.append(_blocker(
                "eval.data.historical_data_insufficient",
                severity="error",
                category="data",
                title_zh="用户指定的日期区间内可用交易日不足",
                detail_zh=(
                    f"指定区间 [{start_d}, {end_d}] 内仅 {len(filtered)} 个交易日，"
                    "至少需要 2 个以上。请放宽日期范围或先补数。"
                ),
                evidence={
                    "user_start_date": str(user_start_date),
                    "user_end_date": str(user_end_date),
                    "available_trade_days_in_range": len(filtered),
                },
                fix_link=_BLOCKER_FIX_INIT_BACKFILL,
                retryable=True,
            ))
            return start_d, end_d, filtered, blockers
        return filtered[0], filtered[-1], filtered, blockers

    # 默认：按门禁要求倒推
    total_needed = math.ceil(
        (min_validation_days + purge_days + embargo_days) / validation_ratio
    )  # = ceil(55/0.2) = 275

    n_available = len(all_trade_dates_asc)

    # end_date = 倒数第 embargo_days + 1 个交易日
    end_idx = max(0, n_available - (embargo_days + 1))
    end_date = all_trade_dates_asc[end_idx]

    # start_date = 从 end_date 倒推 total_needed 个交易日
    start_idx = max(0, end_idx - total_needed + 1)
    start_date = all_trade_dates_asc[start_idx]

    actual_total = end_idx - start_idx + 1
    if actual_total < total_needed:
        # 实际不足，给出 blocker 但允许继续（降低门槛）
        validation_days_after_split = int(actual_total * validation_ratio)
        if validation_days_after_split < min_validation_days:
            blockers.append(_blocker(
                "eval.data.historical_data_insufficient",
                severity="error",
                category="data",
                title_zh=(
                    f"历史交易日不足：仅 {n_available} 日，"
                    f"切分后验证期仅 {validation_days_after_split} 日（要求≥{min_validation_days}）"
                ),
                detail_zh=(
                    f"按门禁要求需要至少 {total_needed} 个交易日，"
                    f"实际可用 {actual_total} 日。建议：① 延长补数窗口覆盖更多历史；"
                    f"② 或暂时接受验证期不足 {min_validation_days} 日导致门禁自动失败。"
                ),
                evidence={
                    "required_total_trade_days": total_needed,
                    "available_total_trade_days": actual_total,
                    "estimated_validation_days": validation_days_after_split,
                    "min_validation_days": min_validation_days,
                    "train_ratio": train_ratio,
                    "validation_ratio": validation_ratio,
                    "purge_days": purge_days,
                    "embargo_days": embargo_days,
                },
                fix_link=_BLOCKER_FIX_INIT_BACKFILL,
                retryable=True,
            ))

    return start_date, end_date, all_trade_dates_asc, blockers


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


# ══════════════════════════════════════════════════════════════════════════════
# Part A: preflight_factor_evaluation（6 项预检服务）
# ══════════════════════════════════════════════════════════════════════════════

_PREFLIGHT_FIX_FACTOR_EDITOR = {
    "tab": "factors", "subtab": "editor", "label_zh": "去因子编辑器修复",
}
_PREFLIGHT_FIX_PIT = {
    "tab": "factors", "subtab": "pit-join", "label_zh": "去 PIT Join 设置页处理",
}
_PREFLIGHT_FIX_TARGETS = {
    "tab": "factor-laboratory", "subtab": "targets", "label_zh": "去目标标签中心生成",
}
_PREFLIGHT_FIX_BACKFILL = {
    "tab": "init-backfill", "label_zh": "去执行初始化补数",
}


def preflight_factor_evaluation(
    *,
    factor_code: str,
    factor_version_id: str | int | None = None,
    universe: str = "all_a_shares",
    start_date: date | None = None,
    end_date: date | None = None,
    target_horizon: int = 5,
) -> dict:
    """执行因子评价前预检（6 项检查，按固定顺序输出）。

    检查项顺序：formula_compile → data_dependencies → market_coverage
              → target_availability → pit_risk → sample_size

    返回：{"overall": {...}, "items": [...]}
    """
    from app.db.session import SessionLocal
    from app.services.factors.factor_registry import get_factor_by_code

    db = SessionLocal()
    items: list[dict] = []
    try:
        factor = get_factor_by_code(db, factor_code)
        version = None
        if factor is not None:
            from app.services.factors.factor_registry import get_latest_version
            version = get_latest_version(db, factor.id)
            if factor_version_id is not None:
                try:
                    fid_int = int(factor_version_id)
                    from sqlalchemy import select
                    from app.models.factor_model import FactorVersion
                    specific = db.scalar(
                        select(FactorVersion).where(FactorVersion.id == fid_int)
                    )
                    if specific is not None:
                        version = specific
                except (ValueError, TypeError):
                    pass
    finally:
        db.close()

    warehouse = FactorWarehouse()

    # ─────────────────────────────────────────────────────────
    # ① formula_compile
    # ─────────────────────────────────────────────────────────
    formula = version.formula_expr if version is not None else (
        factor.formula_expr if (factor is not None and factor.formula_expr) else "close"
    )
    postprocess = None
    params: dict = {}
    if version is not None:
        try:
            postprocess = json.loads(version.postprocess_json) if version.postprocess_json else None
        except (TypeError, json.JSONDecodeError):
            postprocess = None
        try:
            params = json.loads(version.params_json) if version.params_json else {}
        except (TypeError, json.JSONDecodeError):
            params = {}
    direction_from_db = (
        (version.direction if version else None)
        or (factor.direction if factor else None)
        or "higher_better"
    )

    compiler = FactorCompiler()
    compile_result = compiler.compile(
        formula=formula,
        params=params,
        postprocess=postprocess,
        direction=direction_from_db,
        strict_fields=False,
    )
    if compile_result.is_valid:
        items.append({
            "code": "preflight.formula_compile.ok",
            "severity": "pass",
            "category": "formula",
            "title_zh": "公式编译通过",
            "detail_zh": "公式语法、字段引用、后处理配置均已通过静态编译检查。",
            "evidence": {
                "formula": formula,
                "compiler_version": compile_result.execution_plan.compiler_version if compile_result.execution_plan else None,
                "content_hash": (
                    compile_result.execution_plan.content_hash()
                    if compile_result.execution_plan and hasattr(compile_result.execution_plan, "content_hash")
                    else None
                ),
            },
            "retryable": True,
        })
    else:
        err_list = [e.to_dict() for e in compile_result.errors]
        first_msg = err_list[0].get("message") if err_list else "unknown error"
        items.append({
            "code": "preflight.formula_compile.error",
            "severity": "error",
            "category": "formula",
            "title_zh": "公式编译失败",
            "detail_zh": (
                f"共 {len(err_list)} 处错误，首条：{first_msg}。"
                "请在因子编辑器中使用“校验”按钮按提示修复后重新保存版本。"
            ),
            "evidence": {
                "formula": formula,
                "compile_errors": err_list[:10],
                "error_count": len(err_list),
            },
            "fix_link": _PREFLIGHT_FIX_FACTOR_EDITOR,
            "retryable": True,
        })

    # ─────────────────────────────────────────────────────────
    # ② data_dependencies
    # ─────────────────────────────────────────────────────────
    plan = compile_result.execution_plan if compile_result.is_valid else None
    deps = plan.data_dependencies if plan is not None else {}
    needed_fields: list[str] = sorted({str(f) for f in (deps.get("fields") or [])})
    source_tables: list[str] = list(deps.get("source_tables") or [])
    if not source_tables:
        source_tables = ["raw_daily_bars"]

    available: dict[str, list[str]] = {}
    missing: list[str] = []
    for tbl in source_tables:
        cols = warehouse.describe_table(tbl)
        available[tbl] = cols
    available_all: set[str] = set()
    for cols in available.values():
        available_all.update(cols)
    missing = sorted({f for f in needed_fields if f and f not in available_all})

    if not missing:
        items.append({
            "code": "preflight.data_dependencies.ok",
            "severity": "pass",
            "category": "data",
            "title_zh": "公式依赖字段齐全",
            "detail_zh": (
                f"公式共引用 {len(needed_fields)} 个字段，涉及 {len(source_tables)} 张源表，均已在仓库中就绪。"
            ),
            "evidence": {
                "required_fields": needed_fields,
                "source_tables": source_tables,
                "available": available,
                "missing": [],
            },
            "retryable": True,
        })
    else:
        items.append({
            "code": "preflight.data_dependencies.missing",
            "severity": "error",
            "category": "data",
            "title_zh": f"公式依赖字段缺失：{len(missing)} 项",
            "detail_zh": (
                "以下字段在源表中尚未找到："
                f"{', '.join(missing[:8])}{' 等' if len(missing) > 8 else ''}。"
                "请先在初始化补数或外部数据中同步这些字段。"
            ),
            "evidence": {
                "missing": missing,
                "required_fields": needed_fields,
                "source_tables": source_tables,
                "available": {k: list(v)[:50] for k, v in available.items()},
            },
            "fix_link": _PREFLIGHT_FIX_BACKFILL,
            "retryable": True,
        })

    # ─────────────────────────────────────────────────────────
    # ③ market_coverage
    # ─────────────────────────────────────────────────────────
    all_trade_dates_desc = warehouse.list_trade_dates()
    today = date.today()
    ctd = all_trade_dates_desc[0] if all_trade_dates_desc else None
    days_since_ctd = (today - ctd).days if ctd is not None else 9999
    if ctd is None:
        items.append({
            "code": "preflight.market_coverage.none",
            "severity": "error",
            "category": "data",
            "title_zh": "仓库中没有任何交易日快照",
            "detail_zh": "DuckDB 仓库中未找到任何历史交易日。请先执行初始化补数导入一段行情。",
            "evidence": {
                "snapshot_count": 0,
                "latest_trade_date": None,
                "days_since_latest": None,
            },
            "fix_link": _PREFLIGHT_FIX_BACKFILL,
            "retryable": True,
        })
    elif days_since_ctd <= 3:
        items.append({
            "code": "preflight.market_coverage.fresh",
            "severity": "pass",
            "category": "data",
            "title_zh": f"行情数据新鲜，最近交易日 {ctd.isoformat()}",
            "detail_zh": f"距今日 {days_since_ctd} 天，在 3 天窗口内，可直接用于评价。",
            "evidence": {
                "snapshot_count": len(all_trade_dates_desc),
                "latest_trade_date": ctd.isoformat(),
                "days_since_latest": days_since_ctd,
            },
            "retryable": True,
        })
    elif days_since_ctd <= 7:
        items.append({
            "code": "preflight.market_coverage.stale_warn",
            "severity": "warn",
            "category": "data",
            "title_zh": f"行情数据略陈旧，最近交易日 {ctd.isoformat()}",
            "detail_zh": (
                f"距今日 {days_since_ctd} 天，超过 3 天新鲜窗口。"
                "评价仍可进行，但建议先同步最近几个交易日。"
            ),
            "evidence": {
                "snapshot_count": len(all_trade_dates_desc),
                "latest_trade_date": ctd.isoformat(),
                "days_since_latest": days_since_ctd,
            },
            "fix_link": _PREFLIGHT_FIX_BACKFILL,
            "retryable": True,
        })
    elif days_since_ctd <= 30:
        items.append({
            "code": "preflight.market_coverage.stale_error",
            "severity": "error",
            "category": "data",
            "title_zh": f"行情数据陈旧，最近交易日 {ctd.isoformat()}",
            "detail_zh": (
                f"距今日 {days_since_ctd} 天，超过 7 天陈旧阈值。"
                "请先同步最新行情数据后再评价。"
            ),
            "evidence": {
                "snapshot_count": len(all_trade_dates_desc),
                "latest_trade_date": ctd.isoformat(),
                "days_since_latest": days_since_ctd,
            },
            "fix_link": _PREFLIGHT_FIX_BACKFILL,
            "retryable": True,
        })
    else:
        items.append({
            "code": "preflight.market_coverage.ancient",
            "severity": "error",
            "category": "data",
            "title_zh": f"行情数据严重过时（>30 天），最近交易日 {ctd.isoformat()}",
            "detail_zh": (
                f"距今日 {days_since_ctd} 天，超过 30 天上限。"
                "评价结果没有业务参考价值，请先同步最新数据。"
            ),
            "evidence": {
                "snapshot_count": len(all_trade_dates_desc),
                "latest_trade_date": ctd.isoformat(),
                "days_since_latest": days_since_ctd,
            },
            "fix_link": _PREFLIGHT_FIX_BACKFILL,
            "retryable": True,
        })

    # ─────────────────────────────────────────────────────────
    # ④ target_availability
    # ─────────────────────────────────────────────────────────
    target_code = f"target_{target_horizon}d_return"
    latest_batch_id = warehouse.get_latest_target_batch_id(target_code)
    if latest_batch_id is not None:
        items.append({
            "code": "preflight.target_availability.ok",
            "severity": "pass",
            "category": "target",
            "title_zh": f"目标标签可用：{target_code}",
            "detail_zh": (
                f"仓库中已冻结 {target_code} 的可交易批次 {latest_batch_id}，"
                "评价将基于真实标签计算 IC。"
            ),
            "evidence": {
                "target_code": target_code,
                "target_horizon": target_horizon,
                "latest_batch_id": latest_batch_id,
                "fallback_needed": False,
            },
            "retryable": True,
        })
    elif target_horizon == 5:
        items.append({
            "code": "preflight.target_availability.fallback_5d",
            "severity": "warn",
            "category": "target",
            "title_zh": f"目标标签 {target_code} 未冻结，将回退至同源占位收益率",
            "detail_zh": (
                "horizon=5 支持回退（shift 同源 pct_change），但 IC 不再具有严格业务含义。"
                "强烈建议先在目标标签中心生成并冻结一批真实标签。"
            ),
            "evidence": {
                "target_code": target_code,
                "target_horizon": target_horizon,
                "latest_batch_id": None,
                "fallback_needed": True,
                "fallback_type": "homologous_shift_5d",
            },
            "fix_link": _PREFLIGHT_FIX_TARGETS,
            "retryable": True,
        })
    else:
        items.append({
            "code": "eval.data.target_horizon_unavailable",
            "severity": "error",
            "category": "target",
            "title_zh": f"目标标签不可用：{target_code}",
            "detail_zh": (
                f"target_horizon={target_horizon} 没有已冻结的真实标签批次，"
                "且不支持同源占位 fallback（仅 horizon=5 支持 fallback）。"
                "请先到目标标签中心生成并冻结对应 horizon 的标签批次。"
            ),
            "evidence": {
                "target_code": target_code,
                "target_horizon": target_horizon,
                "latest_batch_id": None,
                "fallback_needed": True,
                "fallback_supported": False,
            },
            "fix_link": _PREFLIGHT_FIX_TARGETS,
            "retryable": True,
        })

    # ─────────────────────────────────────────────────────────
    # ⑤ pit_risk
    # ─────────────────────────────────────────────────────────
    pit_fields: list[str] = sorted({
        str(f) for f in (deps.get("pit_fields") or [])
    }) if plan is not None else []
    if pit_fields:
        items.append({
            "code": "preflight.pit_risk.warn",
            "severity": "warn",
            "category": "pit",
            "title_zh": f"公式引用 {len(pit_fields)} 个 PIT 字段，请注意 Point-in-Time 对齐",
            "detail_zh": (
                "财报/公告等 Point-in-Time 字段若未经正确的公告日期对齐，"
                "可能引入未来函数偏差，导致评价虚高。请确认 PIT Join 策略。"
            ),
            "evidence": {"pit_fields": pit_fields},
            "fix_link": _PREFLIGHT_FIX_PIT,
            "retryable": True,
        })
    else:
        items.append({
            "code": "preflight.pit_risk.none",
            "severity": "pass",
            "category": "pit",
            "title_zh": "无 PIT 风险字段",
            "detail_zh": "公式未引用财报公告类 PIT 字段，无需额外 Point-in-Time 对齐处理。",
            "evidence": {"pit_fields": []},
            "retryable": True,
        })

    # ─────────────────────────────────────────────────────────
    # ⑥ sample_size
    # ─────────────────────────────────────────────────────────
    cutoff_for_range = ctd or today
    sel_start, sel_end, sel_tds, _range_blockers = select_evaluation_date_range(
        warehouse,
        cutoff_for_range,
        min_validation_days=50,
        purge_days=0,
        embargo_days=5,
        train_ratio=0.6,
        validation_ratio=0.2,
        user_start_date=start_date,
        user_end_date=end_date,
    )
    n_days = len(sel_tds)
    median_stock_universe = 5550
    coverage_rate = 0.85
    expected_cells = int(n_days * median_stock_universe * coverage_rate)
    recommended_date_range: list[str] | None = None
    if sel_start and sel_end and n_days >= 2:
        recommended_date_range = [sel_start.isoformat(), sel_end.isoformat()]

    if expected_cells >= 100_000:
        severity = "pass"
        title_zh = f"样本规模充足：预计约 {expected_cells/10000:.1f} 万单元格"
        detail_zh = (
            f"{n_days} 交易日 × 约 {median_stock_universe} 中位数股票 × {coverage_rate:.0%} 覆盖率"
            f"= {expected_cells} 个预期单元格，≥10 万，样本量充足。"
        )
    elif expected_cells >= 50_000:
        severity = "warn"
        title_zh = f"样本规模临界：预计约 {expected_cells/10000:.1f} 万单元格"
        detail_zh = (
            f"{n_days} 交易日 × 约 {median_stock_universe} 中位数股票 × {coverage_rate:.0%} 覆盖率"
            f"= {expected_cells} 个预期单元格，在 5~10 万之间。"
            "建议补长历史窗口或放宽日期范围。"
        )
    else:
        severity = "error"
        title_zh = f"样本规模不足：预计约 {expected_cells/10000:.1f} 万单元格"
        detail_zh = (
            f"{n_days} 交易日 × 约 {median_stock_universe} 中位数股票 × {coverage_rate:.0%} 覆盖率"
            f"= {expected_cells} 个预期单元格，<5 万。"
            "IC/分组结果统计显著性不足，请先执行更长窗口的历史补数。"
        )

    items.append({
        "code": f"preflight.sample_size.{severity}",
        "severity": severity,
        "category": "sample",
        "title_zh": title_zh,
        "detail_zh": detail_zh,
        "evidence": {
            "trade_days_in_range": n_days,
            "median_stock_universe": median_stock_universe,
            "coverage_rate": coverage_rate,
            "expected_cells": expected_cells,
            "threshold_ok": 100_000,
            "threshold_warn": 50_000,
            "selected_date_range": recommended_date_range,
            "user_start_date": start_date.isoformat() if start_date else None,
            "user_end_date": end_date.isoformat() if end_date else None,
        },
        "fix_link": _PREFLIGHT_FIX_BACKFILL if severity != "pass" else None,
        "retryable": True,
    })

    # ─────────────────────────────────────────────────────────
    # Execute the same compiled plan on a bounded recent window. Static field
    # checks cannot detect joins or rolling formulas that produce only NaN.
    if plan is not None and not missing and sel_tds:
        sample_dates = sorted(sel_tds)[-40:]
        sample_start = sample_dates[0]
        sample_end = sample_dates[-1]
        value_column = resolve_evaluation_column(postprocess)
        try:
            sample_outcome = FactorExecutor(warehouse).execute_panel(
                plan,
                start_date=sample_start,
                end_date=sample_end,
            )
            sample_frame = sample_outcome.factors_long
            if value_column in sample_frame.columns:
                sampled_values = pd.to_numeric(
                    sample_frame[value_column], errors="coerce"
                ).replace([math.inf, -math.inf], math.nan)
            else:
                sampled_values = pd.Series(dtype=float)

            valid_mask = sampled_values.notna()
            valid_rows = int(valid_mask.sum())
            total_rows = int(len(sample_frame))
            valid_rate = valid_rows / total_rows if total_rows else 0.0
            valid_days = int(
                sample_frame.loc[valid_mask, "trade_date"].nunique()
            ) if valid_rows and "trade_date" in sample_frame.columns else 0
            valid_symbols = int(
                sample_frame.loc[valid_mask, "symbol"].nunique()
            ) if valid_rows and "symbol" in sample_frame.columns else 0
            sample_evidence = {
                "sample_start_date": sample_start.isoformat(),
                "sample_end_date": sample_end.isoformat(),
                "evaluation_value_column": value_column,
                "sample_rows": total_rows,
                "valid_factor_rows": valid_rows,
                "valid_rate": round(valid_rate, 6),
                "valid_trade_days": valid_days,
                "valid_symbols": valid_symbols,
                "read_errors": list(sample_outcome.read_errors or [])[:10],
            }

            if valid_rows == 0:
                items.append({
                    "code": "preflight.factor_values.all_nan",
                    "severity": "error",
                    "category": "formula",
                    "title_zh": "公式取样结果全部无效",
                    "detail_zh": (
                        "依赖字段虽然存在，但按正式执行口径取样后没有任何有效因子值。"
                        "请检查跨表连接、字段非空率、滚动窗口和除零处理后再提交评价。"
                    ),
                    "evidence": sample_evidence,
                    "fix_link": _PREFLIGHT_FIX_FACTOR_EDITOR,
                    "retryable": True,
                })
            elif valid_rate < 0.05 or valid_days < 5:
                items.append({
                    "code": "preflight.factor_values.insufficient",
                    "severity": "error",
                    "category": "sample",
                    "title_zh": "公式有效样本不足",
                    "detail_zh": (
                        "真实取样可以出数，但有效率低于 5% 或有效交易日少于 5 天，"
                        "不足以支撑正式 IC 与分组评价。"
                    ),
                    "evidence": sample_evidence,
                    "fix_link": _PREFLIGHT_FIX_BACKFILL,
                    "retryable": True,
                })
            else:
                items.append({
                    "code": "preflight.factor_values.ok",
                    "severity": "pass" if valid_rate >= 0.2 else "warn",
                    "category": "sample",
                    "title_zh": "公式真实取样可用",
                    "detail_zh": (
                        f"最近取样窗口得到 {valid_rows} 条有效因子值，"
                        f"覆盖 {valid_days} 个交易日、{valid_symbols} 个标的。"
                    ),
                    "evidence": sample_evidence,
                    "fix_link": (
                        _PREFLIGHT_FIX_BACKFILL if valid_rate < 0.2 else None
                    ),
                    "retryable": True,
                })
        except Exception as exc:  # noqa: BLE001
            items.append({
                "code": "preflight.factor_values.execution_failed",
                "severity": "error",
                "category": "formula",
                "title_zh": "公式真实取样执行失败",
                "detail_zh": (
                    f"真实取样执行出现 {type(exc).__name__}: {exc}。"
                    "正式评价会使用同一执行计划，因此当前配置不可提交。"
                ),
                "evidence": {
                    "sample_start_date": sample_start.isoformat(),
                    "sample_end_date": sample_end.isoformat(),
                    "exception_type": type(exc).__name__,
                },
                "fix_link": _PREFLIGHT_FIX_FACTOR_EDITOR,
                "retryable": True,
            })

    # overall
    # ─────────────────────────────────────────────────────────
    blocking_count = sum(1 for it in items if it["severity"] == "error")
    overall_passed = blocking_count == 0
    overall = {
        "passed": overall_passed,
        "blocking_count": blocking_count,
        "recommended_date_range": recommended_date_range,
    }
    return {"overall": overall, "items": items}


def _inject_fingerprint(task_dump: dict) -> dict:
    """给 task model_dump() 结果注入计算后的 fingerprint（若 payload_json 存在）。"""
    if not task_dump:
        return task_dump
    if task_dump.get("fingerprint"):
        return task_dump
    payload_json = task_dump.get("payload_json")
    if not payload_json:
        return task_dump
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
    except (TypeError, json.JSONDecodeError):
        return task_dump
    if isinstance(payload, dict):
        task_dump["fingerprint"] = compute_payload_fingerprint(payload)
    # 兼容前端：把 task_id 别名也暴露（如果还没）
    if "task_id" not in task_dump and "id" in task_dump:
        task_dump["task_id"] = task_dump["id"]
    return task_dump


def create_evaluation_task(
    *,
    factor_code: str,
    factor_kind: str = "continuous",
    factor_version_id: int | None = None,
    universe: str = "all_a_shares",
    start_date: date | None = None,
    end_date: date | None = None,
    target_horizon: int = 5,
    n_groups: int = 5,
    cost_rate: float = 0.001,
    direction: str | None = None,
    created_by: str = "local_user",
) -> dict:
    """创建评估异步任务（完整配置哈希幂等）。

    幂等逻辑：
    1. payload 规范化（key 排序、None 移除、日期字符串化）→ SHA256 fingerprint
    2. 若已存在同 fingerprint 且 status ∈ {queued, running, done, warn} → 返回旧任务
    3. EvaluationRun 本身的幂等由 create_evaluation_run 保证（config_hash + cutoff）
    """
    payload = {
        "factor_code": factor_code,
        "factor_kind": factor_kind,
        "factor_version_id": factor_version_id,
        "universe": universe,
        "start_date": start_date,
        "end_date": end_date,
        "target_horizon": target_horizon,
        "n_groups": n_groups,
        "cost_rate": cost_rate,
        "direction": direction,
        "created_by": created_by,
    }

    fingerprint = compute_payload_fingerprint(payload)

    # 单飞检查：同 fingerprint 有 queued/running/done/warn 任务则复用
    existing = list_async_tasks(task_type=TASK_TYPE, limit=50)
    for t in existing:
        if t.status not in {"queued", "running", "pending", "processing", "done", "warn"}:
            continue
        try:
            p = json.loads(t.payload_json) if t.payload_json else {}
        except (TypeError, json.JSONDecodeError):
            continue
        existing_fp = compute_payload_fingerprint(p)
        if existing_fp == fingerprint:
            d = t.model_dump()
            d["fingerprint"] = existing_fp
            if "task_id" not in d and "id" in d:
                d["task_id"] = d["id"]
            return d

    task = create_async_task(TASK_TYPE, payload)
    _start_worker(task.id, _run_evaluation_worker)
    d = task.model_dump()
    d["fingerprint"] = fingerprint
    if "task_id" not in d and "id" in d:
        d["task_id"] = d["id"]
    return d


def _find_active_run_id(db: Session, tid: str) -> str | None:
    """通过 task_id 反查最近创建的 EvaluationRun.id（用于异常回滚门禁）。"""
    from sqlalchemy import select
    try:
        stmt = (
            select(EvaluationRun.id)
            .where(EvaluationRun.task_id == tid)
            .order_by(EvaluationRun.created_at.desc())
            .limit(1)
        )
        return db.scalar(stmt)
    except Exception:
        return None


def _rollback_gate_if_passed(db: Session, run_id: str | None) -> None:
    """异常后安全回滚：如果 run 已终态为 passed/rejected，强制降级为 warn。"""
    if not run_id:
        return
    try:
        run = db.get(EvaluationRun, run_id)
        if run is None:
            return
        if run.gate_result in TERMINAL_GATE_RESULTS:
            # 强制直接 UPDATE（不通过 finalize 的终态保护，因为失败任务不能显示 passed）
            run.gate_result = "warn"
            run.fallback_reason = "task_failed_gate_rollback"
            db.flush()
    except Exception:
        logger.debug("rollback gate_result failed for run %s", run_id, exc_info=True)


def _run_evaluation_worker(task_id: str) -> None:
    """评估任务 worker（修正状态机顺序：先压力测试 → 终态写入）。"""
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    SessionLocal = get_session_local()
    db = SessionLocal()
    active_run_id: str | None = None
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return
        if task.status == "cancelled":
            return

        # 解析 payload（Task 6 新增字段全部支持，并提供默认值向后兼容）
        payload = json.loads(task.payload_json) if task.payload_json else {}
        factor_code = payload.get("factor_code", "")
        factor_kind = payload.get("factor_kind", "continuous")
        factor_version_id_from_payload = payload.get("factor_version_id")
        universe_from_payload = payload.get("universe", "all_a_shares")
        user_start_date_raw = payload.get("start_date")
        user_end_date_raw = payload.get("end_date")
        if isinstance(user_start_date_raw, str) and user_start_date_raw:
            try:
                user_start_date: date | None = date.fromisoformat(user_start_date_raw[:10])
            except ValueError:
                user_start_date = None
        else:
            user_start_date = user_start_date_raw if isinstance(user_start_date_raw, date) else None
        if isinstance(user_end_date_raw, str) and user_end_date_raw:
            try:
                user_end_date: date | None = date.fromisoformat(user_end_date_raw[:10])
            except ValueError:
                user_end_date = None
        else:
            user_end_date = user_end_date_raw if isinstance(user_end_date_raw, date) else None
        target_horizon = int(payload.get("target_horizon", 5))
        n_groups = int(payload.get("n_groups", 5))
        cost_rate = float(payload.get("cost_rate", 0.001))
        direction_from_payload = payload.get("direction")
        created_by = payload.get("created_by", "local_user")

        if not factor_code:
            blockers = [
                _blocker("eval.missing_factor_code", category="config",
                         title_zh="未选择要评估的因子",
                         detail_zh="请在评估实验室页顶部的“因子代码”下拉框中选择一个因子后，再提交评估。",
                         fix_link=None),
            ]
            _set_task(db, task_id, status="failed", stage="failed",
                      message="missing factor_code", finished_at=_now(),
                      errors_json=json.dumps(blockers, ensure_ascii=False))
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
            blockers = [
                _blocker("eval.factor_not_found", category="version",
                         title_zh=f"因子代码不存在：{factor_code}",
                         detail_zh="因子库中未找到该代码对应的因子草稿或已发布因子。请先在因子编辑器中创建并保存该因子。",
                         fix_link=_BLOCKER_FIX_FACTOR_EDITOR),
            ]
            _set_task(db, task_id, status="failed", stage="failed",
                      message=f"factor_not_found:{factor_code}", finished_at=_now(),
                      errors_json=json.dumps(blockers, ensure_ascii=False))
            return

        # 选择版本：优先 payload 指定的 factor_version_id，否则使用最新版本
        version = None
        if factor_version_id_from_payload is not None:
            try:
                fid_int = int(factor_version_id_from_payload)
                from sqlalchemy import select
                from app.models.factor_model import FactorVersion as FV
                version = db.scalar(
                    select(FV).where(
                        (FV.id == fid_int) & (FV.factor_id == factor.id)
                    )
                )
            except (ValueError, TypeError):
                version = None
        if version is None:
            version = get_latest_version(db, factor.id)
        if version is None:
            blockers = [
                _blocker("eval.no_factor_version", category="version",
                         title_zh=f"因子「{factor_code}」暂无可评估的版本",
                         detail_zh="因子存在，但尚未保存任何版本（公式 + 后处理 + 方向）。请先在因子编辑器中填写公式、完成校验并“保存”一个版本。",
                         fix_link=_BLOCKER_FIX_FACTOR_EDITOR),
            ]
            _set_task(db, task_id, status="failed", stage="failed",
                      message="no_factor_version", finished_at=_now(),
                      errors_json=json.dumps(blockers, ensure_ascii=False))
            return

        _set_task(db, task_id, stage="loading", percent=10,
                  message=f"已加载版本 v{version.version}（id={version.id}）")

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
            error_items = [error.to_dict() for error in compile_result.errors]
            blockers = []
            for i, err in enumerate(error_items[:10], 1):
                detail = err.get("detail") if isinstance(err.get("detail"), dict) else {}
                line = detail.get("line")
                offset = detail.get("offset")
                location = ""
                if line is not None and offset is not None:
                    location = f"（第 {line} 行第 {offset} 列）"
                blockers.append(
                    _blocker(
                        f"eval.formula.compile_{i}",
                        category="formula",
                        title_zh=f"公式编译问题{i}：{err.get('message') or '语法错误'}",
                        detail_zh=(
                            f"{location}错误类型：{err.get('type') or '-'}。"
                            "请打开因子编辑器，粘贴相同公式并点击“校验”按提示修复。"
                        ),
                        fix_link=_BLOCKER_FIX_FACTOR_EDITOR,
                    )
                )
            user_message = (
                f"公式编译失败（共 {len(error_items)} 处错误），请根据下方问题清单到因子编辑器修复后重新保存版本。"
            )
            _set_task(db, task_id, status="failed", stage="failed",
                      message=user_message,
                      result_json=json.dumps({
                          "factor_code": factor_code,
                          "factor_version_id": version.id,
                          "formula_expr": version.formula_expr,
                          "compile_errors": error_items,
                      }, ensure_ascii=False),
                      errors_json=json.dumps(blockers, ensure_ascii=False),
                      finished_at=_now())
            return

        # 3-4. 批量计算因子面板（只读快照 + 面板执行）
        if _cancelled(task_id):
            return
        _set_task(db, task_id, stage="reading", percent=35,
                  message="选择评价区间并计算因子面板")
        warehouse = FactorWarehouse()
        executor = FactorExecutor(warehouse)
        read_errors: list[dict[str, Any]] = []
        blockers: list[dict] = []

        # 步骤 A：选择评价日期区间（支持 payload 指定的起止日期）
        plan = compile_result.execution_plan
        start_date, end_date, all_tds_asc, date_blockers = select_evaluation_date_range(
            warehouse,
            cutoff_date,
            min_validation_days=50,
            purge_days=0,
            embargo_days=5,
            train_ratio=0.6,
            validation_ratio=0.2,
            user_start_date=user_start_date,
            user_end_date=user_end_date,
        )
        blockers.extend(date_blockers)

        if _cancelled(task_id):
            return

        # 步骤 B：调用 execute_panel 批量计算因子面板
        _set_task(db, task_id, stage="reading", percent=40,
                  message=f"批量计算因子面板（{start_date} → {end_date}）")
        panel_crashed = False
        try:
            panel_outcome = executor.execute_panel(
                plan,
                start_date=start_date,
                end_date=end_date,
            )
            read_errors.extend(panel_outcome.read_errors)
        except Exception as exc:  # noqa: BLE001
            logger.debug("execute_panel failed", exc_info=True)
            read_errors.append({
                "source_table": None,
                "required_fields": sorted(
                    plan.data_dependencies.get("fields", [])
                ) if plan else [],
                "category": "read",
                "correlation_id": uuid.uuid4().hex[:8],
                "message": f"{type(exc).__name__}: {exc}",
            })
            panel_outcome = None
            panel_crashed = True

        # 从 factors_long 构造三个 pivot
        factor_values_raw: pd.DataFrame | None = None
        factor_values_winsorized: pd.DataFrame | None = None
        factor_values_normalized: pd.DataFrame | None = None
        factor_values: pd.DataFrame | None = None
        factors_long = panel_outcome.factors_long if panel_outcome is not None else None

        # Part B-2: 按 postprocess 配置决定使用哪一列因子值
        evaluation_value_column = resolve_evaluation_column(postprocess)

        if factors_long is not None and not factors_long.empty:
            try:
                factor_values_raw = factors_long.pivot_table(
                    index="trade_date",
                    columns="symbol",
                    values="raw_value",
                    aggfunc="first",
                )
                factor_values_winsorized = factors_long.pivot_table(
                    index="trade_date",
                    columns="symbol",
                    values="winsorized_value",
                    aggfunc="first",
                )
                factor_values_normalized = factors_long.pivot_table(
                    index="trade_date",
                    columns="symbol",
                    values="normalized_value",
                    aggfunc="first",
                )
                # Task 6: 根据 FactorVersion.postprocess 选择口径，并记录到 run config
                if evaluation_value_column == "raw_value":
                    factor_values = factor_values_raw
                elif evaluation_value_column == "winsorized_value":
                    factor_values = factor_values_winsorized
                else:
                    factor_values = factor_values_normalized
            except Exception as exc:  # noqa: BLE001
                logger.debug("pivot factors_long failed", exc_info=True)
                read_errors.append({
                    "category": "schema",
                    "correlation_id": uuid.uuid4().hex[:8],
                    "message": f"pivot_failed: {type(exc).__name__}: {exc}",
                })

        if factor_values is None or factor_values.empty:
            # ================================================================
            # 人性化诊断：拆分成多个维度的 blockers，每个给出跳转修复链接
            # ================================================================
            needed_fields: set[str] = set()
            source_tables: list[str] = []
            pit_fields: list[str] = []
            if plan is not None:
                deps = plan.data_dependencies or {}
                needed_fields.update(str(x) for x in deps.get("fields", []))
                source_tables = list(deps.get("source_tables", []))
                pit_fields = list(deps.get("pit_fields", []))

            # ① 基础行情（close/open/high/low/volume）有没有 → 说明做过初始化补数没
            base_market_fields = {"close", "open", "high", "low", "volume", "amount", "pct_chg"}

            available_cols: set[str] = set()
            for tbl in source_tables or ["raw_daily_bars"]:
                cols = warehouse.describe_table(tbl)
                available_cols.update(cols)
            has_base_market = bool(base_market_fields & available_cols)

            # ② 交易日/快照覆盖：看 warehouse 有没有可用日期
            snapshot_count: int | None = None
            try:
                snap_dates = warehouse.list_trade_dates()
                snapshot_count = len(snap_dates)
            except Exception:  # noqa: BLE001
                snapshot_count = None

            if not has_base_market and (snapshot_count is None or snapshot_count == 0):
                blockers.append(
                    _blocker("eval.data.no_init_backfill", category="data",
                             title_zh="从未执行过初始化补数（数据库里没有任何股票快照）",
                             detail_zh=(
                                 "系统既找不到 close/open 等基础行情字段，也没有任何交易日快照。"
                                 "评估至少需要先导入一段历史行情，才能算出因子值。"
                             ),
                             fix_link=_BLOCKER_FIX_INIT_BACKFILL)
                )
            elif snapshot_count is not None and snapshot_count < 20:
                blockers.append(
                    _blocker("eval.data.trade_days_insufficient", category="data",
                             title_zh="已同步的交易日太少",
                             detail_zh=(
                                 f"仓库中仅找到 {snapshot_count} 个交易日快照，"
                                 f"评估需要至少 20 日，建议 ≥ 60 日。"
                                 "请补数更长时间段后再评估。"
                             ),
                             fix_link=_BLOCKER_FIX_INIT_BACKFILL)
                )

            # ③ 公式依赖字段：哪些不存在
            missing_required: list[str] = sorted(
                f for f in needed_fields if f and f not in available_cols
            )
            builtin_market = base_market_fields | {"symbol", "trade_date"}
            external_missing = [f for f in missing_required if f not in builtin_market]
            base_missing = [f for f in missing_required if f in base_market_fields]

            for field_name in missing_required:
                src_table = None
                for tbl in source_tables:
                    tbl_cols = warehouse.describe_table(tbl)
                    if field_name not in tbl_cols:
                        src_table = tbl
                        break
                read_errors.append({
                    "source_table": src_table,
                    "field_name": field_name,
                    "required_fields": [field_name],
                    "category": "schema",
                    "correlation_id": uuid.uuid4().hex[:8],
                    "message": f"missing field: {field_name}",
                })

            if base_missing:
                blockers.append(
                    _blocker("eval.data.base_market_missing", category="data",
                             title_zh=f"基础行情字段缺失：{', '.join(base_missing)}",
                             detail_zh=(
                                 "这些是评估/公式计算必需的基础行情字段。"
                                 "请到初始化补数重新执行，并确保包含日K行情（OHLCV）。"
                             ),
                             fix_link=_BLOCKER_FIX_INIT_BACKFILL)
                )
            if external_missing:
                blockers.append(
                    _blocker("eval.data.external_missing", category="data",
                             title_zh=f"公式依赖的外部字段尚未同步：{', '.join(external_missing)}",
                             detail_zh=(
                                 "这些字段不是系统自带的基础行情，通常是财务/研报/另类数据等。"
                                 "请到外部数据源页面配置对应字段并完成同步后再评估。"
                             ),
                             fix_link=_BLOCKER_FIX_EXTERNAL_DATA)
                )

            # ④ 方向/后处理配置导致的全 NaN：提醒去编辑器
            if not any(b.get("severity") == "error" for b in blockers):
                blockers.append(
                    _blocker("eval.data.values_all_nan", category="formula",
                             title_zh="公式能运行，但算出的因子值全部为 NaN（无有效数值）",
                             detail_zh=(
                                 "常见原因：①公式引用的字段虽存在但当日全是空值；"
                                 "②除法时分子分母同时为 0 / NaN；"
                                 "③方向或缺失值处理（dropna/填 0）过于激进。"
                                 "可先在因子编辑器 → 预览 中逐个交易日查看。"
                             ),
                             fix_link=_BLOCKER_FIX_FACTOR_EDITOR)
                )

            # 通用兜底：加一条"看覆盖诊断"
            blockers.append(
                _blocker("eval.data.check_coverage", severity="warning", category="data",
                         title_zh="建议：查看数据覆盖诊断，逐个字段核对覆盖率",
                         detail_zh="数据覆盖诊断页会按字段×交易日画出热力图，能一眼看出是哪一天/哪个字段没补上。",
                         fix_link=_BLOCKER_FIX_DATA_COVERAGE)
            )

            if read_errors:
                for err in read_errors:
                    err_code = f"eval.data.read_err_{err['correlation_id']}"
                    blockers.append(
                        _blocker(err_code,
                                 severity="warning",
                                 category=err["category"],
                                 title_zh=f"读取/SQL异常: {err['category']}",
                                 detail_zh=(
                                     f"correlation_id={err['correlation_id']}, "
                                     f"message={err['message']}"
                                 ),
                                 fix_link=_BLOCKER_FIX_DATA_COVERAGE)
                    )

            # status: 如果面板计算本身 crashed（RuntimeError 等）→ failed；
            # 否则仅有诊断 read_errors → warn（提示缺字段等软修复场景）；纯无数据 → failed
            _final_status = "failed" if panel_crashed or not read_errors else "warn"
            _set_task(db, task_id, status=_final_status, stage="failed",
                      message="no_factor_data:无法读取因子值",
                      errors_json=json.dumps(blockers, ensure_ascii=False),
                      result_json=json.dumps({
                          "factor_code": factor_code,
                          "formula_expr": version.formula_expr,
                          "required_fields": sorted(needed_fields),
                          "source_tables": source_tables,
                          "pit_fields": pit_fields,
                          "available_fields_sample": sorted(list(available_cols))[:50],
                          "snapshot_count": snapshot_count,
                          "read_errors": read_errors,
                          "date_range_selection": {
                              "start_date": str(start_date),
                              "end_date": str(end_date),
                          },
                          "diagnosis": {
                              "has_base_market": has_base_market,
                              "missing_required_fields": missing_required,
                              "external_missing": external_missing,
                          },
                      }, ensure_ascii=False),
                      finished_at=_now())
            return

        _set_task(db, task_id, stage="reading", percent=55,
                  message=f"因子值矩阵 {factor_values.shape}")

        # 写入 run_context：三个口径的因子面板 + 评价口径 + 方向 meta
        direction_effective = (
            direction_from_payload
            or (version.direction if version else None)
            or (factor.direction if factor else None)
            or "higher_better"
        )
        run_context_extra: dict[str, Any] = {
            "date_range": {
                "start_date": str(start_date),
                "end_date": str(end_date),
                "selected_trade_days": len(factor_values.index),
                "universe": universe_from_payload,
                "user_start_date": str(user_start_date) if user_start_date else None,
                "user_end_date": str(user_end_date) if user_end_date else None,
            },
            "factor_panels_meta": {
                "raw_shape": list(factor_values_raw.shape) if factor_values_raw is not None else None,
                "winsorized_shape": list(factor_values_winsorized.shape) if factor_values_winsorized is not None else None,
                "normalized_shape": list(factor_values_normalized.shape) if factor_values_normalized is not None else None,
                "evaluation_value_column": evaluation_value_column,
            },
            "direction": {
                "original_direction": direction_effective,
                "payload_direction": direction_from_payload,
                "version_direction": version.direction if version else None,
                "factor_direction": factor.direction if factor else None,
            },
        }

        # 5. 构造目标收益：优先读取 warehouse 中持久化的真实标签
        if _cancelled(task_id):
            return
        _set_task(db, task_id, stage="targets", percent=60,
                  message="对齐目标收益")

        forward_returns, blockers_for_step5, run_ctx = resolve_forward_returns(
            warehouse,
            factor_values,
            target_horizon,
        )
        target_code = run_ctx["target_code"]
        latest_batch_id = run_ctx["latest_batch_id"]
        fallback_used = run_ctx["fallback_used"]

        has_error_blocker = any(
            b.get("severity") == "error" for b in blockers_for_step5
        ) or any(
            b.get("severity") == "error" for b in blockers
        )
        if has_error_blocker:
            errors_payload = json.dumps(blockers + blockers_for_step5, ensure_ascii=False)
            _set_task(db, task_id, status="failed", stage="failed",
                      message=f"target_horizon_unavailable:{target_code}",
                      errors_json=errors_payload,
                      result_json=json.dumps({
                          "run_context": run_ctx,
                      }, ensure_ascii=False),
                      finished_at=_now())
            return

        common_idx = factor_values.index.intersection(forward_returns.index)
        factor_values = factor_values.loc[common_idx]
        forward_returns = forward_returns.loc[common_idx]

        # Part C-1: 步骤 5 和 6 之间调用：方向统一（对齐后、IC 计算前）
        factor_values, dir_blockers = apply_direction_alignment(
            factor_values,
            direction_effective,
        )
        run_context_extra["direction"]["applied_negation"] = (
            direction_effective == "lower_better"
        )
        blockers_for_step5.extend(dir_blockers)

        # 6. 执行评估（run_evaluation 会内部写入 passed/rejected 终态；压力测试不通过再覆盖）
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
        active_run_id = outcome.run_id

        # Task 6: 把 evaluation_value_column 追加写入 EvaluationRun.config
        run = db.get(EvaluationRun, outcome.run_id)
        final_gate_result: str = outcome.gate_result
        final_rejection_reasons: list[str] = list(outcome.rejection_reasons) if outcome.rejection_reasons else []
        if run is not None:
            try:
                run_cfg = json.loads(run.config_json) if run.config_json else {}
                if not isinstance(run_cfg, dict):
                    run_cfg = {}
                run_cfg["evaluation_value_column"] = evaluation_value_column
                run_cfg["direction_meta"] = dict(run_context_extra["direction"])
                run_cfg["universe"] = universe_from_payload
                run.config_json = json.dumps(run_cfg, ensure_ascii=False, default=str)
                db.flush()
            except Exception:
                logger.debug("failed to write evaluation_value_column for run %s", outcome.run_id, exc_info=True)

        if blockers_for_step5 or blockers:
            try:
                existing = db.get(AsyncTaskRecord, task_id)
                if existing is not None:
                    from app.services.async_tasks import normalize_errors
                    prev = normalize_errors(existing.errors_json) if existing.errors_json else []
                    for _b in blockers:
                        prev.extend(normalize_errors(_b))
                    for _b in blockers_for_step5:
                        prev.extend(normalize_errors(_b))
                    existing.errors_json = json.dumps(prev[-100:], ensure_ascii=False, default=str)
                    if existing.status in {"done", None} and (
                        any(b.get("severity") == "warn" for b in prev)
                    ):
                        existing.status = "warn"
                    db.flush()
            except Exception:
                logger.debug("failed to persist step5 blockers for task %s", task_id, exc_info=True)

        _set_task(db, task_id, stage="evaluating", percent=85,
                  message=f"阶段1门禁: {outcome.gate_result}（待压力测试）")

        # 7. 压力测试（Part C-2 状态机修正：压力测试在终态写入前完成）
        if _cancelled(task_id):
            return
        _set_task(db, task_id, stage="stress", percent=90,
                  message="执行参数扰动和时间段稳定性")

        # 提取验证期数据用于压力测试
        stress_summary = None
        try:
            if outcome.time_split is not None:
                val_mask = (factor_values.index >= outcome.time_split.validation_start) & (
                    factor_values.index <= outcome.time_split.validation_end
                )
                val_features = factor_values.loc[val_mask]
                val_targets = forward_returns.loc[forward_returns.index.isin(val_features.index)]
                if not val_features.empty and not val_targets.empty:
                    stress_summary = run_stress_test(
                        features=val_features,
                        targets=val_targets,
                    )
        except Exception as stress_exc:  # noqa: BLE001
            logger.warning("stress test raised exception (non-fatal, degrade to warn)", exc_info=True)
            dir_blockers.append(_blocker(
                "eval.stress.exception",
                severity="warn",
                category="config",
                title_zh="压力测试执行异常，门禁降级",
                detail_zh=(
                    f"参数扰动/时间分段压力测试抛出 {type(stress_exc).__name__}: {stress_exc}。"
                    "门禁不会置为 passed，需要修复压力测试后重试。"
                ),
                evidence={"exception_type": type(stress_exc).__name__},
                retryable=True,
            ))
            stress_summary = None

        # 构建压力测试 metrics（即使跳过也要保留结构）
        if stress_summary is not None:
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
            stress_overall = stress_summary.overall_verdict
            stress_failures = list(stress_summary.failure_reasons or [])
        else:
            stress_metrics = {"overall_verdict": "skipped", "skipped_reason": "insufficient_validation_data"}
            stress_overall = "skipped"
            stress_failures = ["stress_test_skipped"]

        # Part C-2: 根据压力结论最终决定门禁（覆盖 run_evaluation 写入的终态）
        run = db.get(EvaluationRun, outcome.run_id)
        if run is not None:
            # 合并 stress metrics
            existing_metrics = json.loads(run.metrics_json) if run.metrics_json else {}
            existing_metrics["stress_test"] = stress_metrics

            if stress_overall != "stable" and stress_overall != "skipped":
                # 压力不通过：无论 phase1 是 passed 还是 rejected，门禁降级为 warn
                stress_reasons_raw: list[dict] = []
                if stress_failures:
                    for r in stress_failures:
                        if isinstance(r, dict) and r.get("code"):
                            stress_reasons_raw.append(dict(r))
                        else:
                            stress_reasons_raw.append(
                                _blocker(
                                    "eval.stability.not_stable",
                                    severity="warning",
                                    category="stability",
                                    title_zh="压力测试不通过",
                                    detail_zh=f"压力失败项：{r}",
                                    evidence={"failure_reason": str(r)},
                                )
                            )
                else:
                    stress_reasons_raw.append(
                        _blocker(
                            "eval.stability.not_stable",
                            severity="warning",
                            category="stability",
                            title_zh="压力测试不通过（not_stable）",
                            detail_zh="整体压力结论非 stable，且未给出具体失败原因。",
                        )
                    )
                from app.services.async_tasks import normalize_errors
                # final_rejection_reasons 可能是 list[str] 或 list[dict] → 归一化
                phase1_structured = normalize_errors(final_rejection_reasons)
                stress_structured = normalize_errors(stress_reasons_raw)
                merged_structured = phase1_structured + stress_structured
                # 直接 UPDATE（绕过 finalize 的终态保护，因为压力测试是必过门槛）
                run.gate_result = "warn"
                run.rejection_reasons_json = json.dumps(merged_structured, ensure_ascii=False, default=str)
                final_gate_result = "warn"
                final_rejection_reasons = merged_structured  # 保持结构化，方便 result_json 写入
            elif final_rejection_reasons:
                # 压力 stable，但 phase1 有拒绝项 → 也归一化并写回，保证 DB / result_json 都带 correlation_id
                from app.services.async_tasks import normalize_errors
                structured = normalize_errors(final_rejection_reasons)
                run.rejection_reasons_json = json.dumps(structured, ensure_ascii=False, default=str)
                final_rejection_reasons = structured
            # 如果压力 stable → 保留 phase1 的 passed/rejected

            run.metrics_json = json.dumps(existing_metrics, ensure_ascii=False, default=str)
            db.flush()

        # 8. 完成（只有压力通过才用 phase1 的 passed/rejected，否则一律 warn）
        final_status = "done"
        if blockers_for_step5 and any(
            b.get("severity") == "warn" for b in blockers_for_step5
        ):
            final_status = "warn"
        if final_gate_result == "warn":
            final_status = "warn"
        _set_task(
            db, task_id,
            status=final_status,
            stage="done",
            percent=100,
            message=f"评估完成: gate={final_gate_result}, 压力={stress_overall}",
            finished_at=_now(),
            result_json=json.dumps({
                "run_id": outcome.run_id,
                "gate_result": final_gate_result,
                "rejection_reasons": final_rejection_reasons,
                "stress_verdict": stress_overall,
                "evaluation_value_column": evaluation_value_column,
                "run_context": {
                    **run_ctx,
                    "direction": run_context_extra["direction"],
                    "universe": universe_from_payload,
                },
            }, ensure_ascii=False),
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("WP5 evaluation task %s failed", task_id)
        try:
            db.rollback()
        except Exception:
            pass
        if active_run_id is None:
            active_run_id = _find_active_run_id(db, task_id)
        _rollback_gate_if_passed(db, active_run_id)
        try:
            existing = db.get(AsyncTaskRecord, task_id)
            if existing is not None and existing.status == "cancelled":
                return
        except Exception:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:
                pass
        # T3-5: Worker 未知异常也要带 correlation_id (8 hex)
        cid = uuid.uuid4().hex[:8]
        blockers = []
        try:
            blockers = [
                _blocker("eval.unexpected_error", category="config",
                         title_zh="评估执行出现未知异常",
                         detail_zh=(
                             f"[correlation_id={cid}] 异常类型：{type(exc).__name__}，信息：{exc}。"
                             "常见原因：①评估配置（分组数/手续费/回看期）不合理；②目标收益或因子数据存在极端异常值。"
                             "可先在因子编辑器 → 预览 检查公式能否正常出数，再回到本页调整参数重试。"
                         ),
                         evidence={
                             "correlation_id": cid,
                             "exception_type": type(exc).__name__,
                             "exception_msg": str(exc),
                         },
                         fix_link=_BLOCKER_FIX_FACTOR_EDITOR),
            ]
        except Exception as _nested:
            blockers = [{
                "code": "eval.unexpected_error",
                "severity": "error",
                "category": "config",
                "title_zh": "评估执行出现未知异常",
                "detail_zh": f"[cid={cid}] type={type(exc).__name__}, msg={exc}, nested_exc={_nested}",
                "evidence": {"correlation_id": cid, "exception_type": type(exc).__name__, "exception_msg": str(exc)},
                "retryable": True,
            }]
        try:
            _set_task(
                db, task_id,
                status="failed",
                stage="failed",
                message=f"evaluation_failed:{exc}",
                errors_json=json.dumps(blockers, ensure_ascii=False),
                result_json=json.dumps({"exception_type": type(exc).__name__,
                                        "exception": str(exc),
                                        "correlation_id": cid}, ensure_ascii=False),
                finished_at=_now(),
            )
        except Exception as _nested2:
            # 终极 fallback：直接 UPDATE 原始 SQL，避免 ORM 嵌套异常丢错误
            try:
                from sqlalchemy import text
                db.commit()
                db.execute(text(
                    "UPDATE async_task_records SET status='failed', stage='failed', "
                    "message=:msg, errors_json=:ej, result_json=:rj, finished_at=:fn "
                    "WHERE id=:tid"
                ), {
                    "msg": f"evaluation_failed_nested:{exc}|{_nested2}",
                    "ej": json.dumps(blockers, ensure_ascii=False),
                    "rj": json.dumps({"exception_type": type(exc).__name__, "exception": str(exc), "correlation_id": cid, "nested": str(_nested2)}, ensure_ascii=False),
                    "fn": _now(),
                    "tid": task_id,
                })
                db.commit()
            except Exception as _n3:
                pass
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
    return _inject_fingerprint(task.model_dump()) if task else None


def list_evaluation_tasks(limit: int = 20) -> list[dict]:
    """列出评估任务。"""
    tasks = list_async_tasks(task_type=TASK_TYPE, limit=limit)
    return [_inject_fingerprint(t.model_dump()) for t in tasks]


def cancel_evaluation_task(task_id: str) -> dict:
    """取消评估任务。"""
    from app.services.async_tasks import cancel_async_task
    task = cancel_async_task(task_id)
    return _inject_fingerprint(task.model_dump())


__all__ = [
    "TASK_TYPE",
    "create_evaluation_task",
    "get_evaluation_task",
    "list_evaluation_tasks",
    "cancel_evaluation_task",
    "preflight_factor_evaluation",
    "compute_payload_fingerprint",
    "resolve_evaluation_column",
    "apply_direction_alignment",
]
