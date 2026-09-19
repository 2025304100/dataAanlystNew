# -*- coding: utf-8 -*-
"""TD-FE-RED-2 步骤 1：把「断言 i18n key 字面量」的测试改为断言 t(key) 译文。

**定性依据**（见 docs/前端既有红定性报告.md）：
- 组件侧一律 `t("factorModelShadow")`（符合「文案走 t()」规范）；
- zh-CN 语言包中这些 key **均有译文**（如 factorModelShadow → 影子运行）；
- 测试却断言 `getByText("factorModelShadow")` —— 该写法**只有在 t() 查不到 key、
  回退返回 key 本身时**才成立，即**写测试时语言包尚未补齐**（8-05 提交补齐后即红）。
→ 结论：**测试过时**，处置 = 断言改为 `t("key")`（动态取值，译文再改也不会红）。

安全规则：
- **只替换「字符串确实是 i18n key」的情形**：该字符串必须在 zh-CN 语言包中作为 key 存在，
  且形如 camelCase（无空格/连字符/点）；
- 覆盖 getByText / queryByText / getAllByText / findByText / queryAllByText / findAllByText；
- 若测试文件未 import t，则自动补 import（沿用该文件既有的 i18n 导入写法）。

用法：
    python .workbuddy/mining/_fix_c_key_assertions.py            # 干跑
    python .workbuddy/mining/_fix_c_key_assertions.py --apply
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
FE = ROOT / "frontend"
ZH = FE / "src/i18n/zh-CN.ts"

CALLS = ("getByText", "queryByText", "getAllByText", "findByText",
         "queryAllByText", "findAllByText", "getByRole")
CALL_RE = re.compile(
    r"\b(" + "|".join(CALLS) + r")\(\s*\"([A-Za-z][A-Za-z0-9]*)\"\s*\)"
)
KEY_SHAPE = re.compile(r"^[a-z][A-Za-z0-9]*$")


def zh_keys() -> set[str]:
    """从 zh-CN.ts 提取 key（宽松匹配缩进变体：任意空白 + 标识符 + 冒号 + 引号）。"""
    txt = ZH.read_text(encoding="utf-8")
    return set(re.findall(r"^\s+([a-z][A-Za-z0-9]*):\s*[\"`']", txt, re.MULTILINE))


def main() -> int:
    apply = "--apply" in sys.argv
    keys = zh_keys()
    print(f"zh-CN key 数（源码提取）: {len(keys)}")

    items = json.loads((ROOT / ".workbuddy/mining/td_fe_red2_items.json").read_text("utf-8"))
    files = sorted({b["head"].split(" > ")[0].replace("FAIL  ", "").strip()
                    for b in items})
    files = [f for f in files if f.endswith((".tsx", ".ts"))]
    print(f"失败文件 {len(files)} 个\n")

    total_hits, changed_files = 0, []
    for rel in files:
        p = FE / rel
        text = p.read_text(encoding="utf-8")
        hits: list[str] = []

        def repl(m: re.Match[str]) -> str:
            call, arg = m.group(1), m.group(2)
            if not KEY_SHAPE.match(arg) or arg not in keys:
                return m.group(0)          # 不是 i18n key（如数据 id）→ 不动
            hits.append(f"{call}(\"{arg}\")")
            return f'{call}(t("{arg}"))'

        new = CALL_RE.sub(repl, text)
        if not hits:
            continue
        total_hits += len(hits)
        if "from" in new and re.search(r"import\s*\{[^}]*\bt\b[^}]*\}\s*from", new):
            pass                            # 已有 t
        else:
            # 补 import：沿用既有 i18n 路径写法（默认 ../../i18n 由调用方核对）
            depth = rel.count("/")
            rel_import = "../" * (depth - 1) + "i18n"
            new = f'import {{ t }} from "{rel_import}";\n' + new
            hits.append(f"+import t from {rel_import}")
        changed_files.append((rel, hits))
        if apply:
            p.write_text(new, encoding="utf-8")

    print(f"命中断言 {total_hits} 处，涉及 {len(changed_files)} 个文件\n")
    for rel, hits in changed_files:
        print(f"  {rel}  ({len(hits)} 处)")
        for h in hits[:6]:
            print(f"      {h}")
        if len(hits) > 6:
            print(f"      ... 另 {len(hits) - 6} 处")
    print("\n" + ("已应用" if apply else "干跑未修改（--apply 生效）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
