"""WPD-02 验证脚本：连接实际 MySQL，调用 latest_complete_trade_date 输出证据。

只读检查，不修改任何数据。MySQL 不可用时给出明确错误并优雅退出。

运行方式：
    python tmp/wpd02_verify.py

前置条件：pymysql 已安装（如未安装会提示）。
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPORT_PATH = Path(__file__).resolve().parent / "wpd02_verify_report.json"


def main() -> int:
    try:
        from app.core.config import build_mysql_url, load_db_config
        from app.services.factors.trade_calendar import latest_complete_trade_date
    except ImportError as exc:
        print(f"[WPD-02] 无法导入依赖模块: {exc}")
        return 2

    cfg = load_db_config()
    if not cfg.get("use_mysql"):
        report = {
            "mysql_enabled": False,
            "error": "db_config.json 中 use_mysql=false，跳过 MySQL 验证",
        }
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("[WPD-02] 当前未启用 MySQL（use_mysql=false），跳过验证。")
        return 0

    try:
        import pymysql  # noqa: F401
    except ImportError:
        report = {
            "mysql_enabled": True,
            "error": "pymysql 未安装，请执行 pip install pymysql 后重试",
        }
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("[WPD-02] pymysql 未安装，请执行: pip install pymysql")
        return 1

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = build_mysql_url(cfg)
    try:
        engine = create_engine(url, pool_pre_ping=True)
        SessionLocal = sessionmaker(bind=engine, future=True)
        session = SessionLocal()
    except Exception as exc:
        report = {
            "mysql_enabled": True,
            "error": f"无法创建数据库引擎: {type(exc).__name__}",
        }
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[WPD-02] 无法创建数据库引擎: {type(exc).__name__}")
        return 1

    try:
        evidence = latest_complete_trade_date(session)
    except Exception as exc:
        report = {
            "mysql_enabled": True,
            "error": f"算法执行失败: {type(exc).__name__}: {exc}",
        }
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[WPD-02] 算法执行失败: {type(exc).__name__}: {exc}")
        return 1
    finally:
        session.close()
        engine.dispose()

    payload = asdict(evidence)
    # date 对象转 ISO 字符串以便 JSON 序列化
    payload["selected_trade_date"] = evidence.selected_trade_date.isoformat()
    payload["evaluated_candidate_dates"] = [
        d.isoformat() for d in evidence.evaluated_candidate_dates
    ]

    report = {
        "mysql_enabled": True,
        "evidence": payload,
    }
    REPORT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("[WPD-02] 完整交易日判定结果：")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\n报告已写入: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
