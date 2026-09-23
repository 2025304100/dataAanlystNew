"""GA 主循环 —— 选择 / 繁殖 / 收敛 / 逐代统计（M1 朴素 + M2 集成双路径）。

**范围（任务卡 T23，M1 朴素；P0 集成卡补 M2 路径）**
- 评估（短样本，**只用 train 段**）→ 观测 → 选择 → 繁殖 → 校验 → 停止
- `selection_mode="naive"`（默认）：**朴素排序（按 ICIR 降序）** + **固定三率** +
  参数级变异/交叉 —— 与 M1 行为完全一致（回退与复现基准）
- `selection_mode="advanced"`（M2）：A1 赛道竞争 / B1 NSGA-II / B2 锦标赛选父代；
  C1 自适应调度（或固定三率）+ C2 四种变异 + C3 子树交叉 + D2 随机注入；
  D3 三层多样性观测喂 C1；D1 先自救（stall≥2）后停止（stall≥3）

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
from app.services.factors.mining.dedup import ELIM_REASON_SIMILAR

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
    """GA 配置（需求 §6.1 输入项 + 收敛口径；M2 字段见下方分组）。"""

    population_size: int = 60
    max_generations: int = 8
    #: 精英比例（按排序取 Top；精英不参与繁殖）
    selection_ratio: float = 0.20
    #: 固定三率（和为 1；护栏见常量）
    mutation_rate: float = 0.55
    crossover_rate: float = 0.25
    random_rate: float = 0.20
    #: 收敛判断：**连续 N 代**最优 ICIR 提升 < 阈值 → 停（不是固定 ICIR 门禁）
    convergence_threshold: float = 1e-3
    convergence_generations: int = 2
    seed: int = 42

    # ── M2 选择/繁殖配置（P0 集成；缺省值 = 朴素路径，行为零变化） ──
    #: "naive"（M1 朴素）| "advanced"（A1/B1/B2 + C1/C2/C3/D1/D2/D3）
    selection_mode: str = "naive"
    #: B1 启用目标（目标池子集，3~5 个）；None → DEFAULT_ENABLED_OBJECTIVES
    objectives: tuple[str, ...] | None = None
    #: B2 锦标赛 K（2~7，默认 3）
    tournament_k: int = 3
    #: C1 自适应调度开关；False → 用固定三率（但仍走 M2 选择/结构级繁殖）
    adaptive: bool = False
    #: C3 跨赛道交叉比例（非自适应时的固定值；自适应时由 C1 调节）
    cross_category_ratio: float = 0.2
    #: C2 启用的变异类型（param/op/field/struct 子集）；None → 四种全开
    mutation_ops_enabled: tuple[str, ...] | None = None
    #: 字段替换（C2）与随机注入（D2）的字段域；None → 不限
    selected_fields: tuple[str, ...] | None = None
    #: A1 弱赛道判定的 ICIR 预筛门槛
    prefilter_icir: float = 0.1

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
                f"random_rate={self.random_rate}。")
        for name, (lo, hi), value in (
            ("mutation_rate", GUARD_MUTATION, self.mutation_rate),
            ("crossover_rate", GUARD_CROSSOVER, self.crossover_rate),
            ("random_rate", GUARD_RANDOM, self.random_rate),
        ):
            if not (lo <= value <= hi):
                raise ValueError(
                    f"{name}={value} 超出安全护栏 [{lo}, {hi}]（需求 §6.1.2）。")
        mode = str(self.selection_mode)
        if mode not in ("naive", "advanced"):
            raise ValueError(
                f"selection_mode 必须是 'naive' 或 'advanced'（当前 {mode!r}）。")
        if not (0.0 <= float(self.cross_category_ratio) <= 1.0):
            raise ValueError("cross_category_ratio 必须在 [0, 1]。")
        if self.mutation_ops_enabled is not None:
            from app.services.factors.mining.reproduction.scheduler import (
                MUTATION_TYPES,
            )
            unknown = set(self.mutation_ops_enabled) - set(MUTATION_TYPES)
            if unknown:
                raise ValueError(
                    f"mutation_ops_enabled 含未知类型 {sorted(unknown)}；"
                    f"合法值 {list(MUTATION_TYPES)}。")
        if mode == "advanced":
            # B1 目标池与数量（3~5）在配置期校验，失败早于任务启动
            from app.services.factors.mining.selection.multi_objective import (
                DEFAULT_ENABLED_OBJECTIVES,
                resolve_specs,
            )
            from app.services.factors.mining.selection.tournament import (
                validate_tournament_k,
            )
            resolve_specs(self.objectives or DEFAULT_ENABLED_OBJECTIVES)
            validate_tournament_k(self.tournament_k)


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


# ══════════════════════════════════════════════════════════
# M2 集成辅助（A1/B1/B2 + C1/C2/C3 + D1/D2/D3 的编排胶水；
# 算法本体在 selection/ 与 reproduction/ 包内，此处只做调用编排）
# ══════════════════════════════════════════════════════════

#: 从公式串提取字段名 / 算子名的正则（与 reproduction.mutation 同口径）
_FORMULA_FIELD_RE = re.compile(
    r"(?<![A-Za-z_0-9])([a-z_][a-z_0-9]*)(?![A-Za-z_0-9(])")
_FORMULA_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)\s*\(")


def classify_formula(formula: str, *, declared: str | None = None) -> str:
    """按公式串重判 6 类归属（§6.5.2：变异/交叉后类别可能改变，须重新归类）。

    字段 = 非「算子名(」的标识符；算子 = `name(` 调用名；整体取负 → 反转特征。
    `declared` 仅在无任何特征命中时兜底（与 category.classify 契约一致）。
    """
    from app.services.factors.mining import category as CAT

    src = str(formula or "").strip()
    fields = [m.group(1) for m in _FORMULA_FIELD_RE.finditer(src)]
    functions = [m.group(1) for m in _FORMULA_CALL_RE.finditer(src)]
    return CAT.classify(
        fields=fields, functions=functions,
        negated=src.startswith("-"), declared=declared,
    ).category


def _b1_keys(ranked: Sequence[Mapping[str, Any]], specs: Any) -> list[Any]:
    """B1 全种群 rank + CD（评估失败的个体目标全缺失 → 沉到最后层）。"""
    from app.services.factors.mining.selection.multi_objective import (
        rank_population,
    )

    objs = [(e.get("fitness") or {}) for e in ranked]
    return rank_population(objs, specs)


def _m2_fixed_strategy(cfg: GAConfig) -> Any:
    """非自适应（adaptive=False）时的固定策略：配置三率 + 启用变异类型均分。"""
    from app.services.factors.mining.contracts import Strategy
    from app.services.factors.mining.reproduction.scheduler import MUTATION_TYPES

    enabled = tuple(cfg.mutation_ops_enabled or MUTATION_TYPES)
    share = 1.0 / len(enabled) if enabled else 0.0
    return Strategy(
        mutation_rate=float(cfg.mutation_rate),
        crossover_rate=float(cfg.crossover_rate),
        random_rate=float(cfg.random_rate),
        mutation_type_distribution={k: share for k in enabled},
        cross_category_ratio=float(cfg.cross_category_ratio),
        adaptive_state="fixed",
    )


def _mutation_dist_for(distribution: Mapping[str, float] | None,
                       enabled: Sequence[str] | None) -> dict[str, float]:
    """把 C1 的变异类型分布过滤到启用集合并归一（空 → 启用集合内均分）。"""
    from app.services.factors.mining.reproduction.scheduler import MUTATION_TYPES

    keys = tuple(enabled) if enabled else MUTATION_TYPES
    raw = {k: max(0.0, float((distribution or {}).get(k, 0.0))) for k in keys}
    total = sum(raw.values())
    if total <= 0:
        share = 1.0 / len(keys) if keys else 0.0
        return {k: share for k in keys}
    return {k: v / total for k, v in raw.items()}


def run_ga_loop(
    cfg: GAConfig, initial_population: Sequence[Mapping[str, Any]], *,
    evaluate: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    random_supplier: Callable[[int], Sequence[Mapping[str, Any]]] | None = None,
    on_generation: Callable[[int, Mapping[str, Any], list[Mapping[str, Any]]],
                            None] | None = None,
    runtime_dedup: Callable[[Sequence[Mapping[str, Any]]], Any] | None = None,
) -> GAResult:
    """**GA 主循环**（`selection_mode` 双路径）。

    Args:
        initial_population: 初始种群（T17/T18/T22/T19 产出的候选 dict）
        evaluate: 适应度评估（**只允许 train 段**，C8）→ 返回含 `icir` 的映射；
            **单个个体评估异常会被隔离**（该个体沉底淘汰），不中断整代
        random_supplier: 受约束随机注入（T18/D2）；缺省时 M2 路径回退
            `inject_individuals`（仍受约束随机），两条路径最终都用变异兜底
        on_generation: 逐代持久化回调 `(generation, record, ranked)`；
            抛异常**会向上传播**（落库失败必须让调用方知道，不能静默丢代）
        runtime_dedup: 第 4 层运行时相关性去重回调（P1-6）。每一代评估+排序后调用，
            原地把近似个体标 `eliminated_similar` 并返回 `RuntimeDedupResult`；
            被淘汰个体不进精英/繁殖池，但仍在 `ranked`（on_generation 落库用）。
            `total_trials` 仍按**全量评估数**累加（去重不减 DSR 输入源）。

    路径说明：
    - `naive`（默认）：M1 朴素——ICIR 降序精英 + 固定三率 + 参数级算子，
      收敛 = 连续 `convergence_generations` 代提升 < 阈值。
    - `advanced`（M2）：B1 rank+CD 精英与排序 → A1 赛道配额（弱赛道流转）
      → B2 锦标赛选父代 → C1 策略（自适应或固定）→ C2 四种变异 +
      C3 子树交叉 + D2 随机注入 → D3 三层 health 落库；
      D1 收敛 = 先自救（stall≥2 喂 C1）后停止（stall≥3，固定口径，
      `convergence_generations` 仅对 naive 生效）。
      `record` 额外携带 M2 代际字段（actual_ 三率/adaptive_state/diversity_*/
      category_evenness/pareto_front_count 等，由 persist_generation 落库）。

    Returns:
        `GAResult`（`total_trials` 每代累加 = Σ 种群大小，DSR 唯一输入源）
    """
    rng = random.Random(int(cfg.seed))
    population: list[dict[str, Any]] = [dict(x) for x in initial_population]
    if not population:
        raise ValueError("初始种群为空。")

    # ── M2 路径初始化（naive 时全部为 None，行为与 M1 完全一致） ──
    m2 = str(cfg.selection_mode) == "advanced"
    specs: Any = None
    scheduler: Any = None
    track_state: Any = None
    if m2:
        from app.services.factors.mining.reproduction.scheduler import (
            AdaptiveScheduler,
        )
        from app.services.factors.mining.selection.multi_objective import (
            DEFAULT_ENABLED_OBJECTIVES,
            resolve_specs,
        )
        from app.services.factors.mining.selection.track import TrackQuotaState

        specs = resolve_specs(cfg.objectives or DEFAULT_ENABLED_OBJECTIVES)
        scheduler = AdaptiveScheduler() if cfg.adaptive else None
        track_state = TrackQuotaState()

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
            entry["signature"] = fitness.pop("signature", None)
            entry["eval_error"] = None
            ranked.append(entry)

        ranked.sort(key=lambda e: _icir_of(e.get("fitness")), reverse=True)

        gen_best = _icir_of(ranked[0].get("fitness")) if ranked else float("-inf")
        gen_best = None if gen_best == float("-inf") else gen_best

        values = [e["fitness"].get("icir") for e in ranked if e.get("fitness")]
        values = [DN.to_db_float(v) for v in values if v is not None]
        avg_icir = round(sum(values) / len(values), 6) if values else None
        median_icir = (round(sorted(values)[len(values) // 2], 6)
                       if values else None)

        # ── 1.5 第 4 层运行时相关性去重（P1-6）──
        # 评估+排序后标 `eliminated_similar`；淘汰个体不进精英/繁殖池，
        # 但仍在 `ranked`（on_generation 落库保留淘汰元数据）。
        # total_trials 按**全量评估数**累加（去重只减繁殖池，不减 DSR 输入源）。
        evaluated_count = len(ranked)
        if runtime_dedup is not None:
            try:
                runtime_dedup(ranked)
            except Exception:  # noqa: BLE001 - 去重失败退化为全量繁殖，不阻断进化
                for e in ranked:
                    e.setdefault("elimination_status", "active")
        active = [e for e in ranked
                  if e.get("elimination_reason") != ELIM_REASON_SIMILAR]

        # ── 2. 精英选择（naive：按 ICIR 降序；M2：按 B1 钥匙 rank→CD）──
        elite_count = max(1, round(float(cfg.selection_ratio)
                                   * int(cfg.population_size)))
        elite_count = min(elite_count, len(active))

        m2_extra: dict[str, Any] = {}
        parents: list[dict[str, Any]] = []
        strategy: Any = None
        health: float | None = None
        conv: Any = None

        if m2:
            # 2a. B1 全种群 rank+CD；active 重排为 B1 序（rank 升、同 rank CD 降）
            keys = _b1_keys(active, specs)
            for k in keys:
                active[k.index]["_b1_key"] = (k.rank, k.cd)
            active = [active[k.index] for k in keys]
            m2_extra["pareto_front_count"] = sum(1 for k in keys if k.rank == 1)

            # 2b. 类别归类补全（无 category 的个体按公式重判，§6.5.2）
            counts_by_track: dict[str, int] = {}
            for e in active:
                cat = e.get("category") or classify_formula(
                    str(e.get("formula") or e.get("canonical_formula") or ""))
                e["category"] = cat
                counts_by_track[cat] = counts_by_track.get(cat, 0) + 1
            m2_extra["category_distribution_json"] = dict(counts_by_track)

            # 2c. D3 观测（只产 health，不改任何率）
            from app.services.factors.mining.diversity import (
                diversity_health,
                genotype_diversity,
                phenotype_diversity,
            )
            from app.services.factors.mining.selection.track import (
                category_evenness,
            )

            evenness = category_evenness(counts_by_track, len(active))
            geno = genotype_diversity(
                [str(e.get("formula") or "") for e in active])
            pheno = phenotype_diversity(values)
            health = diversity_health(evenness, geno, pheno)
            m2_extra.update({
                "category_evenness": round(evenness, 6),
                "diversity_genotype": round(geno, 6),
                "diversity_phenotype": round(pheno, 6),
                "diversity_health": round(health, 6),
                "diversity_score": round(health, 6),
            })

            # 2d. D1 观测（先自救 stall≥2，后停止 stall≥3；只判停止不调参）
            from app.services.factors.mining.convergence import (
                update_convergence,
            )

            conv = update_convergence(
                best_icir=(gen_best if gen_best is not None else float("nan")),
                prev_best=best_icir, stall_count=stall,
                threshold=float(cfg.convergence_threshold))
            stall = int(conv.stall_count)
            m2_extra["convergence_delta"] = DN.to_db_float(conv.convergence_delta)

            # 2e. A1 赛道配额 + B2 锦标赛选父代（精英不参与）
            non_elites = active[elite_count:] or list(active[:1])
            ne_counts: dict[str, int] = {}
            best_rank_by_track: dict[str, tuple[int, float]] = {}
            best_icir_by_track: dict[str, float] = {}

            def _b1_better(a: tuple[int, float], b: tuple[int, float]) -> bool:
                # B1 字典序：rank 升序优先，同 rank 比 CD 降序（与 tournament 一致）
                return a[0] < b[0] or (a[0] == b[0] and a[1] > b[1])

            for e in non_elites:
                cat = str(e.get("category"))
                ne_counts[cat] = ne_counts.get(cat, 0) + 1
                key = e["_b1_key"]
                if cat not in best_rank_by_track or _b1_better(
                        key, best_rank_by_track[cat]):
                    best_rank_by_track[cat] = key
                icir_val = _icir_of(e.get("fitness"))
                if cat not in best_icir_by_track or icir_val > best_icir_by_track[cat]:
                    best_icir_by_track[cat] = icir_val

            from app.services.factors.mining.selection.track import (
                compute_quotas,
                update_weak_streaks,
            )

            weak = update_weak_streaks(
                track_state,
                best_rank_by_track={c: k[0] for c, k in best_rank_by_track.items()},
                best_icir_by_track=best_icir_by_track,
                population_max_rank=max(k.rank for k in keys),
                prefilter_icir=float(cfg.prefilter_icir))
            try:
                quotas = compute_quotas(
                    len(non_elites), ne_counts,
                    best_rank_by_track={c: k[0] for c, k in
                                        best_rank_by_track.items()},
                    best_icir_by_track=best_icir_by_track,
                    weak_tracks=weak)
            except ValueError:
                quotas = {}                        # 防御：配额不可解 → 直通

            from app.services.factors.mining.selection.tournament import (
                tournament_select,
            )

            for track in sorted(quotas):
                quota = int(quotas[track])
                pool = [e for e in non_elites if e.get("category") == track]
                if pool and quota > 0:
                    parents.extend(tournament_select(
                        pool, quota, k=int(cfg.tournament_k), rng=rng,
                        key=lambda it: it["_b1_key"]))
            if not parents:
                parents = list(non_elites)

            # 2f. C1 调度（唯一调参者；adaptive=False → 固定三率策略）
            if scheduler is not None:
                strategy = scheduler.step(
                    generation=gen, max_generations=int(cfg.max_generations),
                    health=health, stall_count=stall,
                    convergence_delta=float(conv.convergence_delta),
                    threshold=float(cfg.convergence_threshold))
            else:
                strategy = _m2_fixed_strategy(cfg)
            m2_extra["adaptive_state"] = str(strategy.adaptive_state)
            m2_extra["cross_category_ratio"] = DN.to_db_float(
                strategy.cross_category_ratio)

        elites = [dict(e, operation=OP_ELITE) for e in active[:elite_count]]
        breeding = active[elite_count:] or active[:1]      # 防全空（naive 用）

        # ── 3. 繁殖配额（三率 × 非精英名额；余数归变异）──
        slots = max(0, int(cfg.population_size) - elite_count)
        mutation_type_counts: dict[str, int] = {}

        if m2:
            # M2：C2 四种变异 + C3 子树交叉 + D2 随机注入（比例全部来自 C1）
            from app.services.factors.mining.reproduction.crossover import (
                crossover_formula as rep_crossover,
                should_cross_category,
            )
            from app.services.factors.mining.reproduction.injection import (
                injection_quota,
            )
            from app.services.factors.mining.reproduction.mutation import (
                allocate_mutation_types,
                mutate_formula as rep_mutate,
            )

            mutation_count = round(float(strategy.mutation_rate) * slots)
            crossover_count = round(float(strategy.crossover_rate) * slots)
            random_count = injection_quota(int(cfg.population_size),
                                           float(strategy.random_rate))
            # 名额对齐 slots（注入按全种群折算，与 slots 可能有 1~2 差异 → 调变异）
            diff = slots - (mutation_count + crossover_count + random_count)
            if diff > 0:
                mutation_count += diff
            elif diff < 0:
                cut = min(-diff, mutation_count)
                mutation_count -= cut
                leftover = -diff - cut
                if leftover > 0:
                    random_count = max(0, random_count - leftover)

            kinds = allocate_mutation_types(
                mutation_count,
                _mutation_dist_for(strategy.mutation_type_distribution,
                                   cfg.mutation_ops_enabled),
                rng=rng)
            for kind in kinds:
                mutation_type_counts[kind] = \
                    mutation_type_counts.get(kind, 0) + 1

            offspring: list[dict[str, Any]] = []
            # C2 变异（四种类型，结构级；字段替换不越出已选字段域）
            for i, kind in enumerate(kinds):
                parent = dict(parents[i % len(parents)])
                base_formula = str(parent.get("formula")
                                   or parent.get("canonical_formula") or "")
                new_formula = rep_mutate(
                    base_formula, kind=kind, rng=rng,
                    allowed_fields=(tuple(cfg.selected_fields)
                                    if cfg.selected_fields else None))
                child = dict(parent)
                child["formula"] = new_formula
                child["canonical_formula"] = new_formula
                child["formula_hash"] = new_formula or parent.get("formula_hash")
                child["operation"] = OP_MUTATION
                child["generation"] = gen + 1
                child["parent_ids"] = [str(parent.get("id")
                                           or parent.get("formula_hash") or "")]
                child.pop("fitness", None)
                child.pop("eval_error", None)
                child.pop("_b1_key", None)
                child["category"] = classify_formula(
                    new_formula, declared=parent.get("category"))
                offspring.append(child)
            # C3 子树交叉（同/跨赛道比例由 C1 决定）
            for i in range(crossover_count):
                pa = parents[(i * 2) % len(parents)]
                if should_cross_category(float(strategy.cross_category_ratio),
                                         rng):
                    pool = [p for p in parents
                            if p.get("category") != pa.get("category")]
                else:
                    pool = [p for p in parents
                            if p.get("category") == pa.get("category")
                            and p is not pa]
                if not pool:
                    pool = [p for p in parents if p is not pa] or [pa]
                pb = pool[rng.randrange(len(pool))]
                child_formula = rep_crossover(
                    str(pa.get("formula") or pa.get("canonical_formula") or ""),
                    str(pb.get("formula") or pb.get("canonical_formula") or ""),
                    rng=rng)
                child = dict(pa)
                child["formula"] = child_formula
                child["canonical_formula"] = child_formula
                child["formula_hash"] = child_formula or pa.get("formula_hash")
                child["operation"] = OP_CROSSOVER
                child["generation"] = gen + 1
                child["parent_ids"] = [
                    str(pa.get("id") or pa.get("formula_hash") or ""),
                    str(pb.get("id") or pb.get("formula_hash") or "")]
                child.pop("fitness", None)
                child.pop("eval_error", None)
                child.pop("_b1_key", None)
                child["category"] = classify_formula(child_formula)
                offspring.append(child)
            # D2 随机注入（注入个体为受约束随机公式）
            if random_count:
                injected: list[Mapping[str, Any]] = []
                if random_supplier is not None:
                    try:
                        injected = list(random_supplier(random_count))
                    except Exception:               # noqa: BLE001 - 注入失败回退
                        injected = []
                if not injected:
                    from app.services.factors.mining.reproduction.injection import (
                        inject_individuals,
                    )
                    injected = inject_individuals(
                        strategy=strategy,
                        population_size=int(cfg.population_size), rng=rng,
                        selected_fields=(tuple(cfg.selected_fields)
                                         if cfg.selected_fields else None),
                        existing_formulas=[
                            str(e.get("formula") or "") for e in ranked])
                for item in injected[:random_count]:
                    injected_item = dict(item)
                    injected_item["operation"] = OP_RANDOM
                    injected_item["generation"] = gen + 1
                    injected_item.pop("fitness", None)
                    injected_item.pop("eval_error", None)
                    injected_item.pop("_b1_key", None)
                    if not injected_item.get("category"):
                        injected_item["category"] = classify_formula(
                            str(injected_item.get("formula") or ""))
                    offspring.append(injected_item)
                # ⚠️ record 记**分配名额**（与 M1 口径一致）：supplier 返回不足时
                #    由下方参数级变异兜底补齐，三角和恒等于非精英名额。
            # 兜底：名额不足时用参数级变异补齐（保证种群大小稳定）
            while len(offspring) < slots:
                parent = dict(parents[len(offspring) % len(parents)])
                base_formula = str(parent.get("formula")
                                   or parent.get("canonical_formula") or "")
                parent["formula"] = mutate_formula(base_formula, rng)
                parent["operation"] = OP_MUTATION
                parent["generation"] = gen + 1
                parent.pop("fitness", None)
                parent.pop("eval_error", None)
                parent.pop("_b1_key", None)
                parent["category"] = classify_formula(
                    parent["formula"], declared=parent.get("category"))
                offspring.append(parent)
            offspring = offspring[:slots]
        else:
            # ── M1 朴素路径（固定三率 + 参数级算子；与原实现逐行等价） ──
            mutation_count = round(float(cfg.mutation_rate) * slots)
            crossover_count = round(float(cfg.crossover_rate) * slots)
            random_count = max(0, slots - mutation_count - crossover_count)

            rng_shuffled = list(breeding)
            rng.shuffle(rng_shuffled)

            offspring = []
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

        total_trials += evaluated_count
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
        if m2:
            # M2 代际附加字段（persist_generation 落库；探针由 T21 另行合入）
            record.update(m2_extra)
            record["actual_mutation_rate"] = (
                round(mutation_count / slots, 6) if slots > 0 else None)
            record["actual_crossover_rate"] = (
                round(crossover_count / slots, 6) if slots > 0 else None)
            record["actual_random_rate"] = (
                round(random_count / slots, 6) if slots > 0 else None)
            if mutation_count > 0:
                record["mutation_type_distribution_json"] = {
                    k: round(v / mutation_count, 6)
                    for k, v in mutation_type_counts.items()}
            else:
                record["mutation_type_distribution_json"] = {}
        history.append(record)
        if on_generation is not None:
            on_generation(gen, record, ranked)

        # ── 4. 收敛判断 ──
        # M2（D1）：先自救（stall≥2 已喂 C1）后停止（stall≥3）；
        # naive：连续 convergence_generations 代提升 < 阈值。
        if m2:
            if conv is not None and conv.should_stop:
                stopped_reason = "converged"
                break
        elif best_icir is not None and gen_best is not None:
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
    "GAConfig", "GAResult", "run_ga_loop",
    "mutate_formula", "crossover_formulas", "classify_formula",
]
