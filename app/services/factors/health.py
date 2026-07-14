"""Coverage and freshness diagnostics for the local factor warehouse."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.services.factors.definitions import FACTOR_DEFINITIONS
from app.services.factors.store import FactorWarehouse


@dataclass(frozen=True)
class RawTableHealth:
    table: str
    row_count: int
    latest_date: str | None


@dataclass(frozen=True)
class FactorCoverage:
    factor_code: str
    latest_trade_date: str | None
    universe_symbols: int
    eligible_symbols: int
    imputed_symbols: int
    coverage: float


@dataclass
class FactorHealthReport:
    status: str
    warehouse_available: bool
    warehouse_path: str
    schema_version: str | None = None
    calc_batch_id: str | None = None
    latest_bar_date: str | None = None
    raw_tables: list[RawTableHealth] = field(default_factory=list)
    factors: list[FactorCoverage] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_RAW_TABLE_DATES = {
    "raw_daily_bars": "trade_date",
    "raw_valuation_snapshots": "trade_date",
    "raw_financial_reports": "announcement_date",
    "raw_fund_flows": "trade_date",
    "raw_sentiment": "trade_date",
    "raw_macro": "period",
    "factor_values": "trade_date",
}


def _raw_table_health(conn) -> list[RawTableHealth]:
    result = []
    for table, date_column in _RAW_TABLE_DATES.items():
        count, latest = conn.execute(
            f"SELECT COUNT(*), MAX({date_column}) FROM {table}"
        ).fetchone()
        result.append(
            RawTableHealth(
                table=table,
                row_count=int(count or 0),
                latest_date=str(latest) if latest is not None else None,
            )
        )
    return result


def _latest_batch(conn) -> str | None:
    row = conn.execute(
        """
        SELECT calc_batch_id
        FROM factor_values
        GROUP BY calc_batch_id
        ORDER BY MAX(created_at) DESC, calc_batch_id DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row[0]) if row else None


def _factor_coverage(
    conn, *, calc_batch_id: str, adjust: str
) -> list[FactorCoverage]:
    result = []
    for definition in FACTOR_DEFINITIONS:
        latest_row = conn.execute(
            """
            SELECT MAX(trade_date)
            FROM factor_values
            WHERE calc_batch_id = ? AND factor_code = ?
            """,
            [calc_batch_id, definition.code],
        ).fetchone()
        latest = latest_row[0] if latest_row else None
        if latest is None:
            result.append(
                FactorCoverage(
                    factor_code=definition.code,
                    latest_trade_date=None,
                    universe_symbols=0,
                    eligible_symbols=0,
                    imputed_symbols=0,
                    coverage=0.0,
                )
            )
            continue
        universe = conn.execute(
            """
            SELECT COUNT(DISTINCT symbol)
            FROM raw_daily_bars
            WHERE trade_date = ? AND adjust = ?
            """,
            [latest, adjust],
        ).fetchone()[0]
        eligible, imputed = conn.execute(
            """
            SELECT
                COUNT(DISTINCT CASE WHEN eligible THEN symbol END),
                COUNT(DISTINCT CASE WHEN is_imputed THEN symbol END)
            FROM factor_values
            WHERE calc_batch_id = ?
              AND factor_code = ?
              AND trade_date = ?
            """,
            [calc_batch_id, definition.code, latest],
        ).fetchone()
        universe_count = int(universe or 0)
        eligible_count = int(eligible or 0)
        coverage = (
            eligible_count / universe_count if universe_count else 0.0
        )
        result.append(
            FactorCoverage(
                factor_code=definition.code,
                latest_trade_date=str(latest),
                universe_symbols=universe_count,
                eligible_symbols=eligible_count,
                imputed_symbols=int(imputed or 0),
                coverage=round(coverage, 6),
            )
        )
    return result


def get_factor_health(
    warehouse: FactorWarehouse,
    *,
    calc_batch_id: str | None = None,
    adjust: str = "qfq",
    minimum_coverage: float = 0.7,
    healthy_coverage: float = 0.9,
) -> FactorHealthReport:
    if not 0 <= minimum_coverage <= healthy_coverage <= 1:
        raise ValueError(
            "coverage thresholds must satisfy "
            "0 <= minimum_coverage <= healthy_coverage <= 1"
        )
    warehouse_health = warehouse.health()
    report = FactorHealthReport(
        status="failed",
        warehouse_available=warehouse_health.available,
        warehouse_path=warehouse_health.path,
        schema_version=warehouse_health.schema_version,
    )
    if not warehouse_health.available:
        report.reasons.append(
            warehouse_health.error or "warehouse_unavailable"
        )
        return report

    with warehouse.connection(read_only=True) as conn:
        report.raw_tables = _raw_table_health(conn)
        table_map = {item.table: item for item in report.raw_tables}
        report.latest_bar_date = table_map["raw_daily_bars"].latest_date
        report.calc_batch_id = calc_batch_id or _latest_batch(conn)
        if report.calc_batch_id is None:
            report.status = "degraded"
            report.reasons.append("factor_batch_missing")
            return report
        report.factors = _factor_coverage(
            conn, calc_batch_id=report.calc_batch_id, adjust=adjust
        )

    degraded = []
    warnings = []
    for factor in report.factors:
        if factor.latest_trade_date is None:
            degraded.append(f"{factor.factor_code}:missing")
        elif factor.coverage < minimum_coverage:
            degraded.append(
                f"{factor.factor_code}:coverage={factor.coverage:.3f}"
            )
        elif factor.coverage < healthy_coverage:
            warnings.append(
                f"{factor.factor_code}:coverage={factor.coverage:.3f}"
            )
        if (
            report.latest_bar_date is not None
            and factor.latest_trade_date is not None
            and factor.latest_trade_date < report.latest_bar_date
        ):
            degraded.append(f"{factor.factor_code}:stale")
    if degraded:
        report.status = "degraded"
        report.reasons.extend(degraded)
    elif warnings:
        report.status = "warn"
        report.reasons.extend(warnings)
    else:
        report.status = "healthy"
    return report
