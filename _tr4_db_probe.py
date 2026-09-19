import json
from datetime import date
from sqlalchemy import create_engine, text

cfg = json.load(open('config/db_config.json', encoding='utf-8'))
if cfg.get('use_mysql'):
    m = cfg['mysql']
    url = 'mysql+pymysql://' + m['user'] + ':' + m['password'] + '@' + str(m['host']) + ':' + str(m['port']) + '/' + m['database'] + '?charset=utf8mb4'
else:
    url = 'sqlite:///app.db'

print('URL (masked):', url.split('@')[-1] if '@' in url else url)
engine = create_engine(url)

with engine.connect() as conn:
    print('=== trade_calendar table ===')
    try:
        rows = conn.execute(text("SHOW TABLES LIKE 'trade_calendar'")).fetchall()
        print('Exists:', bool(rows))
        if rows:
            cols = conn.execute(text('DESCRIBE trade_calendar')).fetchall()
            for c in cols:
                print('  col:', c)
            cnt_q = "SELECT COUNT(1) FROM trade_calendar"
            cnt = conn.execute(text(cnt_q)).scalar()
            print('Total rows:', cnt)
            tcnt_q = "SELECT COUNT(1) FROM trade_calendar WHERE is_trading_day=1 AND date BETWEEN '2021-01-01' AND '2024-01-01'"
            tcnt = conn.execute(text(tcnt_q)).scalar()
            print('Trade days 2021-2024:', tcnt)
    except Exception as ex:
        print('Error:', type(ex).__name__, ex)

    print()
    print('=== security_status_daily ===')
    try:
        ssd_ex = conn.execute(text("SHOW TABLES LIKE 'security_status_daily'")).fetchall()
        print('Exists:', bool(ssd_ex))
        if ssd_ex:
            c1 = "SELECT COUNT(1) FROM security_status_daily"
            cnt = conn.execute(text(c1)).scalar()
            print('Total rows:', cnt)
            c2 = "SELECT COUNT(DISTINCT trade_date) FROM security_status_daily"
            dcnt = conn.execute(text(c2)).scalar()
            print('Distinct dates:', dcnt)
            c3 = "SELECT COUNT(DISTINCT trade_date) FROM security_status_daily WHERE trade_date BETWEEN '2021-01-01' AND '2024-01-01'"
            cv = conn.execute(text(c3)).scalar()
            print('SSD distinct dates 2021-2024:', cv)
            c4 = "SELECT status_source, COUNT(1) FROM security_status_daily GROUP BY status_source"
            src = conn.execute(text(c4)).fetchall()
            for s in src:
                print('  src:', s)
            c_st = "SELECT COUNT(1) FROM security_status_daily WHERE is_st=1 AND trade_date BETWEEN '2021-01-01' AND '2024-01-01'"
            st = conn.execute(text(c_st)).scalar()
            c_su = "SELECT COUNT(1) FROM security_status_daily WHERE is_suspended=1 AND trade_date BETWEEN '2021-01-01' AND '2024-01-01'"
            sus = conn.execute(text(c_su)).scalar()
            c_de = "SELECT COUNT(1) FROM security_status_daily WHERE is_delisting_period=1 AND trade_date BETWEEN '2021-01-01' AND '2024-01-01'"
            de = conn.execute(text(c_de)).scalar()
            c_un = "SELECT COUNT(1) FROM security_status_daily WHERE is_listed=0 AND trade_date BETWEEN '2021-01-01' AND '2024-01-01'"
            un = conn.execute(text(c_un)).scalar()
            print('ST=', st, 'Suspended=', sus, 'DelistingPeriod=', de, 'Unlisted=', un)
            c_sy = "SELECT DISTINCT symbol_id FROM security_status_daily LIMIT 10"
            syms = conn.execute(text(c_sy)).fetchall()
            print('Sample sids:', [x[0] for x in syms])
    except Exception as ex:
        print('Error:', type(ex).__name__, ex)

    print()
    s, e = date(2021,1,1), date(2024,1,1)
    nat = (e-s).days + 1
    print('Natural days:', nat, '  5/7 estimate:', int(nat * 5 / 7))
print('DONE')
