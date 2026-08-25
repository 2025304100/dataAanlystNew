"""Stage-3 contract: account ledger must accept a unified matcher result."""
from datetime import date

from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.services.sim_accounts import cash_balance, ensure_sim_account_seed, place_sim_order


def test_precomputed_match_fee_is_not_recalculated_by_account_adapter(db_session):
    portfolio = Portfolio(
        name="stage3-account-cost",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=50_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.0,
        currency="CNY",
        default_single_position_pct=0.2,
        auto_trade_enabled=1,
    )
    symbol = Symbol(symbol="600971", name="Matcher Cost", asset_type="stock", market="SH")
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    db_session.add(DailyBar(
        symbol_id=symbol.id,
        trade_date=date(2026, 1, 5),
        open=10.0, high=10.1, low=9.9, close=10.0, volume=100_000.0,
    ))
    ensure_sim_account_seed(db_session, portfolio)
    db_session.commit()

    # 10.005 is already a 5bps buy-side matcher price. The account adapter
    # must neither apply a second slippage nor replace the matcher fee.
    order, _ = place_sim_order(
        db_session,
        portfolio=portfolio,
        symbol=symbol,
        side="buy",
        quantity=100,
        price=10.005,
        enforce_rules=False,
        apply_fees=False,
        fee_override=5.12,
        execution_price_is_final=True,
    )
    db_session.commit()

    assert order.filled_price == 10.005
    assert order.fee == 5.12
    assert cash_balance(db_session, portfolio.id) == 50_000.0 - 1_000.5 - 5.12
