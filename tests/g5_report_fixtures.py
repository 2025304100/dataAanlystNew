"""Isolated persisted capture contracts; tests do not run or implement engines."""
import hashlib
import json
from datetime import date, datetime, timedelta

from sqlalchemy import select, text

from app.models.decision_engine import DecisionEvidence, DecisionRun, StrategyExecutionSnapshot
from app.models.scan import ScanResult, ScanRun
from app.models.symbol import Symbol

DATES = [f"2026-08-{day:02d}" for day in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14)]
ACTIONS = ("BUY", "SELL", "HOLD", "NO_ACTION", "REJECTED", "DATA_BLOCKED")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()).hexdigest()


def values(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _captured_rows(db, portfolio_id, *, eligible=True, cutoff_offset_days=0):
    """One checked symbol per day, ten real calendar days, actual source rows."""
    symbol = db.execute(select(Symbol).where(Symbol.symbol == "600987")).scalar_one_or_none()
    if symbol is None:
        symbol = Symbol(symbol="600987", name="隔离证据测试标的", asset_type="stock", market="sh")
        db.add(symbol)
        db.flush()
    db.execute(text("CREATE TABLE IF NOT EXISTS trade_calendar (date DATE PRIMARY KEY, is_trading_day INTEGER NOT NULL)"))
    for number in range(3, 15):
        day = f"2026-08-{number:02d}"
        db.execute(text("INSERT OR REPLACE INTO trade_calendar (date,is_trading_day) VALUES (:day,:open)"), {"day": day, "open": int(day in DATES)})
    snapshots = db.execute(select(StrategyExecutionSnapshot).where(StrategyExecutionSnapshot.portfolio_id == portfolio_id)).scalars().all()
    number = max((row.snapshot_no for row in snapshots), default=0) + 1
    snapshot = StrategyExecutionSnapshot(id=f"g5-proof-{portfolio_id}-{number}", portfolio_id=portfolio_id, snapshot_no=number,
        decision_clock_json="{}", member_snapshot_json=json.dumps([symbol.id]), snapshot_hash="e" * 64,
        snapshot_type="save_and_apply", effective_from=datetime(2026, 8, 1))
    db.add(snapshot)
    db.flush()
    daily, manifest = [], []
    for day in DATES:
        td = date.fromisoformat(day)
        cutoff = datetime.fromisoformat(day + "T07:00:00") + timedelta(days=cutoff_offset_days)
        decision = cutoff + timedelta(minutes=5)
        execution = cutoff + timedelta(days=1)
        old = ScanRun(run_name=f"proof-{portfolio_id}-{number}-{day}", portfolio_id=portfolio_id,
            scope_snapshot=json.dumps([symbol.id]), filters_snapshot=json.dumps({"trade_date": day, "data_cutoff_at": cutoff.isoformat()}),
            status="completed", total_in_snapshot=1, started_at=decision, finished_at=decision + timedelta(seconds=1))
        new = DecisionRun(id=f"g5-new-{portfolio_id}-{number}-{day}", portfolio_id=portfolio_id, strategy_snapshot_id=snapshot.id,
            run_type="research_preflight", status="SUCCEEDED", trade_date=td, decision_at=decision, data_cutoff_at=cutoff,
            execution_at=execution, started_at=decision, finished_at=decision + timedelta(seconds=1),
            universe_count=1, member_count=1, score_coverage_pct=100, blocking_status="READY", is_result_production_eligible=1)
        db.add_all([old, new])
        db.flush()
        a_row = ScanResult(scan_run_id=old.id, symbol_id=symbol.id, result_type="candidate", rank_no=1,
                           action="HOLD", quality_score=80, timing_score=80)
        b_row = DecisionEvidence(id=f"g5-row-{portfolio_id}-{number}-{day}", decision_run_id=new.id,
            portfolio_id=portfolio_id, strategy_snapshot_id=snapshot.id, symbol_id=symbol.id, trade_date=td,
            decision_at=decision, data_cutoff_at=cutoff, execution_at=execution, action="HOLD", content_hash="f" * 64)
        db.add_all([a_row, b_row])
        db.flush()
        # Export persisted DB values, including its normalized floats and UTC datetimes.
        for source in (old, new, a_row, b_row, snapshot):
            db.refresh(source)
        a_hash = digest({"run": values(old), "rows": [values(a_row)]})
        b_hash = digest({"run": values(new), "rows": [values(b_row)], "strategy": values(snapshot)})
        matrix = {a: {b: int(a == b == "HOLD") for b in ACTIONS} for a in ACTIONS}
        daily.append({"trade_date": day, "action_match_rate": 1.0, "universe_jaccard": 1.0,
            "p0_unexplained_count": 0, "p1_hold_noaction_flip": 0, "action_confusion": matrix,
            "chain_a_meta": {"capture_mode": "legacy_dry_run", "source_run_id": str(old.id),
                "data_cutoff_at": cutoff.isoformat(), "source_manifest_sha256": a_hash},
            "chain_b_meta": {"capture_mode": "unified_dry_run", "source_run_id": new.id,
                "data_cutoff_at": cutoff.isoformat(), "source_manifest_sha256": b_hash}})
        manifest.append({"trade_date": day, "chain_a_sha256": a_hash, "chain_b_sha256": b_hash})
    return {"portfolio_id": portfolio_id, "replay_id": f"real-{portfolio_id}-{number}", "status": "COMPLETED",
        "start_date": DATES[0], "end_date": DATES[-1], "total_days": 10, "days_replayed": 10, "skipped_days": [],
        "total_p0_unexplained": 0, "total_p1_hold_noaction_flip": 0, "g5_eligible_for_g6": eligible,
        "daily_reports": daily, "avg_action_match_rate": 1.0, "avg_universe_jaccard": 1.0,
        "failing_days_p0": [], "failing_days_p1": [], "summary_notes": [],
        "provenance": {"capture_mode": "real_dry_run_export", "trading_calendar": "trade_calendar",
            "exported_at": "2026-08-15T12:00:00+08:00", "strategy_snapshot_id": snapshot.id,
            "expected_trade_dates": DATES,
            "source_manifest_sha256": digest({"portfolio_id": portfolio_id, "strategy_snapshot_id": snapshot.id, "days": manifest})}}


def passing_g5(db, portfolio_id, *, eligible=True, cutoff_offset_days=0):
    """Explicit source-contract and reviewed empty-inventory test fixture.

    Its positive assertions cover validation of DB evidence. They are not
    evidence that the unavailable production runner has been implemented.
    """
    from app.models.app_setting import AppSetting
    from app.models.daily_bar import DailyBar
    from app.models.score import Score
    from app.models.market_data import MarketCalendar
    from app.models.g5_admission_governance import G5AdmissionControl, G5AdmissionEvent
    from app.services.g5_admission_governance import get_g5_admission_state, set_g5_admission_freeze
    from app.services.g5_legacy_inventory import build_g5_legacy_inventory
    from app.services.g5_dual_run_replay import DailyChainResult, SecurityDecision, compare_one_day, get_g5_acceptance_policy
    from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
    from app.services.data_governance_audit import write_audit_event
    G5AdmissionControl.__table__.create(db.connection(), checkfirst=True)
    G5AdmissionEvent.__table__.create(db.connection(), checkfirst=True)
    control = get_g5_admission_state(db)
    inventory = build_g5_legacy_inventory(db)
    if control["frozen"] and not inventory["reports"]:
        set_g5_admission_freeze(db, frozen=False, expected_version=control["version"],
            inventory_snapshot_id=inventory["snapshot_id"], review_note="隔离测试明确审核空报告库存，仅开放新的测试准入",
            request_key="isolated-g5-reviewed-empty-inventory", operator_id="isolated-test-reviewer",
            acknowledge_only_new_admission=True, impact_reviews=[])
    summary = _captured_rows(db, portfolio_id, eligible=eligible, cutoff_offset_days=cutoff_offset_days)
    year_start = date(2026, 1, 1)
    weekdays = [year_start + timedelta(days=n) for n in range(365) if (year_start + timedelta(days=n)).weekday() < 5]
    closures = set(weekdays[:21])
    existing = {row.trade_date for row in db.scalars(select(MarketCalendar))}
    for n in range(365):
        td = year_start + timedelta(days=n)
        if td not in existing:
            db.add(MarketCalendar(trade_date=td, is_trading_day=int(td.weekday() < 5 and td not in closures)))
    db.flush()
    calendar_setting = db.get(AppSetting, "market.cn_trade_calendar")
    if calendar_setting is None:
        calendar_setting = AppSetting(key="market.cn_trade_calendar"); db.add(calendar_setting)
    calendar_setting.updated_by = "exchange-calendar-sync"
    calendar_setting.value_json = json.dumps({"source": "https://www.sse.com.cn/disclosure/dealinstruc/closed/", "year": 2026,
        "refreshed_at": "2026-08-01T00:00:00+00:00", "trading_days": sum(db.scalars(select(MarketCalendar.is_trading_day)))})
    candidate = db.get(StrategyExecutionSnapshot, summary["provenance"]["strategy_snapshot_id"])
    baseline_number = candidate.snapshot_no
    candidate.snapshot_no += 1
    candidate.created_at = datetime(2026, 8, 2)
    config = {"quality": {"weight": 1}, "timing": {"weight": 1}}
    config_hash = digest(config)
    candidate.versions_json = json.dumps({"runner_ref": "unified_decision", "engine_build_id": "isolated-capture-new-build",
        "auto_trade_source_mode": "members_only", "scoring_config_sha256": config_hash})
    db.flush()
    baseline = StrategyExecutionSnapshot(id=candidate.id + "-baseline", portfolio_id=portfolio_id, snapshot_no=baseline_number,
        decision_clock_json="{}", member_snapshot_json=candidate.member_snapshot_json, snapshot_hash="d" * 64,
        snapshot_type="save_and_apply", effective_from=datetime(2026, 8, 1), created_at=datetime(2026, 8, 1),
        versions_json=json.dumps({"runner_ref": "legacy_scan", "engine_build_id": "isolated-capture-old-build",
            "auto_trade_source_mode": "portfolio", "scoring_config_sha256": config_hash}))
    db.add(baseline); db.flush()
    write_audit_event(db, "FACTOR_USAGE_APPLIED", portfolio_id=portfolio_id, occurred_at=datetime(2026, 8, 1),
        after={"accepted_binding": {"snapshot_id": baseline.id}}, attributes={"confirmation_type": "MODEL_BINDING_CONFIRMED"})
    db.refresh(baseline); db.refresh(candidate)
    scope = {"comparison_kind": "engine_source", "baseline_snapshot_id": baseline.id, "candidate_snapshot_id": candidate.id,
        "baseline_snapshot_sha256": digest(values(baseline)), "candidate_snapshot_sha256": digest(values(candidate)),
        "baseline_runner_ref": "legacy_scan", "candidate_runner_ref": "unified_decision",
        "baseline_engine_build_id": "isolated-capture-old-build", "candidate_engine_build_id": "isolated-capture-new-build",
        "from_source_mode": "portfolio", "to_source_mode": "members_only", "scoring_config_sha256": config_hash,
        **{key: getattr(candidate, key) for key in ("factor_model_run_id", "factor_set_id", "rule_id", "rule_version", "cost_config_json")}}
    manifest = []
    for daily in summary["daily_reports"]:
        td = date.fromisoformat(daily["trade_date"])
        available = datetime.combine(td, datetime.min.time()) + timedelta(hours=6)
        cutoff = datetime.fromisoformat(daily["chain_a_meta"]["data_cutoff_at"])
        old = db.get(ScanRun, int(daily["chain_a_meta"]["source_run_id"]))
        new = db.get(DecisionRun, daily["chain_b_meta"]["source_run_id"])
        a_row = db.scalar(select(ScanResult).where(ScanResult.scan_run_id == old.id))
        b_row = db.scalar(select(DecisionEvidence).where(DecisionEvidence.decision_run_id == new.id))
        bar = db.scalar(select(DailyBar).where(DailyBar.symbol_id == a_row.symbol_id, DailyBar.trade_date == td))
        if bar is None:
            bar = DailyBar(symbol_id=a_row.symbol_id, trade_date=td, open=10, high=11, low=9, close=10,
                           source="isolated-test", created_at=available); db.add(bar)
        score = Score(symbol_id=a_row.symbol_id, trade_date=td, quality_score=80, quality_grade="A", timing_score=80,
            stage="mature", action="HOLD", priority_score=80, calc_batch_id=candidate.id,
            scoring_config_snapshot_json=json.dumps(config), created_at=available, published_at=available,
            first_published_at=available, factor_data_cutoff_at=available, pit_safety="PIT_VERIFIED")
        db.add(score); db.flush(); db.refresh(bar); db.refresh(score)
        inputs = [{"source": "daily_bars", "record_id": bar.id, "sha256": digest(values(bar))},
                  {"source": "scores", "record_id": score.id, "sha256": digest(values(score))}]
        input_hash = digest({"rows": inputs})
        old.filters_snapshot = json.dumps({"trade_date": td.isoformat(), "data_cutoff_at": cutoff.isoformat(),
            "execution_contract": {"snapshot_id": baseline.id, "runner_ref": "legacy_scan", "engine_build_id": scope["baseline_engine_build_id"]},
            "input_manifest_sha256": input_hash})
        new.versions_json = json.dumps({"execution_contract": {"snapshot_id": candidate.id, "runner_ref": "unified_decision",
            "engine_build_id": scope["candidate_engine_build_id"]}, "input_manifest_sha256": input_hash})
        for row in (old, new, a_row, b_row): row.created_at = new.decision_at
        b_row.score_id, b_row.score_published_at, b_row.pit_safe_flag = score.id, score.published_at, "PIT_SAFE"
        db.flush()
        for row in (old, new, a_row, b_row): db.refresh(row)
        a_hash = digest({"run": values(old), "rows": [values(a_row)]})
        b_hash = digest({"run": values(new), "rows": [values(b_row)], "strategy": values(candidate)})
        actual = compare_one_day(DailyChainResult(chain="A", trade_date=td, universe_symbol_ids=[a_row.symbol_id],
            decisions={a_row.symbol_id: SecurityDecision(symbol_id=a_row.symbol_id, action="HOLD")}, coverage_pct=100),
            DailyChainResult(chain="B", trade_date=td, universe_symbol_ids=[b_row.symbol_id],
            decisions={b_row.symbol_id: SecurityDecision(symbol_id=b_row.symbol_id, action="HOLD")}, coverage_pct=100), portfolio_id=portfolio_id).as_dict()
        daily.update(actual, input_manifest=inputs)
        daily["chain_a_meta"] = {"capture_mode": "legacy_dry_run", "source_run_id": str(old.id),
            "data_cutoff_at": cutoff.isoformat(), "source_manifest_sha256": a_hash, "input_manifest_sha256": input_hash}
        daily["chain_b_meta"] = {"capture_mode": "unified_dry_run", "source_run_id": new.id,
            "data_cutoff_at": cutoff.isoformat(), "source_manifest_sha256": b_hash, "input_manifest_sha256": input_hash}
        manifest.append({"trade_date": td.isoformat(), "chain_a_sha256": a_hash, "chain_b_sha256": b_hash,
                         "input_manifest_sha256": input_hash})
    policy = get_g5_acceptance_policy()
    summary.update(execution_mode="synchronous_export", acceptance_policy_version=policy["version"],
                   acceptance_policy=policy, applicable_scope=scope, valid_symbol_days=10,
                   evidence_scope="decision_actions_only", g5_eligible_for_g6=False)
    summary["provenance"]["calendar_version"] = resolve_inspection_calendar(db)["version"]
    summary["provenance"]["source_manifest_sha256"] = digest({"portfolio_id": portfolio_id,
        "strategy_snapshot_id": candidate.id, "applicable_scope": scope, "days": manifest})
    return summary
