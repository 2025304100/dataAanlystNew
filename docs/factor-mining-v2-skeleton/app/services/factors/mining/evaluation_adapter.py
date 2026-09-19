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
from typing import Any, Sequence

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
SPLIT_MINIMUMS: dict[str, tuple[int, int, int]] = {
    "daily": (252, 100, 50),
    "weekly": (104, 20, 10),
    "monthly": (36, 6, 3),
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


def evaluate_full(
    ctx: MiningContext, *, individual: Individual
) -> Any:
    """最终验证：全量区间评估，**test 段仅此一次**（红线 C8）。

    实现要点：
      1. `factor_registry.create_factor_draft` 先生成临时版本号（评估需要 factor_version_id）
      2. 复用 `factor_evaluator.run_evaluation(db, *, factor_version_id, factor_values,
         forward_returns, config, ...)`，`EvaluationConfig` 的切分直接用 `ctx.split`
      3. 结果落 `factor_evaluation_runs`，并回填 `test_start_date` / `test_end_date`
      4. 评估完成后 `run.status: validating → succeeded`，**禁止回炉**
    TODO(M10): 接入 factor_evaluator.run_evaluation。
    """
    raise NotImplementedError("M10: evaluate_full")


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
