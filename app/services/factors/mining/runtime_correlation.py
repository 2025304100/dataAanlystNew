"""运行时相关性去重（第 4 层；向导 §6.4 / WBS M1B-04 / 任务 P1-6）。

四层去重机制（dedup.py 承载第 1~3 层，本模块承载第 4 层）：

| 层 | 手段 | 环节 |
|---|---|---|
| 1 | 生成时规避 | T17/T18 生成器 |
| 2 | formula_hash 精确去重 | `dedup_candidates` |
| 3 | AST 结构相似度聚类 | `dedup_candidates` |
| 4 | **因子值相关性去重（运行时）** | **本模块**（每代短样本评估后） |

算法（向导 §6.4 第 762-767 行；WBS M1B-04 口径）：
1. **语义类别指纹**：不同 factor category 直接跳过比对
2. **统计指纹**：多空方向 / IC 均值 / 因子值标准差 / 换手率，任意 3 项差异大则跳过
3. **采样相关**：随机抽 10 个调仓日算截面相关，均值 < 0.7 则跳过
4. 只对通过前三层的个体对算**全量精确 Spearman**，|ρ| ≥ 0.95 → `eliminated_similar`

淘汰**不物理删除**（与第 2/3 层同一纪律）：被淘汰个体标
`elimination_status="eliminated"` / `elimination_reason="eliminated_similar"` /
`similar_to_candidate_id`（代表 `formula_hash`）/ `similarity_score`（|ρ|）。

⚠️ `total_trials`（DSR 唯一输入源）记的是**全量评估数**：本模块只从繁殖池
移除近似个体，不减评估计数（该纪律由 `genetic_algorithm.run_ga_loop` 保证）。
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from app.services.factors.factor_correlation import EXTREME_CORRELATION_THRESHOLD
from app.services.factors.mining.dedup import ELIM_REASON_SIMILAR

# ══════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════

#: 第 4 层淘汰阈值（与 factor_correlation.EXTREME_CORRELATION_THRESHOLD 对齐）
CORRELATION_THRESHOLD = float(EXTREME_CORRELATION_THRESHOLD)  # 0.95

#: 采样相关：随机抽 N 个调仓日
SAMPLE_DATES = 10
#: 采样相关门槛：截面相关均值的 |·| 低于此值视为不相似，跳过精确计算
SAMPLE_CORR_FLOOR = 0.7

#: 签名降采样上限（控制单个体签名内存：日期 × 符号）
MAX_SIGNATURE_DATES = 20
MAX_SIGNATURE_SYMBOLS = 300

#: 统计指纹「差异大」阈值（任意 3 项超阈 → 跳过精确 Spearman）
IC_MEAN_DIFF = 0.03        # IC 均值绝对差
FACTOR_STD_DIFF = 0.10     # 因子值标准差绝对差（rank 口径 ∈[0, 0.289]）
TURNOVER_DIFF = 0.30       # 换手率绝对差

#: 精确 Spearman 需最少有效配对样本数（低于则统计无意义 → 不合并）
MIN_PAIRED_SAMPLES = 30
#: 采样相关单个截面需最少有效符号数
MIN_SECTION_SAMPLES = 5


# ══════════════════════════════════════════════════════════
# 签名
# ══════════════════════════════════════════════════════════


@dataclass
class FactorValueSignature:
    """单个体因子值的降采样签名（供第 4 层去重比对）。"""

    factor_std: float | None
    panel: pd.DataFrame | None          # date × symbol 原始因子值（降采样）
    top_overlap_vector: str | None      # Top20% 成分 MinHash 签名（落库用）

    @property
    def empty(self) -> bool:
        return self.panel is None or self.panel.empty


def _downsample_panel(
    features: pd.DataFrame, *, max_dates: int, max_symbols: int,
) -> pd.DataFrame | None:
    """按日期均匀抽样 + 符号排序截断，得到确定性的降采样面板。

    日期/符号选择对同一代内所有个体**一致**（同一 train 窗口尾部、同一股票池），
    因此任意两签名可直接按 (date, symbol) 索引对齐比对。
    """
    if features is None or features.empty:
        return None
    panel = features.copy()
    try:
        dates = sorted(d for d in panel.index if d is not None)
    except TypeError:  # 索引含不可比类型 → 退化为原顺序
        dates = list(panel.index)
    if len(dates) > max_dates:
        idx = np.linspace(0, len(dates) - 1, max_dates).round().astype(int)
        dates = [dates[int(i)] for i in sorted(set(idx.tolist()))]
    symbols = sorted(str(c) for c in panel.columns)
    if len(symbols) > max_symbols:
        symbols = symbols[:max_symbols]
    return panel.reindex(index=dates, columns=symbols)


def _top_overlap_vector(rank: pd.DataFrame) -> str | None:
    """Top20% 成分 MinHash 签名（落库 `top_overlap_vector`）。

    取截面百分位秩均值 Top20% 的符号，逐个 md5 取前 8 位，排序后 JSON 化。
    这是**冗余落库**字段（真正的第 3 层门禁用采样相关）；仅作可追溯证据。
    """
    if rank.empty:
        return None
    mean_rank = rank.mean()
    n = int(max(1, round(len(mean_rank) * 0.2)))
    top = mean_rank.nlargest(n).index
    hashes = sorted(
        hashlib.md5(str(s).encode("utf-8")).hexdigest()[:8] for s in top)
    return json.dumps(hashes, ensure_ascii=False)


def factor_value_signature(
    features: pd.DataFrame,
    *, max_dates: int = MAX_SIGNATURE_DATES,
    max_symbols: int = MAX_SIGNATURE_SYMBOLS,
) -> FactorValueSignature | None:
    """由 date × symbol 因子值面板构造降采样签名。

    `factor_std` 取**截面百分位秩**的逐日标准差均值（尺度无关，0~0.289）。
    数据不足以降采样时返回 None（调用方视为「无法判定相关 → 不合并」）。
    """
    panel = _downsample_panel(features, max_dates=max_dates, max_symbols=max_symbols)
    if panel is None or panel.empty:
        return None
    rank = panel.rank(axis=1, pct=True)
    std = float(rank.std(axis=1).mean()) if rank.shape[0] > 0 else 0.0
    factor_std = float(std) if np.isfinite(std) else None
    return FactorValueSignature(
        factor_std=factor_std,
        panel=panel,
        top_overlap_vector=_top_overlap_vector(rank),
    )


# ══════════════════════════════════════════════════════════
# 相关性计算
# ══════════════════════════════════════════════════════════


def spearman_rho(a: pd.Series, b: pd.Series,
                 *, min_n: int = MIN_PAIRED_SAMPLES) -> float:
    """精确 Spearman 秩相关（复用 pandas，避免 scipy 依赖）。

    样本不足或不可算时返回 NaN（调用方据此「无法判定 → 不合并」）。
    """
    if a is None or b is None or len(a) < min_n or len(b) < min_n:
        return float("nan")
    try:
        rho = float(a.corr(b, method="spearman"))
    except Exception:  # noqa: BLE001 - 相关计算失败 → 不判定
        return float("nan")
    return rho if np.isfinite(rho) else float("nan")


def _paired(a: pd.DataFrame, b: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """两面板按 (date, symbol) 内联对齐，取双非空配对。"""
    aa = a.stack()
    bb = b.stack()
    joined = pd.concat([aa.rename("a"), bb.rename("b")], axis=1, join="inner")
    joined = joined.replace([np.inf, -np.inf], np.nan).dropna()
    return joined["a"], joined["b"]


def _sample_correlation(
    a: pd.DataFrame, b: pd.DataFrame, *, sample_dates: int, rng: random.Random,
) -> float:
    """采样相关：随机抽 `sample_dates` 个调仓日，逐日截面 Spearman 取均值。"""
    common = sorted(set(a.index) & set(b.index))
    if not common:
        return float("nan")
    n = min(sample_dates, len(common))
    days = rng.sample(common, n) if n < len(common) else common
    rhos: list[float] = []
    for d in days:
        ra, rb = a.loc[d], b.loc[d]
        valid = ra.notna() & rb.notna()
        if int(valid.sum()) < MIN_SECTION_SAMPLES:
            continue
        r = spearman_rho(ra[valid], rb[valid], min_n=MIN_SECTION_SAMPLES)
        if np.isfinite(r):
            rhos.append(r)
    return float(np.mean(rhos)) if rhos else float("nan")


# ══════════════════════════════════════════════════════════
# 统计指纹
# ══════════════════════════════════════════════════════════


def _direction_code(entry: Mapping[str, Any]) -> int:
    d = str(entry.get("expected_direction") or "").strip().lower()
    if d == "positive":
        return 1
    if d == "negative":
        return -1
    return 0


def _fval(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def _stat_diff_count(
    sig_a: FactorValueSignature | None,
    sig_b: FactorValueSignature | None,
    fa: Mapping[str, Any], fb: Mapping[str, Any],
    dir_a: int, dir_b: int,
) -> int:
    """四项统计指纹中「差异大」的项数（≥3 → 跳过精确 Spearman）。"""
    diff = 0
    if dir_a != dir_b:
        diff += 1
    ic_a = _fval(fa.get("ic_mean")) or 0.0
    ic_b = _fval(fb.get("ic_mean")) or 0.0
    if abs(ic_a - ic_b) > IC_MEAN_DIFF:
        diff += 1
    std_a = sig_a.factor_std if sig_a and sig_a.factor_std is not None else 0.0
    std_b = sig_b.factor_std if sig_b and sig_b.factor_std is not None else 0.0
    if abs(std_a - std_b) > FACTOR_STD_DIFF:
        diff += 1
    to_a = _fval(fa.get("turnover")) or 0.0
    to_b = _fval(fb.get("turnover")) or 0.0
    if abs(to_a - to_b) > TURNOVER_DIFF:
        diff += 1
    return diff


# ══════════════════════════════════════════════════════════
# 淘汰判定与主入口
# ══════════════════════════════════════════════════════════


def build_fingerprint(
    entry: Mapping[str, Any], signature: FactorValueSignature | None,
) -> dict[str, Any]:
    """由个体 + 签名组装预筛指纹（落库 `factor_mining_prescreen_fingerprints`）。"""
    fitness = entry.get("fitness") or {}
    return {
        "semantic_category": entry.get("category"),
        "long_short_direction": _direction_code(entry),
        "ic_mean_short": _fval(fitness.get("ic_mean")),
        "factor_std": signature.factor_std if signature else None,
        "turnover_rate": _fval(fitness.get("turnover")),
        "top_overlap_vector": (signature.top_overlap_vector
                               if signature else None),
    }


def _similarity(
    entry: Mapping[str, Any], rep: Mapping[str, Any], *,
    rng: random.Random, threshold: float, sample_dates: int, sample_floor: float,
) -> float | None:
    """漏斗判定：通过四层返回精确 Spearman 相似度（|ρ|≥threshold），否则 None。"""
    a_cat = entry.get("category")
    b_cat = rep.get("category")
    if a_cat and b_cat and a_cat != b_cat:
        return None                                   # 1. 语义类别不同
    a_sig = entry.get("signature")
    b_sig = rep.get("signature")
    if (a_sig is None or b_sig is None
            or getattr(a_sig, "panel", None) is None
            or getattr(b_sig, "panel", None) is None):
        return None                                   # 无法计算 → 保守不合并
    fa = entry.get("fitness") or {}
    fb = rep.get("fitness") or {}
    if _stat_diff_count(a_sig, b_sig, fa, fb,
                        _direction_code(entry), _direction_code(rep)) >= 3:
        return None                                   # 2. 统计指纹 3 项差异大
    sample = _sample_correlation(
        a_sig.panel, b_sig.panel, sample_dates=sample_dates, rng=rng)
    if np.isfinite(sample) and abs(sample) < sample_floor:
        return None                                   # 3. 采样相关低
    pa, pb = _paired(a_sig.panel, b_sig.panel)
    rho = spearman_rho(pa, pb)
    if np.isfinite(rho) and abs(rho) >= threshold:
        return abs(rho)                               # 4. 全量精确 Spearman
    return None


@dataclass
class RuntimeDedupResult:
    kept: list[dict[str, Any]] = field(default_factory=list)
    eliminated: list[dict[str, Any]] = field(default_factory=list)
    fingerprints: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def dedup_by_correlation(
    evaluated: Sequence[Mapping[str, Any]], *,
    threshold: float = CORRELATION_THRESHOLD,
    sample_dates: int = SAMPLE_DATES,
    sample_floor: float = SAMPLE_CORR_FLOOR,
    seed: int = 42,
) -> RuntimeDedupResult:
    """第 4 层去重主入口：贪心保留代表（**按输入序**，优者优先），近似的标淘汰。

    `evaluated` 为已排序的个体 dict 序列（naive=ICIR 降序，advanced=B1 序前），
    每个 dict 需含 `category` / `expected_direction` / `fitness`（Mapping，
    含 `ic_mean`/`turnover`）/ `signature`（`FactorValueSignature`）。

    **原地标记**：淘汰个体写入 `elimination_status` / `elimination_reason` /
    `similar_to_candidate_id` / `similarity_score`；存活个体写
    `elimination_status="active"`；每个个体附 `_prescreen_fingerprint`。
    返回的 `kept`/`eliminated` 引用同一批 dict（不拷贝）。
    """
    rng = random.Random(int(seed))
    kept: list[dict[str, Any]] = []
    eliminated: list[dict[str, Any]] = []
    fingerprints: list[dict[str, Any]] = []

    for entry in evaluated:
        row = entry if isinstance(entry, dict) else dict(entry)
        signature = row.get("signature")
        fp = build_fingerprint(row, signature)
        fingerprints.append(fp)
        row["_prescreen_fingerprint"] = fp

        similar_to = None
        similarity: float | None = None
        for rep in kept:
            sim = _similarity(
                row, rep, rng=rng, threshold=threshold,
                sample_dates=sample_dates, sample_floor=sample_floor)
            if sim is not None:
                similar_to = rep
                similarity = sim
                break

        if similar_to is not None:
            row["elimination_status"] = "eliminated"
            row["elimination_reason"] = ELIM_REASON_SIMILAR
            row["similar_to_candidate_id"] = str(
                similar_to.get("formula_hash")
                or similar_to.get("formula") or "")
            row["similarity_score"] = float(similarity)
            eliminated.append(row)
        else:
            row["elimination_status"] = "active"
            row["elimination_reason"] = None
            kept.append(row)

    return RuntimeDedupResult(
        kept=kept, eliminated=eliminated, fingerprints=fingerprints,
        stats={
            "input_count": len(evaluated),
            "kept_count": len(kept),
            "eliminated_count": len(eliminated),
            "dedup_rate": (round(len(eliminated) / len(evaluated), 6)
                           if evaluated else 0.0),
        },
    )


__all__ = [
    "CORRELATION_THRESHOLD",
    "SAMPLE_DATES",
    "SAMPLE_CORR_FLOOR",
    "MAX_SIGNATURE_DATES",
    "MAX_SIGNATURE_SYMBOLS",
    "IC_MEAN_DIFF",
    "FACTOR_STD_DIFF",
    "TURNOVER_DIFF",
    "MIN_PAIRED_SAMPLES",
    "MIN_SECTION_SAMPLES",
    "FactorValueSignature",
    "RuntimeDedupResult",
    "factor_value_signature",
    "spearman_rho",
    "build_fingerprint",
    "dedup_by_correlation",
]