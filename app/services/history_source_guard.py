"""Protect single-source history rows until field-level provenance is available."""
from __future__ import annotations

import math


VALUE_FIELDS = ("open", "high", "low", "close", "volume", "amount", "turnover_rate")


def require_compatible_history_source(existing, incoming: dict, source: str) -> None:
    """Never attribute retained old fields to a fallback provider.

    A complete replacement can upgrade a legacy unlabelled observation; its
    availability time must advance. An established source can only be updated
    by that source. Cross-source corrections need separately reviewed evidence,
    not automatic fallback priority or a guess based on the numeric value.
    """
    if existing.source == source:
        return
    legacy_upgrade = existing.source in {None, "", "akshare"} and source not in {"", "akshare"}
    complete = all(
        getattr(existing, field) is None or
        (incoming.get(field) is not None and math.isfinite(float(incoming[field])))
        for field in VALUE_FIELDS
    )
    if legacy_upgrade and complete:
        return
    raise ValueError(
        f"history_source_conflict: row_id={existing.id}, trade_date={existing.trade_date}, "
        f"existing={existing.source!r}, incoming={source!r}; existing row preserved"
    )
