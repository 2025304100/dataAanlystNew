# -*- coding: utf-8 -*-
"""回滚 _fix_c_key_assert2.py 对 FactorModelPage.test.tsx 的改动（精确反向替换）。

原因：该改动使失败数从 12 升到 14（**引入新失败**），违反「无新增失败」护栏。
诊断结论：该测试文件的 i18n mock 为**部分生效**（`t` 被 mock 成返回 key，
但页面另有文本来自未被 mock 的入口），故「统一改断言为真实译文」的策略不成立。

本回滚**只撤销本次改动**（`zhCN.x` → `"x"` 与新增 import），
不影响此前 A 类（api mock 补齐）的修复。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / "frontend/src/components/__tests__/FactorModelPage.test.tsx"

text = p.read_text(encoding="utf-8")
orig = text

# 1) 反向替换断言
text, n1 = re.subn(r"\b(getByText|queryByText|getAllByText|findByText|"
                   r"queryAllByText|findAllByText)\(zhCN\.([A-Za-z0-9]+)\)",
                   r'\1("\2")', text)
# 2) 移除我新增的 import
text, n2 = re.subn(r'^import zhCN from "[^"]*i18n/zh-CN";\n', "", text, flags=re.MULTILINE)

# 3) 断言无残留 zhCN 引用
leftover = len(re.findall(r"\bzhCN\b", text))
p.write_text(text, encoding="utf-8")
print(f"回滚：断言 {n1} 处、import {n2} 处；残留 zhCN 引用 {leftover}")
print("恢复后行数:", len(text.splitlines()), "| 原行数:", len(orig.splitlines()))
