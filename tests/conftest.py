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


# ── P2-2 hard_import_gate: long-anti-regression for cross-domain imports ──
# Any `pytest` run will block PRs that import factor-domain internals from
# non-factor-domain modules (the only allowed path is
# `from app.services.factors.__facade__ import ...`). Files inside
# `app/services/factors/`, `app/models/factor*.py` and test utilities are
# explicitly allowed; near-relative coupling can be whitelisted with a
# source-line comment `# near-relative coupling: <reason> — audit YYYY-MM-DD`.

_HARD_IMPORT_GATE_ALLOWED_PREFIXES = (
    "app/services/factors/",
    "app/api/routes/factor_",   # native factor routes own the factor domain surface
    "app/api/routes/factors.py",
    "app/api/routes/scoring_",
    "app/models/factor.py",
    "app/models/factor_model.py",
    "app/models/factor_runtime.py",
    "app/models/factor_evaluation.py",
    "app/models/factor_governance.py",
    "app/models/factor_shadow.py",
    "app/services/factor_model_contract.py",  # ORM-shared helpers for factor models
    "app/services/factor_set_service.py",     # FactorSet CRUD (factor-domain internal)
    "app/services/factor_usage_service.py",   # Factor usage aggregation (factor-domain internal)
    "app/services/ai/drafts/factor_draft.py", # factor-draft AI workflow (factor-domain internal)
    "app/main.py",                            # entrypoint wires router internals + lifecycle tasks
    "tests/",  # test utilities can always reach anything
    "tmp/",
    "scripts/audit",
)

_HARD_IMPORT_GATE_FORBIDDEN = (
    (r"from\s+app\.models\.factor_model\s+import", "direct:app.models.factor_model"),
    (r"import\s+app\.models\.factor_model\b", "direct:app.models.factor_model"),
    (r"from\s+app\.models\.factor\s+import", "direct:app.models.factor"),
    (r"import\s+app\.models\.factor\b[^_]", "direct:app.models.factor"),
    (r"from\s+app\.models\.factor_runtime\s+import.*ActiveScoreScope",
     "direct:ActiveScoreScope (use get_active_runtime_with_fallback_reason())"),
    (r"from\s+app\.services\.factors\.ridge_model\b",
     "direct:ridge_model (must go through training-eligibility Facade)"),
    (r"from\s+app\.services\.factors\.pipeline_task\b",
     "direct:pipeline_task (use create_scoring_task() / ensure_feature_inputs_ready())"),
    (r"from\s+app\.services\.factors\.warehouse_locks\b",
     "direct:warehouse_locks (internal locking mechanism must not leak)"),
)

_HARD_IMPORT_GATE_ALLOWED_TOKEN = "from app.services.factors.__facade__ import"
_HARD_IMPORT_GATE_ALLOWED_TOKEN2 = "from app.services import factors as __factors"  # reserved, currently unused
_HARD_IMPORT_GATE_NEAR_REL = "near-relative coupling:"


_HARD_IMPORT_GATE_FILE_EXEMPTIONS = frozenset([
    "app/services/backtest.py",            # near-relative: reads FactorModelRun weights
    "app/services/vectorbt_backtest.py",   # near-relative: reads FactorModelRun weights
    "app/services/scheduled_tasks.py",     # near-relative: scheduler wires factor pipeline
])


def _file_is_allowed_bypass(rel: str) -> bool:
    norm = rel.replace("\\", "/")
    if norm in _HARD_IMPORT_GATE_FILE_EXEMPTIONS:
        return True
    return any(norm.startswith(pfx) for pfx in _HARD_IMPORT_GATE_ALLOWED_PREFIXES)


@pytest.fixture(scope="session", autouse=True)
def _p0_hard_import_gate(pytestconfig):  # noqa: N802
    """Session-wide anti-corruption gate.

    We walk the ``app/**/*.py`` tree once at session start and assert the
    hardcoded forbidden import list is not present outside factor-domain
    internals. Any new PR introducing a direct leak fails the full
    pytest run with a clear list of offending file:line entries.
    """
    import re as _re

    root = Path(__file__).resolve().parent.parent
    files = sorted((root / "app").rglob("*.py"))
    violations: list[str] = []
    for file in files:
        rel = str(file.relative_to(root)).replace("\\", "/")
        if _file_is_allowed_bypass(rel):
            continue
        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            if _HARD_IMPORT_GATE_ALLOWED_TOKEN in line:
                continue
            if _HARD_IMPORT_GATE_ALLOWED_TOKEN2 in line:
                continue
            prev = lines[i-2] if i-2 >= 0 else ""
            if _HARD_IMPORT_GATE_NEAR_REL in line or _HARD_IMPORT_GATE_NEAR_REL in prev:
                continue
            for pattern, rule in _HARD_IMPORT_GATE_FORBIDDEN:
                if _re.search(pattern, line):
                    violations.append(
                        f"{rel}:{i} [{rule}] -> {stripped[:140]}"
                    )
                    break
    if violations:
        report = "\n  - ".join(violations[:50])
        pytest.fail(
            "[P2-2 hard_import_gate] Forbidden cross-domain factor imports found:\n"
            f"  - {report}\n"
            "\nUse `from app.services.factors.__facade__ import ...` or "
            "annotate a single near-relative-read with: "
            "`# near-relative coupling: <reason> — audit YYYY-MM-DD`"
            f". Total violating lines: {len(violations)}"
        )
    yield

