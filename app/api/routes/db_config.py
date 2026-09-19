"""数据库配置管理 API

提供配置读写、连接测试、热切换引擎、数据迁移等端点。
"""
from __future__ import annotations

import logging
import threading
from urllib.parse import quote_plus

from fastapi import APIRouter, HTTPException
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

from app.core.config import (
    build_mysql_url,
    load_db_config,
    save_db_config,
    settings,
)
from app.db.init_db import init_db
from app.db.manager import DatabaseManager
from app.schemas.db_config import (
    DbConfigResponse,
    DbConfigUpdate,
    MySQLConfig,
    TestConnectionResult,
)
from app.services.migration import get_migration_status, is_migration_running, run_migration

router = APIRouter()

# ── 迁移后台线程 ──────────────────────────────────────────

_migration_thread: threading.Thread | None = None


def _run_migration_in_thread():
    """在后台线程中执行迁移。"""
    run_migration()


# ── 端点 ──────────────────────────────────────────────────

@router.get("/settings/db-config", response_model=DbConfigResponse)
def get_db_config():
    """获取当前数据库配置（密码脱敏）。"""
    cfg = load_db_config()
    mysql = cfg.get("mysql", {})
    password = mysql.get("password", "")

    # 密码脱敏：仅显示末两位
    masked = ""
    if password:
        masked = "****" + password[-2:] if len(password) > 2 else "****"

    return DbConfigResponse(
        use_mysql=cfg.get("use_mysql", False),
        mysql=MySQLConfig(
            host=mysql.get("host", "127.0.0.1"),
            port=mysql.get("port", 3306),
            database=mysql.get("database", ""),
            user=mysql.get("user", ""),
            password=masked,
        ),
    )


@router.put("/settings/db-config")
def update_db_config(config: DbConfigUpdate):
    """保存数据库配置并热切换引擎。"""
    mgr = DatabaseManager.get()
    new_cfg = config.model_dump()

    # 如果密码是脱敏格式，保留磁盘上的原密码
    if config.mysql.password.startswith("****"):
        existing = load_db_config()
        new_cfg["mysql"]["password"] = existing.get("mysql", {}).get("password", "")

    # 先保存配置
    save_db_config(new_cfg)

    if config.use_mysql:
        # 验证必填字段
        m = config.mysql
        if not m.host or not m.database or not m.user:
            raise HTTPException(400, "MySQL 配置不完整，请填写主机、数据库名和用户名")

        url = build_mysql_url(new_cfg)

        # 先测试连接
        try:
            test_engine = create_engine(url, pool_pre_ping=True)
            with test_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            test_engine.dispose()
        except Exception as e:
            # 安全：异常信息可能含完整连接串（含密码），仅记录服务端日志，不回传前端
            logger.exception("MySQL 连接测试失败")
            raise HTTPException(400, "MySQL 连接失败，请检查主机、端口、用户名和密码")

        # 热切换引擎
        mgr.initialize(url, db_type="mysql")
    else:
        # 切换回 SQLite
        mgr.initialize(settings.database_url, db_type="sqlite")

    # 重新初始化数据库（确认 schema）
    try:
        init_db()
    except Exception as e:
        raise HTTPException(500, f"数据库初始化失败：{e}")

    return {
        "status": "ok",
        "db_type": mgr.db_type,
        "message": f"已切换到 {'MySQL' if mgr.is_mysql else 'SQLite'}",
    }


@router.post("/settings/db-config/test", response_model=TestConnectionResult)
def test_connection(config: DbConfigUpdate):
    """测试 MySQL 连接（不保存配置）。"""
    m = config.mysql
    if not m.host or not m.database or not m.user:
        return TestConnectionResult(
            success=False,
            message="请填写主机、数据库名和用户名",
        )

    # 如果是脱敏密码，从磁盘读取原密码
    cfg = config.model_dump()
    if m.password.startswith("****"):
        existing = load_db_config()
        cfg["mysql"]["password"] = existing.get("mysql", {}).get("password", "")

    url = build_mysql_url(cfg)

    try:
        test_engine = create_engine(url, pool_pre_ping=True)
        with test_engine.connect() as conn:
            result = conn.execute(text("SELECT VERSION()"))
            version = result.scalar()
        test_engine.dispose()
        return TestConnectionResult(
            success=True,
            message="连接成功",
            server_version=str(version),
        )
    except Exception as e:
        # 安全：异常信息可能含完整连接串（含密码），仅记录服务端日志，不回传前端
        logger.exception("MySQL 连接测试失败")
        return TestConnectionResult(
            success=False,
            message="连接失败，请检查主机、端口、用户名和密码配置",
        )


@router.post("/settings/db-config/migrate")
def trigger_migration():
    """触发 SQLite → MySQL 数据迁移（后台执行）。"""
    global _migration_thread

    mgr = DatabaseManager.get()
    if not mgr.is_mysql:
        raise HTTPException(400, "当前数据库不是 MySQL，请先切换到 MySQL 再执行迁移")

    if is_migration_running():
        raise HTTPException(409, "迁移任务正在进行中，请稍后再试")

    # 在后台线程中执行迁移
    _migration_thread = threading.Thread(target=_run_migration_in_thread, daemon=True)
    _migration_thread.start()

    return {"status": "ok", "message": "迁移任务已启动"}


@router.get("/settings/db-config/migration-status")
def migration_status():
    """轮询迁移进度。"""
    return get_migration_status()
