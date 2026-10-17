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
    observed_bar_symbols: int = 0
    denominator_basis: str = 'active_cn_stock_universe_with_unknown_bar_symbols'


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
    "raw_tail_proxy": "trade_date",
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
    # This reader describes the default native factor suite. A newer frozen-
    # set research batch can intentionally contain only one different version;
    # its unrequested factors are not evidence of missing default inputs.
    expected = [(definition.code, definition.version) for definition in FACTOR_DEFINITIONS]
    pairs = ",".join("(?,?)" for _ in expected)
    row = conn.execute(
        f"""
        SELECT calc_batch_id
        FROM factor_values
        WHERE (factor_code, factor_version) IN ({pairs})
        GROUP BY calc_batch_id
        HAVING COUNT(DISTINCT factor_code) = ?
        ORDER BY MAX(created_at) DESC, calc_batch_id DESC
        LIMIT 1
        """,
        [value for pair in expected for value in pair] + [len(expected)],
    ).fetchone()
    return str(row[0]) if row else None


def _factor_coverage(
    conn, *, calc_batch_id: str, adjust: str
) -> list[FactorCoverage]:
    result = []
    denominator_cache = {}
    for definition in FACTOR_DEFINITIONS:
        latest_row = conn.execute(
            """
            SELECT MAX(trade_date)
            FROM factor_values
            WHERE calc_batch_id = ? AND factor_code = ? AND factor_version = ?
            """,
            [calc_batch_id, definition.code, definition.version],
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
        observed = conn.execute(
            """
            SELECT COUNT(DISTINCT symbol)
            FROM raw_daily_bars
            WHERE trade_date = ? AND adjust = ?
            """,
            [latest, adjust],
        ).fetchone()[0]
        # Partial-date imports must not shrink the market denominator. Use the
        # active CN stock directory, retaining unknown historical bar symbols
        # conservatively when their metadata is absent. A missing directory
        # falls back to all observed symbols through the calculation date.
        if latest not in denominator_cache:
            denominator_cache[latest] = conn.execute('''
            SELECT COUNT(*) FROM (
                SELECT symbol FROM raw_asset_universe
                WHERE asset_type='stock' AND region='cn' AND is_active
                UNION
                SELECT DISTINCT b.symbol FROM raw_daily_bars b
                WHERE b.adjust=? AND b.trade_date<=?
                  AND NOT EXISTS (SELECT 1 FROM raw_asset_universe u WHERE u.symbol=b.symbol)
            ) expected
            ''', [adjust, latest]).fetchone()[0]
        universe = denominator_cache[latest]
        eligible, imputed = conn.execute(
            """
            SELECT
                COUNT(DISTINCT CASE WHEN eligible THEN symbol END),
                COUNT(DISTINCT CASE WHEN is_imputed THEN symbol END)
            FROM factor_values
            WHERE calc_batch_id = ?
              AND factor_code = ?
              AND factor_version = ?
              AND trade_date = ?
            """,
            [calc_batch_id, definition.code, definition.version, latest],
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
                observed_bar_symbols=int(observed or 0),
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
        definition = next(
            item
            for item in FACTOR_DEFINITIONS
            if item.code == factor.factor_code
        )
        if not definition.health_required:
            continue
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
