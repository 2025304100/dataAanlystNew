"""WP5-05: 参数和样本扰动（压力测试）。

对齐 docs/因子设置与专业因子库改造方案.md §8.3。

扰动标准：
- 时间窗口参数默认测试 -20%、-10%、基准、+10%、+20%
- 邻近参数的 IC 符号大体一致
- 基准参数不是唯一显著尖峰
- 扰动后的中位 IC 不低于基准的 60%
- 至少 3 个相邻参数通过最小有效门禁
- 若 IC 出现断崖式下跌，版本直接 rejected
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from app.services.factors.factor_evaluator import (
    EvaluationConfig,
    ICMetrics,
    compute_rank_ic,
    compute_quantile_returns,
    compute_cost_adjusted_return,
    compute_coverage,
    evaluate_gate,
)


# 默认扰动比例（-20%、-10%、基准、+10%、+20%）
DEFAULT_PERTURBATION_RATIOS = [-0.20, -0.10, 0.0, 0.10, 0.20]

# 扰动门禁阈值
PERTURBATION_THRESHOLDS = {
    "min_consistent_sign_ratio": 0.60,  # 邻近参数 IC 符号一致比例 >= 60%
    "min_median_ic_ratio": 0.60,  # 扰动后中位 IC >= 基准的 60%
    "min_passing_neighbors": 3,  # 至少 3 个相邻参数通过最小门禁
    "cliff_drop_threshold": 0.50,  # IC 断崖阈值：低于基准 50% 视为断崖
}


@dataclass
class PerturbationPoint:
    """单个扰动点结果。"""

    label: str  # 如 "-20%" / "baseline" / "+10%"
    ratio: float  # 扰动比例
    param_value: float  # 扰动后的参数值
    ic_mean: float
    icir: float
    passed_min_gate: bool  # 是否通过最小有效门禁


@dataclass
class ParameterPerturbationResult:
    """参数扰动结果。"""

    param_name: str  # 扰动的参数名
    baseline_value: float  # 基准参数值
    points: list[PerturbationPoint]  # 各扰动点
    sign_consistency_ratio: float  # IC 符号一致性比例
    median_ic_ratio: float  # 扰动后中位 IC / 基准 IC
    passing_neighbor_count: int  # 通过最小门禁的相邻参数数
    has_cliff_drop: bool  # 是否出现断崖式下跌
    verdict: str  # stable / unstable / cliff_drop


def perturb_parameter(
    *,
    param_name: str,
    baseline_value: float,
    ratios: list[float] | None = None,
    eval_fn: Callable[[float], ICMetrics],  # 给定参数值返回 IC 指标
) -> ParameterPerturbationResult:
    """对单个参数执行扰动测试。

    Args:
        param_name: 参数名（如 "window"）
        baseline_value: 基准参数值
        ratios: 扰动比例列表（默认 [-20%, -10%, 0%, +10%, +20%]）
        eval_fn: 接受扰动后参数值，返回 ICMetrics
    """
    ratios = ratios or DEFAULT_PERTURBATION_RATIOS
    points: list[PerturbationPoint] = []

    for ratio in ratios:
        # 扰动后的参数值（整数参数取整）
        perturbed = baseline_value * (1 + ratio)
        if baseline_value == int(baseline_value):
            perturbed = float(max(1, round(perturbed)))

        label = "baseline" if ratio == 0.0 else f"{ratio:+.0%}"
        ic_metrics = eval_fn(perturbed)

        # 最小有效门禁：ICIR > 0 且 IC 符号正确
        passed = ic_metrics.icir > 0 and ic_metrics.rank_ic_mean > 0

        points.append(PerturbationPoint(
            label=label,
            ratio=ratio,
            param_value=perturbed,
            ic_mean=ic_metrics.rank_ic_mean,
            icir=ic_metrics.icir,
            passed_min_gate=passed,
        ))

    # 分析扰动稳定性
    baseline_point = next((p for p in points if p.ratio == 0.0), points[0])
    baseline_ic = abs(baseline_point.ic_mean) if baseline_point.ic_mean != 0 else 1e-10

    # IC 符号一致性
    signs = [1 if p.ic_mean > 0 else (-1 if p.ic_mean < 0 else 0) for p in points]
    baseline_sign = 1 if baseline_point.ic_mean > 0 else (-1 if baseline_point.ic_mean < 0 else 0)
    consistent = sum(1 for s in signs if s == baseline_sign)
    sign_ratio = float(consistent / len(signs)) if signs else 0.0

    # 中位 IC / 基准 IC
    ic_values = [abs(p.ic_mean) for p in points]
    median_ic = float(np.median(ic_values)) if ic_values else 0.0
    median_ratio = float(median_ic / baseline_ic) if baseline_ic > 1e-10 else 0.0

    # 通过最小门禁的相邻参数数
    passing_count = sum(1 for p in points if p.passed_min_gate)

    # 断崖检测：任一非基准点的 IC 低于基准的 50%
    cliff = any(
        p.ratio != 0.0 and abs(p.ic_mean) < baseline_ic * PERTURBATION_THRESHOLDS["cliff_drop_threshold"]
        for p in points
    )

    # 最终判定
    if cliff:
        verdict = "cliff_drop"
    elif (sign_ratio >= PERTURBATION_THRESHOLDS["min_consistent_sign_ratio"]
          and median_ratio >= PERTURBATION_THRESHOLDS["min_median_ic_ratio"]
          and passing_count >= PERTURBATION_THRESHOLDS["min_passing_neighbors"]):
        verdict = "stable"
    else:
        verdict = "unstable"

    return ParameterPerturbationResult(
        param_name=param_name,
        baseline_value=baseline_value,
        points=points,
        sign_consistency_ratio=sign_ratio,
        median_ic_ratio=median_ratio,
        passing_neighbor_count=passing_count,
        has_cliff_drop=cliff,
        verdict=verdict,
    )


@dataclass
class TimeSegmentResult:
    """时间段扰动结果。"""

    segment_label: str  # 如 "2024-H1" / "2024-H2"
    ic_mean: float
    icir: float
    positive_ic_ratio: float
    sample_count: int


@dataclass
class TimePerturbationResult:
    """时间段扰动结果。"""

    segments: list[TimeSegmentResult]
    ic_stability: float  # 各段 IC 均值的标准差/绝对均值（越低越稳定）
    verdict: str  # stable / unstable


def perturb_time_segments(
    *,
    features: pd.DataFrame,
    targets: pd.DataFrame,
    segment_size: int = 60,  # 每段交易日数
) -> TimePerturbationResult:
    """按时间段切分做扰动测试。

    将验证期按 segment_size 个交易日切分，分别计算 IC，检验稳定性。
    """
    common_dates = sorted(features.index.intersection(targets.index))
    if len(common_dates) < segment_size * 2:
        return TimePerturbationResult(segments=[], ic_stability=1.0, verdict="unstable")

    segments: list[TimeSegmentResult] = []
    for i in range(0, len(common_dates) - segment_size + 1, segment_size):
        seg_dates = common_dates[i:i + segment_size]
        seg_features = features.loc[seg_dates]
        seg_targets = targets.loc[seg_dates]

        ic = compute_rank_ic(seg_features, seg_targets)
        segments.append(TimeSegmentResult(
            segment_label=f"{seg_dates[0].isoformat()}~{seg_dates[-1].isoformat()}",
            ic_mean=ic.rank_ic_mean,
            icir=ic.icir,
            positive_ic_ratio=ic.positive_ic_ratio,
            sample_count=len(seg_dates),
        ))

    if len(segments) < 2:
        return TimePerturbationResult(segments=segments, ic_stability=1.0, verdict="unstable")

    ic_values = [s.ic_mean for s in segments]
    mean_ic = float(np.mean(ic_values))
    std_ic = float(np.std(ic_values, ddof=1))
    stability = float(std_ic / abs(mean_ic)) if abs(mean_ic) > 1e-10 else 1.0

    verdict = "stable" if stability < 1.0 else "unstable"
    return TimePerturbationResult(segments=segments, ic_stability=stability, verdict=verdict)


@dataclass
class MissingSensitivityResult:
    """缺失敏感度结果。"""

    dropout_ratio: float  # 随机丢弃比例
    ic_mean: float
    icir: float
    coverage: float


@dataclass
class MissingSensitivityBatch:
    """缺失敏感度批量结果。"""

    results: list[MissingSensitivityResult]
    ic_decay_ratio: float  # 最大丢弃下 IC 衰减比例
    verdict: str  # robust / sensitive


def perturb_missing_sensitivity(
    *,
    features: pd.DataFrame,
    targets: pd.DataFrame,
    dropout_ratios: list[float] | None = None,
    random_seed: int = 42,
) -> MissingSensitivityBatch:
    """缺失敏感度测试：随机丢弃因子值后重新计算 IC。"""
    dropout_ratios = dropout_ratios or [0.0, 0.05, 0.10, 0.20]
    results: list[MissingSensitivityResult] = []
    rng = np.random.RandomState(random_seed)

    baseline_ic = 0.0
    for ratio in dropout_ratios:
        if ratio == 0.0:
            perturbed = features.copy()
        else:
            mask = rng.random(features.shape) < ratio
            perturbed = features.mask(mask)

        ic = compute_rank_ic(perturbed, targets)
        coverage = float(perturbed.notna().sum().sum() / perturbed.size) if perturbed.size > 0 else 0.0
        results.append(MissingSensitivityResult(
            dropout_ratio=ratio,
            ic_mean=ic.rank_ic_mean,
            icir=ic.icir,
            coverage=coverage,
        ))
        if ratio == 0.0:
            baseline_ic = abs(ic.rank_ic_mean) if ic.rank_ic_mean != 0 else 1e-10

    # 计算最大衰减比例
    max_drop_ic = min(abs(r.ic_mean) for r in results)
    decay_ratio = float(1.0 - max_drop_ic / baseline_ic) if baseline_ic > 1e-10 else 1.0

    verdict = "robust" if decay_ratio < 0.50 else "sensitive"
    return MissingSensitivityBatch(results=results, ic_decay_ratio=decay_ratio, verdict=verdict)


@dataclass
class StressTestSummary:
    """压力测试汇总。"""

    parameter_results: list[ParameterPerturbationResult]
    time_result: TimePerturbationResult | None
    missing_result: MissingSensitivityBatch | None
    overall_verdict: str  # stable / unstable / cliff_drop
    failure_reasons: list[str]


def run_stress_test(
    *,
    features: pd.DataFrame,
    targets: pd.DataFrame,
    parameter_perturbations: list[dict[str, Any]] | None = None,
    eval_fn_factory: Callable[[pd.DataFrame, pd.DataFrame], Callable[[float], ICMetrics]] | None = None,
    segment_size: int = 60,
    dropout_ratios: list[float] | None = None,
) -> StressTestSummary:
    """执行完整压力测试。

    Args:
        features: 验证期因子值
        targets: 验证期目标收益
        parameter_perturbations: 参数扰动配置列表，每项含 {name, baseline_value, ratios?}
        eval_fn_factory: 给定 features/targets 返回 eval_fn（参数值→ICMetrics）的工厂
        segment_size: 时间段切分大小
        dropout_ratios: 缺失敏感度丢弃比例
    """
    param_results: list[ParameterPerturbationResult] = []
    failure_reasons: list[str] = []

    # 参数扰动
    if parameter_perturbations and eval_fn_factory:
        base_eval_fn = eval_fn_factory(features, targets)
        for cfg in parameter_perturbations:
            result = perturb_parameter(
                param_name=cfg["name"],
                baseline_value=cfg["baseline_value"],
                ratios=cfg.get("ratios"),
                eval_fn=base_eval_fn,
            )
            param_results.append(result)
            if result.verdict == "cliff_drop":
                failure_reasons.append(
                    f"param_{result.param_name}_cliff_drop:IC 断崖式下跌"
                )
            elif result.verdict == "unstable":
                failure_reasons.append(
                    f"param_{result.param_name}_unstable:参数扰动不稳定"
                )

    # 时间段扰动
    time_result = perturb_time_segments(
        features=features,
        targets=targets,
        segment_size=segment_size,
    )
    if time_result.verdict == "unstable":
        failure_reasons.append("time_segment_unstable:时间段 IC 不稳定")

    # 缺失敏感度
    missing_result = perturb_missing_sensitivity(
        features=features,
        targets=targets,
        dropout_ratios=dropout_ratios,
    )
    if missing_result.verdict == "sensitive":
        failure_reasons.append("missing_sensitive:缺失敏感度过高")

    # 总体判定
    if any(r.verdict == "cliff_drop" for r in param_results):
        overall = "cliff_drop"
    elif failure_reasons:
        overall = "unstable"
    else:
        overall = "stable"

    return StressTestSummary(
        parameter_results=param_results,
        time_result=time_result,
        missing_result=missing_result,
        overall_verdict=overall,
        failure_reasons=failure_reasons,
    )


__all__ = [
    "DEFAULT_PERTURBATION_RATIOS",
    "PERTURBATION_THRESHOLDS",
    "PerturbationPoint",
    "ParameterPerturbationResult",
    "perturb_parameter",
    "TimeSegmentResult",
    "TimePerturbationResult",
    "perturb_time_segments",
    "MissingSensitivityResult",
    "MissingSensitivityBatch",
    "perturb_missing_sensitivity",
    "StressTestSummary",
    "run_stress_test",
]
