import json
from sqlalchemy import create_engine, text

cfg = json.load(open('config/db_config.json', encoding='utf-8'))
m = cfg['mysql']
url = 'mysql+pymysql://' + m['user'] + ':' + m['password'] + '@' + str(m['host']) + ':' + str(m['port']) + '/' + m['database'] + '?charset=utf8mb4'
engine = create_engine(url)

with engine.connect() as conn:
    print('=== ALL TABLES (grep calendar/status) ===')
    rows = conn.execute(text("SHOW TABLES")).fetchall()
    tables = [r[0] for r in rows]
    for t in tables:
        lname = t.lower()
        if 'calendar' in lname or 'status' in lname or 'trade' in lname:
            print('  TABLE:', t)
    print('Total tables:', len(tables))

    print()
    print('=== SSD date range ===')
    try:
        mn = conn.execute(text("SELECT MIN(trade_date), MAX(trade_date) FROM security_status_daily")).fetchone()
        print('SSD min/max date:', mn)
        dcount = conn.execute(text("SELECT symbol_id, trade_date, is_st, is_suspended, is_delisting_period, is_listed, listing_date, status_source FROM security_status_daily LIMIT 5")).fetchall()
        for r in dcount:
            print('  Row:', r)
    except Exception as ex:
        print('Err:', type(ex).__name__, ex)

    print()
    print('=== Check is_trading_day or similar in daily_bar or universe ===')
    for tbl in ['universe_daily_bars', 'daily_bars', 'index_prices']:
        try:
            r = conn.execute(text(f"SHOW TABLES LIKE '{tbl}'")).fetchall()
            if r:
                td = conn.execute(text(f"SELECT COUNT(DISTINCT trade_date) FROM {tbl} WHERE trade_date BETWEEN '2021-01-01' AND '2024-01-01'")).scalar()
                print(f'  {tbl}: exists, distinct dates 2021-2024 =', td)
                # use as trading day proxy: any day with data is a trading day
                if td and td < 1500:
                    # sample
                    s = conn.execute(text(f"SELECT DISTINCT trade_date FROM {tbl} WHERE trade_date BETWEEN '2024-01-01' AND '2024-02-01' ORDER BY trade_date LIMIT 20")).fetchall()
                    print('   sample 2024 Jan trading days:', [x[0].isoformat() for x in s])
        except Exception as ex:
            print(f'  {tbl} err:', type(ex).__name__, ex)
