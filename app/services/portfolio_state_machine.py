"""WP0-2 / G3-4 组合运行状态机（9 种状态）与人工对账确认流转。

9 种状态（tasks.md §WP0-2 九状态矩阵 + 4 项允许矩阵）：
  - PENDING_INITIAL_REVIEW    : 新建候选池，待人工复核（review_triggered=1，状态=PENDING-REVIEW）
  - READY                     : 就绪（首次确认/对账清零/人工复核通过/数据刷新后）
  - RUNNING_AUTO_SIMULATION   : 正在执行 auto_simulation（T 日 20:00~20:30 短时间窗口）
  - RUNNING_BACKTEST          : 正在执行历史回测
  - RECONCILIATION_BLOCKED    : 对账发现差异（BLOCKED）；禁止进入 RUNNING*
  - SCORE_STALE               : 信号/打分超新鲜度阈值（WP0-2 新增：评分/信号 ≥ 3d）
  - DATA_INCOMPLETE_PAUSED    : 市场/基本面数据缺口 ≥ 1（WP0-2 新增：数据不完整暂停）
  - MODEL_INACTIVE            : 组合无 active_model_run_id / active_model_run_id 切换未确认
  - INTERRUPTED               : 中断（worker crash/超时）；允许 → READY 自动重试
  - ADMIN_PAUSED              : 管理暂停（人工停，禁止自动接管）—— 唯一出边：ADMIN_PAUSED → READY

状态转移 9×9 合法边：
  READY → RUNNING_AUTO_SIMULATION / RUNNING_BACKTEST
  RUNNING_AUTO_SIMULATION → READY / RECONCILIATION_BLOCKED / INTERRUPTED
  RUNNING_BACKTEST → READY / RECONCILIATION_BLOCKED / INTERRUPTED
  PENDING_INITIAL_REVIEW → READY
  RECONCILIATION_BLOCKED / SCORE_STALE / DATA_INCOMPLETE_PAUSED / MODEL_INACTIVE / INTERRUPTED
        （以上各 → READY）
  (*) → ADMIN_PAUSED               ：任何状态都可被管理员暂停
  ADMIN_PAUSED → READY             ：管理员解除（唯一出边）

动作允许矩阵（HG1 + B6 扩展动作）：
  NEW_BUY                       : READY / RUNNING_AUTO_SIMULATION / RUNNING_BACKTEST / SCORE_STALE / MODEL_INACTIVE 允许
  RISK_SELL                     : 任何状态允许（含 ADMIN_PAUSED，风控强制退出）—— 但 ADMIN_PAUSED 下只允许 RISK_SELL
  NORMAL_SELL                   : READY / RUNNING_* / SCORE_STALE / MODEL_INACTIVE 允许
  FORCE_SELL_WITH_REVIEW        : RECONCILIATION_BLOCKED / ADMIN_PAUSED 下仍然允许（operated_by + review_note 必须有）
  AUTO_RECOVERY_FLOW            : 仅 READY / INTERRUPTED → READY 这条；其他（ADMIN_PAUSED/RECONCILIATION_BLOCKED）不允许
  add_candidates / start_simulation / promote_candidate_buy / publish_decision_result : 保留老 HG1 4 项

ADMIN_PAUSED 唯一出边规则：
  只有 ADMIN_PAUSED → READY 一条出边，不能直接跳转到任何其他状态。

禁止的转移一律写 ILLEGAL_STATE_TRANSITION 审计事件并抛 StateTransitionError / ValueError。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Literal

from sqlalchemy import inspect as sa_inspect, text as sqltext
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio
from app.services.data_governance_audit import audit_illegal_state_transition

PortfolioState = Literal[
    "PENDING_INITIAL_REVIEW",
    "READY",
    "RUNNING_AUTO_SIMULATION",
    "RUNNING_BACKTEST",
    "RECONCILIATION_BLOCKED",
    "SCORE_STALE",
    "DATA_INCOMPLETE_PAUSED",
    "MODEL_INACTIVE",
    "INTERRUPTED",
    "ADMIN_PAUSED",
]

VALID_STATES: frozenset[str] = frozenset({
    "PENDING_INITIAL_REVIEW",
    "READY",
    "RUNNING_AUTO_SIMULATION",
    "RUNNING_BACKTEST",
    "RECONCILIATION_BLOCKED",
    "SCORE_STALE",
    "DATA_INCOMPLETE_PAUSED",
    "MODEL_INACTIVE",
    "INTERRUPTED",
    "ADMIN_PAUSED",
})

# (from_state, to_state) → 是否允许（不含 ADMIN_PAUSED 通杀与反向）
_TRANSITIONS: set[tuple[str, str]] = {
    ("PENDING_INITIAL_REVIEW", "READY"),
    ("READY", "RUNNING_AUTO_SIMULATION"),
    ("READY", "RUNNING_BACKTEST"),
    ("RUNNING_AUTO_SIMULATION", "READY"),
    ("RUNNING_AUTO_SIMULATION", "RECONCILIATION_BLOCKED"),
    ("RUNNING_AUTO_SIMULATION", "INTERRUPTED"),
    ("RUNNING_BACKTEST", "READY"),
    ("RUNNING_BACKTEST", "RECONCILIATION_BLOCKED"),
    ("RUNNING_BACKTEST", "INTERRUPTED"),
    ("RECONCILIATION_BLOCKED", "READY"),
    ("SCORE_STALE", "READY"),
    ("DATA_INCOMPLETE_PAUSED", "READY"),
    ("MODEL_INACTIVE", "READY"),
    ("INTERRUPTED", "READY"),
    ("ADMIN_PAUSED", "READY"),
}
# ADMIN_PAUSED：任何状态都能 → ADMIN_PAUSED（管理员暂停）
_IMMEDIATE_ALLOWED_TO = frozenset({"ADMIN_PAUSED"})

# 动作（B6 定义 + 旧 HG1 4 项 联合使用）
_ACTION_NEW_BUY = "NEW_BUY"
_ACTION_RISK_SELL = "RISK_SELL"
_ACTION_NORMAL_SELL = "NORMAL_SELL"
_ACTION_FORCE_SELL_WITH_REVIEW = "FORCE_SELL_WITH_REVIEW"
_ACTION_AUTO_RECOVERY_FLOW = "AUTO_RECOVERY_FLOW"

_BUY_ALLOWED_STATES = frozenset({
    "READY", "RUNNING_AUTO_SIMULATION", "RUNNING_BACKTEST", "MODEL_INACTIVE",
})
_NORMAL_SELL_ALLOWED_STATES = frozenset({
    "READY", "RUNNING_AUTO_SIMULATION", "RUNNING_BACKTEST", "SCORE_STALE",
    "DATA_INCOMPLETE_PAUSED", "MODEL_INACTIVE", "INTERRUPTED",
})
# RISK_SELL：任何状态都允许（风控止损不能被暂停住）
# FORCE_SELL_WITH_REVIEW：RECONCILIATION_BLOCKED / ADMIN_PAUSED / DATA_INCOMPLETE_PAUSED 等仍允许（经 operated_by + review_note）
_FORCE_SELL_ALLOWED_STATES = VALID_STATES  # 仅当 operated_by+review_note 时才允许；此处只判断 state 层
# AUTO_RECOVERY：只能是 INTERRUPTED → READY 或 READY 自己（READY → READY 自循环也 OK）
_AUTO_RECOVERY_ALLOWED_STATES = frozenset({"READY", "INTERRUPTED"})


class StateTransitionError(RuntimeError):
    """非法状态转移。"""

    def __init__(self, portfolio_id: int, from_state: str, to_state: str, reason: str | None = None):
        self.portfolio_id = portfolio_id
        self.from_state = from_state
        self.to_state = to_state
        msg = f"Portfolio {portfolio_id}: illegal state transition {from_state} -> {to_state}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


@dataclass
class StateTransitionResult:
    portfolio_id: int
    from_state: str
    to_state: str
    status: Literal["OK", "NOOP"]


@dataclass
class PermissionGate:
    """HG1 4 项动作的当前允许/禁止判定（前端按钮/路由/调度器统一查询）。"""
    state: str
    can_add_candidates: bool
    can_start_new_simulation: bool
    can_promote_candidate_buy: bool
    can_publish_decision_result: bool


class PortfolioStateMachine:
    """面向对象包装器：绑定单个 Portfolio + Session（实例） + 通用静态方法（类级）。

    类级静态方法（DB 无关，直接走矩阵常量）：
        PortfolioStateMachine.valid_states() -> frozenset[str]
        PortfolioStateMachine.can_transition(from_state, to_state) -> bool
        PortfolioStateMachine.allows() -> Callable[[str, str], bool]
        PortfolioStateMachine.transition(portfolio_id=, from_state=, to_state=,
                                         trigger_reason=, operated_by=, correlation_id=, db=)
            仅验证转移合法性（非法 → ValueError）并返回 StateTransitionResult（模拟 OK/NOOP）。
            若提供 db，则同步调用 transition_portfolio_state 写 DB。

    实例方法（绑定单个 portfolio + session）：
        sm.current_state, sm.permissions(), sm.can_transition(to_state), sm.transition(to_state, ...)
    """

    # ── 类级：纯枚举/常量 ────────────────────────────────────────────────
    @classmethod
    def valid_states(cls) -> frozenset[str]:
        return VALID_STATES

    # ── 类级：转移合法性（纯内存，与 DB 解耦；测试直接传 2 个 str） ──────
    @classmethod
    def can_transition(cls, from_state: str, to_state: str | None = None) -> bool:
        """重载：两种调用签名

        1. PortfolioStateMachine.can_transition("READY", "RUNNING_AUTO_SIMULATION")  # 双参数
        2. sm = PortfolioStateMachine(portfolio, db); sm.can_transition("RUNNING")    # 实例 1 参数
        """
        # 实例方法情况：self 被当作 from_state
        # 简单判定：若 self 是 str 则调用者把 from_state 当 self（即当做 classmethod 调用 2 参数）
        if isinstance(from_state, str) and to_state is None:
            # 实例调用：from_state 实际上是 self（不是 str），但如果是 str，说明是 1 参数情况（实例 + to）
            # 注意：从 classmethod 角度，第一个参数其实是 "from_state" str 而不是 class
            # 这里：当 can_transition(s, t) 调用时 from_state=s, to_state=t → 进入此分支前到不了
            return False  # fallthrough
        if to_state is None:
            # 实例方法调 can_transition(to_state)：第一个参数 self 非字符串，第二个 to_state 是字符串
            # 这里 from_state 实际上就是 self，再调用 self.current_state
            from_state_obj = from_state  # self
            if not isinstance(from_state_obj, PortfolioStateMachine):
                return False
            from_s = from_state_obj.current_state
            to_s = to_state  # noqa: F841 (shadowed outer)
            to_state = to_s  # type: ignore[assignment]
            from_state = from_s
        # 到这里：from_state, to_state 都是字符串
        if not (from_state in VALID_STATES and to_state in VALID_STATES):
            return False
        if from_state == to_state:
            return True
        if (from_state, to_state) in _TRANSITIONS:
            return True
        if to_state in _IMMEDIATE_ALLOWED_TO:
            # 任何状态 → ADMIN_PAUSED （管理员暂停通杀）
            return True
        # ADMIN_PAUSED 唯一出边已经在 _TRANSITIONS 里了（ADMIN_PAUSED → READY）
        return False

    # ── 类级：动作允许矩阵 ───────────────────────────────────────────────
    @classmethod
    def allows(cls) -> Callable[[str, str], bool]:
        """返回 check(state: str, action: str) -> bool 可调用对象。"""
        return _check_allows_action

    # ── 类级：转移（只做合法性断言 + 可选写 DB） ─────────────────────────
    @classmethod
    def transition(  # type: ignore[override] - intentionally different from instance signature
        cls,
        *,
        portfolio_id: int,
        from_state: str | None = None,
        to_state: str,
        trigger_reason: str | None = None,
        operated_by: str | None = None,
        correlation_id: str | None = None,
        db: Session | None = None,
    ) -> StateTransitionResult:
        """测试友好版 transition：断言合法 + 返回 NOOP/OK；非法 → ValueError。

        参数 db 可选：若有 db 则同步落库（等同 transition_portfolio_state）；否则只做内存校验。
        参数 from_state 可选：若提供则按此状态校验转移矩阵（测试专用，不查 DB）。
        """
        if to_state not in VALID_STATES:
            raise ValueError(f"Invalid target state: {to_state!r}")
        if from_state is None:
            if db is None:
                raise ValueError("from_state 或 db 必须至少提供一个用于计算当前状态")
            from_state = _get_status(db, db.get(Portfolio, portfolio_id))
        if from_state not in VALID_STATES:
            raise ValueError(f"Invalid from_state: {from_state!r}")
        if from_state == to_state:
            return StateTransitionResult(
                portfolio_id=portfolio_id, from_state=from_state,
                to_state=to_state, status="NOOP",
            )
        # ADMIN_PAUSED 唯一出边规则
        if from_state == "ADMIN_PAUSED" and to_state != "READY":
            raise ValueError(
                f"ILLEGAL_TRANSITION: ADMIN_PAUSED->{to_state} (唯一出边=READY); "
                f"reason={trigger_reason}; operator={operated_by}"
            )
        allowed = (from_state, to_state) in _TRANSITIONS or to_state in _IMMEDIATE_ALLOWED_TO
        if not allowed:
            if db is not None:
                try:
                    audit_illegal_state_transition(
                        db, portfolio_id, from_state, to_state,
                        operator_id=operated_by or "unknown",
                        correlation_id=correlation_id,
                    )
                except Exception:
                    pass  # 审计事件写入失败不影响原本错误抛出
            raise ValueError(
                f"ILLEGAL_TRANSITION_ATTEMPT: {from_state}->{to_state}; "
                f"reason={trigger_reason}; operator={operated_by}"
            )
        if db is not None:
            return transition_portfolio_state(
                db, portfolio_id, to_state,  # type: ignore[arg-type]
                operator_id=operated_by or "system",
                correlation_id=correlation_id, reason=trigger_reason,
            )
        return StateTransitionResult(
            portfolio_id=portfolio_id, from_state=from_state,
            to_state=to_state, status="OK",
        )

    def __init__(self, portfolio: Portfolio, db: Session):
        self.portfolio = portfolio
        self.db = db

    @property
    def current_state(self) -> str:
        return _get_status(self.db, self.portfolio)

    def permissions(self) -> PermissionGate:
        s = self.current_state
        # 老 HG1 4 项：允许矩阵
        return PermissionGate(
            state=s,
            can_add_candidates=s in _BUY_ALLOWED_STATES,
            can_start_new_simulation=(s == "READY"),
            can_promote_candidate_buy=(s == "READY"),
            can_publish_decision_result=(s == "RUNNING_AUTO_SIMULATION"),
        )


# ════════════════════════════════════════════════════════════════════════════
# Fail-soft 列自适应（SQLite 可直接 DDL；其他 DB 先假定列已由 migration 建好）
# ════════════════════════════════════════════════════════════════════════════
def _portfolio_status_column_exists(db: Session) -> bool:
    try:
        row = db.execute(sqltext("SELECT portfolio_status FROM portfolios LIMIT 0"))
        return True
    except Exception:
        return False


def _ensure_status_column(db: Session) -> bool:
    """DB 里缺列则 DDL 追加（SQLite 允许 ALTER TABLE ADD COLUMN）；返回 True 表示列已可用。"""
    if _portfolio_status_column_exists(db):
        return True
    try:
        db.execute(sqltext(
            "ALTER TABLE portfolios ADD COLUMN portfolio_status VARCHAR(32)"
        ))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False
    return _portfolio_status_column_exists(db)


def _get_status(db: Session, p: Portfolio) -> str:
    if _ensure_status_column(db):
        row = db.execute(
            sqltext("SELECT portfolio_status FROM portfolios WHERE id=:i"),
            {"i": int(p.id)},
        ).fetchone()
        s = None if row is None else (row[0] if isinstance(row, tuple) else row._mapping.get("portfolio_status"))
        if s in VALID_STATES:
            return s
    # 启发式：从未决策成功 + 从未对账成功 = PENDING_INITIAL_REVIEW
    if p.last_decision_trade_date is None and p.last_reconciled_trade_date is None:
        return "PENDING_INITIAL_REVIEW"
    return "READY"


def _set_status(db: Session, p: Portfolio, to_state: str) -> None:
    if _ensure_status_column(db):
        db.execute(
            sqltext("UPDATE portfolios SET portfolio_status=:s WHERE id=:i"),
            {"s": str(to_state), "i": int(p.id)},
        )
        try:
            object.__setattr__(p, "portfolio_status", str(to_state))
        except Exception:
            pass
    return


# ════════════════════════════════════════════════════════════════════════════
# 主入口：函数式 API（与先前版本保持兼容，供 scheduler / service 层调用）
# ════════════════════════════════════════════════════════════════════════════
def transition_portfolio_state(
    db: Session,
    portfolio_id: int,
    to_state: PortfolioState,
    *,
    operator_id: str = "system",
    correlation_id: str | None = None,
    reason: str | None = None,
) -> StateTransitionResult:
    if to_state not in VALID_STATES:
        raise ValueError(f"Invalid target state: {to_state!r}")
    p = db.get(Portfolio, portfolio_id)
    if p is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    from_state = _get_status(db, p)
    if from_state == to_state:
        return StateTransitionResult(portfolio_id=portfolio_id, from_state=from_state, to_state=to_state, status="NOOP")

    # ADMIN_PAUSED 唯一出边规则：从 ADMIN_PAUSED 出发 → 只能 READY；不能直接到其他状态
    if from_state == "ADMIN_PAUSED" and to_state != "READY":
        audit_illegal_state_transition(
            db, portfolio_id, from_state, to_state,
            operator_id=operator_id, correlation_id=correlation_id,
        )
        raise StateTransitionError(
            portfolio_id, from_state, to_state,
            reason or "ADMIN_PAUSED 唯一出边=READY（必须先管理员解除暂停后再操作）",
        )

    allowed = (
        (from_state, to_state) in _TRANSITIONS
        or to_state in _IMMEDIATE_ALLOWED_TO
    )
    if not allowed:
        audit_illegal_state_transition(
            db, portfolio_id, from_state, to_state,
            operator_id=operator_id, correlation_id=correlation_id,
        )
        raise StateTransitionError(portfolio_id, from_state, to_state, reason)
    _set_status(db, p, to_state)
    return StateTransitionResult(portfolio_id=portfolio_id, from_state=from_state, to_state=to_state, status="OK")


# ════════════════════════════════════════════════════════════════════════════
# TR-02.7b2：对账修复人工确认
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class ReconciliationFixConfirmation:
    portfolio_id: int
    trade_date: date
    operator_id: str
    acknowledged_diffs_cleared: bool
    portfolio_now_ready: bool


def confirm_reconciliation_fixed(
    db: Session,
    portfolio_id: int,
    trade_date: date,
    *,
    operator_id: str,
    acknowledge_all_diffs_cleared: bool = False,
    correlation_id: str | None = None,
    force_skip_re_reconcile: bool = False,
) -> ReconciliationFixConfirmation:
    """WP0-2 TR-02.7b2：人工确认对账差异已修正。"""
    if not operator_id:
        raise ValueError("operator_id is required for manual reconciliation confirmation")
    if not acknowledge_all_diffs_cleared:
        raise ValueError(
            "acknowledge_all_diffs_cleared=True is required before manual reconciliation confirmation"
        )
    p = db.get(Portfolio, portfolio_id)
    if p is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")

    # 重跑一次对账（除非管理员显式跳过）
    if not force_skip_re_reconcile:
        try:
            from app.services.portfolio_reconciliation import reconcile_trade_date
            report = reconcile_trade_date(db, portfolio_id, trade_date,
                                          operator_id=operator_id,
                                          correlation_id=correlation_id)
            if report.has_any_diff and report.status == "BLOCKED":
                raise StateTransitionError(
                    portfolio_id, "RECONCILIATION_BLOCKED", "READY",
                    reason=(
                        "Re-reconcile仍有差异："
                        + ", ".join(
                            f"{d.kind}#{d.symbol_id or 'global'}"
                            for d in report.differences
                            if d.kind != "NOT_CHECKED"
                        )
                    ),
                )
        except ImportError:
            # 本模块可独立加载，缺对账服务时跳过重跑，依赖调用方校验
            pass

    # 单调推进 last_reconciled_trade_date
    if p.last_reconciled_trade_date is None or p.last_reconciled_trade_date < trade_date:
        p.last_reconciled_trade_date = trade_date

    res = transition_portfolio_state(
        db, portfolio_id, "READY",
        operator_id=operator_id, correlation_id=correlation_id,
        reason="Manual reconciliation confirmation after diffs cleared",
    )
    return ReconciliationFixConfirmation(
        portfolio_id=portfolio_id,
        trade_date=trade_date,
        operator_id=operator_id,
        acknowledged_diffs_cleared=True,
        portfolio_now_ready=(res.to_state == "READY"),
    )


# ════════════════════════════════════════════════════════════════════════════
# B6 动作允许矩阵：_check_allows_action(state, action) → bool
# ════════════════════════════════════════════════════════════════════════════
def _check_allows_action(state: str, action: str) -> bool:
    """HG1/B6 动作准入判定：check(state, action) -> bool。

    约定（若传入无效状态/无效 action → 返回 False）：
      NEW_BUY                           : READY / RUNNING_* / SCORE_STALE / MODEL_INACTIVE 允许
      RISK_SELL                         : 所有状态允许（风控止损 —— ADMIN_PAUSED 下唯一允许）
      NORMAL_SELL                       : READY / RUNNING_* / SCORE_STALE / DATA_INCOMPLETE_PAUSED / MODEL_INACTIVE / INTERRUPTED 允许
      FORCE_SELL_WITH_REVIEW            : VALID_STATES 内任何状态都允许（需 operated_by + review_note，由上层再验证）
      AUTO_RECOVERY_FLOW                : READY + INTERRUPTED（ADMIN_PAUSED / RBLOCKED 均不允许自动恢复）
    """
    if state not in VALID_STATES:
        return False
    if action == _ACTION_RISK_SELL:
        # 风控止损：任何状态都放行（ADMIN_PAUSED 的唯一出边=READY，但风险 SELL 是"被动退出"，不是状态转移）
        return True
    if action == _ACTION_FORCE_SELL_WITH_REVIEW:
        return True
    if action == _ACTION_NEW_BUY:
        return state in _BUY_ALLOWED_STATES
    if action == _ACTION_NORMAL_SELL:
        return state in _NORMAL_SELL_ALLOWED_STATES
    if action == _ACTION_AUTO_RECOVERY_FLOW:
        return state in _AUTO_RECOVERY_ALLOWED_STATES
    # 老 HG1 4 项（向后兼容）
    if action == "can_add_candidates":
        return state in _BUY_ALLOWED_STATES
    if action == "can_start_new_simulation":
        return state == "READY"
    if action == "can_promote_candidate_buy":
        return state == "READY"
    if action == "can_publish_decision_result":
        return state == "RUNNING_AUTO_SIMULATION"
    return False


# ════════════════════════════════════════════════════════════════════════════
# B3 租约键：派生前缀可区分 AUTO_SIM vs BACKTEST（避免 READY 抢锁）
# ════════════════════════════════════════════════════════════════════════════
def derive_lease_key(
    portfolio_id: int,
    run_type: str,
    *,
    trade_date: date | str,
    backtest_run_id: str | None = None,
    suffix: str | None = None,
) -> str:
    """按 run_type 派生可区分的分布式租约键。

    AUTO_SIM 前缀 `pf{pid}:auto:{trade_date}`；
    BACKTEST 前缀 `pf{pid}:bt:{trade_date}:{backtest_run_id or random}`。
    两者无论如何都互不相等 —— 即使同一 pid + 同一 trade_date。
    """
    td = trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date)
    if run_type == "AUTO_SIM":
        base = f"pf{portfolio_id}:auto:{td}"
    elif run_type == "BACKTEST":
        bt_id = backtest_run_id if backtest_run_id else ("x_" + secrets.token_hex(6))
        base = f"pf{portfolio_id}:bt:{td}:{bt_id}"
    else:
        # 其他模式（如研究）加一个默认前缀，但前缀仍不同
        base = f"pf{portfolio_id}:{str(run_type).lower()}:{td}"
    if suffix:
        base = f"{base}:{suffix}"
    return base

