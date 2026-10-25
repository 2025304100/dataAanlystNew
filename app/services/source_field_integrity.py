"""Read source-column completeness separately from factor eligibility."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import String, case, func, select

from app.models.financial_report import StockFinancialReport
from app.models.index_price import IndexPrice
from app.models.market_data import MarketCalendar
from app.models.stock_valuation import StockValuation
from app.models.universe import UniverseDailyBar, UniverseSymbol


def get_source_field_integrity(db, *, as_of: date | None = None, now: datetime | None = None) -> dict:
    local_now = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))
    end = as_of or local_now.date()
    if end > local_now.date():
        raise ValueError("验收截止日期不能晚于上海时间今天")
    start = end - timedelta(days=29)
    # A fixed audit window must never move backwards with stale source/cache data.
    completed = end - timedelta(days=1) if end == local_now.date() and local_now.time() < time(15) else end
    calendar = dict(db.execute(select(MarketCalendar.trade_date, MarketCalendar.is_trading_day).where(
        MarketCalendar.trade_date.between(completed - timedelta(days=30), completed))).all())
    expected = None
    day = completed
    while day in calendar:
        if calendar[day] == 1:
            expected = day
            break
        day -= timedelta(days=1)
    fields = []
    sources = []
    etf_scope = (UniverseSymbol.asset_type == "etf", UniverseSymbol.region == "cn")
    etf_rows = select(UniverseDailyBar).join(UniverseSymbol).where(*etf_scope)

    def source_dates(table, statement, *, label, cache_latest=None):
        latest, latest_as_of = db.execute(statement.with_only_columns(
            func.max(table.trade_date),
            func.max(case((table.trade_date <= end, table.trade_date), else_=None)),
            maintain_column_froms=True,
        )).one()
        # MySQL 5.7 may expose MAX(CASE ... DATE ...) as a string, while
        # SQLite/ordinary MAX(DATE) return date objects through SQLAlchemy.
        latest = date.fromisoformat(latest) if isinstance(latest, str) else latest
        latest_as_of = date.fromisoformat(latest_as_of) if isinstance(latest_as_of, str) else latest_as_of
        warnings = []
        if expected is None:
            warnings.append("calendar_unverified")
        if latest_as_of is None:
            warnings.append("no_rows_as_of")
        elif expected is not None and latest_as_of < expected:
            warnings.append("source_behind_calendar")
        if table is UniverseDailyBar and cache_latest != latest:
            warnings.append("cache_date_mismatch")
        if latest is not None and latest > local_now.date():
            warnings.append("future_source_date")
        sources.append({"table": table.__tablename__, "label": label,
                        "latest_date": latest, "latest_date_as_of": latest_as_of,
                        "cache_latest_date": cache_latest, "warnings": warnings})

    cache_latest = db.scalar(select(func.max(UniverseSymbol.last_bar_date)).where(*etf_scope))
    source_dates(UniverseDailyBar, etf_rows, label="境内 ETF 行情", cache_latest=cache_latest)
    source_dates(StockValuation, select(StockValuation), label="股票估值")

    def measure(table, columns, statement, *, window=None, note=""):
        metrics = db.execute(statement.with_only_columns(
            func.count(), *[func.count(func.nullif(func.trim(getattr(table, key)), ""))
                           if isinstance(getattr(table, key).type, String)
                           else func.count(getattr(table, key)) for key in columns],
            maintain_column_froms=True,
        )).one()
        total = int(metrics[0])
        for key, nonnull in zip(columns, metrics[1:]):
            nonnull = int(nonnull)
            fields.append({"table": table.__tablename__, "field": key,
                           "total": total, "nonnull": nonnull, "missing": total - nonnull,
                           "coverage_pct": round(nonnull * 100 / total, 2) if total else None,
                           "start_date": window[0] if window else None,
                           "end_date": window[1] if window else None, "note": note})

    measure(StockFinancialReport,
            ("symbol_id", "report_period", "announcement_date", "report_type", "source", "roe_ttm",
             "net_profit", "revenue", "net_profit_yoy", "revenue_yoy"),
            select(StockFinancialReport), note="全部已落库财报版本；不以当前财报替代历史")
    measure(UniverseDailyBar,
            ("universe_symbol_id", "trade_date", "open", "high", "low", "close",
             "volume", "amount", "turnover_rate", "source"),
            etf_rows.where(UniverseDailyBar.trade_date.between(start, end)), window=(start, end),
            note="最近30个自然日的 A股ETF 源表；缺失值不能按零成交解释")
    measure(IndexPrice, ("symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "source"),
            select(IndexPrice), note="全部已落库指数；新浪源不提供成交额，缺失仍需如实披露")
    measure(StockValuation, ("symbol_id", "trade_date", "pe_ttm", "pb", "total_market_cap",
                            "circulating_market_cap", "industry", "industry_pe_percentile", "pe_history_percentile", "source"),
            select(StockValuation).where(StockValuation.trade_date.between(start, end)),
            window=(start, end), note="最近30个自然日估值；不根据其他字段猜测分位")
    return {"as_of": end, "window_start": start, "timezone": "Asia/Shanghai",
            "window_basis": "explicit_as_of" if as_of else "asia_shanghai_today",
            "expected_latest_date": expected, "sources": sources, "fields": fields,
            "semantics": "existing_rows_only_not_calendar_coverage"}
