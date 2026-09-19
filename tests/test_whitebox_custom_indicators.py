"""白盒测试 - 自定义指标 API (P2-3 后端补全)。

覆盖 app/api/routes/custom_indicators.py 路由层逻辑：
1. GET /settings/custom-indicators 列表（scope/enabled 过滤）
2. POST /settings/custom-indicators 创建
3. PUT /settings/custom-indicators/{id} 更新（含版本号 +1）
4. DELETE /settings/custom-indicators/{id} 删除
5. _validate_formula_expr AST 沙箱拒绝非白名单函数（如 __import__、eval、exec）
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routes.custom_indicators import (
    _validate_formula_expr,
    create_custom_indicator,
    delete_custom_indicator,
    list_custom_indicators,
    update_custom_indicator,
)
from app.models.custom_indicator import CustomIndicator, CustomIndicatorVersion
from app.schemas.custom_indicator import (
    CustomIndicatorCreate,
    CustomIndicatorUpdate,
)

pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _make_indicator(db_session, name="QA-MA", key="qa_ma", formula="sma(close, 10)",
                    enabled=True, scope=None) -> CustomIndicator:
    if scope is None:
        scope = ["backtest", "discovery"]
    import json as _json
    row = CustomIndicator(
        name=name,
        key=key,
        description="qa",
        category="custom",
        formula=formula,
        value_type="number",
        params_json=_json.dumps([]),
        scope_json=_json.dumps(scope),
        enabled=enabled,
    )
    db_session.add(row)
    db_session.flush()
    # 写入 v1 版本
    db_session.add(CustomIndicatorVersion(
        indicator_id=row.id,
        version=1,
        formula=row.formula,
        params_json=row.params_json,
        value_type=row.value_type,
        change_note="init",
    ))
    db_session.commit()
    db_session.refresh(row)
    return row


# ----------------------------------------------------------------------------
# 1. _validate_formula_expr AST 沙箱
# ----------------------------------------------------------------------------

def test_validate_formula_expr_accepts_valid_formula():
    """【P2-3 后端补全】_validate_formula_expr 接受白名单函数公式。"""
    valid, reason = _validate_formula_expr("sma(close, 10) > ema(close, 20)")
    assert valid is True
    assert reason is None


def test_validate_formula_expr_rejects_empty():
    """【P2-3 后端补全】_validate_formula_expr 拒绝空公式。"""
    valid, reason = _validate_formula_expr("")
    assert valid is False
    assert "required" in reason.lower()


def test_validate_formula_expr_rejects_dunder_import():
    """【P2-3 后端补全】_validate_formula_expr 拒绝 __import__ 调用。

    防止通过公式注入恶意 Python 代码。
    """
    valid, reason = _validate_formula_expr("__import__('os').system('rm -rf /')")
    assert valid is False
    assert reason is not None


def test_validate_formula_expr_rejects_eval_call():
    """【P2-3 后端补全】_validate_formula_expr 拒绝 eval 调用。"""
    valid, reason = _validate_formula_expr("eval('1+1')")
    assert valid is False
    assert reason is not None


def test_validate_formula_expr_rejects_exec_call():
    """【P2-3 后端补全】_validate_formula_expr 拒绝 exec 调用。"""
    valid, reason = _validate_formula_expr("exec('x=1')")
    assert valid is False
    assert reason is not None


def test_validate_formula_expr_rejects_unknown_variable():
    """【P2-3 后端补全】_validate_formula_expr 拒绝非白名单变量。"""
    valid, reason = _validate_formula_expr("foo + 1")
    assert valid is False
    assert "foo" in reason


# ----------------------------------------------------------------------------
# 2. GET /settings/custom-indicators 列表
# ----------------------------------------------------------------------------

def test_list_custom_indicators_returns_all(db_session):
    """【P2-3 后端补全】list_custom_indicators 返回全部指标。"""
    _make_indicator(db_session, name="QA-A", key="qa_a")
    _make_indicator(db_session, name="QA-B", key="qa_b")

    result = list_custom_indicators(scope=None, enabled=None, db=db_session)
    assert len(result) == 2
    names = {item["name"] for item in result}
    assert names == {"QA-A", "QA-B"}


def test_list_custom_indicators_filter_by_scope(db_session):
    """【P2-3 后端补全】list_custom_indicators 支持 scope 过滤。"""
    _make_indicator(db_session, name="QA-Back", key="qa_back", scope=["backtest"])
    _make_indicator(db_session, name="QA-Disc", key="qa_disc", scope=["discovery"])

    # 只看 backtest scope
    result = list_custom_indicators(scope="backtest", enabled=None, db=db_session)
    assert len(result) == 1
    assert result[0]["name"] == "QA-Back"


def test_list_custom_indicators_filter_by_enabled(db_session):
    """【P2-3 后端补全】list_custom_indicators 支持 enabled 过滤。"""
    _make_indicator(db_session, name="QA-On", key="qa_on", enabled=True)
    _make_indicator(db_session, name="QA-Off", key="qa_off", enabled=False)

    result = list_custom_indicators(scope=None, enabled=False, db=db_session)
    assert len(result) == 1
    assert result[0]["name"] == "QA-Off"


# ----------------------------------------------------------------------------
# 3. POST /settings/custom-indicators 创建
# ----------------------------------------------------------------------------

def test_create_custom_indicator_success(db_session):
    """【P2-3 后端补全】create_custom_indicator 成功创建并写入门档版本。"""
    payload = CustomIndicatorCreate(
        name="QA-New",
        key="qa_new",
        description="qa",
        category="custom",
        formula="sma(close, 5)",
        value_type="number",
        params=[],
        scope=["backtest"],
        enabled=True,
        change_note="init",
    )
    result = create_custom_indicator(payload, db=db_session)
    assert result["id"] is not None
    assert result["name"] == "QA-New"
    assert result["key"] == "qa_new"
    assert result["formula"] == "sma(close, 5)"
    assert result["version"] == 1
    # 验证 DB 中 CustomIndicatorVersion 也已写入 v1
    versions = db_session.query(CustomIndicatorVersion).filter(
        CustomIndicatorVersion.indicator_id == result["id"]
    ).all()
    assert len(versions) == 1
    assert versions[0].version == 1


def test_create_custom_indicator_duplicate_key_raises_409(db_session):
    """【P2-3 后端补全】create_custom_indicator key 重复抛 409。"""
    _make_indicator(db_session, name="QA-Dup", key="qa_dup")
    payload = CustomIndicatorCreate(
        name="QA-Other", key="qa_dup", formula="sma(close, 5)",
    )
    with pytest.raises(HTTPException) as exc:
        create_custom_indicator(payload, db=db_session)
    assert exc.value.status_code == 409


def test_create_custom_indicator_invalid_formula_raises_400(db_session):
    """【P2-3 后端补全】create_custom_indicator 非法公式抛 400。"""
    payload = CustomIndicatorCreate(
        name="QA-Bad", key="qa_bad", formula="__import__('os')",
    )
    with pytest.raises(HTTPException) as exc:
        create_custom_indicator(payload, db=db_session)
    assert exc.value.status_code == 400


# ----------------------------------------------------------------------------
# 4. PUT /settings/custom-indicators/{id} 更新
# ----------------------------------------------------------------------------

def test_update_custom_indicator_formula_increments_version(db_session):
    """【P2-3 后端补全】update_custom_indicator 修改 formula 后版本号 +1。"""
    ind = _make_indicator(db_session, name="QA-Up", key="qa_up", formula="sma(close, 10)")
    assert ind.id is not None
    original_version = list_custom_indicators(scope=None, enabled=None, db=db_session)[0]["version"]
    assert original_version == 1

    payload = CustomIndicatorUpdate(formula="sma(close, 20)", change_note="window change")
    result = update_custom_indicator(ind.id, payload, db=db_session)
    assert result["formula"] == "sma(close, 20)"
    assert result["version"] == 2  # 版本号 +1
    # 验证 DB 中有 v1 + v2 两条版本
    versions = db_session.query(CustomIndicatorVersion).filter(
        CustomIndicatorVersion.indicator_id == ind.id
    ).order_by(CustomIndicatorVersion.version.desc()).all()
    assert len(versions) == 2
    assert versions[0].version == 2
    assert versions[0].formula == "sma(close, 20)"


def test_update_custom_indicator_no_formula_change_keeps_version(db_session):
    """【P2-3 后端补全】仅修改 enabled 时版本号不变。"""
    ind = _make_indicator(db_session, name="QA-NoVer", key="qa_nover", formula="sma(close, 10)")
    payload = CustomIndicatorUpdate(enabled=False)
    result = update_custom_indicator(ind.id, payload, db=db_session)
    assert result["enabled"] is False
    assert result["version"] == 1  # 不变


def test_update_custom_indicator_not_found_raises_404(db_session):
    """【P2-3 后端补全】indicator_id 不存在抛 404。"""
    payload = CustomIndicatorUpdate(enabled=False)
    with pytest.raises(HTTPException) as exc:
        update_custom_indicator(99999, payload, db=db_session)
    assert exc.value.status_code == 404


# ----------------------------------------------------------------------------
# 5. DELETE /settings/custom-indicators/{id} 删除
# ----------------------------------------------------------------------------

def test_delete_custom_indicator_success(db_session):
    """【P2-3 后端补全】delete_custom_indicator 成功删除并清理版本表。"""
    ind = _make_indicator(db_session, name="QA-Del", key="qa_del")
    ind_id = ind.id

    result = delete_custom_indicator(ind_id, db=db_session)
    assert result == {"success": True}
    # 验证已删除
    assert db_session.get(CustomIndicator, ind_id) is None
    # 验证版本表也清理
    versions = db_session.query(CustomIndicatorVersion).filter(
        CustomIndicatorVersion.indicator_id == ind_id
    ).all()
    assert len(versions) == 0


def test_delete_custom_indicator_not_found_raises_404(db_session):
    """【P2-3 后端补全】indicator_id 不存在抛 404。"""
    with pytest.raises(HTTPException) as exc:
        delete_custom_indicator(99999, db=db_session)
    assert exc.value.status_code == 404
