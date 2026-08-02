from __future__ import annotations

from datetime import date

import pytest

from app.models.capital_flow import CapitalFlow, NorthboundFlow
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.services import external_data_sync_task as service


class _DeferredThread:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True


def test_overview_reads_real_external_tables(db_session):
    symbol = Symbol(symbol="600000", name="浦发银行", asset_type="stock", market="cn")
    db_session.add(symbol)
    db_session.flush()
    db_session.add_all([
        StockValuation(symbol_id=symbol.id, trade_date=date(2026, 8, 1), source="test"),
        CapitalFlow(symbol_id=symbol.id, trade_date=date(2026, 8, 2), source="test"),
        NorthboundFlow(trade_date=date(2026, 8, 2), source="test"),
    ])
    db_session.commit()

    overview = service.get_external_data_overview(db_session)
    by_dataset = {item["dataset"]: item for item in overview["datasets"]}

    assert len(overview["datasets"]) == 7
    assert by_dataset["fundamental"]["records"] == 1
    assert by_dataset["fundamental"]["symbols"] == 1
    assert by_dataset["fundamental"]["latest_date"] == date(2026, 8, 1)
    assert by_dataset["capital_flow"]["records"] == 2
    assert by_dataset["capital_flow"]["symbols"] == 1
    assert by_dataset["capital_flow"]["latest_date"] == date(2026, 8, 2)
    assert overview["total_records"] == 3
    assert overview["available_datasets"] == 2


def test_start_creates_persisted_observable_task(db_session, monkeypatch):
    created_threads: list[_DeferredThread] = []

    def make_thread(**kwargs):
        thread = _DeferredThread(**kwargs)
        created_threads.append(thread)
        return thread

    monkeypatch.setattr(service.threading, "Thread", make_thread)

    task = service.start_external_data_sync(
        "fundamental",
        {"source": "watchlist", "include_northbound": True},
    )

    assert task.task_type == "external_sync_fundamental"
    assert task.status == "queued"
    assert task.percent == 0
    assert created_threads and created_threads[0].started is True
    persisted = service.get_external_sync_task(task.id)
    assert persisted is not None
    assert persisted.id == task.id


def test_prevents_duplicate_external_sync_tasks(db_session, monkeypatch):
    monkeypatch.setattr(service.threading, "Thread", lambda **kwargs: _DeferredThread(**kwargs))
    service.start_external_data_sync("hot_rank", {"source": "watchlist"})

    with pytest.raises(RuntimeError, match="already running"):
        service.start_external_data_sync("financial", {"source": "watchlist"})
