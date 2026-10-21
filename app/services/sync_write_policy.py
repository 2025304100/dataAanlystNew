"""Write-side helpers for explicitly filling missing facts without revisions."""
from __future__ import annotations

import json
from datetime import date
from typing import Any


class DateWriteCount(int):
    def __new__(cls, count: int, dates: list[date]):
        value = super().__new__(cls, count)
        value.sync_written_dates = tuple(dates)
        return value


def fill_missing_values(item: Any, values: dict[str, Any]) -> list[str]:
    filled = []
    for field, value in values.items():
        if value is not None and getattr(item, field) is None:
            setattr(item, field, value)
            filled.append(field)
    return filled


def record_missing_fill(item: Any, fields: list[str], observation: Any, source: str) -> None:
    """Keep original evidence and identify which fields the new response filled."""
    try:
        evidence = json.loads(item.raw_json or "{}")
    except (TypeError, ValueError):
        evidence = {"retained_raw": item.raw_json}
    if not isinstance(evidence, dict):
        evidence = {"retained_raw": evidence}
    prior = evidence.get("missing_fields_fills")
    fills = list(prior) if isinstance(prior, list) else []
    fills.append({"fields": fields, "source": source, "observation": observation})
    evidence["missing_fields_fills"] = fills[-8:]
    item.raw_json = json.dumps(evidence, ensure_ascii=False, default=str)
