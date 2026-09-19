#!/usr/bin/env python3
"""TD2 一次性清理：仓库根历史遗留的 integration_* 临时库（用户已批准）。

背景：tests/integration/conftest.py 用 tempfile.mkstemp(prefix="integration_",
dir=ROOT) 建每用例独立 SQLite tmp 库；旧版 teardown 的 unlink 在 Windows 上因
句柄未释放而静默失败，历史会话累计遗留 110 个 .sqlite3（377.5MB）+
208 个 -wal/-shm（58.4MB）。2026-09-18 经用户确认一次性清理。

安全边界：
- 仅删除仓库根目录下三种精确模式：integration_*.sqlite3 / -wal / -shm
- 删除前后计数核对；输出审计摘要；幂等（重跑无文件则 no-op）
运行：CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=1000 {PY} _td2_cleanup_litter.py
"""
from __future__ import annotations

import glob
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")

PATTERNS = ("integration_*.sqlite3", "integration_*.sqlite3-wal", "integration_*.sqlite3-shm")


def main() -> None:
    files: list[str] = []
    for pat in PATTERNS:
        files.extend(glob.glob(str(ROOT / pat)))
    total_bytes = sum(os.path.getsize(f) for f in files)
    print(f"[cleanup] 待删除 {len(files)} 个文件，共 {total_bytes / 1024 / 1024:.1f} MB")
    if not files:
        print("[cleanup] 无遗留文件，no-op")
        return

    errors: list[str] = []
    for f in files:
        try:
            os.unlink(f)
        except OSError as e:
            errors.append(f"{os.path.basename(f)}: {e}")

    leftover = [f for pat in PATTERNS for f in glob.glob(str(ROOT / pat))]
    print(f"[cleanup] 已删除 {len(files) - len(errors)} 个，剩余 {len(leftover)} 个")
    if errors:
        print("[cleanup] 失败清单：")
        for e in errors[:20]:
            print("   ", e)
    if leftover or errors:
        sys.exit(1)
    print("[cleanup] OK — 仓库根 integration_* 遗留已清零")


if __name__ == "__main__":
    main()
