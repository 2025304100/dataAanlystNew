#!/usr/bin/env python3
"""TD4 基线对照：临时把 4 个模型文件的 Double 列声明还原为 Float（简单替换，注释行不动），
用于取证「2 个 backtest_apply 失败用例是既有失败 vs TD4 引入」，然后可恢复 Double 版。

用法：
  python _td4_baseline_check.py revert   # Double 版先备份 .td4_backup/，再写回 Float 版
  python _td4_baseline_check.py restore  # 从 .td4_backup/ 恢复 Double 版
  python _td4_baseline_check.py verify   # 打印当前 4 文件处于哪一版
"""
from __future__ import annotations

import pathlib
import shutil
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
FILES = [
    "app/models/sim_account.py",
    "app/models/portfolio.py",
    "app/models/portfolio_equity_snapshot.py",
    "app/models/backtest.py",
]
BAK = REPO / ".workbuddy" / "mining" / "_td4_backup"


def _to_float(text: str) -> str:
    # 列声明还原（这 4 个文件里没有其它 Double 列，全局替换安全）
    text = text.replace("mapped_column(Double)", "mapped_column(Float)")
    text = text.replace("mapped_column(Double, default=0)", "mapped_column(Float, default=0)")
    # import 行还原（含 Double 的 import 子句）
    text = text.replace("Double, ", "").replace(", Double", "").replace(" Double,", "")
    return text


def revert() -> None:
    BAK.mkdir(parents=True, exist_ok=True)
    for rel in FILES:
        src = REPO / rel
        shutil.copy2(src, BAK / pathlib.Path(rel).name)  # 先备份 Double 版
        text = src.read_text(encoding="utf-8")
        reverted = _to_float(text)
        assert "mapped_column(Double" not in reverted, f"{rel} revert 后仍有 Double 列残留"
        src.write_text(reverted, encoding="utf-8")
        print(f"[revert] {rel} → Float 基线版")


def restore() -> None:
    for rel in FILES:
        bak = BAK / pathlib.Path(rel).name
        assert bak.exists(), f"备份缺失：{bak}"
        shutil.copy2(bak, REPO / rel)
        print(f"[restore] {rel} ← Double 版（TD4 改动）")


def verify() -> None:
    for rel in FILES:
        text = (REPO / rel).read_text(encoding="utf-8")
        print(f"  {rel}: {'Double 版' if 'mapped_column(Double' in text else 'Float 版'}")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "verify"
    {"revert": revert, "restore": restore, "verify": verify}[action]()
