"""Shared release contract for a production FactorModelRun.

The database has historical/legacy models that predate FactorSet traceability.
They may still be inspected, but they must not be selected for a new strategy
snapshot.  Keeping the check in one service prevents the model, score and
strategy endpoints from applying slightly different eligibility rules.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.factor_evaluation import FactorSetMember
from app.models.factor_model import FactorModelRun
from app.models.factor_governance import FactorModelMember
from app.models.factor_runtime import FactorRuntimeState
from app.models.factor_weight_snapshot import FactorWeightSnapshot
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


def model_training_mode(model: FactorModelRun) -> str:
    metadata = _json_object(model.hyperparameters_json)
    explicit = metadata.get("train_mode") or metadata.get("mode")
    if explicit:
        return str(explicit)
    if metadata.get("factor_calc_batch_id") and metadata.get("target_calc_batch_id"):
        return "warehouse"
    return "unknown"


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
    research = _json_object(model.hyperparameters_json).get('trusted_history_dataset')
    if research is not None:
        return _result(
            False, 'MODEL_RESEARCH_DATASET_ONLY',
            '代表股票池历史研究模型仅供研究对照，不能启用、发布正式评分或绑定生产策略。',
            model_run_id=model_run_id, factor_set_id=factor_set_id,
            trusted_history_dataset=research,
        )
    production = _json_object(model.hyperparameters_json).get('production_admission')
    if production is not None:
        # Qualification cannot be asserted through a client flag. A dedicated
        # issuer must bind independently observed predictions and net costs to
        # this exact candidate; pending candidates stay blocked everywhere.
        from app.services.factors.production_release import verify_release_confirmation
        try:
            confirmation = verify_release_confirmation(db, model, production)
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
            return _result(False, 'MODEL_RELEASE_EVIDENCE_CHANGED',
                '模型确认所需的资料或记录不一致，已停止使用，请检查独立验证记录。',
                model_run_id=model_run_id, factor_set_id=factor_set_id, reason=str(exc))
        if confirmation is None:
            return _result(
                False, 'MODEL_INDEPENDENT_QUALIFICATION_PENDING',
                '候选模型需要完成后续独立样本和费用验证，并由本人确认后才能启用。',
                model_run_id=model_run_id, factor_set_id=factor_set_id,
                production_admission=production,
            )
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
    training_mode = model_training_mode(model)
    if training_mode != "warehouse":
        return _result(
            False, "MODEL_TRAINING_SOURCE_UNVERIFIED",
            "演示或来源不明的训练产物不能进入正式执行，请使用真实仓库数据重新训练。",
            model_run_id=model_run_id, factor_set_id=factor_set_id,
            training_mode=training_mode,
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
    weight_rows = list(db.execute(
        select(FactorModelMember).where(FactorModelMember.model_run_id == model_run_id)
    ).scalars())
    actual_weights = {
        (weight.factor_code, int(weight.factor_version))
        for weight in weight_rows
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
    feature_versions = _json_object(model.feature_versions_json)
    try:
        recorded_versions = {
            code: int(value.get("factor_version", value.get("version"))) if isinstance(value, dict) else int(value)
            for code, value in feature_versions.items() if not code.startswith("__") and code not in {"factor_set_id", "train_mode", "mode"}
        }
    except (TypeError, ValueError):
        recorded_versions = {}
    if recorded_versions != dict(expected_members):
        return _result(
            False, "MODEL_FEATURE_VERSION_METADATA_MISMATCH",
            "模型训练特征版本与冻结因子版本不一致，请重新训练。",
            model_run_id=model_run_id, factor_set_id=factor_set_id,
        )

    hp = _json_object(model.hyperparameters_json)
    if (not hp.get("factor_calc_batch_id") or not hp.get("target_calc_batch_id")
            or not model.data_cutoff_at or int(model.sample_count or 0) <= 0
            or int(model.symbol_count or 0) <= 0 or int(model.trade_date_count or 0) <= 0):
        return _result(
            False, "MODEL_TRAINING_EVIDENCE_MISSING",
            "模型缺少真实样本数量、数据批次或截止时间，需重新训练。",
            model_run_id=model_run_id, factor_set_id=factor_set_id,
        )
    try:
        valid_weights = bool(weight_rows) and all(
            math.isfinite(float(w.coefficient)) and math.isfinite(float(w.normalized_weight))
            for w in weight_rows
        ) and math.isfinite(float(hp.get("final_intercept")))
    except (TypeError, ValueError, OverflowError):
        valid_weights = False
    if not valid_weights:
        return _result(
            False, "MODEL_WEIGHTS_INVALID", "模型权重或截距无效，需重新训练。",
            model_run_id=model_run_id, factor_set_id=factor_set_id,
        )
    artifact = db.get(FactorWeightSnapshot, model_run_id)
    if artifact is None:
        return _result(
            False, "MODEL_WEIGHT_SNAPSHOT_MISSING",
            "历史模型的不可变训练权重快照缺失，不能从现有评分补造，请重新训练。",
            model_run_id=model_run_id, factor_set_id=factor_set_id,
        )
    members = list(db.execute(
        select(FactorSetMember).where(FactorSetMember.factor_set_id == factor_set_id)
        .order_by(FactorSetMember.display_order, FactorSetMember.factor_code)
    ).scalars())
    from app.models.factor_evaluation import FactorSet
    frozen_set = db.get(FactorSet, factor_set_id)
    try:
        raw = json.loads(artifact.weights_raw_json)
        normalized = json.loads(artifact.weights_norm_json)
        feature_rows = [m for m in members if m.role == "feature"]
        norm_by_code = (
            {str(row["factor_code"]): float(row.get("weight_norm", row.get("normalized_weight")))
             for row in normalized} if normalized and isinstance(normalized[0], dict)
            else {m.factor_code: float(value) for m, value in zip(feature_rows, normalized, strict=True)}
        )
        aggregate_matches = (
            artifact.factor_set_id == factor_set_id
            and artifact.train_mode == "warehouse"
            and artifact.factor_set_content_hash == getattr(frozen_set, "content_hash", None)
            and json.loads(artifact.factor_ids_json) == [m.factor_id for m in members]
            and json.loads(artifact.factor_version_ids_json) == [m.factor_version_id for m in members]
            and json.loads(artifact.roles_json) == [m.role for m in members]
            and json.loads(artifact.constraints_json) == [m.weight_constraint for m in members]
            and json.loads(artifact.missing_strategies_json) == [m.missing_policy for m in members]
            and set(raw) == {w.factor_code for w in weight_rows}
            and set(norm_by_code) == {w.factor_code for w in weight_rows}
            and all(math.isclose(float(raw[w.factor_code]), float(w.coefficient), rel_tol=1e-10, abs_tol=1e-12)
                    and math.isclose(norm_by_code[w.factor_code], float(w.normalized_weight), rel_tol=1e-10, abs_tol=1e-12)
                    for w in weight_rows)
        )
    except (TypeError, ValueError, KeyError, IndexError, json.JSONDecodeError):
        aggregate_matches = False
    if not aggregate_matches:
        return _result(
            False, "MODEL_WEIGHT_SNAPSHOT_MISMATCH",
            "不可变训练快照与冻结因子版本或实际权重不一致，需重新训练。",
            model_run_id=model_run_id, factor_set_id=factor_set_id,
        )

    if require_runtime_active:
        runtime = db.get(FactorRuntimeState, 1)
        active_model_run_id = runtime.active_model_run_id if runtime else None
        if active_model_run_id != model_run_id or runtime.weight_mode != "ridge":
            return _result(
                False,
                "MODEL_NOT_GLOBAL_ACTIVE",
                "Model is not the current global active model",
                model_run_id=model_run_id,
                factor_set_id=factor_set_id,
                active_model_run_id=active_model_run_id,
                weight_mode=runtime.weight_mode if runtime else "manual",
            )

    return _result(
        True,
        "READY",
        f"FactorModelRun {model_run_id} is ready for a strategy snapshot",
        model_run_id=model_run_id,
        factor_set_id=factor_set_id,
        factor_set_readiness=set_readiness,
        factor_member_versions={code: version for code, version in sorted(expected_members)},
    )


def assess_factor_score_publication_readiness(
    db: Session,
    *,
    model_run_id: str,
    expected_weight_mode: str,
) -> FactorModelReadiness:
    """Permit real shadow observation without relaxing the production gate.

    Trading/strategy callers continue using assess_factor_model_readiness,
    whose runtime-active check requires ridge. Score publication must instead
    match the explicitly activated shadow or ridge model and exact mode.
    """
    readiness = assess_factor_model_readiness(db, model_run_id=model_run_id)
    if not readiness.ready:
        return readiness
    runtime = db.get(FactorRuntimeState, 1)
    active_id = runtime.active_model_run_id if runtime else None
    actual_mode = runtime.weight_mode if runtime else "manual"
    if (expected_weight_mode not in {"shadow", "ridge"}
            or active_id != model_run_id or actual_mode != expected_weight_mode):
        return _result(
            False, "MODEL_NOT_GLOBAL_ACTIVE",
            "Score publication requires the current explicitly activated model and mode",
            model_run_id=model_run_id, factor_set_id=readiness.factor_set_id,
            active_model_run_id=active_id, weight_mode=actual_mode,
            expected_weight_mode=expected_weight_mode,
        )
    return readiness


__all__ = [
    "FactorModelReadiness",
    "assess_factor_model_readiness",
    "assess_factor_score_publication_readiness",
    "model_factor_set_id",
    "model_training_mode",
]
