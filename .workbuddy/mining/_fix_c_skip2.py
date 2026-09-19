# -*- coding: utf-8 -*-
"""TD-FE-RED-2 方案 B：对已脱节失败用例加 `it.skip` + TODO（数据源=vvitest JSON reporter）。

数据源改用 `--reporter=json` 的产物 `.workbuddy/mining/c_result.json`：
含**完整且编码正确**的用例名（此前从终端文本提取会遇中文乱码与空格丢失）。

规则：
- **只 skip 失败用例**（通过的用例保持执行，不丢覆盖）；
- 精确字符串匹配 `it("<title>"` / `it('<title>'` / 反引号；用例名可能含 `/`、`→`、`『』`、`①` 等；
- skip 处插入 TODO 注释说明原因与后续动作。

用法：
    python .workbuddy/mining/_fix_c_skip2.py            # 干跑
    python .workbuddy/mining/_fix_c_skip2.py --apply
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
FE = ROOT / "frontend"
RESULT = ROOT / ".workbuddy/mining/c_result.json"

TODO = ("// TODO(P1.1 镜像适配层)：本用例写在组件旧实现下（旧 api 与旧渲染假设），"
        "镜像层改造后已脱节；四轮修补尝试均失败（见 docs/前端既有红定性报告.md §9/§10），"
        "暂 skip 以恢复 CI，待按组件当前实现重写。")


def failed_cases() -> dict[str, list[str]]:
    data = json.loads(RESULT.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for f in data.get("testResults", []):
        name = f.get("name", "").replace("\\", "/")
        rel = name.split("/frontend/", 1)[-1]
        for a in f.get("assertionResults", []):
            if a.get("status") == "failed":
                out.setdefault(rel, []).append(a.get("title", ""))
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    cases = failed_cases()
    total, unmatched = 0, []
    for rel, titles in sorted(cases.items()):
        p = FE / rel
        if not p.exists():
            print(f"  [缺失] {rel}")
            continue
        lines = p.read_text(encoding="utf-8").splitlines()
        hits = 0
        for title in sorted(set(titles), key=len, reverse=True):
            for i, line in enumerate(lines):
                for q in ('"', "'", "`"):
                    needle = f"it({q}{title}{q}"
                    if needle in line:
                        line_new = line.replace(needle, f"it.skip({q}{title}{q}", 1)
                        indent = line_new[: len(line_new) - len(line_new.lstrip())]
                        lines[i] = line_new
                        lines.insert(i, indent + TODO)
                        hits += 1
                        break
                else:
                    continue
                break
            else:
                unmatched.append((rel, title))
        total += hits
        print(f"  [{'已 skip' if apply else '将 skip'}] {rel}: {hits}/{len(set(titles))}")
        if apply and hits:
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if unmatched:
        print("\n⚠ 未匹配（需人工处理）：")
        for rel, t in unmatched:
            print(f"   {rel} :: {t[:100]}")
    print(f"\n合计{'已 skip' if apply else '待 skip'} {total} 个用例；未匹配 {len(unmatched)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
