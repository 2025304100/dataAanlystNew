"""blackbox / e2e 用例的"活服务不在"统一处理。

为什么需要它
============
原先 5 处 `pytest.skip("后端未运行…")` 各自为政，带来一个**假的绿**：实测后端没起来时，
`pytest -m blackbox` 跑出 **79 skipped / 0 failed**，进程退出码 0，CI job 判为通过——
blackbox 覆盖实际为 0，而任何人都看不出来。而 CI 的 "Wait for backend" 循环即使从没等到
200 也会正常结束（for 循环不返回失败），等于把"服务没起来"降级成"测试全过"。

约定
====
- 默认（本地开发）：服务不在 → 照旧 skip，不误红。
- `REQUIRE_LIVE_BACKEND=1`（CI 必须设）：服务不在 → **fail fast**，明确拒绝把零覆盖当通过。
- 单独放成模块而不是写进 conftest：用例文件 import conftest 当普通模块会重复执行其顶层逻辑。
"""
from __future__ import annotations

import os

import pytest


def live_backend_required() -> bool:
    return os.environ.get("REQUIRE_LIVE_BACKEND", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def skip_or_fail_no_live_backend(exc: object, *, what: str = "后端服务") -> None:
    """活服务不可达时：CI 下失败，本地跳过。永不静默通过。"""
    if live_backend_required():
        pytest.fail(
            f"{what} 未就绪，且 REQUIRE_LIVE_BACKEND=1："
            f"拒绝把零覆盖当成通过（{exc}）"
        )
    pytest.skip(f"{what} 未运行，跳过本组用例（{exc}）")
