"""portfolio-factor-backtest-full-linkage 阻塞点 #3：五维守恒 + 差异统一 BLOCKED（RED TDD 先失败）。

现有 portfolio_reconciliation.py 已实现 4 类差异（R1-R4），但 pf-linkage spec 要求严格
「五维守恒矩阵」+「任何差异统一 RECONCILIATION_BLOCKED」：

五维 = Decision 预期 × 订单计划 × 撮合结果 × 持仓快照 × 现金/NAV
任何一维有 diff → status=BLOCKED → 触发 RECONCILIATION_BLOCKED FSM 状态（见 linkage #2）。

Seam（纯函数，不依赖 Session / DB）：
    from app.services.reconciliation_conservation import (
        FiveVectorInput,
        FiveVectorConservationReport,
        evaluate_five_vector_conservation(input: FiveVectorInput) -> FiveVectorConservationReport,
    )

    FiveVectorInput(
        trade_date: date,
        start_cash: float,
        end_cash: float,
        start_positions: dict[symbol_id, qty_float],   # T-1 收盘持仓（= T 日开盘持仓）
        decisions: list[DecisionLeg],                  # T 日决策 BUY/SELL + 目标价/量
        order_plans: list[OrderPlanLeg],                # T 日订单计划（可能无 = MISSING_ORDER_PLAN）
        match_results: list[MatchLeg],                  # 撮合结果：FILLED / PARTIAL / REJECTED
        end_positions: dict[symbol_id, qty_float],      # T 日收盘后最终持仓快照（= T+1 开盘）
        end_prices: dict[symbol_id, close_price],       # T 日收盘价（用于 NAV）
        tolerance: float = 1e-6,
    )

    DecisionLeg(symbol_id, action in {BUY,SELL,HOLD}, target_qty, target_price_ceiling_or_floor)
    OrderPlanLeg(symbol_id, side, plan_qty, limit_price, plan_id)
    MatchLeg(order_plan_id, symbol_id, side, filled_qty, avg_price, match_status in {FILLED,PARTIAL_FILL,REJECTED})

    FiveVectorConservationReport(
        overall: Literal["PASSED","BLOCKED"],
        diffs: list[ConservationDiff],   # 每条 diff 明确 source_vector + kind
        nav_start: float, nav_end: float,  # 便于 T+1 与 T 连续追踪
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import pytest


# ── 本地构造输入数据的 fixture helper（不依赖 seam 存在与否）───────────────────────

@dataclass
class DecisionLegFx:
    symbol_id: int; action: Literal["BUY","SELL","HOLD"]; target_qty: float; price_ref: float

@dataclass
class OrderPlanFx:
    order_plan_id: str; symbol_id: int; side: Literal["BUY","SELL"]; plan_qty: float; limit_price: float

@dataclass
class MatchFx:
    order_plan_id: str; symbol_id: int; side: Literal["BUY","SELL"]
    filled_qty: float; avg_price: float; status: Literal["FILLED","PARTIAL_FILL","REJECTED"]


def _build_case(
    *,
    start_cash=100_000.0,
    start_positions=None,
    decisions=None,
    order_plans=None,
    match_results=None,
    end_positions=None,
    end_cash=None,
    end_prices=None,
    **extra,
):
    # NOTE: **extra 吸收 legacy / 调试用 extra kwargs（如 start_price_implied），
    # 使调用处的显式 `del d["start_price_implied"]` 语义生效，避免构造期 TypeError。
    out = {
        "trade_date": date(2026, 1, 6),
        "start_cash": start_cash,
        "end_cash": start_cash if end_cash is None else end_cash,
        "start_positions": start_positions or {},
        "decisions": decisions or [],
        "order_plans": order_plans or [],
        "match_results": match_results or [],
        "end_positions": end_positions or {},
        "end_prices": end_prices or {},
    }
    out.update(extra)
    return out


class TestLinkage3FiveVectorContract:
    """五维守恒主契约：完全一致场景 → PASSED。"""

    def test_linkage_3_case_pass_perfect_fill_single_buy(self):
        """
        简单 BUY 场景：
        start_cash=100_000; start_pos={};
        Decision BUY 100@10.0 → OrderPlan BUY 100@10.0 → Match FILLED 100@10.0;
        end_pos={1:100}; end_cash=100_000 - 100*10 = 99_000; end_price={1:10.1}（NAV 上升不用对守恒负责）
        → 应 PASSED（0 diffs）
        """
        from app.services.reconciliation_conservation import evaluate_five_vector_conservation
        d = _build_case(
            decisions=[DecisionLegFx(1, "BUY", 100, 10.0)],
            order_plans=[OrderPlanFx("p1", 1, "BUY", 100, 10.0)],
            match_results=[MatchFx("p1", 1, "BUY", 100, 10.0, "FILLED")],
            end_positions={1: 100},
            end_cash=100_000 - 100 * 10.0,
            end_prices={1: 10.1},
        )
        report = evaluate_five_vector_conservation(d)
        assert report.overall == "PASSED"
        assert len(report.diffs) == 0

    def test_linkage_3_case_hold_all_passed(self):
        """无决策一天：HOLD → 0 订单/撮合；首尾持仓相同；现金不变 → PASSED。"""
        from app.services.reconciliation_conservation import evaluate_five_vector_conservation
        d = _build_case(
            start_positions={7: 200},
            decisions=[DecisionLegFx(7, "HOLD", 200, 0.0)],
            order_plans=[],
            match_results=[],
            end_positions={7: 200},
            end_prices={7: 15.0},
        )
        report = evaluate_five_vector_conservation(d)
        assert report.overall == "PASSED"
        assert len(report.diffs) == 0


class TestLinkage3DecisionVsOrder:
    """R1 / Vector 1 vs 2：有 BUY/SELL 决策但无订单计划 → MISSING_ORDER_PLAN → BLOCKED。"""

    def test_linkage_3_r1_missing_order_plan_for_buy_blocks(self):
        from app.services.reconciliation_conservation import evaluate_five_vector_conservation
        d = _build_case(
            decisions=[DecisionLegFx(1, "BUY", 100, 10.0)],
            order_plans=[],  # 没下单
            match_results=[],
            end_positions={},
            end_cash=100_000,
            end_prices={},
        )
        report = evaluate_five_vector_conservation(d)
        assert report.overall == "BLOCKED"
        kinds = {diff.kind for diff in report.diffs}
        assert "MISSING_ORDER_PLAN" in kinds

    def test_linkage_3_r1_hold_action_allows_zero_orders(self):
        """HOLD 动作本就不该有订单计划 → 不应该误报 MISSING_ORDER_PLAN。"""
        from app.services.reconciliation_conservation import evaluate_five_vector_conservation
        d = _build_case(
            decisions=[DecisionLegFx(1, "HOLD", 0, 0.0)],
            order_plans=[],
            match_results=[],
            end_positions={},
            end_prices={},
        )
        report = evaluate_five_vector_conservation(d)
        # 只要 diff 里不含 MISSING_ORDER_PLAN 即正确（其他 HOLD 语义 diff 如预期/实际持仓不对另算，但这里都是 0）
        kinds = {diff.kind for diff in report.diffs}
        assert "MISSING_ORDER_PLAN" not in kinds


class TestLinkage3OrderVsMatch:
    """R2 / Vector 2 vs 3：有订单但撮合 0 成交（非 REJECTED）→ UNFILLED_PLAN。"""

    def test_linkage_3_r2_unfilled_plan_blocks(self):
        from app.services.reconciliation_conservation import evaluate_five_vector_conservation
        d = _build_case(
            decisions=[DecisionLegFx(1, "BUY", 100, 10.0)],
            order_plans=[OrderPlanFx("p1", 1, "BUY", 100, 10.0)],
            match_results=[MatchFx("p1", 1, "BUY", 0, 0.0, "PARTIAL_FILL")],  # 0 成交
            end_positions={},
            end_cash=100_000,
            end_prices={},
        )
        report = evaluate_five_vector_conservation(d)
        assert report.overall == "BLOCKED"
        kinds = {diff.kind for diff in report.diffs}
        assert "UNFILLED_PLAN" in kinds

    def test_linkage_3_r2_rejected_legitimate_no_fill_not_unfilled(self):
        """REJECTED 显式拒绝不算 UNFILLED_PLAN（拒绝是另一个 diff：预期 vs 实际动作，这里 UNFILLED 特指没撮合结果）。"""
        from app.services.reconciliation_conservation import evaluate_five_vector_conservation
        d = _build_case(
            decisions=[DecisionLegFx(1, "BUY", 100, 10.0)],
            order_plans=[OrderPlanFx("p1", 1, "BUY", 100, 10.0)],
            match_results=[MatchFx("p1", 1, "BUY", 0, 0.0, "REJECTED")],
            end_positions={},
            end_prices={},
        )
        report = evaluate_five_vector_conservation(d)
        kinds = {diff.kind for diff in report.diffs}
        assert "UNFILLED_PLAN" not in kinds


class TestLinkage3MatchVsPosition:
    """R3 / Vector 3 vs 4：撮合 100 BUY 但最终持仓 0 → POSITION_MISMATCH。"""

    def test_linkage_3_r3_position_mismatch(self):
        from app.services.reconciliation_conservation import evaluate_five_vector_conservation
        d = _build_case(
            decisions=[DecisionLegFx(1, "BUY", 100, 10.0)],
            order_plans=[OrderPlanFx("p1", 1, "BUY", 100, 10.0)],
            match_results=[MatchFx("p1", 1, "BUY", 100, 10.0, "FILLED")],
            end_positions={1: 0},  # 应该有 100，这里少了
            end_cash=100_000 - 100 * 10.0,
            end_prices={1: 10.0},
        )
        report = evaluate_five_vector_conservation(d)
        assert report.overall == "BLOCKED"
        kinds = {diff.kind for diff in report.diffs}
        assert "POSITION_MISMATCH" in kinds


class TestLinkage3NavConservation:
    """R4 / NAV 守恒：start_cash + Σ start_pos×start_price ≠ end_cash + Σ end_pos×end_price - 撮合净现金流 → NAV_BROKEN。

    为纯函数不要求 start_prices 参数，默认 start_price 对于新成员用 BUY MatchLeg.avg_price，
    对于已有持仓成员则使用其 end_price（或允许 tolerance=1e-6 时 start_pos 对称相等直接不影响 NAV）。
    """

    def test_linkage_3_r4_nav_broken_if_cash_missing(self):
        """持仓正确（买了 100 股都在）但现金少付了 100 元 → NAV_BROKEN。"""
        from app.services.reconciliation_conservation import evaluate_five_vector_conservation
        d = _build_case(
            start_cash=100_000,
            start_positions={7: 500}, start_price_implied={7: 20.0},  # start_pos 用 end_price 推断（为 19 也行，只要 start*qty=10000）
            decisions=[DecisionLegFx(1, "BUY", 100, 10.0)],
            order_plans=[OrderPlanFx("p1", 1, "BUY", 100, 10.0)],
            match_results=[MatchFx("p1", 1, "BUY", 100, 10.0, "FILLED")],
            end_positions={7: 500, 1: 100},
            end_cash=100_000 - 100 * 10.0 - 100,  # 少了 100
            end_prices={7: 20.0, 1: 10.0},
        )
        # 为了把 start_price_implied 这类 extra kwarg 去掉，我们只通过正式 FiveVectorInput 的关键字
        del d["start_price_implied"]
        report = evaluate_five_vector_conservation(d)
        assert report.overall == "BLOCKED"
        kinds = {diff.kind for diff in report.diffs}
        assert "NAV_BROKEN" in kinds

    def test_linkage_3_r4_nav_ok_within_tolerance(self):
        """买完后 end_price 上浮到 10.1 → NAV 更高，但守恒式仍然满足（NAV 本可以增长），所以应 PASSED。"""
        from app.services.reconciliation_conservation import evaluate_five_vector_conservation
        d = _build_case(
            start_cash=100_000,
            start_positions={7: 500},
            decisions=[DecisionLegFx(1, "BUY", 100, 10.0)],
            order_plans=[OrderPlanFx("p1", 1, "BUY", 100, 10.0)],
            match_results=[MatchFx("p1", 1, "BUY", 100, 10.0, "FILLED")],
            end_positions={7: 500, 1: 100},
            end_cash=100_000 - 100 * 10.0,
            end_prices={7: 20.0, 1: 10.1},  # 1 号涨了 1%
        )
        report = evaluate_five_vector_conservation(d)
        # 除了 HOLD for 7 这种非 BUY/SELL/HOLD 对决策维度的可能不匹配外，不能有 NAV_BROKEN
        kinds = {diff.kind for diff in report.diffs}
        assert "NAV_BROKEN" not in kinds
        assert report.overall == "PASSED"
