# -*- coding: utf-8 -*-
"""统计层 8 方法（M2）—— 设计文档 §7.9 / 需求 §3.2 第二层 / 开发文档 §3.17。

过拟合防线第二层：对单因子产出完整 StatResult（写
`factor_evaluation_runs.metrics_json.stats`，**不新建表**）。

实现路径唯一（§7.9）：Bonferroni / FDR(BH) **自研**，不做双路径开关，
避免两条实现结果不一致；scipy 只用于 t 检验与偏度/峰度/正态分位。

口径硬约束：
- Bonferroni `p_adj = min(p × total_trials, 1)`；校正后 <0.001 才算过；
- FDR(BH)：p 升序 → `p×n/rank` → 自后向前单调化；q < 0.1 才算过；
- Bootstrap：有放回重采样 N_RESAMPLES=1000 次 → ICIR 的 2.5%/97.5% 分位；
- 置换检验：打乱因子面板（逐列独立打乱时间轴）1000 次重算 ICIR 经验分布；
- Deflated Sharpe：total_trials + IC 分布偏度/峰度校正（Bailey & López de
  Prado 2015）；trial 间 SR 方差缺省用 Bootstrap 重采样方差估计；
- 衰减率 `test_ICIR / train_ICIR`，<0.5 疑似过拟合（分级硬约束）；
- Walk-Forward：3 个滚动窗口（步长=总长/窗口数、窗长=总长一半，相邻重叠），
  每窗内按全局比例 60/20/20 再切分、**只取 test 段算 ICIR**；
  **只做方向一致性计数，不做跨窗口均值/合并检验**——重叠窗口非独立样本，
  合并会高估显著性（§3.17.3）；
- 月频降级（§7.9.4）：跳过 Bootstrap / 置换 / DSR / Walk-Forward，
  仅报告 t 检验参考值，`degraded=True`，质量分级最高 B 级。

数值契约：无法计算的统计量返回 NaN（月频置换返回 None），由落库链路
`clean_json_tree`（TD3）统一转 None，**不许归 0**。
"""
from __future__ import annotations

import math
import random
from typing import Any, Mapping, Sequence

import numpy as np
from scipy import stats as _sstats

from app.services.factors.mining.contracts import StatResult

#: 重采样/置换次数（设计 §7.9：Bootstrap 1000、置换 1000）
N_RESAMPLES = 1000

#: FDR 判定阈值（需求 §3.2：q < 0.1）
FDR_ALPHA = 0.1

#: Bonferroni 校正后判定阈值（需求 §3.2：p < 0.001）
BONFERRONI_ALPHA = 0.001

#: Euler–Mascheroni 常数（DSR 期望最大 SR 公式）
_EULER_GAMMA = 0.5772156649015329

#: Walk-Forward 默认窗口数（设计 §7.9：3~4 个滚动窗口）
DEFAULT_WF_WINDOWS = 3

#: 窗口内 test 段占比（与全局 60/20/20 切分比例一致）
_WF_TEST_RATIO = 0.2

#: 置换检验最少有效截面数
_PERM_MIN_CROSS_SECTIONS = 3


# ══════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════

def _finite_array(values: Sequence[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    return arr[np.isfinite(arr)]


def _rng_to_seed(rng: random.Random) -> int:
    """从调用方 random.Random 取一个确定性种子传导给 numpy Generator。"""
    return int(rng.randrange(2 ** 32))


# ══════════════════════════════════════════════════════════
# 多重检验校正（自研，唯一实现路径）
# ══════════════════════════════════════════════════════════

def bonferroni_adjust(
    p_values: Sequence[float], total_trials: int,
) -> list[float]:
    """Bonferroni 校正：`p_adj = min(p × n, 1.0)`（n = total_trials）。"""
    n = max(1, int(total_trials))
    return [min(max(float(p), 0.0) * n, 1.0) for p in p_values]


def bh_adjust(p_values: Sequence[float]) -> list[float]:
    """FDR(BH) 自研校正：p 升序 → `p×n/rank` → 自后向前单调化。

    返回与输入同序的 q 值数组；q 值不超过 1。
    """
    p = [float(x) for x in p_values]
    m = len(p)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p[i])
    q_sorted = [1.0] * m
    running_min = 1.0
    for rank in range(m, 0, -1):          # 自后向前单调化
        idx = order[rank - 1]
        raw = p[idx] * m / rank
        running_min = min(running_min, raw)
        q_sorted[idx] = min(running_min, 1.0)
    # 数值安全：q 不低于原 p（BH 校正的理论性质）
    return [max(q, p[i]) for i, q in enumerate(q_sorted)]


# ══════════════════════════════════════════════════════════
# t 检验 / Bootstrap / 置换
# ══════════════════════════════════════════════════════════

def t_test_pvalue(ic_series: Sequence[float]) -> float:
    """单样本 t 检验 `scipy.stats.ttest_1samp(ic, 0)` → 双尾 p 值。"""
    arr = _finite_array(ic_series)
    if arr.size < 2:
        return float("nan")
    return float(_sstats.ttest_1samp(arr, 0.0).pvalue)


def bootstrap_icir_ci(
    ic_series: Sequence[float],
    *,
    n_resamples: int = N_RESAMPLES,
    rng: random.Random,
) -> tuple[float, float]:
    """Bootstrap ICIR 置信区间：有放回重采样 → 2.5%/97.5% 分位。

    无法计算（有效样本 <2 或全部重采样退化）→ (nan, nan)。
    """
    arr = _finite_array(ic_series)
    if arr.size < 2:
        return (float("nan"), float("nan"))
    gen = np.random.default_rng(_rng_to_seed(rng))
    idx = gen.integers(0, arr.size, size=(int(n_resamples), arr.size))
    samples = arr[idx]
    means = samples.mean(axis=1)
    stds = samples.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        icirs = means / stds
    icirs = icirs[np.isfinite(icirs)]
    if icirs.size == 0:
        return (float("nan"), float("nan"))
    lo, hi = np.percentile(icirs, [2.5, 97.5])
    return (float(lo), float(hi))


def _cross_section_ic_series(
    factor: np.ndarray, returns: np.ndarray,
) -> np.ndarray:
    """逐期截面 Pearson IC（NaN 对安全；有效截面内样本 <3 时跳过该期）。"""
    ics: list[float] = []
    for t in range(factor.shape[0]):
        x = factor[t]
        y = returns[t]
        mask = np.isfinite(x) & np.isfinite(y)
        if int(mask.sum()) < _PERM_MIN_CROSS_SECTIONS:
            continue
        corr = float(np.corrcoef(x[mask], y[mask])[0, 1])
        if math.isfinite(corr):
            ics.append(corr)
    return np.asarray(ics, dtype=float)


def _icir_of(ic_array: np.ndarray) -> float:
    if ic_array.size < 2:
        return float("nan")
    sd = float(ic_array.std(ddof=1))
    if sd <= 0.0:
        return float("nan")
    return float(ic_array.mean() / sd)


def permutation_icir_pvalue(
    factor: np.ndarray,
    returns: np.ndarray,
    *,
    n_permutations: int = N_RESAMPLES,
    rng: random.Random,
) -> float | None:
    """置换检验：打乱因子面板重算 ICIR 经验分布 → `P(perm >= obs)`。

    打乱方式：逐列独立打乱时间轴（保持截面值集合、破坏跨期对齐）。
    有效截面不足 / 观测 ICIR 不可得 → None（月频降级同样返回 None）。
    """
    f = np.asarray(factor, dtype=float)
    r = np.asarray(returns, dtype=float)
    if f.ndim != 2 or r.ndim != 2 or f.shape != r.shape:
        raise ValueError("factor_panel / return_panel 必须是同形 (T, N) 面板。")
    obs_ics = _cross_section_ic_series(f, r)
    obs_icir = _icir_of(obs_ics)
    if not math.isfinite(obs_icir):
        return None
    gen = np.random.default_rng(_rng_to_seed(rng))
    exceed = 0
    valid = 0
    for _ in range(int(n_permutations)):
        permuted = gen.permutation(f, axis=0)   # 逐列独立打乱时间轴
        perm_icir = _icir_of(_cross_section_ic_series(permuted, r))
        if not math.isfinite(perm_icir):
            continue
        valid += 1
        if perm_icir >= obs_icir:
            exceed += 1
    if valid == 0:
        return None
    return (exceed + 1) / (valid + 1)           # add-one 经验 p 值


# ══════════════════════════════════════════════════════════
# Deflated Sharpe / 衰减率 / Walk-Forward
# ══════════════════════════════════════════════════════════

def deflated_sharpe(
    ic_series: Sequence[float],
    observed_icir: float,
    total_trials: int,
    *,
    sr_variance: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Deflated Sharpe Ratio（Bailey & López de Prado 2015）。

    `dsr = Φ( (SR_obs − E[max SR]) · √(T−1) /
              √(1 − γ₃·SR + (γ₄−1)/4·SR²) )`，γ₃/γ₄ 为 IC 序列偏度/峰度，
    `E[max SR] = √(V[SR_trials]) · ((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e)))`。

    `sr_variance`（跨 trial 的 SR 方差）可注入；缺省用 IC 序列
    Bootstrap 重采样的 ICIR 方差估计。输出为 [0,1] 概率。
    """
    arr = _finite_array(ic_series)
    n_trials = max(1, int(total_trials))
    if arr.size < 3 or n_trials < 2 or not math.isfinite(observed_icir):
        return float("nan")
    if sr_variance is None:
        if rng is None:
            rng = random.Random(0)
        resampled = []
        gen = np.random.default_rng(_rng_to_seed(rng))
        idx = gen.integers(0, arr.size, size=(N_RESAMPLES, arr.size))
        samples = arr[idx]
        with np.errstate(divide="ignore", invalid="ignore"):
            sr_samples = samples.mean(axis=1) / samples.std(axis=1, ddof=1)
        sr_samples = sr_samples[np.isfinite(sr_samples)]
        if sr_samples.size < 2:
            return float("nan")
        sr_variance = float(sr_samples.var(ddof=1))
    g3 = float(_sstats.skew(arr))
    g4 = float(_sstats.kurtosis(arr, fisher=False))   # 非超额峰度
    e_max = (math.sqrt(max(sr_variance, 1e-12))
             * ((1.0 - _EULER_GAMMA)
                * float(_sstats.norm.ppf(1.0 - 1.0 / n_trials))
                + _EULER_GAMMA
                * float(_sstats.norm.ppf(1.0 - 1.0 / (n_trials * math.e)))))
    denom = math.sqrt(max(
        1e-12, 1.0 - g3 * observed_icir + (g4 - 1.0) / 4.0 * observed_icir ** 2))
    z = (observed_icir - e_max) * math.sqrt(arr.size - 1) / denom
    return float(_sstats.norm.cdf(z))


def decay_ratio(train_icir: float | None, test_icir: float | None) -> float:
    """衰减率 `test_ICIR / train_ICIR`；train 无效（None/NaN/0）→ NaN。

    <0.5 标记疑似过拟合（分级硬约束：不得 B 级以上）；
    test 段反向 → 负值照实返回，不截断。
    """
    if train_icir is None:
        return float("nan")
    try:
        train = float(train_icir)
    except (TypeError, ValueError):
        return float("nan")
    if not math.isfinite(train) or abs(train) < 1e-12:
        return float("nan")
    if test_icir is None:
        return float("nan")
    try:
        test = float(test_icir)
    except (TypeError, ValueError):
        return float("nan")
    if not math.isfinite(test):
        return float("nan")
    return test / train


def walk_forward(
    ic_series: Sequence[float], *, n_windows: int = DEFAULT_WF_WINDOWS,
) -> dict[str, Any]:
    """Walk-Forward 滚动窗口方向一致性（§3.17.3）。

    - 窗口：步长 = 总长/窗口数，窗长 = 总长一半（相邻窗口重叠）；
    - 每窗内按全局比例 60/20/20 再切分，**只取 test 段（后 20%）算 ICIR**；
    - `same_direction` = 多数方向窗口数（≥3/4 即方向一致，判定交由消费方）；
    - **只做方向一致性计数，不做跨窗口均值/合并检验**；
    - 样本过短 → `{"windows": 0, "same_direction": 0, "per_window_icir": []}`
      （不硬凑，前端灰掉展示"样本不足"）。
    """
    empty: dict[str, Any] = {
        "windows": 0, "same_direction": 0, "per_window_icir": [],
    }
    arr = _finite_array(ic_series)
    total = int(arr.size)
    n_windows = max(1, int(n_windows))
    if total < n_windows * 2:
        return empty
    window_len = total // 2
    step = total // n_windows
    per_window: list[float] = []
    for i in range(n_windows):
        start = min(i * step, total - window_len)
        segment = arr[start:start + window_len]
        test_len = max(1, int(window_len * _WF_TEST_RATIO))
        test_seg = segment[-test_len:]
        per_window.append(_icir_of(test_seg))
    finite = [v for v in per_window if math.isfinite(v)]
    pos = sum(1 for v in finite if v > 0)
    neg = sum(1 for v in finite if v < 0)
    return {
        "windows": n_windows,
        "same_direction": max(pos, neg) if finite else 0,
        "per_window_icir": per_window,
    }


# ══════════════════════════════════════════════════════════
# 总装：8 方法 → StatResult（契约见 contracts.py）
# ══════════════════════════════════════════════════════════

def compute_stats(
    *,
    ic_series: Sequence[float],
    train_icir: float | None,
    test_icir: float | None,
    total_trials: int,
    frequency: str,
    observed_icir: float | None = None,
    factor_panel: np.ndarray | None = None,
    return_panel: np.ndarray | None = None,
    n_resamples: int = N_RESAMPLES,
    rng: random.Random | None = None,
    degraded: bool | None = None,
) -> StatResult:
    """统计层总装：8 方法 → `contracts.StatResult`（写 metrics_json.stats）。

    Args:
        ic_series: 逐期 IC 序列（评估链路产出）。
        train_icir / test_icir: 两段 ICIR（衰减率输入）。
        total_trials: 一次挖掘任务累计评估过的候选因子总数（GA 主循环
            累加，唯一输入源 `factor_mining_runs.total_trials`）。
        frequency: "daily" | "weekly" | "monthly"（月频触发降级）。
        observed_icir: 观测 ICIR（缺省由 ic_series 估计）。
        factor_panel / return_panel: (T, N) 面板（置换检验输入；
            缺省时 perm_p_value=None）。
        n_resamples: 重采样/置换次数（测试可调小）。
        rng: `random.Random`（None 时用固定种子，输出完全确定）。
        degraded: 显式指定降级；缺省按 frequency=="monthly" 判定。

    Returns:
        `StatResult`。NaN/None 语义：无法计算的统计量返回 NaN（置换
        返回 None），由 clean_json_tree 落库转 None，不许归 0。
    """
    rng = rng if rng is not None else random.Random(0)
    arr = _finite_array(ic_series)
    is_degraded = (str(frequency) == "monthly") if degraded is None \
        else bool(degraded)

    # 1) t 检验（月频降级也保留——"仅报告 t 检验参考值"）
    p_value = t_test_pvalue(arr)

    # 2) Bonferroni（自研）/ 3) FDR(BH)（自研；单因子退化为标量数组）
    n = max(1, int(total_trials))
    if math.isfinite(p_value):
        p_adj = min(max(p_value, 0.0) * n, 1.0)
        q_value = bh_adjust([p_value])[0]
    else:
        p_adj = float("nan")
        q_value = float("nan")

    if is_degraded:
        # §7.9.4：Bootstrap / 置换 / DSR / Walk-Forward 均跳过
        ci_lower = float("nan")
        ci_upper = float("nan")
        perm_p: float | None = None
        dsr = float("nan")
        wf: dict[str, Any] = {
            "windows": 0, "same_direction": 0, "per_window_icir": [],
        }
    else:
        # 4) Bootstrap ICIR 置信区间
        ci_lower, ci_upper = bootstrap_icir_ci(
            arr, n_resamples=n_resamples, rng=rng)
        # 5) 置换检验（需面板；缺省 None 不炸）
        perm_p = None
        if factor_panel is not None and return_panel is not None:
            perm_p = permutation_icir_pvalue(
                factor_panel, return_panel,
                n_permutations=n_resamples, rng=rng)
        # 8) Walk-Forward（重叠窗口，只做方向一致性计数）
        wf = walk_forward(arr, n_windows=DEFAULT_WF_WINDOWS)
        # 6) Deflated Sharpe（total_trials + 偏度/峰度校正）
        if observed_icir is None:
            observed_icir = _icir_of(arr)
        if observed_icir is not None and math.isfinite(observed_icir):
            dsr = deflated_sharpe(
                arr, observed_icir, int(total_trials), rng=rng)
        else:
            dsr = float("nan")

    # 7) 衰减率（月频也计算：分级硬约束 decay<0.5 需要）
    ratio = decay_ratio(train_icir, test_icir)

    return StatResult(
        p_value=float(p_value),
        p_adj_bonferroni=float(p_adj),
        q_value_fdr=float(q_value),
        ci_lower=float(ci_lower),
        ci_upper=float(ci_upper),
        perm_p_value=perm_p,
        total_trials=n,
        dsr_icir=float(dsr),
        decay_ratio=float(ratio),
        walk_forward=wf,
        degraded=is_degraded,
    )


__all__ = [
    "N_RESAMPLES", "FDR_ALPHA", "BONFERRONI_ALPHA", "DEFAULT_WF_WINDOWS",
    "StatResult",
    "bonferroni_adjust", "bh_adjust", "t_test_pvalue",
    "bootstrap_icir_ci", "permutation_icir_pvalue", "deflated_sharpe",
    "decay_ratio", "walk_forward", "compute_stats",
]
