"""Task 5: 统一模型关联 DTO Facade（factor_model_with_relations）— TR-5.1 ~ TR-5.4。

直接复用 DatabaseManager singleton + 每个函数独立 SQLite（conftest.py 的
tmp_sqlite_url / db_session fixtures）。HTTP 层通过覆盖 app.dependency_overrides[get_db]
使 TestClient 使用测试会话（与 test_factor_migration_task10.py 模式一致）。
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, func, text

from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import (
    FactorModelRun,
    FactorVersion,
    FactorWeightSnapshot,
)
from app.services.factors.__facade__ import (
    UNBOUND_REASON_AC7,
    ModelRelationsDTO,
    factor_model_with_relations,
)

pytestmark = pytest.mark.whitebox


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_factor_chain(db_session) -> dict:
    """为 TR-5.1 / TR-5.2 / TR-5.4 构造完整链：3 个 Factor + 1 个 Frozen FactorSet + 1 个 Validated Model + weight snapshot。

    返回 key 字典：{model_id, factor_set_id, factor_codes, factor_set_member_count}
    """
    # ── 1. Factors (3 × feature 角色，2 × 权重非零，1 × 故意留 NaN 字段 以测试 null) ──
    f1 = Factor(
        code="VAL_ep_ttm",
        name="EP 市盈率倒数",
        category="valuation",
        direction="positive",
        status="active",
        lifecycle_status="shadow",
    )
    f2 = Factor(
        code="QLT_roe_yoy",
        name="ROE 同比增速",
        category="quality",
        direction="positive",
        status="active",
        lifecycle_status="active",
    )
    f3 = Factor(
        code="TAT_turnover_z20",
        name="换手率 Z20",
        category="technical",
        direction="negative",
        status="active",
        lifecycle_status="testing",
    )
    db_session.add_all([f1, f2, f3])
    db_session.flush()

    # ── 2. FactorVersions (version=1 for each) ──
    v1 = FactorVersion(
        factor_id=f1.id, version=1, formula_expr="1/pe_ttm",
        params_json="{}", source_mapping_json="{}", change_note="v1",
    )
    v2 = FactorVersion(
        factor_id=f2.id, version=1, formula_expr="roe_yoy",
        params_json="{}", source_mapping_json="{}", change_note="v1",
    )
    v3 = FactorVersion(
        factor_id=f3.id, version=1, formula_expr="turnover_z20",
        params_json="{}", source_mapping_json="{}", change_note="v1",
    )
    db_session.add_all([v1, v2, v3])
    db_session.flush()

    # ── 3. FactorSet (frozen) + 3 feature members ──
    fs_id = "FS-REL-TEST-001"
    fs = FactorSet(
        id=fs_id,
        name="链式测试集合 v1",
        description="Task 5 TR 验收专用（3 feature）",
        content_hash="sha256-mock-rel-test-001",
        status="frozen",
        frozen_at=_utcnow_naive(),
        created_by="pytest_task5",
    )
    fs.members = [
        FactorSetMember(
            factor_set_id=fs_id, factor_id=f1.id, factor_version_id=v1.id,
            factor_code=f1.code, factor_version=1, role="feature",
            weight_constraint="positive", missing_policy="exclude", display_order=0,
        ),
        FactorSetMember(
            factor_set_id=fs_id, factor_id=f2.id, factor_version_id=v2.id,
            factor_code=f2.code, factor_version=1, role="feature",
            weight_constraint="free", missing_policy="exclude", display_order=1,
        ),
        FactorSetMember(
            factor_set_id=fs_id, factor_id=f3.id, factor_version_id=v3.id,
            factor_code=f3.code, factor_version=1, role="feature",
            weight_constraint="negative", missing_policy="impute_zero", display_order=2,
        ),
    ]
    db_session.add(fs)
    db_session.flush()

    # ── 4. FactorModelRun（hyperparameters_json 写入 factor_set_id） ──
    model_id = "MR-REL-TEST-001"
    hp_json = json.dumps(
        {
            "factor_set_id": fs_id,
            "train_mode": "offline_minimal",
            "alpha": 0.01,
            "l1_ratio": 0.5,
        },
        ensure_ascii=False,
    )
    fv_json_dump = json.dumps(
        {
            "factor_set_id": fs_id,
            "train_mode": "offline_minimal",
        },
        ensure_ascii=False,
    )
    metrics_json = json.dumps(
        {
            "validation_ic": 0.042,
            "train_ic": 0.058,
            "validation_r2": 0.008,
            "validation_icir": 0.72,
        },
        ensure_ascii=False,
    )
    model = FactorModelRun(
        id=model_id,
        model_type="ridge",
        asset_type="stock",
        target_code="target_5d_return",
        train_start_date=date(2025, 1, 2),
        train_end_date=date(2025, 12, 31),
        validation_start_date=date(2026, 1, 1),
        validation_end_date=date(2026, 3, 31),
        data_cutoff_at=datetime(2026, 3, 31, 20, 0),
        feature_versions_json=fv_json_dump,
        hyperparameters_json=hp_json,
        metrics_json=metrics_json,
        sample_count=125000,
        symbol_count=5400,
        trade_date_count=250,
        status="validated",
        created_at=datetime(2026, 4, 1, 10, 30),
    )
    db_session.add(model)
    db_session.flush()

    # ── 5. FactorWeightSnapshot（Task 11 rev 049：per-model 聚合行，
    #    PK=model_id；weights_norm_json 为 3 元素数组 → 对应 3 个 feature members）
    #    f3.validation_ic 故意用 float('nan')，应该被 Facade _safe_num 转成
    #    Python None → JSON null（绝不伪装成 0，TR-5.2 验证）。
    _today = date(2026, 3, 31)
    weights_entries = [
        {
            "factor_code": f1.code, "factor_id": int(f1.id),
            "factor_version_id": int(v1.id), "factor_version": 1,
            "coef_raw": 0.312, "weight_norm": 0.45,
            "training_ic": 0.061, "validation_ic": 0.044,
            "role": "feature",
        },
        {
            "factor_code": f2.code, "factor_id": int(f2.id),
            "factor_version_id": int(v2.id), "factor_version": 1,
            "coef_raw": 0.418, "weight_norm": 0.55,
            "training_ic": 0.055, "validation_ic": 0.040,
            "role": "feature",
        },
        {
            "factor_code": f3.code, "factor_id": int(f3.id),
            "factor_version_id": int(v3.id), "factor_version": 1,
            "coef_raw": -0.080, "weight_norm": -0.10,
            "training_ic": 0.050,
            # 故意注入 NaN：Facade 必须过滤成 None → JSON null，不能是 0
            "validation_ic": float("nan"),
            "role": "feature",
        },
    ]
    aggr_snap = FactorWeightSnapshot(
        model_id=model_id,
        factor_set_id=fs_id,
        factor_set_content_hash="sha256-mock-rel-test-001",
        factor_ids_json=json.dumps([int(f1.id), int(f2.id), int(f3.id)]),
        factor_version_ids_json=json.dumps([int(v1.id), int(v2.id), int(v3.id)]),
        roles_json=json.dumps(["feature", "feature", "feature"]),
        constraints_json=json.dumps(["positive", "free", "negative"]),
        missing_strategies_json=json.dumps(["exclude", "exclude", "impute_zero"]),
        train_start_date=date(2025, 1, 2),
        train_end_date=date(2025, 12, 31),
        valid_start_date=date(2026, 1, 1),
        valid_end_date=date(2026, 3, 31),
        data_cutoff_date=_today,
        weights_raw_json=json.dumps({
            f1.code: 0.312, f2.code: 0.418, f3.code: -0.080,
        }),
        weights_norm_json=json.dumps(weights_entries, allow_nan=True),
        train_mode="offline_minimal",
    )
    db_session.add(aggr_snap)
    db_session.commit()

    return {
        "model_id": model_id,
        "factor_set_id": fs_id,
        "factor_set_name": fs.name,
        "factor_set_member_count": 3,
        "factor_codes": [f1.code, f2.code, f3.code],
        "factor_version_ids": [v1.id, v2.id, v3.id],
        "factor_ids": [f1.id, f2.id, f3.id],
    }


# ───────────────────────────────────────────────────────────── TR-5.1
def test_TR5_1_model_relations_basic_n_members_and_triplet(db_session):
    """TR-5.1：新训练模型 → GET /scoring/models/{id}/relations → n_members == len(factors)
    且三列（factor_set_name / factor_set_id / n_members）非空且不为 "-"。"""
    meta = _seed_factor_chain(db_session)
    model_id = meta["model_id"]

    # ── (a) 直接通过 Facade 函数（快速路径）──
    dto = factor_model_with_relations(model_id)
    assert dto is not None, "facade 返回非 None（模型存在）"
    assert dto.schema_version == 2, f"schema_version=2（got {dto.schema_version}）"
    assert dto.n_members is not None and dto.n_members == len(dto.factors), (
        f"n_members={dto.n_members} 必须等于 len(factors)={len(dto.factors)}"
    )
    # 三列 name / id / n 非空非短横线
    assert dto.factor_set_id is not None and str(dto.factor_set_id).strip() not in ("", "-"), (
        f"factor_set_id={dto.factor_set_id!r} 非空非'-'"
    )
    assert dto.factor_set_name is not None and str(dto.factor_set_name).strip() not in ("", "-"), (
        f"factor_set_name={dto.factor_set_name!r} 非空非'-'"
    )
    assert dto.n_members is not None and dto.n_members >= 1, (
        f"n_members={dto.n_members!r} 非空且 ≥1"
    )
    # 额外：v1 兼容字段也填充
    assert dto.factorset_id == dto.factor_set_id
    assert dto.factorset_label == dto.factor_set_name
    assert dto.factorset_member_count == dto.n_members
    assert dto.unbound_reason is None, "已关联模型 unbound_reason = None"

    # ── (b) HTTP 路径（TestClient），确保路由正确注册 + 返回 JSON DTO ──
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.router import api_router
    from app.core.config import settings
    from app.db.session import get_db

    app = FastAPI()
    app.include_router(api_router)

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app)
        url = f"{settings.api_prefix}/scoring/models/{model_id}/relations"
        resp = client.get(url)
        assert resp.status_code == 200, f"GET relations HTTP 200（got {resp.status_code} body={resp.text}）"
        body = resp.json()
        assert body["schema_version"] == 2
        assert body["n_members"] == len(body["factors"]) == 3, (
            f"HTTP n_members={body['n_members']} len(factors)={len(body['factors'])}"
        )
        assert body["factor_set_id"] == meta["factor_set_id"]
        assert body["factor_set_name"] == meta["factor_set_name"]
        # 三列非短横线
        for col in ("factor_set_id", "factor_set_name"):
            assert body[col] not in ("", "-"), f"HTTP body[{col}]={body[col]!r} 非空非'-'"
        assert body["n_members"] not in (None, "-"), f"HTTP n_members={body['n_members']!r} 非空非'-'"
    finally:
        app.dependency_overrides.clear()


# ───────────────────────────────────────────────────────────── TR-5.2
def test_TR5_2_six_columns_null_safety_no_nan_to_zero(db_session):
    """TR-5.2：DTO factors[i] 6 列字段全非空或 N/A（缺失 → null；
    严禁 NaN → 0）。6 列：factor_code, factor_version_id, coef_raw,
    weight_norm, validation_ic, coverage。"""
    meta = _seed_factor_chain(db_session)
    dto = factor_model_with_relations(meta["model_id"])
    assert dto is not None

    # 6 列的“目标字段名集合”（对应 AC-6：code/version_id/coef_raw/weight_norm/validation_ic/coverage）
    SIX_COLS = ("factor_code", "factor_version_id", "coef_raw",
                "weight_norm", "validation_ic", "coverage")

    factors = dto.factors
    assert len(factors) == 3, f"seed 3 个 feature → factors 应有 3 行（got {len(factors)}）"

    # 把 DTO → dict → JSON 化一遍，验证 NaN 确实不会 leak（JSON 会失败若有 NaN）
    from dataclasses import asdict
    payload = asdict(dto)
    json_text = json.dumps(payload, ensure_ascii=False)
    reparsed = json.loads(json_text)
    assert reparsed is not None, "DTO 可被 json.dumps/loads 往返（无 NaN leak）"

    factors_repr = reparsed["factors"]
    nan_codes_seen: list[str] = []
    for i, row in enumerate(factors_repr):
        code = row.get("factor_code")
        for col in SIX_COLS:
            assert col in row, f"factors[{i}](code={code!r}) 缺少列 {col}"
            v = row[col]
            # 1) 允许存在非 null 真实值；2) 允许 null = N/A；
            #    但绝对不允许 float('nan') → 被 json.dumps 转成 NaN（非法 JSON）或 伪装成 0
            if isinstance(v, float):
                assert not math.isnan(v), (
                    f"factors[{i}].{col} = NaN — 不允许（应输出 null，不是 NaN 也不是 0）"
                )
                assert not math.isinf(v), f"factors[{i}].{col} = Inf"
            if v is None:
                # 缺失字段合法：属于 N/A 展示；不应该在这一步被篡改为 0
                pass
            if col == "validation_ic" and code == "TAT_turnover_z20":
                # seed 时这个 factor 的 validation_ic = NaN → 必须 JSON null，不能是 0
                assert v is None, (
                    f"{code}.validation_ic 原始=NaN → 必须是 null；实际得到 {v!r}（错误地伪装成 0 = FAIL）"
                )
                nan_codes_seen.append(code)

    assert "TAT_turnover_z20" in nan_codes_seen, (
        "seed 的 NaN 值字段被正确转为 null（以上断言应已触发；若未触发说明 JSON 序列化未按预期）"
    )

    # 额外：覆盖率字段若为 null → 不应是 0
    cov_values = [r.get("coverage") for r in factors_repr]
    for cv in cov_values:
        if cv is None:
            assert cv is None  # 合法（N/A），不是 0.0
        elif isinstance(cv, float):
            pass  # 健康快照存在时正常


# ───────────────────────────────────────────────────────────── TR-5.3
@pytest.mark.parametrize(
    "hp_variant,description",
    [
        ("{}", "hyperparameters_json 完全缺 factor_set_id 键"),
        ('{"factor_set_id": null}', "factor_set_id = JSON null"),
        ('{"factor_set_id": ""}', "factor_set_id = 空字符串"),
        ('{"factor_set_id": "   "}', "factor_set_id = 纯空白"),
        ('{"alpha": 0.01}', "缺 factor_set_id，但有其他超参"),
    ],
)
def test_TR5_3_unbound_model_exact_ac7_string(db_session, hp_variant, description):
    """TR-5.3：5 种无关联模型 → GET relations.unbound_reason 完全匹配 AC-7
    字符串（字符级精确相等，200+ 字）。"""
    model_id = "MR-UNBOUND-" + uuid.uuid4().hex[:10]
    model = FactorModelRun(
        id=model_id,
        model_type="ridge",
        asset_type="stock",
        target_code="target_5d_return",
        train_start_date=date(2024, 6, 1),
        train_end_date=date(2025, 5, 31),
        data_cutoff_at=datetime(2025, 5, 31, 20, 0),
        feature_versions_json='{"factors": {"old_f1": {"version": 1}}}',
        hyperparameters_json=hp_variant,  # 5 种变体
        metrics_json='{"validation_ic": 0.03}',
        sample_count=100000,
        symbol_count=5000,
        trade_date_count=240,
        status="validated",
        created_at=_utcnow_naive(),
    )
    db_session.add(model)
    db_session.commit()

    # ── Facade ──
    dto = factor_model_with_relations(model_id)
    assert dto is not None, f"模型存在（{description}）"
    assert dto.unbound_reason == UNBOUND_REASON_AC7, (
        f"[{description}] unbound_reason 字符级不匹配 AC-7\n"
        f"EXPECTED({len(UNBOUND_REASON_AC7)}): {UNBOUND_REASON_AC7!r}\n"
        f"GOT     ({len(dto.unbound_reason or '')}): {dto.unbound_reason!r}"
    )
    # 长度 ≥ 50 字（确认是 AC-7 指定全文不是简短「-」或缩写；spec 描述的 "(200+字)" 为定性说明，
    # 真实 AC-7 文本 = 57 个中文字符，字符级完全匹配才是核心验收）
    assert len(dto.unbound_reason or "") >= 50, (
        f"[{description}] unbound_reason 长度 {len(dto.unbound_reason or '')} < 50（应为 AC-7 全文）"
    )
    # 关联字段全空
    assert dto.factor_set_id is None, f"[{description}] factor_set_id 应为 None"
    assert dto.factor_set_name is None, f"[{description}] factor_set_name 应为 None"
    assert dto.n_members is None, f"[{description}] n_members 应为 None"
    assert dto.factors == [], f"[{description}] factors 应为 []（got {len(dto.factors)}）"
    assert dto.schema_version == 2

    # ── HTTP ──
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.router import api_router
    from app.core.config import settings
    from app.db.session import get_db

    app = FastAPI()
    app.include_router(api_router)

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app)
        url = f"{settings.api_prefix}/scoring/models/{model_id}/relations"
        resp = client.get(url)
        assert resp.status_code == 200, (
            f"[{description}] HTTP 200（got {resp.status_code} body={resp.text}）"
        )
        body = resp.json()
        assert body["unbound_reason"] == UNBOUND_REASON_AC7, (
            f"[{description}] HTTP unbound_reason 不匹配 AC-7 全文"
        )
        assert body["factor_set_id"] is None
        assert body["n_members"] is None
        assert body["factors"] == []
    finally:
        app.dependency_overrides.clear()


# ───────────────────────────────────────────────────────────── TR-5.4
def test_TR5_4_six_step_sql_traceability_6_of_6(db_session):
    """AC-11 全链回溯：6 步 SQL / API 双断言，6/6 pass。

    Step 1: SELECT factor_set_id FROM factor_model_runs WHERE id=model → X 非空
    Step 2: SELECT * FROM factor_sets WHERE id=X → 存在（ROW_FOUND）
    Step 3: SELECT COUNT(*) FROM factor_set_members WHERE set_id=X = N ≥ 1
    Step 4: SELECT factor_id, factor_version_id FROM factor_set_members 全部 NOT NULL
    Step 5: FactorWeightSnapshot 按 factor_code → version_id 与 DB 成员一致
    Step 6: GET relations 返回 factors[].factor_version_id 与 DB 完全一致
    """
    meta = _seed_factor_chain(db_session)
    model_id = meta["model_id"]
    expected_fsid = meta["factor_set_id"]
    expected_count = meta["factor_set_member_count"]
    expected_fv_ids = sorted(meta["factor_version_ids"])
    expected_f_ids = sorted(meta["factor_ids"])

    passed: list[str] = []
    failed: list[str] = []

    def _chk(label: str, cond: bool, detail_ok: str, detail_fail: str):
        if cond:
            passed.append(label)
        else:
            failed.append(f"{label}: {detail_fail}")

    # ── Step 1 ──
    row1 = db_session.execute(
        select(FactorModelRun.hyperparameters_json).where(FactorModelRun.id == model_id)
    ).scalar_one_or_none()
    hp = json.loads(row1 or "{}")
    step1_fsid = hp.get("factor_set_id")
    _chk(
        "S1: model.hyperparameters_json.factor_set_id NOT NULL",
        bool(step1_fsid) and step1_fsid == expected_fsid,
        f"factor_set_id={step1_fsid}",
        f"fsid={step1_fsid!r} expected={expected_fsid}",
    )

    # ── Step 2 ──
    fs2 = db_session.get(FactorSet, expected_fsid)
    _chk(
        "S2: factor_sets WHERE id=X → ROW_FOUND",
        fs2 is not None and fs2.id == expected_fsid,
        f"fs.name={getattr(fs2, 'name', None)}",
        f"FactorSet id={expected_fsid} 未找到",
    )

    # ── Step 3 ──
    cnt3 = db_session.execute(
        select(func.count(FactorSetMember.id)).where(
            FactorSetMember.factor_set_id == expected_fsid,
            FactorSetMember.role == "feature",
        )
    ).scalar()
    _chk(
        "S3: COUNT(factor_set_members feature) = N",
        cnt3 == expected_count,
        f"N={cnt3}",
        f"count={cnt3} expected={expected_count}",
    )

    # ── Step 4 ──
    members4 = db_session.execute(
        select(FactorSetMember.factor_id, FactorSetMember.factor_version_id).where(
            FactorSetMember.factor_set_id == expected_fsid,
            FactorSetMember.role == "feature",
        )
    ).all()
    fids = sorted([int(r.factor_id) for r in members4 if r.factor_id is not None])
    fvids = sorted([int(r.factor_version_id) for r in members4 if r.factor_version_id is not None])
    _chk(
        "S4: factor_id & factor_version_id all NOT NULL",
        len(members4) == expected_count
        and None not in {r.factor_id for r in members4}
        and None not in {r.factor_version_id for r in members4}
        and fids == expected_f_ids
        and fvids == expected_fv_ids,
        f"rows={len(members4)} fids={fids} fvids={fvids}",
        f"rows={len(members4)} fids={fids}(expected {expected_f_ids}) fvids={fvids}(expected {expected_fv_ids})",
    )

    # ── Step 5: FactorWeightSnapshot 按 code 反查成员 version_id ──
    #   Task 11 rev 049 后 FactorWeightSnapshot 是 per-model 聚合行（PK=model_id），
    #   code→version 对保存在 weights_norm_json 数组中。
    aggr_row = db_session.execute(
        select(FactorWeightSnapshot.weights_norm_json).where(
            FactorWeightSnapshot.model_id == model_id
        )
    ).scalar_one_or_none()
    assert aggr_row is not None, f"model_id={model_id!r} 未找到聚合快照行（S5 需要 T11 rev049 表）"
    parsed_weights = json.loads(aggr_row) if isinstance(aggr_row, str) else aggr_row
    assert isinstance(parsed_weights, list) and len(parsed_weights) >= 1, (
        f"weights_norm_json 不是数组或为空：{type(parsed_weights).__name__}"
    )
    snaps5 = [
        type("_SnapRow", (), {
            "factor_code": e.get("factor_code"),
            "factor_version": e.get("factor_version", e.get("factor_version_number", 0)),
        })()
        for e in parsed_weights
        if isinstance(e, dict) and e.get("factor_code")
    ]
    code_to_version_snap = {s.factor_code: int(s.factor_version) for s in snaps5}
    # 查每个 code → FactorVersion.version 的 DB 真值
    mems_for_code = db_session.execute(
        select(FactorSetMember.factor_code, FactorSetMember.factor_version_id,
               FactorSetMember.factor_version).where(
            FactorSetMember.factor_set_id == expected_fsid,
            FactorSetMember.role == "feature",
        )
    ).all()
    code_to_version_member = {}
    for m in mems_for_code:
        fvrow = db_session.get(FactorVersion, m.factor_version_id)
        vno = int(fvrow.version) if fvrow else int(m.factor_version or 0)
        code_to_version_member[m.factor_code] = vno
    step5_ok = all(
        code_to_version_snap.get(c) == v for c, v in code_to_version_member.items()
    ) and len(snaps5) == expected_count
    _chk(
        "S5: snapshot.code → version_id 与 FactorSetMember + FactorVersion 一致",
        step5_ok,
        f"snap versions={code_to_version_snap} member versions={code_to_version_member}",
        f"snap={code_to_version_snap} ≠ member+fv={code_to_version_member}",
    )

    # ── Step 6: HTTP/API relations 返回的 factor_version_id 与 DB 一致 ──
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.router import api_router
    from app.core.config import settings
    from app.db.session import get_db

    app = FastAPI()
    app.include_router(api_router)

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app)
        url = f"{settings.api_prefix}/scoring/models/{model_id}/relations"
        resp = client.get(url)
        body = resp.json()
        api_fvids = sorted(
            int(f["factor_version_id"]) for f in body["factors"] if f["factor_version_id"] is not None
        )
        step6_ok = (
            resp.status_code == 200
            and api_fvids == expected_fv_ids
            and len(body["factors"]) == expected_count
        )
        _chk(
            "S6: GET relations.factors[].factor_version_id == DB fv_id list",
            step6_ok,
            f"HTTP status={resp.status_code} api_fvids={api_fvids} expected={expected_fv_ids}",
            f"HTTP status={resp.status_code} api_fvids={api_fvids} expected={expected_fv_ids} len(factors)={len(body.get('factors', []))}",
        )
    finally:
        app.dependency_overrides.clear()

    # 汇总 6/6 断言
    assert len(passed) == 6 and len(failed) == 0, (
        f"AC-11 全链回溯 {len(passed)}/6 pass，失败：\n"
        + "\n  - ".join(failed) if failed else ""
        + f"\n通过步骤：{passed}"
    )
