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
import re
from collections import OrderedDict
from datetime import date, datetime
from dataclasses import dataclass
from app.core import db_numeric as DN
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

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

#: 目标标签面板缓存（性能优化？同 run 内每次 evaluate 都重新 pivot target 是
#: 无谓浪费，50 个体×每次 pivot 397×1407 面板）。按 (batch_id, target_code)
#: 只构建一次；带简单 LRU 上限防长期累积（OrderedDict 移动访问键即保活）。
_TARGET_PIVOT_CACHE: "OrderedDict[tuple[str, str], pd.DataFrame]" = OrderedDict()
_TARGET_PIVOT_CACHE_MAX = 50

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
    边界统一归一为 `date`（`build_time_split` 产物可能存在 date/Timestamp 混型）。
    """
    lo = _as_date(split.train_start)
    hi = _as_date(split.train_end)
    offenders = [d for d in (_as_date(x) for x in used_dates)
                 if d is not None and not (lo <= d <= hi)]
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
    """短样本适应度评估（**只取 train 段**，任务卡 A1）。

    真实链路（设计文档 §6.10 / §7.2）：
      1. `FactorExecutor.execute_panel()` 算出因子值面板（走 G2 子表达式缓存）
      2. 取 `ctx.split.train_start .. train_end` 的样本，`sample_length` 再截尾
      3. 复用 `factor_evaluator.compute_rank_ic / compute_coverage / compute_turnover`
      4. 内部必须 `assert_no_leakage(split, used_dates)`

    返回 `Fitness`；同时把本代探针与 G2 缓存交给 `evaluate_short_probed`
    （探针/抽样校验列由 GA 主循环在 A2 落库）。
    """
    fitness, _probe, _cache = evaluate_short_probed(
        ctx, individual=individual, sample_length=sample_length, probe=None,
    )
    return fitness


def evaluate_short_probed(
    ctx: MiningContext, *, individual: Individual, sample_length: str,
    probe: Any | None = None, cache: Any | None = None,
    verify_sample_size: int | None = None,
) -> tuple[Fitness, Any, Any]:
    """`evaluate_short` 的探针化版本：额外返回 (Fitness, GenerationProbe, SubexpressionCache)。

    数据不足（无目标标签 / train 段无有效日期 / 因子面板全缺失）时**不抛异常**，
    返回全 0 `Fitness` —— GA 主循环对「评估失败的个体」已有沉底淘汰语义（L216-219），
    全 0 评分即最低分，不中断整代（与 `run_ga_loop` 的异常隔离互补）。

    探针/缓存供 `performance_probe.write_generation_probe` 与 A2 批量编排消费。
    """
    fitness, probe, cache, _features = _evaluate_core(
        ctx, individual=individual, sample_length=sample_length,
        probe=probe, cache=cache, verify_sample_size=verify_sample_size,
    )
    return fitness, probe, cache


def evaluate_short_signed(
    ctx: MiningContext, *, individual: Individual, sample_length: str,
    probe: Any | None = None, cache: Any | None = None,
    verify_sample_size: int | None = None,
) -> tuple[Fitness, Any, Any, Any]:
    """`evaluate_short_probed` 的**签名版**（P1-6）：额外返回第 4 层去重签名。

    返回 `(Fitness, GenerationProbe, SubexpressionCache, FactorValueSignature | None)`。
    第 4 项是 `runtime_correlation.factor_value_signature` 对因子值面板降采样的结果；
    面板不可用（数据不足 / 全缺失）时为 None（去重层视为「无法判定 → 不合并」）。
    """
    from app.services.factors.mining import runtime_correlation as RC

    fitness, probe, cache, features = _evaluate_core(
        ctx, individual=individual, sample_length=sample_length,
        probe=probe, cache=cache, verify_sample_size=verify_sample_size,
    )
    signature = (
        RC.factor_value_signature(features)
        if (features is not None and not features.empty) else None
    )
    return fitness, probe, cache, signature


def _evaluate_core(
    ctx: MiningContext, *, individual: Individual, sample_length: str,
    probe: Any | None = None, cache: Any | None = None,
    verify_sample_size: int | None = None,
) -> tuple[Fitness, Any, Any, Any]:
    """短样本评估核心：返回 `(Fitness, probe, cache, features)`。

    `features` 为 date × symbol 的原始因子值面板（`_pivot_dates_as_index` 归一后），
    供第 4 层相关性去重构造签名；零适应度路径返回 None 或空面板。

    `verify_sample_size`：None=沿用本函数默认（单因子必验，sample_size=1）；
    显式传 0=本调跳过采样比对（性能路径由调用方按比例抽样，见 task_runner）。
    """
    from app.services.factors.mining import performance_probe as PROBE
    from app.services.factors.mining import subexpr_cache as SC

    probe = probe if probe is not None else PROBE.new_probe()

    wh = _resolve_warehouse(ctx)
    cache = cache or SC.SubexpressionCache(
        scope=SC.SCOPE_PRESCREEN,
        data_snapshot_version=getattr(ctx, "data_snapshot_version", None) or "snapshot-default",
    )

    with probe.stage("data_load"):
        # 性能：同 run 的目标面板只 pivot 一次（任务内 50+ 个体会反复读同一批）
        cache_key = (str(ctx.target_calc_batch_id or ""), "target_5d_return")
        target_pivot = _TARGET_PIVOT_CACHE.get(cache_key)
        if target_pivot is not None:
            _TARGET_PIVOT_CACHE.move_to_end(cache_key)
        else:
            target_df, _bid, _tcode = wh.get_target_panel(
                ctx.target_calc_batch_id or "", target_code="target_5d_return"
            )
            if target_df is None or target_df.empty:
                target_pivot = None
            else:
                target_pivot = target_df.pivot(
                    index="signal_date", columns="symbol", values="target_value"
                )
                target_pivot.index = [_as_date(d) for d in target_pivot.index]
                _TARGET_PIVOT_CACHE[cache_key] = target_pivot
                _TARGET_PIVOT_CACHE.move_to_end(cache_key)
                while len(_TARGET_PIVOT_CACHE) > _TARGET_PIVOT_CACHE_MAX:
                    _TARGET_PIVOT_CACHE.popitem(last=False)
    if target_pivot is None or target_pivot.empty:
        return _zero_fitness(individual), probe, cache, None

    split = ctx.split
    lo = _as_date(getattr(split, "train_start", None))
    hi = _as_date(getattr(split, "train_end", None))
    all_dates = sorted(d for d in target_pivot.index if d is not None)
    train_dates_all = [d for d in all_dates if (lo is None or lo <= d) and (hi is None or d <= hi)]
    sample_days = _resolve_sample_days(sample_length)
    train_dates = (
        train_dates_all if sample_days is None else train_dates_all[-sample_days:]
    )
    if not train_dates:
        return _zero_fitness(individual), probe, cache, None

    start_date, end_date = train_dates[0], train_dates[-1]

    # ── G2：提取 → 预计算 → 组装（唯一子表达式只算一次；root 即整树面板）──
    formula = individual.canonical_formula or individual.formula_expr
    with probe.stage("ast_eval"):
        batch = SC.extract_batch([{"canonical_formula": formula}])
        per_factor = batch["per_factor"]
        key = next(iter(per_factor))
        cache.extract_total = batch["total"]
        cache.extract_unique = batch["unique"]
        unique_texts = batch["unique_texts"]

    def _compute(text: str) -> pd.DataFrame:
        return _subexpr_matrix(
            wh, text, start_date=start_date, end_date=end_date, probe=probe
        )

    with probe.stage("subexpr_compute"):
        SC.precompute(cache, unique_texts, compute_fn=_compute)

    with probe.stage("factor_assemble"):
        try:
            panel = SC.assemble(
                cache, per_factor[key], expected_scope=SC.SCOPE_PRESCREEN
            )
        except (SC.CacheMissError, SC.ScopeMismatchError, SC.UnsupportedAstError):
            # 缓存是优化不是正确性依赖：缺失时回退直接计算（绝不产出错误值）
            panel = _subexpr_matrix(
                wh, formula, start_date=start_date, end_date=end_date, probe=probe
            )

    if panel is None or panel.empty:
        return _zero_fitness(individual), probe, cache, None

    features = _pivot_dates_as_index(panel)

    with probe.stage("metric_calc"):
        try:
            from app.services.factors.factor_evaluator import (
                align_factor_with_target,
                compute_coverage,
                compute_rank_ic,
                compute_turnover,
            )

            aligned = align_factor_with_target(
                features, target_pivot, factor_kind="continuous"
            )
        except Exception:
            return _zero_fitness(individual), probe, cache, None
        if not aligned.trade_dates:
            return _zero_fitness(individual), probe, cache, None

        # ★ C8 红线：适应度只用 train 段 —— 越界立即中断（宁可失败不给污染结论）
        assert_no_leakage(split, [_as_date(d) for d in aligned.trade_dates])

        ic = compute_rank_ic(aligned.features, aligned.targets)
        cov = compute_coverage(features)          # 覆盖率按原始因子面板口径
        to = compute_turnover(features)

    # ── G2 抽样校验（正确性兜底：缓存组装 vs 直接计算，差异 >1e-6 报警+废弃）──
    # 性能：命中缓存的重复评估无需反复直算比对——只有本次确有新子式被计算
    # （computed>0）时才校验；`verify_sample_size=0` 由调用方显式跳过（其按比例抽样）。
    #
    # ⚠️ 2026-09-23 修复（`cache_validation_passed` 20 代仅 3 代的根因）：
    # 子表达式若**存在空缺**（未就绪 / 被质量门禁拒写，如含 Inf、NaN 比例超限），
    # 说明**该因子自身的值质量不合格**，缓存正确地拒绝了它 —— 这**不是**「缓存算错」。
    # 校验的本意是发现哈希碰撞 / 语义不等价；把「因子质量差」记成校验失败，
    # 会让两条互相独立的指标互相污染。故此类情况**跳过校验、不写校验列**
    # （`absorb_validation(None)` 保持既有值不动），而不是写 0。
    _subs = list(per_factor.get(key) or [])
    _missing_subs = cache.missing(_subs) if _subs else []
    vres: dict[str, Any] | None = None
    if verify_sample_size != 0 and _subs and not _missing_subs:
        vres = SC.verify_sample(
            [{"id": key, "formula": formula}],
            cache=cache, per_factor=per_factor,
            direct_compute=lambda _k: _subexpr_matrix(
                wh, formula, start_date=start_date, end_date=end_date, probe=probe
            ),
            sample_size=min(1, len(per_factor)),
        )
    probe.absorb_cache(cache)
    if vres is not None:
        probe.absorb_validation(vres)

    fitness = Fitness(
        icir=DN.to_db_float(ic.icir),
        coverage=DN.to_db_float(cov.coverage),
        turnover=DN.to_db_float(to.avg_turnover),
        ic_mean=DN.to_db_float(ic.rank_ic_mean),
        complexity=int(individual.complexity or 0),
        valid_cross_sections=int(len(aligned.trade_dates)),
        sample_count=int(aligned.features.notna().sum().sum()),
    )
    return fitness, probe, cache, features


# ══════════════════════════════════════════════════════════
# evaluate_short 私有辅助（A1）
# ══════════════════════════════════════════════════════════


def _zero_fitness(individual: Individual) -> Fitness:
    return Fitness(0.0, 0.0, 0.0, 0.0, int(individual.complexity or 0), 0, 0)


def _resolve_warehouse(ctx: MiningContext):
    """取因子仓库：`ctx.warehouse_path` 优先（测试注入），缺省回退全局配置。"""
    from app.core.config import Settings
    from app.services.factors.store import FactorWarehouse

    path = getattr(ctx, "warehouse_path", None) or Settings().factor_warehouse_path
    return FactorWarehouse(str(path))


def _as_date(value: Any):
    """datetime/date/Timestamp/str/None → date（None → None）。

    ⚠️ `pd.Timestamp` 是 `datetime`（也即 `date`）的子类：
    必须先判 Timestamp / datetime，再判 date，否则 Timestamp 会在
    `isinstance(value, date)` 分支被原样返回，后续 date 比较直接 TypeError。
    """
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except Exception:
            return None
    return None


def _resolve_sample_days(sample_length: str | int | None) -> int | None:
    """把「6m / 1y / 2y / 整数值 / None」解析为交易日数（None=全 train 段）。"""
    if sample_length is None:
        return None
    if isinstance(sample_length, (int, float)):
        return max(1, int(sample_length))
    text = str(sample_length).strip().lower()
    m = re.fullmatch(r"(\d+)\s*(mo|m|y|d)?", text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "y":
        return n * 252
    if unit in ("mo", "m"):
        return n * 21
    return max(1, n)


def _subexpr_matrix(
    wh: Any, text: str, *, start_date: date, end_date: date, probe: Any = None,
) -> pd.DataFrame:
    """子表达式/完整公式 → **纯数值因子面板矩阵**（index=trade_date(date), columns=symbol）。

    缓存值必须是纯数值（float64 可转），否则 `verify_sample` 的逐元素比较会把
    `trade_date/symbol` 等 object 列强制转 float 而失败。
    编译失败或面板读取失败 → 抛 `SC.UnsupportedAstError`（precompute 捕获并标 failed，
    使缓存缺失回退到整式直算，绝不产出错误值）。
    """
    from app.services.factors.factor_compiler import compile_formula
    from app.services.factors.factor_executor import FactorExecutor
    from app.services.factors.mining import performance_probe as PROBE
    from app.services.factors.mining import subexpr_cache as SC

    with PROBE.measure_stage(probe, "ast_eval"):
        result = compile_formula(formula=text, params=None)
        plan = getattr(result, "execution_plan", None)
        if not (getattr(result, "success", False) and plan is not None):
            raise SC.UnsupportedAstError(f"公式无法编译: {text}")

    with PROBE.measure_stage(probe, "data_load"):
        outcome = FactorExecutor(wh).execute_panel(
            plan, start_date=start_date, end_date=end_date
        )
    if outcome.read_errors or outcome.factors_long is None:
        raise SC.UnsupportedAstError(
            f"面板读取失败: {[e.get('category') for e in (outcome.read_errors or [])][:3]}")
    panel = outcome.factors_long.pivot(
        index="trade_date", columns="symbol", values="raw_value"
    )
    return _pivot_dates_as_index(panel)


def _pivot_dates_as_index(frame: pd.DataFrame) -> pd.DataFrame:
    """pivot 后的 index（trade_date）统一为 `date` 类型，便于与 target 对齐。"""
    frame = frame.copy()
    frame.index = [_as_date(d) for d in frame.index]
    return frame


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


def _stat_result_dict(stats: Any) -> dict[str, Any]:
    """`StatResult` → JSON-safe dict（NaN/±Inf → None，P0 数值纪律）。"""
    from app.core.db_numeric import to_db_float

    def _f(v):
        return to_db_float(v) if isinstance(v, (int, float)) else v

    wf = dict(getattr(stats, "walk_forward", {}) or {})
    if isinstance(wf.get("per_window_icir"), list):
        wf["per_window_icir"] = [_f(x) for x in wf["per_window_icir"]]
    return {
        "p_value": _f(stats.p_value),
        "p_adj_bonferroni": _f(stats.p_adj_bonferroni),
        "q_value_fdr": _f(stats.q_value_fdr),
        "ci_lower": _f(stats.ci_lower),
        "ci_upper": _f(stats.ci_upper),
        "perm_p_value": _f(stats.perm_p_value),
        "total_trials": int(stats.total_trials or 0),
        "dsr_icir": _f(stats.dsr_icir),
        "decay_ratio": _f(stats.decay_ratio),
        "walk_forward": wf,
        "degraded": bool(stats.degraded),
    }


def _stats_provider_for(db: Any, ctx: Any, train_icir: float | None = None):
    """B2：把统计层 8 方法 + 「原始/校正 ICIR」并入评估 metrics（run_evaluation 钩子）。

    - `train_icir`：候选 train 段 ICIR（GA 阶段 `generation_icir` 口径），
      test 段 ICIR 取评估的验证段 ICIR（评估仅在此段计量）→ 衰减率 = test/train；
    - `total_trials` 取 run 行（多重检验次数唯一事实来源）；
    - 校正 ICIR 当前无额外多重检验调整项，与原始保持一致（诚实相等，不臆造数字）。
    """
    from app.services.factors.mining import statistical_tests as ST

    run = None
    try:
        from app.services.factors.mining import service as SVC
        run = SVC.load_run(db, ctx.run_id) if getattr(ctx, "run_id", None) else None
    except Exception:  # noqa: BLE001 - 尽力而为
        run = None
    total_trials = max(1, int(getattr(run, "total_trials", 0) or 0))

    def _provide(*, ic_series, icir, factor_panel, return_panel) -> dict[str, Any]:
        stats = ST.compute_stats(
            ic_series=list(ic_series or []),
            train_icir=train_icir,
            test_icir=icir,
            total_trials=total_trials,
            frequency=str(getattr(ctx, "rebalance_frequency", "daily") or "daily"),
            factor_panel=factor_panel,
            return_panel=return_panel,
            n_resamples=200,
        )
        return {
            "stats": _stat_result_dict(stats),
            "icir_raw": icir,
            "icir_adjusted": icir,
        }

    return _provide


def _default_run_evaluator(db: Any, *, factor_version_id: int,
                           factor_values: Any, forward_returns: Any,
                           config: Any, created_by: str,
                           stats_provider: Callable[..., dict[str, Any]] | None = None) -> Any:
    from app.services.factors.factor_evaluator import run_evaluation

    return run_evaluation(db, factor_version_id=factor_version_id,
                          factor_values=factor_values,
                          forward_returns=forward_returns,
                          config=config, created_by=created_by,
                          stats_provider=stats_provider)


def evaluate_full(
    ctx: MiningContext, *, individual: Any, db: Any,
    factor_values: Any, forward_returns: Any,
    draft_creator: Callable[..., Any] | None = None,
    run_evaluator: Callable[..., Any] | None = None,
    created_by: str = "mining",
    train_icir: float | None = None,
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
    # B2：默认评估器注入统计层钩子（stats 8 方法 + 原始/校正 ICIR）；
    # 注入的自定义 runner（测试桩）不接 stats_provider，保持原签名兼容。
    runner_kwargs: dict[str, Any] = {}
    if runner is _default_run_evaluator:
        runner_kwargs["stats_provider"] = _stats_provider_for(
            db, ctx, train_icir=train_icir)
    outcome = runner(db, factor_version_id=int(factor_version_id),
                     factor_values=factor_values,
                     forward_returns=forward_returns, config=config,
                     created_by=created_by, **runner_kwargs)

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
    "evaluate_short_probed",
    "evaluate_full",
]
