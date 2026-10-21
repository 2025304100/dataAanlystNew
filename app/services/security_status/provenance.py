"""Shared rules for rejecting placeholders as historical status evidence."""
from __future__ import annotations

import re

from sqlalchemy import func, select

from app.models.security_status import SecurityStatusDaily


def is_unverified_status_source(source: str | None) -> bool:
    normalized = (source or "").strip().lower()
    if not normalized:
        return True
    if any(token in normalized for token in (
        "unknown", "missing", "inferred", "snapshot", "fixture", "mock", "sample",
        "current", "realtime", "raw_universe",
        "示例", "测试", "未知", "占位", "当前", "快照", "推断",
    )):
        return True
    return bool(re.search(r"(^|[^a-z])(?:test|demo)(?:[^a-z]|$)", normalized))


def is_verified_status_row(row, trade_date) -> bool:
    """Complete dated row with a declared, non-placeholder historical source."""
    return (not is_unverified_status_source(row.status_source)
        and row.listing_date is not None and row.listing_date <= trade_date)


def latest_verified_status_date(db):
    candidates = db.execute(select(
        SecurityStatusDaily.status_source, func.max(SecurityStatusDaily.trade_date),
    ).where(SecurityStatusDaily.listing_date <= SecurityStatusDaily.trade_date)
        .group_by(SecurityStatusDaily.status_source))
    dates = [latest for source, latest in candidates if not is_unverified_status_source(source)]
    return max(dates) if dates else None
