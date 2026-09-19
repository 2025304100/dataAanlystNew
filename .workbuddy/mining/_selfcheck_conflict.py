"""写权限冲突检测的自检（验证 next_task.py 的核心机制）。

跑法：
  D:/ai_project/dataAanlystNew/.venv/Scripts/python.exe .workbuddy/mining/_selfcheck_conflict.py
"""
from __future__ import annotations

import copy
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import next_task as nt  # noqa: E402

TASKS_DOC = json.loads((HERE / "tasks.json").read_text(encoding="utf-8"))
BASE_PROG = json.loads((HERE / "PROGRESS.json").read_text(encoding="utf-8"))
IDX = nt.index_tasks(TASKS_DOC)
nt._HCF = TASKS_DOC.get("high_conflict_files") or {}

ALL_IDS = sorted(IDX)
failures: list[str] = []


def sim(in_progress=(), done=()):
    """模拟一个进度状态，返回 (runnable, wait_deps, wait_conflict)。"""
    prog = copy.deepcopy(BASE_PROG)
    prog["tasks"] = {d: {"status": "done"} for d in done}
    prog["tasks"].update(
        {r: {"status": "in_progress", "agent": "sim"} for r in in_progress}
    )
    runnable, wait_deps, wait_conflict = nt.candidates(TASKS_DOC, IDX, prog)
    return (
        [t["id"] for t in runnable],
        {t["id"]: m for t, m in wait_deps},
        {t["id"]: b for t, b in wait_conflict},
    )


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK  " if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


print("=" * 70)
print("A. paths_conflict 单元行为")
print("=" * 70)
cases = [
    ("a/b.py", "a/b.py", True, "完全相同"),
    ("a/b.py", "a/c.py", False, "同目录不同文件"),
    ("app/api/router.py", "app/api/routes/factor_mining.py", False, "同目录不同文件"),
    ("app/services/factors/mining/service.py", "app/services/factors/mining/ga.py", False, "同目录不同文件"),
    ("alembic/versions/", "alembic/versions/0054_x.py", True, "目录前缀关系"),
    ("frontend/src/components/", "frontend/src/components/Settings.tsx", True, "目录前缀关系"),
    ("alembic/versions/0054_*.py", "alembic/versions/0055_*.py", False, "通配前缀不同（靠 high_conflict 兜底）"),
]
for a, b, expected, note in cases:
    got = nt.paths_conflict(a, b)
    check(f"{a}  vs  {b}", got == expected, f"期望 {expected} 实得 {got} · {note}")

print()
print("=" * 70)
print("B. tasks_conflict（含 high_conflict_files 兜底）")
print("=" * 70)
t03, t05 = IDX["T03"], IDX["T05"]
check("T03 vs T05 冲突（共用 factor_compiler.py）", nt.tasks_conflict(t03, t05, TASKS_DOC))
t07, t15 = IDX["T07"], IDX["T15"]
check("T07 vs T15 冲突（共用 api/router.py）", nt.tasks_conflict(t07, t15, TASKS_DOC))
t01, t07 = IDX["T01"], IDX["T07"]
check("T01 vs T07 冲突（alembic/versions 目录，靠 high_conflict）", nt.tasks_conflict(t01, t07, TASKS_DOC))
t23, t25 = IDX["T23"], IDX["T25"]
check("T23 vs T25 冲突（共用 mining/service.py）", nt.tasks_conflict(t23, t25, TASKS_DOC))
t33, t34 = IDX["T33"], IDX["T34"]
check("T33 vs T34 不冲突（selection/ vs reproduction/）", not nt.tasks_conflict(t33, t34, TASKS_DOC))
t01, t27 = IDX["T01"], IDX["T27"]
check("T01 vs T27 不冲突（后端 vs 前端）", not nt.tasks_conflict(t01, t27, TASKS_DOC))

print()
print("=" * 70)
print("C. 真实调度场景（前置已满足，冲突应独立生效）")
print("=" * 70)

# C1: 初始状态 —— T01 与 T27 应可并行（批次 B2）
r, wd, wc = sim()
check("初始：T01 与 T27 同批可并行", set(r) >= {"T01", "T27"}, f"runnable={r}")

# C2: T01 在跑 —— T27 应仍可开工（后端/前端无交集）
r, wd, wc = sim(in_progress=["T01"])
check("T01 在跑时 T27 仍可开工", "T27" in r and not wc, f"runnable={r} 挡={wc}")

# C3: T07 在跑（写 api/router.py），T15 前置 T12 已满足 → 应被"写权限"挡住
#     （关键：这不是依赖挡住，是冲突挡住，证明冲突检测独立生效）
r, wd, wc = sim(in_progress=["T07"], done=["T12"])
check(
    "T07 在跑时 T15 被写权限挡住（而非依赖）",
    "T15" in wc and "T15" not in wd,
    f"挡={wc.get('T15')} deps_ok={'T15' not in wd}",
)

# C4: T03 在跑时 T04/T05/T06 被拦 —— 依赖与写权限双重保护
r, wd, wc = sim(in_progress=["T03"], done=["T01", "T02"])
blocked_ids = set(wd) | set(wc)
check(
    "T03 在跑时 T04/T05/T06 均不可开工",
    all(t in blocked_ids for t in ("T04", "T05", "T06")),
    f"deps挡={sorted(wd)} 冲突挡={sorted(wc)}",
)
check("T03 在跑时 T07/T27 仍可开工", {"T07", "T27"} <= set(r), f"runnable={r}")

# C5: 迁移串行 —— T07 在跑时 T26/T36 不可开工
r, wd, wc = sim(in_progress=["T07"], done=["T01", "T02", "T12", "T16", "T24", "T35"])
check(
    "T07 在跑时 T26/T36 均不可开工（alembic 必须串行）",
    all(t in (set(wd) | set(wc)) for t in ("T26", "T36")),
    f"deps挡={sorted(wd)} 冲突挡={sorted(wc)}",
)

# C6: i18n 串行 —— T27 在跑时 T32/T37/T40 不可开工
r, wd, wc = sim(in_progress=["T27"], done=["T25", "T26", "T31"])
check(
    "T27 在跑时 T32/T37/T40 不可开工（i18n 两份需串行）",
    all(t in (set(wd) | set(wc)) for t in ("T32", "T37", "T40")),
    f"deps挡={sorted(wd)} 冲突挡={sorted(wc)}",
)

# C7: 全绿后无任务可开工
r, wd, wc = sim(done=ALL_IDS)
check("全部 done 后无候选", not r and not wd and not wc, f"runnable={r}")

# C8: 每个任务的前置 ID 都存在（防笔误）
missing_deps = [
    (t["id"], d) for t in TASKS_DOC["tasks"] for d in t.get("deps", []) if d not in IDX
]
check("所有 deps 指向存在的任务 ID", not missing_deps, str(missing_deps))

# C9: 每个任务都有 DoD（没有可执行验收的任务不允许存在）
no_dod = [t["id"] for t in TASKS_DOC["tasks"] if not t.get("dod")]
check("所有任务都有 DoD 命令", not no_dod, str(no_dod))

# C10: 每个任务都声明了 writes
no_writes = [t["id"] for t in TASKS_DOC["tasks"] if not t.get("writes")]
check("所有任务都声明了写权限", not no_writes, str(no_writes))

# C11: 依赖图无环
def has_cycle() -> bool:
    color: dict[str, int] = {}

    def visit(n: str) -> bool:
        color[n] = 1
        for d in IDX[n].get("deps", []):
            if color.get(d) == 1:
                return True
            if color.get(d) is None and visit(d):
                return True
        color[n] = 2
        return False

    return any(color.get(n) is None and visit(n) for n in IDX)


check("依赖图无环", not has_cycle())

# C12: 粒度纪律 —— 单任务 writes <= 8（超了说明该拆，除非显式豁免）
over = [(t["id"], len(t["writes"])) for t in TASKS_DOC["tasks"]
        if len(t["writes"]) > 8 and not t.get("granularity_exempt")]
exempted = [(t["id"], len(t["writes"]), t.get("granularity_note", "")) for t in TASKS_DOC["tasks"]
            if t.get("granularity_exempt")]
check("无任务超出 8 个产物文件（粒度纪律）", not over, f"超出：{over}" if over else "")
for tid, n, note in exempted:
    print(f"       [豁免] {tid} 有 {n} 个产物 —— {note[:48]}…")
    check(f"豁免任务 {tid} 必须写明理由", bool(note.strip()))

# C13: reads 里引用的文档必须真实存在（防路径笔误）
REPO_ROOT = HERE.parent.parent
bad_docs: list[tuple[str, str]] = []
src_ref_count = 0
for t in TASKS_DOC["tasks"]:
    for ref in t.get("reads", []):
        path_part = ref.split("#")[0].strip()
        if not path_part:
            continue
        if ("开发需求文档" in ref or "开发文档-落地版" in ref or "实验向导" in ref):
            src_ref_count += 1
        target = REPO_ROOT / path_part
        if not target.exists():
            bad_docs.append((t["id"], ref))
check("reads 引用的文档路径全部存在", not bad_docs, f"失效：{bad_docs[:5]}" if bad_docs else "")

# C14: 每个任务都必须引用至少一份原始文档（需求/开发/向导）
no_src = [
    t["id"] for t in TASKS_DOC["tasks"]
    if not any(("开发需求文档" in r or "开发文档-落地版" in r or "实验向导" in r)
               for r in t.get("reads", []))
]
check("40/40 任务都引用了原始文档", not no_src, f"缺失：{no_src}" if no_src else "")
print(f"       原始文档引用共 {src_ref_count} 条")

# C15: source_docs 的优先级规则必须存在且完整
sd = TASKS_DOC.get("source_docs") or {}
check("source_docs.precedence 存在且 >=3 条", len(sd.get("precedence") or []) >= 3)
check("source_docs.docs 覆盖三份原始文档", len(sd.get("docs") or []) == 3,
      f"实得 {len(sd.get('docs') or [])}")
for d in sd.get("docs") or []:
    check(f"  {d['path'].split('/')[-1]} 有 role + known_errors",
          bool(d.get("role")) and isinstance(d.get("known_errors"), list))

# C16: 已知错误清单必须覆盖 8 处（防被误删）
total_known = sum(len(d.get("known_errors") or []) for d in (sd.get("docs") or []))
check("已知错误清单 >=8 处", total_known >= 8, f"实得 {total_known} 处")

# C17: DoD 命令里引用的测试文件必须已在该任务的 writes 里声明
#      否则① 任务卡自相矛盾（DoD 要求建文件、写权限却不含它）
#          ② 写权限冲突检测失效（T36‖T37 曾共用 test_grading.py 却判无冲突）
_DOD_TEST_PATH = re.compile(r"(tests/[\w/.\-]+\.py)")
undeclared: list[tuple[str, str]] = []
for t in TASKS_DOC["tasks"]:
    declared = set(t.get("writes") or [])
    for item in t.get("dod") or []:
        for m in _DOD_TEST_PATH.finditer(item.get("cmd", "")):
            f = m.group(1)
            if f not in declared:
                undeclared.append((t["id"], f))
check(
    "DoD 引用的测试文件均已声明进 writes",
    not undeclared,
    f"未声明：{undeclared[:6]}" if undeclared else "",
)

# C18: 被多个任务共用的测试文件必须登记进 high_conflict_files
_hcf = TASKS_DOC.get("high_conflict_files") or {}
_shared: dict[str, list[str]] = {}
for t in TASKS_DOC["tasks"]:
    for f in t.get("writes") or []:
        if f.startswith("tests/"):
            _shared.setdefault(f, []).append(t["id"])
_unregistered = {
    f: ids for f, ids in _shared.items() if len(ids) > 1 and f not in _hcf
}
check(
    "共享测试文件已登记为高冲突",
    not _unregistered,
    f"未登记：{_unregistered}" if _unregistered else "",
)
if _shared:
    _multi = {f: ids for f, ids in _shared.items() if len(ids) > 1}
    print(f"       tests/ 写权限共 {len(_shared)} 个文件，其中 {len(_multi)} 个被多任务共用")


# C19: tasks.json 不得有**重复对象键**
#      ⚠️ Python 的 json 对重复键取后者且**不报错**；「读→改→写回」还会静默去重。
#      2026-09-16 真实事故：PROGRESS.json 里同一任务出现两次，Edit 改的是前一个，
#      解析时后一个覆盖它，随后的脚本又把重复键去重 → 该任务的完成记录整体消失，
#      文件语法完全合法、看不出异常。故必须在任何写回之前先跑本检测。
class _DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _reject_duplicate_keys(pairs):
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateKeyError(key)
        seen.add(key)
    return dict(pairs)


def find_duplicate_key(path: pathlib.Path) -> str | None:
    try:
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeyError as exc:
        return exc.key
    return None


for _name in ("tasks.json", "PROGRESS.json"):
    _dup = find_duplicate_key(HERE / _name)
    check(f"{_name} 无重复对象键", _dup is None, f"重复键：{_dup!r}" if _dup else "")

# 自检自身：确认检测器能抓到（否则上面的 OK 是假绿）
_probe = HERE / "_tmp_dup_probe.json"
_probe.write_text('{"s": {"k": 1, "k": 2}}', encoding="utf-8")
try:
    check("重复键检测器可用", find_duplicate_key(_probe) == "k")
finally:
    _probe.unlink(missing_ok=True)

# ══════════════════════════════════════════════════════════════
# C20 / C21：一类反复出现的「次生缺口」防回归
# 2026-09-16 T07 期发现，全部为真实缺口，不是假想：
#   - T08 / T25 新建路由文件却没声明 app/api/router.py 写权限
#   - T26 建 4 张表却没声明迁移写权限
#   - T07 / T36 的 alembic 写权限写 `0054_*.py`，匹配不上真实文件名
#     `2026_09_16_0054_wps_...py`（约定是 YYYY_MM_DD_<编号>_...）
# ══════════════════════════════════════════════════════════════
print()

_ROUTER = "app/api/router.py"


def _is_new_route_module(path: str) -> bool:
    """该写权限是不是「**新建**一个路由模块」（而不是给已有模块追加端点）。

    判据：文件在磁盘上**尚不存在** → 属新建，必须同时声明 app/api/router.py 去注册。
    已存在的模块（如 T09/T10/T11 往 T08 建好的 `routes/mining_candidate_pool.py` 追加端点）
    **不需要** router.py 的写权限，因为注册早已由建它的任务完成。

    ⚠️ 这条判据是 2026-09-16 T08 收工时补的：本检查**第一版**是「只要 writes 里有
    `app/api/routes/` 就要求写权限含 router.py」，于是 T09/T10/T11 全部被误报。
    误报的门禁比没有门禁更差（会训练人忽略它，见 MEMORY §5.5），故改为按「是否新建」判定。
    """
    if "*" in path:
        return False  # 通配声明无法判定，跳过而不是误报
    return not (REPO_ROOT / path).exists()


_route_gap = [
    (t["id"], w)
    for t in TASKS_DOC["tasks"]
    for w in (t.get("writes") or [])
    if w.startswith("app/api/routes/")
    and _is_new_route_module(w)
    and _ROUTER not in (t.get("writes") or [])
]
check(
    "**新建**路由模块的任务都声明了 app/api/router.py 写权限",
    not _route_gap,
    f"缺声明：{_route_gap}（新建路由模块必须在 app/api/router.py 加 include_router，"
    f"否则接口 404 且不报错；给已存在模块追加端点不需要）" if _route_gap else "",
)

# alembic 写权限：编号必须被两侧下划线界定（`_00NN_`）
# 精确规则的理由：文件名约定是 `YYYY_MM_DD_<编号>_wps_<rev>.py`。
#   ✅ `alembic/versions/2026_09_16_0054_wps_...py`  → 含 `_0054_`
#   ✅ `alembic/versions/2026_*_0055_*.py`          → 含 `_0055_`（glob 只吃掉日期段）
#   ❌ `alembic/versions/0054_*.py`                 → 不含 `_0054_`（开头直接是编号）
# 为什么不能用「前缀正则」：glob 中间带 `*` 时，`split('*')[0]` 只能取到
# `alembic/versions/2026_`，压根看不到编号 —— 那样的检查会误报（本检查第一版就是这么错的）。
_ALEMBIC_NUM = re.compile(r"_00\d{2}_")
_bad_glob: list[tuple[str, str]] = []
for _t in TASKS_DOC["tasks"]:
    for _w in (_t.get("writes") or []):
        if not _w.startswith("alembic/versions/"):
            continue
        if not _ALEMBIC_NUM.search(_w):
            _bad_glob.append((_t["id"], _w))
check(
    "alembic 写权限里的迁移编号被下划线界定（`_00NN_`）",
    not _bad_glob,
    f"不匹配：{_bad_glob}（应写成 alembic/versions/2026_*_00NN_*.py 或精确文件名；"
    f"`00NN_*.py` 这种形态匹配不上真实文件名，会让越权判断失真）" if _bad_glob else "",
)

# 迁移编号不得重复分配
_numbers: dict[str, list[str]] = {}
for _t in TASKS_DOC["tasks"]:
    for _w in (_t.get("writes") or []):
        _m = re.search(r"(?:^|_)0(\d{3})_", _w)
        if _m and "alembic/versions/" in _w:
            _numbers.setdefault(_m.group(1), []).append(_t["id"])
_dupe_numbers = {k: v for k, v in _numbers.items() if len(set(v)) > 1}
check(
    "迁移编号未被多个任务重复占用",
    not _dupe_numbers,
    f"重复编号：{_dupe_numbers}" if _dupe_numbers else "",
)

print()
print("=" * 70)
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print(f"ALL OK —— 写权限冲突检测可用，依赖图无环，{len(TASKS_DOC['tasks'])} 个任务均有 DoD 与写权限声明。")
