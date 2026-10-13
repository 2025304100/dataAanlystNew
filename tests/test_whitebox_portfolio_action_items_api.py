"""白盒测试 - GET /portfolios/{id}/auto-trade/action-items（P0）。

不碰真实数据库：mock 组合加载与聚合函数，验证路由契约、for_schedule 透传、
404 形态，以及编排函数只读（不 add/commit/flush）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import event, inspect, text
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.api.routes import auto_trade as atr
from app.services import portfolio_action_items as pai
from app.services.auto_trade_readiness import AutoTradeReadinessResult

pytestmark = pytest.mark.whitebox


@pytest.fixture(autouse=True)
def isolate_member_source_overrides(monkeypatch):
    monkeypatch.delenv("AUTO_TRADE_MEMBER_SOURCE_PORTFOLIOS", raising=False)
    monkeypatch.delenv("AUTO_TRADE_MEMBER_SOURCE_EXCLUDED_PORTFOLIOS", raising=False)


def _healthy_readiness() -> AutoTradeReadinessResult:
    return AutoTradeReadinessResult(
        ready=True,
        enabled=True,
        account_ready=True,
        data_ready=True,
        source_ready=True,
        schedule_ready=True,
        position_count=1,
        candidate_count=1,
        eligible_buy_count=1,
        auto_member_count=1,
        blockers=[],
        warnings=[],
    )


@pytest.fixture
def client():
    from app.main import app

    def _override():
        yield {}

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def test_action_items_200_envelopes_portfolio_id(client):
    fake = {
        "items": [{"id": "data-freshness"}],
        "summary": {"blockers": 1, "warnings": 0, "info": 0, "ready": False},
        "generated_at": "t",
    }
    with patch.object(atr, "_get_portfolio_or_404", return_value=object()), patch.object(
        atr, "build_portfolio_action_items", return_value=fake
    ) as build:
        resp = client.get("/api/v1/portfolios/7/auto-trade/action-items")

    assert resp.status_code == 200
    body = resp.json()
    assert body["portfolio_id"] == 7
    assert body["items"][0]["id"] == "data-freshness"
    # 默认与顶栏就绪检查一致：for_schedule=True；portfolio_id 经位置参数传入。
    assert build.call_args.args[1] == 7
    assert build.call_args.kwargs["for_schedule"] is True


def test_for_schedule_query_passthrough(client):
    fake = {
        "items": [],
        "summary": {"blockers": 0, "warnings": 0, "info": 0, "ready": True},
        "generated_at": "t",
    }
    with patch.object(atr, "_get_portfolio_or_404", return_value=object()), patch.object(
        atr, "build_portfolio_action_items", return_value=fake
    ) as build:
        resp = client.get("/api/v1/portfolios/7/auto-trade/action-items?for_schedule=false")

    assert resp.status_code == 200
    assert build.call_args.kwargs["for_schedule"] is False


def test_action_items_404_when_portfolio_missing(client):
    from fastapi import HTTPException

    def _raise(db, portfolio_id):
        raise HTTPException(status_code=404, detail="Portfolio not found")

    with patch.object(atr, "_get_portfolio_or_404", side_effect=_raise):
        resp = client.get("/api/v1/portfolios/999/auto-trade/action-items")
    assert resp.status_code == 404


def test_build_orchestration_is_read_only():
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        account_type="simulated", status_score="READY", status_data="READY",
        status_model="READY", status_reconciliation="READY",
    )
    with patch.object(pai, "get_auto_trade_readiness", return_value=_healthy_readiness()), patch.object(
        pai, "_psm_get_status", return_value="READY"
    ), patch.object(pai, "evaluate_g7_operational_status") as g7, patch.object(
        pai, "_execution_prerequisites", return_value=[]
    ), patch.object(
        pai, "_binding_review", return_value={"requires_confirmation": False}
    ):
        g7.return_value = SimpleNamespace(operational_blockers=[])
        out = pai.build_portfolio_action_items(db, 123)

    assert out["summary"]["ready"] is True
    # 只读取数：绝不允许编排过程写库。
    assert not db.add.called
    assert not db.commit.called
    assert not db.flush.called


def test_real_orchestration_does_not_create_legacy_column_or_flush_pending_work(db_session, monkeypatch):
    """Run all DB readers on an isolated database; do not mock their results."""
    from datetime import date
    from app.models.portfolio import Portfolio
    from app.models.symbol import Symbol

    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    portfolio = Portfolio(
        name="read-only-action-items", account_type="simulated", auto_trade_enabled=1,
        total_capital=100000, investable_ratio=1, cash_reserve_ratio=0,
        last_decision_trade_date=date(2026, 10, 12),
        last_reconciled_trade_date=date(2026, 10, 12),
    )
    db_session.add(portfolio)
    db_session.commit()
    portfolio_id = portfolio.id
    assert "portfolio_status" not in {column["name"] for column in inspect(db_session.connection()).get_columns("portfolios")}

    portfolio.name = "pending-unrelated-rename"
    pending_symbol = Symbol(symbol="UNFLUSHED", name="Pending", market="SH", asset_type="stock")
    db_session.add(pending_symbol)
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", observe)
    try:
        with patch.object(db_session, "commit", wraps=db_session.commit) as commit, patch.object(
            db_session, "flush", wraps=db_session.flush
        ) as flush, patch.object(db_session, "add", wraps=db_session.add) as add:
            out = pai.build_portfolio_action_items(db_session, portfolio_id)
            assert not commit.called
            assert not flush.called
            assert not add.called
        persisted_name = db_session.connection().execute(
            text("SELECT name FROM portfolios WHERE id=:id"), {"id": portfolio_id}
        ).scalar_one()
        assert persisted_name == "read-only-action-items"
        assert pending_symbol in db_session.new
        assert all(statement.lstrip().upper().startswith(("SELECT", "PRAGMA")) for statement in statements)
        assert out["summary"]["ready"] is False
        assert any(item["code"] == "STRATEGY_SNAPSHOT_REQUIRED" for item in out["items"])
        assert any(item["code"] == "SCHEDULE_DISABLED" and item["severity"] == "blocker" for item in out["items"])
        assert "portfolio_status" not in {column["name"] for column in inspect(db_session.connection()).get_columns("portfolios")}
    finally:
        event.remove(db_session.bind, "before_cursor_execute", observe)


def test_real_execution_prerequisites_use_applied_snapshots_and_the_emergency_switch(db_session, monkeypatch):
    from datetime import datetime
    from app.models.decision_engine import StrategyExecutionSnapshot
    from app.models.portfolio import Portfolio

    portfolio = Portfolio(
        name="source-snapshot-prerequisites", account_type="simulated",
        total_capital=100000, investable_ratio=1, cash_reserve_ratio=0,
    )
    db_session.add(portfolio)
    db_session.flush()
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "false")
    codes = {issue.code for issue in pai._execution_prerequisites(db_session, portfolio.id)}
    assert codes == {"DECISION_ENGINE_SOURCE_REQUIRED", "STRATEGY_SNAPSHOT_REQUIRED"}

    snapshot = StrategyExecutionSnapshot(
        id="preflight-only", portfolio_id=portfolio.id, snapshot_no=1,
        decision_clock_json="{}", member_snapshot_json="[]", snapshot_type="preflight",
        snapshot_hash="a" * 64, effective_from=datetime(2026, 10, 13),
    )
    db_session.add(snapshot)
    db_session.flush()
    assert "STRATEGY_SNAPSHOT_REQUIRED" in {issue.code for issue in pai._execution_prerequisites(db_session, portfolio.id)}
    snapshot.snapshot_type = "save_and_apply"
    db_session.flush()
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    assert pai._execution_prerequisites(db_session, portfolio.id) == []


def test_real_candidate_with_fresh_hold_signal_waits_without_changing_execution_readiness(db_session, monkeypatch):
    from datetime import datetime, timezone
    from app.models.daily_bar import DailyBar
    from app.models.decision_engine import PortfolioFactorUsage, StrategyExecutionSnapshot
    from app.models.portfolio import Portfolio, PortfolioRule
    from app.models.portfolio_candidate import PortfolioCandidate
    from app.models.scheduled_task import ScheduledTask
    from app.models.score import Score
    from app.models.symbol import Symbol
    from app.services.auto_trade_readiness import get_auto_trade_readiness
    from tests.test_stage3_factor_binding_contract import _activate, _ready_model

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", "true")
    portfolio = Portfolio(
        name="waiting-fresh-candidate", account_type="simulated", auto_trade_enabled=1,
        total_capital=100000, investable_ratio=1, cash_reserve_ratio=0,
        last_decision_trade_date=now.date(), last_reconciled_trade_date=now.date(),
    )
    symbol = Symbol(symbol="WAITING-HOLD", name="Waiting", market="SH", asset_type="stock")
    db_session.add_all([portfolio, symbol])
    db_session.flush()
    model = _ready_model(db_session, set_id="fs-waiting", model_id="model-waiting")
    _activate(db_session, model.id)
    usage = PortfolioFactorUsage(
        id="waiting-usage", portfolio_id=portfolio.id,
        factor_model_run_id=model.id, factor_set_id="fs-waiting",
        status="active", run_mode="production_sim", pit_mode="strict_pit_safe",
        effective_from=now, content_hash="waiting-usage-hash",
    )
    db_session.add(usage)
    db_session.flush()
    db_session.add_all([
        PortfolioCandidate(portfolio_id=portfolio.id, symbol_id=symbol.id),
        PortfolioRule(
            portfolio_id=portfolio.id, rule_name="valid-rule", is_active=1,
            max_single_position_pct=0.3, max_sector_position_pct=0.6,
            max_stock_position_pct=1, max_etf_position_pct=1,
            max_loss_per_trade_pct=0.1, max_open_positions=10,
            stage_limits_json='{"stock":{"trend":0.3}}',
        ),
        ScheduledTask(name="waiting-schedule", task_type="portfolio_auto_trade", enabled=1),
        DailyBar(
            symbol_id=symbol.id, trade_date=now.date(), created_at=now,
            open=10, high=11, low=9, close=10, volume=100000,
        ),
        Score(
            symbol_id=symbol.id, trade_date=now.date(), created_at=now,
            quality_score=70, quality_grade="B", timing_score=50,
            stage="trend", action="hold", priority_score=60,
        ),
        StrategyExecutionSnapshot(
            id="waiting-applied-snapshot", portfolio_id=portfolio.id,
            portfolio_factor_usage_id=usage.id, factor_model_run_id=model.id, factor_set_id="fs-waiting",
            decision_clock_json="{}", member_snapshot_json="[]", snapshot_type="save_and_apply",
            snapshot_hash="b" * 64, effective_from=now,
        ),
    ])
    db_session.commit()
    actual_readiness = get_auto_trade_readiness(db_session, portfolio.id, for_schedule=True)
    assert actual_readiness.ready is False
    assert {issue.code for issue in actual_readiness.blockers} == {"NO_TRADEABLE_RANGE"}
    out = pai.build_portfolio_action_items(db_session, portfolio.id)
    assert out["summary"]["ready"] is False
    assert out["summary"]["execution_state"] == "waiting_signal"
    assert out["summary"]["waiting_for_signal"] is True
    assert out["summary"]["blockers"] == 0
    assert "NO_TRADEABLE_RANGE" not in {item["code"] for item in out["items"]}
    assert any(item["code"] == "NO_BUY_SIGNALS_TODAY" and item["severity"] == "info" for item in out["items"])
