from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.scan import ScanResult

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_datetime(value):
    """安全地将值转换为 datetime 类型，处理 MySQL 返回的字符串。"""
    from datetime import datetime
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def cleanup_expired_discovery_results(db: Session) -> dict:
    """Delete non-frozen ScanResults whose age exceeds valid_days.

    Returns a summary dict with deleted counts.
    """
    now = _now()

    # Find all non-frozen results that have exceeded their valid_days
    expired_rows = (
        db.execute(
            select(ScanResult)
            .where(ScanResult.is_frozen == 0)
            .where(
                # age >= valid_days  =>  created_at + valid_days days <= now
                # Using raw SQL date arithmetic for SQLite compatibility
            )
        )
        .scalars()
        .all()
    )

    expired_ids: list[int] = []
    for row in expired_rows:
        age_days = max(0, (now - _safe_datetime(row.created_at)).days)
        if age_days >= row.valid_days:
            expired_ids.append(row.id)

    if not expired_ids:
        logger.info("Cleanup: no expired discovery results to remove")
        return {"deleted": 0, "skipped_frozen": 0}

    # Delete expired scan_results
    result = db.execute(
        delete(ScanResult).where(ScanResult.id.in_(expired_ids))
    )
    deleted_count = result.rowcount
    db.commit()

    logger.info("Cleanup: removed %d expired discovery results", deleted_count)
    return {"deleted": deleted_count, "skipped_frozen": 0}
