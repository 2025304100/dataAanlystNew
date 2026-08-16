"""DuckDB-backed analytical warehouse for factor research data.

DuckDB is imported lazily so the existing application still starts when the
feature flag is disabled and the optional dependency has not been installed.
All writes are serialized per warehouse path because DuckDB supports many
readers but only one writer process at a time.
"""
from __future__ import annotations

import threading
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


SCHEMA_VERSION = "3"

_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


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


class FactorWarehouse:
    """Small, explicit access layer around the local DuckDB file."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured = Path(path) if path is not None else settings.factor_warehouse_path
        self.path = configured.expanduser()
        self._write_lock = _path_lock(self.path)
        self._initialized = False

    @contextmanager
    def connection(self, *, read_only: bool = False) -> Iterator[Any]:
        duckdb = _load_duckdb()
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if read_only and not self.path.exists():
            raise FactorWarehouseUnavailable(
                f"Factor warehouse does not exist: {self.path}"
            )
        try:
            conn = duckdb.connect(str(self.path), read_only=read_only)
        except Exception as exc:
            # DuckDB rejects a read-only connection when the same process
            # already has a read-write connection for this file. Health and
            # explanation endpoints are read-only by convention, so reuse the
            # process-wide read-write configuration while a pipeline batch is
            # active. Cross-process file-lock errors still fail normally.
            configuration_conflict = (
                read_only
                and "different configuration" in str(exc).lower()
            )
            if not configuration_conflict:
                raise FactorWarehouseUnavailable(
                    f"Cannot open factor warehouse: {self.path}"
                ) from exc
            try:
                conn = duckdb.connect(str(self.path))
            except Exception as fallback_exc:
                raise FactorWarehouseUnavailable(
                    f"Cannot open factor warehouse: {self.path}"
                ) from fallback_exc
        try:
            yield conn
        finally:
            conn.close()

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

    def get_latest_target_batch_id(
        self, target_code: str = "target_5d_return"
    ) -> str | None:
        """查询 target_code 下 is_tradable=True 且 tradable_rows>0 的最新批次
        calc_batch_id，按 created_at DESC LIMIT 1。返回 None 意味着没有批次。

        如果 factor_targets 表不存在也返回 None。
        """
        try:
            if not self._table_exists("factor_targets"):
                return None
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
        self, calc_batch_id: str, target_code: str = "target_5d_return"
    ) -> tuple[Any, str, str]:
        """返回 (DataFrame 含 columns=[symbol, signal_date, target_value],
        calc_batch_id, target_code)。

        DataFrame 后续被评估器 pivot(index=signal_date, columns=symbol,
        values=target_value) 使用。仅返回 is_tradable=True 的行。

        如果 factor_targets 表不存在，返回空 DataFrame。
        """
        import pandas as pd

        empty_df = pd.DataFrame(columns=["symbol", "signal_date", "target_value"])
        try:
            if not self._table_exists("factor_targets"):
                return empty_df, calc_batch_id, target_code
            with self.connection(read_only=True) as conn:
                df = conn.execute(
                    """
                    SELECT symbol, signal_date, target_value
                    FROM factor_targets
                    WHERE calc_batch_id = ?
                      AND target_code = ?
                      AND is_tradable = TRUE
                    """,
                    [calc_batch_id, target_code],
                ).fetchdf()
                return df, calc_batch_id, target_code
        except Exception:
            return empty_df, calc_batch_id, target_code
