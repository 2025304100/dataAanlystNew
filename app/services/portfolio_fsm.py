"""portfolio-factor-backtest-full-linkage #2：扩展状态机 FSM + 租约键隔离（纯函数实现）。

10 种 ExtendedPortfolioState + 转移矩阵 can_transition_extended（不依赖 DB/Session，
决定"转移是否合法"这一 pure 判断）。
- 租约键隔离：AUTO_SIM vs BACKTEST 前缀不同 + 可选 trade_date
- 运行层映射：AUTO / BACKTEST，用于"视角状态 READY + 后台 RUNNING_BACKTEST 共存"
"""
from __future__ import annotations

from datetime import date
from typing import Literal


# ────────────────────────────────────────────────────────────────────────────
# 10 种扩展状态（pf-linkage tasks.md N1~N9）
# ────────────────────────────────────────────────────────────────────────────
ExtendedPortfolioState = Literal[
    "PENDING_INITIAL_REVIEW",
    "READY",
    "RUNNING_AUTOSIMULATION",
    "RUNNING_BACKTEST",
    "DATA_INCOMPLETE_PAUSED",
    "RECONCILIATION_BLOCKED",
    "MODEL_INACTIVE",
    "INTERRUPTED",
    "ADMIN_PAUSED",
    # 可选的 10+：DEGRADED_READY（研究模式 NOT_PIT_SAFE 但允许，另用）
    "DEGRADED_READY",
]


VALID_EXTENDED_STATES: frozenset[str] = frozenset({
    "PENDING_INITIAL_REVIEW",
    "READY",
    "RUNNING_AUTOSIMULATION",
    "RUNNING_BACKTEST",
    "DATA_INCOMPLETE_PAUSED",
    "RECONCILIATION_BLOCKED",
    "MODEL_INACTIVE",
    "INTERRUPTED",
    "ADMIN_PAUSED",
    "DEGRADED_READY",
})


# 明确允许的转移（不在其中的视为非法，除 *→ADMIN_PAUSED）
_ALLOWED_TRANSITIONS: set[tuple[str, str]] = {
    # N1: 首次审查通过
    ("PENDING_INITIAL_REVIEW", "READY"),
    # N2: READY → 两 RUNNING / 暂停 / 模型失效 / 对账阻塞（临时值/未来触发）
    ("READY", "RUNNING_AUTOSIMULATION"),
    ("READY", "RUNNING_BACKTEST"),
    ("READY", "DATA_INCOMPLETE_PAUSED"),
    ("READY", "MODEL_INACTIVE"),
    ("READY", "RECONCILIATION_BLOCKED"),
    ("READY", "DEGRADED_READY"),
    # N3: AUTOSIM → READY / BLOCKED / INTERRUPTED
    ("RUNNING_AUTOSIMULATION", "READY"),
    ("RUNNING_AUTOSIMULATION", "RECONCILIATION_BLOCKED"),
    ("RUNNING_AUTOSIMULATION", "INTERRUPTED"),
    ("RUNNING_AUTOSIMULATION", "DATA_INCOMPLETE_PAUSED"),
    # N4: BACKTEST → READY / BLOCKED / INTERRUPTED
    ("RUNNING_BACKTEST", "READY"),
    ("RUNNING_BACKTEST", "RECONCILIATION_BLOCKED"),
    ("RUNNING_BACKTEST", "INTERRUPTED"),
    ("RUNNING_BACKTEST", "DATA_INCOMPLETE_PAUSED"),
    # N5: DATA_INCOMPLETE 数据补齐 → READY
    ("DATA_INCOMPLETE_PAUSED", "READY"),
    # N6: 对账修复人工 confirm → READY
    ("RECONCILIATION_BLOCKED", "READY"),
    # N7: 模型激活/退役
    ("MODEL_INACTIVE", "READY"),
    ("READY", "MODEL_INACTIVE"),
    # N9: INTERRUPTED 自动/人工 retry → READY
    ("INTERRUPTED", "READY"),
    # N8: ADMIN_PAUSED → READY 解除
    ("ADMIN_PAUSED", "READY"),
    # DEGRADED_READY: 研究模式 ↔ READY 可切换
    ("DEGRADED_READY", "READY"),
    ("READY", "DEGRADED_READY"),
    ("DEGRADED_READY", "DATA_INCOMPLETE_PAUSED"),
    ("DEGRADED_READY", "MODEL_INACTIVE"),
    ("DEGRADED_READY", "RECONCILIATION_BLOCKED"),
    ("DEGRADED_READY", "RUNNING_AUTOSIMULATION"),
    ("DEGRADED_READY", "RUNNING_BACKTEST"),
}


# 任何状态都可以进入 ADMIN_PAUSED（人工暂停不挑前置）
_IMMEDIATE_ALLOWED_TO = {"ADMIN_PAUSED"}


# ────────────────────────────────────────────────────────────────────────────
# Seam 1：转移合法性判断（纯函数）
# ────────────────────────────────────────────────────────────────────────────
def can_transition_extended(
    from_state: str,
    to_state: str,
) -> bool:
    """判断 from → to 是否合法。不处理 NOOP（from==to 的场景在更外层直接返回 True，
    这里相同返回 True 也安全，调用方可以自己决定 NOOP/OK 语义）。
    """
    if from_state == to_state:
        return True
    if from_state not in VALID_EXTENDED_STATES or to_state not in VALID_EXTENDED_STATES:
        return False
    # 任何状态 → ADMIN_PAUSED：都允许
    if to_state in _IMMEDIATE_ALLOWED_TO:
        return True
    return (from_state, to_state) in _ALLOWED_TRANSITIONS


# ────────────────────────────────────────────────────────────────────────────
# Seam 2：租约键 derive_lease_key（前缀隔离 AUTO vs BT）
# ────────────────────────────────────────────────────────────────────────────
_RUNNING_TYPE_PREFIX: dict[str, str] = {
    "RUNNING_AUTOSIMULATION": "au",
    "RUNNING_BACKTEST": "bt",
}


def derive_lease_key(
    portfolio_id: int,
    running_state: Literal["RUNNING_AUTOSIMULATION", "RUNNING_BACKTEST"],
    trade_date: date | None = None,
) -> str:
    """AUTO vs BT 前缀不同；不同交易日不同键。不传 trade_date 时退化为 {prefix}:{pid}。
    （对于 AUTO_SIM 的"当日调度"，调用方如果没传 trade_date 可接受稳定值。）
    """
    prefix = _RUNNING_TYPE_PREFIX.get(running_state)
    if prefix is None:
        raise ValueError(
            f"derive_lease_key only supports RUNNING_AUTOSIMULATION / RUNNING_BACKTEST; "
            f"got {running_state!r}"
        )
    pid = int(portfolio_id)
    if trade_date is None:
        return f"lease:{prefix}:p{pid}"
    if not isinstance(trade_date, date):
        raise TypeError(f"trade_date must be datetime.date, got {type(trade_date).__name__}")
    return f"lease:{prefix}:p{pid}:{trade_date.isoformat()}"


# ────────────────────────────────────────────────────────────────────────────
# Seam 3：运行层 → None / AUTO / BACKTEST（用于 coexistence 判定）
# ────────────────────────────────────────────────────────────────────────────
def state_running_layer(s: str) -> Literal["AUTO", "BACKTEST"] | None:
    if s == "RUNNING_AUTOSIMULATION":
        return "AUTO"
    if s == "RUNNING_BACKTEST":
        return "BACKTEST"
    return None
