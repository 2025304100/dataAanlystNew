"""进度看板渲染自检。

用一份"有进度"的模拟账本渲染，验证 8 个段落全部正确输出，
再校验"空进度"与"全完成"两种边界不崩。

跑法：
  D:/ai_project/dataAanlystNew/.venv/Scripts/python.exe .workbuddy/mining/_selfcheck_progress_doc.py
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import update_progress_doc as g  # noqa: E402

REAL = json.loads((HERE / "PROGRESS.json").read_text(encoding="utf-8"))
TASKS = json.loads((HERE / "tasks.json").read_text(encoding="utf-8"))
IDX = {t["id"]: t for t in TASKS["tasks"]}

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def render(progress: dict) -> str:
    """注入模拟账本渲染，不写真实文件。"""
    orig = g.PROGRESS_FILE
    tmp = HERE / "_tmp_progress_for_render.json"
    tmp.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    g.PROGRESS_FILE = tmp
    try:
        return g.build(changelog_limit=5)
    finally:
        g.PROGRESS_FILE = orig
        tmp.unlink(missing_ok=True)


class DuplicateKeyError(ValueError):
    """JSON 对象里出现了重复键。"""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _reject_duplicate_keys(pairs):
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise DuplicateKeyError(key)
        seen.add(key)
    return dict(pairs)


def find_duplicate_key(path: pathlib.Path) -> str | None:
    """返回 JSON 文件里**重复出现的对象键**，没有则 None。

    ⚠️ **为什么必须用 raw text 而不是 json.loads 的结果**

    Python 的 json 解析对重复键**取后者、且不报错**；任何「读 JSON → 改 → 写回」
    的脚本还会顺手把重复键**静默去重**。合起来的后果是**数据无声丢失**。

    真实事故（2026-09-16）：T03 记录后面留了占位 `"T04": {"status": "pending"}`，
    T04 开工时又在 `tasks` 开头插入同名键 → 对象里出现两个 `"T04"`。
    Edit 更新的是开头那个（done），但解析时后面那个（pending）覆盖它，
    随后一个改 observations 的脚本把重复键去重 → **T04 的完成记录整体消失**，
    文件本身语法完全合法、看不出任何异常。

    因此：**任何改动 JSON 的脚本，写回之前都必须先跑本检测。**
    """
    try:
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except DuplicateKeyError as exc:
        return exc.key
    return None


# ══════════════════════════════════════════════════════════
# 场景 1：全 pending（**自建 fixture，不依赖真实账本状态**）
# ══════════════════════════════════════════════════════════
# ⚠️ 早期版本这里直接读真实 PROGRESS.json，于是「T01 完成后」本场景就必然失败。
#    测试不能依赖被外部修改的文件——必须自建 fixture。
print("=" * 70)
print("场景 1：全 pending（自建 fixture）")
print("=" * 70)
all_pending = copy.deepcopy(REAL)
all_pending["updated_by"] = "fixture"
all_pending["current_batch"] = "B1"
all_pending["tasks"] = {}
all_pending.pop("observations", None)
out = render(all_pending)
check("含总览段", "## 1. 总览" in out)
check("含门禁段", "## 2. 门禁状态" in out)
check("含阶段段", "## 3. 阶段进度" in out)
check("含明细段", "## 4. 任务明细" in out)
check("全 pending 时不输出「进行中」段", "## 5. 进行中" not in out)
check("全 pending 时不输出「阻塞项」段", "## 6. 阻塞项" not in out)
check("全 pending 时不输出「变更日志」段", "## 7. 变更日志" not in out)
check("提示尚未开工", "尚未开工" in out)
check(f"{len(TASKS['tasks'])} 个任务都出现", all(t["id"] in out for t in TASKS["tasks"]))
check("表头有「决策」列（接续关键）", "| 决策 |" in out)
# 引用块后必须留空行，否则 markdown 会把下一行并进引用块
check(
    "引用块后有空行（防并块渲染 bug）",
    "> 尚未开工。第一个任务请用 `next_task.py` 获取，不要自行挑选。\n\n**当前批次**" in out,
)

# ══════════════════════════════════════════════════════════
# 场景 2：混合状态（含 done / in_progress / blocked / observations）
# ══════════════════════════════════════════════════════════
print()
print("=" * 70)
print("场景 2：混合状态（done + in_progress + blocked + observations）")
print("=" * 70)

mixed = copy.deepcopy(REAL)
mixed["updated_by"] = "agent-alpha"
mixed["updated_at"] = "2026-09-20T18:30:00+08:00"
mixed["current_batch"] = "B5"
mixed["tasks"] = {
    "T01": {
        "status": "done",
        "agent": "agent-alpha",
        "started_at": "2026-09-17T09:00:00+08:00",
        "finished_at": "2026-09-17T11:20:00+08:00",
        "evidence": [
            "cmd: .venv/Scripts/python.exe -m alembic upgrade head",
            "exit: 0",
            "cmd: .venv/Scripts/python.exe -m alembic heads",
            "exit: 0 · 输出 wps_0023_051_factor_mining_core",
        ],
        "artifacts": ["alembic/versions/2026_09_16_0053_wps_0023_051_factor_mining_core.py"],
        "decisions": [
            "alembic head 确认仍为 wps_0023_050_factor_model_members，down_revision 未改动",
            "app/models/__init__.py 未修改——依赖 _auto_discover_models 自动注册",
        ],
        "notes": "迁移双向 + 幂等三次通过",
    },
    "T02": {
        "status": "done",
        "agent": "agent-beta",
        "started_at": "2026-09-17T11:30:00+08:00",
        "finished_at": "2026-09-17T14:05:00+08:00",
        "evidence": ["cmd: pytest tests/.../test_evaluation_adapter_split.py -q", "exit: 0 · 11 passed"],
        "artifacts": ["tests/services/factors/mining/test_evaluation_adapter_split.py"],
        "decisions": [
            "断言用真实 build_time_split 而非 stub",
            "反向断言：仅归一化 + 默认 min_validation_days=50 必须抛 insufficient_dates",
        ],
    },
    "T03": {
        "status": "in_progress",
        "agent": "agent-gamma",
        "started_at": "2026-09-18T09:00:00+08:00",
        "notes": "已完成 cs_rank/cs_zscore/cs_demean 三个算子，剩 cs_scale/cs_quantile/cs_winsorize；沙箱 ALLOWED_FUNCS 尚未同步",
    },
    "T07": {
        "status": "blocked",
        "agent": "agent-delta",
        "blocked_reason": "T03 占用 factor_compiler.py，T07 需改同一文件区域做字段注册校验",
        "needs": "等 T03 完成，或人工裁决 T07 是否可推迟到 T03 之后",
        "attempted": "已尝试避开公共区域，但两者都要动 FUNCTION_CATALOG 附近",
        "partial_artifacts": ["app/models/mining_candidate_pool.py"],
    },
}
mixed["observations"] = [
    "T03 执行时发现 formula_catalog.py:111 的 _TEMPLATES 只有 5 个模板，与需求 25 个不符——已在 tasks.json 体现，未自行修改",
    "仓库根目录 _dbg_*.py 有 100+ 个，疑似历史调试残留，建议人工清理",
]

out = render(mixed)
check("进行中段存在", "## 5. 进行中" in out)
check("进行中显示 agent 与 notes", "agent-gamma" in out and "已完成 cs_rank" in out)
check("进行中列出占用文件", "app/services/factors/factor_compiler.py" in out)
check("阻塞段存在", "## 6. 阻塞项" in out)
check("阻塞段显示原因与需要", "占用 factor_compiler.py" in out and "等 T03 完成" in out)
check("变更日志段存在", "## 7. 变更日志" in out)
check("变更日志含决策", "down_revision 未改动" in out)
# 注意：必须在「变更日志段内」比较，否则会命中前面「阶段进度」里先出现的 T01
changelog_seg = out.split("## 7. 变更日志")[1]
check(
    "变更日志按完成时间倒序（T02 14:05 在 T01 11:20 之前）",
    changelog_seg.find("`T02`") < changelog_seg.find("`T01`"),
    f"T02@{changelog_seg.find('`T02`')} T01@{changelog_seg.find('`T01`')}",
)
check("观察记录段存在", "## 8. 观察记录" in out)
check("观察记录内容正确", "_dbg_*.py" in out)
check(f"总览显示进度百分比（2/{len(TASKS['tasks'])}）", f"2/{len(TASKS['tasks'])}" in out)
check("门禁 G0 标记已通过", "✅ 已通过" in out)
check("门禁 G1 标记进行中（T03 在跑）", "🔄 进行中" in out)
check("阶段 PH0 显示 2/2", "PH0 · 校准与哨兵** — 2/2" in out)
check("当前批次被展示", "B5" in out)

# ══════════════════════════════════════════════════════════
# 场景 3：全部完成
# ══════════════════════════════════════════════════════════
print()
print("=" * 70)
print("场景 3：全部完成")
print("=" * 70)
all_done = copy.deepcopy(REAL)
all_done["tasks"] = {t["id"]: {"status": "done", "finished_at": "2026-10-01T00:00:00+08:00"}
                     for t in TASKS["tasks"]}
out = render(all_done)
_total_tasks = len(TASKS["tasks"])
check(f"显示 {_total_tasks}/{_total_tasks}", f"{_total_tasks}/{_total_tasks}" in out)
check("显示全部完成", "全部任务已完成" in out)
check("门禁全部通过", out.count("✅ 已通过") == len(TASKS["gates"]))
check("无进行中段", "## 5. 进行中" not in out)
check("无阻塞段", "## 6. 阻塞项" not in out)

# ══════════════════════════════════════════════════════════
# 场景 4：容错 —— 字段缺失不应崩
# ══════════════════════════════════════════════════════════
print()
print("=" * 70)
print("场景 4：容错（字段缺失 / 未知状态）")
print("=" * 70)
noisy = copy.deepcopy(REAL)
noisy["tasks"] = {
    "T01": {"status": "done"},                     # 缺 evidence/decisions/agent/时间
    "T02": {"status": "莫名其妙的取值"},             # 非法状态
    "T03": {"status": "in_progress"},              # 缺 agent/notes
    "T04": {"status": "blocked"},                  # 缺 blocked_reason/needs
}
noisy.pop("observations", None)                    # 缺 observations
out = render(noisy)
check("非法状态降级为未开始", "⬜ 未开始" in out)
check("缺字段不崩且占位为 —", "—" in out)
check("缺 observations 不崩", "## 8. 观察记录" not in out)

# ══════════════════════════════════════════════════════════
# 场景 5：已裁决项（global_decisions）
# 决策必须出现在人看的看板上，否则后续 Agent/人会重新讨论已定的事 → 漂移
# ══════════════════════════════════════════════════════════
print()
print("=" * 70)
print("场景 5：已裁决项（global_decisions 段）")
print("=" * 70)

# 5a. 无该字段 → 不渲染该段（且不崩）
without_decisions = copy.deepcopy(REAL)
without_decisions.pop("global_decisions", None)
out = render(without_decisions)
check("无 global_decisions 时不输出 §9", "## 9. 已裁决项" not in out)
check("无 global_decisions 时头部无提示", "已裁决项" not in out.split("## 1.")[0])

# 5b. 空列表 → 同样不渲染
empty_decisions = copy.deepcopy(REAL)
empty_decisions["global_decisions"] = []
out = render(empty_decisions)
check("global_decisions 为空时不输出 §9", "## 9. 已裁决项" not in out)

# 5c. 有内容 → 渲染表格 + 遵守要求 + 头部提示
with_decisions = copy.deepcopy(REAL)
with_decisions["global_decisions"] = copy.deepcopy(REAL.get("global_decisions") or [])
check(
    "真实账本已登记 global_decisions",
    len(with_decisions["global_decisions"]) >= 4,
    f"实得 {len(with_decisions['global_decisions'])} 项",
)
out = render(with_decisions)
check("有 global_decisions 时输出 §9", "## 9. 已裁决项" in out)
check("§9 标题含「不要再重开讨论」", "不要再重开讨论" in out)
check("§9 含「先提出新证据」约束", "必须先提出新证据" in out)
check("§9 渲染全部裁决 ID", all(
    f"**{d['id']}**" in out for d in with_decisions["global_decisions"]
))
check("§9 渲染全部「遵守要求」", all(
    f"**{d['id']} 遵守要求**" in out for d in with_decisions["global_decisions"]
))
# 头部提示按**实际条数**渲染 —— 这里必须动态推导，
# 写死 "已裁决项 4 项" 会在新增裁决（D-E~D-H → 8 项）时误报
check("头部有已裁决项条数提示",
      f"已裁决项 {len(with_decisions['global_decisions'])} 项" in out)
check("头部提示指向 §9", "见 §9" in out)

# 5d. 容错：条目缺字段不崩
tolerant = copy.deepcopy(REAL)
tolerant["global_decisions"] = [
    {"id": "D-X"},                       # 只剩 id
    {},                                  # 全空
]
out = render(tolerant)
check("已裁决项缺字段不崩", "## 9. 已裁决项" in out and "**D-X**" in out)

print()
print("=" * 70)
print("场景 0：真实账本当前状态（只验证不崩 + 关键结构存在）")
print("=" * 70)
out_real = render(REAL)
check("真实账本可渲染", "## 1. 总览" in out_real and "## 4. 任务明细" in out_real)
check("真实账本含全部门禁", all(g in out_real for g in TASKS["gates"]))
check("真实账本含已裁决项段", "## 9. 已裁决项" in out_real)
done_n = sum(1 for t in TASKS["tasks"]
             if (REAL.get("tasks", {}).get(t["id"]) or {}).get("status") == "done")
print(f"       真实进度：{done_n}/{len(TASKS['tasks'])} done")

# ⚠️ 重复键检测（raw text）—— 防「数据无声丢失」
# 见 find_duplicate_key 的 docstring：2026-09-16 真实事故（T04 记录被占位覆盖并抹掉）
print()
print("=" * 70)
print("场景 6：JSON 重复键检测（防静默数据丢失）")
print("=" * 70)
for name in ("PROGRESS.json", "tasks.json"):
    path = HERE / name
    dup = find_duplicate_key(path)
    check(f"{name} 无重复对象键", dup is None, f"重复键：{dup!r}" if dup else "")

# 自检自身：确认检测器真的能抓到重复键（否则上面的 OK 是假绿）
_probe = HERE / "_tmp_duplicate_probe.json"
_probe.write_text('{"a": {"x": 1, "x": 2}, "b": 3}', encoding="utf-8")
try:
    check("检测器能抓到嵌套重复键", find_duplicate_key(_probe) == "x",
          f"实得 {find_duplicate_key(_probe)!r}")
finally:
    _probe.unlink(missing_ok=True)

# ══════════════════════════════════════════════════════════
# 场景 7：evidence / decisions 的**两种写法**都要能渲染
# 见 ev_text 的 docstring：2026-09-16 真实事故（T06 首次用 dict 写 evidence，
# 生成器直接对元素调 .replace() → 看板渲染崩溃）
# ══════════════════════════════════════════════════════════
print()
print("=" * 70)
print("场景 7：evidence / decisions 支持 str 与 dict 两种写法")
print("=" * 70)
_ev = copy.deepcopy(REAL)
_ev["tasks"] = {
    "T01": {
        "status": "done",
        "agent": "agent-dict",
        "finished_at": "2026-09-16T17:55:00+08:00",
        "evidence": [
            {"cmd": "pytest tests/.../test_template_compile_rate.py -q -s", "exit": 0,
             "summary": "26 passed；编译率 100%"},
            {"cmd": "pytest tests/factors -q", "exit": 0, "summary": "197 passed"},
        ],
        "decisions": ["dict 形式必须能渲染", "str 形式也必须能渲染"],
    },
    "T02": {
        "status": "done",
        "agent": "agent-str",
        "finished_at": "2026-09-16T15:45:00+08:00",
        "evidence": ["cmd: pytest -q", "exit: 0 · 93 passed"],
        "decisions": ["旧的字符串形式不能被破坏"],
    },
    "T03": {
        "status": "done",
        "agent": "agent-mixed",
        "finished_at": "2026-09-16T16:30:00+08:00",
        "evidence": [
            "字符串与字典混用",
            {"cmd": "pytest -q", "exit": 0},
            {"summary": "只有 summary 的字典"},
            {"cmd": "只有 cmd 的字典"},
        ],
        "decisions": [],
    },
}
_ev["observations"] = []
_ev_out = render(_ev)
check("dict 形式 evidence 可渲染", "编译率 100%" in _ev_out)
check("string 形式 evidence 未被破坏", "93 passed" in _ev_out)
check("dict evidence 带出 exit 码", "exit=0" in _ev_out)
check("混合形态不崩且都出现", "字符串与字典混用" in _ev_out and "只有 summary 的字典" in _ev_out)
check("缺 summary 的 dict 只留 cmd", "只有 cmd 的字典" in _ev_out)
check("空 decisions 降级为占位", "—" in _ev_out)

# 直接单测规整函数本身（快且定位准）
check("ev_text(str) 原样返回", g.ev_text("abc") == "abc")
check("ev_text(dict) 含 cmd 与 exit", g.ev_text({"cmd": "x", "exit": 0}) == "`x` exit=0")
check("ev_text(空 dict) 不抛异常", isinstance(g.ev_text({}), str))
check("ev_text(None) 不抛异常", isinstance(g.ev_text(None), str))

print()
print("=" * 70)
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL OK —— 进度看板 7 个场景（初始/混合/全完成/容错/已裁决项/重复键/证据格式）渲染全部正确。")
