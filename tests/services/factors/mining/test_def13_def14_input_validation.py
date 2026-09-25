"""DEF-13/DEF-14 回归 · 模板公式校验 + 草稿入参校验（2026-09-25）。

哨兵：
1. 模板：垃圾公式（`这不是公式(((`）/未知函数 → `template_formula_invalid`；
2. 模板：**带参模板**（`mean(close,{n1})` + params）必须能过（25 个系统模板
   raw 公式 21 个过不了编译器、代入后 25/25 全过——校验前必须先代入参数）；
3. 模板：名称 >64 字符 → 拒绝；
4. 草稿：steps 值非对象（如字符串）→ `draft_step_shape_invalid`；
5. 草稿：名称 >64 / 引用不存在的快照 → 拒绝；
6. 路由层：422 均为**结构化中文信封**（此前是裸字符串 detail）。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.services.factors.mining import draft_service as DS
from app.services.factors.mining import template_service as TS

pytestmark = pytest.mark.whitebox

GOOD_FORMULA = "mean(close,{n1})/mean(close,{n2})-1"
GOOD_PARAMS = {"n1": [10, 20], "n2": [30, 60]}


# ══════════════════════════════════════════════════════════
# DEF-13 · 模板公式校验
# ══════════════════════════════════════════════════════════


def test_template_rejects_garbage_formula(db_session):
    """哨兵 1：垃圾公式入库被拦（此前只判「formula 非空」）。"""
    with pytest.raises(ValueError) as ei:
        TS.create_personal_template(
            db_session, name="垃圾模板", description=None,
            rule_config={"formula": "这不是公式((("}, owner="tester")
    assert "template_formula_invalid" in str(ei.value)


def test_template_rejects_unknown_function(db_session):
    """哨兵 2：未知函数被拦（编译器错误码透出）。"""
    with pytest.raises(ValueError) as ei:
        TS.create_personal_template(
            db_session, name="未知函数", description=None,
            rule_config={"formula": "not_a_real_fn(close,20)"}, owner="tester")
    assert "template_formula_invalid" in str(ei.value)


def test_template_accepts_parametrized_formula(db_session):
    """哨兵 3：带参模板（占位符 + params）必须能过——系统模板同款形态。"""
    view = TS.create_personal_template(
        db_session, name="带参模板", description=None,
        rule_config={"formula": GOOD_FORMULA, "params": GOOD_PARAMS},
        owner="tester")
    assert view["template_id"]


def test_template_accepts_plain_formula(db_session):
    """哨兵 4：无占位符的合法公式直接过。"""
    view = TS.create_personal_template(
        db_session, name="无参模板", description=None,
        rule_config={"formula": "mean(close,20)"}, owner="tester")
    assert view["template_id"]


def test_template_rejects_overlong_name(db_session):
    """哨兵 5：名称 >64 字符 → 拒绝（防超长串污染模板列表）。"""
    with pytest.raises(ValueError) as ei:
        TS.create_personal_template(
            db_session, name="超" * 65, description=None,
            rule_config={"formula": "mean(close,20)"}, owner="tester")
    assert "template_name_too_long" in str(ei.value)


def test_all_seed_templates_pass_validator(db_session):
    """哨兵 6：25 个系统模板代入参数后必须 25/25 过校验（防误杀合法模板）。"""
    from app.services.factors import factor_compiler as FC
    from app.services.factors.mining.initial_population import CLASSIC_TEMPLATES
    import re

    def render(formula: str, params) -> str:
        def sub(m):
            vals = (params or {}).get(m.group(1))
            return str(vals[0]) if vals else "20"
        return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, formula)

    for t in CLASSIC_TEMPLATES:
        errs = FC.validate_formula(render(t.formula, dict(t.params or {})))
        assert not errs, f"系统模板 {t.name} 被校验误杀: {errs[:2]}"


# ══════════════════════════════════════════════════════════
# DEF-14 · 草稿入参校验
# ══════════════════════════════════════════════════════════


def test_draft_rejects_non_dict_step(db_session):
    """哨兵 7：steps 值必须对象（此前字符串/列表直接入库，读回残缺结构）。"""
    with pytest.raises(ValueError) as ei:
        DS.save_draft(db_session, payload={
            "name": "残缺草稿", "steps": {"step1": "abc"},
        })
    assert "draft_step_shape_invalid" in str(ei.value)


def test_draft_rejects_overlong_name(db_session):
    """哨兵 8：名称 >64 字符 → 拒绝。"""
    with pytest.raises(ValueError) as ei:
        DS.save_draft(db_session, payload={"name": "超" * 65, "steps": {}})
    assert "draft_name_too_long" in str(ei.value)


def test_draft_rejects_missing_snapshot_reference(db_session):
    """哨兵 9：引用不存在的快照 → 拒绝（等 prepare 才报错太晚）。"""
    with pytest.raises(ValueError) as ei:
        DS.save_draft(db_session, payload={
            "name": "悬空草稿",
            "candidate_pool_snapshot_id": "snap-not-exist-000",
            "steps": {},
        })
    assert "draft_snapshot_not_found" in str(ei.value)


def test_draft_saves_valid_payload(db_session):
    """哨兵 10：合法载荷照常保存（校验不误伤正常链路）。"""
    view = DS.save_draft(db_session, payload={
        "name": "正常草稿",
        "steps": {"step1": {"pool_id": None},
                  "step2": {"start_date": "2026-01-05"}},
    })
    assert view.id


# ══════════════════════════════════════════════════════════
# 路由层：422 结构化中文信封
# ══════════════════════════════════════════════════════════


def _client(db_session) -> TestClient:
    from app.api.routes import factor_mining as FM

    app = FastAPI()
    app.include_router(FM.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_template_route_returns_structured_422(db_session):
    """哨兵 11：垃圾公式 → 422 + 中文信封（此前 detail 是裸字符串）。"""
    resp = _client(db_session).post("/factor-mining/templates", json={
        "name": "垃圾模板", "rule_config": {"formula": "这不是公式((("},
    })
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["detail"]["error_code"] == "VALIDATION_ERROR"
    # 中文框架 + 编译器错误码透出（编译器自身 message 为英文技术串，可接受）
    assert "template_formula_invalid" in body["detail"]["detail_zh"]
    assert "formula_syntax_error" in body["detail"]["detail_zh"]


def test_draft_route_returns_structured_422(db_session):
    """哨兵 12：残缺 steps → 422 + 中文信封。"""
    resp = _client(db_session).post("/factor-mining/drafts", json={
        "name": "残缺草稿", "steps": {"step1": "abc"},
    })
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["detail"]["error_code"] == "VALIDATION_ERROR"
    assert "draft_step_shape_invalid" in body["detail"]["detail_zh"]


# ══════════════════════════════════════════════════════════
# 卡 A · 模板删除端点（system 保护 / personal 删除）
# ══════════════════════════════════════════════════════════


def test_template_delete_protects_system_scope(db_session):
    """哨兵 13：system 模板不可删（GA 初始种群同源资产，只许启停）。"""
    TS.seed_system_templates(db_session)
    from app.models.factor_mining import FactorMiningTemplate

    row = db_session.query(FactorMiningTemplate).filter_by(
        scope="system").first()
    with pytest.raises(ValueError) as ei:
        TS.delete_template(db_session, template_id=str(row.id))
    assert "template_system_protected" in str(ei.value)
    # 未删除
    assert db_session.get(FactorMiningTemplate, str(row.id)) is not None


def test_template_delete_removes_personal_and_versions(db_session):
    """哨兵 14：personal 模板 → 版本行 + 主题行一起删。"""
    view = TS.create_personal_template(
        db_session, name="待删模板", description=None,
        rule_config={"formula": "mean(close,20)"}, owner="tester")
    tpl_id = str(view["template_id"])

    out = TS.delete_template(db_session, template_id=tpl_id)

    assert out["deleted"] is True
    assert out["deleted_versions"] >= 1
    from app.models.factor_mining import (
        FactorMiningTemplate,
        FactorMiningTemplateVersion,
    )

    db_session.expire_all()
    assert db_session.get(FactorMiningTemplate, tpl_id) is None
    assert db_session.query(FactorMiningTemplateVersion).filter_by(
        template_id=tpl_id).count() == 0


def test_template_delete_route_not_found_and_protected(db_session):
    """哨兵 15：路由层——不存在 → 404；system → 409 结构化信封。"""
    TS.seed_system_templates(db_session)
    from app.models.factor_mining import FactorMiningTemplate

    client = _client(db_session)
    r1 = client.delete("/factor-mining/templates/no-such-tpl")
    assert r1.status_code == 404, r1.text

    sys_row = db_session.query(FactorMiningTemplate).filter_by(
        scope="system").first()
    r2 = client.delete(f"/factor-mining/templates/{sys_row.id}")
    assert r2.status_code == 409, r2.text
    assert r2.json()["detail"]["error_code"] == "BUSINESS_BLOCKED"
    assert "系统模板不可删除" in r2.json()["detail"]["detail_zh"]
