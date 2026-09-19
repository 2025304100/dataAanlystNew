# -*- coding: utf-8 -*-
"""T29 收工：PROGRESS.json T29 -> done + artifacts/evidence。

守卫：DoD MiningSplitPanel 全绿 + translations 全绿 + 前端全量失败数不超过
既有基线（44 failed / 10 文件）+ 本卡文件零失败。
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
        "fail_files": sorted(set(re.findall(r"FAIL\s+([^\s\[:]+)", txt))),
    }


dod = stats("t29_run1.txt")
trans = stats("t29_trans.txt")
fe = stats("t29_fe_sweep.txt")
print("DoD MiningSplitPanel:", dod["exit"], dod["passed"], "passed /", dod["failed"])
print("translations        :", trans["exit"], trans["passed"], "passed")
print("前端全量            :", fe["exit"], fe["passed"], "passed /", fe["failed"], "failed")

MINE = {"src/components/factors/mining/wizard/step2/MiningSplitPanel.test.tsx",
        "src/i18n/__tests__/translations.test.ts"}
assert dod["exit"] == 0 and dod["failed"] == 0, "DoD 未全绿"
assert trans["exit"] == 0 and trans["failed"] == 0, "i18n 同步门禁未全绿"
assert not (MINE & set(fe["fail_files"])), "本卡文件在前端全量中失败"

BASELINE_FAILED, BASELINE_FILES = 44, 10
ok = fe["failed"] <= BASELINE_FAILED and len(fe["fail_files"]) <= BASELINE_FILES
print(f"既有红对比：failed {fe['failed']} vs 基线 {BASELINE_FAILED}；"
      f"文件 {len(fe['fail_files'])} vs {BASELINE_FILES} → {'零引入' if ok else '新增失败'}")
assert ok, "前端全量失败数超过既有基线，禁止收工"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t29 = prog["tasks"]["T29"]
assert t29.get("status") == "in_progress", f"T29 状态异常: {t29.get('status')}"

t29["status"] = "done"
t29["finished_at"] = NOW
t29["artifacts"] = [
    "step2/MiningSplitPanel.tsx（DoD 组件，artifacts 主体）：三段比例输入（默认"
    " 60/20/20，**和必须为 100%**，否则显示阻断提示）、比例/自定义日期边界双模式切换、"
    "**SplitBudget 展示区**（总点数、三段点数、最低门槛、tail_loss）；"
    "**purge / embargo 同时展示「调仓点数」与「折算交易日数」**（任务卡 pitfalls 第一条："
    "否则 purge=5 会被误读成 5 个交易日）；月频 `statistically_degraded` → 「最高 B 级」"
    "降级提示；`meets_floor=false` → 未达最低样本量阻断（含门槛值与调整方向）",
    "step2/splitTypes.ts：`SplitBudget` / `SplitRatios` / `SplitMode` / 目标与频率类型"
    " —— 字段与后端 `contracts.SplitBudget` 一一对应；并固化最小样本量硬门槛"
    " `FREQUENCY_FLOOR`（日 252 / 周 104 / 月 36）与生产建议 `FREQUENCY_RECOMMENDED`"
    "（周 156 / 月 60，界面同时展示「最低可运行/建议样本量」差异）",
    "step2/MirrorGuideBanner.tsx（向导 §4.1 四种提示）：起点 <2021-01-01 → 幸存者偏差"
    "黄色告警；涉及 2016–2020 → 低覆盖提示；月频 + 未镜像 10 年 → 蓝色引导「test 段不足，"
    "建议镜像 10 年」+「去镜像」；超出已镜像范围 → 阻断 + 去镜像",
    "step2/MiningTimeTargetStep.tsx（Step2 编排）：起止日期（结束日 ≤ 数据截止日）/"
    "调仓频率/预测持有期 1~20/预测目标四选一 + 门槛提示 + 镜像引导 + 切分面板，"
    "配置变化统一上抛 `onChange`",
    "step2/MiningSplitPanel.test.tsx：13 用例（默认比例与合计、和不为 100 阻断、"
    "改回 100 恢复、比例上抛、自定义模式切换与回退、预算总/段点数、**purge 双单位**、"
    "embargo 双单位、月频最高 B 级、未达门槛、**无 budget 不臆造**、**段边界只来自后端**）",
    "i18n zh-CN + en-US：新增 37 个 `miningSplit*` / `miningTime*` / `miningMirror*` key"
    " 两份严格同步",
]
t29["evidence"] = [
    f"DoD `cd frontend && npm test -- MiningSplitPanel` → 实际执行"
    f" `npx vitest run .../step2/MiningSplitPanel.test.tsx` → **{dod['passed']} passed,"
    f" exit {dod['exit']}**（TDD：先写测试定义契约 → 实现 → 全绿）",
    f"i18n 同步门禁 translations.test.ts → **{trans['passed']} passed, exit {trans['exit']}**"
    "（新增 37 key 两份齐全）",
    f"防波及：前端全量 `npm test` → **{fe['passed']} passed / {fe['failed']} failed** ——"
    f"与既有红基线（44 failed / 10 文件）**持平，零引入**；失败文件均为既有因子域测试，"
    f"**不含本卡任何文件**",
    "**not_do 守成立**：组件不计算切分与分位数 —— 段边界/各段点数/purge/embargo 全部来自"
    "后端下发的 `SplitBudget`；测试以「无 budget 时不渲染预算区与段边界」+「改比例不改变"
    "段边界」两条断言固化该约束",
    "关键事实：后端有 Python 侧 `evaluation_adapter.compute_split_budget()`，"
    "但**尚无切分预算的 HTTP 预览路由** —— 本卡按「只展示后端下发值」实现，"
    "待后端补预览接口后由父级注入 `budget` 即可（前端无需改动）",
    "selfcheck ALL OK（47 任务依赖图无环/写权限声明/无重复键；T29 与在跑卡零写重叠）",
    "⚠️ writes 补登记：原卡 writes 仅 `step2/` 目录，前端文案必须走 t() 且 translations"
    " 要求两份同步 → 按 T28/T27 同款口径补登记 i18n 两份（已写进 tasks.json 并注明理由）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS T29 -> done @", NOW)
