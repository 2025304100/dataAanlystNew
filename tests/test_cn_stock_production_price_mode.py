"""Legacy qfq compatibility until cutover; candidate raw facts use their own receipts."""
from datetime import date, datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services import akshare_registry, market_data, universe_sync
from app.services.factors.bar_mirror import mirror_daily_bars
from app.services.factors.store import FactorWarehouse
from app.services.market_data_sources.sources import sina_history_http
from app.services.market_data_sources.sources._normalize import attach_cn_stock_history_contract
from app.services.market_history_contract import (
    explicit_source_price_mode, mark_cn_stock_production_mode, production_history_source_tag,
    require_cn_stock_qfq,
)

pytestmark = pytest.mark.whitebox

DAY = date(2026, 10, 15)


def cn_symbol(**kwargs):
    return SimpleNamespace(symbol="000425", market="sz", asset_type="stock", **kwargs)


@pytest.mark.parametrize("mode", ["", "hfq", "unknown", None])
def test_cn_write_and_legacy_fetch_reject_before_any_io(monkeypatch, mode):
    class NoIO:
        def execute(self, *_args, **_kwargs):
            pytest.fail("rejected mode queried the production database")
    monkeypatch.setattr(market_data, "_proxy_bypass", lambda: pytest.fail("rejected mode started provider I/O"))
    with pytest.raises(ValueError, match="requires_qfq"):
        market_data.sync_symbol_daily_bars(NoIO(), cn_symbol(), DAY, DAY, mode)
    with pytest.raises(ValueError, match="requires_qfq"):
        market_data._fetch_history(cn_symbol(), DAY, DAY, mode)
    with pytest.raises(ValueError, match="requires_qfq"):
        universe_sync._fetch_universe_history(cn_symbol(), DAY, DAY, mode)


@pytest.mark.parametrize("market,asset_type", [("us", "stock"), ("us", "etf")])
def test_us_independent_price_modes_preserved(market, asset_type):
    symbol = SimpleNamespace(market=market, asset_type=asset_type)
    require_cn_stock_qfq(symbol, "")
    frame = pd.DataFrame()
    assert mark_cn_stock_production_mode(frame, symbol, "") is frame


def standard_frame():
    return attach_cn_stock_history_contract(pd.DataFrame([{
        "trade_date": pd.Timestamp(DAY), "open": 7.2, "high": 7.24, "low": 6.98,
        "close": 7.03, "volume": 111451405, "amount": 790238885, "turnover_rate": 1.2386125082,
    }]), "sina")


@pytest.mark.parametrize("block,fields", [
    ("history_response", {"requested_adjustment": "qfq", "actual_adjustment": ""}),
    ("history_response", {"requested_adjustment": "", "actual_adjustment": "qfq"}),
    ("history_response", {"requested_adjustment": "qfq", "actual_adjustment": "qfq", "returned_mode": "day"}),
    ("sina_evidence", {"adjust": ""}),
    ("production_price_mode", {"requested_adjustment": "hfq"}),
])
def test_frame_conflicts_reject_both_writers_before_sql(block, fields):
    frame = standard_frame()
    frame.attrs[block] = fields
    with pytest.raises(ValueError, match="price_mode_conflict"):
        mark_cn_stock_production_mode(frame, cn_symbol(), "qfq")
    with pytest.raises(ValueError, match="price_mode_conflict"):
        market_data._upsert_bars(None, cn_symbol(id=1), frame)
    with pytest.raises(ValueError, match="price_mode_conflict"):
        universe_sync._upsert_universe_bars(None, 1, frame)


def test_qfq_source_suffix_records_intent_without_inventing_actual_proof():
    frame = mark_cn_stock_production_mode(standard_frame(), cn_symbol(), "qfq")
    assert production_history_source_tag(frame) == "akshare:sina:cn-stock:v1:qfq"
    assert len(production_history_source_tag(frame)) <= 32
    assert frame.attrs["production_price_mode"]["actual_adjustment"] is None
    assert frame.attrs["production_price_mode"]["evidence"] == "request_only"
    assert explicit_source_price_mode("akshare:sina:cn-stock:v1") is None
    assert explicit_source_price_mode("akshare:sina:cn-stock:v1:raw") == ""


def test_cn_initialization_raw_rejects_before_creating_task(monkeypatch):
    monkeypatch.setattr(market_data, "_set_task", lambda *_a, **_kw: pytest.fail("task mutation escaped guard"))
    with pytest.raises(ValueError, match="requires_qfq"):
        market_data.start_history_initialization_task(symbol_source="cn-stock", adjust="", repair_mode="bars")


def test_sync_market_rejects_cn_raw_before_optional_business_work(monkeypatch):
    monkeypatch.setattr(market_data, "_resolve_sync_symbols", lambda **_kw: [cn_symbol()])
    monkeypatch.setattr(market_data, "get_default_portfolio", lambda *_a, **_kw: pytest.fail("portfolio write escaped preflight"))
    monkeypatch.setattr(market_data, "refresh_symbol_name", lambda *_a, **_kw: pytest.fail("name fetch escaped preflight"))
    with pytest.raises(ValueError, match="requires_qfq"):
        market_data.sync_market_data(db=None, scope="all", watchlist_id=None, symbol_ids=None, asset_types=None,
            start_date=DAY, end_date=DAY, adjust="", auto_scan=False, portfolio_id=None, portfolio_rule_id=None)


def test_history_record_display_preserves_explicit_raw():
    task = SimpleNamespace(result={"adjust": "", "status": "failed"}, id="isolated", status="failed",
        started_at=None, finished_at=None, message="isolated")
    assert market_data._history_run_record(task)["adjust"] == ""


def test_real_frozen_sina_qfq_writes_mode_tag_and_mirrors_without_relabeling(db_session, tmp_path, monkeypatch):
    folder = Path(__file__).parent / "fixtures/sina_000425_history_20261016"
    bodies = {name: folder.joinpath(name + ".response").read_bytes() for name in ("history", "shares", "qfq")}
    closed = []
    class Response:
        status_code, headers = 200, {}
        def __init__(self, payload): self.payload = payload
        def iter_content(self, chunk_size): yield self.payload
        def close(self): closed.append(True)
    def get(url, **kwargs):
        name = "shares" if "getAmountBySymbol" in url else "qfq" if url.endswith("qfq.js") else "history"
        return Response(bodies[name])
    monkeypatch.setattr(sina_history_http.requests, "get", get)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", lambda **_kw: (_ for _ in ()).throw(ValueError("isolated EM unavailable")))
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", True)
    monkeypatch.setattr(akshare_registry, "_config_cache", {key: {"enabled": True, "delay_min_ms": 0,
        "delay_max_ms": 0, "max_retries": 1} for key in ("stock_zh_a_hist", "stock_zh_a_daily")})
    symbol = Symbol(symbol="000425", name="isolated", market="sz", asset_type="stock")
    db_session.add(symbol); db_session.commit()
    assert market_data.sync_symbol_daily_bars(db_session, symbol, DAY, DAY, "qfq")["status"] == "ok"
    db_session.commit()
    bar = db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == symbol.id))
    assert bar.source == "akshare:sina:cn-stock:v1:qfq" and bar.close == 7.03
    # M05.5 retired the business overlay. Exercise the retained universe mirror
    # with the same normalized fixture values and explicit legacy qfq tag.
    universe = UniverseSymbol(symbol="000425", name="isolated", market="sz", region="cn", asset_type="stock")
    db_session.add(universe); db_session.flush()
    db_session.add(UniverseDailyBar(universe_symbol_id=universe.id,trade_date=DAY,
        **{name:getattr(bar,name) for name in ("open","high","low","close","volume","amount","turnover_rate","source")}))
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / "qfq-mode.duckdb")
    result = mirror_daily_bars(db_session, warehouse=warehouse, include_business=False, start_date=DAY, end_date=DAY)
    with warehouse.connection(read_only=True) as con:
        stored = con.execute("SELECT close,adjust,source FROM raw_daily_bars WHERE symbol='000425'").fetchone()
    assert stored == (7.03, "qfq", "akshare:sina:cn-stock:v1:qfq")
    assert result.price_mode_unknown_rows == result.price_mode_conflict_rows == 0
    assert closed == [True, True, True]


@pytest.mark.parametrize("origin", ["business", "universe"])
def test_mirror_rejects_explicit_raw_hfq_but_preserves_unknown_legacy(db_session, tmp_path, origin):
    business = Symbol(symbol="000425", name="isolated", market="sz", asset_type="stock")
    universe = UniverseSymbol(symbol="000425", name="isolated", market="sz", region="cn", asset_type="stock")
    db_session.add_all([business, universe]); db_session.flush()
    sources = ["akshare:sina:cn-stock:v1:raw", "akshare:sina:cn-stock:v1:hfq", "akshare"]
    for day, source in zip((date(2026,10,13),date(2026,10,14),DAY),sources):
        values = dict(trade_date=day,open=7,high=8,low=6,close=7,volume=100,amount=700,
                      turnover_rate=1,source=source,created_at=datetime(2026,10,16))
        db_session.add(DailyBar(symbol_id=business.id,**values) if origin=="business"
                       else UniverseDailyBar(universe_symbol_id=universe.id,**values))
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / (origin+"-modes.duckdb"))
    if origin=="business":
        with pytest.raises(ValueError,match="LEGACY_BUSINESS_MIRROR_RETIRED"):
            mirror_daily_bars(db_session,warehouse=warehouse,include_business=True,include_universe=False)
        assert warehouse.health().raw_daily_bars==0
        assert len(db_session.scalars(select(DailyBar)).all())==3
        return
    result = mirror_daily_bars(db_session,warehouse=warehouse,include_business=origin=="business",
                              include_universe=origin=="universe")
    with warehouse.connection(read_only=True) as con:
        stored = con.execute("SELECT trade_date,source FROM raw_daily_bars").fetchall()
    assert stored == [(DAY,"akshare")]
    assert result.price_mode_conflict_rows == 2 and result.price_mode_unknown_rows == 1
    assert "旧行情" in result.to_dict()["price_mode_notice_zh"]
    # Rejected rows remain untouched in the source SQL database.
    table = DailyBar if origin=="business" else UniverseDailyBar
    assert len(db_session.scalars(select(table)).all()) == 3
