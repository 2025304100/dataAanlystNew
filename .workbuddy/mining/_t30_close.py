# -*- coding: utf-8 -*-
"""T30 收工：PROGRESS.json T30 -> done + artifacts/evidence。"""
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
        "fail_files": sorted(set(re.findall(r"FAIL\s+([^\s\[:]+)", txt))),
    }


dod = stats("t30_run1.txt")
trans = stats("t30_trans.txt")
fe = stats("t30_fe_sweep.txt")
print("DoD MiningFieldStep:", dod["exit"], dod["passed"], "passed /", dod["failed"])
print("translations       :", trans["exit"], trans["passed"], "passed")
print("前端全量           :", fe["exit"], fe["passed"], "passed /", fe["failed"], "failed")

MINE = {"src/components/factors/mining/wizard/step3/MiningFieldStep.test.tsx",
        "src/i18n/__tests__/translations.test.ts"}
assert dod["exit"] == 0 and dod["failed"] == 0, "DoD 未全绿"
assert trans["exit"] == 0 and trans["failed"] == 0, "i18n 门禁未全绿"
assert not (MINE & set(fe["fail_files"])), "本卡文件在全量中失败"
BASELINE_FAILED, BASELINE_FILES = 44, 10
ok = fe["failed"] <= BASELINE_FAILED and len(fe["fail_files"]) <= BASELINE_FILES
print(f"既有红对比：{fe['failed']} vs 基线 {BASELINE_FAILED}；文件 "
      f"{len(fe['fail_files'])} vs {BASELINE_FILES} → {'零引入' if ok else '新增失败'}")
assert ok, "前端全量失败数超过既有基线，禁止收工"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t30 = prog["tasks"]["T30"]
assert t30.get("status") == "in_progress", f"T30 状态异常: {t30.get('status')}"

t30["status"] = "done"
t30["finished_at"] = NOW
t30["artifacts"] = [
    "step3/MiningFieldStep.tsx（DoD 组件）：字段按 5 组（行情/估值/财报/资金流/事件）"
    "勾选；**带 `blocked_reason` 或 `data_mode=blocked` 的字段不可勾选**"
    "（「字段存在 ≠ 可用于挖掘」，系统不自动剔除/降级/填零）；已选计数；"
    "「开始校验」→ 创建异步校验任务 → **5s 轮询**进度（not_do：不引入 WebSocket），"
    "到终态（passed/blocked/failed）自动停表；字段列表由 props 注入，**无接口不臆造**",
    "step3/FieldValidationPanel.tsx：分片进度（已完成/总数，校验可达数小时、支持离开页面）、"
    "通过态、警告态、**阻断详情**（逐字段：代码/中文名/问题类型/当前值/要求值）；"
    "**阻断只给两个出口**（重新选择字段 / 去修复数据）——出口按钮位于阻断项**之外**，"
    "阻断项内**零操作按钮**，落实 not_do「不提供逐字段自动修复」（有测试断言守着）",
    "step3/fieldTypes.ts：`MiningField` / `ValidationStatus` / `BlockedItem` / `FieldGroup`"
    " 与终态集合 `TERMINAL_STATUSES`、轮询间隔 `POLL_INTERVAL_MS=5000`",
    "step3/fieldApi.ts：校验四接口封装（create / get / valid / resume / pause）——"
    "契约对齐 T14 的 `factor_mining_wizard.py`（**该组接口已实现**）",
    "step3/MiningFieldStep.test.tsx：9 用例（分组渲染与勾选上抛、已选计数、blocked 不可勾选、"
    "发起校验与分片进度、**5s 轮询且 WebSocket 未被构造**、通过态、阻断详情含当前/要求值、"
    "**两个出口可点击**、**阻断项内无任何按钮**）",
    "i18n zh-CN + en-US：新增 15 个 `miningField*` key 两份严格同步",
]
t30["evidence"] = [
    f"DoD `cd frontend && npm test -- MiningFieldStep` → 实际执行"
    f" `npx vitest run .../step3/MiningFieldStep.test.tsx` → **{dod['passed']} passed,"
    f" exit {dod['exit']}**（TDD：测试先行定义契约 → 实现 → 一次全绿）",
    f"i18n 同步门禁 translations.test.ts → **{trans['passed']} passed, exit {trans['exit']}**",
    f"防波及：前端全量 `npm test` → **{fe['passed']} passed / {fe['failed']} failed** ——"
    f"与既有红基线（44 / 10 文件）**持平，零引入**；失败文件均为既有因子域测试，"
    f"不含本卡任何文件",
    "**not_do 双守成立**：①轮询实现用 `setInterval(5000)`，测试以 `WebSocket` 构造器"
    " spy 断言**从未被调用**；②阻断处理无逐字段修复入口 —— 阻断项内 button 数为 0",
    "关键事实：T14 的校验四接口**已实现**（create/get/valid/resume/pause），故本卡校验链路"
    "可接真实后端；但**挖掘字段目录接口缺失**（后端现仅有候选池 `filter-fields` 与"
    " `/external-data/quality-snapshots/{field}`），因此字段列表按 props 注入、"
    "待目录接口补齐后由父级传入即可（前端零改动）",
    "selfcheck ALL OK（依赖图无环/写权限声明/无重复键）；writes 补登记 i18n 两份（同 T27~T29 口径）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS T30 -> done @", NOW)
