# -*- coding: utf-8 -*-
"""批次 6 收口：FactorEvaluationLab 3 次尝试未果 → 按纪律暂停；全量保持稳定。"""
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"
NOW = "2026-09-19T20:05:00+08:00"

# 1) PROGRESS
ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
d2 = prog["debt"]["TD-FE-RED-2"]
d2["rewrite_progress"]["batch6_paused"] = {
    "at": NOW,
    "result": "全量 **807 passed / 0 failed / 5 skipped，exit 0**（保持稳定）",
    "paused": [
        "FactorEvaluationLab.test.tsx 2 项 —— **3 次重写尝试均未完成**（覆盖率渲染路径 + "
        "轮询 timer 管理），按「三次失败则暂停」纪律止损；建议在专门会话处理："
        "①先单独诊断该文件的轮询 useEffect 依赖（为何 timer 不清理导致 vitest 挂起）；"
        "②再确认覆盖率数值的实际渲染路径（页面无 `98.29%` 文本）",
        "FactorModelSettings.test.tsx 3 项 —— 未开始（错误定位已有：找不到 ridge-20260714 / "
        "无法定位运行流水线按钮 / 仓库初始化 spy 0 次）",
    ],
}
d2["updated_at"] = NOW
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS 已登记 batch6_paused")

# 2) memory 日志
mem = ROOT / ".workbuddy/memory/2026-09-19.md"
mem.open("a", encoding="utf-8").write("""

---

## C 类重写 · 批次 6（20:05）—— FactorEvaluationLab 第 3 次尝试未果，按纪律暂停

- **FactorEvaluationLab 2 项**：3 次重写尝试均未完成（①覆盖率渲染路径——页面无 98.29% 文本；
  ②轮询 timer 管理——vitest 挂起/JSON 不产出）。按「三次失败则暂停」止损，恢复 skip + TODO。
  下批建议：先单独诊断该文件轮询 useEffect 的依赖数组（为何 timer 不清理），
  再确认覆盖率数值的实际渲染路径（该组件渲染为硬编码，非 t() 包裹）。
- **全量保持稳定：807 passed / 0 failed / 5 skipped，exit 0** ✓（failed 恒为 0）
- 剩余 5 项：FactorEvaluationLab 2（已 3 次尝试，需专门会话）+ FactorModelSettings 3（未开始，错误定位已有）。

### 会话级总结
| 项 | 值 |
|---|---|
| 排期 | **47/47（100%）** |
| 前端全量 | **807 passed / 0 failed / 5 skipped，exit 0** |
| C 类重写 | **17/22（77%）**：批次1=3、批次2=12、批次3=1、批次4=1；余 5 项 skip + TODO |
| 关键纪律 | 每批全量验「failed 恒为 0 + skipped 单调减少」；3 次失败即暂停 |
""")
print("memory 已追加")
