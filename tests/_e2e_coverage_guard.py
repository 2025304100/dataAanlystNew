"""e2e 闸门的「至少真跑 N 例」最低覆盖断言。

为什么还需要它（`_live_backend_guard` 之外）
============================================
`_live_backend_guard` 只解决「活服务不在 → 全体 skip → CI 算通过」这一条假绿路径。
但 e2e 还有别的"0 failed 却几乎没执行"的可能，全都不会让 job 变红：

- 浏览器不可用 / `E2E_BROWSER_CHANNEL` 配错 → 各 fixture 走 skip；
- 前端 dev server 没起来但探活写法宽松 → 全体 skip；
- marker 被误删或改名 → `-m e2e` 选择集直接变 0，pytest 以 0 退出；
- 选择器大面积腐烂把用例改成 skip（历史上真实发生过 13 例假结果）。

所以除了"服务在不在"，还必须判"真跑了几条"。

约定
====
- 默认（本地开发）：**不判定**，允许只跑单条 e2e 用例调试。
- `E2E_REQUIRE_MINIMUM=1`（CI 必须设）：真正执行过的 e2e 用例数
  （passed + failed + error，不含 skip / deselect）低于阈值即判失败。
- 阈值默认 20，可用 `E2E_MIN_EXECUTED` 覆盖。当前 e2e 实测 36 passed + 5 skipped，
  取 20 既留了重构余量，又足以抓住"整体没跑起来"。

单独成模块而不是写进 conftest：用例文件把 conftest 当普通模块 import 会重复执行
其顶层逻辑（与 `_live_backend_guard.py` 同一理由）。
"""
from __future__ import annotations

import os

DEFAULT_MIN_EXECUTED = 20

_TRUTHY = {"1", "true", "yes", "on"}


def truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in _TRUTHY


def e2e_minimum_required() -> bool:
    """CI 是否要求判定最低执行数。"""
    return truthy(os.environ.get("E2E_REQUIRE_MINIMUM"))


def min_executed_threshold() -> int:
    """阈值：`E2E_MIN_EXECUTED`，非法值回退默认而不是抛错（守卫不该比被测物更脆）。"""
    raw = os.environ.get("E2E_MIN_EXECUTED", "").strip()
    if not raw:
        return DEFAULT_MIN_EXECUTED
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MIN_EXECUTED
    return value if value >= 0 else DEFAULT_MIN_EXECUTED


def evaluate_coverage(
    executed: int,
    skipped: int,
    *,
    required: bool,
    minimum: int | None = None,
) -> str | None:
    """返回 None 表示通过；否则返回要打印给 CI 的失败说明。

    只看 executed（真跑过的条数）：skip 再多也不能替它凑数，否则"全体 skip"
    又会变成绿灯——那正是这套守卫要消灭的东西。
    """
    if not required:
        return None
    floor = min_executed_threshold() if minimum is None else minimum
    if executed >= floor:
        return None
    return (
        f"e2e 闸门实际只执行了 {executed} 条（阈值 {floor}，另有 {skipped} 条被跳过）。"
        "拒绝把「几乎没跑」当成通过：请检查前后端是否真起来、浏览器 channel 是否可用、"
        "marker/选择器是否被批量改坏。"
        + (f"（当前阈值来自 E2E_MIN_EXECUTED={os.environ.get('E2E_MIN_EXECUTED')}）"
           if os.environ.get("E2E_MIN_EXECUTED") else "")
    )
