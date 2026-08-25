"""portfolio-factor-backtest-full-linkage 阻塞点 #2：扩展状态机 FSM（RED TDD 先失败）。

现有 portfolio_state_machine.py 已实现 7 种状态 + transition_portfolio_state，但还缺：
  - 新的 FSM 规范状态（N1~N9 共 10 种，补齐 MODEL_INACTIVE、DATA_INCOMPLETE_PAUSED 等）
  - 租约键：自动模拟 ≠ 回测，同一组合允许 READY + 后台 RUNNING_BACKTEST 同时存在
  - 纯函数 Seam：portfolio_fsm 负责决定"状态转移是否允许 + 返回租约键 + 组合当前运行层语义"

Seam（纯函数）：
    from app.services.portfolio_fsm import (
        ExtendedPortfolioState,           # 10 种 Literal 枚举字符串
        can_transition_extended(
            from_state: ExtendedPortfolioState,
            to_state: ExtendedPortfolioState,
        ) -> bool
        derive_lease_key(
            portfolio_id: int,
            running_state: Literal["RUNNING_AUTOSIMULATION", "RUNNING_BACKTEST"],
            trade_date: date | None = None,
        ) -> str
        # 租约隔离验证：AUTO_SIM vs BACKTEST 键必须互相不可见（前缀不同 → 互斥租约）
        state_running_layer(s: ExtendedPortfolioState) -> Literal[None, "AUTO", "BACKTEST"]
    )

Seam 要求（对应 pf-linkage tasks.md N1~N10）：
  - N1: PENDING_INITIAL_REVIEW 必须存在；首次审查通过 → READY
  - N2: READY 是唯一 HG1 生产就绪状态（可回测/可模拟/可预检）
  - N3: RUNNING_AUTOSIMULATION（原 RUNNING_AUTO_SIMULATION），租约前缀 = lease:au
  - N4: RUNNING_BACKTEST，租约前缀 = lease:bt；允许和 READY "视角状态"同时存在
  - N5: DATA_INCOMPLETE_PAUSED（数据缺失/HEAVY → 自动暂停新买单，保留风险退出）
  - N6: RECONCILIATION_BLOCKED（对账差异）
  - N7: MODEL_INACTIVE（绑定模型退役/未激活）
  - N8: ADMIN_PAUSED（人工暂停）
  - N9: INTERRUPTED（异常中断）
"""
from __future__ import annotations

from datetime import date

import pytest


class TestLinkage2ExtendedStateLiteralEnum:
    """N1~N9：ExtendedPortfolioState 必须包含全部 10 种状态。"""

    def test_linkage_2_extended_state_contains_10_named_literals(self):
        from app.services.portfolio_fsm import ExtendedPortfolioState
        assert ExtendedPortfolioState is not None
        # Literal[str] 的 __args__：顺序不敏感，集合比较
        try:
            actual_states = set(ExtendedPortfolioState.__args__)
        except AttributeError:
            # 如果实现为 Enum，则取 ._value2member_map_ 或成员 value
            actual_states = {s.value for s in ExtendedPortfolioState}  # type: ignore[union-attr]
        expected = {
            "PENDING_INITIAL_REVIEW",
            "READY",
            "RUNNING_AUTOSIMULATION",
            "RUNNING_BACKTEST",
            "DATA_INCOMPLETE_PAUSED",
            "RECONCILIATION_BLOCKED",
            "MODEL_INACTIVE",
            "INTERRUPTED",
            "ADMIN_PAUSED",
            # 至少 10；另外可选 DEGRADED_READY 研究模式不阻塞等
        }
        assert expected.issubset(actual_states), (
            f"missing states: {sorted(expected - actual_states)}; "
            f"have: {sorted(actual_states)}"
        )
        assert len(actual_states) >= 10


class TestLinkage2TransitionMatrix:
    """N1~N9 状态转移矩阵 can_transition_extended。"""

    @staticmethod
    def _c(f, t):
        from app.services.portfolio_fsm import can_transition_extended
        return can_transition_extended(f, t)

    def test_linkage_2_n1_initial_review_only_ready(self):
        """PENDING_INITIAL_REVIEW 只能 → READY，不能 → 任何 RUNNING。"""
        assert self._c("PENDING_INITIAL_REVIEW", "READY") is True
        for bad in ("RUNNING_AUTOSIMULATION", "RUNNING_BACKTEST",
                    "DATA_INCOMPLETE_PAUSED", "RECONCILIATION_BLOCKED"):
            assert self._c("PENDING_INITIAL_REVIEW", bad) is False

    def test_linkage_2_n2_ready_can_go_autosim_or_backtest(self):
        """READY → 模拟 / 回测 都允许（两个 RUNNING 前缀相互独立）。"""
        assert self._c("READY", "RUNNING_AUTOSIMULATION") is True
        assert self._c("READY", "RUNNING_BACKTEST") is True
        assert self._c("READY", "DATA_INCOMPLETE_PAUSED") is True
        assert self._c("READY", "MODEL_INACTIVE") is True

    def test_linkage_2_n3_n4_running_return_ready_or_blocked_or_interrupted(self):
        for s in ("RUNNING_AUTOSIMULATION", "RUNNING_BACKTEST"):
            assert self._c(s, "READY") is True
            assert self._c(s, "RECONCILIATION_BLOCKED") is True
            assert self._c(s, "INTERRUPTED") is True
            # ← 两个 RUNNING 之间不能互相跳（租约不同）
            other = "RUNNING_BACKTEST" if s == "RUNNING_AUTOSIMULATION" else "RUNNING_AUTOSIMULATION"
            assert self._c(s, other) is False

    def test_linkage_2_n5_data_incomplete_paused_ready_only_roundtrip(self):
        assert self._c("DATA_INCOMPLETE_PAUSED", "READY") is True
        # DATA_INCOMPLETE_PAUSED → 模拟/回测 都不行（缺数据不允许开跑）
        assert self._c("DATA_INCOMPLETE_PAUSED", "RUNNING_AUTOSIMULATION") is False
        assert self._c("DATA_INCOMPLETE_PAUSED", "RUNNING_BACKTEST") is False

    def test_linkage_2_n6_reconciliation_blocked_only_to_ready_via_confirm(self):
        assert self._c("RECONCILIATION_BLOCKED", "READY") is True
        assert self._c("RECONCILIATION_BLOCKED", "RUNNING_AUTOSIMULATION") is False
        assert self._c("RECONCILIATION_BLOCKED", "RUNNING_BACKTEST") is False

    def test_linkage_2_n7_model_inactive_roundtrip(self):
        assert self._c("MODEL_INACTIVE", "READY") is True
        assert self._c("READY", "MODEL_INACTIVE") is True
        # MODEL_INACTIVE 不能直接跑
        assert self._c("MODEL_INACTIVE", "RUNNING_AUTOSIMULATION") is False

    def test_linkage_2_n8_admin_paused_any_entry_only_exit_to_ready(self):
        """ADMIN_PAUSED：任何状态都能进（*→ADMIN_PAUSED），出来只能→READY。"""
        for s in ("PENDING_INITIAL_REVIEW", "READY", "RUNNING_BACKTEST",
                  "DATA_INCOMPLETE_PAUSED", "MODEL_INACTIVE", "INTERRUPTED",
                  "RECONCILIATION_BLOCKED"):
            assert self._c(s, "ADMIN_PAUSED") is True, f"{s}→ADMIN_PAUSED should be allowed"
        assert self._c("ADMIN_PAUSED", "READY") is True
        for bad in ("RUNNING_AUTOSIMULATION", "RUNNING_BACKTEST",
                    "RECONCILIATION_BLOCKED", "PENDING_INITIAL_REVIEW"):
            assert self._c("ADMIN_PAUSED", bad) is False

    def test_linkage_2_n9_interrupted_retry_to_ready(self):
        assert self._c("INTERRUPTED", "READY") is True
        assert self._c("INTERRUPTED", "RUNNING_AUTOSIMULATION") is False


class TestLinkage2LeaseKeys:
    """N3/N4 租约键隔离：AUTO vs BACKTEST 前缀不同 + trade_date 可选。"""

    def test_linkage_2_lease_auto_vs_backtest_different_prefixes(self):
        from app.services.portfolio_fsm import derive_lease_key
        k_auto = derive_lease_key(123, "RUNNING_AUTOSIMULATION", date(2026, 8, 18))
        k_bt = derive_lease_key(123, "RUNNING_BACKTEST", date(2026, 8, 18))
        assert k_auto != k_bt
        # 前缀契约：au = auto sim / bt = backtest
        assert "au" in k_auto.lower() or "auto" in k_auto.lower()
        assert "bt" in k_bt.lower() or "backtest" in k_bt.lower()

    def test_linkage_2_same_running_type_different_trade_date_different_keys(self):
        """相同 RUNNING 类型不同交易日 → 不同租约键（按日串行执行）。"""
        from app.services.portfolio_fsm import derive_lease_key
        k1 = derive_lease_key(123, "RUNNING_BACKTEST", date(2026, 8, 17))
        k2 = derive_lease_key(123, "RUNNING_BACKTEST", date(2026, 8, 18))
        assert k1 != k2

    def test_linkage_2_trade_date_optional_for_autosim_same_day(self):
        """AUTO_SIM 租约键不传 trade_date（用默认）时，不同日期应不同（impl 用 today）。"""
        from app.services.portfolio_fsm import derive_lease_key
        # 传 vs 不传都得稳定（不能抛异常）；无参版应稳定（同一 portfolio 同一值对相同 pid/run_type 返回稳定）
        k_a = derive_lease_key(123, "RUNNING_AUTOSIMULATION")
        k_b = derive_lease_key(123, "RUNNING_AUTOSIMULATION")
        assert isinstance(k_a, str) and k_a == k_b


class TestLinkage2RunningLayers:
    """coexistence 契约：RUNNING_AUTOSIMULATION=AUTO 层、RUNNING_BACKTEST=BACKTEST 层，其他=None。"""

    def test_linkage_2_layer_mapping(self):
        from app.services.portfolio_fsm import state_running_layer
        assert state_running_layer("RUNNING_AUTOSIMULATION") == "AUTO"
        assert state_running_layer("RUNNING_BACKTEST") == "BACKTEST"
        for s in ("PENDING_INITIAL_REVIEW", "READY", "DATA_INCOMPLETE_PAUSED",
                  "RECONCILIATION_BLOCKED", "MODEL_INACTIVE", "ADMIN_PAUSED",
                  "INTERRUPTED"):
            assert state_running_layer(s) is None
