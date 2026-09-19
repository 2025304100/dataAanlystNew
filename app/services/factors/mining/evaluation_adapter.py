"""因子挖掘 —— 评估接入与三段切分适配（★ v2.0 核心修正模块）。

设计文档 §6.10 / §7.2。模块编号 M10。

本模块是挖掘域与 L1 因子域之间**唯一**的接缝。它解决两件事：

1. **purge/embargo 单位归一化（P0）**
   `factor_evaluator.build_time_split` 的 `purge_days`/`embargo_days` 是对传入
   `all_dates` 序列的**索引间隔**（见 `factor_evaluator.py:268-269`）。
   挖掘传入的 `all_dates` 是「有效调仓点」而非交易日，因此：
       daily   → 1 调仓点 = 1 交易日  → 传 5  = 5 个交易日（正确）
       weekly  → 1 调仓点 = 5 交易日  → 传 5  = 25 个交易日（错误，隔离 5 周）
       monthly → 1 调仓点 = 21 交易日 → 传 5  = 105 个交易日（错误，隔离 5 个月）
   持有期为 T+1 买入 → T+5 卖出（约 5 交易日），只需丢弃
   `ceil(5 / 每期交易日数)` 个调仓点即可阻断标签跨界。

2. **显式传入样本量地板（P0）**
   `build_time_split` 默认 `min_validation_days=50`，推出
   `derived_min_total = ceil((50+5+5)/0.2) = 300`（`factor_evaluator.py:259-263`），
   使周频 243 点、月频 60 点**必然触发 hard_fail 抛 ValueError**。
   故必须按调仓频率显式传 `min_validation_days` / `min_test_days` / `min_total_days`。

⚠️ 本模块**不得修改** `factor_evaluator.py` 的任何默认值（红线 B5 / 风险 R13），
   只能通过调用侧传参适配。
"""
from __future__ import annotations

import math
from datetime import date
from dataclasses import dataclass
from app.core import db_numeric as DN
from typing import Any, Callable, Mapping, Sequence

from app.services.factors.factor_evaluator import (
    TimeSplit,
    build_time_split,
)

from app.services.factors.mining.contracts import (
    Fitness,
    Individual,
    MiningContext,
    SplitBudget,
)

# ══════════════════════════════════════════════════════════
# 常量表（设计文档 §7.2.2）
# ══════════════════════════════════════════════════════════

#: 每个调仓期约含多少交易日
REBALANCE_INTERVAL_DAYS: dict[str, int] = {
    "daily": 1,
    "weekly": 5,
    "monthly": 21,
}

#: 频率标定的样本量地板 → (min_total_points, min_val_points, min_test_points)
#: min_total 与需求 §3.2「最小样本量硬门槛 日 252 / 周 104 / 月 36」对齐；
#: min_val / min_test 是切分函数内部的段内下限，需低于实际可达值否则 hard_fail。
#: 三段最小点数（**自洽三元组**，2026-09-17 需求方裁决 D-I 落地）。
#:
#: 旧值 `(252,100,50)/(104,20,10)/(36,6,3)` 数学上**不自洽**：
#: `build_time_split` 会推出 `derived_min_total = ceil((min_val+purge+embargo)/val_ratio)`
#: = 550/110/41，均 **大于** 声明地板 → `[地板, derived)` 的「死区」内
#: `compute_split_budget().meets_floor=True` 但 `build_split` 抛裸 ValueError。
#:
#: 新值的推导（分段先定、总门槛由分段反推，保证 derived ≤ 声明地板）：
#: - **daily**  floor 252（保住需求 §3.2）：floor 处可用分段 = train 151 / val 45 /
#:   test 41（扣 purge 5 + embargo 5 + tail 5）→ min_val=40、min_test=40
#: - **weekly** floor 104（2 年）：floor 处 = train 62 / val 20 / test 15
#:   → min_val=18、min_test=10
#: - **monthly** floor **41**（原 36 在 floor 处 test 仅 2 点，无统计意义；
#:   上调 5 点≈3.5 年，使 test ≥ 3）：floor 处 = train 24 / val 7 / test 3
#:   → min_val=5、min_test=3
#:
#: 回归：`test_split_minimums_self_consistent`（floor 处逐点扫描 [floor, floor+40]
#: 全部可切分）。月频 test < 24 仍走「降级」路径（`DEGRADED_TEST_THRESHOLD`，最高 B 级）。
SPLIT_MINIMUMS: dict[str, tuple[int, int, int]] = {
    "daily": (252, 40, 40),
    "weekly": (104, 18, 10),
    "monthly": (41, 5, 3),
}

#: 月频（及任何 test 点不足 24 的频率）不做 Bootstrap / 置换，最高评 B 级
DEGRADED_TEST_THRESHOLD = 24

#: 切分算法版本——参数变更会改变历史任务的可复现区间，必须随快照落库
SPLIT_ALGORITHM_VERSION = "split-1.0.0"


class MiningSplitError(ValueError):
    """切分不达标。路由层应转 FactorSevenError('MINING_SAMPLE_INSUFFICIENT')。"""

    def __init__(self, message: str, *, budget: SplitBudget | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.budget = budget


# ══════════════════════════════════════════════════════════
# 纯计算（确定性，可脱离 DB 单测）
# ══════════════════════════════════════════════════════════


def normalize_purge_points(*, target_horizon_days: int, frequency: str) -> int:
    """把「交易日」为单位的隔离期换算为「调仓点」计数，最少 1 期。

    >>> normalize_purge_points(target_horizon_days=5, frequency="daily")
    5
    >>> normalize_purge_points(target_horizon_days=5, frequency="weekly")
    1
    >>> normalize_purge_points(target_horizon_days=5, frequency="monthly")
    1
    """
    if frequency not in REBALANCE_INTERVAL_DAYS:
        raise ValueError(f"unsupported rebalance frequency: {frequency!r}")
    interval = REBALANCE_INTERVAL_DAYS[frequency]
    return max(1, math.ceil(int(target_horizon_days) / interval))


def normalize_purge_trading_days(*, purge_points: int, frequency: str) -> int:
    """把归一化后的调仓点数折回交易日数（仅供 UI 展示，设计文档 §7.2.4）。"""
    interval = REBALANCE_INTERVAL_DAYS.get(frequency, 1)
    return int(purge_points) * interval


def compute_split_budget(
    *,
    all_dates: Sequence[date],
    frequency: str,
    purge_points: int,
    embargo_points: int,
    tail_loss: int,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
) -> SplitBudget:
    """复算 `build_time_split` 的段内点数，产出可展示、可门禁的预算。

    本函数是**纯函数**，不调用 `build_time_split`，以便在段内点数不达标、
    `build_time_split` 会抛异常的场合仍能给出人类可读的诊断。
    计数口径与 `factor_evaluator.py:266-297` 保持一致。
    """
    n = len(all_dates)
    usable_n = max(0, n - int(tail_loss))

    train_end_idx = max(0, min(n - 1, int(n * train_ratio))) if n else 0
    val_end_idx = (
        max(train_end_idx, min(n - 1, int(n * (train_ratio + validation_ratio))))
        if n else 0
    )
    val_start_idx = min(n - 1, train_end_idx + int(purge_points)) if n else 0
    test_start_idx = min(n - 1, val_end_idx + int(embargo_points)) if n else 0

    train_points = max(0, train_end_idx)
    val_n_raw = max(0, val_end_idx - val_start_idx) if n else 0
    test_n_raw = (max(0, (n - 1) - test_start_idx + 1) if test_start_idx < n else 0) if n else 0

    val_points = max(0, min(val_n_raw, usable_n - val_start_idx)) if n else 0
    test_points = (
        max(0, min(test_n_raw, usable_n - test_start_idx))
        if (n and test_start_idx < n) else 0
    )

    floor_total, _floor_val, _floor_test = SPLIT_MINIMUMS.get(frequency, (0, 0, 0))
    meets_floor = n >= floor_total and val_points > 0 and test_points > 0

    return SplitBudget(
        frequency=frequency,  # type: ignore[arg-type]
        total_points=n,
        train_points=train_points,
        val_points=val_points,
        test_points=test_points,
        purge_points=int(purge_points),
        embargo_points=int(embargo_points),
        purge_trading_days=normalize_purge_trading_days(
            purge_points=purge_points, frequency=frequency
        ),
        embargo_trading_days=normalize_purge_trading_days(
            purge_points=embargo_points, frequency=frequency
        ),
        tail_loss=int(tail_loss),
        frequency_floor=floor_total,
        meets_floor=meets_floor,
        statistically_degraded=test_points < DEGRADED_TEST_THRESHOLD,
    )


def build_split(
    *,
    all_dates: Sequence[date],
    frequency: str,
    target_horizon: int,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
) -> tuple[TimeSplit, SplitBudget]:
    """归一化参数后调用 `build_time_split`，返回 (TimeSplit, SplitBudget)。

    这是 M10 的主入口。所有挖掘侧调用**必须**走这里，
    禁止直接调 `build_time_split`（红线 B9 / B10）。
    """
    purge_points = normalize_purge_points(
        target_horizon_days=target_horizon, frequency=frequency
    )
    embargo_points = purge_points
    min_total, min_val, min_test = SPLIT_MINIMUMS.get(frequency, (0, 0, 0))

    budget = compute_split_budget(
        all_dates=all_dates,
        frequency=frequency,
        purge_points=purge_points,
        embargo_points=embargo_points,
        tail_loss=target_horizon,
        train_ratio=train_ratio,
        validation_ratio=validation_ratio,
    )

    if not budget.meets_floor:
        raise MiningSplitError(
            "有效调仓点不足："
            f"{frequency} 频率共 {budget.total_points} 点（地板 {min_total}），"
            f"切分后 train={budget.train_points} / val={budget.val_points} / "
            f"test={budget.test_points}。"
            "建议扩大时间范围、调整三段比例或降低调仓频率；"
            "若为月频想评 S/A 级，请先镜像 10 年数据。",
            budget=budget,
        )

    split = build_time_split(
        all_dates=all_dates,
        target_horizon=target_horizon,
        train_ratio=train_ratio,
        validation_ratio=validation_ratio,
        purge_days=purge_points,       # ← 归一化后的调仓点数（C11）
        embargo_days=embargo_points,   # ← 归一化后的调仓点数
        min_validation_days=min_val,   # ← 显式传入（C12）
        min_test_days=min_test,        # ← 显式传入（C12）
        min_total_days=min_total,      # ← 显式传入（C12）
    )
    return split, budget


def assert_no_leakage(split: TimeSplit, used_dates: Sequence[date]) -> None:
    """断言适应度计算只用了 train 段（红线 C8）。

    在 `run_evolution` 每代调用；任何越界立即抛异常，宁可中断也不出污染结论。
    """
    lo, hi = split.train_start, split.train_end
    offenders = [d for d in used_dates if not (lo <= d <= hi)]
    if offenders:
        raise AssertionError(
            "fitness leakage: train window is "
            f"[{lo} .. {hi}] but {len(offenders)} dates outside were used, "
            f"first few: {offenders[:5]}"
        )


# ══════════════════════════════════════════════════════════
# 需要指标计算的实现（签名冻结，实现指向设计文档章节）
# ══════════════════════════════════════════════════════════


def build_context_split(
    ctx: MiningContext, *, all_dates: Sequence[date]
) -> tuple[TimeSplit, SplitBudget]:
    """由 MiningContext 还原切分（快照重放时用，参数全部取自快照，不重算）。"""
    return build_split(
        all_dates=all_dates,
        frequency=ctx.rebalance_frequency,
        target_horizon=ctx.target_horizon,
        train_ratio=ctx.train_ratio,
        validation_ratio=ctx.validation_ratio,
    )


def evaluate_short(
    ctx: MiningContext, *, individual: Individual, sample_length: str
) -> Fitness:
    """短样本适应度评估（**只取 train 段**）。

    实现要点（设计文档 §6.10 / §7.2）：
      1. `FactorExecutor.execute_panel()` 算出因子值面板（走 G2 缓存）
      2. 取 `ctx.split.train_start .. train_end` 的样本，`sample_length` 再截尾
      3. 复用 `factor_evaluator.compute_rank_ic / compute_coverage / compute_turnover`
      4. 内部必须 `assert_no_leakage(split, used_dates)`
    TODO(M10): 接入 factor_executor + factor_evaluator。
    """
    raise NotImplementedError("M10: evaluate_short")


@dataclass
class TestEvalOutcome:
    """单个候选的最终验证（test 段）结果。"""

    factor_version_id: int | None
    evaluation_run_id: str | None
    metrics: dict[str, Any]
    test_start: Any | None
    test_end: Any | None
    gate_result: str
    success: bool
    error: str | None = None


def create_candidate_draft(db: Any, *, digest: str, formula: str,
                           category: str | None, direction: str,
                           logic: str, created_by: str,
                           frequency: str | None = None) -> tuple[int, str]:
    """默认草稿链（红线 C6）：`create_factor_draft` → `create_factor_version`。

    code 冲突视为「同哈希已建过」（幂等复用既有 factor/version）。
    """
    from app.schemas.factor_library import FactorDraftCreate, FactorVersionCreate
    from app.services.factors import factor_registry as FR

    # code 必须 match ^[a-z][a-z0-9_]*$：哈希可能来自任意来源（含连字符/大写），
    # 先清洗再截取 —— 否则草稿链在 Pydantic 校验处才炸（运行期）
    import re as _re

    safe = _re.sub(r"[^a-z0-9_]", "_", str(digest).lower())[:12].lstrip("_") or "cand"
    base_code = f"mining_{safe}"
    direction_map = {"positive": "higher_better", "negative": "lower_better"}
    mapped = direction_map.get(direction, "higher_better")

    for attempt in range(3):
        code = base_code if attempt == 0 else f"{base_code}_{attempt}"
        try:
            factor = FR.create_factor_draft(db, draft=FactorDraftCreate(
                code=code,
                name=f"挖掘候选 {digest[:12]}",
                category=category or "trend",
                direction=mapped,
                description=(logic or "")[:500] or None,
                thesis=(logic or "")[:500] or None,
                created_by=created_by,
                frequency=frequency,
            ))
            break
        except ValueError as exc:
            if "factor_code_conflict" in str(exc):
                existing = FR.get_factor_by_code(db, code)
                if existing is not None:
                    factor = existing
                    break
            raise
    else:
        raise ValueError(f"因子 code 冲突且重试耗尽：{base_code}")

    version = FR.create_factor_version(db, factor_id=factor.id, request=FactorVersionCreate(
        formula_expr=formula,
        direction=mapped,
        change_note=f"mining:{digest[:12]}",
        created_by=created_by,
    ))
    return int(version.id), code


def _default_run_evaluator(db: Any, *, factor_version_id: int,
                           factor_values: Any, forward_returns: Any,
                           config: Any, created_by: str) -> Any:
    from app.services.factors.factor_evaluator import run_evaluation

    return run_evaluation(db, factor_version_id=factor_version_id,
                          factor_values=factor_values,
                          forward_returns=forward_returns,
                          config=config, created_by=created_by)


def evaluate_full(
    ctx: MiningContext, *, individual: Any, db: Any,
    factor_values: Any, forward_returns: Any,
    draft_creator: Callable[..., Any] | None = None,
    run_evaluator: Callable[..., Any] | None = None,
    created_by: str = "mining",
) -> TestEvalOutcome:
    """最终验证：全量区间评估，**test 段仅此一次**（红线 C8）。

    链路（红线 C6）：`create_factor_draft` → `create_factor_version`
    → `factor_evaluator.run_evaluation`（`EvaluationConfig` 的切分直接用
    `ctx` 的调仓点/隔离期）→ 结果落 `factor_evaluation_runs` 并回填
    `test_start` / `test_end`。

    ⚠️ 口径（任务卡坑）：OOS 样本数 = `SplitBudget.test_points`（**已扣 tail_loss**），
    **不是** `test_end - test_start` 的日期跨度（后者含 `target_horizon` 个尾部点）。

    两个钩子均可注入（测试离线跑；生产用默认实现直连 L1）。
    """
    from app.schemas.errors import FactorSevenError
    from app.services.factors.factor_evaluator import EvaluationConfig

    # ① test 段锁定守卫：已带 factor_version_id 的个体**不许再评**（防过拟合）
    version = None
    if isinstance(individual, Mapping):
        version = individual.get("factor_version_id")
    else:
        version = getattr(individual, "factor_version_id", None)
    if version:
        raise FactorSevenError(
            "MINING_TEST_LOCKED",
            detail_zh=f"候选已有 factor_version_id={version}，test 段已评估过。",
        )

    if isinstance(individual, Mapping):
        formula = str(individual.get("canonical_formula")
                      or individual.get("formula") or "")
        digest = str(individual.get("formula_hash") or "")[:32]
        category = individual.get("category")
        direction = str(individual.get("expected_direction") or "positive")
        logic = str(individual.get("economic_logic") or "")
    else:
        formula = str(getattr(individual, "canonical_formula", "")
                      or getattr(individual, "formula_expr", "") or "")
        digest = str(getattr(individual, "formula_hash", ""))[:32]
        category = getattr(individual, "category", None)
        direction = "positive"
        logic = ""
    if not formula:
        raise ValueError("候选缺少公式，无法做最终验证。")

    creator = draft_creator or create_candidate_draft
    runner = run_evaluator or _default_run_evaluator

    factor_version_id, code = creator(
        db, digest=digest, formula=formula,
        category=str(category) if category else None,
        direction=direction, logic=logic, created_by=created_by,
        frequency=str(ctx.rebalance_frequency),
    )

    config = EvaluationConfig(
        target_horizon=int(ctx.target_horizon),
        purge_days=int(ctx.purge_points),       # 已归一化为「调仓点数」（§7.2）
        embargo_days=int(ctx.embargo_points),
        train_ratio=float(ctx.train_ratio),
        validation_ratio=float(ctx.validation_ratio),
    )
    outcome = runner(db, factor_version_id=int(factor_version_id),
                     factor_values=factor_values,
                     forward_returns=forward_returns, config=config,
                     created_by=created_by)

    split = getattr(outcome, "time_split", None) or ctx.split
    metrics = dict(getattr(outcome, "metrics", {}) or {})
    # 🚨 指标里的 NaN/Inf → None（P0：MySQL 数值列拒收特殊浮点）
    metrics = {k: (DN.to_db_float(v) if isinstance(v, (int, float)) else v)
               for k, v in metrics.items()}

    return TestEvalOutcome(
        factor_version_id=int(factor_version_id),
        evaluation_run_id=str(getattr(outcome, "run_id", "") or ""),
        metrics=metrics,
        test_start=getattr(split, "test_start", None),
        test_end=getattr(split, "test_end", None),
        gate_result=str(getattr(outcome, "gate_result", "unknown")),
        success=bool(getattr(outcome, "run_id", None)),
        error=None,
    )


__all__ = [
    "REBALANCE_INTERVAL_DAYS",
    "SPLIT_MINIMUMS",
    "DEGRADED_TEST_THRESHOLD",
    "SPLIT_ALGORITHM_VERSION",
    "MiningSplitError",
    "normalize_purge_points",
    "normalize_purge_trading_days",
    "compute_split_budget",
    "build_split",
    "assert_no_leakage",
    "build_context_split",
    "evaluate_short",
    "evaluate_full",
]
