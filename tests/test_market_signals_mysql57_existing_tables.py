"""Public Alembic lifecycle against pre-existing TEXT tables with retained rows."""
import json
import os
from datetime import datetime

import pytest
from sqlalchemy import inspect, text

from tests.market_news_acceptance_support import REV91, REV92, assert_identity, engine_for, evidence, isolated_url, migrate

pytestmark = [pytest.mark.whitebox, pytest.mark.skipif(not os.environ.get("MARKET_SIGNALS_LEGACY_MYSQL57_URL"),
    reason="Requires a fresh explicit legacy schema from verify_market_signals_acceptance.py")]

TABLES = ("market_signal_rules", "market_signal_rule_versions", "market_signal_evidence", "market_signal_settings",
          "market_signal_assessments", "market_signal_predictions", "market_signal_outcomes")
LONG_FIELDS = {"market_signal_evidence": ("text",), "market_signal_assessments": ("analysis_json", "input_json"),
               "market_signal_predictions": ("snapshot_json", "outcome_json"), "market_signal_outcomes": ("result_json",)}


def snapshot(engine):
    with engine.connect() as connection:
        return {name: [dict(row) for row in connection.execute(text(f"SELECT * FROM `{name}` ORDER BY id")).mappings()]
                for name in TABLES}


def contract(engine):
    with engine.connect() as connection:
        inspector = inspect(connection)
        columns = list(connection.execute(text("SELECT TABLE_NAME,COLUMN_NAME,IS_NULLABLE,COLUMN_DEFAULT,"
            "CHARACTER_SET_NAME,COLLATION_NAME,COLUMN_COMMENT,EXTRA FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'market_signal_%' ORDER BY TABLE_NAME,ORDINAL_POSITION")).mappings())
        return {"columns": [dict(row) for row in columns], "constraints": {name: {
            "primary": inspector.get_pk_constraint(name), "indexes": inspector.get_indexes(name),
            "unique": inspector.get_unique_constraints(name), "foreign_keys": inspector.get_foreign_keys(name),
        } for name in TABLES}}


def field_types(engine):
    with engine.connect() as connection:
        return {f"{table}.{column['name']}": str(column["type"]).lower()
            for table in LONG_FIELDS for column in inspect(connection).get_columns(table)
            if column["name"] in LONG_FIELDS[table]}


@pytest.fixture(scope="module")
def legacy():
    url = isolated_url("MARKET_SIGNALS_LEGACY_MYSQL57_URL")
    engine = engine_for(url)
    with engine.connect() as connection:
        identity = assert_identity(connection, url)
        assert not inspect(connection).get_table_names(), "Only a fresh isolated schema is accepted"
    migrate(url, REV92)
    migrate(url, REV91, downgrade=True)
    # Fixture preparation only: empty retained tables model the old TEXT layout.
    with engine.begin() as connection:
        for table, names in LONG_FIELDS.items():
            for name in names:
                attributes = " CHARACTER SET utf8mb4 COLLATE utf8mb4_bin" if name == "input_json" else ""
                comment = " COMMENT '存量用途说明'" if name == "input_json" else ""
                connection.exec_driver_sql(f"ALTER TABLE `{table}` MODIFY COLUMN `{name}` TEXT{attributes} NOT NULL{comment}")
    from app.models.market_signal import (MarketSignalRule, MarketSignalRuleVersion, MarketSignalEvidence,
        MarketSignalSettings, MarketSignalAssessment, MarketSignalPrediction, MarketSignalOutcomeVersion)
    at = datetime(2026, 10, 1, 8)
    body = "存量条款中文🚀"*1800
    payload = json.dumps({"依据": body, "空值": None, "特殊字符": "引号\"反斜线\\换行\n"}, ensure_ascii=False)
    with engine.begin() as db:
        db.execute(MarketSignalRule.__table__.insert().values(id=101, name="保留原关注", target_type="symbol", target_key="000001",
            enabled=False, revision=7, config_json='{"保留":true}', created_at=at, updated_at=at))
        db.execute(MarketSignalEvidence.__table__.insert().values(id=103, fingerprint="a"*64, title="存量中文与emoji🚀", text=body, source="fixture",
            source_url=None, published_at=None, time_quality="unknown", kind="news", metadata_json=payload, first_seen_at=at))
        db.execute(MarketSignalSettings.__table__.insert().values(id=1, config_json='{"enabled":false,"说明":"原设置"}', updated_at=at))
        db.execute(MarketSignalRuleVersion.__table__.insert().values(id=102, rule_id=101, revision=7, config_json='{"保留":true}', created_at=at))
        db.execute(MarketSignalAssessment.__table__.insert().values(id=104, evidence_id=103, rule_id=101, rule_revision=7, request_key="b"*64,
            status="insufficient_evidence", score=None, event_key=None, fact_key=None,
            analysis_json=payload, input_json=payload, model_name=None, total_tokens=0, created_at=at))
        db.execute(MarketSignalPrediction.__table__.insert().values(id=105, dedup_key="c"*64, event_key="d"*64, fact_key="e"*64,
            assessment_id=104, rule_id=101, rule_revision=7, target_type="symbol", target_key="000001",
            delivery_status="sent", outbox_id=None, snapshot_json=payload, outcome_status="unverifiable",
            outcome_json=payload, sent_at=at, due_at=at, created_at=at))
        db.execute(MarketSignalOutcomeVersion.__table__.insert().values(id=106, prediction_id=105, version=1, status="unverifiable", result_json=payload, created_at=at))
        db.execute(text("INSERT INTO app_settings (`key`,value_json,updated_by,updated_at) "
            "VALUES ('signals.compat.sentinel','\"旧业务设置不变\"','fixture',:at)"), {"at":at})
    before, schema_before = snapshot(engine), contract(engine)
    assert all(value.startswith("text") for value in field_types(engine).values())
    yield {"url":url, "engine":engine, "before":before, "contract":schema_before, "identity":identity}
    engine.dispose()


def test_existing_text_upgrade_preserves_all_rows_and_constraints(legacy):
    migrate(legacy["url"], REV92)
    types = field_types(legacy["engine"])
    assert len(types) == 6 and all(value.startswith("longtext") for value in types.values()), types
    assert snapshot(legacy["engine"]) == legacy["before"]
    assert contract(legacy["engine"]) == legacy["contract"]
    evidence("signals-existing-text-upgrade.json", {"identity":legacy["identity"], "types":types,
        "all_seven_original_rows_equal":True, "constraints_nullability_collation_comments_equal":True})


def test_large_payloads_survive_replay_downgrade_and_reupgrade(legacy):
    url, engine = legacy["url"], legacy["engine"]
    migrate(url, REV92)
    large = "扩容正文🚀"*6000
    payload = json.dumps({"扩容后证据":large,"空值":None}, ensure_ascii=False)
    assert len(large.encode("utf-8")) > 65535
    replacements = {
        "market_signal_evidence": {"text":large, "fingerprint":"f"*64},
        "market_signal_assessments": {"analysis_json":payload,"input_json":payload,"evidence_id":1103,"request_key":"g"*64},
        "market_signal_predictions": {"snapshot_json":payload,"outcome_json":payload,"assessment_id":1104,"dedup_key":"h"*64},
        "market_signal_outcomes": {"result_json":payload,"prediction_id":1105},
    }
    with engine.begin() as connection:
        for table, changes in replacements.items():
            record = {**legacy["before"][table][0], **changes}
            record["id"] += 1000
            columns = ",".join(f"`{name}`" for name in record)
            bindings = ",".join(":"+name for name in record)
            connection.execute(text(f"INSERT INTO `{table}` ({columns}) VALUES ({bindings})"), record)
    expected = snapshot(engine)
    assert expected["market_signal_evidence"][1]["text"] == large
    for table, names in LONG_FIELDS.items():
        for name in names:
            assert len(expected[table][1][name].encode("utf-8")) > 65535
    types = field_types(engine)
    migrate(url, REV92)
    assert snapshot(engine) == expected
    migrate(url, REV91, downgrade=True)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REV91
    assert snapshot(engine) == expected
    assert field_types(engine) == types  # Rollback never shrinks large payloads.
    migrate(url, REV92)
    migrate(url, REV92)
    assert snapshot(engine) == expected
    assert contract(engine) == legacy["contract"]
    for table in TABLES:
        assert expected[table][0] == legacy["before"][table][0]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT value_json FROM app_settings WHERE `key`='signals.compat.sentinel'")) == '"旧业务设置不变"'
    evidence("signals-existing-text-roundtrip.json", {"revision":REV92, "large_payload_bytes":len(large.encode("utf-8")),
        "types":types,"row_counts":{table:len(rows) for table,rows in expected.items()},
        "old_rows_equal":True,"large_payload_all_six_fields_retained":True,
        "repeat_upgrade_equal":True,"downgrade_preserves_longtext_and_data":True,"reupgrade_equal":True,
        "unrelated_sentinel_unchanged":True})
