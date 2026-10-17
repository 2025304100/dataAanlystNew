"""Read-only coverage evidence for the factor-library usage view."""
from functools import lru_cache
from time import monotonic

from app.services.factors.store import FactorWarehouse


@lru_cache(maxsize=16)
def _market_window(path: str, _five_second_bucket: int):
    # All rows on a library page share the same denominator. Cache only this
    # market window briefly; per-factor coverage still reads current values.
    with FactorWarehouse(path).connection(read_only=True) as connection:
        return connection.execute("""
            WITH recent AS (
                SELECT DISTINCT trade_date FROM raw_daily_bars
                WHERE adjust = 'qfq' ORDER BY trade_date DESC LIMIT 30
            )
            SELECT MIN(b.trade_date), MAX(b.trade_date), COUNT(DISTINCT b.trade_date), COUNT(*)
            FROM raw_daily_bars b JOIN recent r USING (trade_date)
            WHERE b.adjust = 'qfq'
        """).fetchone()


def read_usage_coverage(warehouse: FactorWarehouse, *, code: str, version: int | None) -> dict:
    result = {"coverage_30d": None, "coverage_window_days": None, "coverage_as_of": None}
    if version is None or not warehouse.health().available:
        return result
    try:
        start, end, days, denominator = _market_window(str(warehouse.path), int(monotonic() // 5))
        if not denominator or start is None:
            return result
        result.update(coverage_window_days=int(days), coverage_as_of=str(end))
        with warehouse.connection(read_only=True) as connection:
            observed, eligible = connection.execute("""
                WITH batches AS (
                    SELECT trade_date, calc_batch_id, MAX(created_at) AS created_at
                    FROM factor_values WHERE factor_code = ? AND factor_version = ?
                        AND trade_date BETWEEN ? AND ?
                    GROUP BY trade_date, calc_batch_id
                ), latest AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY trade_date ORDER BY created_at DESC, calc_batch_id DESC
                    ) AS seq FROM batches
                )
                SELECT COUNT(*), SUM(CASE WHEN f.eligible THEN 1 ELSE 0 END)
                FROM factor_values f
                JOIN latest l ON f.trade_date = l.trade_date AND f.calc_batch_id = l.calc_batch_id AND l.seq = 1
                JOIN raw_daily_bars b ON b.trade_date = f.trade_date AND b.symbol = f.symbol AND b.adjust = 'qfq'
                WHERE f.factor_code = ? AND f.factor_version = ?
            """, [code, version, start, end, code, version]).fetchone()
        # No calculated rows means unknown, rather than a fabricated zero.
        if observed:
            result["coverage_30d"] = round(int(eligible or 0) / int(denominator), 6)
        return result
    except Exception:
        # Missing/busy warehouse is unknown evidence; the library must remain
        # readable and must not misreport a healthy 0%/100% value.
        return result
