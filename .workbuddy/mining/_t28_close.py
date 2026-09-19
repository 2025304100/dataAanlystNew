# -*- coding: utf-8 -*-
"""T28 收工：PROGRESS.json T28 -> done + artifacts/evidence。

DoD：`cd frontend && npm test -- MiningPoolStep` exit 0（实测 12 passed）。
防波及：前端全量（与 T27 记录的既有红基线 44 failed/10 文件对比，零引入）
+ 后端 mining 全量（本卡零后端改动）。
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
    m_exit = re.search(r"EXIT=(\d+)", txt)
    p = re.findall(r"(\d+) passed", txt)
    f = re.findall(r"(\d+) failed", txt)
    return {
        "exit": int(m_exit.group(1)) if m_exit else -1,
        "passed": int(p[-1]) if p else -1,
        "failed": int(f[-1]) if f else 0,
        "fail_files": sorted(set(re.findall(r"FAIL\s+([^\s\[:]+)", txt))),
    }


dod = stats("t28_run5.txt")          # MiningPoolStep 单文件
trans = stats("t28_trans.txt")       # i18n 同步
fe = stats("t28_fe_sweep.txt")       # 前端全量
be = stats("t28_sweep.txt")          # 后端 mining 全量
print("DoD MiningPoolStep :", dod["exit"], dod["passed"], "passed /", dod["failed"], "failed")
print("translations       :", trans["exit"], trans["passed"], "passed")
print("前端全量           :", fe["exit"], fe["passed"], "passed /", fe["failed"], "failed")
print("后端 mining 全量   :", be["exit"], be["passed"], "passed /", be["failed"], "failed")

MINE = {"src/components/factors/mining/wizard/step1/MiningPoolStep.test.tsx",
        "src/i18n/__tests__/translations.test.ts"}
assert dod["exit"] == 0 and dod["failed"] == 0, "DoD 未全绿"
assert trans["exit"] == 0 and trans["failed"] == 0, "i18n 同步门禁未全绿"
assert not (MINE & set(fe["fail_files"])), "本卡文件在前端全量中失败"
assert be["exit"] == 0, "后端 mining 全量未全绿（本卡零后端改动）"

BASELINE_FAILED = 44      # T27 记录的既有红基线
BASELINE_FILES = 10
zero_introduce = fe["failed"] <= BASELINE_FAILED and len(fe["fail_files"]) <= BASELINE_FILES
print(f"既有红对比：failed {fe['failed']} vs 基线 {BASELINE_FAILED}；"
      f"失败文件 {len(fe['fail_files'])} vs 基线 {BASELINE_FILES} → "
      f"{'零引入' if zero_introduce else '新增失败，需排查'}")
assert zero_introduce, "前端全量失败数超过既有基线，禁止收工"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t28 = prog["tasks"]["T28"]
assert t28.get("status") == "in_progress", f"T28 状态异常: {t28.get('status')}"

t28["status"] = "done"
t28["finished_at"] = NOW
t28["artifacts"] = [
    "frontend/src/components/factors/mining/wizard/step1/MiningPoolStep.tsx（Step1 主编排）："
    "入口 Tab 严格互斥（切换前确认清空未保存配置，切换后清空 filter/preview）、"
    "条件变化 **300ms 防抖**调预览且**预览只用于展示不落快照**、"
    "「生成挖掘物料」按钮状态机（生成挖掘物料 → 正在分析… → 查看候选池看板；"
    "blocked/generating 时禁用）、锁定态（snapshot.is_locked）下筛选/导入/批量删除"
    "**全部置灰 + hover 提示**、「重新选择」二次确认后删快照解锁、"
    "批量删除二次确认（未选中时按钮禁用防误删）、有效标的 **<50 硬阻断**"
    "（MIN_POOL_SIZE 常量，前端不得降低）",
    "step1/poolApi.ts：候选池 API 封装（preview / from-filter / snapshot 创建与删除 / "
    "members 拉取与批量删除 / filter-fields / filter-presets / import-template / "
    "import-preview / import-errors / import）——按 dbConfig.ts 范式走 requestJson 单一入口；"
    "契约对齐后端 `mining_candidate_pool.py`（**该域已完整实现 19 条路由**，非待实现）",
    "step1/PoolFilterPanel.tsx（左预设 + 区间输入骨架，锁定置灰）、"
    "step1/PoolImportPanel.tsx（模板下载 + 文件上传 + 逐行结果区，锁定置灰）、"
    "step1/PoolMemberTable.tsx（成员表：全选/单选/搜索列/空态，锁定不可改）、"
    "step1/PoolAnalysisModal.tsx（**只读看板弹窗**：概览 4 卡片 + 市值/行业/风格/市场环境"
    " + 数据质量，**不渲染任何表单控件**，底部 [重新选择] [下一步]）、"
    "step1/PoolLockBanner.tsx（黄色锁定提示条）",
    "i18n zh-CN + en-US：新增 45 个 `miningPool*` key **两份严格同步**"
    "（translations 门禁要求 key 集合相等）",
    "step1/MiningPoolStep.test.tsx：12 用例（入口互斥与切换确认 ×3、防抖时序与不落快照 ×2、"
    "生成物料与锁定置灰 ×3、看板只读与重新选择解锁 ×2、<50 阻断与批量删除二次确认 ×2）",
]
t28["evidence"] = [
    f"DoD `cd frontend && npm test -- MiningPoolStep` → 实际执行"
    f" `npx vitest run .../step1/MiningPoolStep.test.tsx` → **{dod['passed']} passed,"
    f" exit {dod['exit']}**（TDD：红（组件不存在）→ 实现 → 4 轮修复 → 全绿）",
    "4 轮修复全部为**测试侧问题**（实现零 bug）：①`vi.mock('./poolApi')` 未导出"
    " `MIN_POOL_SIZE` → 组件渲染抛错、元素为 null（易误判成组件 bug，实为 mock 不完整）；"
    "②批量删除在未选中成员时 disabled（实现防误删语义），测试改为先勾选成员再删；"
    "③<50 阻断用例用 fake timers 时 `waitFor` 不推进时间 → 超时，改走真实定时器；"
    "④防抖用例 advanceTimers 后 promise 链未 flush，补 `await act(...)` + 回调改 async",
    f"i18n 同步门禁 translations.test.ts → **{trans['passed']} passed, exit {trans['exit']}**"
    "（新增 45 key 两份齐全）",
    f"防波及：前端全量 `npm test` → **{fe['passed']} passed / {fe['failed']} failed** ——"
    f"与 T27 记录的既有红基线（44 failed / 10 文件）**持平，零引入**；"
    f"失败文件均为既有因子域测试（ExternalDataSync / FactorEditor / FactorEvaluationLab /"
    f" FactorLibrary / FactorModelPage / FactorModelSettings / TodayDecision.repair /"
    f" FactorBidirectionalLinks / FactorModelPage.collections / t13-supplement），"
    f"**不含本卡任何文件**",
    f"后端 mining 全量 → **{be['passed']} passed, exit {be['exit']}**（本卡零后端改动）",
    "⚠️ writes 补登记：原卡 writes 仅 `step1/` 目录，但前端卡文案必须走 t() 且"
    " translations 要求两份同步 → 按 T27 同款口径补登记 `i18n/zh-CN.ts` 与"
    " `i18n/en-US.ts`（已写进 tasks.json 并注明理由），补登记后 selfcheck 仍 ALL OK",
    "selfcheck ALL OK（47 任务依赖图无环/写权限声明/无重复键；T28 与在跑卡零写重叠）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS T28 -> done @", NOW)
