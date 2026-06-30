"""Utilities for running synchronous code without blocking the event loop."""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def run_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous function in the default executor to avoid blocking."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
