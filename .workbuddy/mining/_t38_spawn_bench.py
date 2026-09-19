# -*- coding: utf-8 -*-
"""T38 补充实测：Windows spawn 子进程开销（G1 关键风险项）。

G1 三大风险中「Windows 无 fork，spawn 每子进程重 import pandas/numpy」是
**可离线实测**的（不依赖挖掘主链路是否打通）。测量：
  1. 冷启动：spawn 一个子进程并执行 `import numpy, pandas` 的墙钟耗时；
  2. 往返：进程池创建 + 一次极轻量任务的往返延迟；
  3. 池复用：同一池内第二次任务的边际开销（判断"进程池复用"能否摊薄）。
"""
import io
import multiprocessing as mp
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def _child_import_cost() -> float:
    t0 = time.perf_counter()
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    return (time.perf_counter() - t0) * 1000.0


def _child_bootstrap() -> tuple[float, float]:
    """子进程内：解释器就绪 → import 耗时 → 返回 (bootstrap_ms, import_ms)。"""
    t0 = time.perf_counter()
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    t1 = time.perf_counter()
    return ((t1 - t0) * 1000.0, (t1 - t0) * 1000.0)


def _echo(x: int) -> int:
    return x * 2


def main() -> None:
    print("start method:", mp.get_start_method(), "| cpu:", __import__("os").cpu_count())
    ctx = mp.get_context("spawn")

    # 1) 冷启动：单次 spawn 子进程（含 import numpy/pandas）
    t0 = time.perf_counter()
    with ctx.Pool(processes=1) as pool:
        cost = pool.apply(_child_import_cost)
    cold_ms = (time.perf_counter() - t0) * 1000.0
    print(f"1) 冷启动 spawn+init+一次任务: {cold_ms:8.1f} ms "
          f"(其中子进程 import numpy+pandas: {cost:8.1f} ms)")

    # 2) 池复用：同一池内连续 20 次轻量任务的平均往返
    t0 = time.perf_counter()
    with ctx.Pool(processes=2) as pool:
        for _ in range(20):
            pool.apply(_echo, (21,))
    reuse_total = (time.perf_counter() - t0) * 1000.0
    print(f"2) 建池(2 进程)+20 次轻量任务往返: {reuse_total:8.1f} ms "
          f"→ 单次往返(含建池摊薄): {reuse_total / 20:8.1f} ms")

    # 3) 池已建后，边际往返（不含建池）
    with ctx.Pool(processes=2) as pool:
        pool.apply(_echo, (1,))            # 预热
        t0 = time.perf_counter()
        for _ in range(50):
            pool.apply(_echo, (21,))
        marginal = (time.perf_counter() - t0) * 1000.0
    print(f"3) 池已就绪后 50 次轻量任务: {marginal:8.1f} ms "
          f"→ 边际单次往返: {marginal / 50:8.2f} ms")

    print("\n结论提示：G1 要划算，单代『可并行段』耗时必须显著大于"
          "「子进程冷启动 + 数据序列化往返」，且并行段应是 CPU 密集型。")


if __name__ == "__main__":
    main()
