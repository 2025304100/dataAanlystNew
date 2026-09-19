# -*- coding: utf-8 -*-
"""T38 补充实测 2：spawn 下大数据量（因子/收益面板）序列化往返成本。

G1 若做「因子级/子表达式级并行」，父→子必须传面板数据；spawn 走 pickle，
序列化成本可能吃掉并行收益。测量三种量级的往返：
  - 小：(60, 50)    ≈ 24 KB
  - 中：(250, 500)  ≈ 1 MB（日线 5 年 × 500 标的）
  - 大：(1000, 3000) ≈ 24 MB（长周期全市场）
并与「纯计算」对照，给出盈亏平衡判据。
"""
import io
import multiprocessing as mp
import sys
import time

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def _roundtrip(payload: np.ndarray) -> float:
    """子进程：仅接收并返回 checksum（不含计算），衡量纯传输成本。"""
    return float(payload.sum())


def _compute(payload: np.ndarray) -> float:
    """子进程：接收后做一次真实量级的向量化计算（模拟 ast_eval/subexpr）。"""
    x = payload
    acc = np.zeros_like(x)
    for _ in range(20):                     # 20 次向量化 pass ≈ 一代内的算子求值量级
        acc = acc + np.sin(x) * 0.5
    return float(acc.sum())


def bench(name: str, shape: tuple[int, int]) -> None:
    data = np.random.default_rng(0).normal(0, 1, shape)
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=2) as pool:
        pool.apply(_roundtrip, (data,))     # 预热（含 import + 首次传参）
        t0 = time.perf_counter()
        for _ in range(20):
            pool.apply(_roundtrip, (data,))
        transfer = (time.perf_counter() - t0) * 1000.0 / 20

        t0 = time.perf_counter()
        for _ in range(20):
            pool.apply(_compute, (data,))
        with_compute = (time.perf_counter() - t0) * 1000.0 / 20
    mb = data.nbytes / (1024 * 1024)
    print(f"  {name:<22} {str(shape):<14} {mb:>7.2f} MB | "
          f"传输往返 {transfer:>8.2f} ms | 传输+20 pass 计算 {with_compute:>9.2f} ms | "
          f"计算净增 {with_compute - transfer:>9.2f} ms")


def main() -> None:
    print("== spawn 大数据往返（2 进程池，20 次均值）==")
    bench("小(周频/少量标的)", (60, 50))
    bench("中(日线5年×500)", (250, 500))
    bench("大(长周期全市场)", (1000, 3000))
    print("\n盈亏平衡判据：单任务『纯计算』耗时 > (传输往返 + 调度) × 进程数 才划算；")
    print("若纯计算 < 传输往返，则并行反而更慢（应改走共享内存或不做）。")


if __name__ == "__main__":
    main()
