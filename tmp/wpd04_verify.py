"""WPD-04 验证脚本：因子数据 readiness 评估。

连接实际环境，输出：
  1. 8 因子的 readiness 表格（available/degraded/blocked + 证据）
  2. source_table_stats（验证 估值 6、财报 336、资金流 0、尾盘 0）
  3. 完整交易日证据（来自 WPD-02）
  4. summary 汇总

DuckDB / MySQL 不可用时优雅降级，不报错退出。

运行方式：
    python tmp/wpd04_verify.py
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

REPORT_PATH = Path(__file__).resolve().parent / "wpd04_verify_report.json"


def _init_database():
    """初始化数据库连接，返回 (session, engine) 或 (None, None)。"""
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
            print("[WPD-04] pymysql 未安装，无法连接 MySQL")
            return None, None
        url = build_mysql_url(cfg)
        print(
            f"[WPD-04] 连接 MySQL: "
            f"{cfg['mysql']['host']}:{cfg['mysql']['port']}/{cfg['mysql']['database']}"
        )
        mgr.initialize(url, db_type="mysql")
    else:
        print(f"[WPD-04] 使用 SQLite: {settings.database_url}")
        mgr.initialize(settings.database_url, db_type="sqlite")

    init_db()
    return mgr.session_factory(), mgr.engine


def _print_factor_table(factors):
    """输出 8 因子的 readiness 表格。"""
    print("\n" + "=" * 100)
    print("[1] 8 因子 readiness 评估结果")
    print("=" * 100)
    header = (
        f"{'因子代码':<32} {'层级':<14} {'状态':<10} {'覆盖率':>8} "
        f"{'标的数':>6} {'动作':<14} 阻断原因"
    )
    print(header)
    print("-" * 100)
    for f in factors:
        reasons = ", ".join(f["blocking_reasons"]) if f["blocking_reasons"] else "-"
        print(
            f"{f['factor_code']:<32} {f['layer']:<14} {f['status']:<10} "
            f"{f['coverage']:>8.4f} {f['eligible_symbols']:>6} "
            f"{f['recommended_action']:<14} {reasons}"
        )


def _print_source_table_stats(stats):
    """输出源表统计（验证阻断证据数字）。"""
    print("\n" + "=" * 70)
    print("[2] 源表统计（阻断证据）")
    print("=" * 70)
    print(f"{'表名':<30} {'行数':>10} {'最近日期':<15} 日期列")
    print("-" * 70)
    for table, info in sorted(stats.items()):
        rows = info.get("rows", 0)
        latest = info.get("latest_date") or "-"
        date_col = info.get("date_column", "-")
        marker = ""
        if rows == 0:
            marker = " ← EMPTY (阻断)"
        elif rows < 500:
            marker = " ← 不足"
        print(f"{table:<30} {rows:>10} {str(latest):<15} {date_col}{marker}")

    print("\n关键阻断证据：")
    valuation_rows = stats.get("raw_valuation_snapshots", {}).get("rows", 0)
    financial_rows = stats.get("raw_financial_reports", {}).get("rows", 0)
    fund_flow_rows = stats.get("raw_fund_flows", {}).get("rows", 0)
    tail_rows = stats.get("raw_tail_proxy", {}).get("rows", 0)
    print(f"  估值表 (raw_valuation_snapshots): {valuation_rows} 条")
    print(f"  财报表 (raw_financial_reports):   {financial_rows} 条")
    print(f"  资金流表 (raw_fund_flows):        {fund_flow_rows} 条")
    print(f"  尾盘代理表 (raw_tail_proxy):      {tail_rows} 条")


def _print_complete_trade_day(ctd_evidence):
    """输出完整交易日证据（来自 WPD-02）。"""
    print("\n" + "=" * 70)
    print("[3] 完整交易日证据（WPD-02）")
    print("=" * 70)
    print(f"  selected_trade_date:  {ctd_evidence.get('selected_trade_date')}")
    print(f"  observed_symbols:     {ctd_evidence.get('observed_symbols')}")
    print(f"  expected_symbols:     {ctd_evidence.get('expected_symbols')}")
    print(f"  completeness_ratio:   {ctd_evidence.get('completeness_ratio')}")
    print(f"  fallback_reason:      {ctd_evidence.get('fallback_reason')}")
    print(f"  median_baseline:      {ctd_evidence.get('median_baseline')}")


def _print_summary(summary):
    """输出 summary 汇总。"""
    print("\n" + "=" * 50)
    print("[4] readiness 汇总")
    print("=" * 50)
    print(f"  available: {summary.get('available', 0)}")
    print(f"  degraded:  {summary.get('degraded', 0)}")
    print(f"  blocked:   {summary.get('blocked', 0)}")


def main() -> int:
    try:
        from app.services.factors.readiness import get_factor_readiness_report
    except ImportError as exc:
        print(f"[WPD-04] 无法导入依赖模块: {exc}")
        return 2

    session, engine = _init_database()
    if session is None:
        report = {"error": "无法初始化数据库连接"}
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("[WPD-04] 无法初始化数据库连接，退出。")
        return 1

    try:
        report_obj = get_factor_readiness_report(session)
    except Exception as exc:
        report = {"error": f"readiness 评估失败: {type(exc).__name__}: {exc}"}
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[WPD-04] readiness 评估失败: {type(exc).__name__}: {exc}")
        return 1
    finally:
        session.close()
        if engine is not None:
            engine.dispose()

    payload = report_obj.to_dict()

    # 输出报告
    print(f"\n[WPD-04] 因子数据 readiness 评估报告")
    print(f"  generated_at:       {payload['generated_at']}")
    print(f"  warehouse_available: {payload['warehouse_available']}")
    print(f"  warehouse_path:     {payload['warehouse_path']}")

    _print_factor_table(payload["factors"])
    _print_source_table_stats(payload["source_table_stats"])
    _print_complete_trade_day(payload["complete_trade_day_evidence"])
    _print_summary(payload["summary"])

    # 写入 JSON 报告
    REPORT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n报告已写入: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
