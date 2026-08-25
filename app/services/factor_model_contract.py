"""Shared release contract for a production FactorModelRun.

The database has historical/legacy models that predate FactorSet traceability.
They may still be inspected, but they must not be selected for a new strategy
snapshot.  Keeping the check in one service prevents the model, score and
strategy endpoints from applying slightly different eligibility rules.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.factor_evaluation import FactorSetMember
from app.models.factor_model import FactorModelRun, FactorWeightSnapshot
from app.models.factor_runtime import FactorRuntimeState
from app.services.factor_set_service import factor_set_readiness


@dataclass(frozen=True)
class FactorModelReadiness:
    """Result of checking whether a model can back a new strategy snapshot."""

    ready: bool
    code: str
    message: str
    model_run_id: str
    factor_set_id: str | None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "code": self.code,
            "message": self.message,
            "model_run_id": self.model_run_id,
            "factor_set_id": self.factor_set_id,
            "details": self.details,
        }


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _model_factor_set_metadata(model: FactorModelRun) -> dict[str, str]:
    """Return every non-empty immutable FactorSet reference with its source."""
    references: dict[str, str] = {}
    for source, raw in (
        ("hyperparameters_json", model.hyperparameters_json),
        ("feature_versions_json", model.feature_versions_json),
    ):
        payload = _json_object(raw)
        for key in ("factor_set_id", "__factor_set_id__"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                references[f"{source}.{key}"] = value.strip()
    return references


def model_factor_set_id(model: FactorModelRun) -> str | None:
    """Read the immutable FactorSet reference persisted with a model run."""
    references = _model_factor_set_metadata(model)
    for source in (
        "hyperparameters_json.factor_set_id",
        "hyperparameters_json.__factor_set_id__",
        "feature_versions_json.factor_set_id",
        "feature_versions_json.__factor_set_id__",
    ):
        value = references.get(source)
        if value:
            return value
    return None


def _result(
    ready: bool,
    code: str,
    message: str,
    *,
    model_run_id: str,
    factor_set_id: str | None,
    **details: Any,
) -> FactorModelReadiness:
    return FactorModelReadiness(
        ready=ready,
        code=code,
        message=message,
        model_run_id=model_run_id,
        factor_set_id=factor_set_id,
        details=details,
    )


def assess_factor_model_readiness(
    db: Session,
    *,
    model_run_id: str,
    expected_factor_set_id: str | None = None,
    require_runtime_active: bool = False,
) -> FactorModelReadiness:
    """Validate the exact model/FactorSet pair used by a new snapshot.

    A model is eligible only when it is validated, points to a frozen nonempty
    FactorSet and its stored weight versions exactly match the frozen feature
    members.  ``require_runtime_active`` adds the global release pointer check
    used by strategy save-and-apply and score publication.
    """
    model = db.get(FactorModelRun, model_run_id)
    if model is None:
        return _result(
            False,
            "MODEL_NOT_FOUND",
            f"FactorModelRun {model_run_id} does not exist",
            model_run_id=model_run_id,
            factor_set_id=expected_factor_set_id,
        )

    factor_set_id = model_factor_set_id(model)
    factor_set_metadata = _model_factor_set_metadata(model)
    if model.status in {"retired", "deprecated"}:
        return _result(
            False,
            "MODEL_RETIRED",
            f"FactorModelRun {model_run_id} is retired and cannot be selected",
            model_run_id=model_run_id,
            factor_set_id=factor_set_id,
            status=model.status,
        )
    if model.status != "validated":
        return _result(
            False,
            "MODEL_NOT_VALIDATED",
            f"FactorModelRun {model_run_id} status={model.status}; expected validated",
            model_run_id=model_run_id,
            factor_set_id=factor_set_id,
            status=model.status,
        )
    referenced_factor_set_ids = sorted(set(factor_set_metadata.values()))
    if len(referenced_factor_set_ids) > 1:
        return _result(
            False,
            "MODEL_FACTOR_SET_METADATA_MISMATCH",
            "FactorModelRun stores conflicting immutable factor_set_id metadata",
            model_run_id=model_run_id,
            factor_set_id=factor_set_id,
            factor_set_metadata=factor_set_metadata,
            referenced_factor_set_ids=referenced_factor_set_ids,
        )
    if not factor_set_id:
        return _result(
            False,
            "MODEL_FACTOR_SET_MISSING",
            f"FactorModelRun {model_run_id} has no immutable factor_set_id",
            model_run_id=model_run_id,
            factor_set_id=None,
        )
    if expected_factor_set_id and factor_set_id != expected_factor_set_id:
        return _result(
            False,
            "MODEL_FACTOR_SET_MISMATCH",
            "Requested FactorSet does not match the model's immutable FactorSet",
            model_run_id=model_run_id,
            factor_set_id=factor_set_id,
            expected_factor_set_id=expected_factor_set_id,
        )

    set_readiness = factor_set_readiness(db, factor_set_id, require_frozen=True)
    if not set_readiness["ready"]:
        return _result(
            False,
            "FACTOR_SET_NOT_READY",
            set_readiness["message"],
            model_run_id=model_run_id,
            factor_set_id=factor_set_id,
            factor_set_readiness=set_readiness,
        )

    expected_members = {
        (member.factor_code, int(member.factor_version))
        for member in db.execute(
            select(FactorSetMember).where(
                FactorSetMember.factor_set_id == factor_set_id,
                FactorSetMember.role == "feature",
            )
        ).scalars()
    }
    actual_weights = {
        (weight.factor_code, int(weight.factor_version))
        for weight in db.execute(
            select(FactorWeightSnapshot).where(
                FactorWeightSnapshot.model_run_id == model_run_id,
            )
        ).scalars()
    }
    if expected_members != actual_weights:
        return _result(
            False,
            "MODEL_FACTOR_VERSION_MISMATCH",
            "Model weights do not exactly match the frozen FactorSet features",
            model_run_id=model_run_id,
            factor_set_id=factor_set_id,
            expected_members=sorted(expected_members),
            actual_weights=sorted(actual_weights),
        )

    if require_runtime_active:
        runtime = db.get(FactorRuntimeState, 1)
        active_model_run_id = runtime.active_model_run_id if runtime else None
        if active_model_run_id != model_run_id:
            return _result(
                False,
                "MODEL_NOT_GLOBAL_ACTIVE",
                "Model is not the current global active model",
                model_run_id=model_run_id,
                factor_set_id=factor_set_id,
                active_model_run_id=active_model_run_id,
            )

    return _result(
        True,
        "READY",
        f"FactorModelRun {model_run_id} is ready for a strategy snapshot",
        model_run_id=model_run_id,
        factor_set_id=factor_set_id,
        factor_set_readiness=set_readiness,
    )


__all__ = [
    "FactorModelReadiness",
    "assess_factor_model_readiness",
    "model_factor_set_id",
]
