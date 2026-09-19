"""因子挖掘域 —— L3 内部唯一交互面（冻结契约）。

设计文档 §4.5 / §8.7。
分层规则 R3：L3 各模块之间**只通过本文件的数据类**交互，不互相 import 实现。

⚠️ 签名冻结：本文件的类名与字段名在 M1a 冻结后只能**加可选字段**，
   不得改名 / 删除 / 改类型（设计文档 §4.5）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from app.services.factors.factor_evaluator import TimeSplit

# ── 枚举别名（与前端 types/mining.ts 一一对应） ──────────────────────

FactorCategory = Literal[
    "trend", "reversal", "volatility", "valuation", "quality", "volume_price"
]
RebalanceFrequency = Literal["daily", "weekly", "monthly"]
OperationType = Literal[
    "elite", "mutation", "crossover", "random", "ai_generated", "enumerated"
]
LogicSource = Literal["ai", "template", "manual"]
QualityGrade = Literal["S", "A", "B", "C", "D"]

#: 6 类归类的特征专属度优先级（设计文档 §7.7 A1）
#: trend 最宽泛，作为兜底类
CATEGORY_PRIORITY: tuple[FactorCategory, ...] = (
    "quality", "valuation", "volatility", "volume_price", "reversal", "trend",
)


# ══════════════════════════════════════════════════════════
# 切分预算（★ v2.0 新增，服务于设计文档 §7.2）
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SplitBudget:
    """三段切分的实际样本预算。

    需求要求「系统自动计算并展示：原始交易日数、有效调仓点数、各分段边界、
    被扣除的回看缓冲、……purge 数量」，本对象即该展示的唯一数据源。

    `purge_points` 是**归一化后的调仓点计数**，`purge_trading_days` 是其折算的
    交易日数——两者必须同时展示，否则用户会把「5」误读成「5 个交易日」。
    """

    frequency: RebalanceFrequency
    total_points: int
    train_points: int
    val_points: int
    test_points: int
    purge_points: int
    embargo_points: int
    purge_trading_days: int
    embargo_trading_days: int
    tail_loss: int
    frequency_floor: int
    meets_floor: bool
    statistically_degraded: bool  # 月频 → True（不做 Bootstrap/置换，最高 B 级）

    def to_dict(self) -> dict[str, Any]:
        return {
            "frequency": self.frequency,
            "totalPoints": self.total_points,
            "trainPoints": self.train_points,
            "valPoints": self.val_points,
            "testPoints": self.test_points,
            "purgePoints": self.purge_points,
            "embargoPoints": self.embargo_points,
            "purgeTradingDays": self.purge_trading_days,
            "embargoTradingDays": self.embargo_trading_days,
            "tailLoss": self.tail_loss,
            "frequencyFloor": self.frequency_floor,
            "meetsFloor": self.meets_floor,
            "statisticallyDegraded": self.statistically_degraded,
        }


# ══════════════════════════════════════════════════════════
# 挖掘上下文与个体
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class MiningContext:
    """一次挖掘实验的不可变上下文（由 experiment snapshot 还原）。"""

    run_id: str
    candidate_pool_snapshot_id: str
    data_cutoff_at: datetime
    start_date: date
    end_date: date
    rebalance_frequency: RebalanceFrequency
    target_horizon: int
    split: TimeSplit
    purge_points: int
    embargo_points: int
    train_ratio: float
    validation_ratio: float
    random_seed: int
    config_hash: str
    split_algorithm_version: str = "split-1.0.0"


@dataclass
class Individual:
    """一个候选因子个体。"""

    formula_expr: str
    canonical_formula: str
    formula_hash: str
    execution_plan: Any                      # factor_compiler.ExecutionPlan
    dependency: Any                          # CollectedDependencies
    category: FactorCategory | None
    generation: int
    parent_ids: tuple[str, ...] = ()
    operation: OperationType = "enumerated"
    complexity: int = 0
    source: str = "template"
    # AI 强制输出的 4 字段（设计文档 §7.5）
    economic_logic: str | None = None
    expected_direction: Literal["positive", "negative"] | None = None
    interpretability_score: float | None = None
    logic_source: LogicSource | None = None


@dataclass(frozen=True)
class Fitness:
    """个体适应度（**只来自 train 段**，设计文档 §7.2 / C8）。"""

    icir: float
    coverage: float
    turnover: float
    ic_mean: float
    complexity: int
    valid_cross_sections: int
    sample_count: int


@dataclass(frozen=True)
class Strategy:
    """C1 输出的本代繁殖策略（唯一调参者，设计文档 §7.8）。"""

    mutation_rate: float
    crossover_rate: float
    random_rate: float
    mutation_type_distribution: dict[str, float] = field(default_factory=dict)
    cross_category_ratio: float = 0.2
    adaptive_state: str = "mid"


@dataclass
class GenerationStat:
    """每代汇总（对应 factor_mining_generations 一行）。"""

    run_id: str
    generation: int
    population_size: int
    best_icir: float
    avg_icir: float
    median_icir: float
    diversity_score: float
    category_distribution_json: dict[str, int] = field(default_factory=dict)
    category_evenness: float = 0.0
    pareto_front_count: int = 0
    diversity_genotype: float = 0.0
    diversity_phenotype: float = 0.0
    diversity_health: float = 0.0
    stall_count: int = 0
    actual_mutation_rate: float = 0.0
    actual_crossover_rate: float = 0.0
    actual_random_rate: float = 0.0
    mutation_type_distribution_json: dict[str, float] = field(default_factory=dict)
    cross_category_ratio: float = 0.2
    adaptive_state: str = ""
    elite_count: int = 0
    mutation_count: int = 0
    crossover_count: int = 0
    random_count: int = 0
    eliminated_count: int = 0
    evaluation_duration_ms: int = 0
    convergence_delta: float = 0.0
    # ── 性能探针（M1 必埋，设计文档 §7.4） ──
    probe_data_load_ms: int = 0
    probe_ast_eval_ms: int = 0
    probe_subexpr_compute_ms: int = 0
    probe_factor_assemble_ms: int = 0
    probe_metric_calc_ms: int = 0
    probe_db_write_ms: int = 0
    probe_subexpr_total: int = 0
    probe_subexpr_unique: int = 0
    probe_g2_hit_rate: float = 0.0
    # ── G2 抽样校验（每代必写，设计文档 §7.3） ──
    cache_validation_passed: int | None = None
    cache_validation_max_diff: float | None = None


@dataclass(frozen=True)
class StatResult:
    """统计层 8 方法输出（M2，写 factor_evaluation_runs.metrics_json.stats）。"""

    p_value: float
    p_adj_bonferroni: float
    q_value_fdr: float
    ci_lower: float
    ci_upper: float
    perm_p_value: float | None
    total_trials: int
    dsr_icir: float
    decay_ratio: float
    walk_forward: dict[str, Any]
    degraded: bool = False


@dataclass(frozen=True)
class GradeResult:
    """质量分级结果（M2）。"""

    grade: QualityGrade
    reason: str
    metrics_snapshot: dict[str, Any]
    thresholds_source: Literal["default", "custom"] = "default"


__all__ = [
    "FactorCategory", "RebalanceFrequency", "OperationType", "LogicSource",
    "QualityGrade", "CATEGORY_PRIORITY",
    "SplitBudget", "MiningContext", "Individual", "Fitness", "Strategy",
    "GenerationStat", "StatResult", "GradeResult",
]
