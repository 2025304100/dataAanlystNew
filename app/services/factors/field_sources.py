"""Physical input columns shared by coverage diagnostics and DSL execution."""
from __future__ import annotations


_FIELD_COLUMNS = {
    ("raw_etf_indicators", "etf_premium_discount"): "premium_discount",
    ("raw_etf_indicators", "etf_tracking_error"): "tracking_error",
    ("raw_etf_indicators", "etf_fund_size"): "fund_size",
}


def physical_field_column(table: str, field: str) -> str:
    return _FIELD_COLUMNS.get((table, field), field)
