# -*- coding: utf-8 -*-
"""T39：把 Settings.tsx 的 `<nav class="settings-sidebar">…</nav>` 替换为 <SettingsNav/>。

纯机械替换（不改 JSX 语义）：保留原有缩进；并用断言确认替换前后
「settings-nav-item」出现次数从 16 次变为 0（组件内渲染）。
"""
import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / "frontend/src/components/Settings.tsx"
text = p.read_text(encoding="utf-8")

before_nav_items = text.count("settings-nav-item")
before_lines = len(text.splitlines())

start = text.find('<nav className="settings-sidebar"')
assert start > 0, "未找到 nav 起点"
end = text.find("</nav>", start)
assert end > start, "未找到 nav 终点"
end += len("</nav>")

# 该 nav 所在行的缩进（8 空格）
line_start = text.rfind("\n", 0, start) + 1
indent = text[line_start:start]

replacement = f"<SettingsNav active={{activeSection}} onSelect={{setActiveSection}} />"
new = text[:line_start] + indent + replacement + text[end:]

# 补 import（插在 i18n import 之后，保持 import 区整洁）
anchor = 'import { t, DOT } from "../i18n";'
assert anchor in new
new = new.replace(
    anchor,
    anchor + '\nimport SettingsNav from "./settings/SettingsNav";',
    1,
)

after_nav_items = new.count("settings-nav-item")
p.write_text(new, encoding="utf-8")

print(f"行数 {before_lines} -> {len(new.splitlines())}（减少 {before_lines - len(new.splitlines())}）")
print(f"settings-nav-item 出现次数 {before_nav_items} -> {after_nav_items}（应为 0）")
print(f"indent 保留: {indent!r}")
print("import 已插入:", 'import SettingsNav from "./settings/SettingsNav";' in new)
