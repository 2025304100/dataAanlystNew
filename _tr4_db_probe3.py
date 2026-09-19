import json
from datetime import date
from sqlalchemy import create_engine, text

cfg = json.load(open('config/db_config.json', encoding='utf-8'))
m = cfg['mysql']
url = 'mysql+pymysql://' + m['user'] + ':' + m['password'] + '@' + str(m['host']) + ':' + str(m['port']) + '/' + m['database'] + '?charset=utf8mb4'
engine = create_engine(url)

with engine.connect() as conn:
    # T26 fixture details
    print('=== T26_FIXTURE breakdown ===')
    t26 = conn.execute(text(
        "SELECT symbol_id, MIN(trade_date), MAX(trade_date), COUNT(1), "
        "SUM(is_st), SUM(is_suspended), SUM(is_delisting_period), SUM(CASE WHEN is_listed=0 THEN 1 ELSE 0 END) "
        "FROM security_status_daily WHERE status_source='T26_FIXTURE' GROUP BY symbol_id"
    )).fetchall()
    for r in t26:
        print('  ', r)

    print()
    print('=== T30_FIXTURE breakdown ===')
    t30 = conn.execute(text(
        "SELECT symbol_id, MIN(trade_date), MAX(trade_date), COUNT(1), "
        "SUM(is_st), SUM(is_suspended), SUM(is_delisting_period), SUM(CASE WHEN is_listed=0 THEN 1 ELSE 0 END) "
        "FROM security_status_daily WHERE status_source='T30_FIXTURE' GROUP BY symbol_id"
    )).fetchall()
    for r in t30:
        print('  ', r)

    print()
    # new_listing detection: listing_date vs trade_date within 120 days
    print('=== New listing detection (trade_date - listing_date <= 120) ===')
    nl = conn.execute(text(
        "SELECT symbol_id, COUNT(1), MIN(trade_date), MAX(trade_date) "
        "FROM security_status_daily "
        "WHERE listing_date IS NOT NULL "
        "  AND DATEDIFF(trade_date, listing_date) <= 120 "
        "  AND trade_date >= listing_date "
        "GROUP BY symbol_id"
    )).fetchall()
    for r in nl:
        print('  sid', r[0], 'new_listing_days:', r[1], 'range:', r[2], '~', r[3])

    print()
    print('=== Portfolio id and members (for autofill) ===')
    try:
        pids = conn.execute(text("SELECT id, name FROM portfolios LIMIT 5")).fetchall()
        for p in pids:
            print('  Portfolio:', p)
            pm = conn.execute(text(
                "SELECT COUNT(DISTINCT symbol_id) FROM portfolio_members "
                "WHERE portfolio_id=%s AND status='active' AND effective_to IS NULL"
            ), (p[0],)).scalar()
            pc = conn.execute(text(
                "SELECT COUNT(DISTINCT symbol_id) FROM portfolio_candidates "
                "WHERE portfolio_id=%s AND effective_to IS NULL AND removed_manually_flag=0"
            ), (p[0],)).scalar()
            print(f'    members: {pm}, candidates: {pc}')
    except Exception as ex:
        print('Err:', type(ex).__name__, ex)
