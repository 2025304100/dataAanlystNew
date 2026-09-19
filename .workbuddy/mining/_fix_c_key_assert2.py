# -*- coding: utf-8 -*-
"""TD-FE-RED-2 步骤 2：把「断言 i18n key 字面量」改为「断言真实语言包译文」。

**实测定性**（证据：失败用例的实际 DOM 打印）
- 这些测试 `vi.mock("../../i18n", () => ({ t: (k) => k }))` 的本意是「让 t 返回 key 便于断言」，
  但**该 mock 未生效** —— 实际 DOM 渲染的是真实中文译文（如「步骤 1」「因子库」），
  而非 key 字面量；
- 于是 `getByText("factorModelShadow")` 必然失败（页面上是「影子运行」）。
→ 处置：断言改为取**真实语言包** `zh-CN` 的值：
  `getByText(zhCN.factorModelShadow)` —— 既不依赖 mock 是否生效，译文迭代也不会红。

注意：`zh-CN` 是独立模块，mock 只替换了 `../../i18n` 入口，故 `zh-CN` 取值不受影响。

用法：
    python .workbuddy/mining/_fix_c_key_assert2.py <文件相对路径> [--apply]
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
FE = ROOT / "frontend"
ZH = FE / "src/i18n/zh-CN.ts"

CALLS = ("getByText", "queryByText", "getAllByText", "findByText",
         "queryAllByText", "findAllByText")
CALL_RE = re.compile(r"\b(" + "|".join(CALLS) + r")\(\s*\"([a-z][A-Za-z0-9]*)\"\s*\)")
KEY_SHAPE = re.compile(r"^[a-z][A-Za-z0-9]*$")


def zh_keys() -> set[str]:
    return set(re.findall(r"^\s+([a-z][A-Za-z0-9]*):\s*[\"`']",
                          ZH.read_text(encoding="utf-8"), re.MULTILINE))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("用法: python _fix_c_key_assert2.py <文件> [--apply]")
        return 2
    apply = "--apply" in sys.argv
    rel = args[0]
    p = FE / rel
    keys = zh_keys()
    text = p.read_text(encoding="utf-8")

    hits: list[tuple[str, str]] = []

    def repl(m: re.Match[str]) -> str:
        call, arg = m.group(1), m.group(2)
        if not KEY_SHAPE.match(arg) or arg not in keys:
            return m.group(0)
        hits.append((f'{call}("{arg}")', f"{call}(zhCN.{arg})"))
        return f"{call}(zhCN.{arg})"

    new = CALL_RE.sub(repl, text)
    if hits:
        # 确保 zhCN 已导入（相对路径按目录层级推）
        if not re.search(r'import\s+zhCN\s+from', new):
            depth = rel.count("/")            # src/... 的层级
            rel_import = "../" * (depth - 1) + "i18n/zh-CN"
            new = f'import zhCN from "{rel_import}";\n' + new
            print(f"  补 import: {rel_import}")
        if apply:
            p.write_text(new, encoding="utf-8")

    print(f"{rel}: 命中 {len(hits)} 处")
    for before, after in hits:
        print(f"    {before}  ->  {after}")
    print("\n" + ("已应用" if apply else "干跑未修改（--apply 生效）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
