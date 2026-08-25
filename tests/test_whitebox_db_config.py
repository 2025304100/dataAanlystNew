"""白盒测试 - 数据库配置 API (P2-3 后端补全)。

覆盖 app/api/routes/db_config.py 路由层逻辑：
1. GET /settings/db-config 返回密码脱敏（"****" + 后2位）
2. PUT /settings/db-config 密码以 "****" 开头时从磁盘读原密码
3. POST /settings/db-config/test 连接失败时不暴露密码
4. POST /settings/db-config/migrate 迁移进行中返回 409

这些路由不依赖 Depends(get_db)，用全局 DatabaseManager，需要 mock：
- app.api.routes.db_config.load_db_config
- app.api.routes.db_config.is_migration_running
- app.api.routes.db_config.create_engine
- app.api.routes.db_config.DatabaseManager
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.api.routes.db_config import (
    get_db_config,
    trigger_migration,
    update_db_config,
)
# 注意：导入时必须重命名为不以 test_ 开头的名字，否则 pytest 会把它当作测试用例收集
from app.api.routes.db_config import test_connection as db_test_connection_endpoint
from app.schemas.db_config import DbConfigUpdate, MySQLConfig

pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# 0. Process-level DATABASE_URL override (isolated verification / containers)
# ----------------------------------------------------------------------------

def test_runtime_database_url_env_overrides_persisted_mysql_config(monkeypatch):
    """An explicit environment URL must win over the persisted MySQL config."""
    from unittest.mock import MagicMock

    from app import main

    manager = MagicMock()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///D:/tmp/isolated-ui.db")
    monkeypatch.setattr(main, "load_db_config", lambda: {
        "use_mysql": True,
        "mysql": {"host": "shared-db", "port": 3306, "database": "prod"},
    })
    monkeypatch.setattr(main.DatabaseManager, "get", lambda: manager)

    main.initialize_runtime_database()

    manager.initialize.assert_called_once_with(
        "sqlite:///D:/tmp/isolated-ui.db", db_type="sqlite"
    )


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _make_db_config_update(password="****ab", use_mysql=True) -> DbConfigUpdate:
    """构造一个 DbConfigUpdate 用于测试。"""
    return DbConfigUpdate(
        use_mysql=use_mysql,
        mysql=MySQLConfig(
            host="127.0.0.1",
            port=3306,
            database="qa_db",
            user="qa_user",
            password=password,
        ),
    )


# ----------------------------------------------------------------------------
# 1. GET /settings/db-config 密码脱敏
# ----------------------------------------------------------------------------

def test_get_db_config_masks_password(db_session):
    """【P2-3 后端补全】get_db_config 返回密码脱敏（"****" + 后2位）。

    防止前端拿到完整密码后泄露。
    """
    cfg = {
        "use_mysql": True,
        "mysql": {
            "host": "127.0.0.1",
            "port": 3306,
            "database": "qa_db",
            "user": "qa_user",
            "password": "secret123",  # 末两位 "23"
        },
    }
    with patch("app.api.routes.db_config.load_db_config", return_value=cfg):
        result = get_db_config()
    assert result.use_mysql is True
    assert result.mysql.password == "****23"
    assert result.mysql.host == "127.0.0.1"
    assert result.mysql.database == "qa_db"
    assert result.mysql.user == "qa_user"


def test_get_db_config_short_password_fully_masked():
    """【P2-3 后端补全】密码 <= 2 位时全部脱敏为 "****"。"""
    cfg = {
        "use_mysql": True,
        "mysql": {
            "host": "127.0.0.1",
            "port": 3306,
            "database": "",
            "user": "",
            "password": "ab",  # 仅 2 位
        },
    }
    with patch("app.api.routes.db_config.load_db_config", return_value=cfg):
        result = get_db_config()
    assert result.mysql.password == "****"


def test_get_db_config_no_password_returns_empty():
    """【P2-3 后端补全】无密码时返回空字符串。"""
    cfg = {
        "use_mysql": False,
        "mysql": {
            "host": "127.0.0.1",
            "port": 3306,
            "database": "",
            "user": "",
            "password": "",
        },
    }
    with patch("app.api.routes.db_config.load_db_config", return_value=cfg):
        result = get_db_config()
    assert result.mysql.password == ""
    assert result.use_mysql is False


# ----------------------------------------------------------------------------
# 2. PUT /settings/db-config 密码以 "****" 开头时从磁盘读原密码
# ----------------------------------------------------------------------------

def test_update_db_config_masked_password_reads_from_disk(db_session):
    """【P2-3 后端补全】update_db_config 密码以 "****" 开头时从磁盘读原密码。

    防止用户编辑其他字段时把脱敏密码 "****ab" 当真密码写入磁盘。
    """
    payload = _make_db_config_update(password="****ab", use_mysql=False)

    saved_configs = []

    def _save(cfg):
        saved_configs.append(cfg)

    # mock load_db_config 返回真实密码
    with patch(
        "app.api.routes.db_config.load_db_config",
        return_value={"mysql": {"password": "real_password_xyz"}},
    ), patch("app.api.routes.db_config.save_db_config", side_effect=_save), patch(
        "app.api.routes.db_config.DatabaseManager"
    ) as MockMgr, patch("app.api.routes.db_config.init_db"):
        # 配置 mock DatabaseManager
        mock_mgr_instance = MockMgr.get.return_value
        mock_mgr_instance.db_type = "sqlite"
        mock_mgr_instance.is_mysql = False

        result = update_db_config(payload)

    assert result["status"] == "ok"
    # 验证保存的密码是磁盘原密码，而不是 "****ab"
    assert len(saved_configs) == 1
    assert saved_configs[0]["mysql"]["password"] == "real_password_xyz"


# ----------------------------------------------------------------------------
# 3. POST /settings/db-config/test 连接失败不暴露密码
# ----------------------------------------------------------------------------

def test_test_connection_failure_returns_safe_message(db_session):
    """【P2-3 后端补全】test_connection MySQL 连接失败时返回固定消息，不暴露密码。

    防止 SQLAlchemy 异常信息中包含完整连接串（含密码）泄露给前端。
    """
    payload = _make_db_config_update(password="real_pw", use_mysql=True)

    with patch("app.api.routes.db_config.create_engine") as mock_create_engine:
        # 模拟 create_engine 抛异常（连接失败）
        mock_engine = mock_create_engine.return_value
        mock_engine.connect.side_effect = Exception("Access denied for user 'qa_user'@'localhost' (using password: YES)")

        result = db_test_connection_endpoint(payload)

    assert result.success is False
    # 验证返回消息是固定提示，不包含密码
    assert "real_pw" not in result.message
    assert "检查" in result.message or "失败" in result.message


def test_test_connection_missing_required_fields_returns_failure():
    """【P2-3 后端补全】test_connection 必填字段缺失时返回失败提示。"""
    payload = DbConfigUpdate(
        use_mysql=True,
        mysql=MySQLConfig(host="", port=3306, database="", user="", password=""),
    )

    result = db_test_connection_endpoint(payload)
    assert result.success is False
    assert "请填写" in result.message or "主机" in result.message


# ----------------------------------------------------------------------------
# 4. POST /settings/db-config/migrate 迁移进行中返回 409
# ----------------------------------------------------------------------------

def test_trigger_migration_already_running_raises_409():
    """【P2-3 后端补全】trigger_migration 迁移进行中抛 409。

    防止用户重复触发迁移任务造成数据冲突。
    """
    with patch("app.api.routes.db_config.is_migration_running", return_value=True), patch(
        "app.api.routes.db_config.DatabaseManager"
    ) as MockMgr:
        mock_mgr_instance = MockMgr.get.return_value
        mock_mgr_instance.is_mysql = True

        with pytest.raises(HTTPException) as exc:
            trigger_migration()
    assert exc.value.status_code == 409
    assert "进行中" in exc.value.detail or "running" in exc.value.detail.lower()


def test_trigger_migration_not_mysql_raises_400():
    """【P2-3 后端补全】trigger_migration 非 MySQL 模式抛 400。"""
    with patch("app.api.routes.db_config.is_migration_running", return_value=False), patch(
        "app.api.routes.db_config.DatabaseManager"
    ) as MockMgr:
        mock_mgr_instance = MockMgr.get.return_value
        mock_mgr_instance.is_mysql = False

        with pytest.raises(HTTPException) as exc:
            trigger_migration()
    assert exc.value.status_code == 400
