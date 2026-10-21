"""Fund-flow update/read contract on an actual, isolated MySQL 5.7 database.

Set CAPITAL_FLOW_MYSQL57_TEST=1 to use the configured local server, or supply
CAPITAL_FLOW_MYSQL57_TEST_URL. An unavailable/mismatched requested server fails;
these tests never fall back to SQLite or write fixtures to the application DB.
"""
from datetime import date, datetime, timedelta
import os
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import MetaData, create_engine, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.models.akshare_api_config import AkshareApiConfig
from app.models.capital_flow import CapitalFlow
from app.models.market_data import MarketCalendar
from app.models.symbol import Symbol
from app.services import capital_flow_data as flow, capital_flow_provider as provider
from app.services.factors.data_sync import mirror_factor_inputs
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox
DAY = date(2026, 10, 19)


class DateMeta(type):
    def __instancecheck__(cls, value):
        return isinstance(value, date)


class Today(date, metaclass=DateMeta):
    @classmethod
    def today(cls):
        return DAY


@pytest.fixture(scope="module")
def mysql57_engine():
    configured = os.getenv("CAPITAL_FLOW_MYSQL57_TEST_URL")
    if not configured and os.getenv("CAPITAL_FLOW_MYSQL57_TEST") != "1":
        pytest.skip("Enable CAPITAL_FLOW_MYSQL57_TEST=1 for real MySQL 5.7 regression; no SQLite fallback")
    if not configured:
        from app.core.config import build_mysql_url, load_db_config
        cfg = load_db_config()
        assert cfg.get("use_mysql"), "The requested MySQL regression has no configured server"
        configured = build_mysql_url(cfg)
    original = make_url(configured)
    assert original.drivername == "mysql+pymysql", "The real MySQL regression requires PyMySQL"
    database = "test_capital_flow_" + uuid4().hex
    assert database != original.database and re.fullmatch(r"test_capital_flow_[0-9a-f]{32}", database)
    options = {"connect_args": {"connect_timeout": 5, "read_timeout": 15, "write_timeout": 15}, "poolclass": NullPool}
    admin = create_engine(original.set(database=None), **options)
    engine, created = None, False
    try:
        with admin.begin() as connection:
            version = connection.exec_driver_sql("SELECT VERSION()").scalar_one()
            assert version.startswith("5.7."), f"Expected actual MySQL 5.7, got {version}"
            connection.exec_driver_sql(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            created = True
        engine = create_engine(original.set(database=database), **options)
        tables = MetaData()
        for model in (Symbol, CapitalFlow, MarketCalendar, AkshareApiConfig):
            table = model.__table__.to_metadata(tables)
            # Match the application's InnoDB connection policy, regardless of
            # this server's global default. Per-test rollback must be real.
            table.dialect_options["mysql"]["engine"] = "InnoDB"
        tables.create_all(engine)
        with engine.connect() as connection:
            storage = connection.exec_driver_sql("SHOW TABLE STATUS").mappings().all()
            assert len(storage) == 4 and all(row["Engine"] == "InnoDB" for row in storage)
        yield engine, version
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            # Only this fixture's fresh, UUID-named database may be removed.
            assert database != original.database and re.fullmatch(r"test_capital_flow_[0-9a-f]{32}", database)
            with admin.begin() as connection:
                connection.exec_driver_sql(f"DROP DATABASE `{database}`")
        admin.dispose()


@pytest.fixture
def mysql57_db(mysql57_engine, monkeypatch, record_property):
    engine, version = mysql57_engine
    record_property("actual_mysql_version", version)
    monkeypatch.setattr(flow, "date", Today)
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
            db.add(MarketCalendar(trade_date=DAY, is_trading_day=1))
            db.flush()
            yield db
        transaction.rollback()


def source_quotes(monkeypatch, values, *, quote_day=DAY):
    rows = [{"f12": code, "f13": 1 if code.startswith("6") else 0, "f62": value,
             "f184": 1, "f66": 20, "f72": 80, "f78": -10, "f84": -90,
             "f124": datetime.combine(quote_day, datetime.min.time(), ZoneInfo("Asia/Shanghai")).timestamp(),
             "f297": int(quote_day.strftime("%Y%m%d"))} for code, value in values.items()]
    calls = []
    class Response:
        def __init__(self, selected):
            self.selected = selected
        def raise_for_status(self):
            pass
        def json(self):
            return {"rc": 0, "data": {"total": len(self.selected), "diff": self.selected}}
    def get(_url, *, params, **_kwargs):
        secids = params["secids"].split(",")
        calls.append(secids)
        return Response([row for row in rows if f"{row['f13']}.{row['f12']}" in secids])
    monkeypatch.setattr(provider.requests, "get", get)
    return calls


@pytest.mark.parametrize("previous_source,current_source", [
    ("akshare", "verified:fixed129-v1"), ("verified:fixed129-v1", "akshare"),
])
def test_mysql57_fund_flow_mirror_repairs_old_precision_and_replaced_sources(
    mysql57_db, tmp_path, previous_source, current_source,
):
    db = mysql57_db
    symbol = Symbol(symbol="000001.SZ", name="镜像回归", market="SZ", asset_type="stock")
    db.add(symbol)
    db.flush()
    historical_day = date(2026, 5, 19)
    saved = CapitalFlow(symbol_id=symbol.id, trade_date=historical_day, source=previous_source,
                        main_net_inflow=-41_708_780, main_net_inflow_pct=-4.840000152587891,
                        super_large_net_inflow=13_295_991, large_net_inflow=-55_004_772,
                        medium_net_inflow=44_602_384, small_net_inflow=-2_893_606)
    db.add(saved)
    db.flush()
    warehouse = FactorWarehouse(tmp_path / "mysql57-flow-mirror.duckdb")
    flags = dict(include_valuations=False, include_financial_reports=False, include_fund_flows=True,
                 include_sentiment=False, include_etf_indicators=False, include_macro=False)
    mirror_factor_inputs(db, warehouse=warehouse, **flags)
    # Reproduce the old text-protocol mirror, then correct the existing SQL
    # record's source. Widened DOUBLE values must survive the actual DB read.
    warehouse.upsert_records("raw_fund_flows", [dict(symbol="000001", trade_date=historical_day,
        main_net_inflow=-41_708_800, main_net_inflow_pct=-4.84, super_large_net_inflow=13_296_000,
        large_net_inflow=-55_004_800, medium_net_inflow=44_602_400, small_net_inflow=-2_893_610,
        source=previous_source, ingested_at=datetime(2026, 8, 24), batch_id="factor-inputs-legacy")])
    saved.source = current_source
    db.flush()
    db.expire_all()
    changed = mirror_factor_inputs(db, warehouse=warehouse, full_refresh=True, **flags)
    assert changed.fund_flow_rows == 1
    with warehouse.connection(read_only=True) as connection:
        actual = connection.execute("SELECT symbol,trade_date,source,main_net_inflow,main_net_inflow_pct,"
            "super_large_net_inflow,large_net_inflow,medium_net_inflow,small_net_inflow,batch_id "
            "FROM raw_fund_flows").fetchall()
    assert actual == [("000001", historical_day, current_source, -41_708_780, -4.840000152587891,
                       13_295_991, -55_004_772, 44_602_384, -2_893_606, changed.batch_id)]
    assert mirror_factor_inputs(db, warehouse=warehouse, full_refresh=True, **flags).fund_flow_rows == 0
    with warehouse.connection(read_only=True) as connection:
        assert connection.execute("SELECT symbol,trade_date,source,main_net_inflow,main_net_inflow_pct,"
            "super_large_net_inflow,large_net_inflow,medium_net_inflow,small_net_inflow,batch_id "
            "FROM raw_fund_flows").fetchall() == actual


@pytest.mark.parametrize("saved_score", [81.21320343559643, None])
def test_mysql57_score_read_uses_corrected_history_without_writing_the_saved_score(mysql57_db, saved_score):
    db = mysql57_db
    symbol = Symbol(symbol="000001", name="历史重读", market="SZ", asset_type="stock")
    db.add(symbol)
    db.flush()
    db.add_all([CapitalFlow(symbol_id=symbol.id, trade_date=DAY-timedelta(days=i), main_net_inflow=50)
                for i in range(1, 5)])
    current = CapitalFlow(symbol_id=symbol.id, trade_date=DAY, main_net_inflow=50,
                          main_net_inflow_score=saved_score)
    db.add(current)
    db.flush()
    db.expire_all()
    before = db.scalar(select(CapitalFlow.main_net_inflow_score).where(CapitalFlow.id == current.id))
    assert flow.get_main_net_inflow_score(db, symbol, DAY) == 60.0
    assert db.scalar(select(CapitalFlow.main_net_inflow_score).where(CapitalFlow.id == current.id)) == before


def test_mysql57_updates_and_reads_score_from_the_nine_recent_prior_records(mysql57_db, monkeypatch):
    db = mysql57_db
    symbol = Symbol(symbol="600000", name="回归测试", market="SH", asset_type="stock")
    db.add(symbol)
    db.flush()
    # Gaps in dates do not remove real observations; older/future outliers must
    # not enter the ten-sample score. Nine prior values 10..90 plus current 100
    # have mean 55 and population variance 825: score = 83.50048355.
    db.add_all([CapitalFlow(symbol_id=symbol.id, trade_date=DAY-timedelta(days=2*i),
                           main_net_inflow=100-10*i, source="test") for i in range(1, 10)])
    db.add_all([CapitalFlow(symbol_id=symbol.id, trade_date=DAY-timedelta(days=20), main_net_inflow=1e6, source="test"),
                CapitalFlow(symbol_id=symbol.id, trade_date=DAY+timedelta(days=1), main_net_inflow=2e6, source="test")])
    db.flush()
    source_quotes(monkeypatch, {"600000": 100})
    assert flow.sync_market_capital_flow_snapshot(db, [symbol], DAY) == {symbol.id}
    saved = flow.get_or_sync_capital_flow(db, symbol, DAY)
    assert saved.trade_date == DAY
    assert saved.main_net_inflow == 100
    assert saved.main_net_inflow_score == pytest.approx(83.50048355, abs=1e-6)


def test_mysql57_keeps_symbol_histories_separate_and_excludes_etfs_and_foreign_stocks(mysql57_db, monkeypatch):
    db = mysql57_db
    sh = Symbol(symbol="600000", name="沪股", market="SH", asset_type="stock")
    sz = Symbol(symbol="000001", name="深股", market="SZ", asset_type="stock")
    etf = Symbol(symbol="510300", name="ETF", market="SH", asset_type="etf")
    foreign = Symbol(symbol="AAPL", name="美股", market="US", asset_type="stock")
    db.add_all([sh, sz, etf, foreign])
    db.flush()
    for i in range(1, 10):
        db.add(CapitalFlow(symbol_id=sh.id, trade_date=DAY-timedelta(days=i), main_net_inflow=100-10*i))
        db.add(CapitalFlow(symbol_id=sz.id, trade_date=DAY-timedelta(days=i), main_net_inflow=-100+10*i))
    db.flush()
    calls = source_quotes(monkeypatch, {"600000": 100, "000001": -100})
    assert flow.sync_market_capital_flow_snapshot(db, [sh, sz, etf, foreign], DAY) == {sh.id, sz.id}
    assert calls == [["1.600000", "0.000001"]]
    assert flow.get_or_sync_capital_flow(db, sh, DAY).main_net_inflow_score == pytest.approx(83.50048355, abs=1e-6)
    assert flow.get_or_sync_capital_flow(db, sz, DAY).main_net_inflow_score == pytest.approx(36.49951645, abs=1e-6)
    assert db.scalars(select(CapitalFlow).where(CapitalFlow.symbol_id.in_([etf.id, foreign.id]))).all() == []


def test_mysql57_does_not_fill_null_history_slots_with_older_observations(mysql57_db, monkeypatch):
    db = mysql57_db
    symbol = Symbol(symbol="600000", name="历史缺值", market="SH", asset_type="stock")
    db.add(symbol)
    db.flush()
    db.add_all([CapitalFlow(symbol_id=symbol.id, trade_date=DAY-timedelta(days=i),
                           main_net_inflow=10*i if i <= 2 else None) for i in range(1, 10)])
    db.add_all([CapitalFlow(symbol_id=symbol.id, trade_date=DAY-timedelta(days=i),
                           main_net_inflow=1000) for i in range(10, 16)])
    db.flush()
    source_quotes(monkeypatch, {"600000": 30})
    assert flow.sync_market_capital_flow_snapshot(db, [symbol], DAY) == {symbol.id}
    saved = flow.get_or_sync_capital_flow(db, symbol, DAY)
    assert saved.main_net_inflow == 30
    assert saved.main_net_inflow_score is None  # Only three real samples in the selected ten records.
    assert len(db.scalars(select(CapitalFlow).where(CapitalFlow.symbol_id == symbol.id)).all()) == 16


def test_mysql57_repeated_refresh_updates_one_row_and_recalculates_the_score(mysql57_db, monkeypatch):
    db = mysql57_db
    symbol = Symbol(symbol="600000", name="重复更新", market="SH", asset_type="stock")
    db.add(symbol)
    db.flush()
    db.add_all([CapitalFlow(symbol_id=symbol.id, trade_date=DAY-timedelta(days=i), main_net_inflow=40-10*i)
                for i in range(1, 5)])
    db.flush()
    calls = source_quotes(monkeypatch, {"600000": 40})
    flow.sync_market_capital_flow_snapshot(db, [symbol], DAY)
    saved = flow.get_or_sync_capital_flow(db, symbol, DAY)
    first_id, first_score = saved.id, saved.main_net_inflow_score
    assert first_score == pytest.approx(81.21320344, abs=1e-6)
    second_calls = source_quotes(monkeypatch, {"600000": 50})
    assert flow.sync_market_capital_flow_snapshot(db, [symbol, symbol], DAY) == {symbol.id}
    refreshed = flow.get_or_sync_capital_flow(db, symbol, DAY)
    assert refreshed.id == first_id and refreshed.main_net_inflow == 50
    assert refreshed.main_net_inflow_score > first_score
    assert calls == second_calls == [["1.600000"]]
    assert len(db.scalars(select(CapitalFlow).where(CapitalFlow.symbol_id == symbol.id)).all()) == 5
    assert list(db.scalars(select(CapitalFlow.main_net_inflow).where(
        CapitalFlow.symbol_id == symbol.id, CapitalFlow.trade_date < DAY).order_by(CapitalFlow.trade_date.desc()))) == [30, 20, 10, 0]


def test_mysql57_updates_101_symbols_in_bounded_batches(mysql57_db, monkeypatch):
    db = mysql57_db
    symbols = [Symbol(symbol=f"{600000+i:06d}", name="批量更新", market="SH", asset_type="stock") for i in range(101)]
    db.add_all(symbols)
    db.flush()
    calls = source_quotes(monkeypatch, {symbol.symbol: i for i, symbol in enumerate(symbols)})
    capital_reads = []
    def observe(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT") and "capital_flows" in statement:
            capital_reads.append(statement)
    connection = db.connection()
    event.listen(connection, "before_cursor_execute", observe)
    try:
        assert flow.sync_market_capital_flow_snapshot(db, symbols, DAY) == {symbol.id for symbol in symbols}
    finally:
        event.remove(connection, "before_cursor_execute", observe)
    assert [len(batch) for batch in calls] == [100, 1]
    assert len(capital_reads) == 4  # Two bounded reads per batch, including on MySQL 5.7.
    saved = db.scalars(select(CapitalFlow).order_by(CapitalFlow.symbol_id)).all()
    assert len(saved) == 101
    assert [row.main_net_inflow for row in saved] == list(range(101))
    assert all(row.trade_date == DAY and row.main_net_inflow_score is None for row in saved)


@pytest.mark.parametrize("response", ["empty", "previous_session", "missing_inflow"])
def test_mysql57_never_stamps_missing_or_stale_quotes_as_today(mysql57_db, monkeypatch, response):
    db = mysql57_db
    symbol = Symbol(symbol="600000", name="无当日数据", market="SH", asset_type="stock")
    db.add(symbol)
    db.flush()
    prior = CapitalFlow(symbol_id=symbol.id, trade_date=DAY-timedelta(days=1), main_net_inflow=123,
                        main_net_inflow_score=61, source="test", raw_json='{"retained": true}')
    db.add(prior)
    db.flush()
    source_quotes(monkeypatch, {} if response == "empty" else {"600000": None if response == "missing_inflow" else 999},
                  quote_day=DAY-timedelta(days=1) if response == "previous_session" else DAY)
    assert flow.sync_market_capital_flow_snapshot(db, [symbol], DAY) == set()
    rows = db.scalars(select(CapitalFlow)).all()
    assert len(rows) == 1 and rows[0].id == prior.id
    assert (prior.trade_date, prior.main_net_inflow, prior.main_net_inflow_score, prior.source, prior.raw_json) == (
        DAY-timedelta(days=1), 123, 61, "test", '{"retained": true}')


def test_mysql57_provider_failure_can_roll_back_an_earlier_batch(mysql57_db, monkeypatch):
    db = mysql57_db
    symbols = [Symbol(symbol=f"{600000+i:06d}", name="失败回滚", market="SH", asset_type="stock") for i in range(101)]
    db.add_all(symbols)
    db.commit()
    source_quotes(monkeypatch, {symbol.symbol: 10 for symbol in symbols})
    normal_get = provider.requests.get
    def get(url, **kwargs):
        response = normal_get(url, **kwargs)
        if kwargs["params"]["secids"] == "1.600100":
            response.json = lambda: {"rc": 1, "data": None}
        return response
    monkeypatch.setattr(provider.requests, "get", get)
    with pytest.raises(ValueError, match="当日接口返回异常"):
        flow.sync_market_capital_flow_snapshot(db, symbols, DAY)
    db.rollback()
    assert db.scalars(select(CapitalFlow)).all() == []
    assert len(db.scalars(select(Symbol)).all()) == 101


def test_mysql57_cancelled_update_does_not_contact_provider_or_write(mysql57_db, monkeypatch):
    db = mysql57_db
    symbol = Symbol(symbol="600000", name="取消更新", market="SH", asset_type="stock")
    db.add(symbol)
    db.flush()
    calls = source_quotes(monkeypatch, {"600000": 10})
    with pytest.raises(InterruptedError, match="已取消"):
        flow.sync_market_capital_flow_snapshot(db, [symbol], DAY, should_cancel=lambda: True)
    assert calls == []
    assert db.scalars(select(CapitalFlow)).all() == []


def test_mysql57_current_update_uses_the_public_guest_quote_route(mysql57_db, monkeypatch):
    db = mysql57_db
    symbol = Symbol(symbol="600000", name="公开行情更新", market="SH", asset_type="stock")
    db.add(symbol)
    db.flush()
    source_quotes(monkeypatch, {"600000": 123})
    normal_get = provider.requests.get
    urls = []
    def get(url, **kwargs):
        urls.append(url)
        if url != "https://push2.eastmoney.com/webguest/api/qt/ulist.np/get":
            raise provider.requests.ConnectionError("Remote end closed connection without response")
        assert kwargs["params"]["secids"] == "1.600000"
        return normal_get(url, **kwargs)
    monkeypatch.setattr(provider.requests, "get", get)
    assert flow.sync_market_capital_flow_snapshot(db, [symbol], DAY) == {symbol.id}
    saved = flow.get_or_sync_capital_flow(db, symbol, DAY)
    assert saved.trade_date == DAY and saved.main_net_inflow == 123
    assert urls == ["https://push2.eastmoney.com/webguest/api/qt/ulist.np/get"]


@pytest.mark.parametrize("backup", ["current", "previous_session", "disconnected", "out_of_scope"])
def test_mysql57_disconnect_retries_once_on_the_same_sources_backup_host(mysql57_db, monkeypatch, backup):
    db = mysql57_db
    symbol = Symbol(symbol="600000", name="连接重试", market="SH", asset_type="stock")
    db.add(symbol)
    db.flush()
    source_quotes(monkeypatch, {"600000": 123}, quote_day=DAY-timedelta(days=1) if backup == "previous_session" else DAY)
    normal_get = provider.requests.get
    urls, requested = [], []
    def get(url, **kwargs):
        urls.append(url)
        requested.append(kwargs["params"]["secids"])
        if url == "https://push2.eastmoney.com/webguest/api/qt/ulist.np/get" or backup == "disconnected":
            raise provider.requests.ConnectionError("Remote end closed connection")
        response = normal_get(url, **kwargs)
        if backup == "out_of_scope":
            response.selected[0]["f12"] = "600999"
        return response
    monkeypatch.setattr(provider.requests, "get", get)
    if backup in {"disconnected", "out_of_scope"}:
        expected = provider.requests.ConnectionError if backup == "disconnected" else ValueError
        with pytest.raises(expected):
            flow.sync_market_capital_flow_snapshot(db, [symbol], DAY)
    else:
        synced = flow.sync_market_capital_flow_snapshot(db, [symbol], DAY)
        assert synced == ({symbol.id} if backup == "current" else set())
    assert urls == ["https://push2.eastmoney.com/webguest/api/qt/ulist.np/get", "https://82.push2.eastmoney.com/webguest/api/qt/ulist.np/get"]
    assert requested == ["1.600000", "1.600000"]
    rows = db.scalars(select(CapitalFlow)).all()
    assert len(rows) == (1 if backup == "current" else 0)
    if rows:
        assert rows[0].trade_date == DAY and rows[0].main_net_inflow == 123
