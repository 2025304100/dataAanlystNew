"""Task 10 (FR-11) — 历史模型迁移报告 + 审计签名写回（仅 case2 允许）。

测试用例：
 - TR-10.1（rule）：8 条模型 seed → 3 already_linked / 3 match_candidates / 2 unlinked_unknown。
 - TR-10.2（rule）：POST 申请 case2 成功（audit +1）、POST unknown case HTTP 400 且 DB 不变。

运行：
    pytest tests/test_factor_migration_task10.py -v
"""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import select

from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorModelRun, FactorVersion
from app.models.factor_runtime import FactorAuditLog

pytestmark = pytest.mark.whitebox


# ──────────────────────────────────────────────────────────────── helpers

def _make_factor(db, code: str, fid: int, version: int) -> tuple[Factor, FactorVersion]:
    factor = Factor(
        id=fid,
        code=code,
        name=f"Factor-{code}",
        category="fundamental",
        direction="positive",
        status="active",
        source_type="akshare",
        frequency="daily",
    )
    fv = FactorVersion(
        factor_id=fid,
        version=version,
        formula_expr=f"{code}_raw",
        params_json="{}",
        source_mapping_json="{}",
        change_note=f"v{version}",
    )
    db.add_all([factor, fv])
    db.flush()
    return factor, fv


def _make_frozen_set(
    db,
    set_id: str,
    name: str,
    # (factor_id, factor_code, factor_version, factor_version_row_id)
    members_spec: list[tuple[int, str, int, int]],
) -> FactorSet:
    fs = FactorSet(
        id=set_id,
        name=name,
        description=f"frozen set {name}",
        status="frozen",
        frozen_at=datetime(2026, 1, 1),
        created_by="test_seed",
    )
    db.add(fs)
    db.flush()
    for idx, (factor_id, factor_code, factor_version, factor_version_id) in enumerate(members_spec):
        db.add(FactorSetMember(
            factor_set_id=set_id,
            factor_id=factor_id,
            factor_version_id=factor_version_id,  # 真实 FactorVersion.id（FK 必须存在）
            factor_code=factor_code,
            factor_version=factor_version,
            role="feature",
            display_order=idx,
            missing_policy="exclude",
        ))
    db.flush()
    return fs


def _fv_for(pairs: list[tuple[str, int]], *, fsid_link: str | None = None) -> str:
    """构造训练格式的 feature_versions_json（与 train 路由 L217-L230 一致）。"""
    body: dict = {}
    for code, version in pairs:
        body[code] = {
            "factor_id": 1,
            "factor_version_id": 1,
            "factor_version": version,
            "role": "feature",
            "missing_policy": "exclude",
        }
    if fsid_link:
        body["__factor_set_id__"] = fsid_link
        # 加个 hash 占位，避免 key 过滤
        body["__content_hash__"] = "deadbeef"
    return json.dumps(body, ensure_ascii=True)


def _hyper(*, fsid: str | None = None, mode: str = "offline_minimal") -> str:
    body = {"alpha": 1.0, "window_days": 250, "validation_days": 50, "mode": mode}
    if fsid:
        body["factor_set_id"] = fsid
    return json.dumps(body, ensure_ascii=True)


def _seed_8_models(db) -> list[FactorModelRun]:
    """TR-10.1 seed：

    分类目标：
      already_linked (3) ：m1, m2, m3 （hyperparameters 或 feature_versions 中有 fsid）
      match_candidates (3)：m4/m5/m6 （空 fsid，但 feature_versions 分别唯一匹配 FS-U1/FS-U2/FS-U3）
      unlinked_unknown (2)：m7 多个匹配；m8 0 匹配
    """
    # ── Factor / FactorVersion seed ──
    # 每个 code 创建一个 FactorVersion，flush 后拿到真实 FK id
    codes = ["ep_ttm", "pb_ratio", "roe_yoy", "momentum_20d", "lowvol_60d", "quality_alt"]
    # version_map[code] = (factor_id, version_num, fv_row_id)
    version_map: dict[str, tuple[int, int, int]] = {}
    for i, code in enumerate(codes, start=1):
        _f, _fv = _make_factor(db, code, fid=i, version=i)
        version_map[code] = (i, i, _fv.id)

    alt_codes = [f"{c}_alt" for c in codes]
    for i, code in enumerate(alt_codes, start=101):
        _f, _fv = _make_factor(db, code, fid=i, version=1)
        version_map[code] = (i, 1, _fv.id)
    db.flush()

    def _spec(codes_and_versions: list[tuple[str, int]]):
        """根据 code+version 转 (factor_id, code, version, fv_row_id) 4 元组。"""
        spec = []
        for code, v in codes_and_versions:
            fid, vv, fvid = version_map[code]
            assert vv == v, f"version mismatch for {code}: want {v}, seed has {vv}"
            spec.append((fid, code, v, fvid))
        return spec

    # ── frozen FactorSets ──
    # FS-A1/FS-A2/FS-A3：用于 already_linked 关联展示
    _make_frozen_set(db, "FS-A1", "A类集合1", _spec([("ep_ttm", 1), ("pb_ratio", 2)]))
    _make_frozen_set(db, "FS-A2", "A类集合2", _spec([("roe_yoy", 3)]))
    _make_frozen_set(db, "FS-A3", "A类集合3", _spec([("momentum_20d", 4), ("lowvol_60d", 5)]))

    # FS-U1/FS-U2/FS-U3：唯一匹配集合（不重复 sig）
    _make_frozen_set(db, "FS-U1", "唯一候选1", _spec([("ep_ttm", 1), ("pb_ratio", 2), ("roe_yoy", 3)]))
    _make_frozen_set(db, "FS-U2", "唯一候选2", _spec([("momentum_20d", 4)]))
    _make_frozen_set(db, "FS-U3", "唯一候选3", _spec([("lowvol_60d", 5), ("quality_alt", 6)]))

    # FS-DUP1/FS-DUP2："双胞胎" 同 sig 两套，用于多匹配 → unknown
    dup_spec = _spec([("ep_ttm_alt", 1), ("pb_ratio_alt", 1)])
    _make_frozen_set(db, "FS-DUP1", "重复候选A", dup_spec)
    _make_frozen_set(db, "FS-DUP2", "重复候选B", dup_spec)

    def add_model(mid: str, pairs: list, *, fsid: str | None = None,
                  fv_fsid: bool = False, mode: str = "offline_minimal") -> FactorModelRun:
        feature_versions_str = _fv_for(pairs, fsid_link=fsid if fv_fsid else None)
        hyper_str = _hyper(fsid=fsid if not fv_fsid else None, mode=mode)
        m = FactorModelRun(
            id=mid,
            model_type="ridge",
            asset_type="stock",
            target_code="target_5d_return",
            train_start_date=date(2025, 1, 1),
            train_end_date=date(2026, 1, 1),
            validation_start_date=date(2025, 11, 12),
            validation_end_date=date(2026, 1, 1),
            data_cutoff_at=datetime(2026, 1, 1, 18, 0),
            feature_versions_json=feature_versions_str,
            hyperparameters_json=hyper_str,
            metrics_json="{}",
            sample_count=250,
            symbol_count=0,
            trade_date_count=250,
            status="validated",
            created_at=datetime(2026, 1, 2),
        )
        db.add(m)
        return m

    models: list[FactorModelRun] = []
    # (a) already_linked 3 条
    models.append(add_model("m1-hyper-fsid-A1",
                            [("ep_ttm", 1), ("pb_ratio", 2)],
                            fsid="FS-A1"))  # hyper 中写 fsid
    models.append(add_model("m2-fv-fsid-A2",
                            [("roe_yoy", 3)],
                            fsid="FS-A2", fv_fsid=True))  # feature_versions.__factor_set_id__
    models.append(add_model("m3-hyper-fsid-A3",
                            [("momentum_20d", 4), ("lowvol_60d", 5)],
                            fsid="FS-A3"))

    # (b) match_candidates 3 条（空 fsid，唯一匹配）
    models.append(add_model("m4-match-U1",
                            [("ep_ttm", 1), ("pb_ratio", 2), ("roe_yoy", 3)]))  # → FS-U1
    models.append(add_model("m5-match-U2",
                            [("momentum_20d", 4)]))                       # → FS-U2
    models.append(add_model("m6-match-U3",
                            [("lowvol_60d", 5), ("quality_alt", 6)]))    # → FS-U3

    # (c) unlinked_unknown 2 条
    #   c1: 多匹配（FS-DUP1 + FS-DUP2 两套完全相同 sig）
    models.append(add_model("m7-multi-match",
                            [("ep_ttm_alt", 1), ("pb_ratio_alt", 1)]))
    #   c2: 0 匹配（不存在任何集合包含这些 tuples）
    models.append(add_model("m8-no-match",
                            [("ep_ttm", 999), ("ghost_factor", 42)]))
    db.flush()
    return models


# ───────────────────────────────────────────── TR-10.1（rule）

def test_TR10_1_migration_report_three_buckets(db_session):
    """pytest seed 8 条模型 → already_linked=3, match_candidates=3, unlinked_unknown=2, total=8。"""
    from app.api.routes.factor_models import build_migration_report

    _seed_8_models(db_session)
    db_session.commit()

    report = build_migration_report(db_session)

    # 总计数
    assert report["total"] == 8, f"total mismatch: {report}"

    linked = report["already_linked"]
    cand = report["match_candidates"]
    unk = report["unlinked_unknown"]

    assert len(linked) == 3, f"already_linked count={len(linked)}: {linked}"
    assert len(cand) == 3, f"match_candidates count={len(cand)}: {cand}"
    assert len(unk) == 2, f"unlinked_unknown count={len(unk)}: {unk}"

    # ── already_linked 内容校验 ──
    linked_ids = {x["model_id"]: x for x in linked}
    assert "m1-hyper-fsid-A1" in linked_ids
    assert linked_ids["m1-hyper-fsid-A1"]["factor_set_id"] == "FS-A1"
    assert linked_ids["m1-hyper-fsid-A1"]["n_members"] == 2
    assert linked_ids["m2-fv-fsid-A2"]["factor_set_id"] == "FS-A2"
    assert linked_ids["m2-fv-fsid-A2"]["n_members"] == 1

    # ── match_candidates 内容校验 ──
    cand_by_model = {x["model_id"]: x for x in cand}
    assert cand_by_model["m4-match-U1"]["candidate_factor_set_id"] == "FS-U1"
    assert cand_by_model["m4-match-U1"]["confidence"] == 1.0
    assert cand_by_model["m4-match-U1"]["matching_hash"]
    assert any("唯一匹配" in r for r in cand_by_model["m4-match-U1"]["reasons"])

    assert cand_by_model["m5-match-U2"]["candidate_factor_set_id"] == "FS-U2"
    assert cand_by_model["m6-match-U3"]["candidate_factor_set_id"] == "FS-U3"

    # ── unlinked_unknown 内容校验 ──
    unk_by_model = {x["model_id"]: x for x in unk}
    assert "m7-multi-match" in unk_by_model
    assert "FS-DUP1" in unk_by_model["m7-multi-match"]["why_hint"]
    assert "不唯一" in unk_by_model["m7-multi-match"]["why_hint"]

    assert "m8-no-match" in unk_by_model
    assert (
        "找不到唯一匹配" in unk_by_model["m8-no-match"]["why_hint"]
        or "无法匹配任何冻结集合" in unk_by_model["m8-no-match"]["why_hint"]
    )


# ───────────────────────────────────────────── TR-10.2（rule）

def test_TR10_2_apply_candidate_success_and_unknown_forbidden(db_session):
    """POST 申请 case2 → UPDATE 成功；audit_count + 1；POST unknown → HTTP 400 且 DB 值不变。"""
    from fastapi.testclient import TestClient

    from app.api.router import api_router
    from app.core.config import settings
    from app.db.session import get_db

    _seed_8_models(db_session)
    db_session.commit()

    # ── 构造 FastAPI app 并 override get_db ──
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(api_router)

    def _override_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    prefix = settings.api_prefix

    # ── baseline audit count ──
    audit_before = db_session.execute(
        select(FactorAuditLog)
        .where(FactorAuditLog.action == "apply_migration_candidate")
    ).scalars().all()
    audit_count_before = len(audit_before)

    # ── POST case2 (match_candidate m5-match-U2 → FS-U2) ──
    url = f"{prefix}/factor-models/apply-migration-candidate"
    resp_ok = client.post(url, json={
        "model_id": "m5-match-U2",
        "audit_signoff": {
            # 用默认以外的值便于断言（Open Q-2 验收整改=默认值）
            "approver": "验收整改",
            "reason": "Task10 TR-10.2: 历史模型唯一匹配写回",
            # signed_at 留空，测试默认=signed_at=当前时间
        },
    })
    assert resp_ok.status_code == 200, f"POST candidate: {resp_ok.status_code} {resp_ok.text}"
    body_ok = resp_ok.json()
    assert body_ok["ok"] is True
    assert body_ok["factor_set_id"] == "FS-U2"
    assert body_ok["audit_signoff"]["approver"] == "验收整改"
    assert body_ok["matching_hash"]

    # ── 验证 DB：FactorModelRun.hyperparameters_json / feature_versions 更新 ──
    model_after = db_session.get(FactorModelRun, "m5-match-U2")
    assert model_after is not None
    hyper_after = json.loads(model_after.hyperparameters_json or "{}")
    assert hyper_after.get("factor_set_id") == "FS-U2", (
        f"factor_set_id 未写入 hyperparameters_json: {hyper_after}"
    )
    fv_after = json.loads(model_after.feature_versions_json or "{}")
    assert fv_after.get("__factor_set_id__") == "FS-U2"

    # ── 验证 DB：audit_count 增加 1 ──
    audit_after_rows = db_session.execute(
        select(FactorAuditLog)
        .where(FactorAuditLog.action == "apply_migration_candidate")
    ).scalars().all()
    audit_count_after = len(audit_after_rows)
    assert audit_count_after - audit_count_before == 1, (
        f"audit_count delta={audit_count_after - audit_count_before}, expected +1"
    )
    just_written = audit_after_rows[-1]
    assert just_written.actor == "验收整改"
    assert just_written.model_run_id == "m5-match-U2"
    assert just_written.factor_set_id == "FS-U2"
    assert just_written.action == "apply_migration_candidate"
    # before/after json 必须包含 before_fsid=None + after_fsid=candidate
    before_snap = json.loads(just_written.before_json or "{}")
    after_snap = json.loads(just_written.after_json or "{}")
    assert before_snap["before_fsid"] is None
    assert before_snap["after_fsid"] == "FS-U2"
    assert before_snap == after_snap  # 等价要求

    # ── POST unknown model (m7-multi-match) → 400 FORBIDDEN 且 DB 不变 ──
    # 先读申请前 DB 值
    m7_before = db_session.get(FactorModelRun, "m7-multi-match")
    hyper_m7_before = m7_before.hyperparameters_json
    fv_m7_before = m7_before.feature_versions_json

    resp_400 = client.post(url, json={
        "model_id": "m7-multi-match",
        "audit_signoff": {
            "approver": "恶意用户",
            "reason": "尝试对多匹配模型强制写入",
        },
    })
    assert resp_400.status_code == 400, (
        f"POST unknown should be HTTP 400, got {resp_400.status_code} {resp_400.text}"
    )
    err_detail = resp_400.json().get("detail", "")
    assert "FORBIDDEN" in err_detail or "不在 match_candidates" in err_detail

    # DB 不变（hyperparameters_json 字节级完全一致）
    db_session.expire_all()
    m7_after = db_session.get(FactorModelRun, "m7-multi-match")
    assert m7_after.hyperparameters_json == hyper_m7_before, (
        "POST unknown case 不应修改 hyperparameters_json"
    )
    assert m7_after.feature_versions_json == fv_m7_before, (
        "POST unknown case 不应修改 feature_versions_json"
    )

    # audit_count 不应增长（事务 rollback 或 没写）
    audit_final = db_session.execute(
        select(FactorAuditLog)
        .where(FactorAuditLog.action == "apply_migration_candidate")
    ).scalars().all()
    assert len(audit_final) == audit_count_after, (
        f"POST unknown case 不应新增 audit: delta={len(audit_final) - audit_count_after}"
    )

    # ── POST already_linked 模型 → 400（仍不允许改） ──
    resp_400_linked = client.post(url, json={
        "model_id": "m1-hyper-fsid-A1",
        "audit_signoff": {"approver": "验收整改", "reason": "试改 already_linked"},
    })
    assert resp_400_linked.status_code == 400

    # ── GET 报告再次确认状态不变 ──
    resp_report = client.get(f"{prefix}/factor-models/migration-report")
    assert resp_report.status_code == 200, (
        f"GET migration-report failed: HTTP {resp_report.status_code}, body={resp_report.text[:500]}"
    )
    rep = resp_report.json()
    # m5-match-U2 现在已写入 fsid，应从 match_candidates → already_linked
    linked_ids = {x["model_id"] for x in rep["already_linked"]}
    cand_ids = {x["model_id"] for x in rep["match_candidates"]}
    assert "m5-match-U2" in linked_ids
    assert "m5-match-U2" not in cand_ids
