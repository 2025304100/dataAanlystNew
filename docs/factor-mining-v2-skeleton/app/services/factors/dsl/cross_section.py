"""截面算子族 `cs_` 求值（设计文档 §6.2，模块 M2）。

背景：现有 `rank`/`zscore` 只是**后处理配置项**（`factor_executor.apply_postprocess`
作用于最终因子值，`factor_executor.py:423`），无法表达 `cs_rank(A) - cs_rank(B)`。
本模块提供**公式内**的截面算子，按 `trade_date` 分组向量化。

⚠️ 与后处理的关系：两者**语义不同、并存**（设计文档 §6.2 作用域边界）。
   `apply_postprocess` 保持不变；本模块的算子在 AST 求值阶段生效。

⚠️ 注册双写：新增算子必须**同时**加到
   - `factor_compiler.FUNCTION_CATALOG`（编译期白名单）
   - `indicator_ast_sandbox.ALLOWED_FUNCS`（沙箱白名单）
   只加一处会导致沙箱拒绝（设计文档 §9.5）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

#: 本模块提供的算子名（供 compilers / sandbox 同步注册）
CROSS_SECTION_OPS: tuple[str, ...] = (
    "cs_rank",
    "cs_zscore",
    "cs_demean",
    "cs_scale",
    "cs_quantile",
    "cs_winsorize",
)

#: 竖表约定：index = (trade_date, symbol_id)，或 index = trade_date 且 columns = symbol_id
#: 本模块统一要求 **index = trade_date（已按日分组）**，由调用方保证排序。
DEFAULT_WINSORIZE_MAD_SCALE = 1.4826


# ══════════════════════════════════════════════════════════
# 单算子实现（纯 pandas，可用小样本单测）
# ══════════════════════════════════════════════════════════


def cs_rank(values: pd.DataFrame, *, pct: bool = True) -> pd.DataFrame:
    """横截面百分位排名。组内 NaN 保持 NaN。"""
    return values.groupby(level=0, sort=False).rank(pct=pct, na_option="keep")


def cs_zscore(values: pd.DataFrame) -> pd.DataFrame:
    """横截面标准化 (x - mean) / std。

    ⚠️ 退化截面（组内全部同值 → std=0，如全部停牌或数据未就绪）返回 **NaN**，
    而非 0。理由：返回 0 会向进化器注入"该日全市场无差异"的伪信号，
    与需求 §3.5「不得自动剔除/降级/填零」的取向一致，且 NaN 会在覆盖率
    计算中被正确识别为无效截面。

    ⚠️ 与后处理配置项 `zscore`（`factor_executor._zscore`，作用于最终因子值）
    的退化处理可能不同，两者语义不同、**并存不互相约束**。
    """
    grouped = values.groupby(level=0, sort=False)
    mean = grouped.transform("mean")
    std = grouped.transform("std")
    return (values - mean) / std.replace(0.0, np.nan)


def cs_demean(values: pd.DataFrame) -> pd.DataFrame:
    """横截面去均值。"""
    return values - values.groupby(level=0, sort=False).transform("mean")


def cs_scale(values: pd.DataFrame) -> pd.DataFrame:
    """缩放到单位和：x / sum(|x|)。组内绝对值和为 0 时返回 0。"""
    grouped = values.groupby(level=0, sort=False)
    denom = grouped.transform(lambda s: s.abs().sum())
    return values / denom.replace(0.0, np.nan)


def cs_quantile(values: pd.DataFrame, n: int) -> pd.DataFrame:
    """横截面分位组编号 0..n-1（1-based 更直观处由调用方 +1）。"""
    if n < 2:
        raise ValueError("cs_quantile: n must be >= 2")
    return values.groupby(level=0, sort=False).transform(
        lambda s: pd.qcut(s.rank(method="first"), n, labels=False, duplicates="drop")
    )


def cs_winsorize(
    values: pd.DataFrame,
    n: float,
    *,
    method: str = "mad",
) -> pd.DataFrame:
    """横截面去极值。

    method="mad"（默认）：median ± n × 1.4826 × MAD，超出即截断
    method="quantile"   ：按 n 分位（如 n=0.01 表示 1%/99%）截断
    """
    if method == "quantile":
        lo = values.groupby(level=0, sort=False).transform(
            lambda s: s.quantile(n)
        )
        hi = values.groupby(level=0, sort=False).transform(
            lambda s: s.quantile(1.0 - n)
        )
        return values.clip(lower=lo, upper=hi)

    grouped = values.groupby(level=0, sort=False)
    med = grouped.transform("median")
    mad = grouped.transform(lambda s: (s - s.median()).abs().median())
    spread = DEFAULT_WINSORIZE_MAD_SCALE * mad
    lower = med - n * spread
    upper = med + n * spread
    return values.clip(lower=lower, upper=upper)


# ══════════════════════════════════════════════════════════
# 统一入口（factor_executor 的截面分支调用它）
# ══════════════════════════════════════════════════════════


def eval_cross_section(
    op: str,
    values: pd.DataFrame,
    *,
    n: float | None = None,
    method: str = "mad",
) -> pd.DataFrame:
    """按算子名分发。`values` 必须 index = trade_date。

    Raises:
        ValueError: 未知算子或参数缺失。
    """
    if not isinstance(values.index, pd.Index):
        raise ValueError("eval_cross_section: values must be a DataFrame with trade_date index")

    if op == "cs_rank":
        return cs_rank(values)
    if op == "cs_zscore":
        return cs_zscore(values)
    if op == "cs_demean":
        return cs_demean(values)
    if op == "cs_scale":
        return cs_scale(values)
    if op == "cs_quantile":
        if n is None:
            raise ValueError("cs_quantile requires n")
        return cs_quantile(values, int(n))
    if op == "cs_winsorize":
        if n is None:
            raise ValueError("cs_winsorize requires n")
        return cs_winsorize(values, float(n), method=method)
    raise ValueError(f"unknown cross-section operator: {op!r}")


__all__ = [
    "CROSS_SECTION_OPS",
    "cs_rank",
    "cs_zscore",
    "cs_demean",
    "cs_scale",
    "cs_quantile",
    "cs_winsorize",
    "eval_cross_section",
]
