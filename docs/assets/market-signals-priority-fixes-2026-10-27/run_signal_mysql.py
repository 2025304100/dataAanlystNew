from pathlib import Path
import json
import os
import subprocess
import sys
from sqlalchemy.engine import URL

folder = Path(__file__).resolve().parent
root = folder.parents[1]
cfg = json.loads((root/'config/db_config.json').read_text(encoding='utf-8-sig'))['mysql']
assert cfg['host'] in ('127.0.0.1', 'localhost')
url = URL.create('mysql+pymysql', host=cfg['host'], port=int(cfg.get('port',3306)), username=cfg['user'],
                 password=cfg.get('password',''), query={'charset':'utf8mb4'})
env = dict(os.environ, MARKET_SIGNAL_PRIORITY_MYSQL57_URL=url.render_as_string(hide_password=False),
    DATABASE_URL='sqlite:///:memory:', APP_VALIDATION_MODE='0', AUTO_UNIVERSE_SYNC_ON_STARTUP='0',
    AUTO_TRADE_CALENDAR_REFRESH='0', FACTOR_WAREHOUSE_PATH=str(folder/'native-unused.duckdb'))
with (folder/'signal-mysql.log').open('w',encoding='utf-8') as log:
    result = subprocess.run([sys.executable,'-m','pytest','tests/test_market_signal_priority_mysql57.py','-q',
        '--tb=short','-o','junit_family=legacy','--junitxml='+str(folder/'signal-mysql.xml')],
        cwd=folder/'workspace',env=env,stdout=log,stderr=subprocess.STDOUT)
print(json.dumps({'stage':'isolated_signal_mysql','exit_code':result.returncode}))
raise SystemExit(result.returncode)
