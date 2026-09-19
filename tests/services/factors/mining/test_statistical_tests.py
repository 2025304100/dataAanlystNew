# -*- coding: utf-8 -*-
"""T35 统计层 8 方法单元测试。

规格来源：
- 设计文档 v2.0 §7.9（8 方法实现表 + §7.9.4 月频降级）
- 开发需求文档 §3.2 第二层统计层（口径与阈值）
- 开发文档 §3.17（实现细则：Bonferroni/FDR 自研唯一路径、Walk-Forward
  只做方向一致性计数、月频跳过 Bootstrap/置换、存储进 metrics_json.stats）

纯计算测试：不触 DB；Bootstrap/置换/DSR 全部注入固定种子 rng，可复现。
"""
from __future__ import annotations

import dataclasses
import math
import random

import numpy as np
import pytest

from app.core.db_numeric import clean_json_tree
from app.services.factors.mining.statistical_tests import (
    FDR_ALPHA,
    N_RESAMPLES,
    StatResult,
    bh_adjust,
    bonferroni_adjust,
    bootstrap_icir_ci,
    compute_stats,
    decay_ratio,
    deflated_sharpe,
    permutation_icir_pvalue,
    walk_forward,
)


# ══════════════════════════════════════════════════════════
# Bonferroni / FDR（自研，唯一实现路径）
# ══════════════════════════════════════════════════════════

class TestBonferroni:
    def test_basic_scaling(self):
        out = bonferroni_adjust([0.01, 0.2], total_trials=10)
        assert out[0] == pytest.approx(0.1)
        assert out[1] == 1.0  # 0.2*10=2 → 封顶 1

    def test_caps_at_one_never_negative(self):
        out = bonferroni_adjust([0.0, 0.999, 1.0], total_trials=2000)
        assert out[0] == 0.0
        assert out[1] == 1.0
        assert out[2] == 1.0


class TestBHAdjust:
    def test_known_vector(self):
        # 经典 BH 手算：p=[0.01,0.04,0.03,0.005]，n=4
        # 排序后 raw q = p*n/rank = [0.02,0.02,0.04,0.04]，
        # 自后向前单调化（running min）→ 原序 q = [0.02, 0.04, 0.04, 0.02]
        out = bh_adjust([0.01, 0.04, 0.03, 0.005])
        assert out == pytest.approx([0.02, 0.04, 0.04, 0.02])

    def test_monotone_in_p(self):
        p = [0.001, 0.005, 0.01, 0.02, 0.04, 0.08, 0.2, 0.5]
        q = bh_adjust(p)
        for a, b in zip(q, q[1:]):
            assert a <= b + 1e-12  # q 随 p 单调不减

    def test_q_never_below_p(self):
        p = [0.03, 0.2, 0.6]
        q = bh_adjust(p)
        for pi, qi in zip(p, q):
            assert qi >= pi - 1e-12

    def test_single_value_matches_bonferroni(self):
        p = [0.007]
        assert bh_adjust(p)[0] == pytest.approx(0.007 * 1)  # rank=1
        # 单因子在 total_trials=n 下两法一致：p*n/rank(=1) = p*n
        assert bonferroni_adjust(p, total_trials=5)[0] == pytest.approx(0.035)

    def test_alpha_constants(self):
        # 需求 §3.2：Bonferroni 校正后 <0.001、FDR q<0.1
        assert FDR_ALPHA == 0.1


# ══════════════════════════════════════════════════════════
# t 检验
# ══════════════════════════════════════════════════════════

class TestTStat:
    def test_significant_signal(self):
        rng = np.random.default_rng(42)
        ic = 0.05 + rng.normal(0, 0.01, 120)   # IC 均值显著非零
        result = compute_stats(
            ic_series=ic, train_icir=1.0, test_icir=0.9,
            total_trials=10, frequency="daily", rng=random.Random(1))
        assert result.p_value < 0.05

    def test_noise_not_significant(self):
        rng = np.random.default_rng(7)
        ic = rng.normal(0, 0.05, 120)          # 纯噪声
        result = compute_stats(
            ic_series=ic, train_icir=0.1, test_icir=-0.1,
            total_trials=10, frequency="daily", rng=random.Random(1))
        assert result.p_value > 0.05


# ══════════════════════════════════════════════════════════
# Bootstrap ICIR 置信区间
# ══════════════════════════════════════════════════════════

class TestBootstrap:
    def test_ci_contains_point_estimate_and_ordered(self):
        rng = np.random.default_rng(3)
        ic = 0.05 + rng.normal(0, 0.02, 120)
        lo, hi = bootstrap_icir_ci(ic, rng=random.Random(11))
        point = float(np.mean(ic) / np.std(ic, ddof=1))
        assert lo < point < hi
        assert lo <= hi

    def test_fixed_seed_reproducible(self):
        ic = list(np.linspace(0.01, 0.09, 60))
        a = bootstrap_icir_ci(ic, rng=random.Random(2026))
        b = bootstrap_icir_ci(ic, rng=random.Random(2026))
        assert a == b

    def test_resample_count_constant(self):
        assert N_RESAMPLES == 1000


# ══════════════════════════════════════════════════════════
# 置换检验（面板打乱重算 ICIR 经验分布）
# ══════════════════════════════════════════════════════════

def _panel_pair(seed: int, t: int = 60, n: int = 50,
                signal: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """构造 (因子面板, 收益面板)：signal>0 时因子含可预测成分。"""
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.02, (t, n))
    factor = signal * ret + rng.normal(0, 0.02, (t, n))
    return factor, ret


class TestPermutation:
    def test_strong_signal_low_pvalue(self):
        factor, ret = _panel_pair(5, signal=2.0)
        p = permutation_icir_pvalue(
            factor, ret, n_permutations=300, rng=random.Random(9))
        assert p is not None and p < 0.05

    def test_noise_high_pvalue(self):
        factor, ret = _panel_pair(6, signal=0.0)
        p = permutation_icir_pvalue(
            factor, ret, n_permutations=300, rng=random.Random(9))
        assert p is not None and p > 0.1

    def test_fixed_seed_reproducible(self):
        factor, ret = _panel_pair(5, signal=1.5)
        a = permutation_icir_pvalue(
            factor, ret, n_permutations=100, rng=random.Random(3))
        b = permutation_icir_pvalue(
            factor, ret, n_permutations=100, rng=random.Random(3))
        assert a == b


# ══════════════════════════════════════════════════════════
# Deflated Sharpe（total_trials + 偏度/峰度校正）
# ══════════════════════════════════════════════════════════

class TestDeflatedSharpe:
    def test_output_probability_range(self):
        rng = np.random.default_rng(4)
        ic = 0.05 + rng.normal(0, 0.02, 120)
        dsr = deflated_sharpe(ic, observed_icir=2.5, total_trials=100,
                              rng=random.Random(5))
        assert 0.0 <= dsr <= 1.0

    def test_more_trials_more_penalty(self):
        # observed_icir 取边际水平并注入固定 sr_variance，让 E[max SR] 随
        # total_trials 的移动可分辨（observed 过强时 Φ 两端均饱和为 1.0，
        # 校正效应被淹没）；sr_variance 固定也隔离了 Bootstrap 估计波动。
        ic = list(0.05 + np.random.default_rng(4).normal(0, 0.02, 120))
        dsr_small = deflated_sharpe(ic, observed_icir=0.4, total_trials=10,
                                    sr_variance=0.01, rng=random.Random(5))
        dsr_huge = deflated_sharpe(ic, observed_icir=0.4, total_trials=5000,
                                   sr_variance=0.01, rng=random.Random(5))
        assert dsr_huge < dsr_small          # 试验次数越多，选择偏差校正越重
        assert dsr_small - dsr_huge > 0.1    # 防饱和：两端必须落在区分区

    def test_quality_signal_high_noise_low(self):
        good = list(0.05 + np.random.default_rng(8).normal(0, 0.01, 120))
        noise = list(np.random.default_rng(9).normal(0, 0.05, 120))
        dsr_good = deflated_sharpe(good, observed_icir=4.0, total_trials=50,
                                   rng=random.Random(5))
        dsr_noise = deflated_sharpe(noise, observed_icir=0.2, total_trials=50,
                                    rng=random.Random(5))
        assert dsr_good > 0.9
        assert dsr_noise < 0.5


# ══════════════════════════════════════════════════════════
# 衰减率
# ══════════════════════════════════════════════════════════

class TestDecayRatio:
    def test_basic_ratio(self):
        assert decay_ratio(0.8, 0.4) == pytest.approx(0.5)

    def test_invalid_train_returns_nan(self):
        assert math.isnan(decay_ratio(0.0, 0.4))
        assert math.isnan(decay_ratio(None, 0.4))
        assert math.isnan(decay_ratio(float("nan"), 0.4))

    def test_negative_test_icir_negative_ratio(self):
        # test 段反向 → 负衰减率（严重过拟合/方向反转），照实返回不截断
        assert decay_ratio(0.8, -0.2) == pytest.approx(-0.25)


# ══════════════════════════════════════════════════════════
# Walk-Forward（重叠窗口，只做方向一致性计数）
# ══════════════════════════════════════════════════════════

class TestWalkForward:
    def test_all_positive_windows(self):
        # 240 期 IC：前中后三段都均值正 → 3 窗 test 段全正
        ic = list(0.05 + np.random.default_rng(2).normal(0, 0.01, 240))
        wf = walk_forward(ic, n_windows=3)
        assert wf["windows"] == 3
        assert wf["same_direction"] == 3
        assert len(wf["per_window_icir"]) == 3
        assert all(v > 0 for v in wf["per_window_icir"])

    def test_mixed_direction_counts_majority(self):
        # 布局对齐窗口切分（total=240, window_len=120, step=80）：
        #   窗 0=[0:120]  test=[96:120]  → 正段内 → ICIR>0
        #   窗 1=[80:200] test=[176:200] → 4 正 + 20 负 → 主负 → ICIR<0
        #   窗 2=[160:240] test=[216:240] → 正段内 → ICIR>0
        # 常数段不可用：std 仅剩浮点误差 → ICIR 爆炸且方向随机；
        # 加小幅噪声让 std 稳定在 O(0.005)。
        rng = np.random.default_rng(11)
        ic = (list(0.05 + rng.normal(0, 0.005, 180))
              + list(-0.08 + rng.normal(0, 0.005, 30))
              + list(0.05 + rng.normal(0, 0.005, 30)))
        wf = walk_forward(ic, n_windows=3)
        assert wf["windows"] == 3
        assert wf["same_direction"] == 2     # 多数方向计数（正 2 : 负 1）
        assert wf["per_window_icir"][1] < 0
        assert wf["per_window_icir"][0] > 0
        assert wf["per_window_icir"][2] > 0

    def test_no_cross_window_merge_statistics(self):
        # 聚合口径：只输出计数与各窗 ICIR，无跨窗合并 p 值/均值检验字段
        ic = list(0.05 + np.random.default_rng(2).normal(0, 0.01, 240))
        wf = walk_forward(ic, n_windows=3)
        assert set(wf.keys()) == {"windows", "same_direction", "per_window_icir"}

    def test_too_short_returns_empty(self):
        wf = walk_forward([0.01, 0.02], n_windows=3)
        assert wf == {"windows": 0, "same_direction": 0, "per_window_icir": []}


# ══════════════════════════════════════════════════════════
# compute_stats 总装（StatResult 契约）
# ══════════════════════════════════════════════════════════

class TestComputeStats:
    def _full_input(self):
        rng = np.random.default_rng(42)
        ic = list(0.05 + rng.normal(0, 0.02, 200))
        return ic

    def test_daily_full_path(self):
        factor, ret = _panel_pair(5, signal=1.5)
        stats = compute_stats(
            ic_series=self._full_input(), train_icir=2.0, test_icir=1.2,
            total_trials=1987, frequency="daily",
            factor_panel=factor, return_panel=ret,
            n_resamples=200, rng=random.Random(13))
        assert isinstance(stats, StatResult)
        assert stats.degraded is False
        assert stats.total_trials == 1987
        assert 0.0 <= stats.p_value <= 1.0
        assert stats.perm_p_value is not None
        assert stats.ci_lower < stats.ci_upper
        assert 0.0 <= stats.dsr_icir <= 1.0
        assert stats.decay_ratio == pytest.approx(0.6)
        assert stats.walk_forward["windows"] >= 1

    def test_monthly_degraded(self):
        stats = compute_stats(
            ic_series=self._full_input(), train_icir=2.0, test_icir=1.0,
            total_trials=300, frequency="monthly",
            n_resamples=100, rng=random.Random(13))
        assert stats.degraded is True
        assert stats.perm_p_value is None          # 置换跳过
        assert math.isnan(stats.ci_lower)          # Bootstrap 跳过
        assert math.isnan(stats.ci_upper)
        assert stats.p_value >= 0.0                # 仅 t 检验参考值保留
        assert stats.walk_forward["windows"] == 0  # 窗口样本不足不硬凑

    def test_total_trials_zero_bonferroni_identity(self):
        # total_trials 缺省 1 时 p_adj == p（无校正意义，防御不炸）
        stats = compute_stats(
            ic_series=self._full_input(), train_icir=1.0, test_icir=1.0,
            total_trials=1, frequency="daily", n_resamples=50,
            rng=random.Random(2))
        assert stats.p_adj_bonferroni == pytest.approx(stats.p_value)

    def test_deterministic_with_seed(self):
        factor, ret = _panel_pair(5, signal=1.0)
        base = dict(
            ic_series=self._full_input(), train_icir=2.0, test_icir=1.5,
            total_trials=100, frequency="daily",
            factor_panel=factor, return_panel=ret,
            n_resamples=100)
        # 同种子必须各自新建 rng：共享 rng 对象会让第二次调用消费推进态，
        # 那是"顺序消耗"而非"非确定"——确定性断言要隔离这一混淆。
        s1 = compute_stats(**base, rng=random.Random(77))
        s2 = compute_stats(**base, rng=random.Random(77))
        assert dataclasses.asdict(s1) == dataclasses.asdict(s2)

    def test_statresult_is_json_safe_after_clean_tree(self):
        # 落库契约：metrics_json 走 clean_json_tree（NaN/Inf→None，TD3 口径），
        # allow_nan=False 序列化不得抛异常
        stats = compute_stats(
            ic_series=self._full_input(), train_icir=0.0, test_icir=0.5,
            total_trials=50, frequency="daily", n_resamples=50,
            rng=random.Random(2))
        cleaned = clean_json_tree(dataclasses.asdict(stats))
        import json as _json
        blob = _json.dumps(cleaned, allow_nan=False, ensure_ascii=False)
        assert '"decay_ratio": null' in blob   # train=0 → nan → None
