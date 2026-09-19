"""因子评价面板 PIT 过滤对齐（apply_filter_mask）。

在 factor_evaluator.py 构造完 raw/winsorized/normalized 三面板后调用：

    from app.services.factors.mask_bridge import apply_filter_mask_and_counts
    out = apply_filter_mask_and_counts(db, trade_dates, symbol_ids, raw_panel, winsorized_panel, normalized_panel, cfg)
    # 使用：
    #   out.filtered_raw / out.filtered_winsorized / out.filtered_normalized —— 打上 NaN 掩码（对齐后）
    #   out.raw_count / out.filtered_count / out.effective_count —— 三计数写入 EvaluationRun
    #   out.excluded_summary —— 排除统计（用于 get_filter_events_summary 同级别的展示）

保持纯函数契约（不修改传入面板，就地使用 pandas mask）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Literal

import pandas as pd
from sqlalchemy.orm import Session

from app.services.backtest_filters.config import BacktestFilterConfig
from app.services.security_status.pit_service import SecurityStatusDTO, SecurityStatusPitService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilterMaskResult:
    # 面板（对齐后，被 mask 的行置 NaN；与原面板形状完全相同，索引对齐）
    filtered_raw: pd.DataFrame
    filtered_winsorized: pd.DataFrame | None
    filtered_normalized: pd.DataFrame | None
    # 三计数
    raw_count: int                # 原面板有效行数（non-null 样本）
    filtered_count: int           # PIT mask 之后的有效行数（分母，未考虑目标标签缺失）
    effective_count: int          # filtered_count - 目标标签额外 NaN 行数（若传入 target_panel）
    # 排除摘要（rule_code → count）
    excluded_by_rule: dict[str, int]
    # PIT 状态按 (date, symbol) 映射，方便后续审计
    pit_status_map: dict[tuple[date, int], SecurityStatusDTO]


def _pit_status_iter(
    all_pit: dict[int, dict[date, SecurityStatusDTO]],
) -> Iterable[tuple[date, int, SecurityStatusDTO]]:
    for sid, daily in all_pit.items():
        for td, dto in daily.items():
            yield td, sid, dto


def apply_filter_mask_and_counts(
    db: Session,
    trade_dates: list[date],
    symbol_ids: list[int],
    raw_panel: pd.DataFrame,              # MultiIndex (trade_date, symbol_id) or columns as dates
    winsorized_panel: pd.DataFrame | None,
    normalized_panel: pd.DataFrame | None,
    cfg: BacktestFilterConfig,
    target_panel: pd.DataFrame | None = None,   # 若传，计算 effective_count = filtered_count - 额外缺失
    panel_index_mode: Literal["multiindex_rows", "dates_as_columns"] = "multiindex_rows",
) -> FilterMaskResult:
    """对三面板（raw/winsorized/normalized）打 PIT 可交易性掩码。

    纯函数：不会 mutate 传入的面板。
    """
    # Step 1: 按 trade_date × symbol_id 查 PIT 状态
    pit_by_symbol_dates: dict[int, dict[date, SecurityStatusDTO]] = {}
    for td in trade_dates:
        batch = SecurityStatusPitService.status_batch(db, list(symbol_ids), td)
        for sid, dto in batch.items():
            pit_by_symbol_dates.setdefault(sid, {})[td] = dto

    # 构造 (trade_date, symbol_id) → status 映射
    pit_flat: dict[tuple[date, int], SecurityStatusDTO] = {}
    excluded_counter: dict[str, int] = {}

    def _bump(code: str, n=1) -> None:
        excluded_counter[code] = excluded_counter.get(code, 0) + n

    # Step 2: 判断哪些 (date, symbol) 应 mask（不可交易）
    mask_set: set[tuple[date, int]] = set()
    for td in trade_dates:
        for sid in symbol_ids:
            dto = pit_by_symbol_dates.get(sid, {}).get(td)
            if dto is None:
                if cfg.production_fidelity:
                    mask_set.add((td, sid))
                    _bump("STATUS_UNKNOWN_MASKED")
                continue
            pit_flat[(td, sid)] = dto
            status = dto.status
            if status == "UNKNOWN" and cfg.production_fidelity:
                mask_set.add((td, sid)); _bump("STATUS_UNKNOWN_MASKED")
                continue
            if cfg.filter_new_listing:
                age = dto.listing_age_calendar_days
                if age is None or age < cfg.min_listing_age_calendar_days:
                    mask_set.add((td, sid)); _bump("NEW_LISTING_MASKED"); continue
            if cfg.filter_st and status == "ST":
                mask_set.add((td, sid)); _bump("ST_MASKED"); continue
            if cfg.filter_suspended and status == "SUSPENDED":
                mask_set.add((td, sid)); _bump("SUSPENDED_MASKED"); continue
            if cfg.delisting_period_excluded and status == "DELISTING_PERIOD":
                mask_set.add((td, sid)); _bump("DELISTING_PERIOD_MASKED"); continue
            if status == "DELISTED":
                mask_set.add((td, sid)); _bump("DELISTED_MASKED"); continue

    # Step 3: 对面板打 NaN mask（不修改原对象，.copy() 后再 mask）
    def _apply_mask(panel: pd.DataFrame | None) -> pd.DataFrame | None:
        if panel is None:
            return None
        if panel_index_mode == "multiindex_rows":
            # rows = MultiIndex (trade_date, symbol_id)
            df = panel.copy()
            if not df.empty and mask_set:
                if isinstance(df.index, pd.MultiIndex) and df.index.nlevels == 2:
                    # 判断哪些行要 mask
                    mask_rows: list[bool] = []
                    for idx_val in df.index:
                        d, s = idx_val[0], idx_val[1]
                        # 规范化 date / datetime
                        d0 = d.date() if hasattr(d, "date") else d
                        try:
                            d0 = pd.to_datetime(d0).date()
                        except Exception:
                            pass
                        try:
                            s0 = int(s)
                        except Exception:
                            s0 = s
                        mask_rows.append((d0, s0) in mask_set)
                    if any(mask_rows):
                        df.loc[mask_rows, :] = float("nan")
            return df
        # dates_as_columns：rows = symbol_ids，columns = trade_dates
        df = panel.copy()
        for col in df.columns:
            d = col if isinstance(col, date) else pd.to_datetime(col).date()
            for idx_label in df.index:
                try:
                    sid = int(idx_label)
                except Exception:
                    sid = idx_label
                if (d, sid) in mask_set:
                    df.loc[idx_label, col] = float("nan")
        return df

    f_raw = _apply_mask(raw_panel)
    f_win = _apply_mask(winsorized_panel)
    f_norm = _apply_mask(normalized_panel)

    # Step 4: 三计数（基于 filtered_raw）
    raw_count = int(raw_panel.notna().sum().sum())
    filtered_count = int(f_raw.notna().sum().sum()) if f_raw is not None else 0

    # effective_count：若传 target_panel，再算其 masked 后的非空样本数
    effective_count = filtered_count
    if target_panel is not None:
        t_masked = _apply_mask(target_panel)
        if t_masked is not None:
            effective_count = int(t_masked.notna().sum().sum())

    return FilterMaskResult(
        filtered_raw=f_raw,
        filtered_winsorized=f_win,
        filtered_normalized=f_norm,
        raw_count=raw_count,
        filtered_count=filtered_count,
        effective_count=effective_count,
        excluded_by_rule=excluded_counter,
        pit_status_map=pit_flat,
    )
