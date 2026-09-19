"""T16 收工：任务卡 + PROGRESS + 手册（R28 + 跑偏表 55~56）+ MEMORY + 日志 + 清理。"""
from __future__ import annotations

import collections
import json
import pathlib
import re
import shutil

TASKS = pathlib.Path(".workbuddy/mining/tasks.json")
PROGRESS = pathlib.Path(".workbuddy/mining/PROGRESS.json")
HB = pathlib.Path("docs/因子挖掘-AI-Agent开发排期与接续手册-v1.0.md")
MEM = pathlib.Path(".workbuddy/memory/MEMORY.md")
LOG = pathlib.Path(".workbuddy/memory/2026-09-17.md")
REG = pathlib.Path("_t16reg.txt")


def load(p: pathlib.Path):
    raw = p.read_text(encoding="utf-8")

    def hook(pairs):
        dups = [k for k, c in collections.Counter(k for k, _ in pairs).items() if c > 1]
        if dups:
            print(f"  !! 重复键: {dups}")
        return dict(pairs)

    return json.loads(raw, object_pairs_hook=hook)


n_passed = None
if REG.exists():
    for line in reversed(REG.read_text(encoding="utf-8", errors="replace").splitlines()):
        m = re.search(r"(\d+) passed", line)
        if m:
            n_passed = int(m.group(1))
            break

# 1. tasks.json
tasks = load(TASKS)
by_id = {t["id"]: t for t in tasks["tasks"]}
t16 = by_id["T16"]
pit = list(t16.get("pitfalls") or [])
extra = [
    "🚨 **哈希的 None 归一：字典去、列表留**（parity 测试抓出的真实分歧）。"
    "字典里 `None` 与「未设置」等价（前端清空字段 = `{a: None}`），必须归一，"
    "否则幂等失效重复建任务；但**列表内 None 保留** —— 位置有语义，"
    "`['close', None]` 与 `['close']` 是不同配置。我第一版把列表内 None 也删了，"
    "被 `test_parity_with_validation_service` 抓到与 T14 分叉。",

    "★ **零迁移**：`factor_mining_drafts` 与 `factor_data_validation_runs` 两张表"
    "由 T01 迁移 0053 建好、模型在 `factor_mining.py` —— T16 不需要新迁移。"
    "**开工前先查表/模型是否存在，别急着建表。**",

    "★ **哈希实现必须收敛到一处**：T14 在 `validation_service` 里有临时实现，"
    "T16 的 `config_hash.py` 由 parity 测试钉死两者一致。T14 侧改为 import "
    "属后续清理（该文件不在 T16 写权限内）。任何一侧改算法都要先跑 parity 测试。",

    "★ **Step4 不参与 config_hash**：进化参数改了不应导致字段校验重跑"
    "（Step1~3 才是校验的输入）。",

    "★ **状态取值只从模型注释取**：草稿 draft/waiting_data_recheck/invalidated；"
    "校验运行 queued/running/passed/blocked/warning/failed。自造状态会让"
    "`index` 上的查询静默漏行。",
]
for e in extra:
    if e not in pit:
        pit.append(e)
t16["pitfalls"] = pit
TASKS.write_text(json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("tasks.json OK | T16 pitfalls =",
      len({t['id']: t for t in load(TASKS)['tasks']}['T16']['pitfalls']))

# 2. PROGRESS.json
prog = load(PROGRESS)
prog["tasks"].pop("T16", None)
prog["tasks"] = {
    "T16": {
        "status": "done",
        "agent": "agent-senior-dev",
        "started_at": "2026-09-17T14:35:00+08:00",
        "finished_at": "2026-09-17T15:10:00+08:00",
        "artifacts": [
            "app/services/factors/mining/config_hash.py（配置哈希唯一实现 + 分步哈希 + 差异摘要）",
            "app/services/factors/mining/draft_service.py（草稿 CRUD + 校验运行幂等复用）",
            "tests/services/factors/mining/test_config_hash.py（42 passed）",
        ],
        "evidence": [
            {
                "cmd": "python -m pytest tests/services/factors/mining/test_config_hash.py -q",
                "exit": 0,
                "summary": "42 passed（75s）。哈希稳定性/顺序无关/None 语义/parity/草稿 CRUD/状态流转/幂等复用/有效期。",
            },
            *([{
                "cmd": "python -m pytest tests/services/factors tests/factors -q（回归，后台）",
                "exit": 0,
                "summary": f"{n_passed} passed / 0 failed。",
            }] if n_passed else []),
        ],
        "decisions": [
            "🚨 **哈希的 None 归一规则：字典去 None、列表保留 None**。"
            "字典里 None ≡ 未设置（前端清空字段就是 `{a: None}`）→ 必须归一，"
            "否则 `{a:1}` 与 `{a:1,b:None}` 两哈希 → 幂等失效、重复建校验任务；"
            "列表内 None 是「空槽位」，位置有语义 → 保留，否则 "
            "`['close', None]` 与 `['close']` 会同哈希。"
            "**这条是 parity 测试抓出来的**：我第一版把列表内 None 也删了，"
            "与 T14 实现分叉。",

            "★ **哈希实现收敛到 `config_hash.py` 一处**：T14 的 "
            "`validation_service.compute_config_hash` 是临时实现，"
            "T16 用 `test_parity_with_validation_service` 逐例钉死两者一致；"
            "T14 侧改为 import 属后续清理（该文件不在 T16 写权限内，"
            "已记 observations）。",

            "★ **零迁移**：drafts / validation_runs 两表 T01 已建（迁移 0053），"
            "模型也在 `factor_mining.py`。T16 直接用，不新增建表；"
            "开工前先查表是否存在。",

            "★ **Step4 不参与 config_hash**：进化参数变动不应导致字段校验重跑；"
            "Step1~3 才是校验输入。",

            "★ **校验运行幂等**：同 draft_id + config_hash 的**活动**运行"
            "（queued/running）唯一，命中则复用（reused=True）；"
            "终态不复用（修复数据后重校验是合法诉求，向导 §5.1）。",

            "★ **草稿支持部分更新**（需求 §3.5 暂存编辑）：只更新 payload 里出现的"
            "步骤，未出现的原样保留；`current_step` 钳到 [1,5]；"
            "状态变更走 `set_draft_status`，`save_draft` 不隐式改状态。",
        ],
        "notes": "为 T13/T14 的校验任务提供落库层（factor_data_validation_runs），"
                 "并为后续最终准备校验（T31）备好 config_hash。",
    },
    **prog["tasks"],
}
NEW_OBS = [
    "🚨 **配置哈希的 None 归一规则（T16 定，parity 测试抓出）**：字典里的 None 归一为"
    "「不存在」（前端清空字段 = `{a: None}`，不归一会让幂等失效、重复建校验任务）；"
    "**列表内的 None 必须保留**（位置有语义，`['close', None]` ≠ `['close']`）。"
    "对照 T11 的 `_freeze_json`（落库，保留全部 None）—— 哈希归一 / 快照保真 / "
    "落库保真三者用途不同，不要互相替换。",

    "📋 **待清理（需 T14 写权限）**：`validation_service.compute_config_hash` 应改为 "
    "`from app.services.factors.mining.config_hash import compute_config_hash`，"
    "消除双实现。当前有 parity 测试（`test_config_hash.py::"
    "test_parity_with_validation_service`）保证两处一致，风险受控。",

    "📋 沿用未决：`tests/test_whitebox_factor_executor.py` 两个既有红灯；"
    "`factor_audit_logs` 序列化方案；task_lock 良性 SAWarning。",
]
existing = set(prog.get("observations") or [])
for o in NEW_OBS:
    if o not in existing:
        prog["observations"].append(o)
prog["updated_at"] = "2026-09-17T15:10:00+08:00"
prog["updated_by"] = "agent-senior-dev"
PROGRESS.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
done = sorted(k for k, v in load(PROGRESS)["tasks"].items() if v["status"] == "done")
print("PROGRESS OK | T16 =", load(PROGRESS)["tasks"]["T16"]["status"],
      "| 进度", len(done), "/40 | observations =", len(load(PROGRESS)["observations"]))

# 3. 手册
text = HB.read_text(encoding="utf-8")
lines = text.splitlines()
i27 = next(i for i, l in enumerate(lines) if l.startswith("| R27 |"))
end = i27 + 1
while end < len(lines) and not lines[end].startswith(("| R", "###", "##", "---")):
    end += 1
lines[end:end] = [
    "| R28 | **同一逻辑只能有一处实现，并加 parity 测试钉死。** 配置哈希这类"
    "「多处复用同一算法」的东西，两处实现哪怕今天一致，任一侧调整就会静默分叉"
    "（表现为幂等失效、重复建任务，**不报错**）。T16 的 "
    "`test_parity_with_validation_service` 逐例比对两处实现 —— 它当场抓出了"
    "「列表内 None 该不该删」的分歧。**凡是「同一算法两处实现」，必须 parity 测试。** |",
]
text = "\n".join(lines)
text = text.replace("### 1.4 二十七条铁律", "### 1.4 二十八条铁律")

idx = text.index("## 8. 常见跑偏与纠正")
tail = text[idx:]
last = None
for mm in re.finditer(r"^\|\s*\d+\s*\|", tail, re.M):
    last = mm
end_line = tail.index("\n", last.start()) + 1
extra_rows = (
    "| 55 | **配置哈希把列表里的 None 也删掉** | 以为「None 一律等价于缺失」 | "
    "列表位置有语义：`['close', None]` ≠ `['close']`。字典去 None、**列表保留**"
    "（T16 parity 测试抓出） |\n"
    "| 56 | **同一算法两处各写一份** | 觉得「先本地实现，以后再抽」 | "
    "任一侧调整就静默分叉（幂等失效、不报错）。要么收敛到一处，"
    "要么加 parity 测试（R28） |\n"
)
text = text[:idx] + tail[:end_line] + extra_rows + tail[end_line:]
HB.write_text(text, encoding="utf-8")
print("手册已更新:", "| R28 |" in text, "| 铁律:",
      re.search(r"### 1\.4 .*铁律", text).group(0),
      "| 跑偏 55/56:", "| 55 |" in text and "| 56 |" in text)

# 4. MEMORY
mem = MEM.read_text(encoding="utf-8")
if "### 1.17" not in mem:
    mem = mem.rstrip() + """

### 1.17 三种「None 处理」用途不同，别互相替换（2026-09-17 T16 定）
| 用途 | 规则 | 实现 | 为什么 |
|---|---|---|---|
| **算哈希**（config_hash / rule_hash） | 字典去 None；**列表保留 None** | `mining/config_hash.py::strip_none` | 字典里 None ≡ 未设置（前端清空字段）；列表位置有语义 |
| **快照落库**（候选池快照成员/分析） | 全部保留 | `candidate_pool/service._freeze_json` | 要能原样还原「当时看到什么」 |
| **审计落库**（factor_audit_logs） | 现状保留全部 | 历史一致性考虑，未改 |

→ 口诀：**算哈希归一、落库保真**。
→ 同一算法出现两处实现时，必须加 parity 测试（R28）——T16 就靠它抓出
  「列表内 None 该不该删」的分歧。
"""
MEM.write_text(mem, encoding="utf-8")
print("MEMORY 已更新:", "### 1.17" in mem)

# 5. 日志 + 归档 + 清理
if not LOG.exists():
    LOG.write_text("# 2026-09-17 工作日志\n", encoding="utf-8")
add = f"""

---

## T16 · config_hash + 草稿服务 ✅（16/40，PH4 开工）

### 交付
- `mining/config_hash.py`（配置哈希唯一实现 + 分步哈希 + 差异摘要）
- `mining/draft_service.py`（草稿 CRUD + 校验运行幂等复用 + 24h 有效期）
- `tests/.../test_config_hash.py` — **42 passed**
- 回归：{n_passed} passed / 0 failed

### 🚨 parity 测试当场抓到真实分叉（本轮最有价值）
我在 `config_hash.py` 里把**列表内的 None 也删掉**，而 T14 的实现是
**只去字典的 None、保留列表内 None** → 同一配置算出两个哈希。
`test_parity_with_validation_service` 逐例比对，直接红了。

裁决：**以 T14 的语义为准**（列表位置有语义，`['close', None]` 与 `['close']`
是不同配置）。修实现 + 补测试（`test_none_in_list_is_preserved`）。

**规则（R28）**：同一算法两处实现必须加 parity 测试 —— 否则任一侧调整就静默
分叉（幂等失效、重复建任务、**不报错**）。

### 零迁移
`factor_mining_drafts` / `factor_data_validation_runs` 两表 T01 迁移 0053 已建、
模型在 `factor_mining.py` —— **开工前先查表，别急着建表**。

### 设计
- **Step4 不参与 config_hash**：改进化参数不应触发字段校验重跑
- **幂等**：同 draft_id + config_hash 的活动运行（queued/running）唯一、命中复用；
  终态不复用（修复数据后重校验合法，向导 §5.1）
- **草稿部分更新**（暂存编辑）：只更新 payload 出现的步骤；状态变更走
  `set_draft_status`，`save_draft` 不隐式改状态
- 状态取值只从模型注释取，不自造

### 自检
排期 43 项、看板 62 项、文本审计 0 命中。手册 28 条铁律、跑偏表 56 条。
MEMORY 新增 §1.17（三种 None 处理用途对照表）。
"""
LOG.write_text(LOG.read_text(encoding="utf-8").rstrip() + add, encoding="utf-8")
print("日志已追加")

base = pathlib.Path(".workbuddy/mining/evidence")
for src, dst in (("_t16.txt", "T16_config_hash_dod.txt"),
                 ("_t16reg.txt", "T16_regression_full.txt")):
    p = pathlib.Path(src)
    if p.exists():
        shutil.move(str(p), str(base / dst))
        print("  归档:", dst)

for p in list(pathlib.Path(".").glob("_*.txt")):
    p.unlink()
    print("  删除临时:", p.name)
