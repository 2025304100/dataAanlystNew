# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加开发任务计划文档生成记录。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## 生成《开发任务及计划》文档（21:55）

基于已复核修正的验收报告 + 项目现状（后端 1090 / 前端 807 全绿、排期 47/47），
生成 `docs/因子挖掘系统-开发任务计划-2026-09-19.md`：

- **15 张卡、4 个阶段**：
  - 阶段 0：D0 git 提交固化（立即）；
  - 阶段 A（P0 关键路径）：A1 evaluate_short 真实评估(L) → A2 GA 阶段链接入 worker(M) →
    A3 核心 API 19 处占位清零(M) → A4 前端提交接线(M) → A5 门禁 G8 真实闭环 E2E(M)；
  - 阶段 B（P1）：B1 分级 4 列(S) → B2 统计/分级落库(M) → B3 F1 内生链路+列表归档(M) →
    B4 drafts/templates/cleanup(L，可拆 3 卡)；
  - 阶段 C（P2）：C1 结果页跳转(S)、C2 缺壳页面(M)、C3 文档一致性(S)、C4 测试重写收尾(M)、C5 告警清理(S)。
- **关键路径**：A1→A2→A3→A4→A5（A1 是全链第一断点）；
- **三个新门禁**：G8=真实挖掘闭环（M1 达成）、G9=M2 落库、G10=体验补齐；最终重跑功能验收翻案；
- 每卡含 writes/reads/DoD/not_do，沿用项目排期卡纪律（双登记、TDD、收工三连、
  前端 failed=0 + skipped 单调减少、迁移 InnoDB/drift、G1 延后至探针数据真实）。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended plan doc record ->", p.name)
