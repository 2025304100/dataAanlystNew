# -*- coding: utf-8 -*-
"""既有前端红收口 第 2 步：批量补齐缺失的 api mock（TD-FE-RED）。

实测缺失方法（`api.X is not a function`）共 5 个 —— 均为 `client.ts` 演进后
测试 mock 未同步：
  scoringGetOverviewAsFactor / scoringGetOverview / scoringGetFactorDefinition /
  scoringGetFactorModelListAsFactor / getEvaluationTaskHeartbeat

**纪律**：只补 mock（且**必须带尾逗号**——首轮漏逗号导致 5 个文件 esbuild 语法错误），
不改断言、不改组件；补完后逐个文件做 **esbuild 语法预检**，避免再次返工。

用法：
    python .workbuddy/mining/_fix_fe_red2.py            # 干跑
    python .workbuddy/mining/_fix_fe_red2.py --apply     # 应用
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

#: 方法 → mock 表达式（**行尾逗号由模板统一加**）
MOCKS: dict[str, str] = {
    "scoringGetOverviewAsFactor": (
        "vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, "
        "health: {}, latest_trade_date: null, factor_coverage: [] }))"),
    "scoringGetOverview": (
        "vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, "
        "health: {}, factor_coverage: [] }))"),
    "scoringGetFactorDefinition": "vi.fn(async () => ({}))",
    "scoringGetFactorModelListAsFactor": (
        "vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 }))"),
    # 第 3 轮补：组件实际调用 `scoringListFactorSetsAsFactor(\"any\", 50)`，多数测试 mock 未定义
    "scoringListFactorSetsAsFactor": (
        "vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 50 }))"),
    "getEvaluationTaskHeartbeat": "vi.fn(async () => ({}))",
    # 第 2 轮补：从全量日志提取出的另外两个缺失方法
    "scoringListTasks": (
        "vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 }))"),
    "getInboxNotifications": (
        "vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 }))"),
}

#: 锚点（按优先级）：在锚点行**之后**插入
ANCHORS = [
    "scoringGetOverviewAsFactor:",
    "scoringGetOverview:",
    "scoringGetFactorDefinition:",
    "activateFactorModel:",
    "initializeFactorWarehouse:",
    "updateFactorSystemConfig:",
    "getFactorOverview:",
    "getDataHealth:",
    "getMacroOverview:",
]
#: 兜底：mock 对象内首个 4+ 空格缩进的 `key: vi.fn(` 行
FALLBACK = re.compile(r"^(\s{4,})\w+: vi\.fn\(")


def target_files() -> list[str]:
    """从 TD-FE-RED-2 第 1 轮的结构化清单取文件列表。

    （verbose 输出里 FAIL 路径会折行，故不直接正则扫日志，而用已解析的 JSON。）
    """
    items = json.loads(
        (ROOT / ".workbuddy/mining/td_fe_red2_items.json").read_text("utf-8"))
    files = set()
    for it in items:
        head = it.get("head", "")
        path = head.split(" > ")[0].replace("FAIL", "").strip()
        if path.startswith("src/") and path.endswith((".tsx", ".ts")):
            files.add(path)
    return sorted(files)


def esbuild_ok(rel: str) -> tuple[bool, str]:
    """用 esbuild 做语法预检（快且准，避免上次的语法返工）。

    ⚠️ flag 必须是 `--loader:.tsx=tsx`（写成 `--loader=tsx` 会报
    "loader without extension only applies when reading from stdin" → 全部误报）。
    """
    r = subprocess.run(
        ["npx", "esbuild", rel, "--loader:.tsx=tsx", "--outfile=NUL"],
        cwd=str(FE), capture_output=True, text=True, encoding="utf-8",
        errors="replace", shell=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        return False, (msg[0] if msg else "unknown")
    return True, ""


def main() -> int:
    apply = "--apply" in sys.argv
    files = target_files()
    print(f"目标文件 {len(files)} 个\n")
    changed, skipped, failed = [], [], []

    for rel in files:
        p = FE / rel
        text = p.read_text(encoding="utf-8")
        missing = {k: v for k, v in MOCKS.items() if k not in text}
        if not missing:
            skipped.append(rel)
            continue

        lines = text.splitlines()
        anchor_idx, indent = None, "    "
        for anchor in ANCHORS:
            for i, line in enumerate(lines):
                if anchor in line:
                    anchor_idx = i
                    indent = line[: len(line) - len(line.lstrip())]
                    break
            if anchor_idx is not None:
                break
        if anchor_idx is None:
            for i, line in enumerate(lines):
                m = FALLBACK.match(line)
                if m:
                    anchor_idx, indent = i, m.group(1)
                    break
        if anchor_idx is None:
            failed.append((rel, "无可用锚点"))
            continue

        new_lines = [
            f"{indent}{key}: {expr}," for key, expr in missing.items()
        ]
        lines[anchor_idx + 1: anchor_idx + 1] = new_lines
        if apply:
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            ok, err = esbuild_ok(rel)
            if not ok:
                failed.append((rel, f"语法预检失败: {err}"))
                continue
        changed.append((rel, list(missing)))

    print(f"补齐 mock: {len(changed)} 个文件")
    for rel, keys in changed:
        print(f"  + {rel}\n      +{keys}")
    if skipped:
        print(f"\n无需改动: {len(skipped)}")
        for r in skipped:
            print("  =", r)
    if failed:
        print(f"\n失败: {len(failed)}")
        for r, why in failed:
            print(f"  ! {r} —— {why}")
    print("\n" + ("已应用" if apply else "干跑未修改"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
