"""Personal backtests charge the tax applicable on the actual execution day."""
from datetime import date

import pytest

from app.services.cost_model import compute_cost
from app.schemas.backtest import BacktestCostConfig, PortfolioBacktestRequest
from tests.test_g1_factor_usage import tmp_alembic_db


pytestmark = pytest.mark.whitebox


def test_default_a_share_sell_tax_changes_on_2023_august_28():
    fees = {"commission_rate": 0.0003, "min_commission": 0, "slippage_rate": 0}

    # RMB 10,000 sale: commission 3; stamp duty 10 before / 5 from Aug 28.
    assert compute_cost(10, 1000, "sell", fees, trade_date=date(2023, 8, 25)) == 13
    assert compute_cost(10, 1000, "sell", fees, trade_date=date(2023, 8, 28)) == 8


def test_a_share_stamp_duty_is_not_charged_to_etfs_or_us_shares():
    fees = {"commission_rate": 0.0003, "min_commission": 0, "slippage_rate": 0}

    assert compute_cost(10, 1000, "sell", fees, trade_date=date(2025, 1, 2), asset_type="etf", market="cn") == 3
    assert compute_cost(10, 1000, "sell", fees, trade_date=date(2025, 1, 2), asset_type="stock", market="us") == 3


@pytest.mark.parametrize("field", ["stamp_tax_rate", "stamp_duty_rate", "stamp_duty_pct"])
@pytest.mark.parametrize("rate,expected", [(0, 3), (0.002, 23)])
def test_explicit_user_tax_override_and_legacy_aliases_are_preserved(field, rate, expected):
    fees = {"commission_rate": 0.0003, "min_commission": 0, "slippage_rate": 0, field: rate}

    assert compute_cost(10, 1000, "sell", fees, trade_date=date(2025, 1, 2)) == expected


def test_backtest_requests_keep_omitted_stamp_tax_in_automatic_mode():
    costs = BacktestCostConfig(slippage_rate=0).model_dump()
    request = PortfolioBacktestRequest(portfolio_id=1, start_date="2023-08-25", end_date="2023-08-28")

    assert costs["stamp_tax_rate"] is None
    assert request.stamp_tax_rate is None
    assert compute_cost(10, 1000, "sell", costs, trade_date=date(2023, 8, 25)) == 15
    assert compute_cost(10, 1000, "sell", costs, trade_date=date(2023, 8, 28)) == 10


@pytest.mark.parametrize("market", ["SH", "SZ", "BJ"])
def test_mainland_exchange_market_codes_apply_the_same_date_rule(market):
    fees = {"commission_rate": 0, "min_commission": 0, "slippage_rate": 0}
    assert compute_cost(10, 1000, "sell", fees, trade_date=date(2025, 1, 2), market=market) == 5


def test_portfolio_backtest_returns_automatic_tax_without_freezing_current_rate(db_session):
    from app.services.portfolio_backtest import run_portfolio_backtest
    from tests.test_whitebox_portfolio_backtest import (
        _make_daily_bar, _make_portfolio, _make_position, _make_symbol,
    )

    portfolio = _make_portfolio(db_session, name="Automatic-tax-contract")
    symbol = _make_symbol(db_session, symbol="600319", name="Tax contract")
    _make_position(db_session, portfolio.id, symbol.id, quantity=100)
    _make_daily_bar(db_session, symbol.id, date(2023, 8, 25), close=10)
    _make_daily_bar(db_session, symbol.id, date(2023, 8, 28), close=10)

    result = run_portfolio_backtest(
        db_session, portfolio_id=portfolio.id, start_date=date(2023, 8, 25),
        end_date=date(2023, 8, 28), source_type="legacy", stamp_tax_rate=None,
    )

    assert result["stamp_tax_rate"] is None
    from app.api.routes.backtest import get_backtest_run
    detail = get_backtest_run(result["run_id"], db=db_session)
    assert detail.cost_config["stamp_tax_rate"] is None


def test_production_cost_freeze_resolves_automatic_tax_and_keeps_actual_commissions():
    from types import SimpleNamespace
    from app.services.factors.production_execution import normalize_portfolio_cost

    portfolio = SimpleNamespace(id=1, currency="CNY", total_capital=100_000,
                                investable_ratio=1, buy_fee_pct=0, sell_fee_pct=0.00025)
    snapshot = SimpleNamespace(id="cost-snapshot", snapshot_hash="a" * 64,
                               cost_config_json='{"stamp_tax_rate":null}')
    result = normalize_portfolio_cost(portfolio, snapshot)

    assert result["fees"]["stamp_tax_rate"] == 0.0005
    assert result["fees"]["buy_commission_rate"] == 0
    assert result["fees"]["sell_commission_rate"] == 0.00025


@pytest.mark.parametrize("day,asset_type,expected", [
    (date(2023, 8, 25), "stock", 10),
    (date(2023, 8, 28), "stock", 5),
    (date(2023, 8, 28), "etf", 0),
])
def test_shared_matcher_stamp_duty_follows_execution_day_and_instrument(day, asset_type, expected):
    from app.services.simulation_matching_engine import CostModelConfig, OrderSide, apply_cost_model
    config = CostModelConfig(commission_rate=0.0003, min_commission=0, transfer_fee_rate=0)

    cost = apply_cost_model(OrderSide.SELL, 1000, 10, cfg=config,
                            trade_date=day, asset_type=asset_type, market="SH")

    assert cost["stamp_tax"] == expected
    assert cost["commission"] == 3


def test_optional_vectorbt_research_requires_explicit_tax_assumption_before_loading_engine():
    from app.services.vectorbt_backtest import VectorBTConfig, run_vectorbt_backtest
    from tests.test_whitebox_vectorbt_backtest import _frames

    with pytest.raises(ValueError, match="显式.*印花税|事件驱动"):
        run_vectorbt_backtest(_frames([10, 10, 11, 12, 13, 12, 11, 10]),
                             VectorBTConfig(fast_window=2, slow_window=3))


def test_manual_simulated_etf_sale_does_not_charge_stock_stamp_duty(db_session):
    from app.services.sim_accounts import place_sim_order
    from tests.test_whitebox_portfolio_backtest import _make_portfolio, _make_position, _make_symbol

    portfolio = _make_portfolio(db_session, name="Manual ETF fee", total_capital=100_000)
    symbol = _make_symbol(db_session, symbol="510319", asset_type="etf", market="SH")
    _make_position(db_session, portfolio.id, symbol.id, quantity=1000)

    order, _ = place_sim_order(
        db_session, portfolio, symbol, "sell", 1000, price=10, enforce_rules=False,
        cost_config={"commission_rate": 0.0003, "min_commission": 0, "slippage_rate": 0},
    )

    assert order.fee == 3


def test_new_applied_strategy_keeps_automatic_stamp_duty_for_execution_day(tmp_alembic_db):
    from app.schemas.decision_engine import FactorUsageBindRequest
    from app.services.decision_engine import default_engine
    from app.services.factor_usage_service import save_and_apply_usage
    from tests.test_g1_factor_usage import _make_portfolio, _set_active_model

    db_session = tmp_alembic_db
    portfolio = _make_portfolio(db_session, buy_fee_pct=0, sell_fee_pct=0.00025)
    _set_active_model(db_session, "tax-policy-model")
    db_session.commit()
    saved = save_and_apply_usage(db_session, portfolio.id, FactorUsageBindRequest(
        factor_model_run_id="tax-policy-model", run_mode="research", pit_mode="best_effort",
    ))
    snapshot = default_engine.load_snapshot(db_session, saved.strategy_snapshot_id)

    assert snapshot.cost_config.get("stamp_duty_pct") is None
    assert snapshot.cost_config["buy_commission_pct"] == 0
    assert snapshot.cost_config["sell_commission_pct"] == 0.00025
