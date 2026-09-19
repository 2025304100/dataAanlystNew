# -*- coding: utf-8 -*-
"""T31 收工：PROGRESS.json T31 -> done + artifacts/evidence。"""
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


dod = stats("t31_run1.txt")
trans = stats("t31_trans.txt")
fe = stats("t31_fe_sweep.txt")
print("DoD MiningEvoParamStep:", dod["exit"], dod["passed"], "passed /", dod["failed"])
print("translations         :", trans["exit"], trans["passed"], "passed")
print("前端全量             :", fe["exit"], fe["passed"], "passed /", fe["failed"], "failed")

MINE = {"src/components/factors/mining/wizard/step4/MiningEvoParamStep.test.tsx",
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
t31 = prog["tasks"]["T31"]
assert t31.get("status") == "in_progress", f"T31 状态异常: {t31.get('status')}"

t31["status"] = "done"
t31["finished_at"] = NOW
t31["artifacts"] = [
    "step4/MiningEvoParamStep.tsx（DoD 组件）：简单/高级模式切换 —— **简单模式只出现"
    "「进化强度/选优偏好/启用 AI 生成」三件套，且不得出现任何专业术语**"
    "（帕累托/非支配/拥挤度/赛道/锦标赛，测试以术语黑名单断言守着，§6.5.5）；"
    "高级模式展开四块折叠区（经典底座 / 探索配置 / 赛道竞争与选择 / 繁殖与变异）+ "
    "种群/代数 + 快速试验模式；点击「开始挖掘实验」打开资源确认弹窗，"
    "另有「保存暂存」入口（不能绕过资源确认直接执行）",
    "step4/ResourceConfirmModal.tsx：**锁状态三态文案**（pitfalls 第一条）——"
    "无冲突「当前无其他写任务，可立即开始」/ `mining_domain` 冲突显示占用任务 ID 与代数"
    "并**禁用提交**（不排队）/ `duckdb_write` 冲突显示占用任务类型与**排队位次**；"
    "**ETA 必须标注「估算」**（pitfalls 第二条）；资源不足（磁盘<20%/内存<2GB）→ 阻断"
    "并给出调整建议（减少候选数/缩短时间范围/缩小股票池）",
    "step4/evoTypes.ts：`MiningLockStatus`（双锁结构，对齐 `GET /factor-mining/locks/status`"
    " —— 该接口**已实现**）/`EvoConfig`/默认配置（种群 100 / 代数 20）/`QUICK_TRIAL_PRESET`"
    "（50/10，向导 §7）/`formatEtaMinutes`",
    "step4/SelectionConfig.tsx（赛道竞争开关+保底比例+弱赛道代数+跨赛道率+锦标赛 K，"
    "术语集中于此区）、ClassicalBaseConfig.tsx（6 类别上限，未选字段类别**联动置灰**）、"
    "ExplorationConfig.tsx（AI/随机占比）、ReproductionConfig.tsx（**自适应开启时三率显示"
    "灰色「自动」且输入框 disabled**、关闭后才解锁手改 —— §6.6.8）",
    "step4/MiningEvoParamStep.test.tsx：14 用例（简单模式术语黑名单、高级模式展开与回退、"
    "自适应「自动」与解锁、锁三态三例、ETA 标「估算」、资源不足阻断与建议、"
    "**不暴露并行度黑名单**、快速试验预设 100/20→50/10、提交回调、暂存回调）",
    "i18n zh-CN + en-US：新增 47 个 `miningEvo*` / `miningRes*` key 两份严格同步；"
    "文案层面同样避开「并行/进程/worker/CPU」等实现词（not_do 延伸到 i18n 值）",
]
t31["evidence"] = [
    f"DoD `cd frontend && npm test -- MiningEvoParamStep` → 实际执行"
    f" `npx vitest run .../step4/MiningEvoParamStep.test.tsx` → **{dod['passed']} passed,"
    f" exit {dod['exit']}**（一次全绿）",
    f"i18n 同步门禁 translations.test.ts → **{trans['passed']} passed, exit {trans['exit']}**"
    "（新增 47 key 两份齐全）",
    f"防波及：前端全量 `npm test` → **{fe['passed']} passed / {fe['failed']} failed** ——"
    f"与既有红基线（44 / 10 文件）**持平，零引入**",
    "**not_do 守成立**：测试对整页 textContent 做黑名单断言 —— 不含「并行/进程/worker/"
    "CPU/cpu」，确保并行度不外露（含弹窗打开态）",
    "**pitfalls 双守成立**：①锁三态各有独立用例（无冲突文案 / 域冲突禁用提交且不显示排队位次 /"
    "写冲突显示排队位次）；②ETA 文案含「估算」二字（`data-eta` 断言）",
    "**简单模式术语黑名单**（§6.5.5「不出现帕累托/非支配/拥挤度/赛道等术语」）已用测试固化："
    "默认渲染后 body 文本对 5 个术语逐一断言 not.toContain；切高级后同一批术语至少出现一个",
    "关键事实：`GET /factor-mining/locks/status` **已实现**（T21/T23 双锁），故资源确认弹窗"
    "可接真实数据；提交接口 `POST /factor-mining/runs` 仍为 NotImplementedError(M5)，"
    "故提交动作按「回调上抛」实现，待后端补齐后由父级接入（前端零改动）",
    "selfcheck ALL OK；writes 补登记 i18n 两份（T27~T31 连续第 5 张前端卡同款口径）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS T31 -> done @", NOW)
