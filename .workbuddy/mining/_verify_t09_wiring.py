"""T09 路由 wiring 校验（**可复用守卫，勿删**）。

⚠️ 不要把它加进任何「清理一次性脚本」的名单 ——
   手册 R21 与 T09/T08 任务卡都引用它做路由顺序断言。
   本文件在 T09 收工清理时被误删过一次，故在此显式标注。

用途
----
新增候选池端点后跑一次，确认：
  1. 模块导入正常
  2. 4 条静态路径都在
  3. **静态路径排在 `{pool_id}` 之前**（FastAPI 按声明顺序匹配，否则静默 404）
  4. `api_router` 里确实注册了（要带上 `settings.api_prefix` 再比较）

用法
----
    .venv/Scripts/python.exe .workbuddy/mining/_verify_t09_wiring.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

FAILS: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


print("=" * 72)
print("1. 模块导入")
print("=" * 72)
from app.services.factors.candidate_pool import presets as pool_presets  # noqa: E402
from app.services.factors.candidate_pool import rules as pool_rules  # noqa: E402

check(True, "rules/presets 导入成功")

from app.api.routes.mining_candidate_pool import router  # noqa: E402

check(True, "路由模块导入成功")

print()
print("=" * 72)
print("2. 路由清单（声明顺序）")
print("=" * 72)
entries: list[tuple[str, str]] = []
for r in router.routes:
    path = getattr(r, "path", "")
    if "/factor-mining/candidate-pools" not in path:
        continue
    entries.append((path, ",".join(sorted(getattr(r, "methods", []) or []))))
for path, methods in entries:
    print(f"    {methods:12s} {path}")

print()
print("=" * 72)
print("3. 顺序断言：静态路径必须在第一个 {pool_id} 之前")
print("=" * 72)
#: 全部**单段**静态路径（T09 的筛选 4 条 + T10 的导入 3 条）。
#: 它们都会与 `/{pool_id}` 争匹配，必须排在前面。
STATIC = (
    # T09 条件筛选
    "/factor-mining/candidate-pools/filter-fields",
    "/factor-mining/candidate-pools/filter-presets",
    "/factor-mining/candidate-pools/preview",
    "/factor-mining/candidate-pools/from-filter",
    # T10 外部导入
    "/factor-mining/candidate-pools/import-template",
    "/factor-mining/candidate-pools/import-preview",
    "/factor-mining/candidate-pools/import-errors",
)
idx = {p: i for i, (p, _m) in enumerate(entries)}
first_dynamic = next((i for i, (p, _m) in enumerate(entries)
                      if p.rsplit("/", 1)[-1] == "{pool_id}"), None)
check(first_dynamic is not None, "存在 {pool_id} 动态路径")
for s in STATIC:
    got = idx.get(s)
    check(
        got is not None and first_dynamic is not None and got < first_dynamic,
        f"{s.split('/')[-1]} 排在 {{pool_id}} 之前",
        f"(index {got} < {first_dynamic})" if got is not None else "(未注册!)",
    )

print()
print("=" * 72)
print(f"4. api_router 中 {len(STATIC)} 条静态路径均存在")
print("=" * 72)
from app.api.router import api_router  # noqa: E402
from app.core.config import settings  # noqa: E402

# ⚠️ api_router 带 `settings.api_prefix` 前缀（本项目为 /api/v1），模块 router 没有。
prefix = str(getattr(settings, "api_prefix", "") or "")
all_paths = {getattr(r, "path", "") for r in api_router.routes}
for s in STATIC:
    check(f"{prefix}{s}" in all_paths, f"api_router 含 {prefix}{s}")

print()
print("=" * 72)
print("5. 精确路径不重复")
print("=" * 72)
exact = [p for p, _m in entries if p == "/factor-mining/candidate-pools"]
check(len(exact) == 2, f"精确路径有 GET/POST 两条（实得 {len(exact)}）")

print()
print("=" * 72)
if FAILS:
    print(f"FAILED —— {len(FAILS)} 项: {FAILS}")
    sys.exit(1)
print("ALL OK —— 候选池路由 wiring 正确（含静态路径顺序）")
