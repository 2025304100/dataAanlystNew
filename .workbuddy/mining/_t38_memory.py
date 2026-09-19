# -*- coding: utf-8 -*-
"""向 2026-09-18.md 追加 T38 节（append-only）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-18.md"

text = """

---

## T38 · G1 并行决策与实施 ✅（21:42，38/47）—— **结论：本期不做**

### 交付
- `docs/G1并行决策记录.md`（DoD 交付物，8 节）：结论 / 探针数据现状 / 离线实测 /
  判据对照 / 触发条件 / 实施方案蓝图 / 复现方式 / 一句话结论
- 测量脚本（可复现证据）：`_t38_probe_data.py`（只读查真实库）、`_t38_spawn_bench.py`、
  `_t38_serialize_bench.py`
- `parallel.py` **未落地**（与「不做」结论一致，符合任务卡 not_do）
- 防波及 1062 passed / 0 failed（本卡**零源码改动**）；收工三连 done @21:42

### 决策依据（关键事实，后续卡可直接引用）
1. **探针数据 0 行**：真实库 `factor_mining_generations` 表存在、**0 行 / 0 run**。
   根因不是没埋点（`performance_probe.GenerationProbe` 已实现且正确），
   而是**主链路从未跑完一代**（22 条挖掘路由仅 3 条已实现，批次创建等 M5/M7 未实现）。
2. **离线实测（证明"不是并行不划算"）**：
   - Windows spawn 冷启动 **1199.8 ms**（其中 `import numpy+pandas` **772 ms**）；
     但**池复用后边际往返仅 0.19 ms** → 启动成本可摊薄，**非否决项**。
   - 传输 vs 计算（2 进程池，20 次均值）：小(60,50) 0.21 vs 0.26 ms（**1.2× 不划算**）；
     中(250,500) 2.42 vs 39.26 ms（**16.2×**）；大(1000,3000) 33.98 vs 773.91 ms（**22.8×**）。
   - 判据：单任务纯计算 > 传输往返 × 进程数 才划算。
3. **六条判据仅满足 1 条**（只有"理论可行性"）——缺的是工程必要性。

### 为什么不做
设计 §7.4 要求「依据探针数据定位瓶颈后再决定并行哪一段」→ 决策链在第 1 环即断；
叠加三项 Windows 风险（无 ProcessPool 先例 / 无 fork / shared_memory resource_tracker
泄漏），在**尚未跑通的单进程链路**上引入并发，故障定位成本极高；
且若瓶颈其实在 `db_write`（受 DuckDB 单写锁约束）或 G2 命中率已很高，G1 方向需推翻重来。

### 重开门槛（写进文档 §五）
主链路打通 + `factor_mining_generations` ≥1 run × ≥3 代 + 可并行段（ast_eval/
subexpr_compute/metric_calc）合计占比 ≥60% + 净收益为正 + 瓶颈不在 db_write。

### 坑（新增）
- 🚨 Python 字符串里用 **ASCII 双引号包裹中文**（如 `仅"理论可行性"1 条`）会提前终止
  字符串 → SyntaxError。写长中文字符串一律用**中文引号「」**，写完先 `py_compile`。
  （本次连踩 3 处才修完，同文件串行 Edit 逐处修。）
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T38 section ->", p.name)
