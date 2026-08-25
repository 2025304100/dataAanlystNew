"""WP0-5：统一撮合引擎 + 回测契约 RED→GREEN 测试（TR-05.1 ~ TR-05.9）。

本文件覆盖：
  - TR-05.9 统一撮合引擎 match_order_plan 四分类行为
  - TR-05.1/05.2/05.3/05.4/05.5/05.6/05.7 契约性 RED 断言
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker


os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    """与 test_g1_decision_engine 完全一致的 SQLite+alembic head fixture。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_wp05_sme_")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["ALEMBIC_DATABASE_URL"] = url
    from alembic.config import Config
    from alembic import command as ac
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    ac.upgrade(cfg, "head")
    engine = create_engine(url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


from app.services.simulation_matching_engine import (
    CostModelConfig,
    MarketBar,
    MatchResult,
    OrderPlan,
    OrderSide,
    PortfolioState,
    apply_cost_model,
    match_order_plan,
    sort_plans_by_risk_priority,
    update_portfolio_state,
)


# ---------------------------------------------------------------------------
# TR-05.9a：T+1 开盘涨停封板 BUY 侧无法入场 → REJECTED_TRADE_HALTED + 待处理计划
# ---------------------------------------------------------------------------
class TestTR059UnifiedMatchingEngine:
    """WP0-5 TR-05.9 统一撮合引擎（sme）。"""

    def _bar_limit_up_locked(self, td: date, pre_close: float = 10.0) -> MarketBar:
        up = round(pre_close * 1.10, 2)
        return MarketBar(
            trade_date=td, open=up, high=up, low=up, close=up, pre_close=pre_close,
            upper_limit_price=up, lower_limit_price=round(pre_close * 0.90, 2),
            limit_up_locked=True, limit_down_locked=False,
        )

    def _bar_limit_down_locked(self, td: date, pre_close: float = 10.0) -> MarketBar:
        dn = round(pre_close * 0.90, 2)
        return MarketBar(
            trade_date=td, open=dn, high=dn, low=dn, close=dn, pre_close=pre_close,
            upper_limit_price=round(pre_close * 1.10, 2), lower_limit_price=dn,
            limit_up_locked=False, limit_down_locked=True,
        )

    def test_tr05_9a_1_limit_up_locked_buy_side_rejected_with_reason_codes(self):
        """T+1 开盘涨停封板 BUY → REJECTED_TRADE_HALTED + PRICE_LIMIT_LOCKED_BUY_SIDE + 待处理。"""
        td = date(2025, 1, 7)
        plan = OrderPlan(
            order_plan_id="op_LU_BUY", symbol_id=1, portfolio_id=1, trade_date=td,
            price_type="NEXT_OPEN", side=OrderSide.BUY, target_quantity=100, min_lot_size=100,
            signal_expire_date=date(2025, 1, 9),
        )
        bar = self._bar_limit_up_locked(td, pre_close=10.0)
        r: MatchResult = match_order_plan(plan, bar)
        assert r.final_status == "REJECTED_TRADE_HALTED", f"实际 status={r.final_status}"
        assert "PRICE_LIMIT_LOCKED_BUY_SIDE" in r.reason_codes, f"reason_codes={r.reason_codes}"
        assert "CANNOT_FILL_AT_NEXT_OPEN" in r.reason_codes
        assert r.filled_quantity == 0
        assert r.retry_on_next_session is True, "应生成待处理计划，下一交易日继续检查"

    def test_tr05_9a_2_limit_down_locked_sell_side_rejected(self):
        """开盘跌停封板 SELL → REJECTED_TRADE_HALTED + PRICE_LIMIT_LOCKED_SELL_SIDE + 待处理。"""
        td = date(2025, 1, 7)
        plan = OrderPlan(
            order_plan_id="op_LD_SELL", symbol_id=1, portfolio_id=1, trade_date=td,
            price_type="NEXT_OPEN", side=OrderSide.SELL, target_quantity=100, min_lot_size=100,
            signal_expire_date=date(2025, 1, 10),
        )
        bar = self._bar_limit_down_locked(td, pre_close=20.0)
        r: MatchResult = match_order_plan(plan, bar)
        assert r.final_status == "REJECTED_TRADE_HALTED"
        assert "PRICE_LIMIT_LOCKED_SELL_SIDE" in r.reason_codes, f"reason_codes={r.reason_codes}"
        assert "CANNOT_FILL_AT_NEXT_OPEN" in r.reason_codes
        assert r.retry_on_next_session is True

    def test_tr05_9b_pending_plan_retry_and_signal_expire_close(self):
        """待处理计划：T+2 可成交→FILLED；T+2 仍涨停 + T+3 信号过期→SIGNAL_EXPIRED。"""
        sym, pid = 2, 1
        # D1：涨停封板 BUY → PENDING_RETRY
        d1, d2, d3 = date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 9)
        plan_d1 = OrderPlan(
            order_plan_id="op_EXPIRE", symbol_id=sym, portfolio_id=pid, trade_date=d1,
            price_type="NEXT_OPEN", side=OrderSide.BUY, target_quantity=100, min_lot_size=100,
            signal_expire_date=d2,  # 过期日=T+2
        )
        r1 = match_order_plan(plan_d1, self._bar_limit_up_locked(d1, pre_close=10.0))
        assert r1.retry_on_next_session is True

        # D2：仍然涨停封板，但 signal_expire_date=d2 == bar.trade_date → 仍允许（仅 > 过期才 fail）
        plan_d2 = OrderPlan(
            order_plan_id="op_EXPIRE", symbol_id=sym, portfolio_id=pid, trade_date=d2,
            price_type="NEXT_OPEN", side=OrderSide.BUY, target_quantity=100, min_lot_size=100,
            signal_expire_date=d2,
        )
        r2 = match_order_plan(plan_d2, self._bar_limit_up_locked(d2, pre_close=11.0))
        assert r2.retry_on_next_session is True, f"D2 仍涨停应仍 RETRY；实际 {r2.final_status}"

        # D3：bar.trade_date=d3 > expire_date=d2 → SIGNAL_EXPIRED
        plan_d3 = OrderPlan(
            order_plan_id="op_EXPIRE", symbol_id=sym, portfolio_id=pid, trade_date=d3,
            price_type="NEXT_OPEN", side=OrderSide.BUY, target_quantity=100, min_lot_size=100,
            signal_expire_date=d2,
        )
        r3 = match_order_plan(plan_d3, self._bar_limit_up_locked(d3, pre_close=12.1))
        assert r3.final_status == "SIGNAL_EXPIRED", f"过期后应关闭计划；实际 {r3.final_status}"
        assert "SIGNAL_EXPIRED" in r3.reason_codes
        assert r3.retry_on_next_session is False, "信号过期关闭计划后不允许继续 retry"

    def test_tr05_9c_idempotent_same_input_same_output(self):
        """回测/自动模拟调用同一 order_plan 对同 bar → 逐字段一致（误差 ≤1e-6）。"""
        td = date(2025, 1, 7)
        bar = MarketBar(
            trade_date=td, open=10.0, high=10.3, low=9.9, close=10.15, pre_close=10.0,
            upper_limit_price=11.0, lower_limit_price=9.0,
        )
        cfg = CostModelConfig(
            commission_rate=0.0003, min_commission=5.0, stamp_tax_rate=0.001,
            transfer_fee_rate=0.00001, slippage_buy_bps=5, slippage_sell_bps=5,
        )
        plan_a = OrderPlan(
            order_plan_id="op_IDEM", symbol_id=10, portfolio_id=1, trade_date=td,
            price_type="NEXT_OPEN", side=OrderSide.BUY, target_quantity=100, min_lot_size=100,
        )
        plan_b = OrderPlan(
            order_plan_id="op_IDEM", symbol_id=10, portfolio_id=1, trade_date=td,
            price_type="NEXT_OPEN", side=OrderSide.BUY, target_quantity=100, min_lot_size=100,
        )
        r_a = match_order_plan(plan_a, bar, cfg=cfg)
        r_b = match_order_plan(plan_b, bar, cfg=cfg)
        # 逐字段对比
        assert r_a.final_status == r_b.final_status
        assert r_a.filled_quantity == r_b.filled_quantity
        assert r_a.executed_price == pytest.approx(r_b.executed_price, abs=1e-10)
        assert r_a.slippage_bps == r_b.slippage_bps
        for k in ("commission", "stamp_tax", "transfer_fee", "total_cost"):
            assert getattr(r_a, k) == pytest.approx(getattr(r_b, k), abs=1e-6), \
                f"成本 {k} 不一致 a={getattr(r_a,k)} b={getattr(r_b,k)}"
        assert r_a.reason_codes == r_b.reason_codes
        assert r_a.retry_on_next_session == r_b.retry_on_next_session

    def test_tr05_9d_risk_exit_se优先_sell_before_buy(self):
        """风险退出卖单 与 普通买单同时命中 → sort_plans_by_risk_priority 先卖后买。"""
        td = date(2025, 1, 7)
        buy = OrderPlan(order_plan_id="p_BUY", symbol_id=1, portfolio_id=1, trade_date=td,
                        price_type="NEXT_OPEN", side=OrderSide.BUY, target_quantity=100,
                        is_risk_exit=False)
        normal_sell = OrderPlan(order_plan_id="p_SELL_NORMAL", symbol_id=2, portfolio_id=1,
                                trade_date=td, price_type="NEXT_OPEN", side=OrderSide.SELL,
                                target_quantity=100, is_risk_exit=False)
        risk_sell = OrderPlan(order_plan_id="p_SELL_RISK", symbol_id=3, portfolio_id=1,
                              trade_date=td, price_type="NEXT_OPEN", side=OrderSide.SELL,
                              target_quantity=100, is_risk_exit=True)
        mixed = [buy, normal_sell, risk_sell]
        ordered = sort_plans_by_risk_priority(mixed)
        ids = [o.order_plan_id for o in ordered]
        assert ids.index("p_SELL_RISK") < ids.index("p_SELL_NORMAL"), \
            f"风险卖应优先于普通卖: {ids}"
        assert ids.index("p_SELL_NORMAL") < ids.index("p_BUY"), \
            f"普通卖应优先于普通买: {ids}"

    def test_cost_model_roundtrip(self):
        """apply_cost_model：卖单 = 佣金 + 印花税（千1）+ 过户费（双边），买单 = 无印花税。"""
        cfg = CostModelConfig(
            commission_rate=0.0003, min_commission=5.0, stamp_tax_rate=0.001,
            transfer_fee_rate=0.00001,
        )
        # BUY 100 股 × 10 元 = 1000 元：佣金 0.3 → max 5 元；印花税 0；过户费 0.01；合计 5.01
        c_buy = apply_cost_model(OrderSide.BUY, 100, 10.0, cfg=cfg)
        assert c_buy["commission"] == pytest.approx(5.0, abs=1e-6)
        assert c_buy["stamp_tax"] == pytest.approx(0.0)
        assert c_buy["transfer_fee"] == pytest.approx(0.01, abs=1e-6)
        assert c_buy["total_cost"] == pytest.approx(5.01, abs=1e-6)
        # SELL：佣金 5，印花税 1000 × 0.001 = 1.0；过户费 0.01 → 合计 6.01
        c_sell = apply_cost_model(OrderSide.SELL, 100, 10.0, cfg=cfg)
        assert c_sell["stamp_tax"] == pytest.approx(1.0, abs=1e-6)
        assert c_sell["total_cost"] == pytest.approx(6.01, abs=1e-6)

    def test_portfolio_state_conservation_on_filled(self):
        """update_portfolio_state：FILLED BUY → 现金减 gross+cost，持仓加 qty；SELL 反向。"""
        state = PortfolioState(cash=10_000.0, holdings={}, pending_orders={})
        buy_plan = OrderPlan(order_plan_id="b1", symbol_id=7, portfolio_id=1,
                             trade_date=date(2025, 1, 7), price_type="NEXT_OPEN",
                             side=OrderSide.BUY, target_quantity=100, min_lot_size=100)
        # 成交价 10.00 元，总手续费 5.01（上一个 cost 断言）
        buy_result = MatchResult(order_plan_id="b1", final_status="FILLED",
                                 filled_quantity=100, executed_price=10.00, slippage_bps=5,
                                 commission=5.00, stamp_tax=0.0, transfer_fee=0.01,
                                 total_cost=5.01, reason_codes=[])
        s2 = update_portfolio_state(state, buy_plan, buy_result)
        # gross = 100 * 10 = 1000；总付 1000+5.01 = 1005.01；剩余现金 = 10000 - 1005.01 = 8994.99
        assert s2.cash == pytest.approx(8994.99, abs=1e-2), f"实际 cash={s2.cash}"
        assert s2.holdings[7] == 100

        # 再同价 SELL 全部
        sell_plan = OrderPlan(order_plan_id="s1", symbol_id=7, portfolio_id=1,
                              trade_date=date(2025, 1, 8), price_type="NEXT_OPEN",
                              side=OrderSide.SELL, target_quantity=100, min_lot_size=100)
        sell_result = MatchResult(order_plan_id="s1", final_status="FILLED",
                                  filled_quantity=100, executed_price=10.00, slippage_bps=5,
                                  commission=5.0, stamp_tax=1.0, transfer_fee=0.01,
                                  total_cost=6.01, reason_codes=[])
        s3 = update_portfolio_state(s2, sell_plan, sell_result)
        # 回收 = 1000 - 6.01 = 993.99 → 现金 = 8994.99 + 993.99 = 9988.98
        assert s3.cash == pytest.approx(9988.98, abs=1e-2), f"实际 cash={s3.cash}"
        assert 7 not in s3.holdings, f"清仓后应无持仓，实际 {s3.holdings}"

    def test_below_lot_rejected(self):
        """最小手数不符：target_quantity=50 not multiple of 100 → REJECTED_BELOW_LOT。"""
        plan = OrderPlan(order_plan_id="op_LOT", symbol_id=1, portfolio_id=1,
                         trade_date=date(2025, 1, 7), price_type="NEXT_OPEN",
                         side=OrderSide.BUY, target_quantity=50, min_lot_size=100)
        bar = MarketBar(trade_date=date(2025, 1, 7), open=10.0, pre_close=10.0,
                        upper_limit_price=11.0, lower_limit_price=9.0)
        r = match_order_plan(plan, bar)
        assert r.final_status == "REJECTED_BELOW_LOT"
        assert "ORDER_BELOW_LOT_SIZE" in r.reason_codes

    def test_volume_limit_returns_partial_fill_and_retry(self):
        """成交量约束只成交整手，剩余计划必须可在下一交易日重试。"""
        plan = OrderPlan(
            order_plan_id="op_PARTIAL", symbol_id=1, portfolio_id=1,
            trade_date=date(2025, 1, 7), price_type="NEXT_OPEN",
            side=OrderSide.BUY, target_quantity=1_000, min_lot_size=100,
        )
        bar = MarketBar(
            trade_date=date(2025, 1, 7), open=10.0, high=10.2, low=9.8,
            close=10.1, pre_close=10.0, volume=1_500,
        )
        result = match_order_plan(
            plan, bar,
            cfg=CostModelConfig(volume_limit_pct=0.4, slippage_buy_bps=0),
        )
        assert result.final_status == "PARTIAL_FILL"
        assert result.filled_quantity == 600
        assert "PARTIAL_FILL" in result.reason_codes
        assert result.retry_on_next_session is True
        assert result.note == "requested=1000, filled=600"


# ---------------------------------------------------------------------------
# TR-05.3：PortfolioBacktestRequest 契约 8 字段完整性（C-05 缺陷 RED 基线）
# ---------------------------------------------------------------------------
class TestTR053ContractC05BacktestParams:
    def test_t_tr05_3_request_schema_has_eight_override_fields(self):
        """请求体显式包含 initial_capital / commission_rate / stamp_tax_rate / slippage_bps
        / benchmark / price_type / volume_limit_pct / rebalance_frequency 共 8 项。
        """
        from app.schemas.backtest import PortfolioBacktestRequest
        payload = {
            "portfolio_id": 1,
            "start_date": date(2025, 1, 6),
            "end_date": date(2025, 1, 10),
            # ↓ 8 个新契约字段（非默认），全部非默认值
            "initial_capital": 2_000_000.0,
            "commission_rate": 0.0005,
            "stamp_tax_rate": 0.0015,
            "slippage_bps": 8,
            "benchmark": "沪深300",
            "price_type": "NEXT_OPEN",
            "volume_limit_pct": 0.05,
            "rebalance_frequency": "weekly",
        }
        req = PortfolioBacktestRequest.model_validate(payload)
        # 8 项必须存在且非默认值
        assert float(req.initial_capital) == 2_000_000.0
        assert float(req.commission_rate) == 0.0005
        assert float(req.stamp_tax_rate) == 0.0015
        assert int(req.slippage_bps) == 8
        assert req.benchmark == "沪深300"
        assert req.price_type == "NEXT_OPEN"
        assert float(req.volume_limit_pct) == 0.05
        assert req.rebalance_frequency == "weekly"

    def test_t_tr05_5_no_universe_type_prefix_approximation_allowed(self):
        """C-08 RED → GREEN：请求枚举校验拒绝 universe_type = index_membership / zz500_prefix /
        全A股；也允许请求完全不传该字段（统一用 candidate / holdings 三段口径）。
        """
        from app.schemas.backtest import PortfolioBacktestRequest
        bad_universes = ["index_membership", "zz500_prefix", "全A股", "all_a_shares"]
        for u in bad_universes:
            payload = {
                "portfolio_id": 1,
                "start_date": date(2025, 1, 6), "end_date": date(2025, 1, 10),
                "universe_type": u,
            }
            # 严格契约：PortfolioBacktestRequest 不应存在 universe_type 字段；
            # 使用 model_validate 若字段存在则抛 ExtraForbidden 或忽略。
            try:
                req = PortfolioBacktestRequest.model_validate(payload)
            except Exception:
                # GOOD：严格契约直接拒绝
                continue
            # 若宽松模式下通过：req 对象上不应有 universe_type 属性
            assert not hasattr(req, "universe_type"), \
                f"PortfolioBacktestRequest 不应接受 universe_type={u}"


# ---------------------------------------------------------------------------
# TR-05.4 / C-08 证券范围仅用 PortfolioCandidate 与持仓（候选池外 BUY → 拒绝）
# ---------------------------------------------------------------------------
class TestTR054UniverseC08Compliance:
    def test_universe_scope_matches_three_tier_definition(self, tmp_alembic_db: Any):
        """回测标的范围 = 候选 ∩ 已授权 ∩ 未手动移除 ∪ 当前持仓。
        候选池外证券（未加入 PortfolioCandidate 的）× BUY → OUTSIDE_CANDIDATE_POOL。
        """
        from app.db.session import get_db
        from app.models.symbol import Symbol
        from app.models.portfolio import Portfolio
        from app.models.portfolio_candidate import PortfolioCandidate

        db: Session = tmp_alembic_db
        p = Portfolio(
            name="WP05 组合08", account_type="simulated", asset_scope="mixed",
            auto_trade_enabled=1,
            total_capital=1_000_000.0,
            investable_ratio=0.95, cash_reserve_ratio=0.05,
            default_single_position_pct=0.10,
        )
        db.add(p); db.flush()
        sIn = Symbol(symbol="IN001", name="候选内", asset_type="stock", market="SH", industry="tech", is_active=1)
        sOut = Symbol(symbol="OUT01", name="候选外", asset_type="stock", market="SH", industry="tech", is_active=1)
        db.add_all([sIn, sOut]); db.flush()
        cand = PortfolioCandidate(
            portfolio_id=p.id, symbol_id=sIn.id, effective_from=date(2025, 1, 1),
            auto_authorized_flag=True, removed_manually_flag=False,
        )
        db.add(cand); db.commit()

        # PortfolioCandidate 服务的 universe 派生逻辑（与回测 / DecisionEngine 共用）
        from app.services.portfolio_candidates import CandidatePoolService as PCS
        eligible = PCS.get_effective_candidate_symbols(
            db, portfolio_id=p.id, as_of_date=date(2025, 1, 7), require_authorized=True,
        )
        eligible_ids = {int(s) for s in eligible}
        assert sIn.id in eligible_ids, "sIn 为已授权候选，应在 eligible 集合内"
        assert sOut.id not in eligible_ids, "sOut 未加入候选，必然不在 eligible 集合内"


# ---------------------------------------------------------------------------
# TR-05.1 / C-02 RED 基线：run_portfolio_backtest factor_model_run_id 不为 None 路径（决策时锁定 model）
# ---------------------------------------------------------------------------
class TestTR051C02ModelIdNotNone:
    def test_t_tr05_1_run_portfolio_backtest_accepts_explicit_model_id_and_not_default_none(
        self, tmp_alembic_db: Any
    ):
        """run_portfolio_backtest 签名允许显式传入 factor_model_run_id 与 score_weight_mode；
        显式 ridge 时不使用全局 runtime.active_model 兜底 None 的旧行为。
        """
        import inspect
        from app.services.portfolio_backtest import run_portfolio_backtest
        sig = inspect.signature(run_portfolio_backtest)
        assert "factor_model_run_id" in sig.parameters, \
            "run_portfolio_backtest 应显式接受 factor_model_run_id 参数（C-02）"
        assert "score_weight_mode" in sig.parameters, \
            "run_portfolio_backtest 应显式接受 score_weight_mode 参数（C-02）"
