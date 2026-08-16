"""Public formula catalog derived from the compiler's executable capability set."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from threading import Lock
from time import monotonic
from typing import Any, Mapping

from app.services.factors.factor_compiler import (
    COMPILER_VERSION,
    DSL_VERSION,
    FIELD_CATALOG,
    FUNCTION_CATALOG,
)


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
    "main_net_inflow": "Main Net Inflow",
    "roe_ttm": "ROE TTM",
    "lhb_institution_net": "Institution Net",
    "hot_rank_pct": "Hot Rank Percentile",
    "proxy_score": "Tail Proxy Score",
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
    "proxy_score": {
        "data_mode": "blocked",
        "status_reason_zh": "尾盘分钟代理当前维持 blocked，未接入可评价的分钟历史。",
    },
    "etf_premium_discount": {
        "data_mode": "snapshot", "evaluation_mode": "snapshot",
        "status_reason_zh": "ETF 当日指标快照，仅用于 ETF 快照评价。",
    },
    "etf_tracking_error": {
        "data_mode": "snapshot", "evaluation_mode": "snapshot",
        "status_reason_zh": "ETF 当日指标快照，仅用于 ETF 快照评价。",
    },
    "etf_fund_size": {
        "data_mode": "snapshot", "evaluation_mode": "snapshot",
        "status_reason_zh": "ETF 当日指标快照，仅用于 ETF 快照评价。",
    },
}

_CATALOG_CACHE_TTL_SECONDS = 30.0
_catalog_cache_lock = Lock()
_catalog_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _warehouse_cache_key(warehouse: Any) -> tuple[str, int]:
    path = getattr(warehouse, "path", None)
    if path is None:
        path = getattr(warehouse, "db_path", None)
    path_text = str(path or "")
    try:
        mtime = int(path.stat().st_mtime_ns) if path is not None else 0
    except (AttributeError, OSError):
        mtime = 0
    return path_text, mtime


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
        if rate < minimum_coverage:
            break
        trailing_days += 1
    return {
        "trailing_coverage_days": trailing_days,
        "daily_coverage_p50": percentile(0.50),
        "daily_coverage_p90": percentile(0.90),
        "latest_daily_coverage": round(rates[0], 6) if rates else None,
    }


def _query_cross_section_coverage(
    conn: Any,
    *,
    table: str,
    field: str,
    minimum_coverage: float,
) -> dict[str, Any]:
    """Compare a field with the daily-bar stock universe over recent trade days."""
    try:
        rows = conn.execute(
            f"""
            WITH stock_bars AS (
                SELECT bars.symbol, bars.trade_date
                FROM raw_daily_bars AS bars
                JOIN raw_asset_universe AS universe
                  ON universe.symbol = bars.symbol
                WHERE LOWER(universe.asset_type) = 'stock'
                  AND LOWER(universe.region) = 'cn'
                  AND universe.is_active = TRUE
            ),
            calendar AS (
                SELECT DISTINCT trade_date
                FROM stock_bars
                ORDER BY trade_date DESC
                LIMIT 260
            )
            SELECT
                calendar.trade_date,
                COUNT(DISTINCT bars.symbol) AS universe_symbols,
                COUNT(DISTINCT CASE WHEN source.{field} IS NOT NULL THEN source.symbol END) AS covered_symbols
            FROM calendar
            JOIN stock_bars AS bars ON bars.trade_date = calendar.trade_date
            LEFT JOIN {table} AS source
                ON source.symbol = bars.symbol
                AND source.trade_date = calendar.trade_date
            GROUP BY calendar.trade_date
            ORDER BY calendar.trade_date DESC
            """
        ).fetchall()
    except Exception:
        return {
            "trailing_coverage_days": 0,
            "daily_coverage_p50": None,
            "daily_coverage_p90": None,
            "latest_daily_coverage": None,
        }
    daily_counts = [(row[0], int(row[1] or 0), int(row[2] or 0)) for row in rows]
    return _coverage_summary(daily_counts, minimum_coverage=minimum_coverage)


def _query_field_stats(warehouse: Any) -> dict[str, dict[str, Any]]:
    """Read lightweight field-level coverage evidence from DuckDB.

    The daily-bars table is intentionally inspected through table existence and
    row count only; counting every nullable column on a multi-billion-row
    warehouse would make opening the formula editor an expensive operation.
    External tables are much smaller, so their non-null field counts are read
    in one aggregate query per table.
    """
    table_fields: dict[str, list[str]] = {}
    for field_name, spec in FIELD_CATALOG.items():
        table_fields.setdefault(spec.source_table, []).append(field_name)

    stats: dict[str, dict[str, Any]] = {}
    try:
        with warehouse.connection(read_only=True) as conn:
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
                    if is_daily_table:
                        continue
                    if field not in columns:
                        continue
                    alias = f"nonnull_{index}"
                    count_aliases[field] = alias
                    select_parts.append(f"COUNT({field}) AS {alias}")
                try:
                    row = conn.execute(
                        f"SELECT {', '.join(select_parts)} FROM {table}"
                    ).fetchone()
                except Exception:
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
                        nonnull_rows = (
                            row_values["table_rows"]
                            if is_daily_table and field in columns
                            else row_values.get(f"nonnull:{field}", 0)
                        )
                        stats[field] = {
                            **row_values,
                            "nonnull_rows": nonnull_rows,
                            "column_exists": field in columns,
                            "derived": False,
                        }
                    policy = _FIELD_DATA_POLICIES.get(field, {})
                    if policy.get("minimum_continuity_days"):
                        coverage = _query_cross_section_coverage(
                            conn,
                            table=table,
                            field=field,
                            minimum_coverage=float(
                                policy.get("minimum_daily_coverage", 0.0)
                            ),
                        )
                        stats[field].update(coverage)
                        stats[field]["continuity_days"] = coverage[
                            "trailing_coverage_days"
                        ]
    except Exception:
        return {}
    return stats


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
        reason = "源表为空或字段没有非空记录。"
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
        "first_date": stats.get("first_date"),
        "latest_date": stats.get("latest_date"),
        "derived": bool(stats.get("derived", False)),
        "derived_from": list(policy.get("derived_from", [])),
    }


def build_formula_catalog(warehouse: Any | None = None) -> dict[str, Any]:
    """Build the syntax catalog plus a live data-capability snapshot.

    ``enabled`` continues to mean “accepted by the compiler”, so users can
    save a draft that is waiting for a data source.  ``availability`` and
    ``evaluation_enabled`` are the runtime truth used by preview/evaluation
    gates and the UI.  This prevents a blocked external field from being
    presented as if it already had historical coverage.
    """
    field_stats: dict[str, dict[str, Any]] = {}
    if warehouse is not None:
        cache_key, cache_mtime = _warehouse_cache_key(warehouse)
        now = monotonic()
        with _catalog_cache_lock:
            cached = _catalog_cache.get(cache_key)
            if cached and cached[0] + _CATALOG_CACHE_TTL_SECONDS > now and cached[1] == cache_mtime:
                field_stats = deepcopy(cached[2])
        if not field_stats:
            field_stats = _query_field_stats(warehouse)
            with _catalog_cache_lock:
                _catalog_cache[cache_key] = (now, cache_mtime, deepcopy(field_stats))

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
