"""WP4 白盒测试：自定义指标提升 + AI 因子草案。

覆盖：
- WP4-01 数值指标提升（promote_factor_from_indicator + API 端点）
  - number 指标提升创建 draft 因子
  - boolean 指标拒绝
  - 重复提升幂等（同 execution_plan_hash 返回既有版本，不创建新版本）
  - params list[dict] → dict 转换
  - source_mapping 溯源字段
  - 不存在/非法 code 边界
  - API 端点 201/422/404 + 结构化错误
- WP4-03/04 AI 因子草案（draft_factor / preview_factor / execute_factor）
  - 三步流程：建议（不写 DB）→ 预览（dry-run）→ 确认后执行
  - Pydantic + AST 双重校验
  - content_hash 稳定性
  - 未确认拒绝执行
  - 幂等执行
  - action_type 不匹配拒绝
  - DRAFT_REGISTRY 注册
  - AI 不自动激活（lifecycle_status=draft）

对齐 docs/专业因子库开发计划.md §WP4 和 docs/因子设置与专业因子库改造方案.md §7。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.router import api_router
from app.db.session import get_db
from app.models.custom_indicator import CustomIndicator, CustomIndicatorVersion
from app.models.factor import Factor
from app.schemas.factor_library import (
    CustomIndicatorPromoteRequest,
    FactorDraftCreate,
)
from app.services.ai.context_pack import build_context_pack
from app.services.ai.drafts import DRAFT_REGISTRY, get_draft_functions
from app.services.ai.drafts.factor_draft import (
    draft_factor,
    execute_factor,
    preview_factor,
)
from app.services.ai_session_service import (
    add_action_audit,
    add_message,
    confirm_action,
    create_session,
)
from app.services.factors.factor_registry import (
    get_factor_by_code,
    list_factor_versions,
    promote_factor_from_indicator,
)


pytestmark = pytest.mark.whitebox


# ── 工具函数 ──────────────────────────────────────────────


def _make_indicator(
    db_session,
    *,
    key: str = "rsi_14",
    name: str = "RSI 14",
    value_type: str = "number",
    formula: str = "close",
    params_json: str = "[]",
    category: str = "momentum",
    description: str = "RSI 指标",
) -> CustomIndicator:
    """创建一个自定义指标（含版本记录）。"""
    ind = CustomIndicator(
        name=name,
        key=key,
        description=description,
        category=category,
        formula=formula,
        value_type=value_type,
        params_json=params_json,
        scope_json='["backtest", "discovery"]',
        enabled=True,
    )
    db_session.add(ind)
    db_session.flush()
    db_session.add(CustomIndicatorVersion(
        indicator_id=ind.id,
        version=1,
        formula=formula,
        params_json=params_json,
        value_type=value_type,
        change_note="init",
    ))
    db_session.commit()
    db_session.refresh(ind)
    return ind


def _make_audit(db_session, action_type: str, suggested_payload: dict):
    """创建一条 AIActionAudit 记录（默认未确认）。"""
    session = create_session(
        db_session, title="因子草案测试会话", source_page="research"
    )
    message = add_message(
        db_session,
        session_id=session.id,
        role="assistant",
        content=f"建议执行 {action_type}",
    )
    audit = add_action_audit(
        db_session,
        message_id=message.id,
        action_type=action_type,
        suggested_payload=suggested_payload,
    )
    return audit


def _make_test_client(db_session) -> TestClient:
    """构建覆盖了 get_db 依赖的 TestClient。"""
    app = FastAPI()
    app.include_router(api_router)

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _default_request(**kwargs) -> CustomIndicatorPromoteRequest:
    """构造默认提升请求。"""
    return CustomIndicatorPromoteRequest(**kwargs)


# ══════════════════════════════════════════════════════════
# Part 1: WP4-01 数值指标提升（promote_factor_from_indicator）
# ══════════════════════════════════════════════════════════


def test_promote_number_indicator_creates_draft(db_session):
    """number 指标提升创建 draft 状态因子和版本。"""
    ind = _make_indicator(db_session, key="rsi_14", formula="close")
    req = _default_request()

    factor, version, source_mapping = promote_factor_from_indicator(
        db_session, indicator_id=ind.id, request=req, indicator_row=ind, indicator_version=1
    )
    db_session.commit()

    assert factor.code == "rsi_14"
    assert factor.name == "RSI 14"
    assert factor.lifecycle_status == "draft"
    assert factor.origin == "user"
    assert factor.is_active == 0  # 草稿不激活
    assert version.formula_expr == "close"
    assert version.is_latest == 1
    assert version.version == 1
    assert version.created_via == "manual"


def test_promote_boolean_indicator_rejected(db_session):
    """boolean 指标拒绝提升，抛 indicator_not_number。"""
    ind = _make_indicator(db_session, key="golden_cross", value_type="boolean")
    req = _default_request()

    with pytest.raises(ValueError, match="indicator_not_number"):
        promote_factor_from_indicator(
            db_session, indicator_id=ind.id, request=req, indicator_row=ind
        )


def test_promote_indicator_not_found(db_session):
    """指标不存在抛 indicator_not_found。"""
    req = _default_request()
    with pytest.raises(ValueError, match="indicator_not_found"):
        promote_factor_from_indicator(db_session, indicator_id=99999, request=req)


def test_promote_invalid_code_from_numeric_key(db_session):
    """key 以数字开头时无法生成合法因子 code，抛 invalid_factor_code。"""
    ind = _make_indicator(db_session, key="123_numeric_start", formula="close")
    req = _default_request()  # 不传 code，依赖 key 自动生成

    with pytest.raises(ValueError, match="invalid_factor_code"):
        promote_factor_from_indicator(
            db_session, indicator_id=ind.id, request=req, indicator_row=ind
        )


def test_promote_params_list_to_dict_conversion(db_session):
    """指标 params_json 为 list[dict] 时正确转为因子 params dict。"""
    ind = _make_indicator(
        db_session,
        key="sma_20",
        formula="sma(close, 20)",
        params_json=json.dumps([
            {"name": "window", "value": 20},
            {"name": "offset", "value": 0},
        ]),
    )
    req = _default_request()

    factor, version, _ = promote_factor_from_indicator(
        db_session, indicator_id=ind.id, request=req, indicator_row=ind, indicator_version=1
    )
    db_session.commit()

    params = json.loads(version.params_json)
    assert params == {"window": 20, "offset": 0}


def test_promote_source_mapping_recorded(db_session):
    """source_mapping 含 indicator_id/key/version/promoted_at 溯源字段。"""
    ind = _make_indicator(db_session, key="momentum_5d", formula="close")
    req = _default_request()

    factor, version, source_mapping = promote_factor_from_indicator(
        db_session, indicator_id=ind.id, request=req, indicator_row=ind, indicator_version=3
    )
    db_session.commit()

    assert source_mapping["source_type"] == "custom_indicator"
    assert source_mapping["indicator_id"] == ind.id
    assert source_mapping["indicator_key"] == "momentum_5d"
    assert source_mapping["indicator_version"] == 3
    assert "promoted_at" in source_mapping

    # 版本的 source_mapping_json 也保存了溯源
    stored = json.loads(version.source_mapping_json)
    assert stored["indicator_id"] == ind.id


def test_promote_idempotent_same_content_returns_existing(db_session):
    """重复提升同指标返回既有版本，不创建新版本（execution_plan_hash 匹配）。"""
    ind = _make_indicator(db_session, key="ep_proxy", formula="close")
    req = _default_request()

    factor1, version1, sm1 = promote_factor_from_indicator(
        db_session, indicator_id=ind.id, request=req, indicator_row=ind, indicator_version=1
    )
    db_session.commit()

    # 第二次提升同一指标
    factor2, version2, sm2 = promote_factor_from_indicator(
        db_session, indicator_id=ind.id, request=req, indicator_row=ind, indicator_version=1
    )
    db_session.commit()

    assert factor1.id == factor2.id
    assert version1.id == version2.id  # 同一版本
    assert version1.version == version2.version
    # 数据库中只有一个版本
    versions = list_factor_versions(db_session, factor1.id)
    assert len(versions) == 1


def test_promote_custom_code_override(db_session):
    """request.code 覆盖 indicator.key 自动生成。"""
    ind = _make_indicator(db_session, key="auto_key", formula="close")
    req = CustomIndicatorPromoteRequest(code="custom_factor_code", name="自定义名称")

    factor, version, _ = promote_factor_from_indicator(
        db_session, indicator_id=ind.id, request=req, indicator_row=ind
    )
    db_session.commit()

    assert factor.code == "custom_factor_code"
    assert factor.name == "自定义名称"


def test_promote_existing_factor_different_formula_creates_new_version(db_session):
    """同 code 但公式不同时创建新版本。"""
    ind1 = _make_indicator(db_session, key="shared_code", formula="close")
    req = _default_request()

    factor1, version1, _ = promote_factor_from_indicator(
        db_session, indicator_id=ind1.id, request=req, indicator_row=ind1, indicator_version=1
    )
    db_session.commit()

    # 修改指标公式后再次提升（同 code）
    ind1.formula = "volume"
    db_session.commit()

    factor2, version2, _ = promote_factor_from_indicator(
        db_session, indicator_id=ind1.id, request=req, indicator_row=ind1, indicator_version=2
    )
    db_session.commit()

    assert factor1.id == factor2.id  # 同一因子
    assert version1.id != version2.id  # 不同版本
    assert version2.version == 2
    assert version2.is_latest == 1
    assert version1.is_latest == 0
    assert version2.formula_expr == "volume"


def test_promote_does_not_auto_activate(db_session):
    """提升创建的因子 lifecycle_status=draft，不自动进入 candidate/active。"""
    ind = _make_indicator(db_session, key="draft_only", formula="close")
    req = _default_request()

    factor, version, _ = promote_factor_from_indicator(
        db_session, indicator_id=ind.id, request=req, indicator_row=ind
    )
    db_session.commit()

    assert factor.lifecycle_status == "draft"
    assert factor.is_active == 0
    # legacy status 也应是 draft
    assert factor.status == "draft"


# ══════════════════════════════════════════════════════════
# Part 2: WP4-01 API 端点（promote_indicator_to_factor）
# ══════════════════════════════════════════════════════════


def test_api_promote_number_returns_201(db_session):
    """API：number 指标提升返回 201 + 结构化响应。"""
    ind = _make_indicator(db_session, key="api_number", formula="close")
    client = _make_test_client(db_session)

    resp = client.post(
        f"/api/v1/settings/custom-indicators/{ind.id}/promote-to-factor",
        json={},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["success"] is True
    assert body["factor_code"] == "api_number"
    assert body["lifecycle_status"] == "draft"
    assert body["origin"] == "user"
    assert body["source_mapping"]["indicator_id"] == ind.id
    assert "factor_version_id" in body


def test_api_promote_boolean_returns_422(db_session):
    """API：boolean 指标提升返回 422 + error_code=indicator_not_number。"""
    ind = _make_indicator(db_session, key="api_bool", value_type="boolean")
    client = _make_test_client(db_session)

    resp = client.post(
        f"/api/v1/settings/custom-indicators/{ind.id}/promote-to-factor",
        json={},
    )
    assert resp.status_code == 422
    body = resp.json()
    detail = body["detail"]
    assert isinstance(detail, dict)
    assert detail["error_code"] == "indicator_not_number"
    assert detail["retryable"] is False


def test_api_promote_not_found_returns_404(db_session):
    """API：不存在的指标返回 404。"""
    client = _make_test_client(db_session)
    resp = client.post(
        "/api/v1/settings/custom-indicators/99999/promote-to-factor",
        json={},
    )
    assert resp.status_code == 404


def test_api_promote_idempotent_no_duplicate(db_session):
    """API：重复调用不创建重复因子。"""
    ind = _make_indicator(db_session, key="api_idem", formula="close")
    client = _make_test_client(db_session)

    resp1 = client.post(
        f"/api/v1/settings/custom-indicators/{ind.id}/promote-to-factor", json={}
    )
    assert resp1.status_code == 201
    factor_id_1 = resp1.json()["factor_id"]

    resp2 = client.post(
        f"/api/v1/settings/custom-indicators/{ind.id}/promote-to-factor", json={}
    )
    assert resp2.status_code == 201
    factor_id_2 = resp2.json()["factor_id"]

    assert factor_id_1 == factor_id_2
    # 数据库中只有一个因子
    factors = db_session.execute(
        select(Factor).where(Factor.code == "api_idem")
    ).scalars().all()
    assert len(factors) == 1


# ══════════════════════════════════════════════════════════
# Part 3: WP4-03/04 AI 因子草案（draft_factor / preview_factor / execute_factor）
# ══════════════════════════════════════════════════════════


def _valid_ai_suggestion() -> dict:
    """构造一个合法的 AI 因子建议。"""
    return {
        "code": "ai_momentum_5d",
        "name": "AI 动量因子 5日",
        "category": "momentum",
        "formula_expr": "close",
        "params": {},
        "direction": "higher_better",
        "factor_kind": "continuous",
        "risk_level": "medium",
        "description": "基于收盘价的动量因子",
        "thesis": "短期动量效应在 A 股显著",
        "change_note": "ai assisted draft",
    }


def test_draft_factor_valid_returns_confirmation(db_session):
    """合法 AI 草案返回 requires_confirmation=True 且不写 DB。"""
    context = build_context_pack(db_session, "建议一个动量因子", "research")
    suggestion = _valid_ai_suggestion()

    result = draft_factor(db_session, context, suggestion)

    assert result["draft_type"] == "draft_factor"
    assert result["requires_confirmation"] is True
    assert result["validation_status"] == "valid"
    assert result["validation_errors"] == []
    assert result["suggested_payload"]["code"] == "ai_momentum_5d"
    # 预览含 content_hash 和 execution_plan
    assert "content_hash" in result["preview"]["extra"]
    assert "execution_plan" in result["preview"]["extra"]
    # 第一步不写 DB
    existing = get_factor_by_code(db_session, "ai_momentum_5d")
    assert existing is None


def test_draft_factor_invalid_formula_returns_errors(db_session):
    """非法公式（未知字段）返回 validation_status=invalid。"""
    context = build_context_pack(db_session, "建议因子", "research")
    suggestion = _valid_ai_suggestion()
    suggestion["formula_expr"] = "unknown_field_xyz + 1"

    result = draft_factor(db_session, context, suggestion)

    assert result["validation_status"] == "invalid"
    assert len(result["validation_errors"]) > 0
    # 错误信息含 error_code
    assert any("unknown_field" in e or "公式校验失败" in e for e in result["validation_errors"])


def test_draft_factor_missing_required_fields(db_session):
    """缺少必填字段返回 invalid。"""
    context = build_context_pack(db_session, "建议因子", "research")
    suggestion = _valid_ai_suggestion()
    suggestion["code"] = ""
    suggestion["name"] = ""

    result = draft_factor(db_session, context, suggestion)

    assert result["validation_status"] == "invalid"
    assert any("code" in e for e in result["validation_errors"])
    assert any("name" in e for e in result["validation_errors"])


def test_draft_factor_content_hash_stable(db_session):
    """相同输入产生相同 content_hash。"""
    context = build_context_pack(db_session, "建议因子", "research")
    suggestion = _valid_ai_suggestion()

    result1 = draft_factor(db_session, context, suggestion)
    result2 = draft_factor(db_session, context, suggestion)

    hash1 = result1["preview"]["extra"]["content_hash"]
    hash2 = result2["preview"]["extra"]["content_hash"]
    assert hash1 == hash2
    assert len(hash1) == 16  # SHA256[:16]


def test_draft_factor_content_hash_changes_with_formula(db_session):
    """公式变化时 content_hash 变化。"""
    context = build_context_pack(db_session, "建议因子", "research")
    suggestion = _valid_ai_suggestion()

    result1 = draft_factor(db_session, context, suggestion)
    suggestion2 = dict(suggestion)
    suggestion2["formula_expr"] = "volume"
    result2 = draft_factor(db_session, context, suggestion2)

    assert result1["preview"]["extra"]["content_hash"] != result2["preview"]["extra"]["content_hash"]


def test_preview_factor_duplicate_code_detected(db_session):
    """重名检查：已存在的 code 被检测出来。"""
    # 先创建一个因子
    from app.services.factors.factor_registry import create_factor_draft
    draft = FactorDraftCreate(code="dup_code", name="已存在因子", category="momentum")
    create_factor_draft(db_session, draft=draft)
    db_session.commit()

    payload = _valid_ai_suggestion()
    payload["code"] = "dup_code"

    result = preview_factor(db_session, payload)

    assert result["is_valid"] is False
    assert any("因子代码已存在" in e for e in result["errors"])


def test_preview_factor_invalid_direction(db_session):
    """非法 direction 被检测。"""
    payload = _valid_ai_suggestion()
    payload["direction"] = "invalid_direction"

    result = preview_factor(db_session, payload)

    assert result["is_valid"] is False
    assert any("direction" in e for e in result["errors"])


def test_preview_factor_does_not_write_db(db_session):
    """preview_factor 是 dry-run，不写 DB。"""
    payload = _valid_ai_suggestion()
    preview_factor(db_session, payload)

    existing = get_factor_by_code(db_session, "ai_momentum_5d")
    assert existing is None


def test_execute_factor_not_confirmed_rejected(db_session):
    """未确认的审计记录拒绝执行，不产生 DB 变化。"""
    suggestion = _valid_ai_suggestion()
    audit = _make_audit(db_session, "draft_factor", suggestion)

    result = execute_factor(db_session, audit.id)

    assert result["success"] is False
    assert result["error"] == "not_confirmed"
    # 未写 DB
    assert get_factor_by_code(db_session, "ai_momentum_5d") is None


def test_execute_factor_confirmed_creates_draft(db_session):
    """确认后执行创建 draft 因子和版本。"""
    suggestion = _valid_ai_suggestion()
    audit = _make_audit(db_session, "draft_factor", suggestion)
    confirm_action(db_session, audit.id)

    result = execute_factor(db_session, audit.id)

    assert result["success"] is True
    assert result["factor_code"] == "ai_momentum_5d"
    assert result["lifecycle_status"] == "draft"
    assert "factor_version_id" in result

    # DB 中确实创建了因子
    factor = get_factor_by_code(db_session, "ai_momentum_5d")
    assert factor is not None
    assert factor.lifecycle_status == "draft"
    assert factor.origin == "user"
    versions = list_factor_versions(db_session, factor.id)
    assert len(versions) == 1


def test_execute_factor_idempotent(db_session):
    """重复执行返回既有因子，不创建重复。"""
    suggestion = _valid_ai_suggestion()
    audit = _make_audit(db_session, "draft_factor", suggestion)
    confirm_action(db_session, audit.id)

    result1 = execute_factor(db_session, audit.id)
    assert result1["success"] is True
    factor_id_1 = result1["factor_id"]

    result2 = execute_factor(db_session, audit.id)
    assert result2["success"] is True
    assert result2.get("idempotent") is True
    assert result2["factor_id"] == factor_id_1

    # DB 中只有一个因子
    factors = db_session.execute(
        select(Factor).where(Factor.code == "ai_momentum_5d")
    ).scalars().all()
    assert len(factors) == 1


def test_execute_factor_action_type_mismatch(db_session):
    """action_type 不匹配时拒绝执行。"""
    suggestion = _valid_ai_suggestion()
    # 用 draft_indicator 类型创建审计，但调用 execute_factor
    audit = _make_audit(db_session, "draft_indicator", suggestion)
    confirm_action(db_session, audit.id)

    result = execute_factor(db_session, audit.id)

    assert result["success"] is False
    assert result["error"] == "action_type_mismatch"


def test_execute_factor_audit_not_found(db_session):
    """审计记录不存在时返回 audit_not_found。"""
    result = execute_factor(db_session, 99999)
    assert result["success"] is False
    assert result["error"] == "audit_not_found"


def test_execute_factor_does_not_auto_activate(db_session):
    """AI 草案创建的因子不自动提交 candidate/active。"""
    suggestion = _valid_ai_suggestion()
    audit = _make_audit(db_session, "draft_factor", suggestion)
    confirm_action(db_session, audit.id)

    result = execute_factor(db_session, audit.id)

    assert result["success"] is True
    factor = get_factor_by_code(db_session, "ai_momentum_5d")
    assert factor.lifecycle_status == "draft"
    assert factor.is_active == 0
    # 不存在 TransitionAudit 记录（未触发状态迁移）
    from app.models.factor_evaluation import TransitionAudit
    audits = db_session.execute(
        select(TransitionAudit).where(TransitionAudit.factor_id == factor.id)
    ).scalars().all()
    assert len(audits) == 0


def test_draft_factor_registered_in_registry():
    """draft_factor 已注册到 DRAFT_REGISTRY。"""
    assert "draft_factor" in DRAFT_REGISTRY
    funcs = get_draft_functions("draft_factor")
    assert funcs is not None
    draft_fn, preview_fn, execute_fn = funcs
    assert draft_fn is draft_factor
    assert preview_fn is preview_factor
    assert execute_fn is execute_factor


def test_ai_suggestion_api_key_not_in_payload(db_session):
    """AI 建议中若混入 api_key 等敏感字段，不应进入 suggested_payload。"""
    context = build_context_pack(db_session, "建议因子", "research")
    suggestion = _valid_ai_suggestion()
    suggestion["api_key"] = "sk-secret-12345"  # 模拟 AI 误传敏感字段

    result = draft_factor(db_session, context, suggestion)

    # suggested_payload 只保留 FactorDraftSchema 定义的字段，api_key 不应出现
    assert "api_key" not in result["suggested_payload"]
    assert result["suggested_payload"]["code"] == "ai_momentum_5d"


# ══════════════════════════════════════════════════════════
# Part 4: WP4-05 AI 草案确认流程 API（/ai/drafts/{audit_id}/*）
# ══════════════════════════════════════════════════════════


def _make_factor_audit(db_session, suggested_payload: dict | None = None):
    """创建一条 draft_factor 审计记录（未确认）。"""
    return _make_audit(
        db_session,
        action_type="draft_factor",
        suggested_payload=suggested_payload or _valid_ai_suggestion(),
    )


def test_api_get_draft_returns_original_and_current(db_session):
    """GET /ai/drafts/{audit_id} 返回原始建议与当前 payload。"""
    audit = _make_factor_audit(db_session)
    db_session.commit()

    client = _make_test_client(db_session)
    resp = client.get(f"/api/v1/ai/drafts/{audit.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["audit_id"] == audit.id
    assert body["action_type"] == "draft_factor"
    assert body["user_confirmed"] is False
    assert body["was_modified"] is False
    assert body["original_suggested_payload"]["code"] == "ai_momentum_5d"
    assert body["current_payload"]["code"] == "ai_momentum_5d"


def test_api_get_draft_not_found_returns_404(db_session):
    """不存在的 audit_id 返回 404。"""
    client = _make_test_client(db_session)
    resp = client.get("/api/v1/ai/drafts/99999")
    assert resp.status_code == 404


def test_api_preview_draft_validates_payload(db_session):
    """POST /ai/drafts/{audit_id}/preview 对当前 payload 做 dry-run 校验。"""
    audit = _make_factor_audit(db_session)
    db_session.commit()

    client = _make_test_client(db_session)
    resp = client.post(f"/api/v1/ai/drafts/{audit.id}/preview")

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_valid"] is True
    assert body["errors"] == []
    assert len(body["changes"]) > 0


def test_api_preview_draft_with_modified_payload(db_session):
    """POST /ai/drafts/{audit_id}/preview 携带 modified_payload 做校验。"""
    audit = _make_factor_audit(db_session)
    db_session.commit()

    client = _make_test_client(db_session)
    # 修改为非法 formula
    resp = client.post(
        f"/api/v1/ai/drafts/{audit.id}/preview",
        json={"modified_payload": {**_valid_ai_suggestion(), "formula_expr": "__import__('os')"}},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_valid"] is False
    assert len(body["errors"]) > 0


def test_api_confirm_draft_marks_confirmed(db_session):
    """POST /ai/drafts/{audit_id}/confirm 标记 user_confirmed=True。"""
    audit = _make_factor_audit(db_session)
    db_session.commit()

    client = _make_test_client(db_session)
    resp = client.post(f"/api/v1/ai/drafts/{audit.id}/confirm")

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_confirmed"] is True
    assert body["confirmed_at"] is not None


def test_api_confirm_draft_with_modified_payload_preserves_original(db_session):
    """confirm 携带 modified_payload 时，原始建议保存在 original_suggested_payload。"""
    audit = _make_factor_audit(db_session)
    db_session.commit()

    original_name = audit.suggested_payload
    client = _make_test_client(db_session)
    modified = {**_valid_ai_suggestion(), "name": "修改后的名称"}
    resp = client.post(
        f"/api/v1/ai/drafts/{audit.id}/confirm",
        json={"modified_payload": modified},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_confirmed"] is True
    assert body["was_modified"] is True
    assert body["current_payload"]["name"] == "修改后的名称"
    assert body["original_suggested_payload"]["name"] == "AI 动量因子 5日"


def test_api_reject_draft_records_reason(db_session):
    """POST /ai/drafts/{audit_id}/reject 记录拒绝原因。"""
    audit = _make_factor_audit(db_session)
    db_session.commit()

    client = _make_test_client(db_session)
    resp = client.post(
        f"/api/v1/ai/drafts/{audit.id}/reject",
        json={"reason": "公式不合理"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_confirmed"] is False
    assert body["rejected_reason"] == "公式不合理"


def test_api_execute_draft_not_confirmed_returns_409(db_session):
    """未确认的草案 execute 返回 409 error_code=not_confirmed。"""
    audit = _make_factor_audit(db_session)
    db_session.commit()

    client = _make_test_client(db_session)
    resp = client.post(f"/api/v1/ai/drafts/{audit.id}/execute")

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["error_code"] == "not_confirmed"


def test_api_execute_draft_confirmed_creates_factor(db_session):
    """确认后 execute 创建 draft 因子。"""
    audit = _make_factor_audit(db_session)
    db_session.commit()

    client = _make_test_client(db_session)
    # 先确认
    client.post(f"/api/v1/ai/drafts/{audit.id}/confirm")
    # 再执行
    resp = client.post(f"/api/v1/ai/drafts/{audit.id}/execute")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["factor_code"] == "ai_momentum_5d"
    assert body["lifecycle_status"] == "draft"

    # 因子确实创建
    factor = get_factor_by_code(db_session, "ai_momentum_5d")
    assert factor is not None
    assert factor.lifecycle_status == "draft"


def test_api_execute_draft_idempotent_returns_existing(db_session):
    """重复 execute 返回既有成功结果，不创建重复。"""
    audit = _make_factor_audit(db_session)
    db_session.commit()

    client = _make_test_client(db_session)
    client.post(f"/api/v1/ai/drafts/{audit.id}/confirm")
    resp1 = client.post(f"/api/v1/ai/drafts/{audit.id}/execute")
    resp2 = client.post(f"/api/v1/ai/drafts/{audit.id}/execute")

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["factor_id"] == resp2.json()["factor_id"]

    # 只创建一个因子
    factors = db_session.execute(
        select(Factor).where(Factor.code == "ai_momentum_5d")
    ).scalars().all()
    assert len(factors) == 1


def test_api_confirm_executed_draft_returns_409(db_session):
    """已执行的草案不可重新确认（409 already_executed）。"""
    audit = _make_factor_audit(db_session)
    db_session.commit()

    client = _make_test_client(db_session)
    client.post(f"/api/v1/ai/drafts/{audit.id}/confirm")
    client.post(f"/api/v1/ai/drafts/{audit.id}/execute")

    resp = client.post(f"/api/v1/ai/drafts/{audit.id}/confirm")
    assert resp.status_code == 409


def test_api_full_flow_confirm_with_modification_then_execute(db_session):
    """完整流程：AI 建议 → 用户修改 → 确认 → 执行 → 创建修改后的因子。"""
    audit = _make_factor_audit(db_session)
    db_session.commit()

    client = _make_test_client(db_session)
    # 用户修改 code 和 name
    modified = {
        **_valid_ai_suggestion(),
        "code": "ai_momentum_10d",
        "name": "AI 动量因子 10日",
    }
    # 预览修改后的 payload
    preview_resp = client.post(
        f"/api/v1/ai/drafts/{audit.id}/preview",
        json={"modified_payload": modified},
    )
    assert preview_resp.status_code == 200
    assert preview_resp.json()["is_valid"] is True

    # 确认（携带修改）
    confirm_resp = client.post(
        f"/api/v1/ai/drafts/{audit.id}/confirm",
        json={"modified_payload": modified},
    )
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["was_modified"] is True

    # 执行
    exec_resp = client.post(f"/api/v1/ai/drafts/{audit.id}/execute")
    assert exec_resp.status_code == 200
    body = exec_resp.json()
    assert body["success"] is True
    assert body["factor_code"] == "ai_momentum_10d"

    # 验证创建的是修改后的因子
    factor = get_factor_by_code(db_session, "ai_momentum_10d")
    assert factor is not None
    assert factor.name == "AI 动量因子 10日"

