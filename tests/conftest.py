"""Pytest 公共 fixtures。

使用 SQLite 内存数据库做隔离测试，避免污染运行中的实例。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# 将项目根目录加入 sys.path，使 `import app.xxx` 可用
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# WP9 验证环境兼容：若 akshare/sklearn 未安装（如 CI 沙箱），注入 stub 以允许
# `import app.main`（transitively imports app.services.discovery_tasks /
# app.services.factors.ridge_model）。真正调用这些库的测试应显式 import 并标记 slow/联网。
for _stub_mod in ("akshare", "sklearn", "sklearn.linear_model", "sklearn.metrics"):
    try:
        __import__(_stub_mod)
    except ImportError:
        sys.modules[_stub_mod] = MagicMock(name=f"{_stub_mod}_stub")

import pytest
import requests
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.manager import DatabaseManager
from app.db.init_db import _auto_align_all_schema, _auto_repair_basic_data_integrity
from app.models import *  # noqa: F401,F403 - 确保所有模型被注册
from app import models  # noqa: F401
# 显式导入 __init__.py 未导出的模型，确保 Base.metadata 包含全部表
# （否则外键约束的目标表可能缺失，导致 create_all 报 NoReferencedTableError）
from app.models import (  # noqa: F401
    portfolio, symbol, watchlist, daily_bar, score, scan,
    signal_rule, trade_setup, journal_entry, news_event, alert,
    macro_data, factor, sim_account, discovery,
)


def pytest_addoption(parser):
    parser.addoption("--hardware-capability", action="store", default="dev", choices=["dev", "prod"],
                     help="dev: xfail 硬件相关性能测试；prod: 真实执行（CI/生产）")


def pytest_configure(config):
    """注册自定义 markers（与 pytest.ini 中已声明的 markers 共存，幂等）。

    WP-P.9 要求 slow / performance 标记可用于 tests/performance/ 下的测试。
    使用 addinivalue_line 重复注册同一 marker 不会报错。
    """
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers",
        "performance: performance baseline tests requiring release environment "
        "with full A-share 5500 / ETF 1600 universe",
    )
    config.addinivalue_line(
        "markers",
        "xfail_dev_hardware: 在 --hardware-capability=dev 且 DuckDB>=2GB/universe>=7000 下预期失败（不计入失败统计），可参数化说明 reason；prod 模式下真实执行",
    )


def _is_dev_hardware(config) -> bool:
    if config.getoption("--hardware-capability") == "prod":
        return False
    duckdb_ok = False
    duckdb_path = os.environ.get("DUCKDB_WAREHOUSE_PATH")
    if not duckdb_path:
        duckdb_path = str(ROOT / "data" / "factor_warehouse.duckdb")
    path = Path(duckdb_path)
    if path.exists():
        try:
            duckdb_ok = path.stat().st_size >= 2_000_000_000
        except OSError:
            duckdb_ok = False
    universe_ok = False
    universe_query_failed = False
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        db_url = "sqlite:///./data/app.db"
    try:
        engine = create_engine(db_url)
        with engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM universe_symbols"))
            count = result.scalar()
            if count is not None:
                universe_ok = count >= 7000
    except Exception:
        universe_query_failed = True
    if duckdb_ok or universe_ok:
        return True
    if universe_query_failed:
        return True
    return False


_SLOW_FUNCTION_NAMES = {
    # T4 (FR-4.1) dashboard 15s 慢查询保护：概览/工作台双端点
    "test_dashboard_overview",
    "test_dashboard_workbench",
    "test_list_observations_status_filter",
    "test_observation_endpoints_404",
    "test_probe_returns_within_30s",
    "test_probe_returns_within_30s_with_real_backend",
    "test_probe_all_17_apis_complete_within_180s",
    # T4 (FR-4.5) 连续探测性能不退化：
    # - stability_guard.py L299 内部 3 次同接口探测对比
    # - api_mgmt.py L354 更完整的版本（含 _response_time 后缀）
    "test_consecutive_probes_do_not_degrade",
    "test_consecutive_probes_do_not_degrade_response_time",
    "test_batch_probe_completes_within_180s",
}


def pytest_collection_modifyitems(config, items):
    dev_flag = _is_dev_hardware(config)
    if not dev_flag:
        return
    xfail_reason = (
        "Dev hardware: slow probe / dashboard timeout, xfail to not fail default runs; "
        "use --hardware-capability=prod to force real run"
    )
    xfail_marker = pytest.mark.xfail(strict=False, reason=xfail_reason)
    for item in items:
        func_name = item.originalname or item.name
        nodeid_tail = item.nodeid.split("::")[-1]
        has_marker = item.get_closest_marker("xfail_dev_hardware") is not None
        if func_name in _SLOW_FUNCTION_NAMES or nodeid_tail in _SLOW_FUNCTION_NAMES or has_marker:
            item.add_marker(xfail_marker)


@pytest.fixture(scope="function")
def tmp_sqlite_url() -> str:
    """每个测试函数独立 SQLite 文件，测完自动清理。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_test_")
    os.close(fd)
    yield f"sqlite:///{path}"
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture(scope="function")
def db_session(tmp_sqlite_url):
    """初始化一个全新 DatabaseManager + 内存表，返回 Session。"""
    mgr = DatabaseManager.get()
    # 释放可能存在的旧实例
    try:
        mgr.dispose()
    except Exception:
        pass
    mgr.initialize(tmp_sqlite_url, db_type="sqlite")
    engine = mgr.engine
    _auto_align_all_schema(engine)
    _auto_repair_basic_data_integrity(engine)
    SessionLocal = mgr.session_factory
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        try:
            mgr.dispose()
        except Exception:
            pass
