-- gpfx_validation，只读核对看板口径，不修复任何业务数据。
USE gpfx_validation;
SET TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT;

-- 行情存在、同步标记、上市日期是独立指标。
-- 此处上市日期非空数仅表示已登记，不表示来源已核验。
SELECT u.region,u.asset_type,COUNT(*) AS registered,
       SUM(u.is_synced=1) AS sync_flags,
       SUM(COALESCE(b.n,0)>0) AS with_bars,
       SUM(COALESCE(b.n,0)=0) AS without_bars,
       CASE WHEN SUM(COALESCE(b.n,0)>0)=COUNT(*) THEN 100
            ELSE LEAST(99.99,ROUND(100.0*SUM(COALESCE(b.n,0)>0)/COUNT(*),2))
       END AS record_coverage_pct,
       SUM(u.listed_at IS NOT NULL) AS listing_date_recorded,
       SUM(u.listed_at IS NULL) AS listing_date_missing,
       MAX(b.latest) AS latest_record_date
FROM universe_symbols u
LEFT JOIN (
    SELECT universe_symbol_id,COUNT(*) AS n,MAX(trade_date) AS latest
    FROM universe_daily_bars GROUP BY universe_symbol_id
) b ON b.universe_symbol_id=u.id
GROUP BY u.region,u.asset_type
ORDER BY u.region,u.asset_type;

-- 应为 SLY、301583 两行；不能仅用 is_synced=1 判断已有行情。
SELECT u.id,u.symbol,u.name,u.region,u.asset_type,u.is_synced,u.bar_count,u.last_bar_date
FROM universe_symbols u
WHERE NOT EXISTS (SELECT 1 FROM universe_daily_bars b WHERE b.universe_symbol_id=u.id)
ORDER BY u.id;

-- 000004 原登记缓存只对账，不自动覆盖。
SELECT u.id,u.symbol,u.bar_count AS cached_count,COUNT(b.id) AS actual_count,
       u.last_bar_date AS cached_latest,MAX(b.trade_date) AS actual_latest
FROM universe_symbols u LEFT JOIN universe_daily_bars b ON b.universe_symbol_id=u.id
WHERE u.symbol='000004'
GROUP BY u.id,u.symbol,u.bar_count,u.last_bar_date;

SELECT COUNT(*) AS actual_rows,COUNT(*)=11734896 AS row_count_matches
FROM universe_daily_bars;
SELECT COUNT(*)=1 AND MIN(version_num)='wps_0024_082_status_dt6' AS revision_matches
FROM alembic_version;
SELECT COUNT(*)=0 AS idle_matches FROM async_tasks WHERE status IN ('running','queued');
ROLLBACK;
