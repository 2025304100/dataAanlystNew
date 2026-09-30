"""DuckDB-backed analytical warehouse for factor research data.

DuckDB is imported lazily so the existing application still starts when the
feature flag is disabled and the optional dependency has not been installed.
All writes are serialized per warehouse path because DuckDB supports many
readers but only one writer process at a time.
"""
from __future__ import annotations

import threading
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from app.core.config import settings
from app.services.factors.warehouse_locks import (
    WarehouseLockAcquisition,
    WarehouseLockInfo,
    WarehouseLockUnavailable,
    acquire_warehouse_lock,
    diagnose_warehouse_lock,
    release_warehouse_lock,
)


SCHEMA_VERSION = "4"

# 标签库的常驻批次：流水线维护它，挖掘与评估实验室只按窗口读它。
# 批次 ID 不再隐含窗口——窗口是读取参数（``get_target_panel`` 的 start/end/as_of），
# 因此一份覆盖可以服务多方，而不必每个 run 重抄一份全窗口快照（VIZ-0930-27）。
# adjust 不在 factor_targets 的主键里，将来若出现第二种复权口径必须另起批次 ID。
SHARED_TARGET_BATCH_ID = "targets-labels"

#: 当前唯一被 `target_engine` 产出的口径（entry=T+1 / exit=T+5）。
#: 放在 store 侧是为了让"指针优先常驻批次"的判断不必反向依赖 target_engine
#: （后者 import store，反向会成环）；target_engine.TARGET_CODE 即别名至此。
DEFAULT_TARGET_CODE = "target_5d_return"

_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_CLASS_INSTANCES: dict = {}


class FactorWarehouseUnavailable(RuntimeError):
    """Raised when the local analytical warehouse cannot be used."""


@dataclass(frozen=True)
class WarehouseHealth:
    available: bool
    path: str
    schema_version: str | None = None
    raw_daily_bars: int = 0
    latest_trade_date: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS warehouse_metadata (
        key VARCHAR PRIMARY KEY,
        value VARCHAR NOT NULL,
        updated_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS warehouse_watermarks (
        source_key VARCHAR PRIMARY KEY,
        last_row_id BIGINT NOT NULL,
        updated_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingestion_batches (
        batch_id VARCHAR PRIMARY KEY,
        source_key VARCHAR NOT NULL,
        scope_json VARCHAR,
        status VARCHAR NOT NULL,
        started_at TIMESTAMP NOT NULL,
        finished_at TIMESTAMP,
        rows_received BIGINT DEFAULT 0,
        rows_written BIGINT DEFAULT 0,
        error_json VARCHAR,
        source_contract_version VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_daily_bars (
        symbol VARCHAR NOT NULL,
        trade_date DATE NOT NULL,
        adjust VARCHAR NOT NULL,
        business_symbol_id BIGINT,
        universe_symbol_id BIGINT,
        open DOUBLE,
        high DOUBLE,
        low DOUBLE,
        close DOUBLE,
        volume DOUBLE,
        amount DOUBLE,
        turnover_rate DOUBLE,
        source VARCHAR NOT NULL,
        source_origin VARCHAR NOT NULL,
        source_row_id BIGINT NOT NULL,
        source_updated_at TIMESTAMP,
        ingested_at TIMESTAMP NOT NULL,
        batch_id VARCHAR NOT NULL,
        PRIMARY KEY (symbol, trade_date, adjust)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_asset_universe (
        symbol VARCHAR PRIMARY KEY,
        name VARCHAR,
        asset_type VARCHAR NOT NULL,
        market VARCHAR,
        region VARCHAR,
        is_active BOOLEAN NOT NULL,
        source VARCHAR NOT NULL,
        source_row_id BIGINT NOT NULL,
        updated_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_valuation_snapshots (
        symbol VARCHAR NOT NULL,
        trade_date DATE NOT NULL,
        pe_ttm DOUBLE,
        pb DOUBLE,
        dividend_yield DOUBLE,
        total_market_cap DOUBLE,
        circulating_market_cap DOUBLE,
        source VARCHAR NOT NULL,
        source_hash VARCHAR,
        ingested_at TIMESTAMP NOT NULL,
        batch_id VARCHAR NOT NULL,
        PRIMARY KEY (symbol, trade_date, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_financial_reports (
        symbol VARCHAR NOT NULL,
        report_period DATE NOT NULL,
        announcement_date DATE NOT NULL,
        report_type VARCHAR NOT NULL,
        roe_ttm DOUBLE,
        net_profit DOUBLE,
        revenue DOUBLE,
        net_profit_yoy DOUBLE,
        revenue_yoy DOUBLE,
        source VARCHAR NOT NULL,
        source_hash VARCHAR,
        ingested_at TIMESTAMP NOT NULL,
        batch_id VARCHAR NOT NULL,
        PRIMARY KEY (
            symbol, report_period, announcement_date, report_type, source
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_fund_flows (
        symbol VARCHAR NOT NULL,
        trade_date DATE NOT NULL,
        main_net_inflow DOUBLE,
        main_net_inflow_pct DOUBLE,
        super_large_net_inflow DOUBLE,
        large_net_inflow DOUBLE,
        medium_net_inflow DOUBLE,
        small_net_inflow DOUBLE,
        source VARCHAR NOT NULL,
        ingested_at TIMESTAMP NOT NULL,
        batch_id VARCHAR NOT NULL,
        PRIMARY KEY (symbol, trade_date, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_sentiment (
        symbol VARCHAR NOT NULL,
        trade_date DATE NOT NULL,
        hot_rank DOUBLE,
        hot_rank_total DOUBLE,
        hot_rank_pct DOUBLE,
        has_lhb BOOLEAN,
        lhb_institution_net DOUBLE,
        source VARCHAR NOT NULL,
        ingested_at TIMESTAMP NOT NULL,
        batch_id VARCHAR NOT NULL,
        PRIMARY KEY (symbol, trade_date, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_tail_proxy (
        symbol VARCHAR NOT NULL,
        trade_date DATE NOT NULL,
        minute_count INTEGER NOT NULL,
        tail_minute_count INTEGER NOT NULL,
        day_amount DOUBLE NOT NULL,
        tail_amount DOUBLE NOT NULL,
        tail_amount_share DOUBLE NOT NULL,
        tail_activity_ratio DOUBLE NOT NULL,
        tail_return DOUBLE NOT NULL,
        close_location DOUBLE NOT NULL,
        proxy_score DOUBLE NOT NULL,
        source VARCHAR NOT NULL,
        ingested_at TIMESTAMP NOT NULL,
        batch_id VARCHAR NOT NULL,
        PRIMARY KEY (symbol, trade_date, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_etf_indicators (
        symbol VARCHAR NOT NULL,
        trade_date DATE NOT NULL,
        nav DOUBLE,
        close DOUBLE,
        premium_discount DOUBLE,
        fund_size DOUBLE,
        total_shares DOUBLE,
        shares_change DOUBLE,
        tracking_error DOUBLE,
        premium_discount_score DOUBLE,
        source VARCHAR NOT NULL,
        ingested_at TIMESTAMP NOT NULL,
        batch_id VARCHAR NOT NULL,
        PRIMARY KEY (symbol, trade_date, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_macro (
        indicator_key VARCHAR NOT NULL,
        period DATE NOT NULL,
        value DOUBLE,
        previous_value DOUBLE,
        source VARCHAR NOT NULL,
        ingested_at TIMESTAMP NOT NULL,
        batch_id VARCHAR NOT NULL,
        PRIMARY KEY (indicator_key, period, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factor_values (
        symbol VARCHAR NOT NULL,
        trade_date DATE NOT NULL,
        factor_code VARCHAR NOT NULL,
        factor_version INTEGER NOT NULL,
        raw_value DOUBLE,
        winsorized_value DOUBLE,
        normalized_value DOUBLE,
        is_imputed BOOLEAN DEFAULT FALSE,
        imputation_method VARCHAR,
        eligible BOOLEAN NOT NULL,
        data_cutoff_at TIMESTAMP NOT NULL,
        calc_batch_id VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL,
        PRIMARY KEY (
            symbol, trade_date, factor_code, factor_version, calc_batch_id
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factor_targets (
        symbol VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        entry_date DATE,
        exit_date DATE,
        target_code VARCHAR NOT NULL,
        target_value DOUBLE,
        is_tradable BOOLEAN NOT NULL,
        invalid_reason VARCHAR,
        calc_batch_id VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL,
        PRIMARY KEY (symbol, signal_date, target_code, calc_batch_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS warehouse_label_coverage (
        batch_id VARCHAR NOT NULL,
        target_code VARCHAR NOT NULL,
        adjust VARCHAR NOT NULL,
        min_signal_date DATE,
        max_signal_date DATE,
        signal_days INTEGER NOT NULL DEFAULT 0,
        symbol_count INTEGER NOT NULL DEFAULT 0,
        rows_total BIGINT NOT NULL DEFAULT 0,
        rows_tradable BIGINT NOT NULL DEFAULT 0,
        bars_latest_date DATE,
        updated_at TIMESTAMP NOT NULL,
        PRIMARY KEY (batch_id, target_code, adjust)
    )
    """,
)

_DAILY_BAR_COLUMNS = (
    "symbol",
    "trade_date",
    "adjust",
    "business_symbol_id",
    "universe_symbol_id",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover_rate",
    "source",
    "source_origin",
    "source_row_id",
    "source_updated_at",
    "ingested_at",
    "batch_id",
)

_UPSERT_DAILY_BAR_SQL = """
    INSERT INTO raw_daily_bars (
        symbol, trade_date, adjust, business_symbol_id, universe_symbol_id,
        open, high, low, close, volume, amount, turnover_rate, source,
        source_origin, source_row_id, source_updated_at, ingested_at, batch_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (symbol, trade_date, adjust) DO UPDATE SET
        business_symbol_id = COALESCE(
            excluded.business_symbol_id, raw_daily_bars.business_symbol_id
        ),
        universe_symbol_id = COALESCE(
            excluded.universe_symbol_id, raw_daily_bars.universe_symbol_id
        ),
        open = excluded.open,
        high = excluded.high,
        low = excluded.low,
        close = excluded.close,
        volume = excluded.volume,
        amount = excluded.amount,
        turnover_rate = excluded.turnover_rate,
        source = excluded.source,
        source_origin = excluded.source_origin,
        source_row_id = excluded.source_row_id,
        source_updated_at = excluded.source_updated_at,
        ingested_at = excluded.ingested_at,
        batch_id = excluded.batch_id
"""

_UPSERT_DAILY_BAR_FRAME_SQL = """
    INSERT INTO raw_daily_bars (
        symbol, trade_date, adjust, business_symbol_id, universe_symbol_id,
        open, high, low, close, volume, amount, turnover_rate, source,
        source_origin, source_row_id, source_updated_at, ingested_at, batch_id
    )
    SELECT
        symbol, trade_date, adjust, business_symbol_id, universe_symbol_id,
        open, high, low, close, volume, amount, turnover_rate, source,
        source_origin, source_row_id, source_updated_at, ingested_at, batch_id
    FROM incoming_daily_bars
    ON CONFLICT (symbol, trade_date, adjust) DO UPDATE SET
        business_symbol_id = COALESCE(
            excluded.business_symbol_id, raw_daily_bars.business_symbol_id
        ),
        universe_symbol_id = COALESCE(
            excluded.universe_symbol_id, raw_daily_bars.universe_symbol_id
        ),
        open = excluded.open,
        high = excluded.high,
        low = excluded.low,
        close = excluded.close,
        volume = excluded.volume,
        amount = excluded.amount,
        turnover_rate = excluded.turnover_rate,
        source = excluded.source,
        source_origin = excluded.source_origin,
        source_row_id = excluded.source_row_id,
        source_updated_at = excluded.source_updated_at,
        ingested_at = excluded.ingested_at,
        batch_id = excluded.batch_id
"""

_UPSERT_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "raw_asset_universe": (
        "symbol",
        "name",
        "asset_type",
        "market",
        "region",
        "is_active",
        "source",
        "source_row_id",
        "updated_at",
    ),
    "raw_valuation_snapshots": (
        "symbol",
        "trade_date",
        "pe_ttm",
        "pb",
        "dividend_yield",
        "total_market_cap",
        "circulating_market_cap",
        "source",
        "source_hash",
        "ingested_at",
        "batch_id",
    ),
    "raw_financial_reports": (
        "symbol",
        "report_period",
        "announcement_date",
        "report_type",
        "roe_ttm",
        "net_profit",
        "revenue",
        "net_profit_yoy",
        "revenue_yoy",
        "source",
        "source_hash",
        "ingested_at",
        "batch_id",
    ),
    "raw_fund_flows": (
        "symbol",
        "trade_date",
        "main_net_inflow",
        "main_net_inflow_pct",
        "super_large_net_inflow",
        "large_net_inflow",
        "medium_net_inflow",
        "small_net_inflow",
        "source",
        "ingested_at",
        "batch_id",
    ),
    "raw_sentiment": (
        "symbol",
        "trade_date",
        "hot_rank",
        "hot_rank_total",
        "hot_rank_pct",
        "has_lhb",
        "lhb_institution_net",
        "source",
        "ingested_at",
        "batch_id",
    ),
    "raw_tail_proxy": (
        "symbol",
        "trade_date",
        "minute_count",
        "tail_minute_count",
        "day_amount",
        "tail_amount",
        "tail_amount_share",
        "tail_activity_ratio",
        "tail_return",
        "close_location",
        "proxy_score",
        "source",
        "ingested_at",
        "batch_id",
    ),
    "raw_etf_indicators": (
        "symbol", "trade_date", "nav", "close", "premium_discount",
        "fund_size", "total_shares", "shares_change", "tracking_error",
        "premium_discount_score", "source", "ingested_at", "batch_id",
    ),
    "raw_macro": (
        "indicator_key",
        "period",
        "value",
        "previous_value",
        "source",
        "ingested_at",
        "batch_id",
    ),
    "factor_values": (
        "symbol",
        "trade_date",
        "factor_code",
        "factor_version",
        "raw_value",
        "winsorized_value",
        "normalized_value",
        "is_imputed",
        "imputation_method",
        "eligible",
        "data_cutoff_at",
        "calc_batch_id",
        "created_at",
    ),
    "factor_targets": (
        "symbol",
        "signal_date",
        "entry_date",
        "exit_date",
        "target_code",
        "target_value",
        "is_tradable",
        "invalid_reason",
        "calc_batch_id",
        "created_at",
    ),
}

_UPSERT_TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "raw_asset_universe": ("symbol",),
    "raw_valuation_snapshots": ("symbol", "trade_date", "source"),
    "raw_financial_reports": (
        "symbol",
        "report_period",
        "announcement_date",
        "report_type",
        "source",
    ),
    "raw_fund_flows": ("symbol", "trade_date", "source"),
    "raw_sentiment": ("symbol", "trade_date", "source"),
    "raw_tail_proxy": ("symbol", "trade_date", "source"),
    "raw_etf_indicators": ("symbol", "trade_date", "source"),
    "raw_macro": ("indicator_key", "period", "source"),
    "factor_values": (
        "symbol",
        "trade_date",
        "factor_code",
        "factor_version",
        "calc_batch_id",
    ),
    "factor_targets": (
        "symbol",
        "signal_date",
        "target_code",
        "calc_batch_id",
    ),
}


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_duckdb():
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise FactorWarehouseUnavailable(
            "DuckDB is not installed. Install project requirements before "
            "enabling the factor warehouse."
        ) from exc
    return duckdb


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())



# Fix A1. Process-wide shared DuckDB connections + graceful shutdown hooks.
import atexit as _wh_atexit

_WH_POOLS = dict()
_WH_POOLS_GUARD = _PATH_LOCKS_GUARD


def _get_or_create_shared_connections(path):
    key = str(path.resolve())
    with _WH_POOLS_GUARD:
        pool = _WH_POOLS.get(key)
        if pool is None:
            ddb = _load_duckdb()
            path.parent.mkdir(parents=True, exist_ok=True)
            rw = ddb.connect(str(path), read_only=False)
            ro = None
            if path.exists():
                try: ro = ddb.connect(str(path), read_only=True)
                except Exception: ro = None
            lock = threading.RLock()
            pool = dict()
            pool["rw"] = rw; pool["ro"] = ro; pool["lock"] = lock; pool["path"] = path;
            _WH_POOLS[key] = pool
            def _shut(pool_ref=pool):
                with pool_ref["lock"]:
                    for attr in ("ro", "rw"):
                        c = pool_ref.get(attr)
                        if c is None: continue
                        try: c.close()
                        except Exception: pass
                        pool_ref[attr] = None
            _wh_atexit.register(_shut)
        return pool


def shutdown_factor_warehouse_pools():
    keys = list(_WH_POOLS.keys())
    for k in keys:
        pool = _WH_POOLS.pop(k, None)
        if not pool: continue
        with pool["lock"]:
            for attr in ("ro", "rw"):
                c = pool.get(attr)
                if c is None: continue
                try: c.close()
                except Exception: pass
                pool[attr] = None
    with _WH_POOLS_GUARD: _WH_POOLS.clear()
class FactorWarehouse:
    """Small, explicit access layer around the local DuckDB file. Shared connections via get_for_path avoid DB_LOCK_TIMEOUT from concurrent threads."""

    @classmethod
    def get_for_path(cls, path=None):
        configured = Path(path) if path is not None else settings.factor_warehouse_path
        key = str(configured.expanduser().resolve())
        with _PATH_LOCKS_GUARD:
            inst = _PATH_CLASS_INSTANCES.get(key)
            if inst is None:
                inst = cls(path=path)
                _PATH_CLASS_INSTANCES[key] = inst
            return inst

    def __init__(self, path=None):
        configured = Path(path) if path is not None else settings.factor_warehouse_path
        self.path = configured.expanduser()
        self._write_lock = _path_lock(self.path)
        self._initialized = False
        self._pool_ref = _get_or_create_shared_connections(self.path)

    def __enter__(self):
        self._pool_ref["lock"].acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._pool_ref["lock"].release()
        except Exception:
            pass

    def close(self):
        return None

    # Fix A1: use shared process-wide DuckDB conns (no new duckdb.connect per call).
    # Guard: serialize via per-path RLock so threads share conns safely.
    @contextmanager
    def connection(self, *, read_only: bool = False):
        if not getattr(self, "_pool_ref", None):
            self._pool_ref = _get_or_create_shared_connections(self.path)
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if read_only and not self.path.exists():
            raise FactorWarehouseUnavailable("Factor warehouse does not exist: " + str(self.path))
        pool = self._pool_ref
        lock = pool["lock"]
        shared_conn = pool["ro"] if (read_only and pool.get("ro") is not None) else pool["rw"]
        if shared_conn is None:
            raise FactorWarehouseUnavailable("Factor warehouse connections already shut down: " + str(self.path))
        with lock:
            try:
                yield shared_conn
            finally:
                try:
                    if shared_conn is pool.get("rw"):
                        shared_conn.execute("BEGIN TRANSACTION")
                        shared_conn.execute("COMMIT")
                except Exception:
                    pass

    def initialize(self) -> None:
        if self._initialized and self.path.exists():
            return
        with self._write_lock, self.connection() as conn:
            if self._initialized and self.path.exists():
                return
            conn.execute("BEGIN TRANSACTION")
            try:
                for statement in SCHEMA_STATEMENTS:
                    conn.execute(statement)
                conn.execute(
                    """
                    INSERT INTO warehouse_metadata (key, value, updated_at)
                    VALUES ('schema_version', ?, ?)
                    ON CONFLICT (key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    [SCHEMA_VERSION, _utcnow_naive()],
                )
                conn.execute("COMMIT")
                self._initialized = True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def health(self) -> WarehouseHealth:
        try:
            if not self.path.exists():
                return WarehouseHealth(
                    available=False,
                    path=str(self.path),
                    error="warehouse_not_initialized",
                )
            with self.connection(read_only=True) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'main'"
                    ).fetchall()
                }
                if "warehouse_metadata" not in tables:
                    return WarehouseHealth(
                        available=False,
                        path=str(self.path),
                        error="schema_not_initialized",
                    )
                schema_row = conn.execute(
                    "SELECT value FROM warehouse_metadata "
                    "WHERE key = 'schema_version'"
                ).fetchone()
                count = 0
                latest = None
                if "raw_daily_bars" in tables:
                    count, latest = conn.execute(
                        "SELECT COUNT(*), MAX(trade_date) FROM raw_daily_bars"
                    ).fetchone()
                return WarehouseHealth(
                    available=True,
                    path=str(self.path),
                    schema_version=schema_row[0] if schema_row else None,
                    raw_daily_bars=int(count or 0),
                    latest_trade_date=str(latest) if latest is not None else None,
                )
        except Exception as exc:
            return WarehouseHealth(
                available=False,
                path=str(self.path),
                error=f"{type(exc).__name__}: {exc}",
            )

    def get_watermark(self, source_key: str) -> int:
        self.initialize()
        with self.connection(read_only=True) as conn:
            row = conn.execute(
                "SELECT last_row_id FROM warehouse_watermarks "
                "WHERE source_key = ?",
                [source_key],
            ).fetchone()
            return int(row[0]) if row else 0

    def upsert_daily_bars(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        source_key: str,
        watermark: int,
    ) -> int:
        """Atomically upsert a batch and advance its source watermark."""
        records = [
            {column: row.get(column) for column in _DAILY_BAR_COLUMNS}
            for row in rows
        ]
        self.initialize()
        with self._write_lock, self.connection() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                if records:
                    # DuckDB executemany executes the statement once per row.
                    # Register one frame so DuckDB merges the whole batch in a
                    # single vectorized statement.
                    import pandas as pd

                    frame = pd.DataFrame.from_records(
                        records, columns=list(_DAILY_BAR_COLUMNS)
                    )
                    conn.register("incoming_daily_bars", frame)
                    conn.execute(_UPSERT_DAILY_BAR_FRAME_SQL)
                    conn.unregister("incoming_daily_bars")
                conn.execute(
                    """
                    INSERT INTO warehouse_watermarks (
                        source_key, last_row_id, updated_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT (source_key) DO UPDATE SET
                        last_row_id = excluded.last_row_id,
                        updated_at = excluded.updated_at
                    """,
                    [source_key, int(watermark), _utcnow_naive()],
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.unregister("incoming_daily_bars")
                except Exception:
                    pass
                conn.execute("ROLLBACK")
                raise
        return len(records)

    def delete_watermarks(self, source_key_prefix: str) -> int:
        """Delete temporary checkpoint watermarks matching a source prefix."""
        self.initialize()
        with self._write_lock, self.connection() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM warehouse_watermarks "
                        "WHERE source_key LIKE ?",
                        [f"{source_key_prefix}%"],
                    ).fetchone()[0]
                )
                conn.execute(
                    "DELETE FROM warehouse_watermarks WHERE source_key LIKE ?",
                    [f"{source_key_prefix}%"],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return count

    def upsert_records(
        self,
        table: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        source_key: str | None = None,
        watermark: int | None = None,
    ) -> int:
        """Upsert a supported warehouse table and optionally advance a watermark."""
        columns = _UPSERT_TABLE_COLUMNS.get(table)
        keys = _UPSERT_TABLE_KEYS.get(table)
        if columns is None or keys is None:
            raise ValueError(f"unsupported warehouse table: {table}")
        if (source_key is None) != (watermark is None):
            raise ValueError("source_key and watermark must be provided together")

        records = [tuple(row.get(column) for column in columns) for row in rows]
        placeholders = ", ".join("?" for _ in columns)
        update_columns = [column for column in columns if column not in keys]
        assignments = ", ".join(
            f"{column} = excluded.{column}" for column in update_columns
        )
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {assignments}"
        )

        self.initialize()
        with self._write_lock, self.connection() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                if records:
                    conn.executemany(sql, records)
                if source_key is not None and watermark is not None:
                    conn.execute(
                        """
                        INSERT INTO warehouse_watermarks (
                            source_key, last_row_id, updated_at
                        ) VALUES (?, ?, ?)
                        ON CONFLICT (source_key) DO UPDATE SET
                            last_row_id = excluded.last_row_id,
                            updated_at = excluded.updated_at
                        """,
                        [source_key, int(watermark), _utcnow_naive()],
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(records)

    def upsert_frame(self, table: str, frame: Any) -> int:
        """Bulk upsert a pandas-compatible frame through DuckDB registration."""
        columns = _UPSERT_TABLE_COLUMNS.get(table)
        keys = _UPSERT_TABLE_KEYS.get(table)
        if columns is None or keys is None:
            raise ValueError(f"unsupported warehouse table: {table}")
        if frame is None or frame.empty:
            return 0
        missing = [column for column in columns if column not in frame.columns]
        if missing:
            raise ValueError(
                f"{table} frame missing columns: {', '.join(missing)}"
            )
        update_columns = [column for column in columns if column not in keys]
        assignments = ", ".join(
            f"{column} = excluded.{column}" for column in update_columns
        )
        selected = frame.loc[:, list(columns)]
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"SELECT {', '.join(columns)} FROM incoming_frame "
            f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {assignments}"
        )
        self.initialize()
        with self._write_lock, self.connection() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.register("incoming_frame", selected)
                conn.execute(sql)
                conn.unregister("incoming_frame")
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.unregister("incoming_frame")
                except Exception:
                    pass
                conn.execute("ROLLBACK")
                raise
        return len(selected)

    def prune_calculation_batches(
        self,
        *,
        keep_batches: int | None = None,
        protected_batch_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Delete old analytical calculation rows without touching audit metadata.

        ``factor_values`` and ``factor_targets`` deliberately include the
        calculation batch in their primary keys, so repeated score refreshes
        create traceable versions.  Retention is therefore applied by batch,
        not by individual rows.  The newest ``keep_batches`` batches and any
        explicitly protected IDs are retained.  ``ingestion_batches`` is left
        intact so historical audit records remain queryable.
        """
        if keep_batches is None:
            keep_batches = int(os.environ.get("FACTOR_BATCH_RETENTION_COUNT", "30"))
        if keep_batches < 1:
            raise ValueError("keep_batches must be at least 1")
        protected = {str(item) for item in protected_batch_ids if item}
        # 常驻标签批次不能靠"它恰好是最新的一批"活下来：一旦有更新的计算批次，
        # 按数量保留就会把全市场标签删掉，挖掘与评估会静默拿到空面板。
        protected.add(SHARED_TARGET_BATCH_ID)
        self.initialize()
        with self._write_lock, self.connection() as conn:
            rows = conn.execute(
                """
                SELECT calc_batch_id, MAX(created_at) AS latest_at
                FROM (
                    SELECT calc_batch_id, created_at FROM factor_values
                    UNION ALL
                    SELECT calc_batch_id, created_at FROM factor_targets
                ) batches
                GROUP BY calc_batch_id
                ORDER BY latest_at DESC, calc_batch_id DESC
                """
            ).fetchall()
            retained = {str(row[0]) for row in rows[:keep_batches]} | protected
            candidates = [str(row[0]) for row in rows if str(row[0]) not in retained]
            if not candidates:
                return {
                    "keep_batches": keep_batches,
                    "retained_batch_ids": sorted(retained),
                    "deleted_batch_ids": [],
                    "factor_value_rows_deleted": 0,
                    "factor_target_rows_deleted": 0,
                }
            placeholders = ", ".join("?" for _ in candidates)
            conn.execute("BEGIN TRANSACTION")
            try:
                factor_deleted = int(conn.execute(
                    f"SELECT COUNT(*) FROM factor_values WHERE calc_batch_id IN ({placeholders})",
                    candidates,
                ).fetchone()[0] or 0)
                target_deleted = int(conn.execute(
                    f"SELECT COUNT(*) FROM factor_targets WHERE calc_batch_id IN ({placeholders})",
                    candidates,
                ).fetchone()[0] or 0)
                conn.execute(
                    f"DELETE FROM factor_values WHERE calc_batch_id IN ({placeholders})",
                    candidates,
                )
                conn.execute(
                    f"DELETE FROM factor_targets WHERE calc_batch_id IN ({placeholders})",
                    candidates,
                )
                conn.execute("COMMIT")
                # DELETE frees logical rows; CHECKPOINT compacts the local file.
                conn.execute("CHECKPOINT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {
            "keep_batches": keep_batches,
            "retained_batch_ids": sorted(retained),
            "deleted_batch_ids": candidates,
            "factor_value_rows_deleted": factor_deleted,
            "factor_target_rows_deleted": target_deleted,
        }

    # ------------------------------------------------------------------
    # WPD-03: cross-process lock governance
    # ------------------------------------------------------------------

    def acquire_process_lock(
        self, *, timeout_seconds: float = 0.0
    ) -> WarehouseLockAcquisition:
        """Acquire a cross-process mutual-exclusion lock for this warehouse.

        Delegates to :func:`warehouse_locks.acquire_warehouse_lock`.
        """
        return acquire_warehouse_lock(
            self.path, timeout_seconds=timeout_seconds
        )

    def release_process_lock(
        self, *, owner_pid: int | None = None
    ) -> bool:
        """Release the cross-process lock previously acquired via
        :meth:`acquire_process_lock`.
        """
        return release_warehouse_lock(self.path, owner_pid=owner_pid)

    def diagnose_lock(self) -> WarehouseLockInfo:
        """Return diagnostic information about the warehouse file lock."""
        return diagnose_warehouse_lock(self.path)

    def describe_table(self, table_name: str) -> list[str]:
        """返回指定表的列名列表。

        如果表不存在或查询失败，返回空列表。
        """
        try:
            if not self.path.exists():
                return []
            with self.connection(read_only=True) as conn:
                rows = conn.execute(
                    f"DESCRIBE {table_name}"
                ).fetchall()
                return [row[0] for row in rows]
        except Exception:
            return []

    def list_trade_dates(self, *, limit: int | None = None) -> list[date]:
        """返回仓库中 raw_daily_bars 表的去重交易日列表（按 DESC 排序）。

        如果表不存在或查询失败，返回空列表。
        """
        try:
            if not self.path.exists():
                return []
            with self.connection(read_only=True) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'main'"
                    ).fetchall()
                }
                if "raw_daily_bars" not in tables:
                    return []
                limit_clause = f" LIMIT {int(limit)}" if limit else ""
                rows = conn.execute(
                    f"SELECT DISTINCT trade_date FROM raw_daily_bars "
                    f"ORDER BY trade_date DESC{limit_clause}"
                ).fetchall()
                return [row[0] for row in rows]
        except Exception:
            return []

    @contextmanager
    def safe_write_context(
        self, *, timeout_seconds: float = 30.0
    ) -> Iterator[Any]:
        """Context manager that combines cross-process locking with an
        atomic DuckDB write transaction.

        Usage::

            with warehouse.safe_write_context(timeout_seconds=30) as conn:
                conn.execute("INSERT ...")

        Guarantees:

        * The cross-process file lock is acquired before any write.
        * On success the transaction is ``COMMIT``-ed.
        * On failure the transaction is ``ROLLBACK``-ed so a failed
          batch never partially overwrites the last successful data.
        * The lock is **always** released in the ``finally`` block,
          even when an exception occurs.

        Raises :class:`WarehouseLockUnavailable` when the lock cannot be
        acquired within *timeout_seconds*.
        """
        acquisition = acquire_warehouse_lock(
            self.path, timeout_seconds=timeout_seconds
        )
        if not acquisition.acquired:
            raise WarehouseLockUnavailable(
                str(self.path),
                acquisition.owner_pid,
                acquisition.reason or "unknown",
            )
        try:
            self.initialize()
            with self._write_lock, self.connection() as conn:
                conn.execute("BEGIN TRANSACTION")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass  # Connection may already be in error state.
                    raise
        finally:
            release_warehouse_lock(self.path)

    def _table_exists(self, table_name: str) -> bool:
        """检查表是否存在于 DuckDB warehouse 中。"""
        try:
            if not self.path.exists():
                return False
            with self.connection(read_only=True) as conn:
                rows = conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_name = ?",
                    [table_name],
                ).fetchall()
                return len(rows) > 0
        except Exception:
            return False

    def _has_tradable_rows(self, calc_batch_id: str, target_code: str) -> bool:
        """该批次该口径是否存在可交易标签行（存在性检查，不数全量）。"""
        try:
            with self.connection(read_only=True) as conn:
                row = conn.execute(
                    "SELECT 1 FROM factor_targets "
                    "WHERE calc_batch_id = ? AND target_code = ?"
                    "  AND is_tradable = TRUE LIMIT 1",
                    [calc_batch_id, target_code],
                ).fetchone()
            return row is not None
        except Exception:
            return False

    def get_latest_target_batch_id(
        self, target_code: str = DEFAULT_TARGET_CODE
    ) -> str | None:
        """返回该口径应使用的标签批次。

        常驻标签批次存在且该口径有可交易行时，它就是权威答案；否则回落到旧的
        "按 created_at 取最新"扫描——其它 horizon 的标签不在常驻批次里
        （`target_engine` 只产 5d），必须继续走这条路，不能假装常驻批次有。

        回落语义本身的坑（VIZ-0930-29）：老实现下"最新"由最后写库的任务决定，
        挖掘批次（550 天）与流水线批次（10~12 天）谁写完谁当指针，同一因子的
        同一评估会在两种宽度的标签面板之间摆动。
        """
        try:
            if not self._table_exists("factor_targets"):
                return None
            if target_code == DEFAULT_TARGET_CODE and self._has_tradable_rows(
                SHARED_TARGET_BATCH_ID, target_code
            ):
                return SHARED_TARGET_BATCH_ID
            with self.connection(read_only=True) as conn:
                row = conn.execute(
                    """
                    SELECT calc_batch_id
                    FROM (
                        SELECT
                            calc_batch_id,
                            created_at,
                            SUM(CASE WHEN is_tradable THEN 1 ELSE 0 END) AS tradable_rows
                        FROM factor_targets
                        WHERE target_code = ?
                        GROUP BY calc_batch_id, created_at
                        HAVING tradable_rows > 0
                    )
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    [target_code],
                ).fetchone()
                return row[0] if row else None
        except Exception:
            return None

    def get_target_panel(
        self,
        calc_batch_id: str,
        target_code: str = DEFAULT_TARGET_CODE,
        *,
        start_date: Any | None = None,
        end_date: Any | None = None,
        as_of_exit_date: Any | None = None,
    ) -> tuple[Any, str, str]:
        """返回 (DataFrame 含 columns=[symbol, signal_date, target_value],
        calc_batch_id, target_code)。

        DataFrame 后续被评估器 pivot(index=signal_date, columns=symbol,
        values=target_value) 使用。仅返回 is_tradable=True 的行。

        ``start_date``/``end_date`` 按 signal_date 截取窗口，``as_of_exit_date``
        追加 ``exit_date <= as_of`` 过滤。共享标签批次被多方复用时，批次 ID 不再
        隐含窗口，这两组过滤是唯一的 PIT 保证：任何一次运行都读不到 as_of 之后
        才确定的标签。

        如果 factor_targets 表不存在，返回空 DataFrame。
        """
        import pandas as pd

        empty_df = pd.DataFrame(columns=["symbol", "signal_date", "target_value"])
        try:
            if not self._table_exists("factor_targets"):
                return empty_df, calc_batch_id, target_code
            conditions = [
                "calc_batch_id = ?",
                "target_code = ?",
                "is_tradable = TRUE",
            ]
            params: list[Any] = [calc_batch_id, target_code]
            if start_date is not None:
                conditions.append("signal_date >= ?")
                params.append(start_date)
            if end_date is not None:
                conditions.append("signal_date <= ?")
                params.append(end_date)
            if as_of_exit_date is not None:
                conditions.append("exit_date <= ?")
                params.append(as_of_exit_date)
            with self.connection(read_only=True) as conn:
                df = conn.execute(
                    f"""
                    SELECT symbol, signal_date, target_value
                    FROM factor_targets
                    WHERE {' AND '.join(conditions)}
                    """,
                    params,
                ).fetchdf()
                return df, calc_batch_id, target_code
        except Exception:
            return empty_df, calc_batch_id, target_code

    # ------------------------------------------------------------------
    # 标签库覆盖：共享批次的 gate 依据
    # ------------------------------------------------------------------

    def refresh_label_coverage(
        self,
        calc_batch_id: str,
        target_code: str = DEFAULT_TARGET_CODE,
        adjust: str = "qfq",
    ) -> dict[str, Any]:
        """按当前 factor_targets / raw_daily_bars 重算覆盖摘要并写回覆盖表。

        覆盖表是 gate 的物证：调用方不必为判"标签够不够"而扫全量标签行。
        """
        self.initialize()
        with self._write_lock, self.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    MIN(signal_date),
                    MAX(signal_date),
                    COUNT(DISTINCT signal_date),
                    COUNT(DISTINCT symbol),
                    COUNT(*),
                    SUM(CASE WHEN is_tradable THEN 1 ELSE 0 END)
                FROM factor_targets
                WHERE calc_batch_id = ? AND target_code = ?
                """,
                [calc_batch_id, target_code],
            ).fetchone()
            bars_latest = conn.execute(
                "SELECT MAX(trade_date) FROM raw_daily_bars WHERE adjust = ?",
                [adjust],
            ).fetchone()[0]
            summary = {
                "batch_id": calc_batch_id,
                "target_code": target_code,
                "adjust": adjust,
                "min_signal_date": row[0],
                "max_signal_date": row[1],
                "signal_days": int(row[2] or 0),
                "symbol_count": int(row[3] or 0),
                "rows_total": int(row[4] or 0),
                "rows_tradable": int(row[5] or 0),
                "bars_latest_date": bars_latest,
            }
            conn.execute(
                """
                INSERT INTO warehouse_label_coverage (
                    batch_id, target_code, adjust, min_signal_date,
                    max_signal_date, signal_days, symbol_count, rows_total,
                    rows_tradable, bars_latest_date, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (batch_id, target_code, adjust) DO UPDATE SET
                    min_signal_date = excluded.min_signal_date,
                    max_signal_date = excluded.max_signal_date,
                    signal_days = excluded.signal_days,
                    symbol_count = excluded.symbol_count,
                    rows_total = excluded.rows_total,
                    rows_tradable = excluded.rows_tradable,
                    bars_latest_date = excluded.bars_latest_date,
                    updated_at = excluded.updated_at
                """,
                [
                    calc_batch_id,
                    target_code,
                    adjust,
                    summary["min_signal_date"],
                    summary["max_signal_date"],
                    summary["signal_days"],
                    summary["symbol_count"],
                    summary["rows_total"],
                    summary["rows_tradable"],
                    summary["bars_latest_date"],
                    _utcnow_naive(),
                ],
            )
        return summary

    def get_label_coverage(
        self,
        calc_batch_id: str,
        target_code: str = DEFAULT_TARGET_CODE,
        adjust: str = "qfq",
    ) -> dict[str, Any] | None:
        if not self._table_exists("warehouse_label_coverage"):
            return None
        with self.connection(read_only=True) as conn:
            row = conn.execute(
                """
                SELECT batch_id, target_code, adjust, min_signal_date,
                       max_signal_date, signal_days, symbol_count, rows_total,
                       rows_tradable, bars_latest_date, updated_at
                FROM warehouse_label_coverage
                WHERE batch_id = ? AND target_code = ? AND adjust = ?
                """,
                [calc_batch_id, target_code, adjust],
            ).fetchone()
        if row is None:
            return None
        keys = (
            "batch_id", "target_code", "adjust", "min_signal_date",
            "max_signal_date", "signal_days", "symbol_count", "rows_total",
            "rows_tradable", "bars_latest_date", "updated_at",
        )
        return dict(zip(keys, row))

    def list_trading_days(
        self,
        start_date: Any,
        end_date: Any,
        adjust: str = "qfq",
    ) -> list[Any]:
        """返回镜像 bars 日历在 [start, end] 内的全部交易日（升序）。

        覆盖判定的期望值只能来自这个日历，不能用自然日：停牌/节假日会让
        "缺 N 天"变成永久无法补齐的死循环。
        """
        if not self._table_exists("raw_daily_bars"):
            return []
        with self.connection(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT trade_date
                FROM raw_daily_bars
                WHERE adjust = ? AND trade_date >= ? AND trade_date <= ?
                ORDER BY trade_date
                """,
                [adjust, start_date, end_date],
            ).fetchall()
        return [row[0] for row in rows]

    def latest_bar_date(self, adjust: str = "qfq") -> Any | None:
        """全库该复权口径的最新交易日，用于标签覆盖上界推算。"""
        return self._bar_date_edge("MAX", adjust)

    def earliest_bar_date(self, adjust: str = "qfq") -> Any | None:
        """全库该复权口径的最早交易日，用于判定"请求起点早于镜像"这一头缺口。

        必须拿全库边界比，不能拿"请求区间内第一个交易日"比：开始日期落在周末或
        节假日时，后者会永远晚于开始日期，把每一次正常请求都误判成 K 线缺口。
        """
        return self._bar_date_edge("MIN", adjust)

    def _bar_date_edge(self, aggregate: str, adjust: str) -> Any | None:
        if aggregate not in ("MIN", "MAX"):
            raise ValueError("aggregate must be MIN or MAX")
        if not self._table_exists("raw_daily_bars"):
            return None
        with self.connection(read_only=True) as conn:
            row = conn.execute(
                f"SELECT {aggregate}(trade_date) FROM raw_daily_bars "
                "WHERE adjust = ?",
                [adjust],
            ).fetchone()
        return row[0] if row else None

    def shift_back_trading_days(
        self,
        anchor: Any,
        trading_days: int,
        adjust: str = "qfq",
    ) -> Any | None:
        """把 anchor 往回推 N 个交易日，返回落在日历上的那一天。

        标签需要 horizon 个未来交易日才能定值，所以覆盖上界必须用交易日推，
        否则 gate 会永远认为"最近 5 天缺失"。
        """
        if trading_days < 0:
            raise ValueError("trading_days must be >= 0")
        if not self._table_exists("raw_daily_bars"):
            return None
        with self.connection(read_only=True) as conn:
            row = conn.execute(
                """
                SELECT trade_date
                FROM (
                    SELECT DISTINCT trade_date
                    FROM raw_daily_bars
                    WHERE adjust = ? AND trade_date <= ?
                    ORDER BY trade_date DESC
                    LIMIT ?
                )
                ORDER BY trade_date ASC
                LIMIT 1
                """,
                [adjust, anchor, trading_days + 1],
            ).fetchone()
        return row[0] if row else None

    def list_covered_signal_dates(
        self,
        calc_batch_id: str,
        target_code: str,
        start_date: Any,
        end_date: Any,
    ) -> list[Any]:
        if not self._table_exists("factor_targets"):
            return []
        with self.connection(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT signal_date
                FROM factor_targets
                WHERE calc_batch_id = ? AND target_code = ?
                  AND signal_date >= ? AND signal_date <= ?
                ORDER BY signal_date
                """,
                [calc_batch_id, target_code, start_date, end_date],
            ).fetchall()
        return [row[0] for row in rows]

