"""Exact sync coverage and conservative, read-only source inspection.

No function commits or fills a range implicitly. Callers must record only the
real dates they have confirmed; task request/execution ranges remain in
DataSyncPlan. Failed requests and unconfirmed empty results record no coverage.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.data_sync_coverage import DataSyncCoverage

_CN_TIMEZONE = ZoneInfo("Asia/Shanghai")
_BATCH_SIZE = 400  # Also stays below SQLite's older bound-parameter limit.


def _dataset_key(dataset: str) -> str:
    name = str(dataset).strip()
    return "fundamental" if name == "valuation" else name


def _chunks(items: list, size: int = _BATCH_SIZE):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _days(dates: Iterable[date]) -> list[date]:
    result = set()
    for value in dates:
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ValueError("Coverage dates must be source calendar dates, not timestamps")
        result.add(value)
    return sorted(result)


def _field_names(value: str | None) -> set[str]:
    try:
        fields = json.loads(value or "[]")
    except (TypeError, ValueError):
        return set()
    return {item for item in fields if isinstance(item, str)} if isinstance(fields, list) else set()


def record_coverage(
    db: Session, dataset: str, symbol_id: int, dates: Iterable[date], *,
    fields: Iterable[str] | None = None, task_id: str | None = None, complete: bool = True,
) -> int:
    """Record confirmed observations atomically with the caller's source writes.

    Returns the number of distinct date points touched. A later partial response
    does not revoke a previously confirmed final point or erase observed fields.
    This flushes ledger rows but deliberately never commits the caller's work.
    """
    return record_coverage_points(db, dataset, {symbol_id: dates}, fields=fields,
                                  task_id=task_id, complete=complete)


def record_coverage_points(
    db: Session, dataset: str, points: dict[int, Iterable[date]], *,
    fields: Iterable[str] | None = None, task_id: str | None = None, complete: bool = True,
) -> int:
    """Batch variant: one bounded prefetch per 400 symbols, never per stock.

    The caller's source rows and ledger points remain in the same transaction.
    A flush is bounded to 1,000 ledger objects and is never a commit.
    """
    dataset = _dataset_key(dataset)
    if not dataset or len(dataset) > 32:
        raise ValueError("Invalid coverage dataset")
    for symbol_id in points:
        if isinstance(symbol_id, bool) or not isinstance(symbol_id, int) or symbol_id < 0:
            raise ValueError("Invalid coverage symbol identifier")
    requested = {symbol_id: _days(dates) for symbol_id, dates in points.items()}
    requested = {symbol_id: days for symbol_id, days in requested.items() if days}
    if not requested:
        return 0
    if isinstance(fields, str):
        raise ValueError("Coverage fields must be a collection of names")
    names = set(fields or ())
    if any(not isinstance(name, str) or not name for name in names):
        raise ValueError("Coverage fields must be nonempty names")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pending = {(row.symbol_id, row.trade_date): row for row in db.new
               if isinstance(row, DataSyncCoverage) and row.dataset == dataset}
    for batch in _chunks(list(requested)):
        first = min(requested[symbol_id][0] for symbol_id in batch)
        last = max(requested[symbol_id][-1] for symbol_id in batch)
        existing = {(row.symbol_id, row.trade_date): row for row in db.scalars(select(DataSyncCoverage).where(
            DataSyncCoverage.dataset == dataset,
            DataSyncCoverage.symbol_id.in_(batch),
            DataSyncCoverage.trade_date.between(first, last),
        ))}
        # Session factories may disable autoflush. Do not duplicate a point
        # already queued by a prior caller in the same transaction.
        existing.update(pending)
        touched = []
        for symbol_id in batch:
            for day in requested[symbol_id]:
                row = existing.get((symbol_id, day))
                if row is None:
                    row = DataSyncCoverage(dataset=dataset, symbol_id=symbol_id, trade_date=day,
                                           first_synced_at=now, completed=False)
                    db.add(row)
                row.fields_json = json.dumps(sorted(_field_names(row.fields_json) | names), ensure_ascii=False)
                row.completed = bool(row.completed or complete)
                row.last_synced_at = now
                if task_id is not None:
                    row.last_task_id = task_id
                touched.append(row)
                if len(touched) >= 1000:
                    db.flush(touched)
                    touched = []
        if touched:
            db.flush(touched)
    return sum(len(days) for days in requested.values())


def covered_dates(
    db: Session, dataset: str, symbol_ids: Iterable[int], start: date, end: date,
) -> dict[int, set[date]]:
    """Exact confirmed final points, with an empty set for each requested ID."""
    ids = list(dict.fromkeys(symbol_ids))
    dataset = _dataset_key(dataset)
    result = {symbol_id: set() for symbol_id in ids}
    if start > end:
        raise ValueError("Coverage start date must not exceed end date")
    for batch in _chunks(ids):
        for symbol_id, day in db.execute(select(DataSyncCoverage.symbol_id, DataSyncCoverage.trade_date).where(
            DataSyncCoverage.dataset == dataset,
            DataSyncCoverage.symbol_id.in_(batch),
            DataSyncCoverage.trade_date.between(start, end),
            DataSyncCoverage.completed.is_(True),
        )):
            result[symbol_id].add(day)
    return result


def get_coverage_summary(db: Session) -> list[dict]:
    """Display observed bounds and final point counts, without filling gaps.

    Bounds include partial observations; completed bounds/counts are separate.
    The first/last synced timestamps describe source observations, whereas each
    complete execution's start/end timestamps belong to DataSyncPlan.
    """
    final_date = case((DataSyncCoverage.completed.is_(True), DataSyncCoverage.trade_date))
    rows = db.execute(select(
        DataSyncCoverage.dataset,
        func.min(DataSyncCoverage.trade_date).label("earliest_date"),
        func.max(DataSyncCoverage.trade_date).label("latest_date"),
        func.min(DataSyncCoverage.first_synced_at).label("first_synced_at"),
        func.max(DataSyncCoverage.last_synced_at).label("last_synced_at"),
        func.count().label("covered_points"),
        func.sum(case((DataSyncCoverage.completed.is_(True), 1), else_=0)).label("completed_points"),
        func.min(final_date).label("earliest_completed_date"),
        func.max(final_date).label("latest_completed_date"),
    ).group_by(DataSyncCoverage.dataset).order_by(DataSyncCoverage.dataset)).mappings()
    return [dict(row) for row in rows]


def _finite(value, *, positive: bool = False) -> bool:
    try:
        return value is not None and math.isfinite(float(value)) and (not positive or float(value) > 0)
    except (TypeError, ValueError, OverflowError):
        return False


def _final_source_time(raw_json: str | None, day: date) -> bool:
    """Only an explicit provider observation timestamp can prove finality.

    A late local created_at must never turn a stale intraday quote into close.
    Naive timestamps have no trustworthy timezone and are rejected.
    """
    try:
        payload = json.loads(raw_json or "{}")
        if not isinstance(payload, dict):
            return False
        quote = payload.get("quote", payload)
        if not isinstance(quote, dict):
            return False
        value = quote.get("updated_at")
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return False
        stamp = stamp.astimezone(_CN_TIMEZONE)
        return stamp.date() == day and stamp.time() >= time(15)
    except (TypeError, ValueError, OverflowError):
        return False


def source_covered_dates(
    db: Session, dataset: str, symbol_ids: Iterable[int], start: date, end: date, *,
    require_complete: bool = True,
) -> dict[int, set[date]]:
    """Inspect actual source rows without creating or changing ledger coverage.

    Complete daily snapshots need supported source fields plus a same-day
    timezone-aware provider timestamp at/after 15:00 China time. Valuation keeps
    its daily-complete contract; dated history requires explicit final evidence.
    Historical rows without that proof remain incomplete. Financial reports and
    event/ranking scans cannot prove a whole day's scan from existing rows.
    ``require_complete=False`` still requires every supported metric but ignores
    close-time finality, so missing-only mode may fill null metrics in a partial
    row. ``existing_dates`` is the separate any-valid-metric audit query.
    """
    return _source_dates(db, dataset, symbol_ids, start, end,
                         require_complete=require_complete, require_full=True)


def existing_dates(
    db: Session, dataset: str, symbol_ids: Iterable[int], start: date, end: date,
) -> dict[int, set[date]]:
    """Actual dates having at least one valid metric; never infer absent events."""
    return _source_dates(db, dataset, symbol_ids, start, end,
                         require_complete=False, require_full=False)


def _source_dates(db, dataset, symbol_ids, start, end, *, require_complete, require_full):
    from app.models.capital_flow import CapitalFlow
    from app.models.etf_indicator import EtfIndicator
    from app.models.financial_report import StockFinancialReport
    from app.models.hot_rank_snapshot import StockHotRankSnapshot
    from app.models.lhb_institution_trade import LhbInstitutionTrade
    from app.models.stock_valuation import StockValuation

    ids = list(dict.fromkeys(symbol_ids))
    dataset = _dataset_key(dataset)
    result = {symbol_id: set() for symbol_id in ids}
    if start > end:
        raise ValueError("Coverage start date must not exceed end date")
    contracts = {
        "fundamental": (StockValuation, "trade_date", ("pe_ttm", "pb", "total_market_cap", "circulating_market_cap")),
        "capital_flow": (CapitalFlow, "trade_date", ("main_net_inflow", "main_net_inflow_pct", "super_large_net_inflow", "large_net_inflow", "medium_net_inflow", "small_net_inflow")),
        "etf": (EtfIndicator, "trade_date", ("nav", "close", "premium_discount", "fund_size", "total_shares")),
        "financial": (StockFinancialReport, "announcement_date", ("roe_ttm", "net_profit", "revenue", "net_profit_yoy", "revenue_yoy")),
        "hot_rank": (StockHotRankSnapshot, "trade_date", ("hot_rank", "hot_rank_pct")),
        "lhb": (LhbInstitutionTrade, "trade_date", ("institution_buy", "institution_sell", "institution_net")),
    }
    if dataset not in contracts:
        raise ValueError(f"Unsupported coverage source dataset: {dataset}")
    if not ids or (require_complete and dataset in {"financial", "hot_rank", "lhb"}):
        return result
    model, date_field, fields = contracts[dataset]
    day_column = getattr(model, date_field)
    bulk = not hasattr(model, "symbol_id")
    if bulk and 0 not in result:
        return result
    batches = [[0]] if bulk else _chunks(ids)
    for batch in batches:
        statement = select(model).where(day_column.between(start, end))
        if not bulk:
            statement = statement.where(model.symbol_id.in_(batch))
        for row in db.scalars(statement):
            day = getattr(row, date_field)
            positive_fields = {"nav", "close", "fund_size", "total_shares"} if dataset == "etf" else set()
            valid = [_finite(getattr(row, field), positive=field in positive_fields) for field in fields]
            if not any(valid):
                continue
            full = all(valid)
            if require_complete and dataset == "fundamental":
                # Loss-making stocks legitimately have no PE. Preserve the
                # established ordinary-sync completion contract; missing-only
                # mode still checks every supported field for null filling.
                full = _finite(row.pb) and _finite(row.total_market_cap)
            if require_full and not full:
                continue
            if require_complete:
                final = _final_source_time(row.raw_json, day)
                try:
                    evidence = json.loads(row.raw_json or "{}")
                except (TypeError, ValueError):
                    evidence = {}
                if not isinstance(evidence, dict):
                    evidence = {}
                if dataset == "fundamental":
                    final = final or evidence.get("_daily_complete") is True or (
                        row.source == "akshare:stock_value_em" and day < date.today())
                elif dataset == "capital_flow" and day < date.today():
                    # The bounded historical interface has dated final rows but
                    # no quote time. An explicit intraday timestamp must never
                    # acquire finality merely because that date is now past.
                    final = final or (evidence.get("_history_complete") is True and "updated_at" not in evidence)
                if not final:
                    continue
            result[0 if bulk else row.symbol_id].add(day)
    return result
