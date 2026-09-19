"""Stage-2.6 data quality and source-governance contract tests.

These tests intentionally exercise the small public contract used by data
sync/backtest callers.  They do not require a provider network request.
"""
from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select


def test_quality_levels_use_theoretical_denominator_and_critical_rules():
    from app.services.data_quality import assess_data_integrity

    light = assess_data_integrity(
        "daily_bars", expected_rows=1000, missing_rows=5, max_consecutive_gap_days=2
    )
    medium = assess_data_integrity(
        "daily_bars", expected_rows=1000, missing_rows=6, max_consecutive_gap_days=3
    )
    heavy = assess_data_integrity(
        "daily_bars", expected_rows=1000, missing_rows=21, max_consecutive_gap_days=1
    )
    critical = assess_data_integrity(
        "financial", expected_rows=100, missing_rows=0,
        critical_issues=["DISCLOSURE_TIME_MISSING"],
    )

    assert light.quality_level == "LIGHT"
    assert medium.quality_level == "MEDIUM"
    assert heavy.quality_level == "HEAVY"
    assert critical.quality_level == "HEAVY"
    assert critical.quarantine_required is True
    assert critical.missing_ratio == 0.0


def test_quality_gate_is_fail_closed_for_production_and_explicit_for_research():
    from app.services.data_quality import assess_data_integrity, evaluate_quality_gate

    medium = assess_data_integrity("bars", expected_rows=1000, missing_rows=10)
    heavy = assess_data_integrity("bars", expected_rows=1000, missing_rows=30)

    assert evaluate_quality_gate(medium, mode="production_pit", operation="simulation").allowed is False
    confirmed = evaluate_quality_gate(
        medium, mode="production_pit", operation="backtest", manual_confirmed=True
    )
    assert confirmed.allowed is True
    assert confirmed.restricted_pit is True
    assert evaluate_quality_gate(heavy, mode="research", operation="backtest").allowed is False
    isolated = evaluate_quality_gate(
        heavy, mode="research", operation="exploration", isolated=True
    )
    assert isolated.allowed is True
    assert isolated.non_pit is True


def test_quarantine_partition_preserves_payload_and_writes_audit(db_session):
    from app.models.data_governance_quarantine import DataQualityQuarantine
    from app.models.data_sync_plan import DataSyncPartition, DataSyncPlan
    from app.models.audit import DataGovernanceAuditEvent
    from app.services.data_quality import quarantine_partition

    plan = DataSyncPlan(
        id="plan-q-1", task_id="task-q-1", dataset="daily_bars", mode="incremental",
        source="watchlist", status="running", requested_start_date=date(2026, 1, 1),
        requested_end_date=date(2026, 1, 2), partition_strategy="symbol",
        total_partitions=1,
    )
    partition = DataSyncPartition(
        id="part-q-1", plan_id=plan.id, partition_key="symbol:1:2026-01-01:2026-01-02",
        symbol_id=None, symbol="000001", start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 2), status="running",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(partition)
    db_session.commit()

    payload = {"rows": [{"trade_date": "2026-01-01", "close": -1}], "source": "akshare"}
    row = quarantine_partition(
        db_session, partition, dataset="daily_bars", reason_code="OHLC_INVALID",
        reason="close must be positive", raw_payload=payload, source_name="akshare",
        correlation_id="corr-q-1",
    )
    db_session.commit()

    assert partition.status == "quarantined"
    assert row.status == "quarantined"
    assert row.raw_payload_hash
    assert json.loads(row.raw_payload_json) == payload
    event = db_session.execute(
        select(DataGovernanceAuditEvent).where(
            DataGovernanceAuditEvent.action == "DATA_QUALITY_QUARANTINE"
        )
    ).scalar_one()
    attrs = json.loads(event.attributes_json)
    assert attrs["reason_code"] == "OHLC_INVALID"
    assert event.correlation_id == "corr-q-1"


def test_source_failover_audit_contains_attempted_and_selected_sources(db_session):
    from app.models.audit import DataGovernanceAuditEvent
    from app.services.data_governance_audit import audit_source_failover

    audit_source_failover(
        db_session,
        interface_key="akshare.daily_bars",
        symbol="000001",
        attempted_sources=["akshare", "baostock"],
        selected_source="baostock",
        reason="primary_unavailable",
        correlation_id="corr-src-1",
    )
    db_session.commit()

    event = db_session.execute(
        select(DataGovernanceAuditEvent).where(
            DataGovernanceAuditEvent.action == "DATA_SOURCE_FAILOVER"
        )
    ).scalar_one()
    attrs = json.loads(event.attributes_json)
    assert attrs["selected_source"] == "baostock"
    assert attrs["attempted_sources"] == ["akshare", "baostock"]
    assert event.correlation_id == "corr-src-1"


def test_source_chain_can_emit_failover_audit_in_fetch_transaction(db_session):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import pandas as pd

    from app.services.market_data_sources.chain import SourceChain
    from app.services.market_data_sources.sources.base import SourceUnavailable
    from app.models.audit import DataGovernanceAuditEvent

    primary = MagicMock(name="akshare")
    primary.name = "akshare"
    primary.available.return_value = True
    primary.supports.return_value = True
    primary.fetch.side_effect = SourceUnavailable("down")
    backup = MagicMock(name="baostock")
    backup.name = "baostock"
    backup.available.return_value = True
    backup.supports.return_value = True
    backup.fetch.return_value = pd.DataFrame([{"close": 10.0}])

    chain = SourceChain(
        [primary, backup],
        audit_db=db_session,
        audit_context={"interface_key": "akshare.daily_bars", "correlation_id": "corr-chain-1"},
    )
    result = chain.fetch_with_lineage(
        SimpleNamespace(symbol="000001"), date(2026, 1, 1), date(2026, 1, 2), "qfq"
    )
    db_session.commit()

    assert result.selected_source == "baostock"
    event = db_session.execute(
        select(DataGovernanceAuditEvent).where(
            DataGovernanceAuditEvent.correlation_id == "corr-chain-1"
        )
    ).scalar_one()
    assert event.action == "DATA_SOURCE_FAILOVER"
    attrs = json.loads(event.attributes_json)
    assert attrs["attempted_sources"] == ["akshare", "baostock"]


def test_quality_snapshot_exposes_level_and_assessment_id(db_session, monkeypatch):
    import app.services.data_quality as service

    class _Config:
        warehouse_path = "unused"

    class _Warehouse:
        def __init__(self, _path):
            pass

    monkeypatch.setattr(service, "get_factor_system_config", lambda _db: _Config())
    monkeypatch.setattr(service, "FactorWarehouse", _Warehouse)
    monkeypatch.setattr(service, "build_formula_catalog", lambda _warehouse: {
        "fields": [{
            "key": "close", "availability": "available", "data_mode": "continuous",
            "daily_coverage_p50": 0.999, "table_rows": 999, "nonnull_rows": 999,
        }]
    })

    result = service.capture_field_quality_snapshots(db_session, trigger="test")
    assert result["fields"] == 1
    from app.models.data_quality_snapshot import DataQualitySnapshot

    row = db_session.execute(select(DataQualitySnapshot)).scalar_one()
    assert row.quality_level == "LIGHT"
    assert row.quality_assessment_id
    latest = service.list_latest_field_quality(db_session)
    assert latest["fields"][0]["quality_level"] == "LIGHT"
    assert latest["fields"][0]["quality_assessment_id"]
