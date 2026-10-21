"""A durable checkpoint keeps source facts, coverage and retry partitions aligned."""
from datetime import date
import json

import pytest
from sqlalchemy import func, select

from app.models.data_sync_coverage import DataSyncCoverage
from app.models.data_sync_plan import DataSyncPartition, DataSyncPlan
from app.models.etf_indicator import EtfIndicator
from app.models.financial_report import StockFinancialReport
from app.models.market_data import MarketCalendar
from app.models.symbol import Symbol
from app.services import external_data_sync_task as tasks, sync_coverage as ledger
from app.services import etf_basic_data as etf, financial_data as financial
from app.services.financial_data import FinancialWriteCount

pytestmark = pytest.mark.whitebox
TODAY = date(2026, 10, 19)
PAST = date(2026, 10, 16)


class DateMeta(type):
    def __instancecheck__(cls, value):
        return isinstance(value, date)


class Today(date, metaclass=DateMeta):
    @classmethod
    def today(cls):
        return TODAY


class DeferredThread:
    def __init__(self, **kwargs):
        pass

    def start(self):
        pass


@pytest.fixture
def checkpoint_environment(db_session, monkeypatch):
    monkeypatch.setattr(tasks, "date", Today)
    monkeypatch.setattr(ledger, "date", Today)
    monkeypatch.setattr(tasks.threading, "Thread", DeferredThread)
    db_session.add(MarketCalendar(trade_date=TODAY, is_trading_day=1))
    db_session.commit()
    return db_session


@pytest.mark.parametrize("dataset", ["etf", "financial"])
def test_cancel_after_100_of_101_retains_atomic_coverage_and_retries_only_unfinished(
    checkpoint_environment, monkeypatch, dataset,
):
    db = checkpoint_environment
    is_etf = dataset == "etf"
    symbols = [Symbol(symbol=str((510000 if is_etf else 600000) + index), name="checkpoint",
                      asset_type="etf" if is_etf else "stock", market="sh") for index in range(101)]
    db.add_all(symbols)
    db.commit()
    day = TODAY if is_etf else PAST
    created = tasks.start_external_data_sync(dataset, {
        "source": "all", "mode": "incremental", "sync_policy": "normal",
        "start_date": str(day), "end_date": str(day), "include_northbound": False,
    })
    payload = json.loads(created.payload_json)
    processed_ids = []

    def fake_sync(active_db, selected, start, end=None, *, should_cancel, on_result, **options):
        summary = {"total": 101, "success": 0, "skipped": 0, "failed": 0, "records": 0}
        for index, symbol in enumerate(selected[:100]):
            processed_ids.append(symbol.id)
            if is_etf:
                row = EtfIndicator(symbol_id=symbol.id, trade_date=day, nav=1, close=1,
                    premium_discount=0, total_shares=100, fund_size=100,
                    source="eastmoney:etf_snapshot", raw_json=json.dumps({"quote": {
                        "trade_date": str(day), "updated_at": f"{day}T15:01:00+08:00"}}))
                active_db.add(row)
                active_db.flush()
                callback_result = row
                summary["success"] += 1
                summary["records"] += 1
            elif index % 2 == 0:
                active_db.add(StockFinancialReport(symbol_id=symbol.id, report_period=date(2026, 6, 30),
                    announcement_date=day, report_type="periodic", source="sample", roe_ttm=5))
                active_db.flush()
                callback_result = FinancialWriteCount(1, (day,))
                summary["success"] += 1
                summary["records"] += 1
            else:
                # A validated empty announcement scan is also a completed scan.
                callback_result = FinancialWriteCount(0, ())
                summary["skipped"] += 1
            on_result(symbol, callback_result, summary)

        # The 100th callback must publish facts and coverage in the same commit.
        with tasks.SessionLocal() as reader:
            assert reader.scalar(select(func.count()).select_from(DataSyncCoverage)) == 100
            source_model = EtfIndicator if is_etf else StockFinancialReport
            assert reader.scalar(select(func.count()).select_from(source_model)) == (100 if is_etf else 50)
        tasks.cancel_external_data_sync(created.id)
        raise InterruptedError("cancel after durable batch, before the 101st symbol")

    monkeypatch.setattr(etf if is_etf else financial,
                        "sync_etf_snapshots" if is_etf else "sync_financial_announcements", fake_sync)
    tasks._run_external_sync_task(created.id, dataset, payload)
    final = tasks.get_external_sync_task(created.id)
    assert final.status == "cancelled"
    db.expire_all()
    points = db.scalars(select(DataSyncCoverage).where(DataSyncCoverage.dataset == dataset)).all()
    assert len(points) == 100
    assert {point.symbol_id for point in points} == set(processed_ids)
    assert all(point.completed and point.trade_date == day and point.last_task_id == created.id for point in points)
    partitions = db.scalars(select(DataSyncPartition).where(DataSyncPartition.plan_id == payload["sync_plan_id"])).all()
    statuses = [partition.status for partition in partitions]
    assert statuses.count("cancelled") == 1
    assert statuses.count("done") == (100 if is_etf else 50)
    assert statuses.count("skipped") == (0 if is_etf else 50)
    assert sum(partition.rows_written for partition in partitions) == (100 if is_etf else 50)

    db.rollback()
    retry = tasks.retry_external_data_sync(created.id)
    retry_payload = json.loads(retry.payload_json)
    assert retry_payload["sync_policy"] == "normal"
    retry_partitions = db.scalars(select(DataSyncPartition).where(
        DataSyncPartition.plan_id == retry_payload["sync_plan_id"])).all()
    assert len(retry_partitions) == 1
    assert retry_partitions[0].symbol_id not in processed_ids
    retry_plan = db.get(DataSyncPlan, retry_payload["sync_plan_id"])
    assert retry_plan.requested_start_date == retry_plan.requested_end_date == day
