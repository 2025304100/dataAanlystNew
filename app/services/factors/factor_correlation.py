"""WP6-01/02: 相关性治理与残差增量评估。

对齐 docs/专业因子库开发计划.md §WP6-01/02 和 docs/因子设置与专业因子库改造方案.md §9。

核心能力：
- WP6-01 相关矩阵和聚类：Active/Candidate 因子横截面相关矩阵、层次聚类、代表因子识别
- WP6-02 残差增量评估：中性化、正交化、残差 IC，验证新因子相对已有因子的独立增量价值

设计原则：
- 纯计算模块（接受 DataFrame 输入），不直接访问 DB 或 DuckDB，便于测试和复用
- 与 factor_evaluator.py 风格一致，使用 dataclass 返回结构化结果
- 高相关候选（|corr| > 0.7）触发残差增量评估
- 残差无效（残差 ICIR 低于阈值或残差 IC 不显著）则建议 rejected

安全约束：
- 相关性计算只使用截面标准化后的值（rank 或 zscore），避免量纲影响
- 聚类使用基于距离的方法，簇内最大相关系数作为合并标准
- 残差评估对共线性稳健（使用最小二乘 + 正则化兜底）
- 不自动 reject，只返回建议和证据，最终决策由门禁或人工审批
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════════
# WP6-01: 相关矩阵和聚类
# ══════════════════════════════════════════════════════════

# 高相关阈值（超过此值触发残差增量评估）
HIGH_CORRELATION_THRESHOLD = 0.70

# 极高相关阈值（接近完全共线性，残差评估可能不稳定）
EXTREME_CORRELATION_THRESHOLD = 0.95

# 默认聚类合并阈值（簇内最大相关系数低于此值不再合并）
DEFAULT_CLUSTER_THRESHOLD = 0.60


@dataclass
class CorrelationMatrix:
    """因子相关矩阵结果。"""

    matrix: pd.DataFrame  # factor_code × factor_code 的相关系数矩阵
    method: str  # pearson / spearman / rank
    n_dates: int  # 参与计算的交易日数
    n_symbols_avg: float  # 平均标的数
    factor_codes: list[str] = field(default_factory=list)

    def get(self, code_a: str, code_b: str) -> float | None:
        """获取两个因子间的相关系数。"""
        if code_a not in self.matrix.index or code_b not in self.matrix.columns:
            return None
        val = self.matrix.loc[code_a, code_b]
        if pd.isna(val):
            return None
        return float(val)

    def high_correlation_pairs(
        self,
        threshold: float = HIGH_CORRELATION_THRESHOLD,
    ) -> list[tuple[str, str, float]]:
        """返回高相关因子对（|corr| > threshold），按绝对值降序。

        只返回上三角（避免重复 (a,b) 和 (b,a)），且排除自相关。
        """
        pairs: list[tuple[str, str, float]] = []
        codes = list(self.matrix.index)
        for i, a in enumerate(codes):
            for j in range(i + 1, len(codes)):
                b = codes[j]
                val = self.matrix.iloc[i, j]
                if pd.isna(val):
                    continue
                if abs(float(val)) > threshold:
                    pairs.append((a, b, float(val)))
        pairs.sort(key=lambda x: abs(x[2]), reverse=True)
        return pairs

    def to_dict(self) -> dict[str, Any]:
        return {
            "matrix": self.matrix.to_dict(),
            "method": self.method,
            "n_dates": self.n_dates,
            "n_symbols_avg": self.n_symbols_avg,
            "factor_codes": self.factor_codes,
            "high_correlation_pairs": self.high_correlation_pairs(),
        }


@dataclass
class FactorCluster:
    """单个因子簇。"""

    cluster_id: int
    members: list[str]  # 簇内因子 code
    representative: str  # 代表因子 code（ICIR 最高或最先加入）
    intra_max_correlation: float  # 簇内最大相关系数
    mean_correlation: float  # 簇内平均相关系数

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "members": self.members,
            "representative": self.representative,
            "intra_max_correlation": self.intra_max_correlation,
            "mean_correlation": self.mean_correlation,
        }


@dataclass
class ClusterReport:
    """聚类报告。"""

    clusters: list[FactorCluster]
    threshold: float
    n_factors: int
    n_clusters: int
    orphans: list[str]  # 未聚入任何簇的独立因子

    def to_dict(self) -> dict[str, Any]:
        return {
            "clusters": [c.to_dict() for c in self.clusters],
            "threshold": self.threshold,
            "n_factors": self.n_factors,
            "n_clusters": self.n_clusters,
            "orphans": self.orphans,
        }


def compute_correlation_matrix(
    factor_values: pd.DataFrame,
    *,
    method: str = "spearman",
) -> CorrelationMatrix:
    """计算因子间相关矩阵。

    参数：
        factor_values: 宽表，每列为一个因子，索引为 (trade_date, symbol) 或 multiindex。
                      建议先做截面 rank/zscore 标准化。
        method: pearson / spearman / rank
               - pearson: 原值皮尔逊相关
               - spearman: 秩相关（推荐，对异常值稳健）
               - rank: 等价于 spearman（截面 rank 后 pearson）

    返回：CorrelationMatrix
    """
    if factor_values.empty:
        return CorrelationMatrix(
            matrix=pd.DataFrame(),
            method=method,
            n_dates=0,
            n_symbols_avg=0.0,
            factor_codes=[],
        )

    if method == "rank":
        # 截面 rank 后用 pearson
        ranked = factor_values.groupby(level=0).rank(pct=True) if isinstance(
            factor_values.index, pd.MultiIndex
        ) else factor_values.rank(pct=True)
        corr = ranked.corr(method="pearson")
    elif method == "spearman":
        corr = factor_values.corr(method="spearman")
    else:
        corr = factor_values.corr(method="pearson")

    # 统计
    if isinstance(factor_values.index, pd.MultiIndex):
        n_dates = factor_values.index.get_level_values(0).nunique()
        n_symbols_avg = float(
            factor_values.groupby(level=0).size().mean()
        ) if n_dates > 0 else 0.0
    else:
        n_dates = 1
        n_symbols_avg = float(len(factor_values))

    return CorrelationMatrix(
        matrix=corr,
        method=method,
        n_dates=n_dates,
        n_symbols_avg=n_symbols_avg,
        factor_codes=list(corr.columns),
    )


def cluster_factors(
    corr_matrix: CorrelationMatrix,
    *,
    threshold: float = DEFAULT_CLUSTER_THRESHOLD,
    representative_scores: dict[str, float] | None = None,
) -> ClusterReport:
    """基于相关矩阵对因子进行层次聚类。

    使用单链接层次聚类（合并阈值 = threshold）：
    - 簇内任意两因子相关系数 >= threshold 时合并
    - 代表因子取簇内 representative_scores 最高者（无则取首个）

    参数：
        corr_matrix: 相关矩阵
        threshold: 合并阈值（|corr| >= threshold 视为相似）
        representative_scores: factor_code -> ICIR 或其他质量分数，用于选代表
    """
    matrix = corr_matrix.matrix
    codes = corr_matrix.factor_codes
    n = len(codes)

    if n == 0:
        return ClusterReport(
            clusters=[],
            threshold=threshold,
            n_factors=0,
            n_clusters=0,
            orphans=[],
        )

    # 初始化：每个因子自成一簇
    clusters: list[set[str]] = [{c} for c in codes]

    # 迭代合并：找当前最相似的簇对，若 |corr| >= threshold 则合并
    while len(clusters) > 1:
        best_corr = -1.0
        best_pair: tuple[int, int] | None = None

        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                # 单链接：簇间最大相关系数（取绝对值）
                max_corr = 0.0
                for a in clusters[i]:
                    for b in clusters[j]:
                        val = matrix.loc[a, b] if a in matrix.index and b in matrix.columns else np.nan
                        if pd.isna(val):
                            continue
                        abs_val = abs(float(val))
                        if abs_val > max_corr:
                            max_corr = abs_val

                if max_corr > best_corr:
                    best_corr = max_corr
                    best_pair = (i, j)

        if best_pair is None or best_corr < threshold:
            break

        # 合并
        i, j = best_pair
        clusters[i] = clusters[i] | clusters[j]
        clusters.pop(j)

    # 构建报告
    scores = representative_scores or {}
    result_clusters: list[FactorCluster] = []
    orphans: list[str] = []

    for idx, members in enumerate(clusters):
        member_list = sorted(members)
        if len(member_list) == 1:
            orphans.append(member_list[0])
            continue

        # 计算簇内最大和平均相关系数
        corrs: list[float] = []
        for i, a in enumerate(member_list):
            for j in range(i + 1, len(member_list)):
                b = member_list[j]
                val = matrix.loc[a, b] if a in matrix.index and b in matrix.columns else np.nan
                if not pd.isna(val):
                    corrs.append(abs(float(val)))

        intra_max = max(corrs) if corrs else 0.0
        mean_corr = float(np.mean(corrs)) if corrs else 0.0

        # 选代表：scores 最高的成员
        if scores:
            representative = max(member_list, key=lambda c: scores.get(c, -np.inf))
        else:
            representative = member_list[0]

        result_clusters.append(FactorCluster(
            cluster_id=idx,
            members=member_list,
            representative=representative,
            intra_max_correlation=intra_max,
            mean_correlation=mean_corr,
        ))

    return ClusterReport(
        clusters=result_clusters,
        threshold=threshold,
        n_factors=n,
        n_clusters=len(result_clusters),
        orphans=orphans,
    )


# ══════════════════════════════════════════════════════════
# WP6-02: 残差增量评估
# ══════════════════════════════════════════════════════════

# 残差 ICIR 最低门禁（低于此值视为无独立增量价值）
MIN_RESIDUAL_ICIR = 0.15

# 残差 IC 显著性（t 统计量阈值，近似）
MIN_RESIDUAL_IC_TSTAT = 1.96


@dataclass
class ResidualIncrementResult:
    """残差增量评估结果。"""

    candidate_code: str
    reference_codes: list[str]
    residual_ic_mean: float
    residual_icir: float
    residual_ic_tstat: float
    original_ic_mean: float
    original_icir: float
    incremental_ic_ratio: float  # 残差 IC / 原始 IC
    has_incremental_value: bool
    verdict: str  # valuable / marginal / redundant
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_code": self.candidate_code,
            "reference_codes": self.reference_codes,
            "residual_ic_mean": self.residual_ic_mean,
            "residual_icir": self.residual_icir,
            "residual_ic_tstat": self.residual_ic_tstat,
            "original_ic_mean": self.original_ic_mean,
            "original_icir": self.original_icir,
            "incremental_ic_ratio": self.incremental_ic_ratio,
            "has_incremental_value": self.has_incremental_value,
            "verdict": self.verdict,
            "reasons": self.reasons,
        }


def compute_residual_ic(
    candidate: pd.Series,
    references: pd.DataFrame,
    target: pd.Series,
    *,
    candidate_code: str = "candidate",
    original_ic_mean: float | None = None,
    original_icir: float | None = None,
) -> ResidualIncrementResult:
    """计算候选因子对已有因子的残差增量 IC。

    流程：
    1. 用已有因子线性回归候选因子（正交化）
    2. 计算残差的截面 IC（与 target 的 rank 相关）
    3. 对比原始 IC，判断是否有独立增量价值

    参数：
        candidate: 候选因子值（与 target 同索引）
        references: 已有因子值矩阵（每列一个因子，同索引）
        target: 目标收益（同索引）
        candidate_code: 候选因子代码
        original_ic_mean: 候选因子的原始 IC 均值（不传则现场计算）
        original_icir: 候选因子的原始 ICIR（不传则现场计算）

    返回：ResidualIncrementResult
    """
    from app.services.factors.factor_evaluator import compute_rank_ic

    # 对齐索引
    if references.empty:
        ref_codes: list[str] = []
        # 对齐 candidate 和 target，去除任一缺失
        pair = pd.concat([candidate.rename("candidate"), target.rename("target")], axis=1).dropna()
        residual = pair["candidate"]
        target = pair["target"]
    else:
        ref_codes = list(references.columns)
        # 合并对齐，去除任一缺失
        combined = pd.concat([candidate.rename("candidate"), references, target.rename("target")], axis=1)
        combined = combined.dropna(subset=["candidate", "target"] + ref_codes)

        if len(combined) < 30:
            return ResidualIncrementResult(
                candidate_code=candidate_code,
                reference_codes=ref_codes,
                residual_ic_mean=0.0,
                residual_icir=0.0,
                residual_ic_tstat=0.0,
                original_ic_mean=original_ic_mean or 0.0,
                original_icir=original_icir or 0.0,
                incremental_ic_ratio=0.0,
                has_incremental_value=False,
                verdict="redundant",
                reasons=["insufficient_samples"],
            )

        candidate_aligned = combined["candidate"]
        target_aligned = combined["target"]
        refs_aligned = combined[ref_codes]

        # 正交化：用已有因子回归候选因子，取残差
        # 使用最小二乘：beta = (X'X)^-1 X'y
        X = refs_aligned.values
        y = candidate_aligned.values

        # 截面 rank 标准化（减少量纲和异常值影响）
        X_ranked = np.apply_along_axis(
            lambda v: pd.Series(v).rank(pct=True).values, 0, X
        )
        y_ranked = pd.Series(y).rank(pct=True).values

        # 添加截距项
        X_with_const = np.column_stack([np.ones(len(X_ranked)), X_ranked])

        try:
            # 正规方程解
            beta, _, _, _ = np.linalg.lstsq(X_with_const, y_ranked, rcond=None)
            residual_values = y_ranked - X_with_const @ beta
        except np.linalg.LinAlgError:
            # 共线性 fallback：加 L2 正则
            ridge = 1e-6 * np.eye(X_with_const.shape[1])
            beta = np.linalg.solve(
                X_with_const.T @ X_with_const + ridge,
                X_with_const.T @ y_ranked,
            )
            residual_values = y_ranked - X_with_const @ beta

        residual = pd.Series(residual_values, index=combined.index)
        target = target_aligned

    # 计算残差 IC
    # 如果索引是 MultiIndex (trade_date, symbol)，按日截面计算 Rank IC
    if isinstance(residual.index, pd.MultiIndex):
        # compute_rank_ic 期望宽格式（index=trade_date, columns=symbol）
        # 将 long-format Series unstack 为 wide-format DataFrame
        residual_wide = residual.unstack(level="symbol")
        target_wide = target.unstack(level="symbol")
        residual_ic = compute_rank_ic(residual_wide, target_wide)
        residual_ic_mean = float(residual_ic.rank_ic_mean)
        residual_icir = float(residual_ic.icir)
        n = len(residual_ic.ic_series)  # 参与计算的交易日数
    else:
        # 单截面：用 rank 后 pearson 近似 spearman（避免引入 scipy 依赖）
        valid = residual.notna() & target.notna()
        if valid.sum() < 30:
            return ResidualIncrementResult(
                candidate_code=candidate_code,
                reference_codes=ref_codes,
                residual_ic_mean=0.0,
                residual_icir=0.0,
                residual_ic_tstat=0.0,
                original_ic_mean=original_ic_mean or 0.0,
                original_icir=original_icir or 0.0,
                incremental_ic_ratio=0.0,
                has_incremental_value=False,
                verdict="redundant",
                reasons=["insufficient_samples"],
            )
        r_ranked = residual[valid].rank(pct=True)
        t_ranked = target[valid].rank(pct=True)
        rho = float(r_ranked.corr(t_ranked))
        residual_ic_mean = rho if not np.isnan(rho) else 0.0
        # 单截面无 ICIR
        residual_icir = 0.0
        n = int(valid.sum())

    # t 统计量近似：IC_mean * sqrt(n) / std
    # 对于 Rank IC，std ≈ 1/sqrt(3) ≈ 0.5774（均匀分布秩的 std）
    if n > 1:
        residual_ic_tstat = float(residual_ic_mean * np.sqrt(n) / 0.5774)
    else:
        residual_ic_tstat = 0.0

    # 原始 IC（如果未提供，现场计算）
    if original_ic_mean is None or original_icir is None:
        if isinstance(candidate.index, pd.MultiIndex):
            # 转换为宽格式以适配 compute_rank_ic
            candidate_wide = candidate.unstack(level="symbol")
            target_wide_orig = target.unstack(level="symbol")
            orig_ic = compute_rank_ic(candidate_wide, target_wide_orig)
            orig_ic_mean = float(orig_ic.rank_ic_mean)
            orig_icir = float(orig_ic.icir)
        else:
            valid = candidate.notna() & target.notna()
            if valid.sum() >= 30:
                c_ranked = candidate[valid].rank(pct=True)
                t_ranked = target[valid].rank(pct=True)
                rho = float(c_ranked.corr(t_ranked))
                orig_ic_mean = rho if not np.isnan(rho) else 0.0
                orig_icir = 0.0
            else:
                orig_ic_mean = 0.0
                orig_icir = 0.0
    else:
        orig_ic_mean = float(original_ic_mean)
        orig_icir = float(original_icir)

    # 增量比例
    incremental_ratio = float(residual_ic_mean / orig_ic_mean) if abs(orig_ic_mean) > 1e-10 else 0.0

    # 判定
    reasons: list[str] = []
    if abs(residual_ic_mean) < 1e-6:
        verdict = "redundant"
        reasons.append("residual_ic_near_zero")
        has_value = False
    elif residual_icir < MIN_RESIDUAL_ICIR and abs(residual_ic_tstat) < MIN_RESIDUAL_IC_TSTAT:
        verdict = "redundant"
        reasons.append("low_residual_icir")
        reasons.append("insufficient_significance")
        has_value = False
    elif incremental_ratio < 0.30:
        verdict = "marginal"
        reasons.append("low_incremental_ratio")
        has_value = True
    else:
        verdict = "valuable"
        reasons.append("significant_incremental_ic")
        has_value = True

    return ResidualIncrementResult(
        candidate_code=candidate_code,
        reference_codes=ref_codes,
        residual_ic_mean=residual_ic_mean,
        residual_icir=residual_icir,
        residual_ic_tstat=residual_ic_tstat,
        original_ic_mean=orig_ic_mean,
        original_icir=orig_icir,
        incremental_ic_ratio=incremental_ratio,
        has_incremental_value=has_value,
        verdict=verdict,
        reasons=reasons,
    )


@dataclass
class CorrelationGovernanceReport:
    """相关性治理综合报告。"""

    correlation: CorrelationMatrix
    clusters: ClusterReport
    high_correlation_pairs: list[tuple[str, str, float]]
    residual_assessments: list[ResidualIncrementResult]
    recommendations: dict[str, str]  # factor_code -> keep/reject/investigate
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation": self.correlation.to_dict(),
            "clusters": self.clusters.to_dict(),
            "high_correlation_pairs": [
                {"a": a, "b": b, "correlation": c}
                for a, b, c in self.high_correlation_pairs
            ],
            "residual_assessments": [r.to_dict() for r in self.residual_assessments],
            "recommendations": self.recommendations,
            "summary": self.summary,
        }


def run_correlation_governance(
    factor_values: pd.DataFrame,
    target: pd.Series,
    *,
    method: str = "spearman",
    cluster_threshold: float = DEFAULT_CLUSTER_THRESHOLD,
    representative_scores: dict[str, float] | None = None,
) -> CorrelationGovernanceReport:
    """运行相关性治理全流程。

    流程：
    1. 计算因子相关矩阵
    2. 层次聚类识别同质因子簇
    3. 对高相关候选执行残差增量评估
    4. 生成去留建议

    参数：
        factor_values: 宽表，每列为一个因子
        target: 目标收益
        method: 相关计算方法
        cluster_threshold: 聚类合并阈值
        representative_scores: 因子质量分数（ICIR），用于选簇代表
    """
    # 1. 相关矩阵
    corr = compute_correlation_matrix(factor_values, method=method)

    # 2. 聚类
    clusters = cluster_factors(
        corr,
        threshold=cluster_threshold,
        representative_scores=representative_scores,
    )

    # 3. 高相关对
    high_pairs = corr.high_correlation_pairs(threshold=HIGH_CORRELATION_THRESHOLD)

    # 4. 对每个高相关对中的候选因子做残差评估
    residual_results: list[ResidualIncrementResult] = []
    recommendations: dict[str, str] = {}

    for code_a, code_b, _corr_val in high_pairs:
        # 把 b 视为候选，a 视为已有因子（如果 a 在 representative_scores 中分数更高）
        scores = representative_scores or {}
        score_a = scores.get(code_a, 0.0)
        score_b = scores.get(code_b, 0.0)

        if score_b <= score_a:
            # a 是代表，b 是候选
            candidate_code, ref_code = code_b, code_a
        else:
            candidate_code, ref_code = code_a, code_b

        if candidate_code not in factor_values.columns or ref_code not in factor_values.columns:
            continue
        if candidate_code in recommendations and recommendations[candidate_code] == "reject":
            continue

        result = compute_residual_ic(
            candidate=factor_values[candidate_code],
            references=factor_values[[ref_code]],
            target=target,
            candidate_code=candidate_code,
        )
        residual_results.append(result)

        if result.verdict == "redundant":
            recommendations[candidate_code] = "reject"
        elif result.verdict == "marginal":
            recommendations[candidate_code] = "investigate"
        else:
            recommendations[candidate_code] = "keep"

    # 未被标记的因子默认 keep
    for code in corr.factor_codes:
        if code not in recommendations:
            recommendations[code] = "keep"

    # 汇总
    n_reject = sum(1 for v in recommendations.values() if v == "reject")
    n_investigate = sum(1 for v in recommendations.values() if v == "investigate")
    n_keep = sum(1 for v in recommendations.values() if v == "keep")
    summary = (
        f"相关性治理完成：{len(corr.factor_codes)} 个因子，"
        f"{clusters.n_clusters} 个同质簇，{len(high_pairs)} 对高相关。"
        f"建议保留 {n_keep}，调查 {n_investigate}，拒绝 {n_reject}。"
    )

    return CorrelationGovernanceReport(
        correlation=corr,
        clusters=clusters,
        high_correlation_pairs=high_pairs,
        residual_assessments=residual_results,
        recommendations=recommendations,
        summary=summary,
    )


__all__ = [
    "HIGH_CORRELATION_THRESHOLD",
    "EXTREME_CORRELATION_THRESHOLD",
    "DEFAULT_CLUSTER_THRESHOLD",
    "MIN_RESIDUAL_ICIR",
    "MIN_RESIDUAL_IC_TSTAT",
    "CorrelationMatrix",
    "FactorCluster",
    "ClusterReport",
    "ResidualIncrementResult",
    "CorrelationGovernanceReport",
    "compute_correlation_matrix",
    "cluster_factors",
    "compute_residual_ic",
    "run_correlation_governance",
]
