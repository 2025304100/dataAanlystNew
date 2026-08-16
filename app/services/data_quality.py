"""Field quality snapshots shared by the data center and formula gates."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.data_quality_snapshot import DataQualitySnapshot
from app.models.data_sync_plan import DataSyncPartition, DataSyncPlan
from app.services.factors.config import get_factor_system_config
from app.services.factors.formula_catalog import build_formula_catalog
from app.services.factors.store import FactorWarehouse


_FIELD_DATASETS = {
    "pe_ttm": "fundamental", "pb": "fundamental", "roe_ttm": "financial",
    "main_net_inflow": "capital_flow", "lhb_institution_net": "lhb",
    "hot_rank_pct": "hot_rank", "proxy_score": "tail_proxy",
    "etf_premium_discount": "etf", "etf_tracking_error": "etf",
    "etf_fund_size": "etf",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _latest_failure_by_dataset(db: Session) -> dict[str, str]:
    rows = db.execute(
        select(DataSyncPlan.dataset, DataSyncPartition.error_message)
        .join(DataSyncPartition, DataSyncPartition.plan_id == DataSyncPlan.id)
        .where(
            DataSyncPartition.status == "failed",
            DataSyncPartition.error_message.is_not(None),
        )
        .order_by(desc(DataSyncPartition.updated_at))
    ).all()
    result: dict[str, str] = {}
    for dataset, reason in rows:
        result.setdefault(str(dataset), str(reason))
    return result


def capture_field_quality_snapshots(
    db: Session, *, trigger: str = "manual"
) -> dict[str, Any]:
    """Persist the current warehouse evidence without rewriting source data."""
    config = get_factor_system_config(db)
    catalog = build_formula_catalog(FactorWarehouse(config.warehouse_path))
    failures = _latest_failure_by_dataset(db)
    captured_at = _now()
    saved = 0
    for field in catalog.get("fields", []):
        field_name = str(field.get("key", ""))
        dataset = _FIELD_DATASETS.get(field_name, "market")
        metrics = {
            key: field.get(key)
            for key in (
                "daily_coverage_p50", "daily_coverage_p90", "latest_daily_coverage",
                "continuity_days", "derived", "derived_from", "status_reason",
                "preview_enabled", "evaluation_enabled", "data_mode",
            )
        }
        db.add(DataQualitySnapshot(
            id=uuid4().hex,
            dataset=dataset,
            field=field_name,
            readiness=str(field.get("availability", "unknown")),
            evaluation_mode=str(field.get("evaluation_mode", field.get("data_mode", "continuous"))),
            row_count=int(field.get("table_rows", 0) or 0),
            nonnull_rows=int(field.get("nonnull_rows", 0) or 0),
            distinct_symbols=int(field.get("distinct_symbols", 0) or 0),
            distinct_dates=int(field.get("distinct_dates", 0) or 0),
            first_date=str(field["first_date"]) if field.get("first_date") else None,
            latest_date=str(field["latest_date"]) if field.get("latest_date") else None,
            failure_reason=failures.get(dataset),
            metrics_json=json.dumps({"trigger": trigger, **metrics}, ensure_ascii=False, default=str),
            captured_at=captured_at,
        ))
        saved += 1
    db.commit()
    return {"captured_at": captured_at.isoformat(), "fields": saved, "trigger": trigger}


def list_latest_field_quality(db: Session) -> dict[str, Any]:
    rows = db.execute(
        select(DataQualitySnapshot).order_by(desc(DataQualitySnapshot.captured_at))
    ).scalars().all()
    latest: dict[str, DataQualitySnapshot] = {}
    for row in rows:
        latest.setdefault(row.field, row)
    return {
        "captured_at": max((row.captured_at for row in latest.values()), default=None),
        "fields": [_serialize(row) for row in sorted(latest.values(), key=lambda item: item.field)],
    }


def list_field_quality_history(db: Session, field: str, *, limit: int = 30) -> dict[str, Any]:
    rows = db.execute(
        select(DataQualitySnapshot)
        .where(DataQualitySnapshot.field == field)
        .order_by(desc(DataQualitySnapshot.captured_at))
        .limit(limit)
    ).scalars().all()
    return {"field": field, "snapshots": [_serialize(row) for row in reversed(rows)]}


def _serialize(row: DataQualitySnapshot) -> dict[str, Any]:
    try:
        metrics = json.loads(row.metrics_json or "{}")
    except json.JSONDecodeError:
        metrics = {}
    return {
        "id": row.id, "dataset": row.dataset, "field": row.field,
        "readiness": row.readiness, "evaluation_mode": row.evaluation_mode,
        "row_count": row.row_count, "nonnull_rows": row.nonnull_rows,
        "distinct_symbols": row.distinct_symbols, "distinct_dates": row.distinct_dates,
        "first_date": row.first_date, "latest_date": row.latest_date,
        "failure_reason": row.failure_reason, "metrics": metrics,
        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
    }
