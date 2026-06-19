from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import os


@contextmanager
def quiet_akshare_output():
    """Suppress third-party progress output that can break hidden Windows workers."""
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            yield
