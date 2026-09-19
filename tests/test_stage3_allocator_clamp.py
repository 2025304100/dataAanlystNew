"""Focused stage-3 allocator contracts."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.models.portfolio import Portfolio, PortfolioRule
from app.models.symbol import Symbol
from app.services.decision_engine import (
    DecisionEngine,
    DecisionStateContext,
    LoadedSnapshot,
    ScoredUniverse,
    SignalResult,
    UniverseAndEligibility,
)


def test_explicit_zero_stage_limit_blocks_new_buy_and_is_auditable(db_session):
    """A configured zero stage limit must never be treated as an unset limit."""
    portfolio = Portfolio(
        name="stage3-zero-stage-cap",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.0,
        currency="CNY",
        default_single_position_pct=0.20,
    )
    symbol = Symbol(
        symbol="600930",
        name="Zero Stage Cap",
        asset_type="stock",
        market="SH",
        industry="technology",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    rule = PortfolioRule(
        portfolio_id=portfolio.id,
        rule_name="stage-zero-blocks-entry",
        max_single_position_pct=0.20,
        max_sector_position_pct=0.50,
        max_stock_position_pct=1.0,
        max_etf_position_pct=0.0,
        max_loss_per_trade_pct=0.05,
        max_open_positions=10,
        # "start" is explicitly disabled.  It is not a missing/default key.
        stage_limits_json='{"stock":{"start":0.0}}',
        is_active=1,
    )
    db_session.add(rule)
    db_session.flush()

    snapshot = LoadedSnapshot(
        snapshot=SimpleNamespace(portfolio_id=portfolio.id, portfolio=portfolio),
        factor_model_run_id="",
        factor_set_id=None,
        rule_id=rule.id,
        rule_version=None,
        members=[{"symbol_id": symbol.id}],
        cost_config={"min_lot_size": 100},
        price_hints={symbol.id: 10.0},
    )

    engine = DecisionEngine(
        snapshot_loader=lambda _db, _snapshot_id: snapshot,
        gate=lambda _db, _snapshot, _clock: (True, [], {"passed": True}),
        universe_builder=lambda _db, _snapshot, _cutoff: UniverseAndEligibility(
            universe=list(snapshot.members), universe_count=1, member_count=1,
        ),
        health=lambda _db, universe, _cutoff: universe,
        scorer=lambda _db, _snapshot, _universe, _cutoff, _trade_date: ScoredUniverse(
            expected=1,
            actual=1,
            coverage_pct=100.0,
            items=[{
                "symbol_id": symbol.id,
                "quality_score": 0.90,
                "stage": "start",
            }],
        ),
    )
    result = engine.evaluate(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id="stage3-zero-stage-cap-snapshot",
        trade_date=datetime(2026, 8, 21).date(),
        dry_run=True,
        price_data_by_symbol={
            symbol.id: {
                "open_price": 10.0,
                "close_price": 10.0,
                "high_price": 10.0,
                "low_price": 10.0,
                "volume": 1_000_000,
            },
        },
    )

    evidence = result.evidence[0]
    assert evidence.action == "HOLD"
    assert evidence.target_position_pct == 0.0
    assert evidence.target_quantity == 0.0
    assert result.order_plans[0].target_quantity == 0.0
    stage_step = next(
        step for step in evidence.constraints if step["step"] == "SINGLE_STOCK_CAP"
    )
    assert stage_step["trigger"] == "STAGE_LIMIT_CAP"
    assert stage_step["before_pct"] == 0.05
    assert stage_step["after_pct"] == 0.0


def test_injected_cash_reserve_blocks_buy_before_lot_rounding(db_session):
    """The allocator honors Portfolio.cash_reserve_ratio for replay cash."""
    portfolio = Portfolio(
        name="stage3-cash-reserve-cap",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.10,
        currency="CNY",
        default_single_position_pct=0.20,
    )
    symbol = Symbol(
        symbol="600931",
        name="Cash Reserve Cap",
        asset_type="stock",
        market="SH",
        industry="technology",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    rule = PortfolioRule(
        portfolio_id=portfolio.id,
        rule_name="cash-reserve-fallback",
        max_single_position_pct=0.20,
        max_sector_position_pct=0.50,
        max_stock_position_pct=1.0,
        max_etf_position_pct=0.0,
        max_loss_per_trade_pct=0.05,
        max_open_positions=10,
        stage_limits_json='{"stock":{"growth":0.20}}',
        is_active=1,
    )
    db_session.add(rule)
    db_session.flush()

    snapshot = LoadedSnapshot(
        snapshot=SimpleNamespace(portfolio_id=portfolio.id, portfolio=portfolio),
        factor_model_run_id="",
        factor_set_id=None,
        rule_id=rule.id,
        rule_version=None,
        members=[{"symbol_id": symbol.id}],
        cost_config={"min_lot_size": 100},
        price_hints={symbol.id: 10.0},
    )
    engine = DecisionEngine(
        snapshot_loader=lambda _db, _snapshot_id: snapshot,
        gate=lambda _db, _snapshot, _clock: (True, [], {"passed": True}),
        universe_builder=lambda _db, _snapshot, _cutoff: UniverseAndEligibility(
            universe=list(snapshot.members), universe_count=1, member_count=1,
        ),
        health=lambda _db, universe, _cutoff: universe,
        scorer=lambda _db, _snapshot, _universe, _cutoff, _trade_date: ScoredUniverse(
            expected=1,
            actual=1,
            coverage_pct=100.0,
            items=[{
                "symbol_id": symbol.id,
                "quality_score": 0.90,
                "stage": "growth",
            }],
        ),
    )

    result = engine.evaluate(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id="stage3-cash-reserve-snapshot",
        trade_date=datetime(2026, 8, 21).date(),
        dry_run=True,
        state_context=DecisionStateContext(
            available_cash=10_000.0,
            total_capital=100_000.0,
            price_data_by_symbol={
                symbol.id: {
                    "open_price": 10.0,
                    "close_price": 10.0,
                    "high_price": 10.0,
                    "low_price": 10.0,
                    "volume": 1_000_000,
                },
            },
        ),
    )

    evidence = result.evidence[0]
    assert evidence.action == "HOLD"
    assert evidence.target_quantity == 0.0
    cash_step = next(
        step for step in evidence.constraints if step["step"] == "AVAILABLE_CASH_CAP"
    )
    assert cash_step["trigger"] == "AVAILABLE_CASH_CAP"
    assert cash_step["before_pct"] == 0.05
    assert cash_step["after_pct"] == 0.0


def test_open_position_limit_blocks_a_new_buy_and_is_auditable(db_session):
    """The allocator must not open a second position when the slot cap is one."""
    portfolio = Portfolio(
        name="stage3-open-position-cap",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.0,
        currency="CNY",
        default_single_position_pct=0.20,
    )
    held = Symbol(
        symbol="600932",
        name="Held Position",
        asset_type="stock",
        market="SH",
        industry="technology",
    )
    candidate = Symbol(
        symbol="600933",
        name="Blocked Candidate",
        asset_type="stock",
        market="SH",
        industry="technology",
    )
    db_session.add_all([portfolio, held, candidate])
    db_session.flush()
    rule = PortfolioRule(
        portfolio_id=portfolio.id,
        rule_name="one-open-position-only",
        max_single_position_pct=0.20,
        max_sector_position_pct=0.50,
        max_stock_position_pct=1.0,
        max_etf_position_pct=0.0,
        max_loss_per_trade_pct=0.05,
        max_open_positions=1,
        stage_limits_json='{"stock":{"growth":0.20}}',
        is_active=1,
    )
    db_session.add(rule)
    db_session.flush()

    snapshot = LoadedSnapshot(
        snapshot=SimpleNamespace(portfolio_id=portfolio.id, portfolio=portfolio),
        factor_model_run_id="",
        factor_set_id=None,
        rule_id=rule.id,
        rule_version=None,
        members=[{"symbol_id": held.id}, {"symbol_id": candidate.id}],
        cost_config={"min_lot_size": 100},
        price_hints={held.id: 10.0, candidate.id: 10.0},
    )
    engine = DecisionEngine(
        snapshot_loader=lambda _db, _snapshot_id: snapshot,
        gate=lambda _db, _snapshot, _clock: (True, [], {"passed": True}),
        universe_builder=lambda _db, _snapshot, _cutoff: UniverseAndEligibility(
            universe=list(snapshot.members), universe_count=2, member_count=2,
        ),
        health=lambda _db, universe, _cutoff: universe,
        scorer=lambda _db, _snapshot, _universe, _cutoff, _trade_date: ScoredUniverse(
            expected=2,
            actual=2,
            coverage_pct=100.0,
            items=[
                {"symbol_id": held.id, "quality_score": 0.50, "stage": "growth"},
                {"symbol_id": candidate.id, "quality_score": 0.90, "stage": "growth"},
            ],
        ),
        signal=lambda _db, _snapshot, _scored: SignalResult(items=[
            {"symbol_id": held.id, "direction": "HOLD", "stage": "growth"},
            {"symbol_id": candidate.id, "direction": "BUY", "stage": "growth"},
        ]),
    )

    result = engine.evaluate(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id="stage3-open-position-snapshot",
        trade_date=datetime(2026, 8, 21).date(),
        dry_run=True,
        state_context=DecisionStateContext(
            current_by_symbol={
                held.id: {
                    "qty": 1_000.0,
                    "pct": 0.10,
                    "market_value": 10_000.0,
                    "asset_type": "stock",
                    "sector": "technology",
                },
            },
            available_cash=90_000.0,
            total_capital=100_000.0,
            price_data_by_symbol={
                held.id: {
                    "open_price": 10.0, "close_price": 10.0,
                    "high_price": 10.0, "low_price": 10.0, "volume": 1_000_000,
                },
                candidate.id: {
                    "open_price": 10.0, "close_price": 10.0,
                    "high_price": 10.0, "low_price": 10.0, "volume": 1_000_000,
                },
            },
        ),
    )

    evidence = next(item for item in result.evidence if item.symbol_id == candidate.id)
    assert evidence.action == "HOLD"
    assert evidence.target_quantity == 0.0
    slot_step = next(
        step for step in evidence.constraints if step["step"] == "OPEN_POSITION_CAP"
    )
    assert slot_step["trigger"] == "MAX_OPEN_POSITIONS"
    assert slot_step["before_pct"] == 0.05
    assert slot_step["after_pct"] == 0.0


def test_verified_stop_loss_distance_clamps_buy_to_trade_risk_budget(db_session):
    """A verified stop distance limits target exposure before lot rounding."""
    portfolio = Portfolio(
        name="stage3-risk-budget-cap",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.0,
        currency="CNY",
        default_single_position_pct=0.20,
    )
    symbol = Symbol(
        symbol="600934",
        name="Risk Budget Cap",
        asset_type="stock",
        market="SH",
        industry="technology",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    rule = PortfolioRule(
        portfolio_id=portfolio.id,
        rule_name="risk-budget-cap",
        max_single_position_pct=0.20,
        max_sector_position_pct=0.50,
        max_stock_position_pct=1.0,
        max_etf_position_pct=0.0,
        # 0.5% of 100,000 = 500 CNY. At a verified 2 CNY/share risk,
        # the maximum target is 250 shares / 2.5% before lot rounding.
        max_loss_per_trade_pct=0.005,
        max_open_positions=10,
        stage_limits_json='{"stock":{"growth":0.20}}',
        is_active=1,
    )
    db_session.add(rule)
    db_session.flush()

    snapshot = LoadedSnapshot(
        snapshot=SimpleNamespace(portfolio_id=portfolio.id, portfolio=portfolio),
        factor_model_run_id="",
        factor_set_id=None,
        rule_id=rule.id,
        rule_version=None,
        members=[{"symbol_id": symbol.id}],
        cost_config={"min_lot_size": 100},
        price_hints={symbol.id: 10.0},
    )
    engine = DecisionEngine(
        snapshot_loader=lambda _db, _snapshot_id: snapshot,
        gate=lambda _db, _snapshot, _clock: (True, [], {"passed": True}),
        universe_builder=lambda _db, _snapshot, _cutoff: UniverseAndEligibility(
            universe=list(snapshot.members), universe_count=1, member_count=1,
        ),
        health=lambda _db, universe, _cutoff: universe,
        scorer=lambda _db, _snapshot, _universe, _cutoff, _trade_date: ScoredUniverse(
            expected=1,
            actual=1,
            coverage_pct=100.0,
            items=[{
                "symbol_id": symbol.id,
                "quality_score": 0.90,
                "stage": "growth",
            }],
        ),
        signal=lambda _db, _snapshot, _scored: SignalResult(items=[{
            "symbol_id": symbol.id,
            "direction": "BUY",
            "stage": "growth",
        }]),
    )

    result = engine.evaluate(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id="stage3-risk-budget-snapshot",
        trade_date=datetime(2026, 8, 21).date(),
        dry_run=True,
        state_context=DecisionStateContext(
            available_cash=100_000.0,
            total_capital=100_000.0,
            price_data_by_symbol={
                symbol.id: {
                    "open_price": 10.0,
                    "close_price": 10.0,
                    "high_price": 10.0,
                    "low_price": 10.0,
                    "volume": 1_000_000,
                    "stop_loss_price": 8.0,
                },
            },
        ),
    )

    evidence = result.evidence[0]
    assert evidence.action == "BUY"
    assert evidence.target_quantity == 200.0
    risk_step = next(
        step for step in evidence.constraints if step["step"] == "RISK_BUDGET_CAP"
    )
    assert risk_step["trigger"] == "MAX_LOSS_PER_TRADE"
    assert risk_step["before_pct"] == 0.05
    assert risk_step["after_pct"] == 0.025


def test_available_cash_cap_includes_buy_slippage_and_fees(db_session):
    """A plan must not spend cash that only covers the raw share notional."""
    portfolio = Portfolio(
        name="stage3-cash-cost-cap",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.0,
        currency="CNY",
        default_single_position_pct=0.20,
    )
    symbol = Symbol(
        symbol="600935",
        name="Cash Cost Cap",
        asset_type="stock",
        market="SH",
        industry="technology",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    rule = PortfolioRule(
        portfolio_id=portfolio.id,
        rule_name="cash-cost-cap",
        max_single_position_pct=0.20,
        max_sector_position_pct=0.50,
        max_stock_position_pct=1.0,
        max_etf_position_pct=0.0,
        max_loss_per_trade_pct=0.0,
        max_open_positions=10,
        stage_limits_json='{"stock":{"growth":0.20}}',
        is_active=1,
    )
    db_session.add(rule)
    db_session.flush()

    snapshot = LoadedSnapshot(
        snapshot=SimpleNamespace(portfolio_id=portfolio.id, portfolio=portfolio),
        factor_model_run_id="",
        factor_set_id=None,
        rule_id=rule.id,
        rule_version=None,
        members=[{"symbol_id": symbol.id}],
        cost_config={"min_lot_size": 100},
        price_hints={symbol.id: 10.0},
    )
    engine = DecisionEngine(
        snapshot_loader=lambda _db, _snapshot_id: snapshot,
        gate=lambda _db, _snapshot, _clock: (True, [], {"passed": True}),
        universe_builder=lambda _db, _snapshot, _cutoff: UniverseAndEligibility(
            universe=list(snapshot.members), universe_count=1, member_count=1,
        ),
        health=lambda _db, universe, _cutoff: universe,
        scorer=lambda _db, _snapshot, _universe, _cutoff, _trade_date: ScoredUniverse(
            expected=1,
            actual=1,
            coverage_pct=100.0,
            items=[{
                "symbol_id": symbol.id,
                "quality_score": 0.90,
                "stage": "growth",
            }],
        ),
        signal=lambda _db, _snapshot, _scored: SignalResult(items=[{
            "symbol_id": symbol.id,
            "direction": "BUY",
            "stage": "growth",
        }]),
    )

    result = engine.evaluate(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id="stage3-cash-cost-snapshot",
        trade_date=datetime(2026, 8, 21).date(),
        dry_run=True,
        state_context=DecisionStateContext(
            # Raw 500-share notional is exactly 5,000 CNY, but the final
            # buy price and fees are higher than this available cash amount.
            available_cash=5_000.0,
            total_capital=100_000.0,
            price_data_by_symbol={
                symbol.id: {
                    "open_price": 10.0,
                    "close_price": 10.0,
                    "high_price": 10.0,
                    "low_price": 10.0,
                    "volume": 1_000_000,
                },
            },
            cost_config={
                "min_lot_size": 100,
                "slippage_buy_bps": 5.0,
                "commission_rate": 0.0003,
                "min_commission": 5.0,
                "transfer_fee_rate": 0.00001,
            },
        ),
    )

    evidence = result.evidence[0]
    assert evidence.action == "BUY"
    assert evidence.target_quantity == 400.0
    cash_step = next(
        step for step in evidence.constraints if step["step"] == "AVAILABLE_CASH_CAP"
    )
    assert cash_step["trigger"] == "AVAILABLE_CASH_CAP"
    assert cash_step["before_pct"] == 0.05
    assert cash_step["after_pct"] < 0.05


def test_volume_participation_cap_limits_plan_quantity_before_matching(db_session):
    """A plan cannot consume more than its configured share of market volume."""
    portfolio = Portfolio(
        name="stage3-volume-cap",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.0,
        currency="CNY",
        default_single_position_pct=0.20,
    )
    symbol = Symbol(
        symbol="600936",
        name="Volume Cap",
        asset_type="stock",
        market="SH",
        industry="technology",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    rule = PortfolioRule(
        portfolio_id=portfolio.id,
        rule_name="volume-cap",
        max_single_position_pct=0.20,
        max_sector_position_pct=0.50,
        max_stock_position_pct=1.0,
        max_etf_position_pct=0.0,
        max_loss_per_trade_pct=0.0,
        max_open_positions=10,
        stage_limits_json='{"stock":{"growth":0.20}}',
        is_active=1,
    )
    db_session.add(rule)
    db_session.flush()

    snapshot = LoadedSnapshot(
        snapshot=SimpleNamespace(portfolio_id=portfolio.id, portfolio=portfolio),
        factor_model_run_id="",
        factor_set_id=None,
        rule_id=rule.id,
        rule_version=None,
        members=[{"symbol_id": symbol.id}],
        cost_config={"min_lot_size": 100},
        price_hints={symbol.id: 10.0},
    )
    engine = DecisionEngine(
        snapshot_loader=lambda _db, _snapshot_id: snapshot,
        gate=lambda _db, _snapshot, _clock: (True, [], {"passed": True}),
        universe_builder=lambda _db, _snapshot, _cutoff: UniverseAndEligibility(
            universe=list(snapshot.members), universe_count=1, member_count=1,
        ),
        health=lambda _db, universe, _cutoff: universe,
        scorer=lambda _db, _snapshot, _universe, _cutoff, _trade_date: ScoredUniverse(
            expected=1,
            actual=1,
            coverage_pct=100.0,
            items=[{
                "symbol_id": symbol.id,
                "quality_score": 0.90,
                "stage": "growth",
            }],
        ),
        signal=lambda _db, _snapshot, _scored: SignalResult(items=[{
            "symbol_id": symbol.id,
            "direction": "BUY",
            "stage": "growth",
        }]),
    )

    result = engine.evaluate(
        db_session,
        portfolio_id=portfolio.id,
        strategy_snapshot_id="stage3-volume-cap-snapshot",
        trade_date=datetime(2026, 8, 21).date(),
        dry_run=True,
        state_context=DecisionStateContext(
            available_cash=100_000.0,
            total_capital=100_000.0,
            price_data_by_symbol={
                symbol.id: {
                    "open_price": 10.0,
                    "close_price": 10.0,
                    "high_price": 10.0,
                    "low_price": 10.0,
                    # 10% is 150 shares; the 100-share lot restricts the
                    # plan to one lot, rather than the raw 500-share intent.
                    "volume": 1_500,
                },
            },
            cost_config={"min_lot_size": 100, "volume_limit_pct": 0.10},
        ),
    )

    evidence = result.evidence[0]
    assert evidence.action == "BUY"
    assert evidence.target_quantity == 100.0
    volume_step = next(
        step for step in evidence.constraints
        if step["step"] == "VOLUME_PARTICIPATION_CAP"
    )
    assert volume_step["trigger"] == "VOLUME_LIMIT"
    assert volume_step["before_pct"] == 0.05
    assert volume_step["after_pct"] == 0.01
