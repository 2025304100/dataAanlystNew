"""WP8-01: 双读对比工具。

对齐 docs/专业因子库开发计划.md §12.1 第 5-6 步和 spec.md「迁移切换与回退」Scenario: 双读数值一致后才开放写。

核心能力：
- 对相同 ``data_cutoff_at`` 双跑旧路径（``factor_engine.calculate_stock_factors``）
  和 FactorSet 路径（``factor_set_executor.execute_factor_set``）
- 对比行数、缺失、归一化值
- 返回结构化对比报告，判定是否在约定容差内一致

安全约束：
- 两条路径使用独立的 ``calc_batch_id``（factor_values 主键含 calc_batch_id，不互相覆盖）
- 不删除已有 warehouse 数据
- 报告包含逐因子差异详情，便于排障
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.services.factors.factor_engine import calculate_stock_factors
from app.services.factors.factor_set_executor import execute_factor_set
from app.services.factors.store import FactorWarehouse


# ── 常量 ──────────────────────────────────────────────────

DEFAULT_FACTOR_SET_ID = "legacy-system-v1"
DEFAULT_TOLERANCE = 1e-6


# ── 结果 dataclass ────────────────────────────────────────


@dataclass(frozen=True)
class FactorComparisonEntry:
    """单个因子的对比结果。"""

    factor_code: str
    legacy_rows: int
    factor_set_rows: int
    row_count_match: bool
    legacy_missing: int
    factor_set_missing: int
    missing_match: bool
    compared_pairs: int
    max_abs_diff: float | None
    mean_abs_diff: float | None
    within_tolerance: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_code": self.factor_code,
            "legacy_rows": self.legacy_rows,
            "factor_set_rows": self.factor_set_rows,
            "row_count_match": self.row_count_match,
            "legacy_missing": self.legacy_missing,
            "factor_set_missing": self.factor_set_missing,
            "missing_match": self.missing_match,
            "compared_pairs": self.compared_pairs,
            "max_abs_diff": self.max_abs_diff,
            "mean_abs_diff": self.mean_abs_diff,
            "within_tolerance": self.within_tolerance,
        }


@dataclass
class DualReadReport:
    """双读对比报告。"""

    trade_date: date | None
    factor_set_id: str
    tolerance: float
    legacy_batch_id: str
    factor_set_batch_id: str
    legacy_rows_written: int
    factor_set_total_members: int
    factor_set_success_count: int
    factor_set_failed_count: int
    legacy_factor_codes: list[str]
    factor_set_factor_codes: list[str]
    factor_codes_only_in_legacy: list[str]
    factor_codes_only_in_factor_set: list[str]
    entries: list[FactorComparisonEntry] = field(default_factory=list)
    overall_consistent: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
            "factor_set_id": self.factor_set_id,
            "tolerance": self.tolerance,
            "legacy_batch_id": self.legacy_batch_id,
            "factor_set_batch_id": self.factor_set_batch_id,
            "legacy_rows_written": self.legacy_rows_written,
            "factor_set_total_members": self.factor_set_total_members,
            "factor_set_success_count": self.factor_set_success_count,
            "factor_set_failed_count": self.factor_set_failed_count,
            "legacy_factor_codes": self.legacy_factor_codes,
            "factor_set_factor_codes": self.factor_set_factor_codes,
            "factor_codes_only_in_legacy": self.factor_codes_only_in_legacy,
            "factor_codes_only_in_factor_set": self.factor_codes_only_in_factor_set,
            "entries": [e.to_dict() for e in self.entries],
            "overall_consistent": self.overall_consistent,
            "errors": self.errors,
        }


# ── 核心实现 ──────────────────────────────────────────────


def _read_batch_frame(
    warehouse: FactorWarehouse,
    calc_batch_id: str,
) -> pd.DataFrame:
    """从 warehouse 读取指定 calc_batch_id 的 factor_values。"""
    warehouse.initialize()
    with warehouse.connection(read_only=True) as conn:
        df = conn.execute(
            """
            SELECT
                symbol,
                trade_date,
                factor_code,
                factor_version,
                normalized_value,
                raw_value
            FROM factor_values
            WHERE calc_batch_id = ?
            """,
            [calc_batch_id],
        ).fetchdf()

    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def _compare_factor(
    legacy_df: pd.DataFrame,
    factor_set_df: pd.DataFrame,
    factor_code: str,
    tolerance: float,
) -> FactorComparisonEntry:
    """对比单个因子的旧路径和新路径结果。"""
    legacy_sub = legacy_df[legacy_df["factor_code"] == factor_code]
    fs_sub = factor_set_df[factor_set_df["factor_code"] == factor_code]

    legacy_rows = len(legacy_sub)
    fs_rows = len(fs_sub)

    legacy_missing = int(legacy_sub["normalized_value"].isna().sum())
    fs_missing = int(fs_sub["normalized_value"].isna().sum())

    # 按 (symbol, trade_date) 对齐
    merged = legacy_sub.merge(
        fs_sub,
        on=["symbol", "trade_date"],
        how="inner",
        suffixes=("_legacy", "_fs"),
    )

    compared_pairs = len(merged)
    max_abs_diff: float | None = None
    mean_abs_diff: float | None = None
    within_tolerance = True

    if compared_pairs > 0:
        legacy_vals = pd.to_numeric(merged["normalized_value_legacy"], errors="coerce")
        fs_vals = pd.to_numeric(merged["normalized_value_fs"], errors="coerce")
        diff = (legacy_vals - fs_vals).abs()
        finite_mask = diff.replace([np.inf, -np.inf], np.nan).notna()
        finite_diffs = diff[finite_mask]

        if len(finite_diffs) > 0:
            max_abs_diff = float(finite_diffs.max())
            mean_abs_diff = float(finite_diffs.mean())
            within_tolerance = bool(max_abs_diff <= tolerance)

    return FactorComparisonEntry(
        factor_code=factor_code,
        legacy_rows=legacy_rows,
        factor_set_rows=fs_rows,
        row_count_match=legacy_rows == fs_rows,
        legacy_missing=legacy_missing,
        factor_set_missing=fs_missing,
        missing_match=legacy_missing == fs_missing,
        compared_pairs=compared_pairs,
        max_abs_diff=max_abs_diff,
        mean_abs_diff=mean_abs_diff,
        within_tolerance=within_tolerance,
    )


def run_dual_read_compare(
    db: Session,
    warehouse: FactorWarehouse,
    *,
    trade_date: date | None = None,
    factor_set_id: str = DEFAULT_FACTOR_SET_ID,
    tolerance: float = DEFAULT_TOLERANCE,
) -> DualReadReport:
    """双跑旧路径和 FactorSet 路径并对比结果。

    约束：
    - FactorSet 必须为 frozen 状态
    - 两条路径使用独立 calc_batch_id，不互相覆盖
    - 对比维度：行数、缺失数、归一化值差（max abs diff <= tolerance）

    返回：DualReadReport
    """
    from uuid import uuid4

    legacy_batch_id = f"dual-read-legacy-{uuid4().hex[:8]}"
    fs_batch_id = f"dual-read-fs-{uuid4().hex[:8]}"

    errors: list[str] = []

    # 1. 旧路径
    try:
        legacy_result = calculate_stock_factors(
            warehouse,
            start_date=trade_date,
            end_date=trade_date,
            calc_batch_id=legacy_batch_id,
        )
        legacy_rows_written = legacy_result.rows_written
    except Exception as exc:
        errors.append(f"legacy_path_failed: {exc}")
        legacy_result = None
        legacy_rows_written = 0

    legacy_df = (
        _read_batch_frame(warehouse, legacy_batch_id)
        if legacy_result is not None
        else pd.DataFrame()
    )

    # 2. FactorSet 路径
    fs_total = 0
    fs_success = 0
    fs_failed = 0
    try:
        fs_result = execute_factor_set(
            db,
            warehouse,
            factor_set_id=factor_set_id,
            trade_date=trade_date,
            calc_batch_id=fs_batch_id,
        )
        fs_total = fs_result.total_members
        fs_success = fs_result.success_count
        fs_failed = fs_result.failed_count
    except Exception as exc:
        errors.append(f"factor_set_path_failed: {exc}")

    fs_df = _read_batch_frame(warehouse, fs_batch_id)

    # 3. 对比
    legacy_codes = sorted(legacy_df["factor_code"].unique().tolist()) if not legacy_df.empty else []
    fs_codes = sorted(fs_df["factor_code"].unique().tolist()) if not fs_df.empty else []

    only_legacy = [c for c in legacy_codes if c not in fs_codes]
    only_fs = [c for c in fs_codes if c not in legacy_codes]
    common_codes = [c for c in legacy_codes if c in fs_codes]

    entries: list[FactorComparisonEntry] = []
    for code in common_codes:
        entries.append(_compare_factor(legacy_df, fs_df, code, tolerance))

    # 4. 整体一致性判定
    overall_consistent = (
        len(errors) == 0
        and len(only_legacy) == 0
        and len(only_fs) == 0
        and len(entries) > 0
        and all(
            e.row_count_match and e.missing_match and e.within_tolerance
            for e in entries
        )
    )

    return DualReadReport(
        trade_date=trade_date,
        factor_set_id=factor_set_id,
        tolerance=tolerance,
        legacy_batch_id=legacy_batch_id,
        factor_set_batch_id=fs_batch_id,
        legacy_rows_written=legacy_rows_written,
        factor_set_total_members=fs_total,
        factor_set_success_count=fs_success,
        factor_set_failed_count=fs_failed,
        legacy_factor_codes=legacy_codes,
        factor_set_factor_codes=fs_codes,
        factor_codes_only_in_legacy=only_legacy,
        factor_codes_only_in_factor_set=only_fs,
        entries=entries,
        overall_consistent=overall_consistent,
        errors=errors,
    )
