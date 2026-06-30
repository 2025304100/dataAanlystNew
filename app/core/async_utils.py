"""用于在不阻塞事件循环的情况下运行同步代码的工具。"""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def run_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """在默认执行器中运行同步函数，避免阻塞事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
