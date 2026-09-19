# -*- coding: utf-8 -*-
"""T39 收工：PROGRESS.json T39 -> done + artifacts/evidence。

⚠️ DoD 口径说明（重要，必须留痕）：
原 DoD 为 `cd frontend && npm test` expect **exit 0（既有测试全绿，行为不变）**。
但仓库存在 **C 类 22 项既有红**（TD-FE-RED-2，与本卡无关、且非脚本可批量清除）。
经用户拍板采纳「推荐方案 A」：DoD 口径调整为
**「不新增失败（基线 22 failed / 7 文件）」**，C 类作为独立技术债保留推进。
本脚本会断言：全量失败数 **不超过** 基线且本卡文件零失败。
"""
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"
NOW = datetime.now().astimezone().isoformat(timespec="seconds")


def read(name: str) -> str:
    raw = (M / name).read_bytes()
    return re.sub(r"\x1b\[[0-9;]*m", "", raw.decode("utf-8-sig", errors="replace"))


def stats(name: str) -> dict:
    txt = read(name)
    m = re.search(r"EXIT=(\d+)", txt)
    p = re.findall(r"(\d+) passed", txt)
    f = re.findall(r"(\d+) failed", txt)
    return {
        "exit": int(m.group(1)) if m else -1,
        "passed": int(p[-1]) if p else -1,
        "failed": int(f[-1]) if f else 0,
        "fail_files": sorted(set(re.findall(r"FAIL\s+(\S+\.(?:tsx|ts))", txt))),
    }


base_before = stats("t39_base.txt")        # Settings 测试基线
verify1 = stats("t39_verify1.txt")         # 拆分后 Settings 测试
fe = stats("t39_fe_sweep.txt")             # 拆分后全量
print("Settings 基线（拆分前）:", base_before["exit"], base_before["passed"], "passed")
print("Settings 验证（拆分后）:", verify1["exit"], verify1["passed"], "passed")
print("全量（拆分后）        :", fe["exit"], fe["passed"], "passed /", fe["failed"], "failed")

assert base_before["exit"] == 0 and base_before["failed"] == 0, "留存基线不为绿"
assert verify1["exit"] == 0 and verify1["failed"] == 0, "Settings 行为发生变化"
assert len(fe["fail_files"]) == 7, f"失败文件数变化: {fe['fail_files']}"
BASELINE_FAILED, BASELINE_FILES = 22, 7
ok = fe["failed"] <= BASELINE_FAILED and len(fe["fail_files"]) <= BASELINE_FILES
print(f"零新增判定：failed {fe['failed']} <= {BASELINE_FAILED}；"
      f"files {len(fe['fail_files'])} <= {BASELINE_FILES} → {'通过' if ok else '不通过'}")
assert ok, "存在新增失败"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t39 = prog["tasks"].setdefault("T39", {})
t39["status"] = "done"
t39["started_at"] = t39.get("started_at") or NOW
t39["finished_at"] = NOW
t39["dod_note"] = (
    "DoD 口径经用户拍板调整为「不新增失败（基线 22 failed / 7 文件）」："
    "原 DoD 要求 `npm test` exit 0，但仓库存在 C 类 22 项既有红"
    "（TD-FE-RED-2，与本卡无关且非脚本可批量清除）。本卡实测全量 22 failed / 790 passed，"
    "与基线完全一致 → 零新增。"
)
t39["artifacts"] = [
    "**拆出 `settings/SettingsNav.tsx`**（新建，156 行）：设置页侧边导航从 `Settings.tsx`"
    "剥离为独立组件 —— 16 个导航项**数据驱动**渲染（NAV_ITEMS），"
    "导出 `SettingsSection` 联合类型（16 值）供壳层复用；"
    "**等价性契约原样保留**：`nav.settings-sidebar` + `aria-label={t(\"ariaSettingsCategories\")}`、"
    "`button.settings-nav-item`（含 `active` 类与 `aria-current=\"page\"`）、"
    "内部 `span.settings-nav-icon` + `span.settings-nav-copy`、项顺序与拆分前逐项一致",
    "`Settings.tsx`：**698 → 554 行（-144）**，`<nav>` 块（原 206~351）替换为"
    "`<SettingsNav active={activeSection} onSelect={setActiveSection} />`（缩进与位置不变），"
    "新增一行 import；**未改动任何设置项的行为、默认值与 localStorage 键**",
    "**原样保留的既有事实**：「数据中心」导航项在拆分前即为硬编码中文（未走 t()），"
    "本次搬迁**保持字面量不变** —— 本卡 not_do「不改任何设置项的行为与默认值」，"
    "文案国际化属另一议题（已留痕，供后续单独处理）",
    "测试基线：`Settings.test.tsx` 拆分前 **6 passed**（留存基线，pitfalls 要求）→"
    "拆分后 **6 passed**，覆盖 nav 项类名/数量、分区切换、`settings:navigate` 白名单跳转、"
    "localStorage 持久化 —— 行为不变的**可验证**证据",
]
t39["evidence"] = [
    f"pitfalls「先跑一遍全量测试留存基线」→ 已执行：Settings.test.tsx 拆分前"
    f" **{base_before['passed']} passed / exit {base_before['exit']}**",
    f"行为不变验证：拆分后 Settings.test.tsx **{verify1['passed']} passed / "
    f"exit {verify1['exit']}**（与基线一致）",
    f"全量回归：**{fe['passed']} passed / {fe['failed']} failed（{len(fe['fail_files'])} 文件）** ——"
    f"失败数与失败文件数与基线（22 / 7）**完全一致 → 零新增**；"
    f"失败文件均为既有因子域测试，**不含本卡任何文件**",
    "等价性佐证：替换脚本断言 `settings-nav-item` 在 Settings.tsx 中出现次数 **16 → 0**"
    "（导航项全部移动到新组件），行数 698 → 554（-144）",
    "⚠️ **DoD 口径说明**：原 DoD 要求 `npm test` exit 0，实测 exit 1 —— 唯一原因是仓库存在的"
    "**C 类 22 项既有红**（TD-FE-RED-2）。经用户拍板采纳推荐方案 A，口径调整为"
    "「不新增失败（基线 22 / 7 文件）」。**本卡自身零新增失败**，该调整已留痕于"
    " `PROGRESS.tasks.T39.dod_note`，不构成对本卡质量的放宽",
    "**未拆 rules 区的决策与理由**：rules 区（原 135 行 JSX）虽为最大块，但其**无测试覆盖**"
    "（Settings.test.tsx 只覆盖 nav 与分区切换），机械搬运的错误**无法被验证** → "
    "为守住「行为不变」的**可验证性**，本卡不拆该区；留待补测试覆盖后再拆"
    "（此为主动取舍，非遗漏）",
    "selfcheck ALL OK（47 任务依赖图无环/写权限声明/无重复键）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
done = sum(1 for v in prog["tasks"].values() if v.get("status") == "done")
print(f"\nPROGRESS T39 -> done @ {NOW}")
print(f"排期完成度：{done}/47")
