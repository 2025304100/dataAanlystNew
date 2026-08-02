"""WPD-03 验证脚本：DuckDB 仓库锁治理和僵尸任务恢复。

连接实际环境，输出：
  1. 当前流水线锁状态（diagnose_pipeline_lock_state）
  2. 僵尸任务恢复结果（recover_stale_pipeline_tasks）
  3. DuckDB 文件大小和主要表行数
  4. 完整锁诊断报告（run_warehouse_lock_diagnostics）

DuckDB / psutil / MySQL 不可用时优雅降级，不报错退出。

运行方式：
    python tmp/wpd03_verify.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 与 conftest.py 一致：若 sklearn/akshare 未安装，注入 stub 以允许
# import pipeline_task（transitively imports ridge_model）。
for _stub_mod in ("akshare", "sklearn", "sklearn.linear_model", "sklearn.metrics"):
    try:
        __import__(_stub_mod)
    except ImportError:
        sys.modules[_stub_mod] = MagicMock(name=f"{_stub_mod}_stub")

REPORT_PATH = Path(__file__).resolve().parent / "wpd03_verify_report.json"


def _try_imports():
    """尝试导入依赖，返回 (config, db_manager_ok) 元组。"""
    try:
        from app.core.config import load_db_config, build_mysql_url
        from app.db.manager import DatabaseManager
        from app.db.base import Base
        from app.db.init_db import init_db
        return ("ok", True)
    except ImportError as exc:
        print(f"[WPD-03] 无法导入核心依赖: {exc}")
        return (None, False)


def _init_database():
    """初始化数据库连接，返回 SessionLocal 或 None。"""
    from app.core.config import load_db_config, build_mysql_url, settings
    from app.db.manager import DatabaseManager
    from app.db.base import Base
    from app.db.init_db import init_db

    cfg = load_db_config()
    mgr = DatabaseManager.get()
    try:
        mgr.dispose()
    except Exception:
        pass

    if cfg.get("use_mysql") and cfg.get("mysql", {}).get("host"):
        try:
            import pymysql  # noqa: F401
        except ImportError:
            print("[WPD-03] pymysql 未安装，无法连接 MySQL")
            return None
        url = build_mysql_url(cfg)
        print(f"[WPD-03] 连接 MySQL: {cfg['mysql']['host']}:{cfg['mysql']['port']}/{cfg['mysql']['database']}")
        mgr.initialize(url, db_type="mysql")
    else:
        print(f"[WPD-03] 使用 SQLite: {settings.database_url}")
        mgr.initialize(settings.database_url, db_type="sqlite")

    init_db()
    return mgr.session_factory


def _get_warehouse_path(db):
    """从 factor_system_config 获取 warehouse_path。"""
    from app.services.factors.config import get_factor_system_config
    config = get_factor_system_config(db)
    return Path(config.warehouse_path)


def _diagnose_lock_state():
    """调用 diagnose_pipeline_lock_state 输出锁状态。"""
    from app.services.factors.pipeline_task import diagnose_pipeline_lock_state
    print("\n" + "=" * 60)
    print("[1] 流水线锁状态诊断 (diagnose_pipeline_lock_state)")
    print("=" * 60)
    try:
        state = diagnose_pipeline_lock_state()
        print(json.dumps(state, indent=2, ensure_ascii=False, default=str))
        return state
    except Exception as exc:
        print(f"[WPD-03] diagnose_pipeline_lock_state 失败: {exc}")
        return {"error": str(exc)}


def _recover_stale(db):
    """调用 recover_stale_pipeline_tasks 输出恢复结果。"""
    from app.services.factors.pipeline_task import recover_stale_pipeline_tasks
    print("\n" + "=" * 60)
    print("[2] 僵尸任务恢复 (recover_stale_pipeline_tasks)")
    print("=" * 60)
    try:
        records = recover_stale_pipeline_tasks(db)
        if records:
            print(f"恢复 {len(records)} 个僵尸任务:")
            for r in records:
                print(f"  - {r['task_id']}: {r['previous_status']} → {r['new_status']} ({r['stale_reason']})")
        else:
            print("无僵尸任务需要恢复")
        return records
    except Exception as exc:
        print(f"[WPD-03] recover_stale_pipeline_tasks 失败: {exc}")
        return [{"error": str(exc)}]


def _duckdb_stats(warehouse_path):
    """输出 DuckDB 文件大小和主要表行数。"""
    print("\n" + "=" * 60)
    print("[3] DuckDB 仓库统计")
    print("=" * 60)
    stats = {"warehouse_path": str(warehouse_path)}

    # File size
    try:
        stat = warehouse_path.stat()
        size_mb = stat.st_size / (1024 * 1024)
        stats["file_size_bytes"] = stat.st_size
        stats["file_size_mb"] = round(size_mb, 2)
        stats["file_size_gb"] = round(size_mb / 1024, 2)
        print(f"  文件路径: {warehouse_path}")
        print(f"  文件大小: {size_mb:.2f} MB ({size_mb / 1024:.2f} GB)")
    except OSError:
        stats["file_error"] = "warehouse file not found"
        print(f"  仓库文件不存在: {warehouse_path}")
        return stats

    # Table row counts
    try:
        import duckdb  # noqa: F401
        from app.services.factors.store import FactorWarehouse
        wh = FactorWarehouse(warehouse_path)
        health = wh.health()
        stats["health"] = health.to_dict()
        print(f"  available: {health.available}")
        print(f"  schema_version: {health.schema_version}")
        print(f"  raw_daily_bars: {health.raw_daily_bars}")
        print(f"  latest_trade_date: {health.latest_trade_date}")

        if health.available:
            tables = [
                "raw_daily_bars", "raw_asset_universe",
                "raw_valuation_snapshots", "raw_financial_reports",
                "raw_fund_flows", "raw_sentiment", "raw_tail_proxy",
                "raw_macro", "factor_values", "factor_targets",
                "ingestion_batches", "warehouse_watermarks",
            ]
            row_counts = {}
            with wh.connection(read_only=True) as conn:
                for table in tables:
                    try:
                        count = conn.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                        row_counts[table] = int(count)
                        print(f"  {table}: {count} rows")
                    except Exception:
                        row_counts[table] = "N/A"
            stats["table_row_counts"] = row_counts
    except ImportError:
        stats["duckdb_error"] = "duckdb not installed"
        print("  [跳过] duckdb 未安装，无法读取表行数")
    except Exception as exc:
        stats["duckdb_error"] = str(exc)
        print(f"  [跳过] DuckDB 读取失败: {exc}")

    return stats


def _full_diagnostics(warehouse_path):
    """调用 run_warehouse_lock_diagnostics 输出完整诊断。"""
    from app.services.factors.warehouse_locks import run_warehouse_lock_diagnostics
    print("\n" + "=" * 60)
    print("[4] 完整锁诊断报告 (run_warehouse_lock_diagnostics)")
    print("=" * 60)
    try:
        report = run_warehouse_lock_diagnostics(warehouse_path)
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return report
    except Exception as exc:
        print(f"[WPD-03] run_warehouse_lock_diagnostics 失败: {exc}")
        return {"error": str(exc)}


def main() -> int:
    print("=" * 60)
    print("WPD-03 验证: DuckDB 锁和残留进程治理")
    print("=" * 60)

    _, ok = _try_imports()
    if not ok:
        print("[WPD-03] 核心依赖导入失败，退出")
        return 2

    SessionLocal = _init_database()
    if SessionLocal is None:
        print("[WPD-03] 数据库初始化失败，退出")
        return 1

    db = SessionLocal()
    report: dict = {}
    try:
        # 获取 warehouse_path
        try:
            warehouse_path = _get_warehouse_path(db)
        except Exception as exc:
            print(f"[WPD-03] 无法获取 warehouse_path: {exc}")
            warehouse_path = None
            report["config_error"] = str(exc)

        # 1. 锁状态诊断
        report["lock_state"] = _diagnose_lock_state()

        # 2. 僵尸任务恢复
        report["recovery"] = _recover_stale(db)

        # 3. DuckDB 统计
        if warehouse_path is not None:
            report["duckdb_stats"] = _duckdb_stats(warehouse_path)
            # 4. 完整诊断
            report["full_diagnostics"] = _full_diagnostics(warehouse_path)

        # psutil 状态
        try:
            import psutil  # noqa: F401
            report["psutil_available"] = True
        except ImportError:
            report["psutil_available"] = False
            print("\n[注] psutil 未安装，PID 诊断使用 ctypes/os.kill 降级方案")

    finally:
        db.close()

    # 写入报告文件
    REPORT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"\n[WPD-03] 报告已写入: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
