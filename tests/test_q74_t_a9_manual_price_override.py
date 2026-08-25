"""T-A9 Q7.4：数据阻断人工处理单测。3 核心用例：
  1. T-A9.1 confirm_manual_price → evidence 标 manual_price_flag=True，manual_executable_price 注入
  2. T-A9.2 keep_paused → 维持 DATA_BLOCKED（action_subtype 仍 DATA_BLOCKED），manual_price_flag=False
  3. T-A9.3 continue_forward → 原 DATA_BLOCKED 改 HOLD + MANUALLY_SKIPPED，manual_price_flag=True

纯单元（不依赖 DB migration，直接调用 _default_build_evidence + mock context/price_data）
"""
from __future__ import annotations

import pytest
from datetime import date
from typing import Any


class _FakeClock:
    def __init__(self):
        from datetime import datetime
        self.decision_at = datetime(2025, 7, 1, 15, 0, 0)
        self.data_cutoff_at = datetime(2025, 7, 1, 15, 0, 0)
        self.execution_at = datetime(2025, 7, 2, 9, 30, 0)


class _FakeSnap:
    def __init__(self):
        self.versions: dict[str, Any] = {"run_mode": "research", "pit_mode": "best_effort"}
        self.gate_policy_version = "production-v1.0.0"
        self.cost_config: dict[str, Any] = {
            "slippage_buy_bps": 5, "slippage_sell_bps": 5, "min_lot_size": 100,
        }


class _FakeScored:
    def __init__(self, items):
        self.items = items
        self.coverage_pct = 100.0
        self.max_age_days = 0
        self.expected = len(items)
        self.actual = len(items)


class _FakeAlloc:
    def __init__(self, items):
        self.items = items


def _make_evidence_with_ta9(
    *,
    price_data_by_symbol: dict[int, dict[str, Any]] | None,
    trade_date: date,
    manual_overrides_context: dict[str, Any] | None,
    universe_list=None,
    blocking_status="READY",
    blocking_reasons=None,
):
    """调用 _default_build_evidence；默认 1 个 symbol universe"""
    from app.services.decision_engine import _default_build_evidence

    snap = _FakeSnap()
    clock = _FakeClock()  # type: ignore[assignment]
    sym_id = 1001
    if universe_list is None:
        universe_member = {
            "symbol_id": sym_id,
            "current_quantity": 200.0,
            "current_position_pct": 0.1,
        }
        final_universe_list = [universe_member]
    else:
        final_universe_list = list(universe_list)
        universe_member = final_universe_list[0]
        sym_id = universe_member.get("symbol_id", 1001)

    class Universe:
        universe_count = len(final_universe_list)
        member_count = len(final_universe_list)
        universe = final_universe_list

    score_item = {
        "symbol_id": sym_id,
        "score_id": 55,
        "score_value": 88.5,
        "score_rank": 1,
    }
    scored = _FakeScored([score_item])  # type: ignore[arg-type]
    alloc_item = {
        "symbol_id": sym_id,
        "direction": "HOLD",
        "target_position_pct": float(universe_member.get("current_position_pct", 0)),
        "target_quantity": float(universe_member.get("current_quantity", 0.0)),
        "min_lot_size": 100,
    }
    alloc = _FakeAlloc([alloc_item])  # type: ignore[arg-type]

    signal = type("SignalResult", (), {"items": []})()

    return _default_build_evidence(
        snap=snap,  # type: ignore[arg-type]
        clock=clock,  # type: ignore[arg-type]
        universe=Universe(),  # type: ignore[arg-type]
        scored=scored,  # type: ignore[arg-type]
        signal=signal,  # type: ignore[arg-type]
        alloc=alloc,  # type: ignore[arg-type]
        blocking_status=blocking_status,
        blocking_reasons=list(blocking_reasons or []),
        price_data_by_symbol=price_data_by_symbol,
        trade_date=trade_date,
        match_mode="NEXT_OPEN",
        manual_overrides_context=manual_overrides_context,
    )


class TestTA9BuildEvidence:
    @pytest.fixture
    def trade_date(self):
        return date(2025, 7, 1)

    @pytest.fixture
    def symbol_id(self):
        return 1001

    # ── T-A9.1：confirm_manual_price → manual_price_flag=True，_override_applied
    def test_ta9_1_confirm_manual_price_flags_evidence(self, trade_date, symbol_id):
        ov = type("Ov", (), {
            "id": 42,
            "symbol_id": symbol_id,
            "resolved_mode": "confirm_manual_price",
            "manual_executable_price": 18.66,
        })()
        price_data = {
            symbol_id: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None, "volume": 0,
                "is_suspended_today": False,
                "_ta9_override_applied": True,
                "_ta9_override_id": 42,
            },
        }
        ctx = {
            "per_symbol": {symbol_id: ov},
            "portfolio_wide": None,
            "applied_override_ids": [],
        }
        evs = _make_evidence_with_ta9(
            price_data_by_symbol=price_data,
            trade_date=trade_date,
            manual_overrides_context=ctx,
        )
        assert len(evs) == 1
        ev = evs[0]
        assert ev.manual_price_flag is True, "confirm_manual_price → manual_price_flag MUST=1"
        assert ev.manual_price_override_id == 42
        assert 42 in ctx["applied_override_ids"], "override_id 要登记为已消费"

    # ── T-A9.2：keep_paused → 维持 DATA_BLOCKED 原样，不消费，manual_price_flag=False
    def test_ta9_2_keep_paused_stays_data_blocked_unmodified(self, trade_date, symbol_id):
        # 构造 data_blocked 场景：OHLCV 全 None/0，is_suspended=False → DATA_BLOCKED
        price_data = {
            symbol_id: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None, "volume": 0,
                "is_suspended_today": False,
            },
        }
        ov = type("Ov", (), {
            "id": 17,
            "symbol_id": symbol_id,
            "resolved_mode": "keep_paused",
            "manual_executable_price": None,
        })()
        ctx = {
            "per_symbol": {symbol_id: ov},
            "portfolio_wide": None,
            "applied_override_ids": [],
        }
        evs = _make_evidence_with_ta9(
            price_data_by_symbol=price_data,
            trade_date=trade_date,
            manual_overrides_context=ctx,
        )
        ev = evs[0]
        # data_blocked 时 action=HOLD, action_subtype=DATA_BLOCKED（T-A7 规范）
        assert ev.action == "HOLD"
        assert ev.action_subtype == "DATA_BLOCKED", (
            "keep_paused 不允许改 action_subtype，必须保持阻断状态 DATA_BLOCKED"
        )
        assert ev.manual_price_flag is False, "keep_paused → manual_price_flag MUST=0"
        assert ev.manual_price_override_id is None
        assert 17 not in ctx["applied_override_ids"], "keep_paused 不消费 consumed_flag"

    # ── T-A9.3：continue_forward → 原 DATA_BLOCKED → HOLD + MANUALLY_SKIPPED
    def test_ta9_3_continue_forward_changes_to_hold_manually_skipped(self, trade_date, symbol_id):
        price_data = {
            symbol_id: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None, "volume": 0,
                "is_suspended_today": False,
            },
        }
        ov = type("Ov", (), {
            "id": 9,
            "symbol_id": None,  # portfolio_wide_override 批量模式
            "resolved_mode": "continue_forward",
            "manual_executable_price": None,
        })()
        ctx = {
            "per_symbol": {},
            "portfolio_wide": ov,
            "applied_override_ids": [],
        }
        evs = _make_evidence_with_ta9(
            price_data_by_symbol=price_data,
            trade_date=trade_date,
            manual_overrides_context=ctx,
        )
        ev = evs[0]
        assert ev.action == "HOLD"
        assert ev.action_subtype == "MANUALLY_SKIPPED", (
            f"continue_forward → subtype MUST=MANUALLY_SKIPPED；实际={ev.action_subtype!r}"
        )
        assert ev.manual_price_flag is True
        assert ev.manual_price_override_id == 9
        # zero-delta：不调仓
        assert ev.target_qty_delta == 0.0
        assert ev.executed_price is None, "MANUALLY_SKIPPED 不猜价"
        assert 9 in ctx["applied_override_ids"], "continue_forward 需消费 consumed_flag"
        # reason code 里必须含 MANUALLY_SKIPPED，不含原 DATA_BLOCKED
        assert "MANUALLY_SKIPPED" in (ev.reason_codes or [])
        assert "DATA_BLOCKED" not in (ev.reason_codes or [])


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
