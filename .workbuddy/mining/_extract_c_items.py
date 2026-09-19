# -*- coding: utf-8 -*-
"""提取 22 项 C 类失败的结构化清单（文件 / 用例 / 错误类型 / 错误首行）。"""
import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
raw = (ROOT / ".workbuddy/mining/c_detail.txt").read_bytes()
txt = re.sub(r"\x1b\[[0-9;]*m", "", raw.decode("utf-8-sig", errors="replace"))
lines = txt.splitlines()

# FAIL 块：以 'FAIL  <path>' 开始（verbose 下路径会折行，下一行是 'xxx.tsx > 用例名'）
blocks = []
i = 0
while i < len(lines):
    if re.match(r"^\s*FAIL\s+src/", lines[i]):
        head = lines[i]
        # 拼接折行的路径/用例名
        j = i + 1
        while j < len(lines) and lines[j].strip() and not re.match(
                r"^(TestingLibraryElementError|AssertionError|Error|expect|Assertion|"
                r"Ignored nodes|Unable|Expected|Received|\s*[\{\[<])", lines[j]):
            head += lines[j].strip()
            j += 1
        # 错误首行
        err = ""
        k = j
        while k < min(j + 12, len(lines)):
            if re.match(r"^(TestingLibraryElementError|AssertionError|Error|TypeError)", lines[k]):
                err = lines[k].strip()
                if k + 1 < len(lines) and lines[k + 1].strip():
                    err += " " + lines[k + 1].strip()
                break
            k += 1
        blocks.append({"head": head.strip(), "err": err.strip()[:220]})
        i = j
    else:
        i += 1

print(f"提取到 {len(blocks)} 项失败")
for idx, b in enumerate(blocks, 1):
    print(f"\n[{idx}] {b['head'][:160]}")
    print(f"    {b['err'][:200]}")

(ROOT / ".workbuddy/mining/td_fe_red2_items.json").write_text(
    json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n已落盘 td_fe_red2_items.json（{len(blocks)} 项）")
