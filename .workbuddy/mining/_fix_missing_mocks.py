# -*- coding: utf-8 -*-
"""TD-FE-RED-2 第 3 轮：**动态**补齐「组件调用但测试 mock 未定义」的 api 方法。

根因（实测）：`FactorModelPage` 组件调用 18 个 api 方法，而测试 mock 只定义了 3 个
→ 缺 15 个；`FactorModelSettings` 缺 9 个；`ExternalDataSync` 缺 3 个。
组件加载路径上调用的方法一抛 `is not a function`，就落错误态 → 页面渲染不全 →
大量断言「找不到元素」。

本脚本**动态扫描**（不硬编码方法名）：
  组件源码 `api.<method>(` → 得到调用集；测试文件 mock 键 → 得到定义集；
  差集即缺失，按名字推断合理返回值补齐（list/Relation → 分页对象；其余 → 空对象）。

**安全性**：只**新增** mock 行（带尾逗号，且写后做 esbuild 语法预检），
不改任何断言、不改组件；跑完立即用测试验证失败数是否下降。

用法：
    python .workbuddy/mining/_fix_missing_mocks.py            # 干跑
    python .workbuddy/mining/_fix_missing_mocks.py --apply
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
FE = ROOT / "frontend"

#: 测试文件 → 被测组件
PAIRS: dict[str, str] = {
    "src/components/__tests__/ExternalDataSync.test.tsx":
        "src/components/ExternalDataSync.tsx",
    "src/components/__tests__/FactorModelPage.test.tsx":
        "src/components/factors/FactorModelPage.tsx",
    "src/components/__tests__/FactorModelSettings.test.tsx":
        "src/components/FactorModelSettings.tsx",
    "src/components/factors/__tests__/FactorModelPage.collections.test.tsx":
        "src/components/factors/FactorModelPage.tsx",
    "src/components/factors/__tests__/FactorModelPage.t13-supplement.test.tsx":
        "src/components/factors/FactorModelPage.tsx",
}

PAGE = "vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 }))"
EMPTY = "vi.fn(async () => ({}))"

ANCHORS = [
    "scoringGetOverviewAsFactor:", "scoringGetFactorModelListAsFactor:",
    "scoringListFactorSetsAsFactor:", "scoringListTasks:", "getInboxNotifications:",
    "scoringGetOverview:", "scoringGetFactorDefinition:", "getEvaluationTaskHeartbeat:",
    "activateFactorModel:", "getFactorModels:", "listFactorSets:", "api:",
]
FALLBACK = re.compile(r"^(\s{4,})\w+:\s*vi\.fn\(")


def mock_value(name: str) -> str:
    if re.search(r"(List|Relations)", name):
        return PAGE
    return EMPTY


def esbuild_ok(rel: str) -> tuple[bool, str]:
    r = subprocess.run(["npx", "esbuild", rel, "--loader:.tsx=tsx", "--outfile=NUL"],
                       cwd=str(FE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", shell=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        return False, (msg[0] if msg else "unknown")
    return True, ""


def main() -> int:
    apply = "--apply" in sys.argv
    total_added, report = 0, []

    for t_rel, c_rel in PAIRS.items():
        tp, cp = FE / t_rel, FE / c_rel
        if not tp.exists() or not cp.exists():
            report.append((t_rel, "文件缺失", []))
            continue
        called = set(re.findall(r"api\.(\w+)\s*\(", cp.read_text(encoding="utf-8")))
        text = tp.read_text(encoding="utf-8")
        mocked = set(re.findall(r"^\s*(\w+)\s*:", text, re.M))
        missing = sorted(c for c in called if c not in mocked)
        if not missing:
            report.append((t_rel, "无缺失", []))
            continue

        lines = text.splitlines()
        # 锚点：优先已存在的 mock 键所在行，之后插入
        idx, indent = None, "    "
        for anchor in ANCHORS:
            for i, line in enumerate(lines):
                if anchor in line:
                    idx, indent = i, line[: len(line) - len(line.lstrip())]
                    break
            if idx is not None:
                break
        if idx is None:
            for i, line in enumerate(lines):
                m = FALLBACK.match(line)
                if m:
                    idx, indent = i, m.group(1)
                    break
        if idx is None:
            report.append((t_rel, "无锚点", missing))
            continue

        new_lines = [f"{indent}{n}: {mock_value(n)}," for n in missing]
        lines[idx + 1: idx + 1] = new_lines
        if apply:
            tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            ok, err = esbuild_ok(t_rel)
            if not ok:
                report.append((t_rel, f"语法预检失败: {err}", missing))
                continue
        total_added += len(missing)
        report.append((t_rel, "已补" if apply else "将补", missing))

    for rel, status, miss in report:
        print(f"  [{status}] {rel}")
        for m in miss:
            print(f"        + {m}")
    print(f"\n合计{'已补' if apply else '待补'} {total_added} 个 mock 方法")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
