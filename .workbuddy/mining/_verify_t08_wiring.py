"""T08 校验：导入 / 路由注册 / 声明顺序。"""
from __future__ import annotations

import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ok = True


def check(label: str, fn) -> None:
    global ok
    try:
        result = fn()
        print(f"  [OK  ] {label}" + (f"  {result}" if result else ""))
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  [FAIL] {label}  {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)


print("=== 1. 模块导入 ===")
check("service", lambda: __import__(
    "app.services.factors.candidate_pool.service", fromlist=["x"]).SOURCE_TYPE_FILTER)
check("route 模块", lambda: __import__(
    "app.api.routes.mining_candidate_pool", fromlist=["x"]).router is not None)


def route_count() -> str:
    from app.api.router import api_router
    paths = sorted({r.path for r in api_router.routes if "candidate-pools" in r.path})
    return f"{len(paths)} 条候选池路由"


check("router 注册", route_count)

print()
print("=== 2. 路由清单（按声明顺序；静态路径必须在 {pool_id} 之前）===")
from app.api.router import api_router  # noqa: E402

entries = []
for r in api_router.routes:
    path = getattr(r, "path", "")
    # 只看**本任务**的路由前缀（`/api/v1/discovery/candidate-pools` 是 discovery 域的另一个
    # 既有路由，仅因名字里含 candidate-pools 会被宽过滤误抓，且不受本路由遮蔽）
    if path.startswith("/api/v1/factor-mining/candidate-pools"):
        methods = ",".join(sorted(getattr(r, "methods", []) or []))
        entries.append((path, methods))
for path, methods in entries:
    print(f"  {methods:16s} {path}")

print()
print("=== 3. T09 依赖：静态路径不得被 {pool_id} 吞掉 ===")
first_dyn = next((i for i, (p, _m) in enumerate(entries) if "{pool_id}" in p), None)
statics_after = [p for i, (p, _m) in enumerate(entries)
                 if "{pool_id}" not in p and first_dyn is not None and i > first_dyn]
print(f"  首个含 {{pool_id}} 的声明位置 = {first_dyn}")
print(f"  位于其后的静态路径 = {statics_after or '无（OK）'}")

print()
print("=== 4. 关键 DTO 字段 ===")
from app.api.routes.mining_candidate_pool import (  # noqa: E402
    CandidatePoolCreate,
    CandidatePoolUpdate,
    MemberBatchAdd,
    MemberBatchRemove,
)
for model in (CandidatePoolCreate, CandidatePoolUpdate, MemberBatchAdd, MemberBatchRemove):
    print(f"  {model.__name__:22s} fields={sorted(model.model_fields)}")

print()
print("RESULT:", "ALL OK" if ok and not statics_after else "HAS FAILURES")
sys.exit(0 if ok and not statics_after else 1)
