# -*- coding: utf-8 -*-
"""D1 收敛早停（向导 §6.6.7 / 需求 §6.1.2）——**只判断停止，不调参**。

- `stall_count` = 连续 `|convergence_delta| < STALL_THRESHOLD` 的代数；
- **先自救**：`stall_count` 达到 2 不立即停，交给 C1 触发停滞自救
  （提高变异/注入/结构变异/跨赛道）；
- **后停止**：自救后 `stall_count` 仍累计到 3，才置 `converged`
  进入最终全量验证——避免在"高原期"误停；
- **职责红线（not_do）**：D1 只读 val 段信号并判断停止，
  **不修改任何率**（调参归 C1）。本模块因此不导出任何率字段。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

#: 进步判定阈值：`|delta| < 0.01` 视为无进步（默认，高级可调）
STALL_THRESHOLD = 0.01

#: 触发 C1 自救的连续停滞代数（先自救）
RESCUE_STALL = 2

#: 触发早停的连续停滞代数（自救无效后才停）
STOP_STALL = 3


@dataclass(frozen=True)
class ConvergenceState:
    """D1 每代输出（只含观测与停止判断，**不含任何率**）。"""

    best_icir: float
    prev_best: float | None
    convergence_delta: float
    stall_count: int
    should_rescue: bool
    should_stop: bool
    reason: str = ""


def update_convergence(
    *,
    best_icir: float,
    prev_best: float | None,
    stall_count: int,
    threshold: float = STALL_THRESHOLD,
) -> ConvergenceState:
    """更新停滞计数并给出停止/自救判断。

    Args:
        best_icir: 本代最优 ICIR。
        prev_best: 上代最优 ICIR（首代为 None → 视为无比较基准，delta=0 且不计停滞）。
        stall_count: 上代累计的连续停滞代数。
        threshold: 进步判定阈值（默认 0.01）。

    Returns:
        `ConvergenceState`。进度显著 → `stall_count` 归零；
        无进步 → 累加，达到 2 触发自救信号、达到 3 触发早停。
    """
    cur = float(best_icir) if best_icir is not None else float("nan")
    if prev_best is None or not math.isfinite(float(prev_best)) or not math.isfinite(cur):
        delta = 0.0
        stall = max(0, int(stall_count))
    else:
        delta = cur - float(prev_best)
        if abs(delta) < float(threshold):
            stall = max(0, int(stall_count)) + 1
        else:
            stall = 0
    rescue = stall >= RESCUE_STALL
    stop = stall >= STOP_STALL
    if stop:
        reason = "连续 %d 代无显著进步（C1 自救无效）→ 早停进入最终验证" % stall
    elif rescue:
        reason = "连续 %d 代无显著进步 → 交 C1 自救（先自救，不停止）" % stall
    elif stall == 0:
        reason = "本代有进步，停滞计数归零"
    else:
        reason = "停滞 1 代，继续观察"
    return ConvergenceState(
        best_icir=cur,
        prev_best=None if prev_best is None else float(prev_best),
        convergence_delta=delta,
        stall_count=stall,
        should_rescue=rescue,
        should_stop=stop,
        reason=reason,
    )


__all__ = [
    "STALL_THRESHOLD", "RESCUE_STALL", "STOP_STALL",
    "ConvergenceState", "update_convergence",
]
