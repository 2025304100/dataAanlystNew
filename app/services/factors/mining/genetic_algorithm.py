"""GA 主循环（朴素版，M1）—— 选择 / 繁殖 / 收敛 / 逐代统计。

**范围（任务卡 T23，M1 朴素）**
- 评估（短样本，**只用 train 段**）→ 观测 → 选择 → 繁殖 → 校验 → 停止
- 选择：**朴素排序（按 ICIR 降序）**；A1 赛道竞争 / B1 NSGA-II / B2 锦标赛属 M2（T33）
- 繁殖：**固定三率**（变异/交叉/注入），自适应调度属 M2（C1）
- 变异 / 交叉为**参数级**（M1 朴素）；结构级变异 / 子树交叉属 M2（C2/C3）

**任务卡四条坑的落点**
1. **收敛判断 = 连续 N 代提升 < 阈值**（`convergence_generations`），绝不是
   「ICIR 达到某个固定门禁值」—— 门禁式收敛会在低 ICIR 区间永远跑不满代数。
2. **`total_trials` 每代累加**（写入 `factor_mining_runs.total_trials`）——
   它是 DSR（deflated Sharpe）多重检验校正的**唯一输入源**：少记一代，
   DSR 就会低估数据挖掘偏差，直接虚高「统计显著」的置信度。
3. **精英 `operation='elite'` 且不参与变异/交叉/注入** —— 精英是上一代直接
   存活的个体，再被繁殖一次等于重复计次并破坏血统记录。
4. **写 MySQL 数值列前必须过 `app.core.db_numeric`**（见 `service.py` 的落库层）；
   本模块产出的统计值也统一走 `db_numeric._safe` 归一化，保证下游拿不到 NaN/Inf。

**注入点（全部可替换 → 离线可测）**
- `evaluate(individual) -> Mapping`：适应度评估（真实实现 =
  `evaluation_adapter.evaluate_short`，只允许 train 段）
- `random_supplier(count) -> list[dict]`：受约束随机注入（真实实现 = T18）
- `on_generation(record)`：逐代持久化回调（真实实现 = `service.persist_generation`）
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from app.core import db_numeric as DN

# ══════════════════════════════════════════════════════════
# 配置与护栏
# ══════════════════════════════════════════════════════════

#: 三率护栏（需求 §6.1.2 安全护栏；M1 固定三率也必须落在此区间）
GUARD_MUTATION = (0.40, 0.80)
GUARD_CROSSOVER = (0.10, 0.50)
GUARD_RANDOM = (0.05, 0.20)
#: 三率之和允许的容差
RATE_SUM_TOLERANCE = 1e-6

#: 操作类型（与 `contracts.OperationType` 对齐）
OP_ELITE = "elite"
OP_MUTATION = "mutation"
OP_CROSSOVER = "crossover"
OP_RANDOM = "random"

_NUM_RE = re.compile(r"(?<![A-Za-z_0-9.])(\d+(?:\.\d+)?)(?![A-Za-z_0-9])")


@dataclass
class GAConfig:
    """GA 配置（需求 §6.1 输入项 + 收敛口径）。"""

    population_size: int = 60
    max_generations: int = 8
    #: 精英比例（按 ICIR 排序取 Top；精英不参与繁殖）
    selection_ratio: float = 0.20
    #: 固定三率（和为 1；护栏见常量）
    mutation_rate: float = 0.55
    crossover_rate: float = 0.25
    random_rate: float = 0.20
    #: 收敛判断：**连续 N 代**最优 ICIR 提升 < 阈值 → 停（不是固定 ICIR 门禁）
    convergence_threshold: float = 1e-3
    convergence_generations: int = 2
    seed: int = 42

    def __post_init__(self) -> None:
        if int(self.population_size) < 4:
            raise ValueError("population_size 至少为 4（否则选择/繁殖无意义）。")
        if int(self.max_generations) < 1:
            raise ValueError("max_generations 至少为 1。")
        if not (0.0 < float(self.selection_ratio) < 1.0):
            raise ValueError("selection_ratio 必须在 (0, 1)。")
        rate_sum = self.mutation_rate + self.crossover_rate + self.random_rate
        if abs(rate_sum - 1.0) > RATE_SUM_TOLERANCE:
            raise ValueError(
                f"三率之和必须为 1（当前 {rate_sum:.6f}）："
                f"mutation={self.mutation_rate} crossover={self.crossover_rate} "
                f"random={self.random_rate}。")
        for name, (lo, hi), value in (
            ("mutation_rate", GUARD_MUTATION, self.mutation_rate),
            ("crossover_rate", GUARD_CROSSOVER, self.crossover_rate),
            ("random_rate", GUARD_RANDOM, self.random_rate),
        ):
            if not (lo <= value <= hi):
                raise ValueError(
                    f"{name}={value} 超出安全护栏 [{lo}, {hi}]（需求 §6.1.2）。")


@dataclass
class GAResult:
    """GA 运行结果（供 T24 最终验证消费）。"""

    generations_run: int
    total_trials: int                       # 🚨 DSR 唯一输入源（每代累加）
    best_icir: float | None
    best_formula: str | None
    stall_count: int
    stopped_reason: str                     # converged / max_generations
    history: list[dict[str, Any]] = field(default_factory=list)


# ══════════════════════════════════════════════════════════
# M1 朴素算子（参数级；结构级属 M2 C2/C3）
# ══════════════════════════════════════════════════════════


def mutate_formula(formula: str, rng: random.Random, *,
                   intensity: float = 0.25,
                   window_min: int = 1, window_max: int = 250) -> str:
    """**参数级变异**：把公式里的数值参数逐个按 ±`intensity` 扰动（窗口 ≥1）。

    M1 朴素实现：只动参数不动结构。返回新串；无数值参数时原样返回。
    """
    def _perturb(m: re.Match) -> str:
        raw = m.group(1)
        try:
            value = float(raw)
        except ValueError:
            return raw
        delta = value * intensity * (1 if rng.random() < 0.5 else -1)
        new = max(1.0, value + delta)       # 窗口/参数下限 1
        if value == int(value):
            return str(max(window_min, min(window_max, int(round(new)))))
        return f"{new:.4f}"

    return _NUM_RE.sub(_perturb, formula)


def crossover_formulas(a: str, b: str, rng: random.Random) -> str:
    """**参数级交叉**：保留 A 的结构，把 A 的数值参数序列与 B 的**交错混合**。

    两个公式的数值参数个数可能不同 → 取 `min` 长度做交错，尾部保留 A 的。
    M1 朴素实现；子树级交叉属 M2（C3）。
    """
    a_nums = _NUM_RE.findall(a)
    b_nums = _NUM_RE.findall(b)
    if not a_nums or not b_nums:
        return a
    mixed: list[str] = []
    for i in range(len(a_nums)):
        source = b_nums if rng.random() < 0.5 else a_nums
        mixed.append(source[i % len(source)])
    it = iter(mixed)
    return _NUM_RE.sub(lambda _m: next(it), a)


# ══════════════════════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════════════════════


def _icir_of(fitness: Mapping[str, Any] | None) -> float:
    """从适应度映射取 ICIR；缺失/NaN → -inf（排序时沉底）。"""
    if not fitness:
        return float("-inf")
    try:
        value = float(fitness.get("icir"))
    except (TypeError, ValueError):
        return float("-inf")
    return value if not (math_is_nan_inf(value)) else float("-inf")


def math_is_nan_inf(value: float) -> bool:
    import math

    return math.isnan(value) or math.isinf(value)


def run_ga_loop(
    cfg: GAConfig, initial_population: Sequence[Mapping[str, Any]], *,
    evaluate: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    random_supplier: Callable[[int], Sequence[Mapping[str, Any]]] | None = None,
    on_generation: Callable[[int, Mapping[str, Any], list[Mapping[str, Any]]],
                            None] | None = None,
) -> GAResult:
    """**朴素 GA 主循环**（M1）。

    Args:
        initial_population: 初始种群（T17/T18/T22/T19 产出的候选 dict）
        evaluate: 适应度评估（**只允许 train 段**，C8）→ 返回含 `icir` 的映射；
            **单个个体评估异常会被隔离**（该个体沉底淘汰），不中断整代
        random_supplier: 受约束随机注入（T18）；缺省时用参数级变异兜底
        on_generation: 逐代持久化回调 `(generation, record, ranked)`；
            抛异常**会向上传播**（落库失败必须让调用方知道，不能静默丢代）

    Returns:
        `GAResult`（`total_trials` 每代累加 = Σ 种群大小，DSR 唯一输入源）
    """
    rng = random.Random(int(cfg.seed))
    population: list[dict[str, Any]] = [dict(x) for x in initial_population]
    if not population:
        raise ValueError("初始种群为空。")

    total_trials = 0
    stall = 0
    best_icir: float | None = None
    best_formula: str | None = None
    history: list[dict[str, Any]] = []
    stopped_reason = "max_generations"
    generations_run = 0

    for gen in range(int(cfg.max_generations)):
        # ── 1. 评估（异常隔离：单个个体失败不中断整代）──
        ranked: list[dict[str, Any]] = []
        for item in population:
            entry = dict(item)
            try:
                fitness = dict(evaluate(item))
            except Exception as exc:  # noqa: BLE001 - 评估失败 = 该个体淘汰
                entry["fitness"] = None
                entry["eval_error"] = f"{type(exc).__name__}: {exc}"
                ranked.append(entry)
                continue
            entry["fitness"] = fitness
            entry["eval_error"] = None
            ranked.append(entry)

        ranked.sort(key=lambda e: _icir_of(e.get("fitness")), reverse=True)

        # ── 2. 精英选择（朴素：按 ICIR 降序取 Top；不参与繁殖）──
        elite_count = max(1, round(float(cfg.selection_ratio)
                                   * int(cfg.population_size)))
        elite_count = min(elite_count, len(ranked))
        elites = [dict(e, operation=OP_ELITE) for e in ranked[:elite_count]]
        breeding = ranked[elite_count:] or ranked[:1]      # 防全空

        gen_best = _icir_of(ranked[0].get("fitness")) if ranked else float("-inf")
        gen_best = None if gen_best == float("-inf") else gen_best

        values = [e["fitness"].get("icir") for e in ranked if e.get("fitness")]
        values = [DN.to_db_float(v) for v in values if v is not None]
        avg_icir = round(sum(values) / len(values), 6) if values else None
        median_icir = (round(sorted(values)[len(values) // 2], 6)
                       if values else None)

        # ── 3. 繁殖配额（三率 × 非精英名额；余数归变异）──
        slots = max(0, int(cfg.population_size) - elite_count)
        mutation_count = round(float(cfg.mutation_rate) * slots)
        crossover_count = round(float(cfg.crossover_rate) * slots)
        random_count = max(0, slots - mutation_count - crossover_count)

        rng_shuffled = list(breeding)
        rng.shuffle(rng_shuffled)

        offspring: list[dict[str, Any]] = []
        # 变异
        for i in range(mutation_count):
            parent = dict(rng_shuffled[i % len(rng_shuffled)])
            parent["formula"] = mutate_formula(str(parent["formula"]), rng)
            parent["operation"] = OP_MUTATION
            parent["generation"] = gen + 1
            parent["parent_ids"] = [str(parent.get("id") or parent.get("formula_hash") or "")]
            parent.pop("fitness", None)
            parent.pop("eval_error", None)
            offspring.append(parent)
        # 交叉
        for i in range(crossover_count):
            pa = dict(rng_shuffled[(i * 2) % len(rng_shuffled)])
            pb = dict(rng_shuffled[(i * 2 + 1) % len(rng_shuffled)])
            child = dict(pa)
            child["formula"] = crossover_formulas(str(pa["formula"]),
                                                  str(pb["formula"]), rng)
            child["operation"] = OP_CROSSOVER
            child["generation"] = gen + 1
            child["parent_ids"] = [str(pa.get("id") or pa.get("formula_hash") or ""),
                                   str(pb.get("id") or pb.get("formula_hash") or "")]
            child.pop("fitness", None)
            child.pop("eval_error", None)
            offspring.append(child)
        # 随机注入
        if random_count and random_supplier is not None:
            for item in random_supplier(random_count)[:random_count]:
                injected = dict(item)
                injected["operation"] = OP_RANDOM
                injected["generation"] = gen + 1
                offspring.append(injected)
        # 兜底：名额不足时用参数级变异补齐（保证种群大小稳定）
        while len(offspring) < slots:
            parent = dict(rng_shuffled[len(offspring) % len(rng_shuffled)])
            parent["formula"] = mutate_formula(str(parent["formula"]), rng)
            parent["operation"] = OP_MUTATION
            parent["generation"] = gen + 1
            parent.pop("fitness", None)
            parent.pop("eval_error", None)
            offspring.append(parent)
        offspring = offspring[:slots]

        total_trials += len(ranked)
        generations_run = gen + 1

        record = {
            "generation": gen,
            "population_size": len(ranked),
            "best_icir": gen_best,
            "avg_icir": avg_icir,
            "median_icir": median_icir,
            "elite_count": elite_count,
            "mutation_count": mutation_count,
            "crossover_count": crossover_count,
            # ⚠️ 记录**分配名额**而非实际注入数：random_supplier 缺席时由变异兜底，
            #    若按「实际 OP_RANDOM 条数」记，三率配额就对不上（三角和 ≠ 非精英名额）
            "random_count": random_count,
            "total_trials": total_trials,
            "stall_count": stall,
            "eliminated_count": len([e for e in ranked if e.get("eval_error")]),
        }
        history.append(record)
        if on_generation is not None:
            on_generation(gen, record, ranked)

        # ── 4. 收敛判断：**连续 N 代**提升 < 阈值（不是固定 ICIR 门禁）──
        if best_icir is not None and gen_best is not None:
            improvement = gen_best - best_icir
            if improvement < cfg.convergence_threshold:
                stall += 1
            else:
                stall = 0
            if stall >= int(cfg.convergence_generations):
                stopped_reason = "converged"
                if on_generation is None:
                    history[-1]["stall_count"] = stall
                break
        if gen_best is not None and (best_icir is None or gen_best > best_icir):
            best_icir = gen_best
            best_formula = str(ranked[0].get("formula") or "")

        # ── 5. 下一代种群 = 精英 + 子代（精英不参与繁殖，坑 3）──
        population = elites + offspring

    return GAResult(
        generations_run=generations_run,
        total_trials=total_trials,
        best_icir=best_icir,
        best_formula=best_formula,
        stall_count=stall,
        stopped_reason=stopped_reason,
        history=history,
    )


__all__ = [
    "GUARD_MUTATION", "GUARD_CROSSOVER", "GUARD_RANDOM", "RATE_SUM_TOLERANCE",
    "OP_ELITE", "OP_MUTATION", "OP_CROSSOVER", "OP_RANDOM",
    "GAConfig", "GAResult",
    "mutate_formula", "crossover_formulas",
    "run_ga_loop",
]
