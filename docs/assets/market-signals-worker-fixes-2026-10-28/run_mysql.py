from pathlib import Path
import json,os,subprocess,sys
from sqlalchemy.engine import URL
folder=Path(__file__).resolve().parent
root=folder.parents[1]
config=json.loads((root/'config/db_config.json').read_text(encoding='utf-8-sig'))['mysql']
assert config['host'] in ('127.0.0.1','localhost')
url=URL.create('mysql+pymysql',host=config['host'],port=int(config.get('port',3306)),
    username=config['user'],password=config.get('password',''),query={'charset':'utf8mb4'})
env=dict(os.environ,DATABASE_URL='sqlite:///:memory:',APP_VALIDATION_MODE='0',
    AUTO_UNIVERSE_SYNC_ON_STARTUP='0',AUTO_TRADE_CALENDAR_REFRESH='0',PYTHONIOENCODING='utf-8',
    OFFLINE_WORKER_MYSQL_URL=url.render_as_string(hide_password=False),FACTOR_WAREHOUSE_PATH=str(folder/'mysql-unused.duckdb'))
stage=sys.argv[1] if len(sys.argv)>1 else 'mysql'
assert stage in ('mysql','mysql-final')
with (folder/(stage+'.log')).open('w',encoding='utf-8') as log:
    result=subprocess.run([sys.executable,'-m','pytest','tests/test_notification_worker_mysql57.py','-q','-s','--tb=short',
        '-o','junit_family=legacy','--junitxml='+str(folder/(stage+'.xml'))],cwd=folder/'workspace',env=env,stdout=log,stderr=subprocess.STDOUT)
print(json.dumps({'stage':'mysql57','exit_code':result.returncode}))
raise SystemExit(result.returncode)
