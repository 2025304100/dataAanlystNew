"""T11 工作日志 + 清理。"""
from __future__ import annotations

import json
import pathlib
import shutil

LOG = pathlib.Path(".workbuddy/memory/2026-09-16.md")
add = """

---

## T11 · 看板分析 + 冻结快照 + 锁定/重置 ✅（11/40，**PH2 收尾 + G2 门禁**）

### 交付
- `candidate_pool/analysis.py`（分析纯引擎 + 全市场面板装载 + analyze_pool 全流程）
- `candidate_pool/service.py`（快照 freeze/mark_analyzed/reset + `_freeze_json`）
- `api/routes/mining_candidate_pool.py`（+4 条快照端点：snapshot/latest/snapshots/reset）
- `tests/.../test_e2e.py` — **25 passed（55s）**
- 回归：`tests/services/factors` + `tests/factors` = **645 passed / 0 failed（374s）**

### 🚨 本轮最大发现：`canonical_json` 会**静默丢弃所有 None 值**
实测 `{'a': None}` → `{}`（0/False/空串保留）。
- **哈希场景是合理设计**：None 与缺失等价，`execution_plan_hash`/`rule_hash` 都靠它
- **存储场景是数据丢失**：快照要原样还原「当时看到了什么」

实际踩中：冻结成员的行业列全 None，`industry` key 被整个吃掉——
下游拿不到「字段存在但无值」的信号。

**修复**：新增 `service._freeze_json`（json.dumps + sort_keys，保留 None），
members_json / stats_json / analysis_json 三处全部切换；
哈希场景继续用 canonical_json。**口诀：算哈希用 canonical_json，存数据用 _freeze_json**。
影响面已逐表排查（filter_config_json 行为等价；审计保持现状待裁决），已记 observations。

### 🚨 我违反了自己的隔离模型（诚实记录）
用真实 MySQL 做冒烟，往 `symbols` 写了 **61 行测试数据**（T10000~T1060）+ 1 个测试池。
已用专门脚本清理干净：symbols 7700 → **7639**、pools 1 → 0、零残留，
报告存 `evidence/T11_master_data_cleanup.json`（审计日志保留——审计是历史事实）。
**候选池域「不回写主数据」的隔离模型，对测试同样成立。** 已写进手册跑偏表 #39。

### 分析设计要点
- **锁定发生在「分析完成」，不是「冻结」**（向导 §3.7.1）。冻结只写成员；
  `mark_analyzed` 才置锁。锁定在快照上，删快照即自动解锁。
- **`analyze_pool` 幂等复用**「未分析且未锁定」的快照——否则按钮重复点击堆空快照。
- **风格暴露用全市场做基准**：池内 z-score 均值恒为 0，无信息量。
  改为「池内成员的全市场分位数均值」（0~1），需装载全市场面板
  （~5,500 只 × 25 日 ≈ 14 万行，毫秒级，符合 §3.7.6 的 2~15 秒同步预算）。
- **不可用维度如实标注**：成长依赖 `net_profit_yoy`（全 NULL）→ 5 维雷达只报 4 维。
  填 0 会给 AI「该池毫无成长性」的假信号，直接污染 Prompt。
- **市值分档两套口径并存**：看板用 §3.7.4 固定金额（描述性统计），T09 预设用分位数（筛选档位）。
  用途不同，刻意不「统一」。
- **reset 只删最新快照**：历史快照是已挖掘任务的引用物，删了破坏溯源。

### 冒烟实测（内存面板）
动量主导 0.746（夹具成员池每日 +0.4% 漂移，符合构造）、成长 unavailable、
行业「未知/未披露」100%、dividend_yield 0% 标红、市场环境「震荡市/低波动」
→ 因子建议 反转★★★/波动率★★☆（与向导 §3.7.4 规则一致）。

### 自检现状
排期 43 项、看板 62 项、文本审计 0 命中，全绿。
手册新增铁律 **R23**、跑偏表 38~42；MEMORY 新增 §1.12（canonical_json 丢 None）。
"""
LOG.write_text(LOG.read_text(encoding="utf-8").rstrip() + add, encoding="utf-8")
print("日志已追加，行数:", len(LOG.read_text(encoding="utf-8").splitlines()))

# 归档 + 清理
base = pathlib.Path(".workbuddy/mining/evidence")
for src, dst in (("_s11.txt", "T11_analysis_smoke.txt"),
                 ("_ac.txt", "T11_canonical_json_impact.txt"),
                 ("_p11.txt", "T11_data_probe.txt")):
    p = pathlib.Path(src)
    if p.exists():
        shutil.move(str(p), str(base / dst))
        print("  归档:", dst)

d = json.loads(pathlib.Path(".workbuddy/mining/PROGRESS.json").read_text(encoding="utf-8"))
d["tasks"]["T11"]["evidence"].append({
    "cmd": "python .workbuddy/mining/_smoke_t11.py（归档：evidence/T11_analysis_smoke.txt）",
    "exit": 0,
    "summary": "内存面板冒烟：动量主导 0.746、成长如实标注、行业未知 100%、因子建议符合向导星级规则。",
})
d["tasks"]["T11"]["evidence"].append({
    "cmd": "回归 python -m pytest tests/services/factors tests/factors -q",
    "exit": 0,
    "summary": "645 passed / 0 failed（374s）。",
})
pathlib.Path(".workbuddy/mining/PROGRESS.json").write_text(
    json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("  PROGRESS evidence 已补链")

M = pathlib.Path(".workbuddy/mining")
for n in ("_record_t11.py", "_smoke_t11.py", "_probe_t11_data.py",
          "_audit_canonical.py", "_cleanup_t11.py"):
    p = M / n
    if p.exists():
        p.unlink()
        print("  删除一次性脚本:", n)
for p in list(pathlib.Path(".").glob("_*.txt")):
    p.unlink()
    print("  删除临时:", p.name)
