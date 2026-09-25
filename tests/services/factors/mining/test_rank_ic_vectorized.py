"""`compute_rank_ic` 向量化优化 · 等价性黄金测试（2026-09-23）。

**优化动机**：py-spy 抓栈证明「挖掘 worker 长时间无推进」不是死锁，而是
`compute_rank_ic` 的**逐日 pandas 循环**太慢 —— 每天都要
`loc[date].dropna()` ×2 + `index.intersection` + `rank()` ×2 + `std()` ×2 + `corr()`。
生产规模（约 5,500 股 × 389 交易日）单因子评估数秒，100 个体/代 ⇒ 数分钟/代，
表现为「提交后长时间 0/20」。

**本测试的作用**：先把**现有行为**固化为黄金参考（参考实现内联在此），
优化实现后必须逐元素等价 —— 避免"提速但算错"。

覆盖边界：
- 常规随机面板（含 NaN）
- 常量截面（std=0 → 该日 IC 记 0）
- 某日共同有效标的 < 5 → 记 0
- 全 NaN 列 / 空面板
- `positive_ic_ratio` / `icir` 聚合口径
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.services.factors.factor_evaluator import compute_rank_ic


def _reference_rank_ic_series(
    features: pd.DataFrame, targets: pd.DataFrame
) -> list[float]:
    """**优化前的逐日实现**（原封不动搬到这里作为黄金参考）。"""
    ic_series: list[float] = []
    for trade_date in features.index:
        fv = features.loc[trade_date].dropna()
        tv = targets.loc[trade_date].dropna()
        common = fv.index.intersection(tv.index)
        if len(common) < 5:
            ic_series.append(0.0)
            continue
        fv_ranked = fv.loc[common].rank()
        tv_ranked = tv.loc[common].rank()
        if fv_ranked.std() == 0 or tv_ranked.std() == 0:
            ic_series.append(0.0)
            continue
        corr = float(fv_ranked.corr(tv_ranked))
        ic_series.append(0.0 if math.isnan(corr) else corr)
    return ic_series


def _frame(seed: int, *, days: int = 40, symbols: int = 60,
           nan_ratio: float = 0.1) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-05", periods=days, freq="B").date
    cols = [f"{i:06d}" for i in range(symbols)]
    f = rng.normal(size=(days, symbols))
    t = f * 0.4 + rng.normal(size=(days, symbols))  # 与 f 相关，避免恒 0
    mask = rng.random(size=(days, symbols)) < nan_ratio
    f[mask] = np.nan
    t[mask] = np.nan
    return pd.DataFrame(f, index=idx, columns=cols), pd.DataFrame(t, index=idx, columns=cols)


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_matches_reference_on_random_panels(seed):
    """随机面板：ic_series 与聚合指标必须与旧实现逐元素等价。"""
    feats, targets = _frame(seed)
    expected = _reference_rank_ic_series(feats, targets)
    got = compute_rank_ic(feats, targets)

    assert len(got.ic_series) == len(expected)
    assert got.ic_series == pytest.approx(expected, abs=1e-12)
    assert got.rank_ic_mean == pytest.approx(float(np.mean(expected)), abs=1e-12)


def test_constant_cross_section_scores_zero():
    """常量截面（组内 std=0）→ 该日 IC 记 0（与旧实现一致）。"""
    idx = pd.date_range("2026-01-05", periods=4, freq="B").date
    cols = [f"{i:03d}" for i in range(10)]
    feats = pd.DataFrame(np.full((4, 10), 5.0), index=idx, columns=cols)
    targets = pd.DataFrame(np.random.default_rng(3).normal(size=(4, 10)),
                           index=idx, columns=cols)

    expected = _reference_rank_ic_series(feats, targets)
    got = compute_rank_ic(feats, targets)
    assert expected == [0.0] * 4
    assert got.ic_series == pytest.approx(expected, abs=1e-12)


def test_too_few_common_symbols_scores_zero():
    """共同有效标的 < 5 → 记 0（与旧实现一致）。"""
    idx = pd.date_range("2026-01-05", periods=3, freq="B").date
    cols = [f"{i:03d}" for i in range(10)]
    feats = pd.DataFrame(np.random.default_rng(5).normal(size=(3, 10)),
                         index=idx, columns=cols)
    targets = pd.DataFrame(np.nan, index=idx, columns=cols)
    targets.iloc[:, :3] = 1.0        # 只有 3 个标的有值

    expected = _reference_rank_ic_series(feats, targets)
    got = compute_rank_ic(feats, targets)
    assert expected == [0.0] * 3
    assert got.ic_series == pytest.approx(expected, abs=1e-12)


def test_empty_panel_returns_zeros():
    empty = pd.DataFrame()
    got = compute_rank_ic(empty, empty)
    assert got.rank_ic_mean == 0.0
    assert got.icir == 0.0
    assert got.ic_series == []


def test_aggregate_metrics_match_reference(seed=None):
    """聚合口径（mean / median / std(ddof=1) / icir / positive_ratio）与旧实现一致。"""
    feats, targets = _frame(99, days=60, symbols=80, nan_ratio=0.15)
    expected = np.array(_reference_rank_ic_series(feats, targets), dtype=float)
    got = compute_rank_ic(feats, targets)

    assert got.rank_ic_mean == pytest.approx(float(np.mean(expected)), abs=1e-12)
    assert got.rank_ic_median == pytest.approx(float(np.median(expected)), abs=1e-12)
    assert got.rank_ic_std == pytest.approx(float(np.std(expected, ddof=1)), abs=1e-12)
    assert got.positive_ic_ratio == pytest.approx(float(np.mean(expected > 0)), abs=1e-12)
    exp_icir = float(np.mean(expected) / np.std(expected, ddof=1)) \
        if np.std(expected, ddof=1) > 1e-10 else 0.0
    assert got.icir == pytest.approx(exp_icir, abs=1e-12)
