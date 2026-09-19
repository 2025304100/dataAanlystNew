# -*- coding: utf-8 -*-
"""重写 collections 两个用例的断言：key → 组件实际渲染的中文兜底文案。

定性依据：组件 `FactorModelPage.tsx` 使用
`localizedLabel(key, fallbackZh)`：
```js
function localizedLabel(key, fallback) {
  const value = t(key);
  return value === key ? fallback : value;   // t 返回 key（缺翻译/mock）→ 用中文兜底
}
```
测试又 mock 了 `t: (k) => k` → `t(key) === key` → 组件渲染 **fallback 中文**。
而测试断言的是 key 字面量 → 必然找不到。**属测试过时**，断言应改为中文兜底。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / "frontend/src/components/factors/__tests__/FactorModelPage.collections.test.tsx"
text = p.read_text(encoding="utf-8")

# key → 组件 localizedLabel 的中文兜底
PAIRS = [
    ('"factor_code"', '"因子代码"'),
    ('"factor_name"', '"因子名称"'),
    ('"version_label"', '"版本"'),
    ('"role"', '"角色"'),
    ('"weight_constraint"', '"权重约束"'),
    ('"missing_policy"', '"缺失策略"'),
]

total = 0
for old, new in PAIRS:
    n = text.count(old)
    if n:
        text = text.replace(old, new)
        total += n
        print(f"  {old} -> {new}  x{n}")

# 在用例处补一句说明（首个表头断言前）
marker = 'expect(within(frozenTbl).getByText("因子代码")).toBeInTheDocument();'
if marker in text:
    text = text.replace(
        marker,
        "// 组件用 localizedLabel(key, 中文兜底)：mock 的 t 返回 key 时渲染中文兜底，故断言中文\n"
        "    " + marker,
        1,
    )
    print("  已补说明注释（TR-2.1 表头处）")

p.write_text(text, encoding="utf-8")
print(f"\n共替换 {total} 处 key 断言")
