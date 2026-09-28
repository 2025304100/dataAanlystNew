"""白盒：通用应用设置 `app_settings`（服务优先级 + HTTP 契约 + 发现中心接线）。

对应体检报告 §十四（PT-DEF-15 的产品侧收口：把运维级 env 开关升级为用户级持久化）。

钉住的行为：
1. 生效优先级 **表 > env > 默认**（顺序错就会发生"设置页改了但没效果"）；
2. 只接受登记过的键，值按登记类型归一化（防"往设置表里乱插没人读的键"）；
3. 两个参数类错误码在 `ERROR_CODE_LIBRARY` 里登记过（未登记会回退 UNKNOWN_ERROR
   且 retryable=True，与"参数不对、重试无用"相反 —— PT-DEF-5/12 同族教训）；
4. 读路径打翻不得波及主链路：拿不到表/会话时退回 env → 默认值，绝不抛错
   （§七.9 "读一个开关把调用方事务打翻"那一类）；
5. 端点契约：GET 带 source（table/env/default），PUT 落库并回显 updated_by。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.schemas.errors import ERROR_CODE_LIBRARY
from app.services import app_settings as svc
from app.services.app_settings import (
    DISCOVERY_AUTO_DATA_PREP_ENV,
    DISCOVERY_AUTO_DATA_PREP_KEY,
    SettingKeyUnknown,
    SettingValueInvalid,
    resolve_bool_setting,
)

pytestmark = pytest.mark.whitebox

# env 变量名不另写一份：跟登记表用同一个常量（否则改了登记表忘改用例，会测不到真行为）
ENV_VAR = DISCOVERY_AUTO_DATA_PREP_ENV


def _client(db_session) -> TestClient:
    from app.api.routes import app_settings as route_mod

    app = FastAPI()
    app.include_router(route_mod.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


# ─────────────────────────────────────────────────────────────
# 1. 服务层：优先级与类型校（纯逻辑，不依赖 HTTP）
# ─────────────────────────────────────────────────────────────

def test_registry_declares_discovery_switch_with_expected_shape():
    spec = svc.APP_SETTING_REGISTRY[DISCOVERY_AUTO_DATA_PREP_KEY]
    assert spec["type"] == "bool"
    assert spec["default"] is True
    assert spec["env_var"] == ENV_VAR
    # 登记表与 discovery 侧别名必须是同一个值，否则“改了开关读不到”
    from app.services import discovery_data_prep

    assert discovery_data_prep.AUTO_DATA_PREP_ENV == ENV_VAR
    # 设置页靠这两条文案渲染，缺一个就变成"开关没有说明"
    assert spec["label_zh"] and spec["description_zh"]


def test_table_value_wins_over_env(db_session, monkeypatch):
    """表里写 true、env 写 0 → 表赢（用户刚在页面上打开，不该被环境变量吃掉）。"""
    monkeypatch.setenv(ENV_VAR, "0")
    svc.set_app_setting(db_session, DISCOVERY_AUTO_DATA_PREP_KEY, True, actor="test")

    assert resolve_bool_setting(
        DISCOVERY_AUTO_DATA_PREP_KEY, default=True, env_var=ENV_VAR, db=db_session
    ) is True
    # 反向也要成立：表里关、env 没设置 → 关
    svc.set_app_setting(db_session, DISCOVERY_AUTO_DATA_PREP_KEY, False, actor="test")
    assert resolve_bool_setting(
        DISCOVERY_AUTO_DATA_PREP_KEY, default=True, env_var=ENV_VAR, db=db_session
    ) is False


def test_env_used_when_table_has_no_row(db_session, monkeypatch):
    """表里没有这个键 → 回落到 env（运维级强制关闭仍然可用）。"""
    monkeypatch.setenv(ENV_VAR, "0")
    assert resolve_bool_setting(
        DISCOVERY_AUTO_DATA_PREP_KEY, default=True, env_var=ENV_VAR, db=db_session
    ) is False

    monkeypatch.delenv(ENV_VAR, raising=False)
    assert resolve_bool_setting(
        DISCOVERY_AUTO_DATA_PREP_KEY, default=True, env_var=ENV_VAR, db=db_session
    ) is True


def test_coerce_normalizes_and_rejects_bad_input(db_session):
    assert svc.coerce_setting_value(DISCOVERY_AUTO_DATA_PREP_KEY, 1) is True
    assert svc.coerce_setting_value(DISCOVERY_AUTO_DATA_PREP_KEY, "false") is False

    with pytest.raises(SettingKeyUnknown):
        svc.coerce_setting_value("nope.not_registered", True)
    with pytest.raises(SettingValueInvalid):
        svc.coerce_setting_value(DISCOVERY_AUTO_DATA_PREP_KEY, "banana")


def test_resolve_bool_survives_unreadable_table(monkeypatch):
    """表读不了（未迁移/连接坏）时必须退回，不能把调用方打翻。"""

    class _Boom:
        def get(self, *_a, **_k):
            raise RuntimeError("no such table: app_settings")

        def close(self):
            pass

    monkeypatch.setattr("app.db.session.get_session_local", lambda: _Boom())
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert resolve_bool_setting(
        DISCOVERY_AUTO_DATA_PREP_KEY, default=True, env_var=ENV_VAR
    ) is True


def test_list_effective_settings_reports_value_source(db_session, monkeypatch):
    """source 必须区分 table/env/default —— 否则界面说不清"你改的"还是"运维锁的"。"""
    monkeypatch.delenv(ENV_VAR, raising=False)
    items = svc.list_effective_settings(db_session)
    assert [i["key"] for i in items] == [DISCOVERY_AUTO_DATA_PREP_KEY]
    assert items[0]["source"] == "default"

    monkeypatch.setenv(ENV_VAR, "0")
    assert svc.list_effective_settings(db_session)[0]["source"] == "env"

    svc.set_app_setting(db_session, DISCOVERY_AUTO_DATA_PREP_KEY, True, actor="tester")
    row = svc.list_effective_settings(db_session)[0]
    assert row["source"] == "table" and row["value"] is True
    assert row["updated_by"] == "tester"


# ─────────────────────────────────────────────────────────────
# 2. HTTP 契约：GET / PUT /settings/app
# ─────────────────────────────────────────────────────────────

def test_get_settings_app_returns_registered_items_with_metadata(db_session):
    resp = _client(db_session).get("/settings/app")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and body
    item = next(i for i in body if i["key"] == DISCOVERY_AUTO_DATA_PREP_KEY)
    assert item["type"] == "bool" and item["source"] in {"table", "env", "default"}
    assert item["label_zh"]


def test_put_settings_app_persists_and_takes_effect(db_session):
    client = _client(db_session)
    resp = client.put(
        f"/settings/app/{DISCOVERY_AUTO_DATA_PREP_KEY}",
        json={"value": False},
        headers={"X-User": "tester"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["value"] is False and body["source"] == "table"
    assert body["updated_by"] == "settings-page:tester"

    # 写进去就必须马上生效（同一会话内可验证，不做进程缓存）
    assert (
        svc.resolve_bool_setting(
            DISCOVERY_AUTO_DATA_PREP_KEY, default=True, env_var=ENV_VAR, db=db_session
        )
        is False
    )


def test_put_unknown_key_returns_400_with_registered_error_code(db_session):
    resp = _client(db_session).put("/settings/app/made.up.key", json={"value": True})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "UNKNOWN_SETTING_KEY"
    assert DISCOVERY_AUTO_DATA_PREP_KEY in detail["known_keys"]
    # 错误码必须登记（否则全局处理器回退 UNKNOWN_ERROR + retryable=True）
    assert detail["error_code"] in ERROR_CODE_LIBRARY


def test_put_bad_value_returns_422_with_registered_error_code(db_session):
    resp = _client(db_session).put(
        f"/settings/app/{DISCOVERY_AUTO_DATA_PREP_KEY}", json={"value": "banana"}
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "SETTING_VALUE_INVALID"
    assert detail["error_code"] in ERROR_CODE_LIBRARY


# ─────────────────────────────────────────────────────────────
# 3. 发现中心接线：设置页的值真的驱动快扫
# ─────────────────────────────────────────────────────────────

def test_discovery_auto_data_prep_reads_table_setting(db_session, monkeypatch):
    """表里关掉后，`auto_data_prep_enabled(db)` 必须返回 False —— 且 env 没参与。"""
    from app.services import discovery_data_prep

    monkeypatch.delenv(ENV_VAR, raising=False)
    assert discovery_data_prep.auto_data_prep_enabled(db_session) is True

    svc.set_app_setting(db_session, DISCOVERY_AUTO_DATA_PREP_KEY, False, actor="tester")
    assert discovery_data_prep.auto_data_prep_enabled(db_session) is False

    # 表里说开、env 说关 → 表赢（用户在页面上的操作优先于运维兜底）
    monkeypatch.setenv(ENV_VAR, "0")
    svc.set_app_setting(db_session, DISCOVERY_AUTO_DATA_PREP_KEY, True, actor="tester")
    assert discovery_data_prep.auto_data_prep_enabled(db_session) is True


def test_fast_scan_skips_auto_data_prep_when_setting_off(db_session, monkeypatch):
    """端到端一条：把设置写进表，快扫就不该起后台数据准备任务。"""
    from app.services import discovery_data_prep, discovery_fast_scan

    started: list[str] = []
    monkeypatch.setattr(
        discovery_data_prep,
        "start_data_prep_task",
        lambda **kw: started.append(str(kw.get("scope") or "")),
    )
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None
    )
    monkeypatch.delenv(ENV_VAR, raising=False)

    # 先确认默认路径仍会启动（否则这条用例什么都没测到）
    discovery_fast_scan.run_fast_scan(scope="cn_stock", min_score=55, db=db_session)
    assert started == ["cn_stock"], started

    started.clear()
    svc.set_app_setting(db_session, DISCOVERY_AUTO_DATA_PREP_KEY, False, actor="tester")
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert result["degraded_reason"] == "no_ready_snapshot"
    assert started == [], "设置已关闭，快扫不应再隐式启动数据准备"
