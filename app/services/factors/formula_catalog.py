"""Public formula catalog derived from the compiler's executable capability set."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import logging
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Mapping
from app.services.request_coalescing import coalesced_read

from app.services.factors.field_sources import physical_field_column

from app.services.factors.factor_compiler import (
    COMPILER_VERSION,
    DSL_VERSION,
    FIELD_CATALOG,
    FUNCTION_CATALOG,
)

logger = logging.getLogger(__name__)


_FIELD_LABELS_EN = {
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "volume": "Volume",
    "amount": "Amount",
    "turnover_rate": "Turnover Rate",
    "prev_close": "Previous Close",
    "pe_ttm": "P/E TTM",
    "pb": "P/B",
    "dividend_yield": "Dividend Yield",
    "total_market_cap": "Total Market Cap",
    "circulating_market_cap": "Circulating Market Cap",
    "main_net_inflow": "Main Net Inflow",
    "roe_ttm": "ROE TTM",
    "lhb_institution_net": "Institution Net",
    "hot_rank_pct": "Hot Rank Percentile",
    "etf_premium_discount": "ETF Premium / Discount",
    "etf_tracking_error": "ETF Tracking Error",
    "etf_fund_size": "ETF Fund Size",
}

_FUNCTION_META: dict[str, dict[str, Any]] = {
    "abs": {"label_zh": "绝对值", "label_en": "Absolute", "signature": "abs(value)", "snippet": "abs(close)", "params": ["value"]},
    "min": {"label_zh": "最小值", "label_en": "Minimum", "signature": "min(a, b)", "snippet": "min(close, open)", "params": ["a", "b"]},
    "max": {"label_zh": "最大值", "label_en": "Maximum", "signature": "max(a, b)", "snippet": "max(close, open)", "params": ["a", "b"]},
    "round": {"label_zh": "四舍五入", "label_en": "Round", "signature": "round(value, digits?)", "snippet": "round(close, 2)", "params": ["value", "digits"]},
    "log": {"label_zh": "自然对数", "label_en": "Log", "signature": "log(value)", "snippet": "log(max(amount, 1))", "params": ["value"]},
    "sqrt": {"label_zh": "平方根", "label_en": "Square Root", "signature": "sqrt(value)", "snippet": "sqrt(abs(close))", "params": ["value"]},
    "exp": {"label_zh": "指数", "label_en": "Exponential", "signature": "exp(value)", "snippet": "exp(close)", "params": ["value"]},
    "sma": {"label_zh": "简单移动平均", "label_en": "Simple Moving Average", "signature": "sma(series, window)", "snippet": "sma(close, 20)", "params": ["series", "window"]},
    "ema": {"label_zh": "指数移动平均", "label_en": "Exponential Moving Average", "signature": "ema(series, window)", "snippet": "ema(close, 20)", "params": ["series", "window"]},
    "stddev": {"label_zh": "滚动标准差", "label_en": "Rolling Std Dev", "signature": "stddev(series, window)", "snippet": "stddev(close, 20)", "params": ["series", "window"]},
    "sum": {"label_zh": "滚动求和", "label_en": "Rolling Sum", "signature": "sum(series, window)", "snippet": "sum(volume, 20)", "params": ["series", "window"]},
    "mean": {"label_zh": "滚动均值", "label_en": "Rolling Mean", "signature": "mean(series, window)", "snippet": "mean(close, 20)", "params": ["series", "window"]},
    "count": {"label_zh": "非空计数", "label_en": "Non-null Count", "signature": "count(series, window)", "snippet": "count(close, 20)", "params": ["series", "window"]},
    "highest": {"label_zh": "滚动最高", "label_en": "Rolling Highest", "signature": "highest(series, window)", "snippet": "highest(high, 20)", "params": ["series", "window"]},
    "lowest": {"label_zh": "滚动最低", "label_en": "Rolling Lowest", "signature": "lowest(series, window)", "snippet": "lowest(low, 20)", "params": ["series", "window"]},
    "ref": {"label_zh": "历史引用", "label_en": "Historical Reference", "signature": "ref(series, lag)", "snippet": "ref(close, 1)", "params": ["series", "lag"]},
    "pct_change": {"label_zh": "区间涨跌幅", "label_en": "Percentage Change", "signature": "pct_change(series, window)", "snippet": "pct_change(close, 20)", "params": ["series", "window"]},
    # ── 截面算子族（M2 / T03）────────────────────────────────
    # ⚠️ 本字典与 FUNCTION_CATALOG 必须**逐键对齐**：build_formula_catalog() 用
    #    `meta = _FUNCTION_META[key]` **硬索引**遍历 FUNCTION_CATALOG（L534），
    #    漏一个键就 KeyError，公式目录 API（factors.py:390/529）整页 500。
    "cs_rank": {"label_zh": "截面排名", "label_en": "Cross-section Rank", "signature": "cs_rank(series)", "snippet": "cs_rank(close)", "params": ["series"]},
    "cs_zscore": {"label_zh": "截面标准分", "label_en": "Cross-section Z-score", "signature": "cs_zscore(series)", "snippet": "cs_zscore(close)", "params": ["series"]},
    "cs_demean": {"label_zh": "截面去均值", "label_en": "Cross-section Demean", "signature": "cs_demean(series)", "snippet": "cs_demean(close)", "params": ["series"]},
    "cs_scale": {"label_zh": "截面归一", "label_en": "Cross-section Scale", "signature": "cs_scale(series)", "snippet": "cs_scale(close)", "params": ["series"]},
    "cs_quantile": {"label_zh": "截面分位组", "label_en": "Cross-section Quantile", "signature": "cs_quantile(series, groups)", "snippet": "cs_quantile(close, 5)", "params": ["series", "groups"]},
    "cs_winsorize": {"label_zh": "截面去极值", "label_en": "Cross-section Winsorize", "signature": "cs_winsorize(series, mad_multiple)", "snippet": "cs_winsorize(close, 3)", "params": ["series", "mad_multiple"]},
    # ── 时序算子族（M2 / T04）────────────────────────────────
    # `signature` 里刻意写明 n 的单位（trading days / report periods），
    # 因为这是两个 delta 算子唯一的差别，前端参数提示必须让用户看得见。
    "ts_delta_bars": {"label_zh": "N 日差分", "label_en": "N-trading-day Delta", "signature": "ts_delta_bars(series, trading_days)", "snippet": "ts_delta_bars(close, 20)", "params": ["series", "trading_days"]},
    "ts_delta_periods": {"label_zh": "N 期差分", "label_en": "N-report-period Delta", "signature": "ts_delta_periods(series, report_periods)", "snippet": "ts_delta_periods(roe_ttm, 4)", "params": ["series", "report_periods"]},
    "ts_atr": {"label_zh": "平均真实波幅", "label_en": "Average True Range (close-to-close)", "signature": "ts_atr(close, trading_days)", "snippet": "ts_atr(close, 14) / close", "params": ["close", "trading_days"]},
}

_DISABLED_FUNCTIONS = [
    {
        "key": "rank_cs",
        "label_zh": "截面排名",
        "label_en": "Cross-section Rank",
        "category": "cross_section",
        "signature": "rank_cs(value)",
        "snippet": "rank_cs(close)",
        "enabled": False,
        "disabled_reason": "表达式执行暂未开放，请使用后处理中的排名配置",
    },
    {
        "key": "winsorize",
        "label_zh": "去极值",
        "label_en": "Winsorize",
        "category": "cross_section",
        "signature": "winsorize(value, ratio)",
        "snippet": "winsorize(close, 0.01)",
        "enabled": False,
        "disabled_reason": "表达式执行暂未开放，请使用后处理中的去极值配置",
    },
    {
        "key": "neutralize",
        "label_zh": "中性化",
        "label_en": "Neutralize",
        "category": "cross_section",
        "signature": "neutralize(value, exposure)",
        "snippet": "neutralize(close, industry)",
        "enabled": False,
        "disabled_reason": "当前数据依赖尚未完整接入，暂不可用于公式",
    },
]

_OPERATORS = [
    {"key": "add", "label": "+", "snippet": " + ", "description": "加法"},
    {"key": "sub", "label": "−", "snippet": " - ", "description": "减法"},
    {"key": "mul", "label": "×", "snippet": " * ", "description": "乘法"},
    {"key": "div", "label": "÷", "snippet": " / ", "description": "除法"},
    {"key": "mod", "label": "%", "snippet": " % ", "description": "取余"},
    {"key": "pow", "label": "幂", "snippet": " ** ", "description": "乘方"},
    {"key": "gt", "label": ">", "snippet": " > ", "description": "大于"},
    {"key": "gte", "label": "≥", "snippet": " >= ", "description": "大于等于"},
    {"key": "lt", "label": "<", "snippet": " < ", "description": "小于"},
    {"key": "lte", "label": "≤", "snippet": " <= ", "description": "小于等于"},
    {"key": "eq", "label": "=", "snippet": " == ", "description": "等于"},
    {"key": "neq", "label": "≠", "snippet": " != ", "description": "不等于"},
    {"key": "and", "label": "并且", "snippet": " && ", "description": "两个条件同时成立"},
    {"key": "or", "label": "或者", "snippet": " || ", "description": "任一条件成立"},
    {"key": "not", "label": "非", "snippet": "!", "description": "条件取反"},
    {"key": "if", "label": "条件", "snippet": "if(condition, true_value, false_value)", "description": "条件分支"},
]

_TEMPLATES = [
    {"key": "earnings_yield", "name_zh": "盈利收益率", "name_en": "Earnings Yield", "formula": "if(pe_ttm > 0, 1 / pe_ttm, 0)"},
    {"key": "momentum", "name_zh": "20 日动量", "name_en": "20-day Momentum", "formula": "pct_change(close, 20)"},
    {"key": "turnover_z", "name_zh": "换手率标准分", "name_en": "Turnover Z-score", "formula": "(turnover_rate - mean(turnover_rate, 20)) / max(stddev(turnover_rate, 20), 0.000001)"},
    {"key": "ma_distance", "name_zh": "均线乖离率", "name_en": "MA Distance", "formula": "close / sma(close, 20) - 1"},
    {"key": "breakout", "name_zh": "20 日突破", "name_en": "20-day Breakout", "formula": "close >= highest(high, 20)"},
]


_FIELD_DATA_POLICIES: dict[str, dict[str, Any]] = {
    # 日线字段只要行情表存在即可使用；prev_close 由 close 的历史序列派生。
    "open": {"data_mode": "continuous"},
    "high": {"data_mode": "continuous"},
    "low": {"data_mode": "continuous"},
    "close": {"data_mode": "continuous"},
    "volume": {"data_mode": "continuous"},
    "amount": {"data_mode": "continuous"},
    "turnover_rate": {"data_mode": "continuous"},
    "prev_close": {
        "data_mode": "derived",
        "derived_from": ["close"],
        "status_reason_zh": "由 close 按标的和交易日序列派生，不依赖物理列。",
    },
    "pe_ttm": {
        "data_mode": "point_in_time",
        "limited_reason_zh": "仅允许在真实估值快照覆盖区间内评价，禁止向历史缺口填充。",
    },
    "pb": {
        "data_mode": "point_in_time",
        "limited_reason_zh": "仅允许在真实估值快照覆盖区间内评价，禁止向历史缺口填充。",
    },
    # M1/T06 新增三列。total_market_cap / circulating_market_cap 实测非空 100%（3,079,528 行），
    # 属正常 point_in_time；dividend_yield 列存在但**全为 NULL（非空 0 行）**，
    # 故意**不**写死 data_mode=blocked —— 覆盖率检测会因 nonnull_rows<=0 自动判 blocked
    # （formula_catalog._field_capability），补数后自动恢复为 limited，无需再改代码。
    "dividend_yield": {
        "data_mode": "point_in_time",
        "limited_reason_zh": (
            "股息率物理列已存在，但当前估值快照中该列全为空值（未采集）。"
            "补齐采集前能力检测会判为缺数据（blocked），补齐后自动转为覆盖受限（limited）。"
        ),
    },
    "total_market_cap": {
        "data_mode": "point_in_time",
        "limited_reason_zh": "仅允许在真实估值快照覆盖区间内评价，禁止向历史缺口填充。",
    },
    "circulating_market_cap": {
        "data_mode": "point_in_time",
        "limited_reason_zh": "仅允许在真实估值快照覆盖区间内评价，禁止向历史缺口填充。",
    },
    "main_net_inflow": {
        "data_mode": "continuous",
        "minimum_continuity_days": 60,
        "minimum_daily_coverage": 0.70,
        "limited_reason_zh": "资金流连续交易日不足 60 日前，只能观察，不能进入正式评价。",
    },
    "roe_ttm": {
        "data_mode": "point_in_time",
        "limited_reason_zh": "财报必须按 announcement_date 做 PIT 对齐，且当前优先覆盖高流动性池。",
    },
    "lhb_institution_net": {
        "data_mode": "event",
        "evaluation_mode": "event",
        "status_reason_zh": "仅龙虎榜机构事件日有值，非事件日保持缺失。",
    },
    "hot_rank_pct": {
        "data_mode": "snapshot",
        "evaluation_mode": "snapshot",
        "status_reason_zh": "当前仅有热度榜快照，不承诺历史连续性。",
    },
    "etf_premium_discount": {
        "data_mode": "snapshot", "evaluation_mode": "snapshot",
        "status_reason_zh": "同日价格相对 IOPV 的溢折价快照，正为溢价、负为折价；不作为历史净值回填。",
    },
    "etf_tracking_error": {
        "data_mode": "snapshot", "evaluation_mode": "snapshot",
        "status_reason_zh": "ETF 当日指标快照，仅用于 ETF 快照评价。",
        "empty_reason_zh": "跟踪误差尚未接入对应基准及同日收益序列，当前同步不提供该指标。",
    },
    "etf_fund_size": {
        "data_mode": "snapshot", "evaluation_mode": "snapshot",
        "status_reason_zh": "规模为同日 IOPV×基金份额估算（元），用于快照观察；不是已披露的资产净值。",
    },
}

_CATALOG_CACHE_TTL_SECONDS = 30.0
_catalog_cache_lock = Lock()
_catalog_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
_catalog_generation = 0


def invalidate_formula_catalog_cache() -> None:
    global _catalog_generation
    with _catalog_cache_lock:
        _catalog_cache.clear()
        _catalog_generation += 1


def _prepare_coverage_calendar(conn: Any) -> None:
    from app.services.market_session import read_cn_calendar

    rows = read_cn_calendar()
    conn.execute("CREATE OR REPLACE TEMP TABLE coverage_cn_closed_days (trade_date DATE PRIMARY KEY)")
    closed = [(day,) for day, is_open in rows if not is_open]
    if closed:
        conn.executemany("INSERT INTO coverage_cn_closed_days VALUES (?)", closed)
    latest_session = max((day for day, is_open in rows
                          if is_open and day <= date.today() and day.weekday() < 5), default=None)
    conn.execute("CREATE OR REPLACE TEMP TABLE coverage_cn_latest_session AS SELECT CAST(? AS DATE) trade_date",
                 [latest_session])


def _open_cn_day_sql(column: str) -> str:
    return (
        f"EXTRACT(ISODOW FROM {column}) BETWEEN 1 AND 5 "
        f"AND NOT EXISTS (SELECT 1 FROM coverage_cn_closed_days AS closure "
        f"WHERE closure.trade_date = {column})"
    )


def _warehouse_cache_key(warehouse: Any) -> tuple[str, int]:
    path = getattr(warehouse, "path", None)
    if path is None:
        path = getattr(warehouse, "db_path", None)
    path_text = str(path or "")
    try:
        if path is None:
            return path_text, 0
        main = Path(path).stat()
        wal_path = Path(str(path) + ".wal")
        try:
            wal = wal_path.stat()
            wal_stamp = (wal.st_mtime_ns, wal.st_size)
        except FileNotFoundError:
            wal_stamp = (0, 0)
        # DuckDB commits can change only the WAL before a checkpoint. Main
        # file mtime alone would let a fresh sync reuse pre-sync statistics.
        mtime = hash((main.st_mtime_ns, main.st_size, *wal_stamp))
    except (AttributeError, OSError):
        mtime = 0
    return path_text, mtime


def _cached_stats(base_key, cache_key, stamp, now):
    """A fresh full catalog also satisfies a dataset-only quality check."""
    for key in dict.fromkeys((cache_key, base_key)):
        cached = _catalog_cache.get(key)
        if cached and cached[0] + _CATALOG_CACHE_TTL_SECONDS > now and cached[1] == stamp:
            return deepcopy(cached[2])
    return None


def _trailing_trading_streak(
    calendar_dates: list[date], covered_dates: set[date]
) -> int:
    """Count the newest uninterrupted run against the actual trading calendar."""
    streak = 0
    for trade_date in calendar_dates:
        if trade_date not in covered_dates:
            break
        streak += 1
    return streak


def _coverage_summary(
    daily_counts: list[tuple[Any, int, int]], *, minimum_coverage: float
) -> dict[str, Any]:
    """Summarize descending daily (date, universe, covered) rows safely."""
    rates = [covered / universe for _, universe, covered in daily_counts if universe > 0]
    ordered = sorted(rates)

    def percentile(ratio: float) -> float | None:
        if not ordered:
            return None
        index = int(round((len(ordered) - 1) * ratio))
        return round(ordered[index], 6)

    trailing_days = 0
    for _, universe, covered in daily_counts:
        rate = covered / universe if universe else 0.0
        if covered <= 0 or universe <= 0 or rate < minimum_coverage:
            break
        trailing_days += 1
    expected_rows = sum(universe for _, universe, _ in daily_counts)
    covered_rows = sum(covered for _, _, covered in daily_counts)
    max_gap = gap = 0
    for _, universe, covered in daily_counts:
        if universe > 0 and (covered <= 0 or covered / universe < minimum_coverage):
            gap += 1
            max_gap = max(max_gap, gap)
        else:
            gap = 0
    return {
        "trailing_coverage_days": trailing_days,
        "daily_coverage_p50": percentile(0.50),
        "daily_coverage_p90": percentile(0.90),
        "latest_daily_coverage": round(rates[0], 6) if rates else None,
        "expected_rows": expected_rows,
        "covered_rows": covered_rows,
        "missing_ratio": (1.0 - covered_rows / expected_rows if expected_rows else None),
        "max_consecutive_gap_days": max_gap,
    }


def _query_cross_section_coverage(
    conn: Any,
    *,
    table: str,
    field: str,
    minimum_coverage: float,
    _calendar_ready: bool = False,
) -> dict[str, Any]:
    """Compare actual values with the matching active CN asset-day universe."""
    asset_type = "etf" if table == "raw_etf_indicators" else "stock"
    if table == "raw_etf_indicators":
        # ETF bars are intentionally outside the stock model's bar mirror.
        # Snapshot coverage uses the active ETF universe and latest CN session.
        if not _calendar_ready:
            _prepare_coverage_calendar(conn)
        day = conn.execute("SELECT trade_date FROM coverage_cn_latest_session").fetchone()[0]
        if day is None:
            # Standalone warehouses may have no SQL calendar. Only this fallback
            # reads bar dates; ordinary live snapshots use the cached calendar.
            day = conn.execute(f"""SELECT MAX(trade_date) FROM (
                SELECT DISTINCT b.trade_date FROM raw_daily_bars b
                JOIN raw_asset_universe u ON u.symbol=b.symbol
                WHERE LOWER(u.region)='cn' AND b.trade_date<=? AND {_open_cn_day_sql('b.trade_date')}
                UNION SELECT DISTINCT etf.trade_date FROM raw_etf_indicators etf
                WHERE etf.trade_date<=? AND {_open_cn_day_sql('etf.trade_date')}
            )""", [date.today(), date.today()]).fetchone()[0]
        if day is None:
            return _coverage_summary([], minimum_coverage=minimum_coverage)
        expected, covered = conn.execute(f"""SELECT COUNT(*),
            COUNT(*) FILTER (WHERE source.covered) FROM raw_asset_universe u
            LEFT JOIN (SELECT symbol, BOOL_OR({field} IS NOT NULL) covered
                FROM raw_etf_indicators WHERE trade_date=? GROUP BY symbol) source
            ON source.symbol=u.symbol
            WHERE LOWER(u.asset_type)='etf' AND LOWER(u.region)='cn' AND u.is_active=TRUE
        """, [day]).fetchone()
        return _coverage_summary([(day, int(expected), int(covered))], minimum_coverage=minimum_coverage)
    if table == "raw_financial_reports":
        source_join = f"""LEFT JOIN LATERAL (
            SELECT reports.symbol, reports.{field}
            FROM raw_financial_reports AS reports
            WHERE reports.symbol = bars.symbol
              AND reports.announcement_date <= calendar.trade_date
            ORDER BY reports.announcement_date DESC, reports.report_period DESC
            LIMIT 1
        ) AS source ON TRUE"""
    else:
        source_join = f"""LEFT JOIN {table} AS source
            ON source.symbol = bars.symbol
            AND source.trade_date = calendar.trade_date"""
    try:
        if not _calendar_ready:
            _prepare_coverage_calendar(conn)
        rows = conn.execute(
            f"""
            WITH stock_bars AS (
                SELECT DISTINCT bars.symbol, bars.trade_date
                FROM raw_daily_bars AS bars
                JOIN raw_asset_universe AS universe
                  ON universe.symbol = bars.symbol
                WHERE LOWER(universe.asset_type) = '{asset_type}'
                  AND LOWER(universe.region) = 'cn'
                  AND universe.is_active = TRUE
                  AND {_open_cn_day_sql('bars.trade_date')}
            ),
            calendar AS (
                SELECT DISTINCT trade_date
                FROM stock_bars
                ORDER BY trade_date DESC
                LIMIT 260
            ),
            observations AS (
                SELECT bars.symbol, calendar.trade_date,
                    BOOL_OR(source.{field} IS NOT NULL) AS covered
                FROM calendar
                JOIN stock_bars AS bars ON bars.trade_date = calendar.trade_date
                {source_join}
                GROUP BY bars.symbol, calendar.trade_date
            ),
            gap_islands AS (
                SELECT *, SUM(CASE WHEN covered THEN 1 ELSE 0 END) OVER (
                    PARTITION BY symbol ORDER BY trade_date
                ) AS island
                FROM observations
            ),
            gaps AS (
                SELECT symbol, island, COUNT(*) AS gap_days
                FROM gap_islands WHERE NOT covered
                GROUP BY symbol, island
            )
            SELECT
                trade_date,
                COUNT(*) AS universe_symbols,
                COUNT(*) FILTER (WHERE covered) AS covered_symbols,
                (SELECT COALESCE(MAX(gap_days), 0) FROM gaps) AS longest_symbol_gap
            FROM observations
            GROUP BY trade_date
            ORDER BY trade_date DESC
            """
        ).fetchall()
    except Exception:
        return {
            "trailing_coverage_days": 0,
            "daily_coverage_p50": None,
            "daily_coverage_p90": None,
            "latest_daily_coverage": None,
            "expected_rows": None,
            "covered_rows": None,
            "missing_ratio": None,
            "max_consecutive_gap_days": None,
        }
    daily_counts = [(row[0], int(row[1] or 0), int(row[2] or 0)) for row in rows]
    summary = _coverage_summary(daily_counts, minimum_coverage=minimum_coverage)
    if rows:
        summary["max_consecutive_gap_days"] = max(
            summary["max_consecutive_gap_days"], int(rows[0][3] or 0)
        )
    return summary


def _query_field_stats(warehouse: Any, *, field_keys: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """Read lightweight field-level coverage evidence from DuckDB.

    Nullable fields are counted in one aggregate scan per table. Physical
    column existence alone is never evidence that an input has values.
    """
    table_fields: dict[str, list[str]] = {}
    for field_name, spec in FIELD_CATALOG.items():
        if field_keys is not None and field_name not in field_keys:
            continue
        table_fields.setdefault(spec.source_table, []).append(field_name)

    stats: dict[str, dict[str, Any]] = {}
    try:
        with warehouse.connection(read_only=True) as conn:
            _prepare_coverage_calendar(conn)
            stock_coverage_prepared = None
            for table, fields in table_fields.items():
                is_daily_table = table == "raw_daily_bars"
                try:
                    columns = {
                        str(row[1]) for row in conn.execute(
                            f"PRAGMA table_info('{table}')"
                        ).fetchall()
                    }
                except Exception:
                    columns = set()
                if not columns:
                    for field in fields:
                        stats[field] = {"table_rows": 0, "nonnull_rows": 0, "column_exists": False}
                    continue

                date_col = "announcement_date" if table == "raw_financial_reports" else "trade_date"
                select_parts = ["COUNT(*) AS table_rows"]
                if date_col in columns:
                    select_parts.extend([
                        f"MIN({date_col}) AS first_date",
                        f"MAX({date_col}) AS latest_date",
                        (
                            "0 AS distinct_dates"
                            if is_daily_table
                            else f"COUNT(DISTINCT {date_col}) AS distinct_dates"
                        ),
                    ])
                else:
                    select_parts.extend([
                        "NULL AS first_date",
                        "NULL AS latest_date",
                        "0 AS distinct_dates",
                    ])
                if "symbol" in columns and not is_daily_table:
                    select_parts.append("COUNT(DISTINCT symbol) AS distinct_symbols")
                else:
                    select_parts.append("0 AS distinct_symbols")
                count_aliases: dict[str, str] = {}
                for index, field in enumerate(fields):
                    if field == "prev_close":
                        # This field is derived from close and has no physical column.
                        continue
                    column = physical_field_column(table, field)
                    if column not in columns:
                        continue
                    alias = f"nonnull_{index}"
                    count_aliases[field] = alias
                    select_parts.append(f"COUNT({column}) AS {alias}")
                try:
                    # Reports may be announced during holidays and remain
                    # valid PIT evidence. Only daily CN observations are
                    # filtered; other markets retain their own dates.
                    row_filter = ""
                    if date_col == "trade_date" and "symbol" in columns:
                        row_filter = f""" WHERE (
                            NOT EXISTS (SELECT 1 FROM raw_asset_universe AS u
                                WHERE u.symbol = observations.symbol AND LOWER(u.region) = 'cn')
                            OR ({_open_cn_day_sql('observations.trade_date')})
                        )"""
                    row = conn.execute(
                        f"SELECT {', '.join(select_parts)} FROM {table} AS observations{row_filter}"
                    ).fetchone()
                except Exception as exc:
                    logger.warning("Coverage aggregate query failed for %s: %s", table, exc)
                    row = None
                if row is None:
                    row_values: dict[str, Any] = {
                        "table_rows": 0,
                        "first_date": None,
                        "latest_date": None,
                        "distinct_dates": 0,
                        "distinct_symbols": 0,
                    }
                else:
                    row_values = {
                        "table_rows": int(row[0] or 0),
                        "first_date": str(row[1]) if row[1] is not None else None,
                        "latest_date": str(row[2]) if row[2] is not None else None,
                        "distinct_dates": int(row[3] or 0),
                        "distinct_symbols": int(row[4] or 0),
                    }
                    for offset, (field, alias) in enumerate(count_aliases.items(), start=5):
                        row_values[f"nonnull:{field}"] = int(row[offset] or 0)

                coverage_counts = {}
                measurable = list(dict.fromkeys(
                    "close" if field == "prev_close" else physical_field_column(table, field)
                    for field in fields
                    if "symbol" in columns and date_col in columns
                    and ("close" if field == "prev_close" else physical_field_column(table, field)) in columns
                ))
                if measurable and table != "raw_etf_indicators":
                    try:
                        if stock_coverage_prepared is None:
                            _prepare_stock_coverage(conn)
                            stock_coverage_prepared = True
                        if stock_coverage_prepared:
                            coverage_counts = _query_coverage_counts(conn, table=table, fields=measurable)
                    except Exception:
                        stock_coverage_prepared = False
                        logger.warning("Batched coverage unavailable; using the existing field queries", exc_info=True)
                for field in fields:
                    if field == "prev_close":
                        close_count = row_values.get("nonnull:close", row_values["table_rows"])
                        stats[field] = {
                            **row_values,
                            "nonnull_rows": close_count,
                            "column_exists": False,
                            "derived": True,
                        }
                    else:
                        nonnull_rows = row_values.get(f"nonnull:{field}", 0)
                        stats[field] = {
                            **row_values,
                            "nonnull_rows": nonnull_rows,
                            "column_exists": physical_field_column(table, field) in columns,
                            "derived": False,
                        }
                    if table == "raw_etf_indicators" and physical_field_column(table, field) in columns:
                        column = physical_field_column(table, field)
                        condition = row_filter + (" AND " if row_filter else " WHERE ") + f"{column} IS NOT NULL"
                        first, last, dates, symbols = conn.execute(f"""SELECT MIN(trade_date), MAX(trade_date),
                            COUNT(DISTINCT trade_date), COUNT(DISTINCT symbol)
                            FROM raw_etf_indicators AS observations{condition}""").fetchone()
                        stats[field].update(first_date=str(first) if first else None,
                            latest_date=str(last) if last else None, distinct_dates=int(dates), distinct_symbols=int(symbols))
                    policy = _FIELD_DATA_POLICIES.get(field, {})
                    # 日覆盖是字段能力的基础证据，不应只在配置了连续性门槛
                    # （例如资金流 60 日）时才计算。只要数据表具备标的、交易日
                    # 和实际字段列，就按最近交易日的股票横截面统计覆盖率。
                    # prev_close 没有物理列，沿用 close 的覆盖口径。
                    coverage_field = (
                        "close" if field == "prev_close" else physical_field_column(table, field)
                    )
                    can_measure_daily_coverage = (
                        "symbol" in columns
                        and date_col in columns
                        and coverage_field in columns
                    )
                    if can_measure_daily_coverage:
                        minimum = float(policy.get("minimum_daily_coverage", 0.0))
                        if coverage_field in coverage_counts:
                            daily, longest_gap = coverage_counts[coverage_field]
                            coverage = _coverage_summary(daily, minimum_coverage=minimum)
                            coverage["max_consecutive_gap_days"] = max(coverage["max_consecutive_gap_days"], longest_gap)
                        else:
                            coverage = _query_cross_section_coverage(conn, table=table, field=coverage_field,
                                minimum_coverage=minimum, _calendar_ready=True)
                        stats[field].update(coverage)
                        stats[field]["continuity_days"] = coverage[
                            "trailing_coverage_days"
                        ]
    except Exception:
        return {}
    return stats


def _prepare_stock_coverage(conn: Any) -> None:
    """Build the identical 260-session denominator once per catalog read."""
    conn.execute(f"""CREATE OR REPLACE TEMP TABLE coverage_cn_stock_bars AS
        WITH eligible AS (
            SELECT symbol FROM raw_asset_universe
            WHERE LOWER(asset_type)='stock' AND LOWER(region)='cn' AND is_active=TRUE
        ), calendar AS (
            SELECT DISTINCT bars.trade_date FROM raw_daily_bars bars
            JOIN eligible ON eligible.symbol=bars.symbol
            WHERE {_open_cn_day_sql('bars.trade_date')}
            ORDER BY bars.trade_date DESC LIMIT 260
        )
        SELECT DISTINCT bars.symbol, bars.trade_date FROM raw_daily_bars bars
        JOIN calendar ON calendar.trade_date=bars.trade_date
        JOIN eligible ON eligible.symbol=bars.symbol
    """)


def _query_coverage_counts(conn: Any, *, table: str, fields: list[str]) -> dict:
    """Join a source once and calculate all of its fields with one shared sort."""
    if table == "raw_financial_reports":
        selected = ",".join(fields)
        # Collapse only same-announcement ties before the temporal join. The
        # newest visible report (including a NULL metric) must still win.
        join = f"""ASOF LEFT JOIN (
            SELECT symbol,announcement_date,{selected} FROM raw_financial_reports
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY symbol,announcement_date ORDER BY report_period DESC
            )=1
        ) source ON bars.symbol=source.symbol AND bars.trade_date>=source.announcement_date"""
    else:
        join = f"LEFT JOIN {table} source ON source.symbol=bars.symbol AND source.trade_date=bars.trade_date"
    flags = ",".join(f"BOOL_OR(source.{field} IS NOT NULL) AS covered_{i}" for i, field in enumerate(fields))
    islands = ",".join(f"SUM(CASE WHEN covered_{i} THEN 1 ELSE 0 END) OVER streak AS island_{i}" for i in range(len(fields)))
    gaps = ",".join(f"gap_{i} AS (SELECT COUNT(*) gap_days FROM gap_islands WHERE NOT covered_{i} GROUP BY symbol,island_{i})" for i in range(len(fields)))
    counts = ",".join(f"COUNT(*) FILTER (WHERE covered_{i}), (SELECT COALESCE(MAX(gap_days),0) FROM gap_{i})" for i in range(len(fields)))
    rows = conn.execute(f"""WITH observations AS (
        SELECT bars.symbol,bars.trade_date,{flags} FROM coverage_cn_stock_bars bars
        {join} GROUP BY bars.symbol,bars.trade_date
    ), gap_islands AS (
        SELECT *,{islands} FROM observations
        WINDOW streak AS (PARTITION BY symbol ORDER BY trade_date)
    ), {gaps}
    SELECT trade_date,COUNT(*),{counts} FROM observations
    GROUP BY trade_date ORDER BY trade_date DESC""").fetchall()
    return {field: ([(row[0],int(row[1]),int(row[2+2*i])) for row in rows],
                    int(rows[0][3+2*i]) if rows else 0) for i,field in enumerate(fields)}


def _field_capability(field_name: str, stats: Mapping[str, Any]) -> dict[str, Any]:
    policy = _FIELD_DATA_POLICIES.get(field_name, {})
    table_rows = int(stats.get("table_rows", 0) or 0)
    nonnull_rows = int(stats.get("nonnull_rows", 0) or 0)
    distinct_dates = int(stats.get("distinct_dates", 0) or 0)
    continuity_days = int(stats.get("continuity_days", distinct_dates) or 0)
    data_mode = str(policy.get("data_mode", "continuous"))
    derived = bool(policy.get("derived_from"))
    status = "unknown"
    reason = "尚未读取数据仓库能力快照。"
    if data_mode == "blocked":
        status = "blocked"
        reason = str(policy.get("status_reason_zh", "该字段当前不可用于评价。"))
    elif table_rows <= 0 or (nonnull_rows <= 0 and not derived):
        status = "blocked"
        reason = str(policy.get("empty_reason_zh", "源表为空或字段没有非空记录。"))
    elif data_mode == "event":
        status = "event"
        reason = str(policy.get("status_reason_zh", "仅事件日可用。"))
    elif data_mode == "snapshot":
        status = "snapshot"
        reason = str(policy.get("status_reason_zh", "仅快照可用。"))
    elif data_mode == "point_in_time":
        status = "limited"
        reason = str(policy.get("limited_reason_zh", "仅在实际覆盖区间可用。"))
    elif policy.get("minimum_continuity_days") and continuity_days < int(policy["minimum_continuity_days"]):
        status = "limited"
        reason = str(policy.get("limited_reason_zh", "连续交易日不足。"))
    else:
        status = "available"
        reason = str(policy.get("status_reason_zh", "字段有真实数据覆盖。"))

    evaluation_enabled = status in {"available", "limited"}
    preview_enabled = status in {"available", "limited", "event", "snapshot"}
    if (
        policy.get("minimum_continuity_days")
        and continuity_days < int(policy["minimum_continuity_days"])
    ):
        evaluation_enabled = False
    if status in {"blocked", "unknown"}:
        evaluation_enabled = False
        preview_enabled = False
    if status in {"event", "snapshot"}:
        evaluation_enabled = False

    return {
        "availability": status,
        "data_mode": data_mode,
        "evaluation_mode": str(policy.get("evaluation_mode", data_mode)),
        "evaluation_enabled": evaluation_enabled,
        "preview_enabled": preview_enabled,
        "draft_enabled": True,
        "status_reason": reason,
        "table_rows": table_rows,
        "nonnull_rows": nonnull_rows,
        "distinct_symbols": int(stats.get("distinct_symbols", 0) or 0),
        "distinct_dates": distinct_dates,
        "continuity_days": continuity_days,
        "daily_coverage_p50": stats.get("daily_coverage_p50"),
        "daily_coverage_p90": stats.get("daily_coverage_p90"),
        "latest_daily_coverage": stats.get("latest_daily_coverage"),
        "expected_rows": stats.get("expected_rows"),
        "covered_rows": stats.get("covered_rows"),
        "missing_ratio": stats.get("missing_ratio"),
        "max_consecutive_gap_days": stats.get("max_consecutive_gap_days"),
        "first_date": stats.get("first_date"),
        "latest_date": stats.get("latest_date"),
        "derived": bool(stats.get("derived", False)),
        "derived_from": list(policy.get("derived_from", [])),
    }


def build_formula_catalog(warehouse: Any | None = None, *, field_keys: set[str] | None = None) -> dict[str, Any]:
    """Build the syntax catalog plus a live data-capability snapshot.

    ``enabled`` continues to mean “accepted by the compiler”, so users can
    save a draft that is waiting for a data source.  ``availability`` and
    ``evaluation_enabled`` are the runtime truth used by preview/evaluation
    gates and the UI.  This prevents a blocked external field from being
    presented as if it already had historical coverage.
    """
    field_stats: dict[str, dict[str, Any]] = {}
    if warehouse is not None:
        base_key, cache_mtime = _warehouse_cache_key(warehouse)
        cache_key = base_key
        if field_keys is not None:
            cache_key += "#fields=" + ",".join(sorted(field_keys))
        now = monotonic()
        with _catalog_cache_lock:
            cached = _cached_stats(base_key, cache_key, cache_mtime, now)
            generation = _catalog_generation
        if cached is not None:
            field_stats = cached
        else:
            def read_stats():
                # Another caller may have completed a compatible full read
                # between the cache miss and becoming the request leader.
                with _catalog_cache_lock:
                    ready = _cached_stats(base_key, cache_key, cache_mtime, monotonic())
                if ready is not None:
                    return ready
                values = (_query_field_stats(warehouse, field_keys=field_keys)
                          if field_keys is not None else _query_field_stats(warehouse))
                with _catalog_cache_lock:
                    if generation == _catalog_generation:
                        _catalog_cache[cache_key] = (monotonic(), cache_mtime, deepcopy(values))
                return values
            field_stats = coalesced_read(f"catalog:{cache_key}:{cache_mtime}:{generation}", read_stats)

    fields = [
        {
            "key": key,
            "label_zh": spec.description,
            "label_en": _FIELD_LABELS_EN.get(key, key),
            "description": spec.description,
            "dtype": spec.dtype,
            "source_table": spec.source_table,
            "layer": spec.layer,
            "point_in_time": spec.point_in_time,
            "snippet": key,
            **_field_capability(key, field_stats.get(key, {})),
            "enabled": True,
        }
        for key, spec in FIELD_CATALOG.items()
        if field_keys is None or key in field_keys
    ]
    functions = []
    for key, spec in FUNCTION_CATALOG.items():
        meta = _FUNCTION_META[key]
        functions.append({
            "key": key,
            "label_zh": meta["label_zh"],
            "label_en": meta["label_en"],
            "description": spec.description,
            "category": spec.category,
            "signature": meta["signature"],
            "snippet": meta["snippet"],
            "params": meta["params"],
            "min_args": spec.min_args,
            "max_args": spec.max_args,
            "window_arg_index": spec.window_arg_index,
            "enabled": True,
        })
    return {
        "dsl_version": DSL_VERSION,
        "compiler_version": COMPILER_VERSION,
        "fields": fields,
        "functions": functions,
        "disabled_functions": list(_DISABLED_FUNCTIONS),
        "operators": list(_OPERATORS),
        "templates": list(_TEMPLATES),
    }


__all__ = ["build_formula_catalog"]
