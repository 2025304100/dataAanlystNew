"""GA 主循环（设计文档 §6.7 / §7.8，模块 M7）。

M1 用朴素选择（按 ICIR 排序）+ 固定三率；M2 换完整九机制（M8）。

红线（设计文档 §1.3）：
  C8  适应度只用 train 段；val 仅 D1/C1 参考；test 全程锁定、仅最终确认评估一次
  C7  G2 缓存只在主进程内存，绝不写 DuckDB
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from app.services.factors.mining.contracts import (
    Fitness,
    GenerationStat,
    Individual,
    MiningContext,
    Strategy,
)
from app.services.factors.mining.evaluation_adapter import assert_no_leakage

# 固定三率基线（M1）；M2 由 C1 在安全区间内动态调（设计文档 §7.8）
BASE_STRATEGY = Strategy(
    mutation_rate=0.60,
    crossover_rate=0.30,
    random_rate=0.10,
    cross_category_ratio=0.20,
    adaptive_state="fixed",
)


@dataclass
class EvolutionParams:
    population_size: int = 100
    max_generation: int = 20
    selection_ratio: float = 0.30
    elite_count: int = 5
    convergence_threshold: float = 0.01
    tournament_k: int = 3
    max_complexity: int = 6
    short_sample_length: str = "1y"
    final_top_n: int = 50
    random_seed: int = 42
    # 初始种群两层结构（设计文档 §6.6）
    initial_population: dict = field(default_factory=dict)
    ai_generation_config: dict = field(default_factory=dict)


@dataclass
class EvolutionOutcome:
    population: list[Individual]
    generations: list[GenerationStat]
    total_trials: int
    converged: bool
    stop_reason: str


def run_evolution(
    ctx: MiningContext,
    *,
    population: list[Individual],
    params: EvolutionParams,
    progress_cb: Callable[[GenerationStat], None] | None = None,
) -> EvolutionOutcome:
    """进化主循环。每代：评估 → 观测 → 选择 → 调度 → 繁殖 → 校验 → 落库 → 停止判断。

    TODO(M7): 逐代实现，串起 M10(评估)/M8(选择繁殖)/M9(去重)/M11(G2 探针)。
    """
    raise NotImplementedError("M7: run_evolution")


def run_generation(
    ctx: MiningContext,
    *,
    generation: int,
    population: list[Individual],
) -> tuple[list[Individual], GenerationStat]:
    """跑一代，返回 (下一代种群, 本代汇总)。

    执行顺序（设计文档 §6.8 单向信号流）：
      ① 短样本评估（仅 train；内部 assert_no_leakage）
      ② 观测：D3 产 health、D1 更新 stall/delta
      ③ 选择：B1 rank+CD → A1 赛道配额 → B2 锦标赛 → 精英直留
      ④ C1 调度：唯一调参者，输出 Strategy(g)
      ⑤ 繁殖：C2 变异 / C3 交叉 / D2 注入（严格按 Strategy）
      ⑥ G2 抽样校验（每代抽 5 个，差异 >1e-6 → 报警 + 废弃重算）
      ⑦ 落库：factor_mining_generations（含 actual_* + 探针 + cache_validation_*）
      ⑧ 心跳：beat_after_generation
    TODO(M7/M8): 实现。
    """
    raise NotImplementedError("M7: run_generation")


def select_elites(population: Sequence[Individual], *, n: int) -> list[Individual]:
    """精英保留：最优 n 个直接进下一代。

    精英**不参与**锦标赛 / 变异 / 交叉 / 随机注入（设计文档 §7.8 安全护栏）。
    M1 朴素实现：按 generation_icir 降序取 Top n。
    TODO(M8): M2 改为按赛道保底 + 全局 Top。
    """
    ranked = sorted(
        population,
        key=lambda ind: (ind.generation_icir if ind.generation_icir is not None else float("-inf")),
        reverse=True,
    )
    return ranked[: max(0, n)]


def maybe_dedupe_and_validate(
    candidates: Sequence[Individual], *, ctx: MiningContext
) -> list[Individual]:
    """繁殖后统一过 AST 合法性 / 复杂度上限 / 字段依赖 / formula_hash 去重。

    非法丢弃，名额由 D2 随机个体补齐（设计文档 §6.8 执行器约定）。
    TODO(M9): 委托 dedup.batch_dedupe。
    """
    raise NotImplementedError("M9: maybe_dedupe_and_validate")


def finalize_and_evaluate(
    ctx: MiningContext, *, population: Sequence[Individual], top_n: int = 50
) -> tuple[list[tuple[Individual, Fitness]], int]:
    """最终全量验证：Top N 用**完整区间**评估，**test 段仅此一次**（红线 C8）。

    Returns:
        ([(individual, fitness)], total_trials)
    评估后 `run.status` 进入 `succeeded`，**禁止回炉**。
    TODO(M10/M13): 接 evaluation_adapter.evaluate_full + statistical_tests。
    """
    raise NotImplementedError("M10: finalize_and_evaluate")


__all__ = [
    "BASE_STRATEGY",
    "EvolutionParams",
    "EvolutionOutcome",
    "run_evolution",
    "run_generation",
    "select_elites",
    "maybe_dedupe_and_validate",
    "finalize_and_evaluate",
    "assert_no_leakage",
]
