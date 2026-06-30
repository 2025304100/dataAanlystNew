from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import os


@contextmanager
def quiet_akshare_output():
    """抑制第三方库的进度输出，避免在 Windows 隐藏工作进程中出错。"""
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            yield
