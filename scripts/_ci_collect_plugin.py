"""给 scripts/ci_shard.py 用的收集插件：把"最终保留的用例 nodeid"写进文件。

为什么要走文件而不是解析 pytest 控制台输出：
1. 本机 pytest 的 `-q --collect-only` 打的是树（`<Module …>`），不是平铺 nodeid；
2. 汇总行第一个数字是 "collected 5728"（含被 deselect 的），拿它对数会假报不一致；
3. 更关键：在同进程里跑 pytest.main 收集**整个白盒套件**之后，父进程的标准输出
   会被某些测试模块导入时的副作用影响，导致随后 spawn 的子进程 pytest 输出看不到
   （实测：分片执行只剩分片清单，没有测试结果，rc 却正常）。所以一律改用文件交接。
"""
from __future__ import annotations

import os
from pathlib import Path

_OUTPUT_VAR = "CI_SHARD_COLLECT_FILE"

_all: list[str] = []
_dropped: set[str] = set()


def pytest_collection_modifyitems(items):  # noqa: ANN001
    _all.extend(it.nodeid for it in items)


def pytest_deselected(items):  # noqa: ANN001
    # 标记过滤掉的部分不能算进分片权重，否则均衡失真
    _dropped.update(it.nodeid for it in items)


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001
    target = os.environ.get(_OUTPUT_VAR)
    if not target:
        return
    kept = [n for n in _all if n not in _dropped]
    Path(target).write_text("\n".join(kept), encoding="utf-8")
