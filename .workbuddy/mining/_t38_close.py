# -*- coding: utf-8 -*-
"""T38 收工：PROGRESS.json T38 -> done + artifacts/evidence。

DoD 是「决策文档存在且结论明确」（非 pytest），因此收工守卫改为：
  1. 决策文档存在且含结论/依据/触发条件三要素；
  2. 结论与落地一致：结论「不做」时 parallel.py 必须**未落地**（not_do）；
  3. 防波及全量（本卡零源码改动，应维持基线）。
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

# ── 1. DoD 守卫：决策文档三要素 ──────────────────────────────────────
doc = ROOT / "docs/G1并行决策记录.md"
assert doc.exists(), "决策文档缺失"
text = doc.read_text(encoding="utf-8")
for key in ("结论", "探针数据现状", "触发条件", "实施方案", "不做"):
    assert key in text, f"决策文档缺要素: {key}"
print("决策文档:", doc.relative_to(ROOT), f"({len(text)} 字符) 三要素齐备")

# ── 2. 结论与落地一致性 ──────────────────────────────────────────────
parallel = ROOT / "app/services/factors/mining/parallel.py"
conclusion_not_do = "本期不做" in text
if conclusion_not_do:
    assert not parallel.exists(), (
        "结论为「不做」但 parallel.py 已落地 —— 与 not_do 矛盾")
    print("一致性: 结论=不做，parallel.py 未落地 ✓（符合 not_do）")
else:
    assert parallel.exists(), "结论为「做」但 parallel.py 未落地"

# ── 3. 防波及 ────────────────────────────────────────────────────────
sweep = M / "t38_sweep.txt"
passed = failed = -1
exit_code = -1
if sweep.exists():
    raw = re.sub(r"\x1b\[[0-9;]*m", "", sweep.read_bytes().decode("utf-8-sig",
                                                                 errors="replace"))
    m_exit = re.search(r"EXIT=(\d+)", raw)
    exit_code = int(m_exit.group(1)) if m_exit else -1
    p = re.findall(r"(\d+) passed", raw)
    f = re.findall(r"(\d+) failed", raw)
    passed = int(p[-1]) if p else -1
    failed = int(f[-1]) if f else 0
print(f"防波及: {passed} passed / {failed} failed | exit {exit_code}")
assert exit_code == 0 and failed == 0, "防波及未全绿"

# ── 4. PROGRESS 落 done ──────────────────────────────────────────────
ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t38 = prog["tasks"]["T38"]
assert t38.get("status") == "in_progress", f"T38 状态异常: {t38.get('status')}"

t38["status"] = "done"
t38["finished_at"] = NOW
t38["artifacts"] = [
    "docs/G1并行决策记录.md（DoD 交付物）：**结论「本期不做 G1 并行实现」**——"
    "①探针数据现状（真实库 `factor_mining_generations` **表存在 0 行、0 个 run**，"
    "并给出根因：22 条挖掘路由仅 3 条已实现，主链路从未跑完一代，不是没埋点而是没跑过）；"
    "②可离线实测的风险项数据（spawn 冷启动 1199.8 ms、其中 import numpy+pandas 772 ms；"
    "池复用后边际往返仅 0.19 ms；传输 vs 计算：小 1.2× / 中 16.2× / 大 22.8×）；"
    "③六条判据对照（仅「理论可行性」1 条满足，缺工程必要性）；"
    "④**重开触发条件（量化门槛）**：主链路打通 + ≥1 run×≥3 代探针 + 可并行段占比≥60%"
    " + 净收益为正 + 瓶颈不在 db_write；"
    "⑤**实施方案蓝图**（因子级/子表达式级二选一、进程数 min(6,cpu-2)、内存自适应、"
    "池复用、主进程汇总写、三项 Windows 风险的具体规避）；⑥复现脚本路径",
    "测量脚本（可复现证据，落在 .workbuddy/mining/）：`_t38_probe_data.py`（只读查真实库"
    "探针现状）、`_t38_spawn_bench.py`（Windows spawn 进程开销实测）、"
    "`_t38_serialize_bench.py`（大数据序列化往返实测）",
]
t38["evidence"] = [
    "DoD：决策文档存在且结论明确（含「结论 / 探针数据分析 / 触发条件 / 实施方案」四要素，"
    "文档 8 节、结论段首句即为「本期不做 G1 并行实现」）",
    "探针数据分析（决定性事实）：真实库 `factor_mining_generations` **0 行 / 0 run**——"
    "G1 决策链「探针数据 → 定位瓶颈 → 选并行粒度 → 估加速比」在**第 1 环即断**，"
    "单代总耗时、六段分布、G2 命中率全部未知",
    "补充实测（证明「不是并行不划算」）：Windows spawn 冷启动 1199.8 ms"
    "（import numpy+pandas 占 772 ms），**池复用后边际往返仅 0.19 ms** → 启动成本"
    "可被摊薄、非否决项；传输 vs 计算：中量级 (250,500) 2.42 ms vs 39.26 ms（16.2×）、"
    "大量级 (1000,3000) 33.98 ms vs 773.91 ms（22.8×）→ **技术上划算**",
    "结论自洽性守卫：收工脚本断言「结论=不做 → parallel.py 必须未落地」，"
    "防止文档说不做、代码却实现了的自相矛盾（依据任务卡 not_do）",
    f"防波及：pytest tests/services/factors/mining/ 全量 → **{passed} passed / {failed} failed,"
    f" exit {exit_code}**（本卡**零源码改动**，仅新增决策文档与测量脚本，基线不受影响）",
    "selfcheck ALL OK（47 任务依赖图无环/写权限声明/无重复键；T38 无写冲突）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS T38 -> done @", NOW)
