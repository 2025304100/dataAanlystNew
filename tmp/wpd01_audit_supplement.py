"""WPD-01 补充审计：factor_runtime_state + async_tasks 列 + factor_model_runs。"""
import json
import sys
from pathlib import Path

import pymysql

REPORT_PATH = Path(__file__).resolve().parent / "wpd01_audit_supplement.json"


def main() -> int:
    conn = pymysql.connect(
        host="127.0.0.1", port=3306, user="root", password="root",
        database="gpfx", charset="utf8mb4",
    )
    cur = conn.cursor()
    report: dict = {}

    # 1. factor_runtime_state（运行模式与激活模型）
    try:
        cur.execute(
            "SELECT id, weight_mode, active_model_run_id, updated_by, "
            "fallback_reason, version, updated_at FROM factor_runtime_state LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            report["factor_runtime_state"] = {
                "id": row[0],
                "weight_mode": row[1],
                "active_model_run_id": row[2],
                "updated_by": row[3],
                "fallback_reason": row[4],
                "version": row[5],
                "updated_at": str(row[6]) if row[6] else None,
            }
    except Exception as exc:
        report["factor_runtime_state"] = f"ERROR: {exc}"

    # 2. async_tasks 列清单（检查 error_code 是否存在）
    try:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema='gpfx' AND table_name='async_tasks' "
            "ORDER BY ordinal_position"
        )
        report["async_tasks_columns"] = [
            {"column": r[0], "type": r[1], "nullable": r[2], "default": r[3]}
            for r in cur.fetchall()
        ]
    except Exception as exc:
        report["async_tasks_columns"] = f"ERROR: {exc}"

    # 3. factor_model_runs（唯一 Ridge 运行状态）
    try:
        cur.execute(
            "SELECT id, model_type, status, sample_count, symbol_count, "
            "trade_date_count, rejection_reason, data_cutoff_at, "
            "train_start_date, train_end_date, created_at, activated_at "
            "FROM factor_model_runs ORDER BY created_at DESC LIMIT 5"
        )
        report["factor_model_runs"] = [
            {
                "id": r[0], "model_type": r[1], "status": r[2],
                "sample_count": r[3], "symbol_count": r[4],
                "trade_date_count": r[5], "rejection_reason": r[6],
                "data_cutoff_at": str(r[7]) if r[7] else None,
                "train_start_date": str(r[8]) if r[8] else None,
                "train_end_date": str(r[9]) if r[9] else None,
                "created_at": str(r[10]) if r[10] else None,
                "activated_at": str(r[11]) if r[11] else None,
            }
            for r in cur.fetchall()
        ]
    except Exception as exc:
        report["factor_model_runs"] = f"ERROR: {exc}"

    # 4. factor_model_audit_logs（激活/回退审计）
    try:
        cur.execute(
            "SELECT id, action, model_run_id, previous_mode, new_mode, "
            "actor, note, created_at FROM factor_model_audit_logs "
            "ORDER BY created_at DESC LIMIT 10"
        )
        report["factor_model_audit_logs"] = [
            {
                "id": r[0], "action": r[1], "model_run_id": r[2],
                "previous_mode": r[3], "new_mode": r[4],
                "actor": r[5], "note": r[6],
                "created_at": str(r[7]) if r[7] else None,
            }
            for r in cur.fetchall()
        ]
    except Exception as exc:
        report["factor_model_audit_logs"] = f"ERROR: {exc}"

    # 5. 最近因子流水线任务（不用 error_code，只用现有列）
    try:
        cur.execute(
            "SELECT id, task_type, status, percent, message, "
            "created_at, updated_at FROM async_tasks "
            "WHERE task_type LIKE '%factor%' "
            "ORDER BY created_at DESC LIMIT 10"
        )
        report["recent_factor_tasks"] = [
            {
                "id": r[0], "task_type": r[1], "status": r[2],
                "percent": r[3], "message": r[4],
                "created_at": str(r[5]) if r[5] else None,
                "updated_at": str(r[6]) if r[6] else None,
            }
            for r in cur.fetchall()
        ]
    except Exception as exc:
        report["recent_factor_tasks"] = f"ERROR: {exc}"

    conn.close()
    REPORT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"[WPD-01] 补充报告已写入: {REPORT_PATH}")
    if "factor_runtime_state" in report and isinstance(report["factor_runtime_state"], dict):
        rts = report["factor_runtime_state"]
        print(f"[WPD-01] weight_mode={rts.get('weight_mode')}, active_model_run_id={rts.get('active_model_run_id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
