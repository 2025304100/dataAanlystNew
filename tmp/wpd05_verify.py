"""WPD-05 验证脚本：错误协议与乱码修复审计。

连接实际 MySQL，输出：
  1. 最近 10 条 failed 任务的 error_code（从 errors_json[0] 提取）/ message（检查乱码）
  2. DB 连接 charset 审计（connect_args.charset + SET NAMES utf8mb4）
  3. errors_json 中文存储审计（ensure_ascii=False，不含 \\uXXXX 转义）
  4. summary 汇总

MySQL 不可用时优雅降级，不报错退出。

运行方式：
    python tmp/wpd05_verify.py
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

REPORT_PATH = Path(__file__).resolve().parent / "wpd05_verify_report.json"

REPLACEMENT_CHAR = "\ufffd"


def _init_database():
    """初始化数据库连接，返回 (session, engine) 或 (None, None)。"""
    from app.core.config import build_mysql_url, load_db_config
    from app.db.manager import DatabaseManager

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
            print("[WPD-05] pymysql 未安装，无法连接 MySQL")
            return None, None
        url = build_mysql_url(cfg)
        print(
            f"[WPD-05] 连接 MySQL: "
            f"{cfg['mysql']['host']}:{cfg['mysql']['port']}/{cfg['mysql']['database']}"
        )
        mgr.initialize(url, db_type="mysql")
    else:
        print("[WPD-05] 未配置 MySQL，使用 SQLite 本地文件")
        db_path = ROOT / "data" / "local.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.initialize(f"sqlite:///{db_path}", db_type="sqlite")

    return mgr.get_session(), mgr.engine


def _is_likely_mojibake(text: str) -> bool:
    """简单启发式：检测文本是否疑似乱码。"""
    if not text:
        return False
    if REPLACEMENT_CHAR in text:
        return True
    # 连续 4 个以上非可打印控制字符
    consecutive = 0
    for ch in text:
        code = ord(ch)
        if (code < 0x20 and code not in (0x09, 0x0A, 0x0D)) or (0x7F <= code < 0xA0):
            consecutive += 1
            if consecutive >= 4:
                return True
        else:
            consecutive = 0
    return False


def _extract_error_code(errors_json: str | None) -> str | None:
    """从 errors_json[0].error_code 提取顶层 error_code。"""
    if not errors_json:
        return None
    try:
        errors = json.loads(errors_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if errors and isinstance(errors[0], dict):
        return errors[0].get("error_code")
    return None


def _audit_failed_tasks(db_session):
    """查询最近 10 条 failed 任务，输出 error_code 和 message 乱码检测。"""
    from sqlalchemy import text

    print("\n" + "=" * 80)
    print("[WPD-05] 1. 最近 10 条 failed 任务 error_code / message 审计")
    print("=" * 80)

    try:
        rows = db_session.execute(
            text(
                """
                SELECT id, task_type, status, stage, message, errors_json
                FROM async_tasks
                WHERE status = 'failed'
                ORDER BY created_at DESC
                LIMIT 10
                """
            )
        ).fetchall()
    except Exception as exc:
        print(f"[WPD-05] 查询 failed 任务失败: {exc}")
        return []

    if not rows:
        print("[WPD-05] 无 failed 任务记录")
        return []

    results = []
    for row in rows:
        task_id = row[0]
        task_type = row[1]
        message = row[4] or ""
        errors_json = row[5]
        error_code = _extract_error_code(errors_json)
        is_mojibake = _is_likely_mojibake(message)
        has_escaped_unicode = "\\u" in (errors_json or "") if errors_json else False

        entry = {
            "task_id": task_id,
            "task_type": task_type,
            "error_code": error_code,
            "message_preview": message[:80] if message else "",
            "message_is_mojibake": is_mojibake,
            "errors_json_has_escaped_unicode": has_escaped_unicode,
        }
        results.append(entry)

        print(f"\n  task_id: {task_id}")
        print(f"  task_type: {task_type}")
        print(f"  error_code: {error_code}")
        print(f"  message (前80字): {entry['message_preview']}")
        print(f"  message 疑似乱码: {is_mojibake}")
        print(f"  errors_json 含 \\uXXXX 转义: {has_escaped_unicode}")

    return results


def _audit_db_charset(engine):
    """审计 DB 连接 charset。"""
    from sqlalchemy import text

    print("\n" + "=" * 80)
    print("[WPD-05] 2. DB 连接 charset 审计")
    print("=" * 80)

    audit = {
        "connect_args_charset": None,
        "set_names_utf8mb4": None,
        "mysql_charset_variable": None,
    }

    # 检查 engine 的 connect_args
    try:
        # 检查 DatabaseManager 是否配置了 charset
        from app.db.manager import DatabaseManager

        mgr = DatabaseManager.get()
        audit["db_type"] = mgr.db_type
        if mgr.is_mysql:
            # 尝试实际查询 MySQL charset
            with engine.connect() as conn:
                result = conn.execute(text("SHOW VARIABLES LIKE 'character_set_connection'"))
                row = result.fetchone()
                if row:
                    audit["mysql_charset_variable"] = row[1]
                    print(f"  MySQL character_set_connection: {row[1]}")
                result2 = conn.execute(text("SHOW VARIABLES LIKE 'character_set_client'"))
                row2 = result2.fetchone()
                if row2:
                    audit["mysql_client_charset"] = row2[1]
                    print(f"  MySQL character_set_client: {row2[1]}")
        else:
            print(f"  DB 类型: {mgr.db_type} (SQLite，charset 不适用)")
    except Exception as exc:
        print(f"  [WPD-05] charset 查询失败: {exc}")
        audit["error"] = str(exc)

    # 验证 connect_args 配置（从源码审计）
    try:
        import inspect

        from app.db.manager import DatabaseManager

        source = inspect.getsource(DatabaseManager.initialize)
        audit["connect_args_has_charset"] = '"charset"' in source or "'charset'" in source
        audit["set_names_utf8mb4"] = "SET NAMES utf8mb4" in source
        print(f"  manager.py connect_args 含 charset: {audit['connect_args_has_charset']}")
        print(f"  manager.py SET NAMES utf8mb4: {audit['set_names_utf8mb4']}")
    except Exception as exc:
        print(f"  [WPD-05] 源码审计失败: {exc}")

    return audit


def _audit_errors_json_encoding(db_session):
    """审计 errors_json 中文存储是否使用 ensure_ascii=False。"""
    print("\n" + "=" * 80)
    print("[WPD-05] 3. errors_json 中文存储审计（ensure_ascii=False）")
    print("=" * 80)

    audit = {"total_checked": 0, "has_escaped_unicode": 0, "has_chinese_raw": 0}

    try:
        from sqlalchemy import text as sql_text

        rows = db_session.execute(
            sql_text(
                """
                SELECT errors_json FROM async_tasks
                WHERE errors_json IS NOT NULL AND errors_json != ''
                ORDER BY created_at DESC
                LIMIT 20
                """
            )
        ).fetchall()
    except Exception as exc:
        print(f"[WPD-05] 查询 errors_json 失败: {exc}")
        return audit

    for row in rows:
        errors_json = row[0]
        if not errors_json:
            continue
        audit["total_checked"] += 1
        if "\\u" in errors_json:
            audit["has_escaped_unicode"] += 1
        # 检查是否含原始中文字符（而非转义）
        try:
            data = json.loads(errors_json)
            raw = json.dumps(data, ensure_ascii=False)
            if any("\u4e00" <= ch <= "\u9fff" for ch in raw):
                audit["has_chinese_raw"] += 1
        except (json.JSONDecodeError, TypeError):
            pass

    print(f"  检查的 errors_json 数: {audit['total_checked']}")
    print(f"  含 \\uXXXX 转义的: {audit['has_escaped_unicode']}")
    print(f"  含原始中文字符的: {audit['has_chinese_raw']}")

    if audit["total_checked"] > 0 and audit["has_escaped_unicode"] == 0:
        print("  ✓ 所有 errors_json 均使用 ensure_ascii=False 存储（无 \\uXXXX 转义）")
    elif audit["has_escaped_unicode"] > 0:
        print(f"  ⚠ {audit['has_escaped_unicode']} 条 errors_json 含 \\uXXXX 转义（可能是历史数据）")

    return audit


def _audit_async_task_read_schema():
    """审计 AsyncTaskRead schema 是否含 error_code 字段。"""
    print("\n" + "=" * 80)
    print("[WPD-05] 4. AsyncTaskRead schema 审计")
    print("=" * 80)

    audit = {}
    try:
        from app.schemas.async_task import AsyncTaskRead

        fields = AsyncTaskRead.model_fields
        audit["has_error_code_field"] = "error_code" in fields
        audit["error_code_default"] = (
            str(fields["error_code"].default) if "error_code" in fields else None
        )
        print(f"  AsyncTaskRead 含 error_code 字段: {audit['has_error_code_field']}")
        print(f"  error_code 默认值: {audit['error_code_default']}")
    except Exception as exc:
        print(f"  审计失败: {exc}")
        audit["error"] = str(exc)

    # 审计 _task_to_dict 提取逻辑
    try:
        from app.services.async_tasks import _task_to_dict
        import inspect

        source = inspect.getsource(_task_to_dict)
        audit["task_to_dict_extracts_error_code"] = "error_code" in source and "top_error_code" in source
        print(f"  _task_to_dict 提取 error_code: {audit['task_to_dict_extracts_error_code']}")
    except Exception as exc:
        print(f"  _task_to_dict 审计失败: {exc}")

    return audit


def main():
    print("=" * 80)
    print("WPD-05 错误协议与乱码修复 — 验证脚本")
    print("=" * 80)

    db_session, engine = _init_database()
    if db_session is None:
        print("[WPD-05] 无法初始化数据库连接，退出")
        return

    report = {}
    try:
        report["failed_tasks"] = _audit_failed_tasks(db_session)
        report["db_charset"] = _audit_db_charset(engine)
        report["errors_json_encoding"] = _audit_errors_json_encoding(db_session)
        report["async_task_read_schema"] = _audit_async_task_read_schema()

        # Summary
        print("\n" + "=" * 80)
        print("[WPD-05] Summary")
        print("=" * 80)

        failed_tasks = report["failed_tasks"]
        if failed_tasks:
            with_error_code = sum(1 for t in failed_tasks if t["error_code"])
            with_mojibake = sum(1 for t in failed_tasks if t["message_is_mojibake"])
            with_escaped = sum(1 for t in failed_tasks if t["errors_json_has_escaped_unicode"])
            print(f"  failed 任务数: {len(failed_tasks)}")
            print(f"  含 error_code 的: {with_error_code}/{len(failed_tasks)}")
            print(f"  message 疑似乱码: {with_mojibake}/{len(failed_tasks)}")
            print(f"  errors_json 含 \\uXXXX 转义: {with_escaped}/{len(failed_tasks)}")
            report["summary"] = {
                "total_failed": len(failed_tasks),
                "with_error_code": with_error_code,
                "with_mojibake": with_mojibake,
                "with_escaped_unicode": with_escaped,
            }
        else:
            print("  无 failed 任务记录")
            report["summary"] = {"total_failed": 0}

        charset_ok = report["db_charset"].get("connect_args_has_charset", False) and report[
            "db_charset"
        ].get("set_names_utf8mb4", False)
        print(f"  DB charset 配置完整: {charset_ok}")
        print(f"  AsyncTaskRead 含 error_code: {report['async_task_read_schema'].get('has_error_code_field', False)}")
    finally:
        db_session.close()

    # 写入报告
    REPORT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"\n[WPD-05] 报告已写入: {REPORT_PATH}")


if __name__ == "__main__":
    main()
