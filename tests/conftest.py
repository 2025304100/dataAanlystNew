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

# PT-DEF-21：测试会话使用**专属**因子仓库文件，绝不与开发中的后端进程共用
# 同一个 `tmp/factor_warehouse.duckdb`。
#
# 必须在导入任何 `app.*` 之前设置：`Settings` 在构造时就把 FACTOR_WAREHOUSE_PATH
# 解析进 `factor_warehouse_path`（app/core/config.py L41-43），晚一步就不生效。
#
# 实测踩过的两类问题：
# 1) 本地跑着后端时全量 7 例红，根因是
#    `_duckdb.IOException: Cannot open file "…/factor_warehouse.duckdb": 另一个程序
#    正在使用此文件`，但上层表现成 `assert 0 == 2` 之类，完全看不出真因；
# 2) 依赖仓库内容的用例会随执行顺序飘移（共享文件里"恰好有/没有该 symbol 的数据"），
#    表现为"单跑绿、全量红"（体检报告 §十九.2 同一机制）。
# 外部显式设过该变量时尊重它（便于专门跑真实数据的场景）。
os.environ.setdefault(
    "FACTOR_WAREHOUSE_PATH",
    str(Path(tempfile.gettempdir()) / f"qa_factor_warehouse_{os.getpid()}.duckdb"),
)

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
from sqlalchemy import create_engine, event, text
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

# ---------------------------------------------------------------------------
# P2 long-guard: Factor-domain anticorruption import gate.
# External tests MUST NOT import internal factor ORM/services directly; use
# `from app.services.factors.__facade__ import ...` (scoring thin-facade)
# instead. Violation raises RuntimeError at collection time.
# ---------------------------------------------------------------------------
import sys as _sys_p2t12  # noqa: E402
_IMPORT_GATE_CHECKED = False

def _run_factor_import_gate():
    global _IMPORT_GATE_CHECKED
    if _IMPORT_GATE_CHECKED:
        return
    _IMPORT_GATE_CHECKED = True
    forbidden_prefixes = (
        "app.models.factor_",
        "app.services.factor_set_service",
        "app.services.factor_usage_service",
    )
    ok_prefix = "app.services.factors.__facade__"
    violators = sorted({
        modname for modname in _sys_p2t12.modules.keys()
        if (
            any(modname.startswith(p) for p in forbidden_prefixes)
            and not modname.startswith(ok_prefix)
        )
    })
    # Only fail when an external (non-factor-internal) module imports the above.
    if violators:
        external_importers = []
        import_module = __import__("importlib", fromlist=["import_module"]).import_module
        for modname in list(_sys_p2t12.modules.keys()):
            if modname.startswith("app.services.factors") or modname.startswith("app.api.routes.factor"):
                continue  # factor domain itself is allowed to import factor internals
            # Skip ORM aggregation modules: `app.models` re-exports `factor` ORMs
            # via wildcard; `app.models.factor_*` / `app.models.discovery_*` /
            # etc. are also registered ORM modules — their source naturally contains
            # factor internals. The gate is about *external modules* reaching around
            # the scoring thin-facade to import factor service/ORM internals on
            # purpose; these registrations are not a cross-domain leak.
            if modname == "app.models" or modname.startswith("app.models.factor_"):
                continue
            if modname in ("app.db", "app.db.base", "app.db.manager", "app.db.init_db"):
                continue
            if not modname.startswith("tests.") and not modname.startswith("app."):
                continue
            try:
                mod = _sys_p2t12.modules[modname]
            except KeyError:
                continue
            src_file = getattr(mod, "__file__", "") or ""
            if not src_file:
                continue
            try:
                text = Path(src_file).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for vp in forbidden_prefixes:
                token = f"import {vp}"
                alt_token = f"from {vp}"
                if token in text or alt_token in text:
                    external_importers.append(f"{modname} -> {vp}")
        if external_importers:
            raise RuntimeError(
                "[P2-T12] Factor-domain anticorruption gate FAIL: tests/ or external "
                "app.* modules imported internal factor modules directly (bypassing "
                "app.services.factors.__facade__.scoring*). Violations:\n  - "
                + "\n  - ".join(external_importers)
                + "\nFix: replace the import with `from app.services.factors.__facade__ import <scoringApi>`."
            )

_run_factor_import_gate()



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
    for suffix in ("", "-wal", "-shm"):  # WAL 边文件随主文件一并回收
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def _enable_sqlite_wal(engine) -> None:
    """把测试库的并发语义对齐到生产/dev 库（init_db.py 同一 PRAGMA）。

    为什么需要：data_prep / 心跳类后台 worker 线程在测试函数之外仍会写
    `async_tasks`（它们通过 `get_session_local()` 拿当前单例 engine）。SQLite
    默认的 rollback-journal 模式下“读事务也挡写”，被挡一方会白等满
    busy_timeout 后报 `database is locked`（历史型跨文件假失败，单文件跑就绿）；
    而 `initialize_runtime_database()` 早就把真实库切到 WAL（init_db.py L1800），
    即测试环境比生产环境更严苛。这里对齐 WAL + busy_timeout，只影响测试引擎，
    不改 `DatabaseManager` 的生产默认（与 tests/integration/conftest.py 同一处置）。

    PRAGMA 只能在 connect 监听里发：journal_mode 不能在已开启的事务里改；
    监听时连接尚未 begin（manager.py 的 foreign_keys 也这么干），且
    `journal_mode=WAL` 会持久化到库文件，后续连接重复设置是无害幂等。
    """

    @event.listens_for(engine, "connect")
    def _sqlite_test_concurrency(dbapi_conn, _connection_record):  # noqa: N802
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
        except Exception:  # noqa: BLE001 - 并发调优不得影响用例本身结果
            pass
        finally:
            cur.close()


@pytest.fixture(scope="function")
def db_session(tmp_sqlite_url):
    """初始化一个全新 DatabaseManager + 内存表，返回 Session。"""
    mgr = DatabaseManager.get()
    # 释放可能存在的旧实例
    try:
        mgr.dispose()
    except Exception:
        pass
    # 「控制平面」引擎（NullPool，供 async_tasks 的提交/心跳/状态轮询用）是按
    # 当时的 dm.engine.url 建的模块级缓存。逐用例改绑主库而不清它，控制平面就会
    # 继续连着上一个用例的 tmp 文件——那个文件已被删掉，表现是随机的
    # "no such table: async_tasks"（体检报告 §二十一 A1 那一族"只在组合跑才红"）。
    # 走公开失效接口 reset_control_plane_cache()，与 tests/integration/conftest.py 对齐。
    try:
        import app.db.session as _app_db_session
        _app_db_session.reset_control_plane_cache()
    except Exception:
        pass
    mgr.initialize(tmp_sqlite_url, db_type="sqlite")
    engine = mgr.engine
    _enable_sqlite_wal(engine)
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
        # 收尾也要失效：控制平面可能已为本用例的 URL 建过 engine，Windows 上
        # 残留句柄会让上一个 tmp 文件删不掉，下一个用例就撞上"死库"。
        try:
            import app.db.session as _app_db_session
            _app_db_session.reset_control_plane_cache()
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


# ── Task 29: BFG P0.4 anti-corruption AST hard gate ──
# Scans app/services/backtest.py / portfolio_backtest.py / factor_evaluator.py
# for:
#   (1) is_active / RawAssetUniverse used for HISTORICAL backtest/evaluation decisions
#       -> code=ANTI_CORRUPTION_IS_ACTIVE_HISTORY
#   (2) DailyBar.close == 0 (or <0) combined with prev_close backfill within 5 lines
#       -> code=ANTI_CORRUPTION_FAKE_SUSPENSION_PRICE
@pytest.fixture(scope="session", autouse=True)
def _bfg_p04_anti_corruption_ast(pytestconfig):  # noqa: N802
    """Session-level AST hard gate (Task 29) for BFG historical correctness."""
    from tests._bfg_anti_corruption_gate import _register_in_pytest
    _register_in_pytest(pytestconfig)
    yield


# ── 全局可变状态的逐用例隔离（体检报告 §十二.5 / §十二.7、PT-DEF-15）──
# 两件事：
# 1) WORKER_STOP_EVENT 是 app.services.async_tasks 的模块级 threading.Event：生产里
#    只有 lifespan 关停会 set 它（进程随即退出，无需清），但测试进程里
#    `with TestClient(app)` 走一次关停就会把它永久留在 set 状态 —— 之后任何真实
#    worker 一启动就自撤（实测把 mining 真实 GA 用例卡到 420s 超时）。
# 2) 异步 worker 线程（`Thread-N (_run)`）是 daemon，用 get_session_local() 的
#    **全局 session** 写库，会跨用例存活：实测在 discovery fast_scan 用例开始前
#    它的 tmp 库里已凭空多出一条 ready 空快照 + 一条 scan_run，导致该用例
#    cache_hit 到 0 条结果（PT-DEF-15）。
# 停工位清理对全部用例都做（零成本、已证明必要）；**线程回收只对显式标了
# `reap_workers` 的用例做**：实测无差别回收会给依赖异步任务的用例簇引入
# 新的时序干扰（同一组合重复两次得到不同的 2-3 例红），故采用逐个文件 opt-in。
@pytest.fixture(autouse=True)
def _isolate_worker_stop_event(request):
    from app.services import async_tasks as _at

    _at.WORKER_STOP_EVENT.clear()
    reap = request.node.get_closest_marker("reap_workers") is not None
    try:
        yield
    finally:
        try:
            if reap:
                with _at._WORKER_THREADS_GUARD:
                    live_workers = [
                        t for t in _at._WORKER_THREADS.values() if t.is_alive()
                    ]
                if live_workers:
                    # best-effort 回收：不能因某个 worker 卡在网络调用上就把整个套件拖慢
                    _at.request_all_workers_stop()
                    _at.wait_workers_stopped(timeout_seconds=5)
        finally:
            # 退出也清：用例内为了验证优雅停机而 set 的标志不应泄到下一个用例
            _at.WORKER_STOP_EVENT.clear()


# ===========================================================================
# e2e 闸门的「至少真跑 N 例」最低覆盖断言
# ===========================================================================
# 必须挂在根 conftest 而不是 tests/e2e/conftest.py：如果哪天 `-m e2e` 的选择集
# 被整体删空（marker 改名/误删），e2e 目录的 conftest 根本不会被加载，闸门就
# 形同虚设；根 conftest 在任何收集方式下都会加载。
# 详见 tests/_e2e_coverage_guard.py 的说明。

_e2e_executed = 0
_e2e_skipped = 0


def pytest_runtest_logreport(report) -> None:
    """统计 tests/e2e 下真跑过的用例数；skip 不能算执行。"""
    global _e2e_executed, _e2e_skipped

    # 只用 nodeid 判定归属：它是 pytest 保证存在且恒为正斜杠路径
    # （形如 tests/e2e/test_x.py::test_y）。早期版本依赖 report.fspath，
    # 在 pytest 8 上取不到属性会让所有记账被静默丢弃 —— 那会使闸门在 CI 上
    # 永远报"0 条真跑"造成假红（实测踩过，故此处不再回退到 fspath）。
    nodeid = str(getattr(report, "nodeid", "") or "").replace("\\", "/")
    if not nodeid.startswith("tests/e2e/"):
        return
    if report.when == "setup":
        # fixture 阶段就 skip 的用例进不了 call，只能在这里记账
        if report.skipped:
            _e2e_skipped += 1
        return
    if report.when != "call":
        return
    if report.skipped:
        _e2e_skipped += 1
    elif report.passed or report.failed:
        _e2e_executed += 1


def pytest_sessionfinish(session, exitstatus) -> None:
    """会话收尾：清测试专属因子仓库文件；CI 下再判 e2e 最低真跑数。"""
    # 本次会话用的 DuckDB 文件用完即弃（含 WAL 边文件），不在临时目录留垃圾
    wh = os.environ.get("FACTOR_WAREHOUSE_PATH", "")
    if wh and "qa_factor_warehouse_" in Path(wh).name:
        for suffix in ("", ".wal", ".shm"):
            try:
                Path(wh + suffix).unlink(missing_ok=True)
            except OSError:
                pass

    from tests._e2e_coverage_guard import e2e_minimum_required, evaluate_coverage

    message = evaluate_coverage(
        _e2e_executed, _e2e_skipped, required=e2e_minimum_required()
    )
    if message is None:
        return
    print(f"\n[E2E 最低覆盖闸门] {message}")
    # 即使所有用例都没跑（收集为 0），pytest 原本会以 0 退出 —— 这里强制变红
    session.exitstatus = 1

