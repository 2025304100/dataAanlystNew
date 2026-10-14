"""Complete immutable warehouse evidence for isolated release-contract fixtures."""
import json
from datetime import date, datetime

from sqlalchemy import select

from app.models.factor import Factor
from app.models.factor_model import FactorVersion
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_weight_snapshot import FactorWeightSnapshot


def add_training_proof(db, model, factor_set_id=None):
    hp = json.loads(model.hyperparameters_json or "{}")
    fsid = factor_set_id or hp.get("factor_set_id") or f"fs-{model.id}"
    fs = db.get(FactorSet, fsid)
    if fs is None:
        fs = FactorSet(id=fsid, name=fsid, status="frozen", content_hash="a" * 32,
                       frozen_at=datetime(2026, 8, 21), created_by="test")
        db.add(fs)
        db.flush()
        for index, weight in enumerate(model.weights):
            factor = db.scalar(select(Factor).where(Factor.code == weight.factor_code))
            if factor is None:
                factor = Factor(code=weight.factor_code, name=weight.factor_code, category="fundamental",
                                direction="higher_better", status="active", lifecycle_status="active",
                                is_active=1, origin="test")
                db.add(factor)
                db.flush()
            version = db.scalar(select(FactorVersion).where(FactorVersion.factor_id == factor.id,
                                                           FactorVersion.version == weight.factor_version))
            if version is None:
                version = FactorVersion(factor_id=factor.id, version=weight.factor_version,
                                        formula_expr="1", validation_status="valid")
                db.add(version)
                db.flush()
            db.add(FactorSetMember(factor_set_id=fsid, factor_id=factor.id, factor_version_id=version.id,
                                  factor_code=weight.factor_code, factor_version=weight.factor_version,
                                  role="feature", display_order=index, missing_policy="exclude"))
        db.flush()
    hp.update(factor_set_id=fsid, mode="warehouse", train_mode="warehouse",
              factor_calc_batch_id=f"test-factors-{model.id}", target_calc_batch_id=f"test-targets-{model.id}",
              final_intercept=0.0)
    model.hyperparameters_json = json.dumps(hp)
    if not model.feature_versions_json or model.feature_versions_json == "{}":
        model.feature_versions_json = json.dumps({**{w.factor_code: w.factor_version for w in model.weights}, "__factor_set_id__": fsid})
    model.data_cutoff_at = datetime(2026, 8, 31)
    members = list(db.scalars(select(FactorSetMember).where(FactorSetMember.factor_set_id == fsid)
                             .order_by(FactorSetMember.display_order, FactorSetMember.factor_code)))
    if db.get(FactorWeightSnapshot, model.id) is None:
        db.add(FactorWeightSnapshot(
            model_id=model.id, factor_set_id=fsid, factor_set_content_hash=fs.content_hash,
            factor_ids_json=json.dumps([m.factor_id for m in members]),
            factor_version_ids_json=json.dumps([m.factor_version_id for m in members]),
            roles_json=json.dumps([m.role for m in members]),
            constraints_json=json.dumps([m.weight_constraint for m in members]),
            missing_strategies_json=json.dumps([m.missing_policy for m in members]),
            data_cutoff_date=date(2026, 8, 31), train_mode="warehouse",
            weights_raw_json=json.dumps({w.factor_code: w.coefficient for w in model.weights}),
            weights_norm_json=json.dumps([{ "factor_code": w.factor_code, "weight_norm": w.normalized_weight }
                                         for w in model.weights]),
        ))
    db.flush()
    return model
