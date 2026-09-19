# -*- coding: utf-8 -*-
"""提取 FactorModelPage 失败用例的「页面实际可见文本」，用于逐项定性。

从 verbose 输出里定位每个失败用例，抽取其打印 DOM 中的文本节点
（去 class/aria/svg 噪音），与断言期望的 key 做对照。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
raw = (ROOT / ".workbuddy/mining/c_detail.txt").read_bytes()
txt = re.sub(r"\x1b\[[0-9;]*m", "", raw.decode("utf-8-sig", errors="replace"))

# 失败块：FAIL 路径 > 用例名，随后是错误与 DOM
blocks = re.split(r"\n(?=\s*FAIL\s+src/)", txt)
target = [b for b in blocks if "FactorModelPage.test.tsx" in b]
print(f"FactorModelPage 失败块 {len(target)} 个\n")

NOISE = re.compile(r'class="[^"]*"|aria-[a-z]+="[^"]*"|data-[a-z-]+="[^"]*"|'
                   r'd="[^"]*"|viewBox="[^"]*"|fill="[^"]*"|role="[^"]*"|'
                   r'width="[^"]*"|height="[^"]*"|focusable="[^"]*"')


def visible_texts(seg: str, limit: int = 40) -> list[str]:
    """抽取 DOM 里的文本节点（> 与 < 之间的内容）。"""
    body = seg
    m = re.search(r"<body", seg)
    if m:
        body = seg[m.start():]
    body = NOISE.sub("", body)
    texts = re.findall(r">\s*([^<>{}]{2,60}?)\s*<", body)
    out, seen = [], set()
    for s in texts:
        s = s.strip()
        if not s or s in seen:
            continue
        if re.fullmatch(r"[\s/\\.,;:!?·—\-|()\[\]{}\d%]+", s):
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


for i, b in enumerate(target, 1):
    head = re.search(r"FAIL\s+([^\n]+)", b)
    name = head.group(1).strip() if head else "?"
    err = ""
    em = re.search(r"(TestingLibraryElementError|AssertionError|Error)[^\n]*", b)
    if em:
        err = em.group(0)[:120]
    print(f"[{i}] {name[:110]}")
    print(f"     err: {err}")
    print(f"     页面文本: {visible_texts(b)[:18]}")
    print()
