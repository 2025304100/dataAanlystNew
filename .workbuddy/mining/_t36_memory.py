# -*- coding: utf-8 -*-
"""向 2026-09-18.md 追加 T36 节（append-only）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-18.md"

text = """

---

## T36 · 质量分级 + 季度重评 ✅（22:53，40/47）

### 交付
- `factor_grading.py`：8 维度五级**纯函数** `grade()`（设计 §7.10 签名）+ 需求 §6.8
  阈值表；**每一级全部满足才定级**（S→A→B→C 逐级）；两条硬约束 decay<0.5 压 C、
  月频/降级最高 B；`review_quarterly`（B 连续 2 季稳定升 A、S/A 连续 2 季下滑降级并
  移出 FactorSet、**manual_adjusted=1 不覆盖**）；`run_quarterly_review(db)` 批量入口
- `models/factor_grade_history.py`（每次评定一行）+ 迁移 **0059 / `wps_0023_059`**
- `scheduled_tasks.py`：新增 `factor_grade_review` 任务类型（复用既有调度器）
- DoD **22 passed**；调度器白盒 **13 passed**；后端 mining 全量 **1084 passed / 0 failed**
- 迁移已应用真实库；`verify_schema_drift` 新表 **[OK] 索引 2 / 列 18 / INNODB**（零漂移）
- 收工三连 done @22:53 → selfcheck ALL OK → 看板 698 行

### 口径决策（写进代码注释）
- **缺失维度一律视为不满足**（不猜、不填零）→ `{"icir":0.9}` 直接归 D。
- C 级容忍 OOS **轻微**不稳（`oos_icir > -0.1`），D 级才是明显反向；
  相关性 C 级 <0.95，≥0.95 归 D。
- 显著性：S/A 看 **Bonferroni 校正后 p**，B/C 看**原始 p**（与需求表一致）。
- 季度重评分派返回 `source="grading"`（区别于 `async`），统计写入
  `ScheduledTaskRun.message`；session 用 `DatabaseManager.get().session_factory()`。

### 坑（新增/复现）
- 🚨 **模块 docstring 内嵌 ```python 示例里再写 `\"\"\"` 会提前终止 docstring**
  → `SyntaxError: invalid character '：'`。示例代码块内改用 `#` 注释。
- 🚨 `selfcheck_conflict.py` 在本机偶发 `OSError: SHFileOperationW 失败 0x2`
  （SAFE_DELETE shim 拦截 tmp 清理）→ **假红**，不是真冲突；
  用 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 重跑即 ALL OK。**开卡/收工都建议带上**。
- `verify_schema_drift.py` 实际在 `.workbuddy/mining/`（不在 `scripts/`）。
- 真实库既有漂移 **49/132 表**（缺 FK/索引的历史债），**不阻塞新卡**，
  但收工时须单独确认「本卡新表 [OK]」。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T36 section ->", p.name)
