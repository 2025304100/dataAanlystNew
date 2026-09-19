# -*- coding: utf-8 -*-
"""T32 收工：PROGRESS.json T32 -> done + artifacts/evidence。

DoD：`npm test -- FactorMining` exit 0（实测 2 文件 19 用例）。
G5 门禁说明：5 步组件与结果页已交付，但向导容器 `MiningShell.tsx`
（T27 写权限，不在本卡 writes 内）尚未接线到 step1~step5 —— 本卡**不越权修改**，
在 evidence 中记录并提示 G5 验收需容器接线。
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


dod = stats("t32_run1.txt")
trans = stats("t32_trans.txt")
fe = stats("t32_fe_sweep.txt")
print("DoD FactorMining :", dod["exit"], dod["passed"], "passed /", dod["failed"])
print("translations     :", trans["exit"], trans["passed"], "passed")
print("前端全量         :", fe["exit"], fe["passed"], "passed /", fe["failed"], "failed")

MINE = {"src/components/factors/mining/wizard/step5/FactorMiningRunTrack.test.tsx",
        "src/components/factors/mining/result/FactorMiningResult.test.tsx",
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
t32 = prog["tasks"]["T32"]
assert t32.get("status") == "in_progress", f"T32 状态异常: {t32.get('status')}"

t32["status"] = "done"
t32["finished_at"] = NOW
t32["artifacts"] = [
    "wizard/step5/FactorMiningRunTrack.tsx（进化跟踪，向导 §8.3.1~§8.3.7）："
    "进度总览（代数 X/Y、收敛状态、多样性百分比、ETA）；**多样性 <30% → 早熟告警**"
    "并建议提高随机注入率（§8.3.1）；ICIR 进化曲线（最优实点 + 平均虚点）；"
    "**收敛参考线来自配置阈值 `convergence_threshold`**（§8.3.2 明令不得把 0.08 写成"
    "固定门禁 —— 测试断言「取配置值 0.05 且页面不出现 0.08」）；收敛且连续 3 代无显著"
    "进步 → **提示「建议停止」但不自动停**（是否停止由用户确认）；三操作（§8.3.5）："
    "中断（→ 暂停，按钮变「继续」）/ 提前停止（确认框说明保留前 X 代 + 对 Top N 做最终"
    "验证）/ 完全放弃（二次确认 + 提示不可恢复）；**最终验证进度单独展示**（§8.3.7）",
    "result/FactorMiningResult.tsx（结果总览，向导 §8.4）：**顶部研究声明**（固定为页面"
    "第一个区块，pitfalls 第一条）；有效因子表（公式/等级标签/校正后 ICIR/衰减率/来源）；"
    "等级 chip 筛选（全部/S/A/B/C/D + 各级数量）；**D 级默认不勾选**，选中集含 D 级时"
    "「加入 FactorSet」**二次确认**（提示 N 个中含 M 个 D 级）；**跳转因子模型页携带 4 项**"
    "（factor_set_id / data_cutoff_at / candidate_pool_snapshot_id / rebalance_frequency），"
    "缺项时按钮禁用避免带残缺参数（pitfalls 第二条）；**不自动激活因子或模型**（not_do，"
    "测试断言渲染后 onActivate/onGotoFactorModel 均未被调用）",
    "wizard/step5/runTypes.ts + result/resultTypes.ts：`RunProgress`/`CurvePoint`/"
    "`FinalValidationProgress` 与 `MiningResultRow`/`MiningResultContext`/`GRADE_CHIPS`/"
    "`contextComplete()`；常量 `PREMATURE_DIVERSITY=0.3`、`STALL_SUGGEST_STOP=3`",
    "测试 2 文件共 19 用例：step5 侧 9 例（总览、早熟告警正反两例、曲线阈值来自配置、"
    "收敛提示不自动停、中断→继续、停止确认含代数与 Top N、放弃确认含不可恢复、"
    "最终验证进度）；result 侧 10 例（研究声明且位于首区块、**不自动激活**、"
    "跳转 4 项齐全且键数恰为 4、上下文缺项禁用跳转、chip 数量与过滤、D 级默认不勾选、"
    "含 D 级二次确认、不含 D 级直接执行、列表字段断言）",
    "i18n zh-CN + en-US：新增 41 个 `miningRun*` / `miningResult*` key 两份严格同步",
]
t32["evidence"] = [
    f"DoD `cd frontend && npm test -- FactorMining` → 实际执行"
    f" `npx vitest run FactorMining` → **2 文件 {dod['passed']} passed, exit {dod['exit']}**"
    "（一次全绿）",
    f"i18n 同步门禁 translations.test.ts → **{trans['passed']} passed, exit {trans['exit']}**"
    "（新增 41 key 两份齐全）",
    f"防波及：前端全量 `npm test` → **{fe['passed']} passed / {fe['failed']} failed** ——"
    f"与既有红基线（44 / 10 文件）**持平，零引入**",
    "**not_do 守成立**：结果页渲染后不自动激活 —— 测试注入 onActivate/onGotoFactorModel 后"
    "断言均**未被调用**（页面无任何自动跳转副作用）",
    "**pitfalls 双守成立**：①研究声明作为 `data-result-page` 的**第一个元素**（测试断言"
    "首区块即声明）；②跳转参数**恰为 4 个键**（断言 `Object.keys(arg).length === 4` "
    "且四值逐一匹配），上下文缺项时按钮 disabled 且点击不触发回调",
    "**§8.3.2 禁止写死门禁**已用测试固化：页面显示 `data-run-curve` 中的阈值取自 "
    "`progress.convergence_threshold`（传 0.05 时页面出现 0.05、且**不出现 0.08**）",
    "⚠️ **G5 门禁待接线**：5 步组件（step1~step5）与结果页均已交付并各自有测试，"
    "但向导容器 `frontend/src/components/factors/mining/MiningShell.tsx`（**T27 写权限，"
    "不在本卡 writes 内**）目前只有步骤条 `data-mining-step-bar`（5 步键位齐全），"
    "**尚未接线到各 step 组件**。本卡遵守写权限边界**未越权修改容器** —— G5「5 步向导可"
    "点通到结果页」的验收需由容器接线动作完成（建议作为 T27 补充或独立收口卡处理）",
    "关键事实：`GET /factor-mining/runs/{id}` 与 `/generations`、`/candidates` 均为 "
    "M7 未实现 → 本卡按 props 注入 + 空态容错实现，后端补齐后父级接入即可（前端零改动）",
    "selfcheck ALL OK；writes 补登记 i18n 两份（T27~T32 连续第 6 张前端卡同款口径）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS T32 -> done @", NOW)
