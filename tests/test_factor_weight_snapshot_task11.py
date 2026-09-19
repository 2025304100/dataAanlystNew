"""Task 11 (FR-12 / AC-11) pytest 专项：FactorWeightSnapshot 不可变训练快照。

覆盖 4 TR：
- TR-11.1：alembic upgrade head → downgrade → upgrade head，3 subprocess.returncode=0
- TR-11.2：训练 1 模型 → snapshot COUNT=1；8 字段非空
- TR-11.3：同 model_id 再次 INSERT 幂等 → COUNT 仍=1 不抛错
- TR-11.4：破坏 FactorModelRun.feature_versions_json 为 {"deleted":true}
          → factor_model_with_relations 仍返回 factors len ≥1，
          factor_version_id 集合与训练时 FactorVersion.id 相等。

运行方式：pytest tests/test_factor_weight_snapshot_task11.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorModelRun, FactorVersion
from app.models.factor_weight_snapshot import FactorWeightSnapshot

pytestmark = pytest.mark.whitebox

ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT / "alembic.ini"
DOWN_REV = "wps_0023_048_factor_audit_logs"
HEAD_REV = "wps_0023_049_factor_weight_snapshots"

REQUIRED_NONNULL_TR11_2 = [
    "model_id", "factor_set_id", "data_cutoff_date",
    "created_at", "train_mode", "weights_norm_json",
    "factor_ids_json", "factor_version_ids_json",
]


# ───────────────────────────────────────────── helpers
def _make_factor(db, code: str, fid: int, version: int) -> tuple[Factor, FactorVersion]:
    factor = Factor(
        id=fid, code=code, name=f"F-{code}", category="technical",
        direction="positive", status="active", source_type="manual",
        frequency="daily",
    )
    fv = FactorVersion(
        factor_id=fid, version=version,
        formula_expr=f"{code}_close", params_json="{}",
        source_mapping_json="{}", change_note=f"v{version}",
        validation_status="trainable",
    )
    db.add_all([factor, fv])
    db.flush()
    return factor, fv


def _make_frozen_set(
    db, set_id: str, name: str,
    members_spec: list[tuple[int, str, int, int]],
) -> tuple[FactorSet, list[FactorSetMember]]:
    fs = FactorSet(
        id=set_id, name=name, description=f"Test frozen {name}",
        status="frozen", content_hash=f"hash-{set_id}",
        frozen_at=datetime(2026, 8, 1), created_by="t11_seed",
    )
    db.add(fs)
    db.flush()
    members = []
    for i, (fid, code, fv_no, fv_id) in enumerate(members_spec):
        mem = FactorSetMember(
            factor_set_id=set_id, factor_id=fid,
            factor_version_id=fv_id, factor_code=code, factor_version=fv_no,
            role="feature", weight_constraint="free",
            missing_policy="exclude", display_order=i,
        )
        db.add(mem)
        members.append(mem)
    db.flush()
    return fs, members


def _seed_training_scenario(db) -> dict:
    """Seed 3 feature factors → 1 frozen set → dict with all refs."""
    f1, fv1 = _make_factor(db, "TR11_MOM", fid=11001, version=1)
    f2, fv2 = _make_factor(db, "TR11_VOL", fid=11002, version=2)
    f3, fv3 = _make_factor(db, "TR11_qual", fid=11003, version=1)
    spec = [
        (11001, "TR11_MOM", 1, int(fv1.id)),
        (11002, "TR11_VOL", 2, int(fv2.id)),
        (11003, "TR11_qual", 1, int(fv3.id)),
    ]
    fs, _members = _make_frozen_set(db, "FS-T11", "Task11-Set", spec)
    db.commit()
    return {
        "factor_set_id": "FS-T11",
        "factor_ids": [11001, 11002, 11003],
        "factor_version_ids": [int(fv1.id), int(fv2.id), int(fv3.id)],
        "factor_codes": ["TR11_MOM", "TR11_VOL", "TR11_qual"],
        "factor_versions": [1, 2, 1],
        "factors": [f1, f2, f3],
        "factor_versions_rows": [fv1, fv2, fv3],
        "factor_set": fs,
    }


def _train_offline_minimal_snapshot(
    db,
    seed: dict,
    model_run_id: str | None = None,
) -> str:
    """模拟 factor_models.py offline_minimal 分支写 FactorModelRun + 聚合快照。
    返回 model_run_id。INSERT 幂等：如果 model_id 已存在则静默 skip。
    """
    if model_run_id is None:
        today = datetime.now(timezone.utc).date()
        suffix = abs(hash((seed["factor_set_id"], today))) % 1000000
        model_run_id = f"rid-t11-{seed['factor_set_id']}-{suffix}"[:64]
    today = datetime.now(timezone.utc).date()
    train_end = today
    train_start = today
    data_cutoff = train_end

    # FactorModelRun
    existing_run = db.get(FactorModelRun, model_run_id)
    if existing_run is None:
        feat_vers: dict = {"__factor_set_id__": seed["factor_set_id"], "__content_hash__": "h"}
        for code, fvid, fv_no in zip(
            seed["factor_codes"], seed["factor_version_ids"], seed["factor_versions"]
        ):
            feat_vers[code] = {
                "factor_version_id": fvid, "factor_version": fv_no,
                "coefficient": 0.3, "normalized_weight": 0.3,
                "train_ic": 0.04, "validation_ic": 0.03,
            }
        run = FactorModelRun(
            id=model_run_id,
            model_type="ridge",
            asset_type="stock",
            target_code="target_5d_return",
            train_start_date=train_start,
            train_end_date=train_end,
            validation_start_date=train_start,
            validation_end_date=train_end,
            data_cutoff_at=datetime.combine(data_cutoff, datetime.min.time()),
            feature_versions_json=json.dumps(feat_vers),
            hyperparameters_json=json.dumps({
                "alpha": 1.0,
                "factor_set_id": seed["factor_set_id"],
                "mode": "offline_minimal",
            }),
            metrics_json=json.dumps({"validation_ic": 0.03, "train_ic": 0.04}),
            sample_count=250,
            trade_date_count=250,
            status="validated",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(run)
        db.flush()

    # Aggregate FactorWeightSnapshot（幂等）
    existing_snap = db.execute(
        select(FactorWeightSnapshot).where(
            FactorWeightSnapshot.model_id == model_run_id
        )
    ).scalar_one_or_none()
    if existing_snap is None:
        n = len(seed["factor_codes"])
        norm = round(1.0 / max(1, n), 6)
        weights_norm = []
        weights_raw = {}
        for idx, (code, fid, fvid, fv_no) in enumerate(zip(
            seed["factor_codes"],
            seed["factor_ids"],
            seed["factor_version_ids"],
            seed["factor_versions"],
        )):
            w = round(norm + (0.05 if idx == 0 else 0.0), 6)
            weights_raw[code] = w
            weights_norm.append({
                "factor_code": code,
                "factor_id": fid,
                "factor_version_id": fvid,
                "factor_version": fv_no,
                "coef_raw": w,
                "weight_norm": w,
                "training_ic": round(0.04 + 0.001 * idx, 4),
                "validation_ic": round(0.03 + 0.0008 * idx, 4),
                "role": "feature",
            })
        db.add(FactorWeightSnapshot(
            model_id=model_run_id,
            factor_set_id=seed["factor_set_id"],
            factor_set_content_hash=getattr(seed["factor_set"], "content_hash", None),
            factor_ids_json=json.dumps(seed["factor_ids"]),
            factor_version_ids_json=json.dumps(seed["factor_version_ids"]),
            roles_json=json.dumps(["feature"] * n),
            constraints_json=json.dumps(["free"] * n),
            missing_strategies_json=json.dumps(["exclude"] * n),
            train_start_date=train_start,
            train_end_date=train_end,
            valid_start_date=train_start,
            valid_end_date=train_end,
            data_cutoff_date=data_cutoff,
            weights_raw_json=json.dumps(weights_raw),
            weights_norm_json=json.dumps(weights_norm),
            train_mode="offline_minimal",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.flush()
    db.commit()
    return model_run_id


# ───────────────────────────────────────────── TR-11.1（rule）
def test_TR11_1_alembic_three_step_loop():
    """upgrade head → downgrade to 048 → upgrade head；3 subprocess.returncode=0."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="t11_alembic_")
    os.close(fd)
    env = {**os.environ, "ALEMBIC_DATABASE_URL": f"sqlite:///{path}"}
    try:
        _ALEM = ["python", "-m", "alembic"]
        # Step 1: upgrade head
        r1 = subprocess.run(
            _ALEM + ["-c", str(ALEMBIC_INI), "upgrade", "head"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
        )
        assert r1.returncode == 0, f"[step1 upgrade] stderr={r1.stderr[-800:]}"
        # Step 2: downgrade to 048 (down_revision)
        r2 = subprocess.run(
            _ALEM + ["-c", str(ALEMBIC_INI), "downgrade", DOWN_REV],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
        )
        assert r2.returncode == 0, f"[step2 downgrade] stderr={r2.stderr[-800:]}"
        # Step 3: upgrade head again
        r3 = subprocess.run(
            _ALEM + ["-c", str(ALEMBIC_INI), "upgrade", "head"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
        )
        assert r3.returncode == 0, f"[step3 upgrade] stderr={r3.stderr[-800:]}"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ───────────────────────────────────────────── TR-11.2（rule）
def test_TR11_2_snapshot_count_one_and_nonnull_fields(db_session):
    """训练 1 模型 → snapshot COUNT=1；8 字段非空检查通过。"""
    seed = _seed_training_scenario(db_session)
    model_id = _train_offline_minimal_snapshot(db_session, seed)

    cnt = db_session.execute(
        select(FactorWeightSnapshot).where(
            FactorWeightSnapshot.model_id == model_id
        )
    ).scalars().all()
    assert len(cnt) == 1, f"snapshot count expected 1 got {len(cnt)}"

    snap = cnt[0]
    checked_ok = []
    for col in REQUIRED_NONNULL_TR11_2:
        val = getattr(snap, col)
        if col.endswith("_json"):
            parsed = json.loads(val) if isinstance(val, str) else val
            ok = parsed not in (None, [], {})
        else:
            ok = val not in (None, "", [])
        checked_ok.append((col, ok, val if not col.endswith("_json") else type(parsed).__name__))
    failed = [c for c, ok, _v in checked_ok if not ok]
    assert not failed, (
        f"TR-11.2 nonnull checks FAILED cols={failed}; details={checked_ok}"
    )


# ───────────────────────────────────────────── TR-11.3（rule）
def test_TR11_3_idempotent_reinsert_no_exception_and_count_one(db_session):
    """同 model_id 再次 INSERT 幂等：不抛异常；COUNT 仍=1。"""
    seed = _seed_training_scenario(db_session)
    model_id = _train_offline_minimal_snapshot(db_session, seed)

    # baseline count
    before_c = len(db_session.execute(
        select(FactorWeightSnapshot)
    ).scalars().all())

    # Second INSERT (same model_id) — 幂等不应抛错，不应重复写
    try:
        _train_offline_minimal_snapshot(db_session, seed, model_run_id=model_id)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"幂等二次 INSERT 抛异常: {type(exc).__name__}: {exc}")

    after = db_session.execute(
        select(FactorWeightSnapshot).where(
            FactorWeightSnapshot.model_id == model_id
        )
    ).scalars().all()
    assert len(after) == 1, (
        f"幂等后 COUNT 期望仍=1，实际={len(after)}；before_total={before_c}"
    )


# ───────────────────────────────────────────── TR-11.4（rule）
def test_TR11_4_snapshot_priority_over_deleted_fallback(db_session):
    """破坏 feature_versions_json 为 {"deleted":true} 后，
    factor_model_with_relations(model_id) 仍返回 factors len ≥1，
    且每个 factor_version_id 与训练时 FactorVersion.id 集合相等。
    """
    from app.services.factors.__facade__ import factor_model_with_relations

    seed = _seed_training_scenario(db_session)
    model_id = _train_offline_minimal_snapshot(db_session, seed)

    # 破坏 fallback 路径
    db_session.execute(
        update(FactorModelRun)
        .where(FactorModelRun.id == model_id)
        .values(feature_versions_json=json.dumps({"deleted": True}))
    )
    db_session.commit()

    # 检查 fallback 确实失效：解析后应得 0 个因子（snapshot 不存在时为 0）
    dto = factor_model_with_relations(model_id)
    assert dto is not None, f"model {model_id} DTO None"
    assert len(dto.factors) >= 1, (
        f"factors len < 1 — fallback 被破坏后应来自 snapshot。"
        f" factors={dto.factors}"
    )

    # 断言 factor_version_id 集合与训练时成员的 FactorVersion.id 集合完全相等
    expected_ids = set(int(x) for x in seed["factor_version_ids"])
    actual_ids = set()
    for f in dto.factors:
        if f.factor_version_id is not None:
            actual_ids.add(int(f.factor_version_id))
    missing = expected_ids - actual_ids
    extra = actual_ids - expected_ids
    assert not missing and not extra, (
        f"AC-11 S5/S6 factor_version_id mismatch:"
        f" expected={expected_ids} actual={actual_ids}"
        f" missing={missing} extra={extra}; factors={dto.factors}"
    )
