"""WPD-01 临时审计脚本：MySQL schema fingerprint + alembic_version 检查 + 8 因子快照。

只读检查，不修改任何数据。运行后产出 JSON 报告到 tmp/wpd01_audit_report.json。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pymysql

REPORT_PATH = Path(__file__).resolve().parent / "wpd01_audit_report.json"


def main() -> int:
    try:
        conn = pymysql.connect(
            host="127.0.0.1",
            port=3306,
            user="root",
            password="root",
            database="gpfx",
            charset="utf8mb4",
        )
    except Exception as exc:
        report = {
            "mysql_connectable": False,
            "error": str(exc),
            "repo_head_revision": "wps_0801_001_universe_incremental_index",
            "repo_documented_head": "wps_0023_020_api_deprecation_logs (底稿记录，已被覆盖)",
        }
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[WPD-01] MySQL 不可连接: {exc}")
        return 1

    cur = conn.cursor()
    report: dict = {
        "mysql_connectable": True,
        "repo_head_revision": "wps_0801_001_universe_incremental_index",
        "repo_documented_head_in_plan": "wps_0023_020_api_deprecation_logs (底稿记录，已被 2026-08-01 18:30 新增的 universe 增量索引覆盖)",
        "audit_date": "2026-08-01",
    }

    # 1. MySQL 版本
    cur.execute("SELECT VERSION()")
    report["mysql_version"] = cur.fetchone()[0]

    # 2. alembic_version 表是否存在
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='gpfx' AND table_name='alembic_version'"
    )
    alembic_exists = cur.fetchone()[0]
    report["alembic_version_table_exists"] = bool(alembic_exists)
    if alembic_exists:
        cur.execute("SELECT version_num FROM alembic_version")
        rows = cur.fetchall()
        report["alembic_version_rows"] = [r[0] for r in rows]
    else:
        report["alembic_version_rows"] = []

    # 3. schema fingerprint - 表清单
    cur.execute(
        "SELECT table_name, table_type, engine, table_collation, table_rows, "
        "table_comment FROM information_schema.tables "
        "WHERE table_schema='gpfx' ORDER BY table_name"
    )
    tables = []
    for row in cur.fetchall():
        tables.append(
            {
                "table_name": row[0],
                "table_type": row[1],
                "engine": row[2],
                "collation": row[3],
                "rows": row[4],
                "comment": row[5],
            }
        )
    report["tables"] = tables
    report["table_count"] = len(tables)

    # 4. schema fingerprint - 列清单（关键因子相关表）
    factor_tables = [
        "factors",
        "factor_versions",
        "factor_model_runs",
        "factor_weight_snapshots",
        "factor_runtime_state",
        "factor_system_config",
        "custom_indicators",
        "scores",
        "async_tasks",
        "universe_symbols",
        "universe_daily_bars",
        "symbols",
        "daily_bars",
        "stock_valuations",
        "stock_financial_reports",
        "capital_flows",
        "stock_hot_rank_snapshots",
        "lhb_institution_trades",
        "tail_accumulation_snapshots",
    ]
    columns_fingerprint: dict = {}
    for tbl in factor_tables:
        cur.execute(
            "SELECT column_name, data_type, column_type, is_nullable, "
            "column_default, column_key, extra, column_comment "
            "FROM information_schema.columns "
            "WHERE table_schema='gpfx' AND table_name=%s "
            "ORDER BY ordinal_position",
            (tbl,),
        )
        cols = []
        for row in cur.fetchall():
            cols.append(
                {
                    "column": row[0],
                    "data_type": row[1],
                    "column_type": row[2],
                    "nullable": row[3],
                    "default": row[4],
                    "key": row[5],
                    "extra": row[6],
                    "comment": row[7],
                }
            )
        columns_fingerprint[tbl] = cols
    report["columns_fingerprint"] = columns_fingerprint

    # 5. schema fingerprint - 索引清单（因子相关表）
    indexes_fingerprint: dict = {}
    for tbl in factor_tables:
        cur.execute(
            "SELECT index_name, column_name, non_unique, seq_in_index, "
            "index_type "
            "FROM information_schema.statistics "
            "WHERE table_schema='gpfx' AND table_name=%s "
            "ORDER BY index_name, seq_in_index",
            (tbl,),
        )
        idxs = []
        for row in cur.fetchall():
            idxs.append(
                {
                    "index_name": row[0],
                    "column": row[1],
                    "non_unique": row[2],
                    "seq": row[3],
                    "type": row[4],
                }
            )
        indexes_fingerprint[tbl] = idxs
    report["indexes_fingerprint"] = indexes_fingerprint

    # 6. 8 系统因子快照（列对齐 ORM: id, code, name, category, direction, status,
    #    source_type, frequency, default_missing_policy, is_active, description, formula_expr）
    try:
        cur.execute(
            "SELECT id, code, name, category, direction, status, source_type, "
            "frequency, default_missing_policy, is_active, description, formula_expr "
            "FROM factors ORDER BY id"
        )
        factors = []
        for row in cur.fetchall():
            factors.append(
                {
                    "id": row[0],
                    "code": row[1],
                    "name": row[2],
                    "category": row[3],
                    "direction": row[4],
                    "status": row[5],
                    "source_type": row[6],
                    "frequency": row[7],
                    "default_missing_policy": row[8],
                    "is_active": row[9],
                    "description": row[10],
                    "formula_expr": row[11],
                }
            )
        report["factors_snapshot"] = factors
        report["factors_count"] = len(factors)
    except Exception as exc:
        report["factors_snapshot"] = f"ERROR: {exc}"

    # 7. 因子版本快照
    try:
        cur.execute(
            "SELECT id, factor_id, version, formula_expr, params_json, "
            "source_mapping_json, direction, is_latest, change_note, created_at "
            "FROM factor_versions ORDER BY factor_id, version"
        )
        versions = []
        for row in cur.fetchall():
            versions.append(
                {
                    "id": row[0],
                    "factor_id": row[1],
                    "version": row[2],
                    "formula_expr": row[3],
                    "params_json": row[4],
                    "source_mapping_json": row[5],
                    "direction": row[6],
                    "is_latest": row[7],
                    "change_note": row[8],
                    "created_at": str(row[9]) if row[9] else None,
                }
            )
        report["factor_versions_snapshot"] = versions
    except Exception as exc:
        report["factor_versions_snapshot"] = f"ERROR: {exc}"

    # 8. 运行模式与功能开关
    try:
        cur.execute(
            "SELECT feature_enabled, weight_mode, active_model_run_id, "
            "warehouse_path FROM factor_system_config LIMIT 1"
        )
        cfg_row = cur.fetchone()
        if cfg_row:
            report["factor_system_config"] = {
                "feature_enabled": cfg_row[0],
                "weight_mode": cfg_row[1],
                "active_model_run_id": cfg_row[2],
                "warehouse_path": cfg_row[3],
            }
    except Exception as exc:
        report["factor_system_config"] = f"ERROR: {exc}"

    # 9. 关键表行数（用于对账基线）
    count_tables = [
        "universe_symbols",
        "universe_daily_bars",
        "symbols",
        "daily_bars",
        "stock_valuations",
        "stock_financial_reports",
        "capital_flows",
        "stock_hot_rank_snapshots",
        "lhb_institution_trades",
        "tail_accumulation_snapshots",
        "scores",
        "factors",
        "factor_versions",
        "factor_model_runs",
        "factor_weight_snapshots",
        "custom_indicators",
        "async_tasks",
    ]
    counts: dict = {}
    for tbl in count_tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM `{tbl}`")
            counts[tbl] = cur.fetchone()[0]
        except Exception as exc:
            counts[tbl] = f"ERROR: {exc}"
    report["table_row_counts"] = counts

    # 10b. 最近因子流水线任务状态（验证文档中 11.3%/19% failed）
    try:
        cur.execute(
            "SELECT id, task_type, status, percent, message, error_code, "
            "created_at, updated_at FROM async_tasks "
            "WHERE task_type IN ('factor_pipeline','factor_calculation') "
            "ORDER BY created_at DESC LIMIT 10"
        )
        report["recent_factor_tasks"] = [
            {
                "id": r[0],
                "task_type": r[1],
                "status": r[2],
                "percent": r[3],
                "message": r[4],
                "error_code": r[5],
                "created_at": str(r[6]) if r[6] else None,
                "updated_at": str(r[7]) if r[7] else None,
            }
            for r in cur.fetchall()
        ]
    except Exception as exc:
        report["recent_factor_tasks"] = f"ERROR: {exc}"

    # 10. 最新交易日横截面（验证文档中的 150/218 残缺数据）
    try:
        cur.execute(
            "SELECT trade_date, COUNT(*) AS cnt FROM universe_daily_bars "
            "GROUP BY trade_date ORDER BY trade_date DESC LIMIT 10"
        )
        report["universe_daily_recent_dates"] = [
            {"trade_date": str(r[0]), "count": r[1]} for r in cur.fetchall()
        ]
    except Exception as exc:
        report["universe_daily_recent_dates"] = f"ERROR: {exc}"

    conn.close()

    REPORT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"[WPD-01] 审计报告已写入: {REPORT_PATH}")
    print(f"[WPD-01] MySQL 版本: {report['mysql_version']}")
    print(f"[WPD-01] 表数量: {report['table_count']}")
    print(f"[WPD-01] alembic_version 表存在: {report['alembic_version_table_exists']}")
    print(f"[WPD-01] 因子数量: {report['factors_count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
