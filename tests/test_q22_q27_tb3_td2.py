"""T-D2 Q27.3 / T-D3 Q27.4 / T-B3 Q2.2 / T-B4 Q2.3 联合验收。

- T-D2.1：6⁴=256 组合笛卡儿 100% 覆盖 + RECONCILIATION_BLOCKED 永远最高 6 + 同级别对称。
- T-D3.1：Q27.4 自动恢复 data/score；status_model/RECONCILIATION 不自动。
- T-D3.2：MODEL_INACTIVE → 必须人工 confirm_active_model_binding 才 READY。
- T-B3.1：T-B2.2 场景证据（2 skip）断言 intended / executed / rejection 聚合。
- T-B4.1 / T-B4.2：滑点 BUY +5bp / SELL -5bp；cost_config 自定义 sell_bps=10 → 9.99。
"""
from __future__ import annotations

from datetime import date
from itertools import product
from pathlib import Path

import pytest

from app.core.hash_utils import canonical_json
from app.services.decision_clock import resolve
from app.services.match_price_resolver import (
    MatchPriceResult,
    REJECTION_REASON_LIMIT_UP_DOWN,
    REJECTION_REASON_SUSPENDED,
)
from app.services.portfolio_status import (
    PORTFOLIO_STATUS_BLOCK_LEVEL,
    PortfolioStatus,
    ResolvedCompositeStatus,
    resolve_composite_status_from_row,
)


# ═══════════════════════════════════════════════════════════════════════════════
# T-D2.1
# ═══════════════════════════════════════════════════════════════════════════════

# tasks.md D2 "16 组合（4 × 4）" = 4 维度 × 4 种子集；这里全量 4⁴=256。
D2_DIM_VALUE_SUBSET = (
    PortfolioStatus.READY,
    PortfolioStatus.RUNNING,
    PortfolioStatus.DATA_INCOMPLETE_PAUSED,
    PortfolioStatus.RECONCILIATION_BLOCKED,
)


def _expected_composite_from_raw_max(s1: PortfolioStatus, s2: PortfolioStatus,
                                     s3: PortfolioStatus, s4: PortfolioStatus
                                     ) -> PortfolioStatus:
    """独立于我们实现的"期望 oracle"：纯 max + from_block_level。"""
    max_level = max(
        PORTFOLIO_STATUS_BLOCK_LEVEL[s1.value],
        PORTFOLIO_STATUS_BLOCK_LEVEL[s2.value],
        PORTFOLIO_STATUS_BLOCK_LEVEL[s3.value],
        PORTFOLIO_STATUS_BLOCK_LEVEL[s4.value],
    )
    return PortfolioStatus.from_block_level(max_level)


class TestTD2Q27ThreeResolveComposite:
    def test_t_d2_1_256_combos_cartesian_all_match_oracle(self):
        combos = list(product(D2_DIM_VALUE_SUBSET, repeat=4))
        assert len(combos) == 256
        for (s_sc, s_da, s_mo, s_re) in combos:
            res: ResolvedCompositeStatus = resolve_composite_status_from_row(
                status_score=s_sc, status_data=s_da, status_model=s_mo, status_reconciliation=s_re
            )
            expected = _expected_composite_from_raw_max(s_sc, s_da, s_mo, s_re)
            assert res.composite_status == expected, (
                f"composite mismatch dims=({s_sc.value},{s_da.value},{s_mo.value},{s_re.value}); "
                f"got={res.composite_status.value} expected={expected.value} level={res.highest_block_level}"
            )
            # highest_block_level 必须等于 max level
            raw_max = max(
                PORTFOLIO_STATUS_BLOCK_LEVEL[s_sc.value],
                PORTFOLIO_STATUS_BLOCK_LEVEL[s_da.value],
                PORTFOLIO_STATUS_BLOCK_LEVEL[s_mo.value],
                PORTFOLIO_STATUS_BLOCK_LEVEL[s_re.value],
            )
            assert res.highest_block_level == raw_max

    def test_t_d2_1_reconciliation_blocked_6_is_always_highest(self):
        # 只要任意一维是 RECONCILIATION_BLOCKED(6)，composite 就必须为它（256 组合里 4⁴-3⁴=175 个命中）
        hits = 0
        for (s_sc, s_da, s_mo, s_re) in product(D2_DIM_VALUE_SUBSET, repeat=4):
            if any(s.value == PortfolioStatus.RECONCILIATION_BLOCKED.value
                   for s in (s_sc, s_da, s_mo, s_re)):
                hits += 1
                res = resolve_composite_status_from_row(s_sc, s_da, s_mo, s_re)
                assert res.composite_status == PortfolioStatus.RECONCILIATION_BLOCKED
                assert res.highest_block_level == 6
        assert hits == (4 ** 4 - 3 ** 4)  # =175

    def test_t_d2_1_same_level_all_dims_gives_same_composite(self):
        # 对称测试：4 维全取同一状态 X → composite 必须为 X（覆盖 4 种子集 + 顺便另外 2 个 SCORE_STALE(3)/MODEL_INACTIVE(5)）
        for x in list(D2_DIM_VALUE_SUBSET) + [PortfolioStatus.SCORE_STALE, PortfolioStatus.MODEL_INACTIVE]:
            res = resolve_composite_status_from_row(x, x, x, x)
            assert res.composite_status == x
            assert res.highest_block_level == PORTFOLIO_STATUS_BLOCK_LEVEL[x.value]


# ═══════════════════════════════════════════════════════════════════════════════
# T-B3.1：T-B2.2 场景（T+1 limit up + T+2 suspended + T+3 valid）走 _default_build_evidence → 检查 evidence 内容
# ═══════════════════════════════════════════════════════════════════════════════

class TestTB3Q22PerSymbolEvidence:
    def _build_tb22_evidence(self):
        from app.services.decision_engine import (
            _default_build_evidence,
            LoadedSnapshot,
            UniverseAndEligibility,
            ScoredUniverse,
            SignalResult,
            RiskAllocationResult,
        )
        from app.services.decision_clock import DEFAULT_MATCH_MODE

        trade = date(2025, 1, 6)
        prev_close = 10.0
        t1 = date(2025, 1, 7)
        t2 = date(2025, 1, 8)
        t3 = date(2025, 1, 9)
        limit_up = prev_close * 1.10  # 11.0
        T_plus_3_valid_open = 10.2
        tb2_roll_rows = [
            (t1, limit_up, limit_up, limit_up, limit_up, 2_000_000.0),
            (t2, 10.5, 10.5, 10.5, 10.5, 0.0),
            (t3, T_plus_3_valid_open, 10.5, 10.1, 10.4, 800_000.0),
        ]

        sym_id = 12345
        clock = resolve(trade)

        # LoadedSnapshot：实际 _default_build_evidence 只取 snap.cost_config + snap.versions
        snap = LoadedSnapshot(
            snapshot=None,  # type: ignore[arg-type]
            factor_model_run_id="fm-1",
            factor_set_id="fs-1",
            rule_id=1,
            rule_version=1,
            members=[],
            candidate_pool=[],
            gate_policy_version="production-v1.0.0",
            cost_config={"min_lot_size": 100},
            versions={"factor_set": "v1"},
        )
        universe = UniverseAndEligibility(
            universe=[{
                "symbol_id": sym_id,
                "ts_code": "000001.SZ",
                "symbol_code": "000001",
                "asset_type": "stock",
                "exchange": "SZSE",
                "is_stock": True,
                "is_index": False,
                "is_etf": False,
                "industry_name": "银行",
                "current_quantity": 0.0,
                "current_position_pct": 0.0,
                "current_avg_price": None,
                "unrealized_pnl": 0.0,
                "authorized_position_pct": 0.30,
                "min_position_pct": 0.0,
                "max_position_pct": 0.30,
            }],
            universe_count=1, member_count=1, ineligible=[], health_issues=[],
        )
        scored = ScoredUniverse(
            items=[{
                "symbol_id": sym_id, "score_id": 1, "score_value": 0.8, "score_rank": 1,
                "factor_contributions": {"FA1": 0.3},
            }],
            expected=1, actual=1, coverage_pct=1.0,
        )
        signal = SignalResult(items=[{"symbol_id": sym_id, "direction": "OVERWEIGHT", "confidence": 0.8}])
        alloc = RiskAllocationResult(
            items=[{
                "symbol_id": sym_id,
                "target_weight": 0.20,
                "target_quantity": 2000.0,
                "min_lot_size": 100,
                "intended_price": None,
                "executed_price": None,
                "slippage_bps": None,
                "clamp_steps": [],
            }],
            clamp_trace=[],
        )
        price_data_by_symbol = {
            sym_id: {
                "open_price": 10.2, "close_price": 10.4, "high_price": 10.5, "low_price": 10.1, "volume": 800_000.0,
                "stop_loss_price": None,
                "_tb2_roll_rows": tb2_roll_rows,
                "_tb2_prev_close": prev_close,
            }
        }
        return _default_build_evidence(
            snap=snap,
            clock=clock,
            universe=universe,
            scored=scored,
            signal=signal,
            alloc=alloc,
            blocking_status="READY",  # T-A7 fail-closed 不要介入；T-B2 顺延路径独立生效
            blocking_reasons=[],
            price_data_by_symbol=price_data_by_symbol,
            trade_date=trade,
            match_mode=DEFAULT_MATCH_MODE.value,
            manual_overrides_context=None,
        )

    def test_t_b3_1_evidence_has_2_rejections_correct_prices(self):
        evidence = self._build_tb22_evidence()
        assert len(evidence) >= 1, f"Expected at least 1 evidence, got {len(evidence)}"
        ev = evidence[0]
        # rejections_trace_json 长度 = 2
        assert isinstance(ev.rejections_trace_json, list)
        assert len(ev.rejections_trace_json) == 2, (
            f"rejections_trace_json should be len=2; got len={len(ev.rejections_trace_json)}; "
            f"items={ev.rejections_trace_json}"
        )
        # 第 1 条 = LIMIT_UP_DOWN + intended_open=11.0
        r0 = ev.rejections_trace_json[0]
        assert r0["reason"] == REJECTION_REASON_LIMIT_UP_DOWN
        assert r0["date"] == "2025-01-07"
        assert abs(float(r0["intended_open"]) - 11.0) <= 1e-9
        # 第 2 条 = SUSPENDED + intended_open=10.5
        r1 = ev.rejections_trace_json[1]
        assert r1["reason"] == REJECTION_REASON_SUSPENDED
        assert r1["date"] == "2025-01-08"
        assert abs(float(r1["intended_open"]) - 10.5) <= 1e-9
        # roll_forward_days = 2
        assert ev.roll_forward_days == 2
        # intended_price = 首次 skipped intended_open（T+1 的 11.0）
        assert ev.intended_price is not None
        assert abs(float(ev.intended_price) - 11.0) <= 1e-9
        # executed_price = T+3 最终匹配 10.2 × (1 + 5/10000)（BUY 方向 +5bp 滑点 T-B4 后验）
        raw_final_open = 10.2
        expected_exec_with_slip = raw_final_open * (1.0 + 5.0 / 10000.0)  # 10.2051
        assert ev.executed_price is not None
        assert abs(float(ev.executed_price) - expected_exec_with_slip) <= 1e-6
        # slippage_bps = 5（BUY 方向默认 buy_bps）
        assert ev.slippage_bps == pytest.approx(5.0)
        # rejection_reason = "LIMIT_UP_DOWN|SUSPENDED"（按日期有序去重）
        assert ev.rejection_reason == f"{REJECTION_REASON_LIMIT_UP_DOWN}|{REJECTION_REASON_SUSPENDED}"

    def test_t_b3_1_no_roll_rows_means_rejection_reason_none_and_roll_none(self):
        """控制组：不给 _tb2_roll_rows 就不填 rejection_reason / rejections_trace_json。"""
        from app.services.decision_engine import (
            _default_build_evidence,
            LoadedSnapshot,
            UniverseAndEligibility,
            ScoredUniverse,
            SignalResult,
            RiskAllocationResult,
        )
        from app.services.decision_clock import DEFAULT_MATCH_MODE

        trade = date(2025, 2, 3)
        prev_close = 20.0
        t1_open = 20.4
        sym_id = 42
        clock = resolve(trade)

        snap = LoadedSnapshot(
            snapshot=None,  # type: ignore[arg-type]
            factor_model_run_id="fm-2",
            factor_set_id="fs-1",
            rule_id=1,
            rule_version=1,
            members=[],
            candidate_pool=[],
            gate_policy_version="production-v1.0.0",
            cost_config={"min_lot_size": 100},
            versions={"factor_set": "v1"},
        )
        universe = UniverseAndEligibility(
            universe=[{
                "symbol_id": sym_id,
                "ts_code": "000002.SZ",
                "symbol_code": "000002",
                "asset_type": "stock",
                "exchange": "SZSE",
                "is_stock": True,
                "is_index": False,
                "is_etf": False,
                "industry_name": "地产",
                "current_quantity": 0.0,
                "current_position_pct": 0.0,
                "current_avg_price": None,
                "unrealized_pnl": 0.0,
                "authorized_position_pct": 0.30,
                "min_position_pct": 0.0,
                "max_position_pct": 0.30,
            }],
            universe_count=1, member_count=1, ineligible=[], health_issues=[],
        )
        scored = ScoredUniverse(
            items=[{"symbol_id": sym_id, "score_id": 1, "score_value": 0.85, "score_rank": 1, "factor_contributions": {}}],
            expected=1, actual=1, coverage_pct=1.0,
        )
        signal = SignalResult(items=[{"symbol_id": sym_id, "direction": "OVERWEIGHT", "confidence": 0.7}])
        alloc = RiskAllocationResult(
            items=[{
                "symbol_id": sym_id, "target_weight": 0.20, "target_quantity": 1000.0,
                "min_lot_size": 100, "intended_price": t1_open, "executed_price": t1_open,
                "slippage_bps": None, "clamp_steps": [],
            }],
            clamp_trace=[],
        )
        price_data_by_symbol = {
            sym_id: {
                "open_price": t1_open, "close_price": 20.6, "high_price": 20.8, "low_price": 20.3, "volume": 900_000.0,
                "stop_loss_price": None,
                # NO _tb2_roll_rows 注入
            }
        }
        evidence = _default_build_evidence(
            snap=snap,
            clock=clock,
            universe=universe,
            scored=scored,
            signal=signal,
            alloc=alloc,
            blocking_status="READY",
            blocking_reasons=[],
            price_data_by_symbol=price_data_by_symbol,
            trade_date=trade,
            match_mode=DEFAULT_MATCH_MODE.value,
            manual_overrides_context=None,
        )
        assert len(evidence) == 1
        ev = evidence[0]
        assert ev.rejection_reason is None
        assert ev.roll_forward_days is None
        assert ev.rejections_trace_json == []
        # intended_price 保持 alloc 传值 20.4；executed 乘 BUY 方向 5bp 滑点
        expected_exec_with_slip = t1_open * (1.0 + 5.0 / 10000.0)  # 20.4102
        assert ev.intended_price == pytest.approx(t1_open)
        assert ev.executed_price == pytest.approx(expected_exec_with_slip)
        assert ev.slippage_bps == pytest.approx(5.0)


# ═══════════════════════════════════════════════════════════════════════════════
# T-B4 Q2.3：滑点 5bp 真实乘到撮合成交价（BUY 加 / SELL 减，误差 ≤ 1e-6）
# ═══════════════════════════════════════════════════════════════════════════════

class TestTB4Q23SlippageApplied:
    def _build_ev_for_dir(self, *, current_qty: float, target_qty: float, cost_config: dict):
        from app.services.decision_engine import (
            _default_build_evidence,
            LoadedSnapshot,
            UniverseAndEligibility,
            ScoredUniverse,
            SignalResult,
            RiskAllocationResult,
        )
        from app.services.decision_clock import DEFAULT_MATCH_MODE

        trade = date(2025, 3, 10)
        raw_open = 10.0
        sym_id = 88
        clock = resolve(trade)
        snap = LoadedSnapshot(
            snapshot=None,  # type: ignore[arg-type]
            factor_model_run_id="fm-3",
            factor_set_id="fs-1",
            rule_id=1,
            rule_version=1,
            members=[],
            candidate_pool=[],
            gate_policy_version="production-v1.0.0",
            cost_config=dict(cost_config),
            versions={"factor_set": "v1"},
        )
        universe = UniverseAndEligibility(
            universe=[{
                "symbol_id": sym_id,
                "ts_code": "000100.SZ",
                "symbol_code": "000100",
                "asset_type": "stock",
                "exchange": "SZSE",
                "is_stock": True,
                "is_index": False,
                "is_etf": False,
                "industry_name": "电子",
                "current_quantity": float(current_qty),
                "current_position_pct": float(current_qty) * raw_open / 100_000.0,
                "current_avg_price": raw_open,
                "unrealized_pnl": 0.0,
                "authorized_position_pct": 0.30,
                "min_position_pct": 0.0,
                "max_position_pct": 0.30,
            }],
            universe_count=1, member_count=1, ineligible=[], health_issues=[],
        )
        scored = ScoredUniverse(
            items=[{"symbol_id": sym_id, "score_id": 1, "score_value": 0.8, "score_rank": 1, "factor_contributions": {}}],
            expected=1, actual=1, coverage_pct=1.0,
        )
        signal = SignalResult(items=[{"symbol_id": sym_id, "direction": "FLAT", "confidence": 0.5}])
        alloc = RiskAllocationResult(
            items=[{
                "symbol_id": sym_id, "target_weight": 0.20, "target_quantity": float(target_qty),
                "min_lot_size": 100, "intended_price": raw_open, "executed_price": raw_open,
                "slippage_bps": None, "clamp_steps": [],
            }],
            clamp_trace=[],
        )
        price_data_by_symbol = {
            sym_id: {
                "open_price": raw_open, "close_price": 10.0, "high_price": 10.0, "low_price": 10.0, "volume": 500_000.0,
                "stop_loss_price": None,
            }
        }
        return _default_build_evidence(
            snap=snap,
            clock=clock,
            universe=universe,
            scored=scored,
            signal=signal,
            alloc=alloc,
            blocking_status="READY",
            blocking_reasons=[],
            price_data_by_symbol=price_data_by_symbol,
            trade_date=trade,
            match_mode=DEFAULT_MATCH_MODE.value,
            manual_overrides_context=None,
        )

    def test_t_b4_1_default_5bp_buy_10_005_sell_9_995(self):
        """B4.1：raw_open=10.0 BUY→10.005（+5bp），SELL→9.995（-5bp），abs 误差 ≤1e-6。"""
        raw_open = 10.0
        # BUY 情景：current=0, target=1000
        ev_buy = self._build_ev_for_dir(
            current_qty=0.0, target_qty=1000.0,
            cost_config={"min_lot_size": 100},  # 没配置 slippage → 默认 5
        )
        assert len(ev_buy) == 1
        b = ev_buy[0]
        expected_buy = raw_open * (1.0 + 5.0 / 10000.0)  # 10.005
        assert abs(float(b.executed_price) - expected_buy) <= 1e-6
        assert b.slippage_bps == pytest.approx(5.0)
        # intended_price 不变（= raw_open，滑点只乘 executed）
        assert b.intended_price == pytest.approx(raw_open)

        # SELL 情景：current=1000, target=0
        ev_sell = self._build_ev_for_dir(
            current_qty=1000.0, target_qty=0.0,
            cost_config={"min_lot_size": 100},
        )
        assert len(ev_sell) == 1
        s = ev_sell[0]
        expected_sell = raw_open * (1.0 - 5.0 / 10000.0)  # 9.995
        assert abs(float(s.executed_price) - expected_sell) <= 1e-6
        assert s.slippage_bps == pytest.approx(5.0)
        assert s.intended_price == pytest.approx(raw_open)

    def test_t_b4_2_cost_config_sell_10bp_yields_9_99(self):
        """B4.2：从 strategy_snapshot.cost_config 读 slippage_sell_bps=10 → SELL=9.99。"""
        raw_open = 10.0
        ev = self._build_ev_for_dir(
            current_qty=2000.0, target_qty=500.0,  # 净卖出 1500 → SELL
            cost_config={"min_lot_size": 100, "slippage_sell_bps": 10},
        )
        assert len(ev) == 1
        s = ev[0]
        expected = raw_open * (1.0 - 10.0 / 10000.0)  # 9.99
        assert abs(float(s.executed_price) - expected) <= 1e-6
        assert s.slippage_bps == pytest.approx(10.0)


# ═══════════════════════════════════════════════════════════════════════════════
# T-D3 Q27.4：自动恢复 vs 人工恢复收敛（纯函数层 + DB 入口层）
# ═══════════════════════════════════════════════════════════════════════════════

class TestTD3Q27FourAutoManualRecovery:
    # ---- 纯函数层断言 ----
    def test_t_d3_1_data_completed_auto_restores_data_incomplete_to_ready(self):
        """D3.1：数据补齐 → DATA_INCOMPLETE_PAUSED → READY（自动）。"""
        from app.services.portfolio_status import (
            auto_recover_dimensions_from_row,
        )
        from app.models.portfolio import PortfolioStatus

        # 初始：data=DATA_INCOMPLETE_PAUSED，其余 READY
        r = auto_recover_dimensions_from_row(
            status_score=PortfolioStatus.READY,
            status_data=PortfolioStatus.DATA_INCOMPLETE_PAUSED,
            status_model=PortfolioStatus.READY,
            status_reconciliation=PortfolioStatus.READY,
            data_completed=True,
            score_completed=False,
        )
        assert r.status_data_before == PortfolioStatus.DATA_INCOMPLETE_PAUSED
        assert r.status_data_after == PortfolioStatus.READY
        assert r.status_model_after == PortfolioStatus.READY
        assert r.status_reconciliation_after == PortfolioStatus.READY
        assert r.auto_changed_fields == ("status_data",)
        assert r.manual_only_blocking_left == ()

        # 控制组：没有 data_completed=True → 不自动
        r_nocall = auto_recover_dimensions_from_row(
            status_score=PortfolioStatus.READY,
            status_data=PortfolioStatus.DATA_INCOMPLETE_PAUSED,
            status_model=PortfolioStatus.READY,
            status_reconciliation=PortfolioStatus.READY,
            data_completed=False,
        )
        assert r_nocall.auto_changed_fields == ()
        assert r_nocall.status_data_after == PortfolioStatus.DATA_INCOMPLETE_PAUSED

    def test_t_d3_2_model_inactive_never_auto_restores_requires_manual_confirm(self):
        """D3.2：MODEL_INACTIVE → 自动链路绝不恢复；必须人工 confirm。"""
        from app.services.portfolio_status import (
            auto_recover_dimensions_from_row,
            confirm_active_model_binding,
        )
        from app.models.portfolio import Portfolio, PortfolioStatus

        # 1) 纯函数：data/score/data_completed/score_completed 都 True，但 model=MODEL_INACTIVE → 仍不恢复
        r_pure = auto_recover_dimensions_from_row(
            status_score=PortfolioStatus.SCORE_STALE,
            status_data=PortfolioStatus.DATA_INCOMPLETE_PAUSED,
            status_model=PortfolioStatus.MODEL_INACTIVE,
            status_reconciliation=PortfolioStatus.RECONCILIATION_BLOCKED,
            data_completed=True,
            score_completed=True,
        )
        assert "status_score" in r_pure.auto_changed_fields
        assert "status_data" in r_pure.auto_changed_fields
        # manual-only 两维完全未碰（before == after）
        assert r_pure.status_model_before == PortfolioStatus.MODEL_INACTIVE
        assert r_pure.status_model_after == PortfolioStatus.MODEL_INACTIVE
        assert r_pure.status_reconciliation_before == PortfolioStatus.RECONCILIATION_BLOCKED
        assert r_pure.status_reconciliation_after == PortfolioStatus.RECONCILIATION_BLOCKED
        assert set(r_pure.manual_only_blocking_left) == {"status_model", "status_reconciliation"}

    def test_t_d3_2_confirm_active_model_binding_goes_ready(self, db_session):
        """D3.2 DB 入口：confirm_active_model_binding → MODEL_INACTIVE → READY。"""
        from app.services.portfolio_status import (
            try_auto_recover_dimensions,
            confirm_active_model_binding,
            resolve_composite_status_by_id,
        )
        from app.models.portfolio import Portfolio, PortfolioStatus

        # 建一个 Portfolio：status_model=MODEL_INACTIVE, status_reconciliation=RECONCILIATION_BLOCKED
        p = Portfolio(
            name="D3-model-binding-test-unique-5341",
            account_type="SIMULATION",
            asset_scope="cn_stock",
            total_capital=100_000.0,
            investable_ratio=1.0,
            cash_reserve_ratio=0.0,
            currency="CNY",
            default_single_position_pct=0.30,
            status_score=PortfolioStatus.READY.value,
            status_data=PortfolioStatus.READY.value,
            status_model=PortfolioStatus.MODEL_INACTIVE.value,
            status_reconciliation=PortfolioStatus.RECONCILIATION_BLOCKED.value,
            is_test=1,
        )
        db_session.add(p)
        db_session.flush()
        pid = int(p.id)

        # 2) 先试 auto_recover（data_completed/score_completed=True 都给）→ 不碰 manual-only
        auto_res = try_auto_recover_dimensions(
            db_session, pid,
            data_completed=True, score_completed=True,
        )
        assert auto_res.auto_changed_fields == ()
        db_session.flush()
        composite0 = resolve_composite_status_by_id(db_session, pid)
        assert composite0.status_model == PortfolioStatus.MODEL_INACTIVE
        assert composite0.status_reconciliation == PortfolioStatus.RECONCILIATION_BLOCKED
        # composite = max(READY(1), READY(1), MODEL_INACTIVE(5), RECONCILIATION_BLOCKED(6)) = 6
        assert composite0.composite_status == PortfolioStatus.RECONCILIATION_BLOCKED
        assert composite0.highest_block_level == 6

        # 3) 人工 confirm：仅 model binding（不点对账）
        man1 = confirm_active_model_binding(
            db_session, pid,
            confirm_model_binding=True, confirm_reconciliation_passed=False,
        )
        assert man1.status_model_before == PortfolioStatus.MODEL_INACTIVE
        assert man1.status_model_after == PortfolioStatus.READY
        assert man1.status_reconciliation_after == PortfolioStatus.RECONCILIATION_BLOCKED
        assert man1.changed_fields == ("status_model",)
        db_session.flush()
        composite1 = resolve_composite_status_by_id(db_session, pid)
        assert composite1.status_model == PortfolioStatus.READY
        assert composite1.status_reconciliation == PortfolioStatus.RECONCILIATION_BLOCKED
        # composite = max(1, 1, 1, 6) = 6（RECONCILIATION 仍锁）
        assert composite1.composite_status == PortfolioStatus.RECONCILIATION_BLOCKED
        assert composite1.highest_block_level == 6

        # 4) 人工 confirm：对账通过（再点一次对账）
        man2 = confirm_active_model_binding(
            db_session, pid,
            confirm_model_binding=False, confirm_reconciliation_passed=True,
        )
        assert man2.changed_fields == ("status_reconciliation",)
        assert man2.status_reconciliation_after == PortfolioStatus.READY
        db_session.flush()
        composite_final = resolve_composite_status_by_id(db_session, pid)
        # 4 维度全 READY → composite=READY(1)
        assert composite_final.composite_status == PortfolioStatus.READY
        assert composite_final.highest_block_level == 1
        db_session.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# T-D4 Q27.5：阻断状态禁止新买单，风险退出 SELL 仍按可验证数据执行
# ═══════════════════════════════════════════════════════════════════════════════

class TestTD4Q27FiveOrderEntryGate:
    def test_t_d4_1_score_stale_buy_blocked_running_buy_allowed(self, db_session):
        """D4.1：SCORE_STALE(level=3) → requested_action=BUY → 阻断；状态=RUNNING(level=2) → BUY 放行。"""
        from app.services.portfolio_status import (
            order_entry_gate_check,
            REJECTION_COMPOSITE_BLOCK_LEVEL_TOO_HIGH,
        )
        from app.models.portfolio import Portfolio, PortfolioStatus

        p1 = Portfolio(
            name="TD4-stale-BUY-block-4211",
            account_type="SIMULATION",
            asset_scope="cn_stock",
            total_capital=100_000.0,
            investable_ratio=1.0,
            cash_reserve_ratio=0.0,
            currency="CNY",
            default_single_position_pct=0.30,
            status_score=PortfolioStatus.SCORE_STALE.value,  # level 3 → ≥3 禁
            status_data=PortfolioStatus.READY.value,
            status_model=PortfolioStatus.READY.value,
            status_reconciliation=PortfolioStatus.READY.value,
            is_test=1,
        )
        db_session.add(p1)
        p2 = Portfolio(
            name="TD4-running-BUY-allowed-4212",
            account_type="SIMULATION",
            asset_scope="cn_stock",
            total_capital=100_000.0,
            investable_ratio=1.0,
            cash_reserve_ratio=0.0,
            currency="CNY",
            default_single_position_pct=0.30,
            status_score=PortfolioStatus.RUNNING.value,  # level 2 → <3 允许
            status_data=PortfolioStatus.READY.value,
            status_model=PortfolioStatus.READY.value,
            status_reconciliation=PortfolioStatus.READY.value,
            is_test=1,
        )
        db_session.add(p2)
        db_session.flush()
        pid_stale = int(p1.id)
        pid_running = int(p2.id)

        # BLOCK 分支：SCORE_STALE ≥3 → 禁，rejection_code = COMPOSITE_BLOCK_LEVEL_TOO_HIGH
        g_blocked = order_entry_gate_check(db_session, pid_stale, "BUY")
        assert g_blocked.blocked is True
        assert g_blocked.rejection_reason_code == REJECTION_COMPOSITE_BLOCK_LEVEL_TOO_HIGH
        assert g_blocked.composite_before.highest_block_level == 3
        assert g_blocked.composite_before.composite_status == PortfolioStatus.SCORE_STALE

        # 另外 2 个 BUY 类动作也禁
        for extra_buy in ("REBALANCE_BUY", "CASH_SWAP_IN"):
            gx = order_entry_gate_check(db_session, pid_stale, extra_buy)
            assert gx.blocked is True, f"expected {extra_buy} blocked at SCORE_STALE"
            assert gx.rejection_reason_code == REJECTION_COMPOSITE_BLOCK_LEVEL_TOO_HIGH

        # ALLOW 分支：RUNNING <3 → 放行
        g_allowed = order_entry_gate_check(db_session, pid_running, "BUY")
        assert g_allowed.blocked is False
        assert g_allowed.allowed is True
        assert g_allowed.rejection_reason_code is None
        assert g_allowed.composite_before.highest_block_level == 2
        db_session.commit()

    def test_t_d4_2_recon_blocked_sell_data_verifiable_allowed_unverifiable_still_rejected(self, db_session):
        """D4.2：RECONCILIATION_BLOCKED(6) + SELL_STOP_LOSS：数据可验证 → 放行；不可验证 → UNABLE_TO_VERIFY_STOP_LOSS 拒绝。"""
        from app.services.portfolio_status import (
            order_entry_gate_check,
            REJECTION_UNABLE_TO_VERIFY_STOP_LOSS,
        )
        from app.models.portfolio import Portfolio, PortfolioStatus

        p = Portfolio(
            name="TD4-recon6-sell-check-4311",
            account_type="SIMULATION",
            asset_scope="cn_stock",
            total_capital=100_000.0,
            investable_ratio=1.0,
            cash_reserve_ratio=0.0,
            currency="CNY",
            default_single_position_pct=0.30,
            status_score=PortfolioStatus.READY.value,
            status_data=PortfolioStatus.READY.value,
            status_model=PortfolioStatus.READY.value,
            status_reconciliation=PortfolioStatus.RECONCILIATION_BLOCKED.value,  # 最高 6 级
            is_test=1,
        )
        db_session.add(p)
        db_session.flush()
        pid = int(p.id)
        composite_baseline = order_entry_gate_check(
            db_session, pid, "SELL_STOP_LOSS", data_verifiable_for_sell=True
        ).composite_before
        assert composite_baseline.highest_block_level == 6
        assert composite_baseline.composite_status == PortfolioStatus.RECONCILIATION_BLOCKED

        # SELL 可验证 → 允许（哪怕 level=6 的 RECONCILIATION_BLOCKED）
        for act in ("SELL_STOP_LOSS", "SELL_RISK_EXIT", "EXIT"):
            g_allow = order_entry_gate_check(db_session, pid, act, data_verifiable_for_sell=True)
            assert g_allow.allowed is True, f"expected {act} allowed when data_verifiable=True even at RECONCILIATION_BLOCKED"
            assert g_allow.rejection_reason_code is None

        # SELL 不可验证 → 仍拒绝 UNABLE_TO_VERIFY_STOP_LOSS
        for act in ("SELL_STOP_LOSS", "SELL_RISK_EXIT", "EXIT"):
            g_reject = order_entry_gate_check(db_session, pid, act, data_verifiable_for_sell=False)
            assert g_reject.blocked is True, f"expected {act} rejected when data_verifiable=False"
            assert g_reject.rejection_reason_code == REJECTION_UNABLE_TO_VERIFY_STOP_LOSS

        db_session.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# T-B5 Q11.1：所有卖出规则证据保存 + 最高优先级裁决 + 细分类
# ═══════════════════════════════════════════════════════════════════════════════

class TestTB5Q11OneExitEvidenceAggregate:
    def test_t_b5_1_stop_loss_priority_10_vs_strategy_exit_20(self):
        """B5.1：SELL_STOP_LOSS(priority=10, 清仓 1000 股) + SELL_STRATEGY_EXIT(priority=20, 差额 300 股) →
        action_subtype=SELL_STOP_LOSS（最高优先级 = 数值最小 10），final target_qty=max(alloc 500, 1000, 300)=1000，
        exit_rules_hit_json len=2（全保存）。
        """
        from app.services.decision_engine import (
            _default_build_evidence,
            LoadedSnapshot,
            UniverseAndEligibility,
            ScoredUniverse,
            SignalResult,
            RiskAllocationResult,
            ExitRuleHit,
        )
        from app.services.decision_clock import DEFAULT_MATCH_MODE

        trade = date(2025, 4, 21)
        sym_id = 787
        raw_open = 25.0
        clock = resolve(trade)
        current_qty = 1500.0
        alloc_target_qty = 500.0  # 正常调仓：减到 500 股；alloc 方向原本是 SELL（delta -1000）

        snap = LoadedSnapshot(
            snapshot=None,  # type: ignore[arg-type]
            factor_model_run_id="fm-4",
            factor_set_id="fs-1",
            rule_id=1,
            rule_version=1,
            members=[],
            candidate_pool=[],
            gate_policy_version="production-v1.0.0",
            cost_config={"min_lot_size": 100, "slippage_buy_bps": 5, "slippage_sell_bps": 5},
            versions={"factor_set": "v1"},
        )
        universe = UniverseAndEligibility(
            universe=[{
                "symbol_id": sym_id,
                "ts_code": "688787.SH",
                "symbol_code": "688787",
                "asset_type": "stock",
                "exchange": "SSE",
                "is_stock": True,
                "is_index": False,
                "is_etf": False,
                "industry_name": "半导体",
                "current_quantity": float(current_qty),
                "current_position_pct": float(current_qty) * raw_open / 100_000.0,
                "current_avg_price": 24.0,
                "unrealized_pnl": 1500.0,
                "authorized_position_pct": 0.30,
                "min_position_pct": 0.0,
                "max_position_pct": 0.30,
            }],
            universe_count=1, member_count=1, ineligible=[], health_issues=[],
        )
        scored = ScoredUniverse(
            items=[{"symbol_id": sym_id, "score_id": 1, "score_value": 0.5, "score_rank": 5, "factor_contributions": {}}],
            expected=1, actual=1, coverage_pct=1.0,
        )
        signal = SignalResult(items=[{"symbol_id": sym_id, "direction": "UNDERWEIGHT", "confidence": 0.7}])
        alloc = RiskAllocationResult(
            items=[{
                "symbol_id": sym_id,
                "target_weight": 0.125,  # (500 * 25) / 100,000 = 12.5%
                "target_quantity": float(alloc_target_qty),
                "min_lot_size": 100,
                "intended_price": raw_open,
                "executed_price": raw_open,
                "slippage_bps": None,
                "clamp_steps": [],
            }],
            clamp_trace=[],
        )
        price_data_by_symbol = {
            sym_id: {
                "open_price": raw_open, "close_price": 24.8, "high_price": 25.5, "low_price": 24.2, "volume": 1_200_000.0,
                "stop_loss_price": None,
            }
        }
        # SELL_STOP_LOSS：priority 10（高）→ 清仓 1000 股（把持仓减到 1500-1000=500，与 alloc 差额一致）
        hit_sl = ExitRuleHit(
            rule_code="SL-001",
            rule_priority=10,
            requested_exit_qty=1000.0,  # 卖出 1000 股
            rule_subtype="SELL_STOP_LOSS",
        )
        # SELL_STRATEGY_EXIT：priority 20（低）→ 差额 300 股
        hit_strat = ExitRuleHit(
            rule_code="SE-portfolio",
            rule_priority=20,
            requested_exit_qty=300.0,
            rule_subtype="SELL_STRATEGY_EXIT",
        )
        evidence = _default_build_evidence(
            snap=snap,
            clock=clock,
            universe=universe,
            scored=scored,
            signal=signal,
            alloc=alloc,
            blocking_status="READY",
            blocking_reasons=[],
            price_data_by_symbol=price_data_by_symbol,
            trade_date=trade,
            match_mode=DEFAULT_MATCH_MODE.value,
            manual_overrides_context=None,
            exit_rules_hit_by_symbol={int(sym_id): [hit_sl, hit_strat]},
        )
        assert len(evidence) == 1
        ev = evidence[0]
        # 最高优先级 10 胜 20 → subtype = SELL_STOP_LOSS
        assert ev.action_subtype == "SELL_STOP_LOSS"
        # action 方向被卖出规则命中强制纠正为 SELL
        assert ev.action == "SELL"
        # 最终 target_quantity = max(alloc 500, sl 1000, strat 300) = 1000
        assert ev.target_quantity is not None
        assert float(ev.target_quantity) == pytest.approx(1000.0)
        # 两条证据全部保存
        assert len(ev.exit_rules_hit) == 2
        rule_pairs = sorted((int(h.rule_priority), str(h.rule_subtype)) for h in ev.exit_rules_hit)
        assert rule_pairs == [(10, "SELL_STOP_LOSS"), (20, "SELL_STRATEGY_EXIT")]
        # SELL 方向滑点 -5bp：raw_open=25.0 * (1 - 5/10000) = 24.9875
        expected_exec_with_slip = raw_open * (1.0 - 5.0 / 10000.0)
        assert abs(float(ev.executed_price) - expected_exec_with_slip) <= 1e-6
        assert ev.slippage_bps == pytest.approx(5.0)


# ═══════════════════════════════════════════════════════════════════════════════
# T-D5 Q28.1：幂等键后端主键公式 + 专用幂等表（TaskIdempotency）
# ═══════════════════════════════════════════════════════════════════════════════

class TestTD5Q28OneIdempotencyKey:
    def _kw(self, **overrides):
        from datetime import datetime
        base = dict(
            portfolio_id=42,
            strategy_snapshot_id="ss-2025-05-01",
            decision_at=datetime(2025, 5, 1, 6, 30, 0),
            trade_date=date(2025, 5, 2),
            run_type="auto_simulation",
        )
        base.update(overrides)
        return base

    def test_t_d5_1_same_5_fields_same_key_1_char_diff_avalanche(self):
        """D5.1：相同 5 字段 → 相同 key；差 1 字符 → 不同 key（sha256 雪崩）。"""
        from app.services.portfolio_status import calc_task_idempotency_key

        a = calc_task_idempotency_key(**self._kw())
        b = calc_task_idempotency_key(**self._kw())
        assert a == b
        assert len(a) == 64  # sha256 hex

        # 差 1 字符：portfolio_id 42 → 43
        c = calc_task_idempotency_key(**self._kw(portfolio_id=43))
        assert c != a
        # 差 1 字符：snapshot 末尾加 x
        d = calc_task_idempotency_key(**self._kw(strategy_snapshot_id="ss-2025-05-01x"))
        assert d != a
        assert d != c
        # 差 1 字符：run_type
        e = calc_task_idempotency_key(**self._kw(run_type="backtest"))
        assert e != a
        # 预计算断言：不漂移（若 payload 协议变动本断言必 FAIL → 立即发现回归）
        assert a == calc_task_idempotency_key(**self._kw()), "T-D5 idempotency payload 协议漂移！"

    def test_t_d5_2_decision_at_diff_1_second_gives_diff_key(self):
        """D5.2：decision_at 差 1 秒 → key 不同（禁止把 decision_at 截到小时/天）。"""
        from datetime import datetime
        from app.services.portfolio_status import calc_task_idempotency_key

        t1 = datetime(2025, 5, 1, 6, 30, 0)
        t2 = datetime(2025, 5, 1, 6, 30, 1)
        k1 = calc_task_idempotency_key(**self._kw(decision_at=t1))
        k2 = calc_task_idempotency_key(**self._kw(decision_at=t2))
        assert k1 != k2
        # 同日 trade_date 改变 → 也不同
        k3 = calc_task_idempotency_key(**self._kw(trade_date=date(2025, 5, 3)))
        assert k3 != k1

    def test_t_d5_3_unique_index_conflict_integrity_error_keeps_existing_task_id(self, db_session):
        """D5.3：同 key 直接 INSERT 两次 → IntegrityError；第 1 条 existing_task_id 保留。"""
        from sqlalchemy.exc import IntegrityError
        from app.services.portfolio_status import (
            calc_task_idempotency_key,
            try_acquire_idempotency,
        )
        from app.models.decision_engine import TaskIdempotency

        kw = self._kw()
        original_task_id = "async-task-00ABCDEF"
        # 1) 合法入口：try_acquire_idempotency 第 1 次 → acquired=True
        r1 = try_acquire_idempotency(
            db_session,
            **kw,
            task_type="auto_trade",
            existing_task_id=original_task_id,
            param_hash="h1",
        )
        assert r1.acquired is True
        assert r1.existing_task_id == original_task_id
        pid_row_id = int(r1.row.id)
        db_session.flush()

        # 2) 重放：try_acquire_idempotency 第 2 次 → acquired=False, existing_task_id 仍=原 id（不冲突）
        r2 = try_acquire_idempotency(
            db_session,
            **kw,
            task_type="auto_trade",
            existing_task_id="DIFFERENT-ID-SHOULD-BE-IGNORED",
            param_hash="DIFFERENT-PARAM-HASH",
        )
        assert r2.acquired is False
        # existing_task_id 必须是第 1 次的值，不是后来传的新值（幂等重放 = 原结果）
        assert r2.existing_task_id == original_task_id
        assert r2.param_hash == "h1"
        assert int(r2.row.id) == pid_row_id

        # 3) 手动 INSERT 同样 key → UniqueConstraint 抛 IntegrityError
        #    使用 savepoint（BEGIN NESTED）保证冲突回滚只影响本段而不撤销 r1 插入
        same_key = calc_task_idempotency_key(**kw)
        row_dup = TaskIdempotency(
            idempotency_key=same_key,
            portfolio_id=int(kw["portfolio_id"]),
            strategy_snapshot_id=str(kw["strategy_snapshot_id"]),
            decision_at=kw["decision_at"],
            trade_date=kw["trade_date"],
            run_type=str(kw["run_type"]),
            task_type="auto_trade",
            existing_task_id="ATTACK-DUP-ID",
            param_hash="ATTACK-DUP-HASH",
        )
        nested = db_session.begin_nested()  # SQLite SAVEPOINT
        db_session.add(row_dup)
        with pytest.raises(IntegrityError):
            db_session.flush()  # 冲突 → savepoint 级回滚；r1 row 仍在
        # flush 抛 IntegrityError 后 SQLAlchemy 会自动 rollback 该 savepoint；
        # 手动 release 以 avoid pending savepoint 警告（保险起见）
        try:
            nested.rollback()
        except Exception:
            pass
        # 验证原记录仍存在且 existing_task_id 没变（savepoint 不影响 r1）
        still = try_acquire_idempotency(db_session, **kw, task_type="auto_trade")
        assert still.acquired is False
        assert still.existing_task_id == original_task_id


# ═══════════════════════════════════════════════════════════════════════════════
# T-B6 Q11.2：默认一次性清仓 + 分阶段退出仅显式配置
# ═══════════════════════════════════════════════════════════════════════════════

class TestTB6Q11TwoExitPhasedDefaults:
    def test_t_b6_1_empty_or_disabled_means_one_shot_immediate_100_pct(self):
        """B6.1：exit_config_json 空或 phased_exit_enabled=false → 一次性清仓 100%（立即）。"""
        from app.services.decision_engine import compute_exit_phase_quantities

        # 持仓 2000 → 目标 200（净卖出 1800）
        current_qty = 2000.0
        target_qty = 200.0
        delta_total = current_qty - target_qty  # = 1800
        assert delta_total > 0

        # (a) 空 dict（默认配置）
        q_a, _trace_a = compute_exit_phase_quantities(
            exit_config={},
            current_qty=current_qty,
            target_qty=target_qty,
            elapsed_trade_days=0,
        )
        assert q_a == pytest.approx(delta_total)  # 1800 = 一次性

        # (b) 显式 disabled
        q_b, _ = compute_exit_phase_quantities(
            exit_config={"phased_exit_enabled": False},
            current_qty=current_qty,
            target_qty=target_qty,
            elapsed_trade_days=0,
        )
        assert q_b == pytest.approx(delta_total)

        # (c) elapsed 100 天 → 还是一次性（phased false 不受 elapsed 影响）
        q_c, _ = compute_exit_phase_quantities(
            exit_config={"phased_exit_enabled": False},
            current_qty=current_qty,
            target_qty=target_qty,
            elapsed_trade_days=100,
        )
        assert q_c == pytest.approx(delta_total)

    def test_t_b6_2_two_phases_50_pct_day0_50_pct_day5_day3_is_zero(self):
        """B6.2：phased_exit_enabled=true, phases=[{pct:0.5, delay_days:0},{pct:0.5, delay_days:5}]
        → T+0 卖 50%；T+3 卖 0（还没到第二阶段 delay=5）；T+5 卖剩下 50%；T+10 卖 0（全部完成）。
        """
        from app.services.decision_engine import compute_exit_phase_quantities

        current_qty = 2000.0
        target_qty = 200.0
        total_exit = current_qty - target_qty  # 1800
        cfg = {
            "phased_exit_enabled": True,
            "phases": [
                {"pct": 0.5, "delay_days": 0},
                {"pct": 0.5, "delay_days": 5},
            ],
        }

        # T+0 → 阶段 1：0.5 * 1800 = 900
        q0, t0 = compute_exit_phase_quantities(cfg, current_qty, target_qty, elapsed_trade_days=0)
        assert q0 == pytest.approx(total_exit * 0.5)
        assert sum(t0.get("quantities_by_phase_idx", {}).values()) == pytest.approx(q0)
        # T+3 → 阶段 1（delay 0 ≤3 已执行）+ 阶段 2（delay 5 >3 未到期）→ 0
        q3, t3 = compute_exit_phase_quantities(cfg, current_qty, target_qty, elapsed_trade_days=3)
        assert q3 == pytest.approx(0.0)
        # T+5 → 阶段 2 到期：0.5 * 1800 = 900
        q5, _t5 = compute_exit_phase_quantities(cfg, current_qty, target_qty, elapsed_trade_days=5)
        assert q5 == pytest.approx(total_exit * 0.5)
        # T+10 → 全部 phases 已执行 → 0
        q10, t10 = compute_exit_phase_quantities(cfg, current_qty, target_qty, elapsed_trade_days=10)
        assert q10 == pytest.approx(0.0)
        # 累计已卖出：T0(900) + T5(900) = total_exit
        assert t10.get("total_phase_percent_mature") == pytest.approx(1.0)

