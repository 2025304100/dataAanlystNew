# -*- coding: utf-8 -*-
"""批次 7 收口：FactorModelSettings 补 9 mock 但数据仍未送达 → 恢复 skip；全量稳定。"""
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"
NOW = "2026-09-19T21:20:00+08:00"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
d2 = prog["debt"]["TD-FE-RED-2"]
d2["rewrite_progress"]["batch7_partial"] = {
    "at": NOW,
    "result": "全量 **807 passed / 0 failed / 5 skipped，exit 0**（保持稳定）",
    "progress_on": ["FactorModelSettings.test.tsx 3 项（未修完，已恢复 skip）"],
    "what_changed": [
        "补 9 个组件调用的 scoring* mock（scoringActivateModel/CancelTask/CreateTask/"
        "FallbackToManual/GetModelRelations/GetPipelineEta/GetTask/InitializeWarehouse/UpdateSystemConfig）",
        "断言/注入方法名对齐：activateFactorModel → scoringActivateModel、"
        "createFactorPipelineTask → scoringCreateTask",
    ],
    "lessons": [
        "🚨 **动态插 mock 必须选对锚点**：曾把 9 个方法插进 `createFactorPipelineTask: "
        "vi.fn(async () => ({` 的返回对象内部（结构破坏），且 f-string 的 `{{}}` 转义出错——"
        "两次都已修复。**插入后必须 esbuild 预检 + 跑一次确认。**",
        "⚠️ 该文件补齐 mock 后 3 项仍失败：组件 loadData 的数据仍未送达（页面只有头部渲染）"
        "——需要追组件 catch 分支的实际错误（如 `factorSetLoader`、`scoringListTasks` 返回形状），"
        "建议下一会话从「在测试里打印组件 catch 到的 err」入手。",
    ],
    "remaining": {
        "FactorEvaluationLab.test.tsx": "2 项（覆盖率渲染路径 + 轮询 timer，已 3 次尝试，需专门会话）",
        "FactorModelSettings.test.tsx": "3 项（mock 已补齐，需追组件 loadData 错误分支）",
    },
}
d2["updated_at"] = NOW
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS 已登记 batch7_partial")

mem = ROOT / ".workbuddy/memory/2026-09-19.md"
mem.open("a", encoding="utf-8").write("""

---

## C 类重写 · 批次 7（21:20）—— FactorModelSettings 补齐 mock 但数据仍未送达，恢复 skip

- 补 9 个组件调用的 scoring* mock + 断言方法名对齐（activateFactorModel→scoringActivateModel、
  createFactorPipelineTask→scoringCreateTask）；过程中两次自伤（插进返回对象内部、f-string `{{}}`
  转义错）均已修复并 esbuild 预检通过。
- 但 3 项仍失败：页面只渲染头部，组件 loadData 的数据未送达 → 需追组件 catch 分支的实际错误
  （建议：测试里打印 catch 到的 err；重点查 factorSetLoader 与 scoringListTasks 的返回形状）。
- **全量保持稳定：807 passed / 0 failed / 5 skipped，exit 0** ✓
- 剩余 5 项均有「下一步从哪查」的明确指引（PROGRESS.debt.TD-FE-RED-2.rewrite_progress）。
""")
print("memory 已追加")
