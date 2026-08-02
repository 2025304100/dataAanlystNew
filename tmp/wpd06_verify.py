"""WPD-06 验证脚本：因子值增量与批次审计。

连接实际环境，输出：
  1. 完整批次审计报告（含原子提交证据和落后诊断）
  2. 最近批次清单（含输入日期、行数、状态、原子提交证据）
  3. factor_values 落后 raw_daily_bars 诊断
  4. source_table_stats（各 raw_* 表统计）
  5. summary 汇总

DuckDB / MySQL 不可用时优雅降级，不报错退出。

运行方式：
    python tmp/wpd06_verify.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 与 conftest.py 一致：若 sklearn/akshare 未安装，注入 stub
for _stub_mod in ("akshare", "sklearn", "sklearn.linear_model", "sklearn.metrics"):
    try:
        __import__(_stub_mod)
    except ImportError:
        sys.modules[_stub_mod] = MagicMock(name=f"{_stub_mod}_stub")

# 允许从 tmp/duckdb_lib 加载 duckdb（沙箱环境降级）
_duckdb_lib = ROOT / "tmp" / "duckdb_lib"
if _duckdb_lib.exists():
    sys.path.insert(0, str(_duckdb_lib))

REPORT_PATH = Path(__file__).resolve().parent / "wpd06_verify_report.json"


def _get_warehouse():
    """从配置获取 FactorWarehouse 实例。"""
    from app.services.factors.config import get_current_factor_system_config
    from app.services.factors.store import FactorWarehouse

    try:
        config = get_current_factor_system_config()
        return FactorWarehouse(config.warehouse_path)
    except Exception as exc:
        print(f"[WPD-06] 无法读取 factor 配置: {exc}")
        return None


def _print_batch_list(warehouse):
    """输出最近批次清单。"""
    from app.services.factors.batch_audit import list_recent_batches

    print("\n" + "=" * 80)
    print("[WPD-06] 2. 最近批次清单（含原子提交证据）")
    print("=" * 80)

    try:
        records = list_recent_batches(warehouse, limit=20)
    except Exception as exc:
        print(f"[WPD-06] 查询批次清单失败: {exc}")
        return []

    if not records:
        print("[WPD-06] ingestion_batches 表为空（尚无批次记录）")
        return []

    from app.services.factors.batch_audit import audit_batch_atomicity

    output = []
    for record in records:
        entry = audit_batch_atomicity(warehouse, batch_id=record.batch_id)
        actual = entry.actual_rows_in_table if entry else None
        match = (
            entry.atomicity_evidence["match"] if entry else None
        )
        scope = record.scope_json or "{}"
        print(
            f"  batch_id={record.batch_id[:16]}... "
            f"source_key={record.source_key} "
            f"status={record.status} "
            f"rows_written={record.rows_written} "
            f"actual_rows={actual} "
            f"match={match} "
            f"started={record.started_at} "
            f"finished={record.finished_at}"
        )
        print(f"    scope={scope}")
        output.append(
            {
                "batch_id": record.batch_id,
                "source_key": record.source_key,
                "status": record.status,
                "started_at": record.started_at.isoformat()
                if record.started_at
                else None,
                "finished_at": record.finished_at.isoformat()
                if record.finished_at
                else None,
                "rows_received": record.rows_received,
                "rows_written": record.rows_written,
                "actual_rows_in_table": actual,
                "atomicity_match": match,
                "scope_json": record.scope_json,
            }
        )
    return output


def _print_lag_diagnostic(warehouse):
    """输出落后诊断。"""
    from app.services.factors.batch_audit import diagnose_factor_lag

    print("\n" + "=" * 80)
    print("[WPD-06] 3. factor_values 落后 raw_daily_bars 诊断")
    print("=" * 80)

    try:
        diag = diagnose_factor_lag(warehouse)
    except Exception as exc:
        print(f"[WPD-06] 落后诊断失败: {exc}")
        return {}

    print(f"  raw_daily_bars 最新日期: {diag.raw_daily_bars_latest_date}")
    print(f"  factor_values 最新日期: {diag.factor_values_latest_date}")
    print(f"  lag_days: {diag.lag_days}")
    print(f"  is_lagging: {diag.is_lagging}")
    print(f"  severity: {diag.severity}")
    print(f"  raw_daily_bars_count: {diag.raw_daily_bars_count}")
    print(f"  factor_values_count: {diag.factor_values_count}")
    print(f"  latest_factor_batch_id: {diag.latest_factor_batch_id}")
    print(f"  latest_factor_batch_status: {diag.latest_factor_batch_status}")
    print(f"  recommended_actions: {diag.recommended_actions}")
    return diag.to_dict()


def _print_source_table_stats(report_dict):
    """输出源表统计。"""
    print("\n" + "=" * 80)
    print("[WPD-06] 4. source_table_stats（各 raw_* 表统计）")
    print("=" * 80)

    stats = report_dict.get("source_table_stats", {})
    if not stats:
        print("[WPD-06] 无源表统计数据")
        return stats

    for table, info in stats.items():
        print(
            f"  {table}: rows={info.get('rows', 0)}, "
            f"latest_date={info.get('latest_date')}"
        )
    return stats


def main():
    print("=" * 80)
    print("WPD-06 因子值增量与批次审计 — 验证脚本")
    print("=" * 80)

    try:
        import duckdb  # noqa: F401
    except ImportError:
        print("[WPD-06] DuckDB 未安装，无法运行验证")
        report = {
            "warehouse_available": False,
            "error": "duckdb_not_installed",
        }
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"\n[WPD-06] 报告已写入: {REPORT_PATH}")
        return

    warehouse = _get_warehouse()
    if warehouse is None:
        report = {
            "warehouse_available": False,
            "error": "config_unavailable",
        }
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"\n[WPD-06] 报告已写入: {REPORT_PATH}")
        return

    from app.services.factors.batch_audit import get_batch_audit_report

    report = {}
    try:
        print("\n" + "=" * 80)
        print("[WPD-06] 1. 完整批次审计报告")
        print("=" * 80)

        audit_report = get_batch_audit_report(warehouse, recent_batch_limit=20)
        report_dict = audit_report.to_dict()

        print(f"  warehouse_available: {report_dict['warehouse_available']}")
        print(f"  warehouse_path: {report_dict['warehouse_path']}")
        print(f"  generated_at: {report_dict['generated_at']}")
        print(
            f"  summary: {report_dict['summary']}"
        )
        report["report"] = report_dict

        # 2. 批次清单
        report["batches"] = _print_batch_list(warehouse)

        # 3. 落后诊断
        report["lag_diagnostic"] = _print_lag_diagnostic(warehouse)

        # 4. 源表统计
        report["source_table_stats"] = _print_source_table_stats(
            report_dict
        )

        # Summary
        print("\n" + "=" * 80)
        print("[WPD-06] 5. Summary")
        print("=" * 80)
        summary = report_dict.get("summary", {})
        print(f"  total_batches: {summary.get('total_batches', 0)}")
        print(f"  committed: {summary.get('committed', 0)}")
        print(f"  failed: {summary.get('failed', 0)}")
        print(f"  running: {summary.get('running', 0)}")
        lag = report.get("lag_diagnostic", {})
        print(f"  lag_severity: {lag.get('severity', 'unknown')}")
        print(f"  lag_days: {lag.get('lag_days', 0)}")
        report["summary"] = {
            **summary,
            "lag_severity": lag.get("severity"),
            "lag_days": lag.get("lag_days", 0),
        }
    except Exception as exc:
        print(f"[WPD-06] 验证过程中出错: {exc}")
        import traceback

        traceback.print_exc()
        report["error"] = str(exc)
    finally:
        pass

    REPORT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"\n[WPD-06] 报告已写入: {REPORT_PATH}")


if __name__ == "__main__":
    main()
