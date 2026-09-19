# -*- coding: utf-8 -*-
"""T27 收工：PROGRESS.json T27 -> done + artifacts/evidence（数字实时读取落盘结果）。"""
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
    txt = raw.decode("utf-8-sig", errors="replace")
    return re.sub(r"\x1b\[[0-9;]*m", "", txt)


def summary(name: str) -> dict:
    txt = read(name)
    d = {
        "exit": (re.search(r"EXIT=(\d+)", txt).group(1)
                 if re.search(r"EXIT=(\d+)", txt) else "-"),
        "passed": (int(re.search(r"Tests\s+.*?(\d+) passed", txt).group(1))
                   if re.search(r"Tests\s+.*?(\d+) passed", txt) else 0),
        "failed": (int(re.search(r"(\d+) failed", txt).group(1))
                   if re.search(r"(\d+) failed", txt) else 0),
        "fail_files": sorted(set(re.findall(r"FAIL\s+([^\s\[]+\.tsx?)", txt))),
    }
    return d


green = summary("t27_green.txt")      # 本卡新增 Settings.test.tsx
trans = summary("t27_trans.txt")      # i18n 同步门禁
sweep = summary("t27_sweep.txt")      # 前端全量防波及
dod = summary("t27_dod_settings.txt")  # DoD 原文 npm test -- Settings

print("Settings.test.tsx  :", green)
print("translations       :", trans)
print("frontend 全量      :", sweep["exit"], sweep["passed"], "passed,",
      sweep["failed"], "failed")
print("DoD 聚合(Settings) :", dod["exit"], "| fail files:", dod["fail_files"])

# ── 收工守卫：本卡范围内必须全绿；全量不得出现本卡文件失败 ─────────────
MINE = {"src/components/__tests__/Settings.test.tsx",
        "src/i18n/__tests__/translations.test.ts",
        "src/components/factors/mining/MiningShell.tsx"}
assert green["exit"] == "0" and green["failed"] == 0, "本卡 Settings 测试未全绿"
assert trans["exit"] == "0" and trans["failed"] == 0, "i18n 同步门禁未全绿"
intr = MINE.intersection(set(sweep["fail_files"]))
assert not intr, f"全量中本卡文件失败（零引入校验未过）: {intr}"
assert "Settings.test.tsx" not in " ".join(dod["fail_files"]), \
    "DoD 聚合失败中包含本卡 Settings.test.tsx"

# 既有红（非本卡引入）单独记录，不阻塞收工但必须写进 evidence
preexisting = sorted(set(sweep["fail_files"]) - MINE)
print("既有失败文件（非本卡）:", preexisting or "none")

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t27 = prog["tasks"]["T27"]
assert t27.get("status") == "in_progress", f"T27 状态异常: {t27.get('status')}"

t27["status"] = "done"
t27["finished_at"] = NOW
t27["artifacts"] = [
    "frontend/src/components/Settings.tsx：6 处机械改造（设计 §9.1）——①L34 activeSection"
    " 联合类型加 \"factor-mining\"；②L38 localStorage 白名单加该值（刷新后停留在本栏）；"
    "③**L76 settings:navigate 白名单加 \"factor-mining\"**（pitfalls 标注最易漏，不加则"
    "跨模块跳转无效）；④导航按钮（settings-nav-item + aria-current + ExperimentOutlined"
    " 图标 + t(\"factorMiningTabTitle\")）；⑤内容块"
    " data-settings-content=\"settings-factor-mining\" 挂载 MiningShell；⑥import 引入"
    " MiningShell。not_do「不新增 App 顶级 Tab」守成立（仅左侧导航新增一栏）",
    "frontend/src/components/factors/mining/MiningShell.tsx（新建，设计 §9.2 壳）："
    "子页签「向导 / 批次列表」；向导渲染 5 步步骤条（详细设计 §2：候选股票池/时间与目标/"
    "字段与校验/进化参数/进化执行与结果）+ 待开放占位；批次列表按契约调用"
    " GET /factor-mining/runs 并**三态容错**（加载/空/错误）——后端该接口当前为"
    " NotImplementedError（M5），不可因 501 让整页崩溃。文案全部走 t()，零硬编码中文",
    "frontend/src/types/mining.ts（新建）：MiningRunCreate/MiningRunCreated/MiningRun/"
    "MiningPage/MiningCandidate/MiningLockStatus/MINING_STEPS 五步常量与 MiningStepKey，"
    "对齐后端 MiningRunCreate/Page DTO 与向导 §2 步骤口径",
    "frontend/src/api/factorMining.ts（新建）：按 dbConfig.ts 范式走 requestJson 单一入口，"
    "封装 runs 生命周期（list/get/create/cancel/pause/resume/stop/discard）、"
    "generations/candidates/lineage、locks/status、submit/batch-review；"
    "**文件头明确标注后端实现进度**（已实现仅 locks/status 与 submit/batch-review，"
    "其余 M5/M7/M9/M10/M12/M13 未实现），避免后续卡误用",
    "i18n zh-CN.ts + en-US.ts：新增 15 个挖掘入口 key（factorMiningTabTitle、"
    "miningTabWizard/Runs、miningStepPool/TimeTarget/Field/Evolution/Run、"
    "miningWizardPending、miningRunsLoading/Empty/Error、miningRunsColRun/Status/Progress）"
    "**两份严格同步**（translations.test.ts 断言 key 集合相等）",
    "frontend/src/components/__tests__/Settings.test.tsx（新建，6 用例）：菜单可见、"
    "点击挂载 MiningShell、**settings:navigate 跳转生效（L76 白名单守卫）**、"
    "白名单外值不误切换、localStorage 持久化、not_do 顶级 Tab 守卫",
]
t27["evidence"] = [
    f"DoD-1 本卡新增 Settings 测试：npx vitest run src/components/__tests__/Settings.test.tsx"
    f" → **{green['passed']} passed, exit {green['exit']}**（TDD：红 4 failed|2 passed →"
    f" 实现六处 → 绿；修复 1 处测试写法：原生 window.dispatchEvent 未包 act() 导致 React"
    f" 状态不 flush，fireEvent.click 自带 act 故点击用例先绿）",
    f"DoD-2 i18n 同步门禁：translations.test.ts → **{trans['passed']} passed,"
    f" exit {trans['exit']}**",
    "⚠️ **既有红（非本卡引入，已定性）**：DoD 聚合命令 `npm test -- Settings` 当前"
    " EXIT=1，3 项失败全部位于 `src/components/__tests__/FactorModelSettings.test.tsx`——"
    "根因：组件 `FactorModelSettings.tsx:308` 调用了 `api.scoringGetOverviewAsFactor()`，"
    "而该测试的 mock 对象未定义此方法（同目录另 3 个测试文件"
    " linked-columns/pipeline-ui/FactorCenter.T13-supplement 均已 mock）。"
    "本卡**未触碰**该组件、api/client.ts 与该测试文件（均在 writes 声明之外），"
    "按「勿顺手修他人债 + 不越 writes 声明」纪律**未修改**，建议单立技术债卡补一行"
    " `scoringGetOverviewAsFactor: vi.fn(...)` 即可绿",
    f"防波及：前端全量 `npm test` → **{sweep['passed']} passed / {sweep['failed']} failed**"
    f"；失败文件集合 {sweep['fail_files'] or '[]'} 与本卡 3 个目标文件"
    f" **无交集（零引入校验通过）**",
    "i18n 缺口溯源：首次跑 translations 红，误判为「en 有 extReadinessBlocked、zh 缺」"
    "（实为 zh L1940 缩进 5 空格致正则漏检）；改用运行时 Object.keys 差集探针定位真因——"
    "zh 多出 3 个 `portfolioTrading.strategy.validatedFactorModel*` key 而 en 缺失（既有缺口）。"
    "已按中文语义补齐 en-US 3 条使门禁转绿（en-US.ts 在本卡 writes 内，合法写入）",
    "selfcheck ALL OK（写权限无冲突、依赖图无环）；开工/收工双登记完整",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS T27 -> done @", NOW)
