"""P2 regressions: label provenance, custom IDs, batching and complete queries."""
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, inspect, text

from app.api.routes.market_events import router
from app.db.session import get_db
from app.models.market_event import MarketEvent
from app.schemas.market_event import MarketEventCollectRequest, MarketEventCreate, MarketEventUpdate
from app.services import market_events as svc
from app.services.sector_concepts import parse_affected_sectors, update_sector_concept_config

pytestmark = pytest.mark.whitebox
T = datetime(2026, 10, 26)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: T)


@pytest.fixture
def client(db_session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as api:
        yield api


def row(title="光伏订单回升", **overrides):
    fields = dict(title=title, importance_level=3, impact_scope="other", sentiment="positive", source="test",
                  is_manual=0, published_at=T-timedelta(days=1), expires_at=T+timedelta(days=1),
                  affected_sectors="solar_pv", created_at=T)
    fields.update(overrides)
    return MarketEvent(**fields)


def test_auto_tags_follow_title_and_summary_edits(db_session):
    item = svc.create_market_event(db_session, MarketEventCreate(title="光伏订单回升"))
    assert item.sector_tag_source == "auto" and item.affected_sectors == "solar_pv"
    svc.update_market_event(db_session, item.id, MarketEventUpdate(title="央行降息释放流动性"))
    assert "solar_pv" not in item.affected_sectors and "banking_finance" in item.affected_sectors
    assert item.title_hash == svc._title_hash(item.title)
    svc.update_market_event(db_session, item.id, MarketEventUpdate(title="普通消息", summary="黄金价格上涨"))
    assert item.affected_sectors == "precious_metals"
    assert item.sector_tag_source == "auto"


@pytest.mark.parametrize("labels", ["custom_review", "custom_review,custom_review,solar_pv", ""])
def test_explicit_manual_labels_survive_text_edits(db_session, labels):
    item = svc.create_market_event(db_session, MarketEventCreate(title="光伏订单回升", affected_sectors=labels))
    expected = ",".join(parse_affected_sectors(labels))
    svc.update_market_event(db_session, item.id, MarketEventUpdate(title="黄金价格上涨", summary="央行降息"))
    assert item.affected_sectors == expected and item.sector_tag_source == "manual"
    result = svc.get_market_event_hotspots(db_session)
    assert {r.sector_id for r in result.sectors} == set(parse_affected_sectors(labels))


@pytest.mark.parametrize("reset", [{"sector_tag_source": "auto"}, {"affected_sectors": None}])
def test_reset_manual_labels_to_current_auto_rule(db_session, reset):
    item = svc.create_market_event(db_session, MarketEventCreate(title="光伏订单回升", affected_sectors="custom_review"))
    svc.update_market_event(db_session, item.id, MarketEventUpdate(**reset))
    assert item.affected_sectors == "solar_pv" and item.sector_tag_source == "auto"


def test_unknown_legacy_origin_is_preserved_until_explicit_reset(db_session, client):
    item = row(affected_sectors="old_custom", sector_tag_source=None)
    db_session.add(item)
    db_session.commit()
    assert client.get(f"/market-events/{item.id}").json()["sector_tag_source"] == "legacy"
    svc.update_market_event(db_session, item.id, MarketEventUpdate(title="黄金价格上涨"))
    assert item.affected_sectors == "old_custom"
    svc.update_market_event(db_session, item.id, MarketEventUpdate(sector_tag_source="auto"))
    assert item.affected_sectors == "precious_metals"


def test_legacy_null_labels_use_db_rules_consistently_in_list_detail_and_hotspots(db_session, client):
    update_sector_concept_config(db_session, "custom_review", ["冷门概念甲"])
    item = row("冷门概念甲成为市场焦点", affected_sectors=None)
    db_session.add(item)
    db_session.commit()
    assert client.get(f"/market-events/{item.id}").json()["affected_sectors"] == "custom_review"
    assert client.get("/market-events").json()["events"][0]["affected_sectors"] == "custom_review"
    assert svc.get_market_event_hotspots(db_session).sectors[0].sector_id == "custom_review"
    updated = client.put(f"/market-events/{item.id}", json={"sentiment": "negative"})
    assert updated.status_code == 200 and updated.json()["affected_sectors"] == "custom_review"
    db_session.refresh(item)
    assert item.affected_sectors is None  # GET does not silently backfill historical labels.


def test_stored_custom_and_retired_ids_are_authoritative_and_deduplicated(db_session):
    update_sector_concept_config(db_session, "custom_review", ["冷门概念甲"])
    item = row("完全不含关键词的消息", affected_sectors=" custom_review,custom_review,retired_id ")
    db_session.add(item)
    db_session.commit()
    update_sector_concept_config(db_session, "custom_review", [])
    sectors = svc.get_market_event_hotspots(db_session).sectors
    assert {s.sector_id: s.total for s in sectors} == {"custom_review": 1, "retired_id": 1}


def test_empty_stored_labels_do_not_reactivate_auto_matching(db_session):
    db_session.add_all([row("光伏明确无标签", affected_sectors=""), row("光伏未打标历史", affected_sectors=None)])
    db_session.commit()
    sectors = svc.get_market_event_hotspots(db_session).sectors
    assert len(sectors) == 1 and sectors[0].total == 1


@pytest.mark.parametrize("size", [20, 2000])
def test_hotspot_and_list_keyword_config_reads_are_constant_per_batch(db_session, size):
    db_session.add_all(row(f"光伏历史消息{i}", affected_sectors=None) for i in range(size))
    db_session.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM sector_concept_configs" in statement:
            statements.append(statement)
    event.listen(db_session.get_bind(), "before_cursor_execute", capture)
    try:
        assert svc.get_market_event_hotspots(db_session).sectors[0].total == size
        assert len(statements) == 1
        statements.clear()
        assert svc.list_market_events(db_session, limit=200).total == size
        assert len(statements) == 1
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", capture)


@pytest.mark.parametrize("size", [20, 2000])
def test_collection_builds_one_keyword_index_for_the_entire_batch(db_session, monkeypatch, size):
    records = [{"title": f"光伏批次消息{i}", "source": "eastmoney-global", "published_at": T} for i in range(size)]
    monkeypatch.setattr(svc, "_fetch_global_news", lambda: records)
    original = svc.build_index
    builds = []
    def build(concepts):
        builds.append(1)
        return original(concepts)
    monkeypatch.setattr(svc, "build_index", build)
    result = svc.collect_market_events(db_session, MarketEventCollectRequest(sources=["eastmoney-global"]))
    assert result.collected == size
    assert len(builds) == 1


def test_next_request_observes_updated_keywords_without_global_cache(db_session):
    item = row("冷门概念甲", affected_sectors=None)
    db_session.add(item)
    db_session.commit()
    assert svc.get_market_event_hotspots(db_session).sectors == []
    update_sector_concept_config(db_session, "custom_review", ["冷门概念甲"])
    assert svc.get_market_event_hotspots(db_session).sectors[0].total == 1


def test_active_filter_precedes_pagination_and_total_is_not_page_size(db_session, client):
    expired = [row(f"过期高优先级{i}", importance_level=5, expires_at=T-timedelta(seconds=1)) for i in range(200)]
    active = row("第201条有效消息", importance_level=1)
    db_session.add_all([*expired, active])
    db_session.commit()
    response = client.get("/market-events", params={"include_expired": "false", "sort_by": "importance_level", "limit": 50}).json()
    assert response["total"] == 1 and response["expired_total"] == 200
    assert [e["id"] for e in response["events"]] == [active.id]
    assert svc.list_market_events(db_session).total == 201  # Existing API callers can still inspect expired history.


def test_favorites_beyond_first_page_use_the_same_server_filters(db_session, client):
    items = [row(f"市场消息{i}") for i in range(205)]
    items[203].sentiment = "negative"
    items[204].expires_at = T-timedelta(seconds=1)
    db_session.add_all(items)
    db_session.commit()
    selected = [items[0].id, items[202].id, items[203].id, items[204].id, 999999]
    params = [("event_ids", value) for value in selected] + [("include_expired", "false"), ("sentiment", "positive")]
    response = client.get("/market-events", params=params).json()
    assert response["total"] == 2 and response["expired_total"] == 1
    assert {e["id"] for e in response["events"]} == {items[0].id, items[202].id}
    assert svc.list_market_events(db_session, event_ids=[]).total == 0
    assert client.get("/market-events", params=[("event_ids", i) for i in range(201)]).status_code == 422


@pytest.mark.parametrize("sort_by", ["published_at", "importance_level"])
def test_stable_id_tiebreaker_across_all_pages(db_session, sort_by):
    db_session.add_all(row(f"同时间消息{i}") for i in range(205))
    db_session.commit()
    pages = [svc.list_market_events(db_session, limit=50, offset=start, sort_by=sort_by) for start in range(0, 205, 50)]
    ids = [item.id for page in pages for item in page.events]
    assert len(ids) == len(set(ids)) == 205 and ids == sorted(ids, reverse=True)
    assert all(page.total == 205 for page in pages)


def test_snapshot_ceiling_and_expiration_clock_are_reused_across_pages(db_session, monkeypatch):
    db_session.add_all(row(f"快照消息{i}", expires_at=T+timedelta(seconds=10)) for i in range(60))
    db_session.commit()
    first = svc.list_market_events(db_session, limit=50, include_expired=False)
    db_session.add(row("读取期间新插入的消息"))
    db_session.commit()
    monkeypatch.setattr(svc, "_now", lambda: T+timedelta(seconds=20))
    second = svc.list_market_events(db_session, limit=50, offset=50, include_expired=False,
                                    as_of=first.as_of, snapshot_max_id=first.snapshot_max_id)
    assert second.total == 60 and len(second.events) == 10
    assert not ({e.id for e in first.events} & {e.id for e in second.events})
    assert svc.list_market_events(db_session, include_expired=False).total == 1


def test_active_boundary_and_null_expiry(db_session):
    db_session.add_all([row("边界有效", expires_at=T), row("永久有效", expires_at=None), row("已过期", expires_at=T-timedelta(microseconds=1))])
    db_session.commit()
    result = svc.list_market_events(db_session, include_expired=False)
    assert result.total == 2 and result.expired_total == 1


def test_nullable_origin_migration_roundtrip_is_idempotent_and_preserves_rows(tmp_path):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/2026_10_26_0090_market_event_sector_tag_source.py"
    spec = importlib.util.spec_from_file_location("market_sector_origin_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path/'migration.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE market_events (id INTEGER PRIMARY KEY, affected_sectors TEXT)"))
        connection.execute(text("INSERT INTO market_events VALUES (1, 'custom_review'), (2, NULL)"))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        module.upgrade()
        assert "sector_tag_source" in {c["name"] for c in inspect(connection).get_columns("market_events")}
        assert connection.execute(text("SELECT sector_tag_source FROM market_events")).scalars().all() == [None, None]
        module.downgrade()
        assert connection.execute(text("SELECT affected_sectors FROM market_events ORDER BY id")).scalars().all() == ["custom_review", None]
    engine.dispose()
