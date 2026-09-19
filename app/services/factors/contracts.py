"""Shared, network-free helpers for AkShare DataFrame contracts."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd


class DataContractError(ValueError):
    def __init__(
        self, api_key: str, missing: Sequence[str], available: Sequence[str]
    ) -> None:
        self.api_key = api_key
        self.missing = tuple(missing)
        self.available = tuple(str(item) for item in available)
        super().__init__(
            f"{api_key} missing required fields: {', '.join(self.missing)}; "
            f"available: {', '.join(self.available)}"
        )


def resolve_contract(
    frame: pd.DataFrame,
    *,
    api_key: str,
    aliases: Mapping[str, Sequence[str]],
    required: Sequence[str],
) -> pd.DataFrame:
    """Rename known aliases to canonical fields and reject schema drift."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(aliases))
    rename: dict[str, str] = {}
    for canonical, candidates in aliases.items():
        for candidate in candidates:
            if candidate in frame.columns:
                rename[candidate] = canonical
                break
    missing = [field for field in required if field not in rename.values()]
    if missing:
        raise DataContractError(api_key, missing, list(frame.columns))
    normalized = frame.rename(columns=rename).copy()
    for field in aliases:
        if field not in normalized.columns:
            normalized[field] = pd.NA
    return normalized[list(aliases)]


def normalize_symbol(value: object) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value))
    return match.group(1) if match else str(value).strip().upper()


def normalize_dates(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.date


def normalize_numbers(series: pd.Series) -> pd.Series:
    normalized = pd.to_numeric(
        series.astype("string")
        .str.replace(",", "", regex=False)
        .str.replace("%", "", regex=False),
        errors="coerce",
    )
    return normalized.replace([np.inf, -np.inf], np.nan)
