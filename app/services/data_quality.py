"""Field quality snapshots shared by the data center and formula gates."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field as dc_field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Mapping, Sequence
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.data_quality_snapshot import DataQualitySnapshot
from app.models.data_sync_plan import DataSyncPartition, DataSyncPlan
from app.services.factors.config import get_factor_system_config# near-relative coupling: data-quality check reads scoring-system flag — audit 2026-08-30
from app.services.factors.formula_catalog import build_formula_catalog
from app.services.factors.store import FactorWarehouse


# ---------------------------------------------------------------------------
# Stage 2.6 data-integrity contract
# ---------------------------------------------------------------------------

QualityLevel = Literal["LIGHT", "MEDIUM", "HEAVY"]
DataMode = Literal["production_pit", "research"]


class DataQualityLevel(str, Enum):
    """Runtime enum for API/Pydantic callers while retaining string values."""

    LIGHT = "LIGHT"
    MEDIUM = "MEDIUM"
    HEAVY = "HEAVY"


QUALITY_LEVELS: tuple[str, str, str] = ("LIGHT", "MEDIUM", "HEAVY")


@dataclass(frozen=True)
class DataIntegrityIssue:
    """A stable, serializable reason contributing to an assessment."""

    code: str
    message: str = ""
    critical: bool = False
    details: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "critical": bool(self.critical),
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class DataIntegrityAssessment:
    """Immutable quality conclusion for one dataset/field/request window.

    ``expected_rows`` is the theoretical denominator (calendar x eligible
    symbols), never the number of rows returned by a provider.  Callers may
    persist ``to_dict()`` in an execution snapshot without depending on a
    schema migration.
    """

    dataset: str = ""
    field: str | None = None
    expected_rows: int = 0
    missing_rows: int = 0
    missing_ratio: float = 1.0
    max_consecutive_gap_days: int = 0
    quality_level: QualityLevel = "HEAVY"
    issues: tuple[DataIntegrityIssue, ...] = ()
    critical_issues: tuple[DataIntegrityIssue, ...] = ()
    assessment_id: str = dc_field(default_factory=lambda: uuid4().hex)
    data_mode: str = "production_pit"
    quarantine_required: bool = False

    @property
    def level(self) -> QualityLevel:
        """Short alias used by older readiness callers."""
        return self.quality_level

    @property
    def status(self) -> QualityLevel:
        return self.quality_level

    @property
    def missing_stock_day_ratio(self) -> float:
        return self.missing_ratio

    @property
    def longest_consecutive_gap_days(self) -> int:
        return self.max_consecutive_gap_days

    @property
    def critical_rule_hit(self) -> bool:
        return bool(self.critical_issues)

    @classmethod
    def assess(cls, dataset: str = "", **metrics: Any) -> "DataIntegrityAssessment":
        """Convenience constructor mirroring :func:`assess_data_integrity`."""
        return assess_data_integrity(dataset, **metrics)

    @property
    def production_allowed(self) -> bool:
        return self.quality_level == "LIGHT" and not self.quarantine_required

    @property
    def research_allowed(self) -> bool:
        # HEAVY is only allowed after the caller explicitly opts into an
        # isolated research task; the assessment itself must stay fail-closed.
        return self.quality_level != "HEAVY"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["issues"] = [issue.to_dict() for issue in self.issues]
        payload["critical_issues"] = [issue.to_dict() for issue in self.critical_issues]
        payload["level"] = self.quality_level
        payload["status"] = self.quality_level
        payload["missing_stock_day_ratio"] = self.missing_ratio
        payload["longest_consecutive_gap_days"] = self.max_consecutive_gap_days
        payload["critical_rule_hit"] = self.critical_rule_hit
        payload["production_allowed"] = self.production_allowed
        payload["research_allowed"] = self.research_allowed
        return payload


@dataclass(frozen=True)
class DataQualityGateResult:
    """Decision returned by the production/research quality gate."""

    allowed: bool
    quality_level: QualityLevel
    mode: str
    operation: str
    restricted_pit: bool = False
    non_pit: bool = False
    quarantine_required: bool = False
    reason_codes: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    message: str = ""

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "blocked": self.blocked,
            "quality_level": self.quality_level,
            "mode": self.mode,
            "operation": self.operation,
            "restricted_pit": self.restricted_pit,
            "non_pit": self.non_pit,
            "quarantine_required": self.quarantine_required,
            "reason_codes": list(self.reason_codes),
            "risk_flags": list(self.risk_flags),
            "message": self.message,
        }


class DataQualityBlockedError(RuntimeError):
    """Raised by ``enforce_quality_gate`` for a production-blocked run."""

    def __init__(self, result: DataQualityGateResult):
        self.result = result
        super().__init__(result.message or "data quality gate blocked the operation")


def _coerce_issue(value: Any, *, critical_default: bool = False) -> DataIntegrityIssue:
    if isinstance(value, DataIntegrityIssue):
        return value
    if isinstance(value, Mapping):
        code = str(value.get("code") or value.get("reason_code") or "DATA_QUALITY_ISSUE")
        message = str(value.get("message") or value.get("reason") or code)
        details = value.get("details")
        return DataIntegrityIssue(
            code=code,
            message=message,
            critical=bool(value.get("critical", critical_default)),
            details=dict(details) if isinstance(details, Mapping) else {},
        )
    text = str(value)
    return DataIntegrityIssue(code=text, message=text, critical=critical_default)


def assess_data_integrity(
    dataset: str = "",
    expected_rows: int | None = None,
    missing_rows: int | None = None,
    *,
    # Explicit aliases make the contract convenient for sync and report code.
    expected_records: int | None = None,
    missing_records: int | None = None,
    expected_count: int | None = None,
    missing_count: int | None = None,
    expected_stock_days: int | None = None,
    missing_stock_days: int | None = None,
    missing_ratio: float | None = None,
    missing_pct: float | None = None,
    missing_percentage: float | None = None,
    max_consecutive_gap_days: int = 0,
    max_gap_days: int | None = None,
    critical_issues: Sequence[Any] | None = None,
    critical_issue_codes: Sequence[Any] | None = None,
    issues: Sequence[Any] | None = None,
    critical: bool = False,
    field: str | None = None,
    data_mode: str = "production_pit",
    assessment_id: str | None = None,
) -> DataIntegrityAssessment:
    """Classify missingness using the §12.3.1 thresholds.

    A critical issue is always ``HEAVY``.  Otherwise the most severe of the
    missing-ratio and consecutive-gap rules wins.  The denominator is required
    to be theoretical; when it is absent the assessment is fail-closed.
    """
    expected = expected_rows
    if expected is None:
        expected = expected_records if expected_records is not None else expected_count
    if expected is None:
        expected = expected_stock_days
    missing = missing_rows
    if missing is None:
        missing = missing_records if missing_records is not None else missing_count
    if missing is None:
        missing = missing_stock_days
    if missing_ratio is None:
        pct_value = missing_pct if missing_pct is not None else missing_percentage
        if pct_value is not None:
            pct_float = float(pct_value)
            # Accept both 0.005 (ratio form) and 0.5 (percent form).
            missing_ratio = pct_float / 100.0 if abs(pct_float) > 1.0 else pct_float
    if max_gap_days is not None:
        max_consecutive_gap_days = int(max_gap_days)

    issue_values: list[DataIntegrityIssue] = []
    if issues:
        issue_values.extend(_coerce_issue(item) for item in issues)
    critical_values: list[DataIntegrityIssue] = []
    if critical_issues:
        critical_values.extend(_coerce_issue(item, critical_default=True) for item in critical_issues)
        issue_values.extend(critical_values)
    if critical_issue_codes:
        extra_critical = [_coerce_issue(item, critical_default=True) for item in critical_issue_codes]
        critical_values.extend(extra_critical)
        issue_values.extend(extra_critical)
    if critical and not critical_values:
        critical_values.append(DataIntegrityIssue("CRITICAL_DATA_RULE", "critical data rule matched", critical=True))
        issue_values.append(critical_values[-1])

    invalid_counts = False
    if expected is None or int(expected) <= 0:
        expected_int = 0 if expected is None else int(expected)
        ratio = 1.0 if missing_ratio is None else float(missing_ratio)
        invalid_counts = True
        issue_values.append(DataIntegrityIssue(
            "THEORETICAL_DENOMINATOR_MISSING", "expected_rows must be positive", critical=True
        ))
    else:
        expected_int = int(expected)
        if missing is None:
            if missing_ratio is None:
                missing_int = 0
                ratio = 0.0
            else:
                ratio = float(missing_ratio)
                missing_int = int(round(expected_int * ratio))
        else:
            missing_int = int(missing)
            ratio = float(missing_ratio) if missing_ratio is not None else missing_int / expected_int
        if missing_int < 0 or missing_int > expected_int or ratio < 0 or ratio > 1:
            invalid_counts = True
            issue_values.append(DataIntegrityIssue(
                "INVALID_MISSINGNESS_METRIC", "missing count/ratio is outside [0, 1]", critical=True
            ))
            missing_int = max(0, min(expected_int, missing_int))
            ratio = max(0.0, min(1.0, ratio))

    if expected is None or int(expected or 0) <= 0:
        missing_int = int(missing or 0)

    gap = max(0, int(max_consecutive_gap_days or 0))
    if invalid_counts or critical_values or ratio > 0.02 or gap > 10:
        level: QualityLevel = "HEAVY"
    elif ratio > 0.005 or gap >= 3:
        level = "MEDIUM"
    else:
        level = "LIGHT"

    critical_tuple = tuple(item for item in issue_values if item.critical)
    return DataIntegrityAssessment(
        dataset=str(dataset),
        field=field,
        expected_rows=expected_int,
        missing_rows=missing_int,
        missing_ratio=round(float(ratio), 8),
        max_consecutive_gap_days=gap,
        quality_level=level,
        issues=tuple(issue_values),
        critical_issues=critical_tuple,
        assessment_id=str(assessment_id or uuid4().hex),
        data_mode=str(data_mode or "production_pit"),
        quarantine_required=bool(level == "HEAVY"),
    )


# Public aliases used by data-sync integrations and older acceptance scripts.
classify_data_quality = assess_data_integrity
assess_data_quality = assess_data_integrity
classify_quality = assess_data_integrity
calculate_data_quality = assess_data_integrity
build_data_integrity_assessment = assess_data_integrity


def evaluate_quality_gate(
    assessment: DataIntegrityAssessment,
    *,
    mode: str = "production_pit",
    operation: str = "backtest",
    manual_confirmed: bool = False,
    isolated: bool = False,
) -> DataQualityGateResult:
    """Apply production/research treatment to an assessment.

    ``MEDIUM`` production backtests require explicit human confirmation and
    are marked restricted PIT.  ``HEAVY`` is only executable as an explicitly
    isolated research exploration and is marked non-PIT.
    """
    mode_norm = str(mode or "production_pit").lower()
    if mode_norm in {"production", "pit", "prod"}:
        mode_norm = "production_pit"
    operation_norm = str(operation or "backtest").lower()
    level = assessment.quality_level
    reasons = tuple(issue.code for issue in assessment.issues)
    if not reasons and level != "LIGHT":
        reasons = (f"DATA_QUALITY_{level}",)

    if mode_norm == "research":
        if level == "HEAVY" and not isolated:
            return DataQualityGateResult(
                allowed=False, quality_level=level, mode=mode_norm, operation=operation_norm,
                quarantine_required=True, reason_codes=reasons,
                risk_flags=("NON_PIT",),
                message="HEAVY data may only run in an isolated research task",
            )
        return DataQualityGateResult(
            allowed=True, quality_level=level, mode=mode_norm, operation=operation_norm,
            restricted_pit=level == "MEDIUM", non_pit=level == "HEAVY",
            quarantine_required=level == "HEAVY", reason_codes=reasons,
            risk_flags=(("DATA_QUALITY_MEDIUM",) if level == "MEDIUM" else (("NON_PIT",) if level == "HEAVY" else ())),
            message=("research data; not eligible for production" if level != "LIGHT" else ""),
        )

    # Production is fail-closed for explicit quarantine/critical issues.
    if level == "HEAVY" or assessment.quarantine_required:
        return DataQualityGateResult(
            allowed=False, quality_level=level, mode="production_pit", operation=operation_norm,
            quarantine_required=True, reason_codes=reasons,
            risk_flags=("DATA_QUALITY_HEAVY",),
            message="HEAVY data quality blocks production execution",
        )
    if level == "MEDIUM":
        can_confirm_backtest = operation_norm in {"backtest", "replay"} and manual_confirmed
        return DataQualityGateResult(
            allowed=can_confirm_backtest, quality_level=level, mode="production_pit",
            operation=operation_norm, restricted_pit=can_confirm_backtest,
            reason_codes=reasons,
            risk_flags=("RESTRICTED_PIT",),
            message=("restricted PIT; manual confirmation recorded" if can_confirm_backtest
                     else "MEDIUM data requires manual confirmation and cannot activate/simulate"),
        )
    return DataQualityGateResult(
        allowed=True, quality_level="LIGHT", mode="production_pit", operation=operation_norm,
    )


quality_gate = evaluate_quality_gate


def enforce_quality_gate(*args: Any, **kwargs: Any) -> DataQualityGateResult:
    """Evaluate and raise ``DataQualityBlockedError`` when execution is blocked."""
    result = evaluate_quality_gate(*args, **kwargs)
    if not result.allowed:
        raise DataQualityBlockedError(result)
    return result


def _canonical_payload(payload: Any) -> tuple[str, str]:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        text = json.dumps({"_raw_repr": repr(payload)}, ensure_ascii=False, sort_keys=True)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def quarantine_partition(
    db: Session | None,
    partition: Any | None = None,
    *,
    dataset: str | None = None,
    field: str | None = None,
    reason_code: str = "DATA_QUALITY_HEAVY",
    reason: str | None = None,
    raw_payload: Any = None,
    source_name: str | None = None,
    source_version: str | None = None,
    assessment: DataIntegrityAssessment | None = None,
    correlation_id: str | None = None,
    commit: bool = True,
) -> Any:
    """Move a sync partition to ``quarantined`` and retain its evidence.

    The function is intentionally usable by a worker before it has a fully
    materialized assessment.  When ``db`` is ``None`` it returns a lightweight
    dictionary-like record; with a session it persists ``DataQualityQuarantine``
    and writes a governance audit event in the same transaction.
    """
    from app.models.data_governance_quarantine import DataQualityQuarantine

    partition_obj = partition
    if db is not None and isinstance(partition, str):
        from app.models.data_sync_plan import DataSyncPartition

        partition_obj = db.get(DataSyncPartition, partition)
    partition_id = getattr(partition_obj, "id", None)
    payload_json, payload_hash = _canonical_payload(raw_payload) if raw_payload is not None else (None, None)
    dataset_hint = dataset or getattr(partition_obj, "dataset", None)
    if not dataset_hint and db is not None and getattr(partition_obj, "plan_id", None):
        plan = db.get(DataSyncPlan, getattr(partition_obj, "plan_id"))
        dataset_hint = getattr(plan, "dataset", None) if plan is not None else None
    dataset_name = str(dataset_hint or "unknown")
    reason_text = str(reason or reason_code)
    assessment_id = getattr(assessment, "assessment_id", None)
    symbol = getattr(partition_obj, "symbol", None)

    if db is None:
        return {
            "id": uuid4().hex,
            "partition_id": partition_id,
            "dataset": dataset_name,
            "field": field,
            "symbol": symbol,
            "status": "quarantined",
            "reason_code": str(reason_code),
            "reason": reason_text,
            "raw_payload_json": payload_json,
            "raw_payload_hash": payload_hash,
            "source_name": source_name,
            "source_version": source_version,
            "correlation_id": correlation_id,
        }

    if partition_obj is not None:
        partition_obj.status = "quarantined"
        # Keep a structured reason in the existing partition evidence field;
        # no existing rows are overwritten or silently marked successful.
        partition_obj.error_message = f"{reason_code}: {reason_text}"
        partition_obj.rows_written = 0

    row = DataQualityQuarantine(
        id=uuid4().hex,
        partition_id=partition_id,
        assessment_id=assessment_id,
        dataset=dataset_name,
        field=field,
        symbol=symbol,
        status="quarantined",
        reason_code=str(reason_code),
        reason=reason_text,
        raw_payload_json=payload_json,
        raw_payload_hash=payload_hash,
        source_name=source_name,
        source_version=source_version,
        correlation_id=correlation_id,
    )
    db.add(row)
    db.flush()
    try:
        from app.services.data_governance_audit import audit_data_quarantine

        audit_data_quarantine(
            db,
            quarantine_id=row.id,
            partition_id=partition_id,
            dataset=dataset_name,
            reason_code=str(reason_code),
            source_name=source_name,
            correlation_id=correlation_id,
        )
    except Exception:
        # Audit is part of the contract when the table is available.  A caller
        # using a minimal in-memory DB may not have governance tables yet; do
        # not hide the quarantine record in that compatibility case.
        db.rollback()
        raise
    if commit:
        db.commit()
    return row


def release_quarantine(
    db: Session,
    quarantine: Any,
    *,
    operator_id: str = "system",
    reason: str,
    correlation_id: str | None = None,
    commit: bool = True,
) -> Any:
    """Explicitly release a quarantine with a separate immutable audit event."""
    from app.models.data_governance_quarantine import DataQualityQuarantine

    row = quarantine
    if isinstance(quarantine, str):
        row = db.get(DataQualityQuarantine, quarantine)
    if row is None:
        raise ValueError("quarantine record not found")
    row.status = "released"
    row.released_at = _now()
    from app.services.data_governance_audit import audit_data_quarantine_release

    audit_data_quarantine_release(
        db, quarantine_id=row.id, reason=reason, operator_id=operator_id,
        correlation_id=correlation_id,
    )
    if commit:
        db.commit()
    return row


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
        # Formula-catalog coverage is already measured against the theoretical
        # stock-day denominator.  Persist the stable quality conclusion beside
        # the legacy field metrics so reports and execution gates can consume
        # it without changing the snapshot table shape.
        coverage_value = field.get("daily_coverage_p50")
        if coverage_value is None:
            coverage_value = field.get("latest_daily_coverage")
        try:
            coverage_ratio = float(coverage_value) if coverage_value is not None else None
        except (TypeError, ValueError):
            coverage_ratio = None
        assessment = assess_data_integrity(
            dataset=dataset,
            field=field_name,
            expected_rows=(1 if coverage_ratio is not None else None),
            missing_ratio=(1.0 - coverage_ratio if coverage_ratio is not None else None),
            max_consecutive_gap_days=int(field.get("max_consecutive_gap_days", 0) or 0),
            critical_issues=(
                ["FIELD_BLOCKED"]
                if str(field.get("availability", "")).lower() in {"blocked", "unavailable"}
                else []
            ),
            data_mode=str(field.get("data_mode", "production_pit")),
        )
        metrics.update({
            "quality_level": assessment.quality_level,
            "quality_assessment_id": assessment.assessment_id,
            "missing_ratio": assessment.missing_ratio,
            "max_consecutive_gap_days": assessment.max_consecutive_gap_days,
            "quarantine_required": assessment.quarantine_required,
            "quality_issues": [issue.to_dict() for issue in assessment.issues],
        })
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
            quality_level=assessment.quality_level,
            quality_assessment_id=assessment.assessment_id,
            missing_ratio=assessment.missing_ratio,
            max_consecutive_gap_days=assessment.max_consecutive_gap_days,
            quarantine_required=assessment.quarantine_required,
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
        "quality_level": row.quality_level or metrics.get("quality_level"),
        "quality_assessment_id": row.quality_assessment_id or metrics.get("quality_assessment_id"),
        "missing_ratio": row.missing_ratio if row.missing_ratio is not None else metrics.get("missing_ratio"),
        "max_consecutive_gap_days": (
            row.max_consecutive_gap_days
            if row.max_consecutive_gap_days is not None
            else metrics.get("max_consecutive_gap_days")
        ),
        "quarantine_required": bool(
            row.quarantine_required
            if row.quarantine_required is not None
            else metrics.get("quarantine_required", False)
        ),
        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
    }
