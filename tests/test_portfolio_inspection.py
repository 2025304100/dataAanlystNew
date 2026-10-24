"""Real isolated DB evidence, repair checkpoints, and HTTP contract regression."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text

from app.api.routes.portfolio_inspection import router
from app.db.session import get_db
from app.models.app_setting import AppSetting
from app.models.async_task import AsyncTaskRecord
from app.models.daily_bar import DailyBar
from app.models.market_data import MarketCalendar
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_candidate import PortfolioCandidate
from app.models.portfolio_member import PortfolioMember
from app.models.score import Score
from app.models.scoring_config import ScoringConfig
from app.models.symbol import Symbol
from app.schemas.portfolio_inspection import InspectionRepairSubmit
from app.services import portfolio_inspection_diagnostics as diagnostics
from app.services import portfolio_inspection_repair as repair
from app.services.auto_trade_safety import check_data_health

pytestmark = pytest.mark.whitebox
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


def _calendar(db):
    start, end = date(2026, 1, 1), date(2026, 12, 31)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    # A deterministic exchange-calendar fixture with 243 open days. The test
    # verifies trust/provenance/complete coverage, never a weekday fallback.
    closures = set([day for day in days if day.weekday() < 5][:18])
    calendar = {day: int(day.weekday() < 5 and day not in closures) for day in days}
    db.add_all([MarketCalendar(trade_date=day, is_trading_day=flag) for day, flag in calendar.items()])
    db.add(AppSetting(key="market.cn_trade_calendar", updated_by="exchange-calendar-sync",
        value_json=json.dumps({"year": 2026, "source": diagnostics.CN_SOURCE,
            "trading_days": sum(calendar.values()), "refreshed_at": "2026-10-01T00:00:00"})))


def _seed(db, *, count=2, calendar=True):
    portfolio = Portfolio(name="inspection-only", account_type="simulated", total_capital=100000,
        investable_ratio=1, cash_reserve_ratio=0, auto_trade_source_mode="portfolio")
    config = ScoringConfig(asset_type="stock", preset_key="test-explicit", name="实际配置", version=3,
        is_active=1, config_json=json.dumps({"asset_type": "stock", "dimensions": [
            {"key": "trend", "name": "趋势", "enabled": True,
                "factors": [{"key": "trend_score", "source": "builtin"}]},
            {"key": "activity", "name": "成交活跃", "enabled": True,
                "factors": [{"key": "amount_activity", "source": "daily_bar"}]},
        ]}))
    db.add_all([portfolio, config])
    symbols = [Symbol(symbol=f"60000{i}", name=f"标的{i}", asset_type="stock", market="SH") for i in range(count)]
    db.add_all(symbols)
    db.flush()
    for symbol in symbols:
        db.add(PortfolioCandidate(portfolio_id=portfolio.id, symbol_id=symbol.id, effective_from=date(2026, 1, 1)))
    if calendar:
        _calendar(db)
    db.commit()
    return portfolio, config, symbols


def _bar(db, symbol, day, **kwargs):
    row = DailyBar(symbol_id=symbol.id, trade_date=day, open=10, high=11, low=9, close=10,
        amount=100000, volume=10000, created_at=NOW - timedelta(hours=1), **kwargs)
    db.add(row)
    return row


def _score(db, symbol, config, day, **kwargs):
    row = Score(symbol_id=symbol.id, trade_date=day, quality_score=60, quality_grade="B", timing_score=60,
        stage="rise", action="buy", priority_score=60, trend_score=65,
        calc_batch_id=kwargs.pop("calc_batch_id", "inspection"), scoring_config_id=config.id,
        scoring_config_version=config.version, scoring_preset_name=config.name,
        scoring_config_snapshot_json=config.config_json, factor_scores_json=json.dumps({"amount_activity": {"normalized_value": 70}}),
        created_at=NOW - timedelta(hours=1), published_at=NOW - timedelta(hours=1), **kwargs)
    db.add(row)
    return row


def test_all_symbols_and_metrics_are_aggregated_by_business_day(db_session):
    portfolio, config, symbols = _seed(db_session)
    _bar(db_session, symbols[0], date(2026, 10, 20))
    _score(db_session, symbols[0], config, date(2026, 10, 20))
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    assert result["summary"]["symbol_count"] == 2
    assert result["summary"]["stale"] >= 2
    assert result["summary"]["missing"] >= 2
    first, second = result["symbols"]
    assert first["expected_trade_date"] == "2026-10-22"
    assert first["actual_bar_trade_date"] == "2026-10-20"
    assert first["scoring"]["actual_config_version"] == 3
    assert first["scoring"]["actual_config_name"] == "实际配置"
    assert {e["label"] for e in first["evidence"]} >= {"日K行情", "适用评分", "趋势指标", "成交活跃指标"}
    assert second["symbol"] == symbols[1].symbol
    assert result["usable_for_admission"] is False
    assert len(result["repair_plan"]["operations"]) == 4


def test_trusted_calendar_does_not_follow_max_stored_market_date(db_session):
    portfolio, config, symbols = _seed(db_session, count=1)
    _bar(db_session, symbols[0], date(2026, 10, 19))
    _score(db_session, symbols[0], config, date(2026, 10, 19))
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    assert result["symbols"][0]["expected_trade_date"] == "2026-10-22"
    assert result["symbols"][0]["evidence"][0]["status"] == "STALE"
    assert result["symbols"][0]["evidence"][0]["trading_day_lag"] == 3


def test_weekend_and_intraday_respect_actual_due_day(db_session):
    portfolio, config, symbols = _seed(db_session, count=1)
    _bar(db_session, symbols[0], date(2026, 10, 23))
    _score(db_session, symbols[0], config, date(2026, 10, 23))
    db_session.commit()
    sunday = datetime(2026, 10, 25, 9, tzinfo=timezone.utc)
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=sunday)
    assert result["symbols"][0]["expected_trade_date"] == "2026-10-23"
    assert result["symbols"][0]["evidence"][0]["status"] == "PASSED"
    preclose = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=preclose)
    assert result["symbols"][0]["expected_trade_date"] == "2026-10-23"
    assert any(e["status"] == "NOT_DUE" for e in result["symbols"][0]["evidence"])


def test_unknown_calendar_never_enables_automatic_repair(db_session):
    portfolio, config, symbols = _seed(db_session, count=1, calendar=False)
    _bar(db_session, symbols[0], date(2026, 10, 22))
    _score(db_session, symbols[0], config, date(2026, 10, 22))
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    assert result["symbols"][0]["expected_trade_date"] is None
    assert result["summary"]["calendar_unknown"] >= 1
    assert not any(op["enabled"] for op in result["repair_plan"]["operations"])


def test_calendar_rows_without_provenance_are_unknown(db_session):
    portfolio, _, _ = _seed(db_session, count=1)
    state = db_session.get(AppSetting, "market.cn_trade_calendar")
    state.updated_by = "unverified-import"
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    assert result["calendars"][0]["status"] == "CALENDAR_UNKNOWN"


def test_read_error_is_not_missing_and_does_not_drop_other_checks(db_session):
    portfolio, _, _ = _seed(db_session, count=2)
    original = db_session.execute
    def execute(stmt, *args, **kwargs):
        if "FROM daily_bars" in str(stmt):
            raise RuntimeError("SECRET_CONNECTION_STRING")
        return original(stmt, *args, **kwargs)
    with patch.object(db_session, "execute", side_effect=execute):
        result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    assert result["summary"]["read_error"] >= 2
    assert sum(e["status"] == "READ_ERROR" and e["check_code"] == "DAILY_BAR_FRESHNESS"
        for row in result["symbols"] for e in row["evidence"]) == 2
    assert result["summary"]["missing"] >= 2
    assert "SECRET_CONNECTION_STRING" not in json.dumps(result)
    assert all(len(row["evidence"]) >= 3 for row in result["symbols"])


def test_latest_wrong_configuration_is_explicit_version_mismatch(db_session):
    portfolio, config, symbols = _seed(db_session, count=1)
    _bar(db_session, symbols[0], date(2026, 10, 22))
    score = _score(db_session, symbols[0], config, date(2026, 10, 22))
    score.scoring_config_version = 2
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    ev = next(ev for ev in result["symbols"][0]["evidence"] if ev["check_code"] == "SCORE_FRESHNESS")
    assert ev["status"] == "VERSION_MISMATCH"
    assert result["symbols"][0]["scoring"]["actual_config_version"] == 2


def test_inspection_and_plan_are_read_only_even_with_pending_work(db_session):
    portfolio, _, _ = _seed(db_session, count=1)
    portfolio.name = "unflushed-user-edit"
    db_session.add(Symbol(symbol="PENDING-INSPECTION", name="pending", asset_type="stock", market="SH"))
    statements = []
    def observe(_conn, _cursor, sql, *_args):
        statements.append(sql)
    event.listen(db_session.bind, "before_cursor_execute", observe)
    try:
        result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", observe)
    assert result["read_only"] is True
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE")) for sql in statements)


def test_execution_gate_and_inspection_share_business_dates(db_session):
    portfolio, config, symbols = _seed(db_session, count=1)
    _bar(db_session, symbols[0], date(2026, 10, 20))
    _score(db_session, symbols[0], config, date(2026, 10, 20))
    db_session.commit()
    with patch("app.services.auto_trade_safety._now_utc_naive", return_value=NOW.replace(tzinfo=None)):
        health = check_data_health(db_session, symbol_id=symbols[0].id)
    assert health.healthy is False
    assert health.kline_trade_date == date(2026, 10, 20)
    assert health.expected_trade_date == date(2026, 10, 22)
    assert "STALE" in health.reason_codes
    assert len(health.checks) >= 3


def _request(result, *, key="inspection-request-001", ids=None):
    plan = result["repair_plan"]
    return InspectionRepairSubmit(plan_id=plan["plan_id"], snapshot_id=result["snapshot_id"],
        generated_at=result["generated_at"], operation_ids=ids or [op["operation_id"] for op in plan["operations"] if op["enabled"]],
        confirmed=True, idempotency_key=key)


def _current_inspection(db, portfolio_id):
    # Business clock stays deterministic while actual preview TTL uses now.
    real = diagnostics.build_portfolio_inspection(db, portfolio_id, now=NOW)
    real["generated_at"] = datetime.now(timezone.utc).isoformat()
    return real


def test_confirmed_plan_is_scoped_idempotent_and_detects_changes(db_session):
    portfolio, _, symbols = _seed(db_session, count=2)
    result = _current_inspection(db_session, portfolio.id)
    request = _request(result)
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker") as start:
        task = repair.submit_inspection_repair(db_session, portfolio.id, request)
        again = repair.submit_inspection_repair(db_session, portfolio.id, request)
    assert task["task_id"] == again["task_id"]
    assert again["replayed"] is True
    assert start.call_count == 1
    assert {op["symbol_id"] for op in task["objects"]} == {symbol.id for symbol in symbols}
    assert len(task["audit_refs"]) == 1
    changed = {**result, "snapshot_id": "a" * 64}
    request = request.model_copy(update={"idempotency_key": "inspection-request-002"})
    with patch.object(repair, "build_portfolio_inspection", return_value=changed):
        with pytest.raises(repair.InspectionRepairError, match="数据或配置已变化"):
            repair.submit_inspection_repair(db_session, portfolio.id, request)


def test_dependencies_must_be_selected_and_disabled_actions_rejected(db_session):
    portfolio, _, _ = _seed(db_session, count=1)
    result = _current_inspection(db_session, portfolio.id)
    score_op = next(op for op in result["repair_plan"]["operations"] if op["kind"] == "recalculate_rule_score")
    request = _request(result, ids=[score_op["operation_id"]])
    with patch.object(repair, "build_portfolio_inspection", return_value=result):
        with pytest.raises(repair.InspectionRepairError) as error:
            repair.submit_inspection_repair(db_session, portfolio.id, request)
    assert error.value.code == "REPAIR_DEPENDENCY_REQUIRED"
    score_op["enabled"] = False
    with patch.object(repair, "build_portfolio_inspection", return_value=result):
        with pytest.raises(repair.InspectionRepairError) as error:
            repair.submit_inspection_repair(db_session, portfolio.id, request)
    assert error.value.code == "REPAIR_OPERATION_DISABLED"


def test_worker_retains_success_checkpoints_and_retry_only_failed_objects(db_session):
    portfolio, _, symbols = _seed(db_session, count=2)
    result = _current_inspection(db_session, portfolio.id)
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker"):
        created = repair.submit_inspection_repair(db_session, portfolio.id, _request(result))
    def operation(db, op):
        if op["symbol_id"] == symbols[1].id:
            raise repair.InspectionRepairError("SOURCE_DOWN", "测试数据源不可用")
        if op["kind"] == "sync_daily_bars":
            _bar(db, symbols[0], date(2026, 10, 22))
        return {"actual_trade_date": "2026-10-22"}
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_execute_operation", side_effect=operation):
        repair._run_inspection_repair(created["task_id"])
    db_session.expire_all()
    task = repair.get_inspection_repair_task(db_session, portfolio.id, created["task_id"])
    assert task["status"] == "partial"
    assert task["ok_count"] == 2
    assert task["failed_count"] == 2
    assert db_session.execute(select(DailyBar).where(DailyBar.symbol_id == symbols[0].id)).scalars().first() is not None
    assert task["reinspection"]["snapshot_id"] == result["snapshot_id"]
    with patch.object(repair, "_start_worker"):
        retried = repair.retry_inspection_repair(db_session, portfolio.id, created["task_id"], idempotency_key="retry-failed-request")
    assert retried["parent_task_id"] == created["task_id"]
    assert {op["symbol_id"] for op in retried["objects"]} == {symbols[1].id}
    assert all(op["symbol_id"] != symbols[0].id for op in retried["objects"])


def test_http_schema_read_only_gets_404_and_confirmation_required(db_session):
    portfolio, _, _ = _seed(db_session, count=1)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    url = f"/api/v1/portfolios/{portfolio.id}/inspection"
    with patch("app.api.routes.portfolio_inspection.build_portfolio_inspection", return_value=_current_inspection(db_session, portfolio.id)):
        response = client.get(url + "/data-diagnostics")
    assert response.status_code == 200
    assert response.json()["repair_plan"]["operations"]
    assert client.get("/api/v1/portfolios/999999/inspection/data-diagnostics").status_code == 404
    assert client.post(url + "/repairs", json={"confirmed": False}).status_code == 422
    assert client.get(url + "/repairs/nonexistent").status_code == 404


def test_future_publication_and_non_cn_market_are_never_green(db_session):
    portfolio, config, symbols = _seed(db_session, count=2)
    _bar(db_session, symbols[0], date(2026, 10, 22))
    score = _score(db_session, symbols[0], config, date(2026, 10, 22))
    score.published_at = NOW + timedelta(hours=1)
    symbols[1].market = "NASDAQ"
    _bar(db_session, symbols[1], date(2026, 10, 22))
    _score(db_session, symbols[1], config, date(2026, 10, 22))
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    assert any(e["status"] == "FUTURE_DATA" for e in result["symbols"][0]["evidence"])
    assert result["symbols"][1]["expected_trade_date"] is None
    assert next(row for row in result["calendars"] if row["market"] == "NASDAQ")["status"] == "CALENDAR_UNKNOWN"


def test_malformed_config_has_disabled_repair_instead_of_fabricated_indicators(db_session):
    portfolio, config, _ = _seed(db_session, count=1)
    config.config_json = '{"dimensions":"broken"}'
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    assert result["symbols"][0]["scoring"]["current_config_valid"] is False
    operation = next(op for op in result["repair_plan"]["operations"] if op["kind"] == "recalculate_rule_score")
    assert operation["enabled"] is False
    assert operation["disabled_code"] == "SCORING_INPUT_UNVERIFIED"
    assert not any(e["check_code"] == "RULE_INDICATOR" for e in result["symbols"][0]["evidence"])


def test_multiple_scope_memberships_are_one_real_symbol_and_member_rule_is_unknown(db_session):
    portfolio, _, symbols = _seed(db_session, count=1)
    symbol = symbols[0]
    db_session.add(Position(portfolio_id=portfolio.id, symbol_id=symbol.id, quantity=10, asset_type="stock"))
    db_session.add(PortfolioMember(portfolio_id=portfolio.id, symbol_id=symbol.id,
        execution_mode="auto", status="active", effective_from=datetime(2026, 1, 1), entry_rule_version_id=99))
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    assert result["summary"]["symbol_count"] == 1
    assert set(result["symbols"][0]["scopes"]) == {"position", "candidate", "auto_member"}
    assert any(e["check_code"] == "MEMBER_RULE_VERSION" and e["status"] == "UNKNOWN" for e in result["symbols"][0]["evidence"])


def test_model_scoring_repair_is_explicitly_disabled_and_frozen_factors_are_read(db_session):
    from app.models.decision_engine import StrategyExecutionSnapshot
    from app.models.factor import Factor
    from app.models.factor_evaluation import FactorSet, FactorSetMember
    from app.models.factor_model import FactorVersion
    portfolio, config, symbols = _seed(db_session, count=1)
    factor = Factor(code="inspection_volume", name="冻结成交量指标", category="volume", direction="higher_better",
        status="active", frequency="daily")
    db_session.add(factor)
    db_session.flush()
    version = FactorVersion(factor_id=factor.id, version=2, formula_expr="volume", validation_status="valid")
    factor_set = FactorSet(id="inspection-frozen-fs", name="冻结指标集", status="frozen")
    db_session.add_all([version, factor_set])
    db_session.flush()
    db_session.add(FactorSetMember(factor_set_id=factor_set.id, factor_id=factor.id, factor_version_id=version.id,
        factor_code=factor.code, factor_version=2, role="feature"))
    db_session.add(StrategyExecutionSnapshot(id="inspection-model-snapshot", portfolio_id=portfolio.id,
        factor_model_run_id="real-bound-model", factor_set_id=factor_set.id, snapshot_no=1,
        decision_clock_json="{}", member_snapshot_json="[]", snapshot_hash="a" * 64,
        snapshot_type="save_and_apply", effective_from=datetime(2026, 1, 1)))
    _bar(db_session, symbols[0], date(2026, 10, 22))
    _score(db_session, symbols[0], config, date(2026, 10, 22), factor_model_run_id="different-model", weight_mode="ridge")
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    assert result["binding"]["model_run_id"] == "real-bound-model"
    evidence = result["symbols"][0]["evidence"]
    assert any(e["label"] == "冻结成交量指标" and e["status"] == "MISSING" for e in evidence)
    score_ev = next(e for e in evidence if e["check_code"] == "SCORE_FRESHNESS")
    assert score_ev["status"] == "VERSION_MISMATCH"
    operation = next(op for op in result["repair_plan"]["operations"] if op["kind"] == "recalculate_model_score")
    assert not operation["enabled"]
    assert operation["disabled_code"] == "MODEL_SCORING_REQUIRES_FROZEN_PIPELINE"


def test_actual_rule_score_executor_persists_explicit_configuration(db_session):
    from app.services.scoring_config_engine import STOCK_SYSTEM_PRESETS
    portfolio, config, symbols = _seed(db_session, count=1)
    config.config_json = json.dumps(STOCK_SYSTEM_PRESETS[0])
    for i in range(10):
        _bar(db_session, symbols[0], date(2026, 10, 22) - timedelta(days=i))
    db_session.commit()
    output = repair._execute_operation(db_session, {"kind": "recalculate_rule_score", "symbol_id": symbols[0].id,
        "trade_date": "2026-10-22", "config_id": config.id, "config_version": 3})
    db_session.commit()
    score = db_session.execute(select(Score).where(Score.symbol_id == symbols[0].id)).scalars().first()
    assert score.scoring_config_id == config.id
    assert score.scoring_config_version == 3
    assert score.weight_mode == "manual"
    assert output["source_record_ref"] == f"score:{score.id}"
    assert score.factor_scores_json


def test_sync_executor_checks_actual_requested_output_and_never_claims_empty_success(db_session):
    _, _, symbols = _seed(db_session, count=1)
    operation = {"kind": "sync_daily_bars", "symbol_id": symbols[0].id, "trade_date": "2026-10-22",
        "start_date": "2026-10-22", "end_date": "2026-10-22"}
    with patch("app.services.market_data.sync_symbol_daily_bars", return_value={"status": "ok"}):
        with pytest.raises(repair.InspectionRepairError) as error:
            repair._execute_operation(db_session, operation)
    assert error.value.code == "REPAIR_BAR_NOT_PRODUCED"
    def sync(**kwargs):
        _bar(db_session, symbols[0], date(2026, 10, 22))
        assert kwargs["start_date"] == kwargs["end_date"] == date(2026, 10, 22)
        return {"status": "ok"}
    with patch("app.services.market_data.sync_symbol_daily_bars", side_effect=sync):
        output = repair._execute_operation(db_session, operation)
    assert output["actual_trade_date"] == "2026-10-22"


def test_identical_evidence_has_stable_snapshot_and_plan(db_session):
    portfolio, _, _ = _seed(db_session, count=1)
    first = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    later = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW + timedelta(minutes=2))
    assert first["snapshot_id"] == later["snapshot_id"]
    assert first["repair_plan"]["plan_id"] == later["repair_plan"]["plan_id"]
    assert first["generated_at"] != later["generated_at"]


def test_input_field_missing_is_not_hidden_by_normalized_indicator_score(db_session):
    portfolio, config, symbols = _seed(db_session, count=1)
    bar = _bar(db_session, symbols[0], date(2026, 10, 22))
    bar.amount = None
    _score(db_session, symbols[0], config, date(2026, 10, 22))
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    evidence = result["symbols"][0]["evidence"]
    assert any(e["check_code"] == "RAW_BAR_INPUT" and e["status"] == "MISSING" for e in evidence)
    assert any(e["label"] == "成交活跃指标" and e["status"] == "MISSING" for e in evidence)
    operation = next(op for op in result["repair_plan"]["operations"] if op["kind"] == "sync_daily_bars")
    assert operation["enabled"] is True
    assert operation["resolves_evidence_ids"] == [f"{symbols[0].id}:bar_input:amount"]


def test_new_score_using_stale_bar_inputs_is_explicitly_stale(db_session):
    portfolio, config, symbols = _seed(db_session, count=1)
    _bar(db_session, symbols[0], date(2026, 10, 22))
    score = _score(db_session, symbols[0], config, date(2026, 10, 22))
    score.factor_scores_json = json.dumps({"_data_freshness": {"latest_bar_trade_date": "2026-10-19"}})
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    assert next(e for e in result["symbols"][0]["evidence"] if e["check_code"] == "SCORE_FRESHNESS")["status"] == "STALE"


def test_http_auth_access_scope_and_authenticated_actor_are_preserved(db_session, monkeypatch):
    from fastapi import HTTPException
    from app.api.routes import portfolio_governance as governance
    from app.services.data_governance_audit import DataGovernanceAuditEvent
    portfolio, _, _ = _seed(db_session, count=1)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    url = f"/api/v1/portfolios/{portfolio.id}/inspection"
    monkeypatch.setenv("STRICT_AUTH", "1")
    assert client.get(url + "/data-diagnostics").status_code == 401
    checked = []
    def guard(_db, pid, actor):
        checked.append((pid, actor))
        if actor != "authorized-operator":
            raise HTTPException(403, detail={"error_code": "FORBIDDEN_PORTFOLIO_ACCESS"})
    monkeypatch.setattr(governance, "_ensure_portfolio_access", guard)
    assert client.get(url + "/data-diagnostics?X-User=another-operator").status_code == 403
    result = _current_inspection(db_session, portfolio.id)
    body = _request(result).model_dump(mode="json")
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker"):
        response = client.post(url + "/repairs?X-User=authorized-operator", json=body)
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    event_row = db_session.execute(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.business_key == task_id)).scalars().first()
    assert event_row.operator_id == "authorized-operator"
    assert event_row.correlation_id == response.json()["correlation_id"]
    assert json.loads(event_row.attributes_json)["issue_ids"] == response.json()["issue_ids"]
    assert client.get(f"/api/v1/portfolios/999999/inspection/repairs/{task_id}?X-User=authorized-operator").status_code == 404
    assert client.post(f"/api/v1/portfolios/999999/inspection/repairs/{task_id}/retry?X-User=authorized-operator",
        json={"confirmed": True, "idempotency_key": "cross-portfolio-retry"}).status_code == 404
    assert (portfolio.id, "another-operator") in checked


def test_successful_upstream_is_excluded_when_retrying_only_failed_score(db_session):
    portfolio, _, symbols = _seed(db_session, count=1)
    result = _current_inspection(db_session, portfolio.id)
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker"):
        task = repair.submit_inspection_repair(db_session, portfolio.id, _request(result))
    def execute(_db, op):
        if op["kind"] == "recalculate_rule_score":
            raise repair.InspectionRepairError("SCORE_WORKER_DOWN", "评分失败")
        return {"source_record_ref": "daily_bar:verified-test-record"}
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_execute_operation", side_effect=execute):
        repair._run_inspection_repair(task["task_id"])
    db_session.expire_all()
    with patch.object(repair, "_start_worker"):
        retry = repair.retry_inspection_repair(db_session, portfolio.id, task["task_id"], idempotency_key="retry-score-only")
    assert len(retry["objects"]) == 1
    assert retry["objects"][0]["kind"] == "recalculate_rule_score"
    assert retry["objects"][0]["depends_on"] == []


def test_source_failure_keeps_business_reason_and_next_action_in_persistent_task(db_session):
    from app.services.market_data_errors import etf_qfq_source_error
    portfolio, _, _ = _seed(db_session, count=1)
    result = _current_inspection(db_session, portfolio.id)
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker"):
        task = repair.submit_inspection_repair(db_session, portfolio.id, _request(result))
    error = etf_qfq_source_error(ConnectionError("provider connection closed; api_key=not-a-real-key"))
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_execute_operation", side_effect=error):
        repair._run_inspection_repair(task["task_id"])
    db_session.expire_all()
    saved = repair.get_inspection_repair_task(db_session, portfolio.id, task["task_id"])
    failed = next(row for row in saved["objects"] if row["status"] == "failed")
    assert failed["error_code"] == "ETF_QFQ_SOURCE_UNAVAILABLE"
    assert "连接中断" in failed["reason"]
    assert failed["source_label"] == "东方财富 · ETF前复权日K"
    assert "仅重试本标的" in failed["next_action"]
    assert "api_key" not in failed["reason"]
    assert "ETF_QFQ_SOURCE_UNAVAILABLE" in saved["recovery"]["error_codes"]
    assert saved["status"] == "failed"


def test_dispatch_failure_is_persisted_failed_with_retryable_objects(db_session):
    portfolio, _, _ = _seed(db_session, count=1)
    result = _current_inspection(db_session, portfolio.id)
    request = _request(result)
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker", side_effect=RuntimeError("no worker")):
        with pytest.raises(repair.InspectionRepairError) as error:
            repair.submit_inspection_repair(db_session, portfolio.id, request)
    assert error.value.code == "REPAIR_DISPATCH_FAILED"
    row = db_session.execute(select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == repair.TASK_TYPE)).scalars().first()
    task = repair.get_inspection_repair_task(db_session, portfolio.id, row.id)
    assert task["status"] == "failed"
    assert task["retryable_operation_ids"]


def test_current_read_refreshes_identity_map_and_locks_source_selects_only_when_requested(db_session):
    portfolio, config, symbols = _seed(db_session, count=1)
    _bar(db_session, symbols[0], date(2026, 10, 22))
    _score(db_session, symbols[0], config, date(2026, 10, 22))
    db_session.commit()
    portfolio_id = portfolio.id
    # Keep a stale ORM object while changing the database value underneath it.
    assert config.name == "实际配置"
    db_session.connection().execute(text("UPDATE scoring_configs SET name='database-current-value' WHERE id=:id"), {"id": config.id})
    assert config.name == "实际配置"
    captured = []
    original = db_session.execute
    def execute(statement, *args, **kwargs):
        if hasattr(statement, "_for_update_arg"):
            captured.append(statement)
        return original(statement, *args, **kwargs)
    with patch.object(db_session, "execute", side_effect=execute):
        result = diagnostics.build_portfolio_inspection(db_session, portfolio_id, now=NOW, current_read=True)
    assert result["symbols"][0]["scoring"]["current_config_name"] == "database-current-value"
    assert captured and all(statement._for_update_arg is not None for statement in captured), [str(stmt) for stmt in captured if stmt._for_update_arg is None]
    assert diagnostics._CURRENT_READ_KEY not in db_session.info
    captured.clear()
    with patch.object(db_session, "execute", side_effect=execute):
        diagnostics.build_portfolio_inspection(db_session, portfolio_id, now=NOW)
    assert captured and all(statement._for_update_arg is None for statement in captured)


def test_registered_applied_rule_does_not_invent_a_block_from_unused_member_version(db_session):
    from app.models.decision_engine import StrategyExecutionSnapshot
    from app.models.portfolio import PortfolioRule
    portfolio, config, symbols = _seed(db_session, count=1)
    rule = PortfolioRule(portfolio_id=portfolio.id, rule_name="实际策略", is_active=1,
        max_single_position_pct=.3, max_sector_position_pct=.6, max_stock_position_pct=1, max_etf_position_pct=1,
        max_loss_per_trade_pct=.1, max_open_positions=10, stage_limits_json="{}")
    db_session.add(rule)
    db_session.flush()
    db_session.add(StrategyExecutionSnapshot(id="verified-rule-snapshot", portfolio_id=portfolio.id, rule_id=rule.id,
        rule_version=1, snapshot_no=1, decision_clock_json="{}", member_snapshot_json="[]", snapshot_hash="a" * 64,
        snapshot_type="save_and_apply", effective_from=datetime(2026, 1, 1)))
    db_session.add(PortfolioMember(portfolio_id=portfolio.id, symbol_id=symbols[0].id, execution_mode="auto",
        status="active", effective_from=datetime(2026, 1, 1), entry_rule_version_id=9999))
    _bar(db_session, symbols[0], date(2026, 10, 22))
    _score(db_session, symbols[0], config, date(2026, 10, 22))
    db_session.commit()
    result = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    legacy = next(ev for ev in result["symbols"][0]["evidence"] if ev["check_code"] == "MEMBER_RULE_VERSION")
    assert legacy["status"] == "UNKNOWN"
    assert legacy["admission_relevant"] is False
    with patch("app.services.auto_trade_safety._now_utc_naive", return_value=NOW.replace(tzinfo=None)):
        health = check_data_health(db_session, symbol_id=symbols[0].id, portfolio_id=portfolio.id, rule_version_id=9999)
    assert health.healthy is True


def test_score_repair_rejects_in_place_config_mutation_after_confirmation(db_session):
    _, config, symbols = _seed(db_session, count=1)
    _bar(db_session, symbols[0], date(2026, 10, 22))
    fingerprint = diagnostics._hash(json.loads(config.config_json))
    config.config_json = json.dumps({"dimensions": []})
    db_session.commit()
    with pytest.raises(repair.InspectionRepairError) as error:
        repair._execute_operation(db_session, {"kind": "recalculate_rule_score", "symbol_id": symbols[0].id,
            "trade_date": "2026-10-22", "config_id": config.id, "config_version": config.version,
            "config_fingerprint": fingerprint})
    assert error.value.code == "REPAIR_CONFIG_CHANGED"


def test_calendar_version_and_diagnostic_identity_ignore_identical_content_resync(db_session):
    portfolio, _, _ = _seed(db_session, count=1)
    before = diagnostics.resolve_inspection_calendar(db_session, at=NOW)
    before_inspection = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    setting = db_session.get(AppSetting, "market.cn_trade_calendar")
    provenance = json.loads(setting.value_json)
    provenance["refreshed_at"] = "2026-10-22T08:00:00+00:00"
    provenance["changed"] = 0
    setting.value_json = json.dumps(provenance)
    db_session.commit()
    after = diagnostics.resolve_inspection_calendar(db_session, at=NOW)
    after_inspection = diagnostics.build_portfolio_inspection(db_session, portfolio.id, now=NOW)
    assert before["version"] == after["version"]
    assert before["synced_at"] != after["synced_at"]
    assert before_inspection["snapshot_id"] == after_inspection["snapshot_id"]
    assert before_inspection["repair_plan"]["plan_id"] == after_inspection["repair_plan"]["plan_id"]
    provenance["publication_version"] = "exchange-correction-2"
    setting.value_json = json.dumps(provenance)
    db_session.commit()
    released = diagnostics.resolve_inspection_calendar(db_session, at=NOW)
    assert released["version"] != after["version"]
    # A real date correction with unchanged annual open-day count still
    # invalidates the calendar's semantic version.
    db_session.get(MarketCalendar, date(2026, 1, 1)).is_trading_day = 1
    db_session.get(MarketCalendar, date(2026, 10, 19)).is_trading_day = 0
    db_session.commit()
    corrected = diagnostics.resolve_inspection_calendar(db_session, at=NOW)
    assert corrected["status"] == "PASSED"
    assert corrected["version"] != released["version"]


def test_repair_history_is_persistent_scoped_paginated_and_read_only(db_session):
    portfolio, _, _ = _seed(db_session, count=1)
    result = _current_inspection(db_session, portfolio.id)
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker"):
        first = repair.submit_inspection_repair(db_session, portfolio.id, _request(result))
        request = _request(result).model_copy(update={"idempotency_key": "second-persistent-repair"})
        second = repair.submit_inspection_repair(db_session, portfolio.id, request, actor="actual-repairer")
    db_session.add(AsyncTaskRecord(id="unrelated-repair", task_type=repair.TASK_TYPE, status="queued", stage="queued",
        payload_json=json.dumps({"portfolio_id": portfolio.id + 1, "operations": [], "actor": "other-actor"})))
    db_session.commit()
    before = db_session.execute(text("SELECT COUNT(*) FROM data_governance_audit_events")).scalar()
    page = repair.list_inspection_repair_tasks(db_session, portfolio.id, limit=1)
    assert [row["task_id"] for row in page["items"]] == [second["task_id"]]
    assert page["items"][0]["created_by"] == "actual-repairer"
    assert page["items"][0]["can_cancel"] is True
    assert page["next_cursor"] == second["task_id"]
    following = repair.list_inspection_repair_tasks(db_session, portfolio.id, limit=1, cursor=page["next_cursor"])
    assert [row["task_id"] for row in following["items"]] == [first["task_id"]]
    assert following["next_cursor"] is None
    assert db_session.execute(text("SELECT COUNT(*) FROM data_governance_audit_events")).scalar() == before
    assert not db_session.dirty and not db_session.new
    with pytest.raises(LookupError):
        repair.list_inspection_repair_tasks(db_session, portfolio.id, cursor="unrelated-repair")


def test_cancel_queued_repair_is_idempotent_and_retains_retryable_scope_and_actor(db_session):
    from app.services.data_governance_audit import DataGovernanceAuditEvent
    portfolio, _, _ = _seed(db_session, count=1)
    result = _current_inspection(db_session, portfolio.id)
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker"):
        task = repair.submit_inspection_repair(db_session, portfolio.id, _request(result))
    cancelled = repair.cancel_inspection_repair(db_session, portfolio.id, task["task_id"], actor="actual-canceller")
    repeated = repair.cancel_inspection_repair(db_session, portfolio.id, task["task_id"], actor="actual-canceller")
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] is True
    assert cancelled["can_cancel"] is False
    assert {row["status"] for row in cancelled["objects"]} == {"cancelled"}
    assert set(cancelled["retryable_operation_ids"]) == {row["operation_id"] for row in task["objects"]}
    assert repeated["replayed"] is True
    audit = db_session.scalars(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.business_key == task["task_id"])).all()
    cancellation = [row for row in audit if json.loads(row.attributes_json)["original_action"] == "REPAIR_TASK_CANCEL_REQUESTED"]
    assert len(cancellation) == 1 and cancellation[0].operator_id == "actual-canceller"
    with patch.object(repair, "_execute_operation") as execute:
        repair._run_inspection_repair(task["task_id"])
    execute.assert_not_called()
    with pytest.raises(LookupError):
        repair.cancel_inspection_repair(db_session, portfolio.id + 1, task["task_id"], actor="actual-canceller")


def test_cancel_between_objects_preserves_success_and_restart_exposes_only_unfinished_retry(db_session):
    from app.services.async_tasks import interrupt_orphaned_async_tasks
    portfolio, _, _ = _seed(db_session, count=1)
    result = _current_inspection(db_session, portfolio.id)
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker"):
        task = repair.submit_inspection_repair(db_session, portfolio.id, _request(result))
    def execute(worker_db, operation):
        repair.cancel_inspection_repair(worker_db, portfolio.id, task["task_id"], actor="cancel-between-objects")
        return {"source_record_ref": "retained-existing-test-artifact"}
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_execute_operation", side_effect=execute) as execute_mock:
        repair._run_inspection_repair(task["task_id"])
    db_session.expire_all()
    state = repair.get_inspection_repair_task(db_session, portfolio.id, task["task_id"])
    assert execute_mock.call_count == 1
    assert state["status"] == "cancelled"
    assert state["objects"][0]["status"] == "succeeded"
    assert state["objects"][0]["output"]["source_record_ref"] == "retained-existing-test-artifact"
    assert state["retryable_operation_ids"] == [state["objects"][1]["operation_id"]]
    with patch.object(repair, "_start_worker"):
        retry = repair.retry_inspection_repair(db_session, portfolio.id, task["task_id"], idempotency_key="retry-after-safe-cancel")
    retry_row = db_session.get(AsyncTaskRecord, retry["task_id"])
    retry_row.status = "running"
    db_session.commit()
    assert retry["task_id"] in interrupt_orphaned_async_tasks(db_session)
    recovered = repair.get_inspection_repair_task(db_session, portfolio.id, retry["task_id"])
    assert recovered["recovery"]["status"] == "RETRY_AVAILABLE"
    assert "BACKEND_RESTART_INTERRUPTED" in recovered["recovery"]["error_codes"]
    assert recovered["retryable_operation_ids"] == [state["objects"][1]["operation_id"]]


def test_repair_request_and_cancellation_roll_back_when_required_audit_cannot_be_saved(db_session):
    portfolio, _, _ = _seed(db_session, count=1)
    pid = portfolio.id
    result = _current_inspection(db_session, pid)
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker"), patch.object(
            repair, "write_audit_event", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            repair.submit_inspection_repair(db_session, pid, _request(result))
    assert list(db_session.scalars(select(AsyncTaskRecord))) == []
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker"):
        task = repair.submit_inspection_repair(db_session, pid, _request(result))
    with patch.object(repair, "write_audit_event", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            repair.cancel_inspection_repair(db_session, pid, task["task_id"], actor="actual-canceller")
    state = repair.get_inspection_repair_task(db_session, pid, task["task_id"])
    assert state["status"] == "queued" and state["cancel_requested"] is False


def test_terminal_record_does_not_allow_retry_while_original_worker_still_has_a_live_object(db_session):
    portfolio, _, _ = _seed(db_session, count=1)
    result = _current_inspection(db_session, portfolio.id)
    with patch.object(repair, "build_portfolio_inspection", return_value=result), patch.object(repair, "_start_worker"):
        task = repair.submit_inspection_repair(db_session, portfolio.id, _request(result))
    row = db_session.get(AsyncTaskRecord, task["task_id"])
    row.status, row.is_terminal_locked = "failed", 1
    db_session.commit()
    with patch.object(repair, "is_worker_running", return_value=True), patch.object(repair, "_start_worker") as start:
        state = repair.get_inspection_repair_task(db_session, portfolio.id, task["task_id"])
        assert state["recovery"]["status"] == "WAITING_WORKER" and state["retryable_operation_ids"] == []
        with pytest.raises(repair.InspectionRepairError) as error:
            repair.retry_inspection_repair(db_session, portfolio.id, task["task_id"], idempotency_key="no-overlapping-retry")
    assert error.value.code == "REPAIR_WORKER_STOPPING"
    start.assert_not_called()


@pytest.mark.parametrize("final_evidence", ["healthy", "stale", "read_error"])
def test_confirmed_repair_recovers_only_proven_data_dimensions_and_keeps_independent_locks(db_session, final_evidence):
    from app.services.data_governance_audit import DataGovernanceAuditEvent
    from app.services.portfolio_state_machine import _get_status, _set_status

    portfolio, config, symbols = _seed(db_session, count=2)
    pid = portfolio.id
    portfolio.status_data = "DATA_INCOMPLETE_PAUSED"
    portfolio.status_score = "SCORE_STALE"
    portfolio.status_model = "MODEL_INACTIVE"
    portfolio.status_reconciliation = "RECONCILIATION_BLOCKED"
    _set_status(db_session, portfolio, "ADMIN_PAUSED")
    db_session.commit()
    preview = _current_inspection(db_session, pid)
    with patch.object(repair, "build_portfolio_inspection", return_value=preview), patch.object(repair, "_start_worker"):
        task = repair.submit_inspection_repair(db_session, pid, _request(preview), actor="verified-repairer")

    executed = []
    def inspect(worker_db, portfolio_id, **kwargs):
        return diagnostics.build_portfolio_inspection(worker_db, portfolio_id, now=NOW,
            current_read=kwargs.get("current_read", False))

    def execute(worker_db, operation):
        executed.append(operation["operation_id"])
        if operation["symbol_id"] != symbols[0].id:
            raise repair.InspectionRepairError("SOURCE_DOWN", "第二个标的数据源不可用")
        day = date(2026, 10, 22) if final_evidence == "healthy" else date(2026, 10, 20)
        if operation["kind"] == "sync_daily_bars":
            _bar(worker_db, symbols[0], day)
        else:
            _score(worker_db, symbols[0], config, day)
        return {"actual_trade_date": day.isoformat()}

    # Only three objects execute: the second symbol's score is dependency-failed.
    # Trigger the source failure at the final recheck, not at its binding check.
    def final_read(worker_db, portfolio_id, **kwargs):
        if final_evidence == "read_error" and kwargs.get("current_read") and len(executed) == 3:
            raise RuntimeError("final source unavailable")
        return inspect(worker_db, portfolio_id, **kwargs)

    with patch.object(repair, "build_portfolio_inspection", side_effect=final_read), patch.object(
            repair, "_execute_operation", side_effect=execute):
        repair._run_inspection_repair(task["task_id"])
    db_session.expire_all()
    p = db_session.get(Portfolio, pid)
    expected = "READY" if final_evidence == "healthy" else None
    assert p.status_data == (expected or "DATA_INCOMPLETE_PAUSED")
    assert p.status_score == (expected or "SCORE_STALE")
    assert p.status_model == "MODEL_INACTIVE"
    assert p.status_reconciliation == "RECONCILIATION_BLOCKED"
    assert _get_status(db_session, p) == "ADMIN_PAUSED"
    saved = repair.get_inspection_repair_task(db_session, pid, task["task_id"])
    assert saved["status"] == "partial"
    recovery = saved["dimension_recovery"]
    assert recovery["changed_fields"] == (["status_score", "status_data"] if expected else [])
    assert recovery["aggregation_policy"] == "existing_any_healthy_symbol_v1"
    assert recovery["runtime_state_changed"] is False
    if expected:
        assert recovery["restricted_symbol_ids"] == [symbols[1].id]
    event = db_session.scalars(select(DataGovernanceAuditEvent).where(
        DataGovernanceAuditEvent.business_key == task["task_id"]).order_by(DataGovernanceAuditEvent.id.desc())).first()
    assert event.operator_id == "verified-repairer"
    assert json.loads(event.after_json)["dimension_recovery"] == recovery
