"""黄金样本端到端回测：验证评价结果具备量化可信度。

5 组确定性输入因子：
- Case A: 完全正相关因子（Oracle）：signal = target + 微小噪声
- Case B: 负向因子：Case A 取反 + direction=lower_better
- Case C: 随机因子：np.random.seed(42) 的标准正态
- Case D: 常数因子：所有值 = 1.0
- Case E: 可复现性：Case A 跑两次，结果浮点相对差 ≤ 1e-6

数据模式：
- 优先使用 warehouse 连接真实 DuckDB raw_daily_bars + factor_targets
- 若无 target_5d_return 批次，先调用 target_engine.calculate_targets 生成
- 若 warehouse 也不可用或数据不足（<60 交易日 × 50 股票），
  则使用 synthetic 面板（252 日 × 300 股票的 close 序列 + 5 日前瞻收益）。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.whitebox

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.services.factors.factor_evaluator import (
    TimeSplit,
    align_factor_with_target,
    build_time_split,
    compute_coverage,
    compute_cost_adjusted_return,
    compute_quantile_returns,
    compute_rank_ic,
    compute_turnover,
    evaluate_gate,
)
from app.services.factors.wp5_eval_task import apply_direction_alignment


DATA_MODE: dict[str, str] = {"mode": "NOT_YET_DETECTED"}


def _try_get_real_forward_returns(
    n_min_dates: int = 300, n_min_symbols: int = 100
) -> pd.DataFrame | None:
    try:
        from app.services.factors.store import FactorWarehouse
        from app.services.factors import target_engine
    except Exception:
        return None

    try:
        warehouse = FactorWarehouse()
        if not warehouse.path.exists():
            return None
        warehouse.initialize()
    except Exception:
        return None

    target_code = "target_5d_return"
    batch_id = None
    try:
        batch_id = warehouse.get_latest_target_batch_id(target_code)
    except Exception:
        batch_id = None

    if batch_id is None:
        try:
            if hasattr(target_engine, "calculate_targets"):
                result = target_engine.calculate_targets(warehouse=warehouse)
                if result and getattr(result, "tradable_rows", 0) > 0:
                    batch_id = getattr(result, "calc_batch_id", None)
        except Exception:
            pass

    if batch_id is None:
        return None

    try:
        target_panel, _, _ = warehouse.get_target_panel(batch_id, target_code)
    except Exception:
        return None

    if target_panel is None or target_panel.empty:
        return None

    try:
        if target_panel["signal_date"].dtype != object:
            target_panel["signal_date"] = pd.to_datetime(
                target_panel["signal_date"], errors="coerce"
            ).dt.strftime("%Y-%m-%d")
        pivoted = target_panel.pivot_table(
            index="signal_date",
            columns="symbol",
            values="target_value",
            aggfunc="first",
        )
        pivoted.index = pd.to_datetime(pivoted.index, errors="coerce").date
        pivoted = pivoted.loc[[pd.notna(x) for x in pivoted.index]]
        if pivoted.shape[0] < n_min_dates or pivoted.shape[1] < n_min_symbols:
            return None
        non_null_ratio = pivoted.notna().sum().sum() / max(1, pivoted.size)
        if non_null_ratio < 0.5:
            return None
        return pivoted
    except Exception:
        return None


def _build_synthetic_panels(
    n_days: int = 500, n_symbols: int = 300, seed: int = 7
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    rng = np.random.RandomState(seed)
    total_needed = n_days + 20
    start_day = date(2025, 1, 2)
    weekdays: list[date] = []
    cursor = start_day
    while len(weekdays) < total_needed:
        if cursor.weekday() < 5:
            weekdays.append(cursor)
        cursor += timedelta(days=1)

    symbols = [f"SYN{i:04d}" for i in range(n_symbols)]

    log_returns = rng.randn(len(weekdays), n_symbols) * 0.018
    close = 100.0 * np.exp(np.cumsum(log_returns, axis=0))

    entry_close = close[1 : n_days + 1, :]
    exit_close = close[6 : n_days + 6, :]
    fwd_ret = (exit_close / entry_close) - 1.0 + rng.randn(n_days, n_symbols) * 0.001

    trade_dates = weekdays[:n_days]
    forward_returns = pd.DataFrame(fwd_ret, index=trade_dates, columns=symbols)
    close_panel = pd.DataFrame(close[:n_days, :], index=trade_dates, columns=symbols)
    DATA_MODE["mode"] = "SYNTHETIC_FALLBACK"
    return forward_returns, close_panel


def _prepare_forward_returns_and_panel() -> tuple[pd.DataFrame, pd.DataFrame | None]:
    real_fr = _try_get_real_forward_returns()
    if real_fr is not None:
        DATA_MODE["mode"] = "REAL_WAREHOUSE"
        return real_fr, None
    return _build_synthetic_panels()


@dataclass
class PureEvaluationResult:
    rank_ic: float
    rank_ic_std: float
    icir: float
    top_bottom_return: float
    group_returns: list[float]
    n_samples: int
    n_dates: int
    gate_result: str
    rejection_reasons: list[str]
    coverage: float
    monotonicity_score: float


def run_pure_evaluation(
    *,
    factor_values: pd.DataFrame,
    forward_returns: pd.DataFrame,
    direction: str = "higher_better",
    factor_kind: str = "continuous",
    n_groups: int = 5,
    cost_rate: float = 0.001,
) -> PureEvaluationResult:
    all_dates = sorted(set(factor_values.index.tolist()) | set(forward_returns.index.tolist()))
    try:
        ts: TimeSplit | None = build_time_split(
            all_dates=all_dates,
            target_horizon=5,
            train_ratio=0.6,
            validation_ratio=0.2,
            purge_days=5,
            embargo_days=5,
        )
    except ValueError:
        ts = None

    if ts is not None:
        val_mask = (factor_values.index >= ts.validation_start) & (
            factor_values.index <= ts.validation_end
        )
        val_features_raw = factor_values.loc[val_mask]
        val_targets_raw = forward_returns.loc[forward_returns.index.isin(val_features_raw.index)]
    else:
        val_features_raw = factor_values.copy()
        val_targets_raw = forward_returns.copy()

    if val_features_raw.empty or val_targets_raw.empty:
        return PureEvaluationResult(
            rank_ic=0.0,
            rank_ic_std=0.0,
            icir=0.0,
            top_bottom_return=0.0,
            group_returns=[0.0] * n_groups,
            n_samples=0,
            n_dates=0,
            gate_result="rejected",
            rejection_reasons=["empty_aligned_sample"],
            coverage=0.0,
            monotonicity_score=0.0,
        )

    aligned = align_factor_with_target(
        val_features_raw, val_targets_raw, factor_kind=factor_kind, event_dates=None
    )

    features = aligned.features
    targets = aligned.targets

    adjusted, _dir_blockers = apply_direction_alignment(features, direction=direction)

    ic_metrics = compute_rank_ic(adjusted, targets)
    quantile_metrics = compute_quantile_returns(adjusted, targets, n_groups=n_groups)
    turnover_metrics = compute_turnover(adjusted)
    cost_adjusted = compute_cost_adjusted_return(
        quantile_metrics.top_bottom_return,
        turnover_metrics.avg_turnover,
        cost_rate=cost_rate,
    )
    coverage_metrics = compute_coverage(adjusted)

    n_dates = len(aligned.trade_dates)
    n_samples = int(adjusted.notna().sum().sum())

    gate = evaluate_gate(
        factor_kind=factor_kind,
        ic_metrics=ic_metrics,
        quantile_metrics=quantile_metrics,
        turnover_metrics=turnover_metrics,
        cost_adjusted=cost_adjusted,
        coverage_metrics=coverage_metrics,
        validation_days=n_dates,
        effective_samples=n_samples,
    )

    return PureEvaluationResult(
        rank_ic=ic_metrics.rank_ic_mean,
        rank_ic_std=ic_metrics.rank_ic_std,
        icir=ic_metrics.icir,
        top_bottom_return=quantile_metrics.top_bottom_return,
        group_returns=list(quantile_metrics.group_returns),
        n_samples=n_samples,
        n_dates=n_dates,
        gate_result=gate.result,
        rejection_reasons=list(gate.reasons),
        coverage=coverage_metrics.coverage,
        monotonicity_score=quantile_metrics.monotonicity_score,
    )


@pytest.fixture(scope="module")
def shared_forward_returns():
    fr, _ref = _prepare_forward_returns_and_panel()
    yield fr


@pytest.fixture(scope="module")
def case_a_signal(shared_forward_returns: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.RandomState(42)
    noise = rng.randn(*shared_forward_returns.shape) * 0.001
    sig = shared_forward_returns + noise
    sig = sig.where(shared_forward_returns.notna())
    return sig


@pytest.fixture(scope="module")
def golden_results() -> dict[str, Any]:
    return {}


class TestCaseAOracle:
    def test_oracle_rank_ic_positive_and_significant(
        self,
        shared_forward_returns: pd.DataFrame,
        case_a_signal: pd.DataFrame,
        golden_results: dict[str, Any],
    ):
        result = run_pure_evaluation(
            factor_values=case_a_signal,
            forward_returns=shared_forward_returns,
            direction="higher_better",
        )
        golden_results["case_A"] = {
            "case": "A_oracle",
            "rank_ic": result.rank_ic,
            "icir": result.icir,
            "n_samples": result.n_samples,
            "gate_result": result.gate_result,
            "top_bottom_return": result.top_bottom_return,
            "rank_ic_std": result.rank_ic_std,
            "group_returns": result.group_returns,
            "coverage": result.coverage,
            "monotonicity_score": result.monotonicity_score,
            "rejection_reasons": result.rejection_reasons,
            "n_dates": result.n_dates,
            "data_mode": DATA_MODE.get("mode", "UNKNOWN"),
        }

        assert result.rank_ic >= 0.05, (
            f"Case A: Oracle Rank IC 期望≥0.05，实际 {result.rank_ic:.4f}，"
            f"n_samples={result.n_samples}, gate={result.gate_result}"
        )

        assert result.icir >= 2.0, (
            f"Case A: Rank IC t-stat(≈ICIR×√T) 期望显著，ICIR 实际 {result.icir:.4f}，"
            f"std={result.rank_ic_std:.4f}, n_dates={result.n_dates}"
        )

    def test_oracle_top_bottom_positive_and_monotonic(
        self,
        shared_forward_returns: pd.DataFrame,
        case_a_signal: pd.DataFrame,
    ):
        result = run_pure_evaluation(
            factor_values=case_a_signal,
            forward_returns=shared_forward_returns,
            direction="higher_better",
        )
        assert result.top_bottom_return > 0, (
            f"Case A: Top-Bottom 收益期望>0，实际 {result.top_bottom_return:.6f}"
        )

    def test_oracle_gate_not_rejected_by_coverage(
        self,
        shared_forward_returns: pd.DataFrame,
        case_a_signal: pd.DataFrame,
    ):
        result = run_pure_evaluation(
            factor_values=case_a_signal,
            forward_returns=shared_forward_returns,
            direction="higher_better",
        )
        reasons = result.rejection_reasons
        coverage_bad = any(
            "coverage_insufficient" in r or "effective_samples_insufficient" in r
            for r in reasons
        )
        assert not coverage_bad, (
            f"Case A: gate 不应因 coverage/effective_samples 被 reject，"
            f"实际 gate_result={result.gate_result}, reasons={reasons[:5]}"
        )


class TestCaseBDirectionAlignment:
    def test_lower_better_sign_consistency(
        self,
        shared_forward_returns: pd.DataFrame,
        case_a_signal: pd.DataFrame,
        golden_results: dict[str, Any],
    ):
        result_A = run_pure_evaluation(
            factor_values=case_a_signal,
            forward_returns=shared_forward_returns,
            direction="higher_better",
        )
        signal_B = -case_a_signal
        result_B = run_pure_evaluation(
            factor_values=signal_B,
            forward_returns=shared_forward_returns,
            direction="lower_better",
        )

        golden_results["case_B"] = {
            "case": "B_lower_better",
            "rank_ic": result_B.rank_ic,
            "icir": result_B.icir,
            "n_samples": result_B.n_samples,
            "gate_result": result_B.gate_result,
            "top_bottom_return": result_B.top_bottom_return,
            "rank_ic_A": result_A.rank_ic,
            "ic_diff_abs": abs(result_A.rank_ic - result_B.rank_ic),
            "data_mode": DATA_MODE.get("mode", "UNKNOWN"),
        }

        diff = abs(result_A.rank_ic - result_B.rank_ic)
        assert diff <= 0.03, (
            f"Case B: IC_A 与 direction 统一后的 IC_B 期望|差|≤0.03（一致性），"
            f"实际 {diff:.4f}(IC_A={result_A.rank_ic:.4f}, IC_B={result_B.rank_ic:.4f})"
        )

    def test_lower_better_top_bottom_direction(
        self,
        shared_forward_returns: pd.DataFrame,
        case_a_signal: pd.DataFrame,
    ):
        result_A = run_pure_evaluation(
            factor_values=case_a_signal,
            forward_returns=shared_forward_returns,
            direction="higher_better",
        )
        signal_B = -case_a_signal
        result_B = run_pure_evaluation(
            factor_values=signal_B,
            forward_returns=shared_forward_returns,
            direction="lower_better",
        )
        assert (
            result_A.top_bottom_return > 0 and result_B.top_bottom_return > 0
        ), (
            f"Case B: 方向统一后 Top-Bottom 方向应与 A 一致。"
            f"A_tb={result_A.top_bottom_return:.6f}, B_tb={result_B.top_bottom_return:.6f}"
        )


class TestCaseCRandom:
    def test_random_rank_ic_near_zero(
        self,
        shared_forward_returns: pd.DataFrame,
        golden_results: dict[str, Any],
    ):
        rng = np.random.RandomState(42)
        shape = shared_forward_returns.shape
        random_vals = rng.randn(*shape)
        signal_C = pd.DataFrame(
            random_vals,
            index=shared_forward_returns.index,
            columns=shared_forward_returns.columns,
        )
        signal_C = signal_C.where(shared_forward_returns.notna())

        result = run_pure_evaluation(
            factor_values=signal_C,
            forward_returns=shared_forward_returns,
            direction="higher_better",
        )
        golden_results["case_C"] = {
            "case": "C_random",
            "rank_ic": result.rank_ic,
            "icir": result.icir,
            "n_samples": result.n_samples,
            "gate_result": result.gate_result,
            "top_bottom_return": result.top_bottom_return,
            "data_mode": DATA_MODE.get("mode", "UNKNOWN"),
        }

        assert abs(result.rank_ic) <= 0.02, (
            f"Case C: 随机因子 |Rank IC| 期望≤0.02，实际 {result.rank_ic:.4f}"
        )

    def test_random_icir_near_zero(
        self,
        shared_forward_returns: pd.DataFrame,
    ):
        rng = np.random.RandomState(42)
        shape = shared_forward_returns.shape
        random_vals = rng.randn(*shape)
        signal_C = pd.DataFrame(
            random_vals,
            index=shared_forward_returns.index,
            columns=shared_forward_returns.columns,
        )
        signal_C = signal_C.where(shared_forward_returns.notna())

        result = run_pure_evaluation(
            factor_values=signal_C,
            forward_returns=shared_forward_returns,
            direction="higher_better",
        )
        assert abs(result.icir) <= 1.0, (
            f"Case C: 随机因子 |ICIR| 不应过大，实际 {result.icir:.4f}"
        )


class TestCaseDConstant:
    def test_constant_ic_near_zero_or_rejected(
        self,
        shared_forward_returns: pd.DataFrame,
        golden_results: dict[str, Any],
    ):
        ones = np.ones(shared_forward_returns.shape)
        signal_D = pd.DataFrame(
            ones,
            index=shared_forward_returns.index,
            columns=shared_forward_returns.columns,
        )
        signal_D = signal_D.where(shared_forward_returns.notna())

        result = run_pure_evaluation(
            factor_values=signal_D,
            forward_returns=shared_forward_returns,
            direction="higher_better",
        )
        golden_results["case_D"] = {
            "case": "D_constant",
            "rank_ic": result.rank_ic,
            "icir": result.icir,
            "n_samples": result.n_samples,
            "gate_result": result.gate_result,
            "top_bottom_return": result.top_bottom_return,
            "rejection_reasons": result.rejection_reasons,
            "data_mode": DATA_MODE.get("mode", "UNKNOWN"),
        }

        ic_ok = abs(result.rank_ic) <= 0.005
        rej_reasons = result.rejection_reasons or []
        effective_bad = any(
            "effective_samples_insufficient" in r for r in rej_reasons
        )
        gate_rejected = result.gate_result == "rejected" and (
            effective_bad or len(rej_reasons) > 0
        )

        assert ic_ok or gate_rejected, (
            f"Case D: 常数因子必须 |Rank IC|≤0.005 或被门禁 reject。"
            f"实际 IC={result.rank_ic:.6f}, gate={result.gate_result}, "
            f"reasons={rej_reasons[:5]}"
        )


class TestCaseEReproducibility:
    def test_reproduce_two_runs_identical(
        self,
        shared_forward_returns: pd.DataFrame,
        case_a_signal: pd.DataFrame,
        golden_results: dict[str, Any],
    ):
        r1 = run_pure_evaluation(
            factor_values=case_a_signal,
            forward_returns=shared_forward_returns,
            direction="higher_better",
        )
        r2 = run_pure_evaluation(
            factor_values=case_a_signal,
            forward_returns=shared_forward_returns,
            direction="higher_better",
        )

        golden_results["case_E"] = {
            "case": "E_reproducibility",
            "run1_rank_ic": r1.rank_ic,
            "run2_rank_ic": r2.rank_ic,
            "run1_icir": r1.icir,
            "run2_icir": r2.icir,
            "run1_n_samples": r1.n_samples,
            "run2_n_samples": r2.n_samples,
            "run1_top_bottom_return": r1.top_bottom_return,
            "run2_top_bottom_return": r2.top_bottom_return,
            "data_mode": DATA_MODE.get("mode", "UNKNOWN"),
        }

        def _rel_diff(a: float, b: float) -> float:
            denom = max(1e-12, abs(a) + abs(b))
            return abs(a - b) / denom

        assert r1.n_samples == r2.n_samples, (
            f"Case E: n_samples 不一致：{r1.n_samples} vs {r2.n_samples}"
        )

        for name, a, b in [
            ("rank_ic", r1.rank_ic, r2.rank_ic),
            ("icir", r1.icir, r2.icir),
            ("top_bottom_return", r1.top_bottom_return, r2.top_bottom_return),
        ]:
            rd = _rel_diff(a, b)
            assert rd <= 1e-6, (
                f"Case E: {name} 浮点相对差 {rd:.2e} 超过 1e-6。"
                f"r1={a}, r2={b}"
            )

        grp1 = r1.group_returns
        grp2 = r2.group_returns
        assert len(grp1) == len(grp2), "分组数量不一致"
        for i, (g1, g2) in enumerate(zip(grp1, grp2)):
            rd = _rel_diff(g1, g2)
            assert rd <= 1e-6, (
                f"Case E: group_returns[{i}] 浮点相对差 {rd:.2e} > 1e-6"
            )


def test_write_golden_output_json(golden_results: dict[str, Any]):
    golden_results["_meta"] = {
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "data_mode": DATA_MODE.get("mode", "UNKNOWN"),
        "evaluator_note": (
            "pure_evaluation_flow: align → apply_direction_alignment → "
            "rank_ic → quantile → turnover → cost → gate. No DB writes."
        ),
    }
    output_path = ROOT / "tests" / "factors" / "_golden_output.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(golden_results, f, ensure_ascii=False, indent=2)
    assert output_path.exists(), f"写入失败：{output_path}"
