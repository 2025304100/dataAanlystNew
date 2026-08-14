import json, sys
from datetime import datetime, timedelta
sys.path.insert(0, r'd:\ai_project\dataAanlystNew')
from app.core.config import load_db_config, build_mysql_url, settings
from app.db.manager import DatabaseManager
from app.db.init_db import init_db

cfg = load_db_config()
mgr = DatabaseManager.get()
if cfg.get("use_mysql") and cfg.get("mysql", {}).get("host"):
    url = build_mysql_url(cfg)
    print('USE_MYSQL url=', url)
    mgr.initialize(url, db_type="mysql")
else:
    print('USE_SQLITE url=', settings.database_url)
    mgr.initialize(settings.database_url, db_type="sqlite")

from app.db.session import SessionLocal
from app.models.async_task import AsyncTaskRecord

db = SessionLocal()
cutoff = datetime.now() - timedelta(days=3)
rows = db.query(AsyncTaskRecord).filter(
    AsyncTaskRecord.created_at >= cutoff
).order_by(AsyncTaskRecord.created_at.desc()).limit(15).all()
print('TOTAL ROWS:', len(rows))

def safe_loads(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception as e:
        return 'JSON_ERR:' + str(e) + ' |RAW_FIRST_200| ' + s[:200]

for r in rows:
    payload = safe_loads(r.payload_json)
    errors = safe_loads(r.errors_json)
    result = safe_loads(r.result_json)
    factor_code = 'N/A'
    if isinstance(payload, dict):
        factor_code = (payload.get('factor_code')
                       or payload.get('factorCode')
                       or payload.get('factor')
                       or payload.get('factor_version_id')
                       or 'N/A')
    print()
    print('=' * 100)
    print('TASK_ID      :', r.id)
    print('TASK_TYPE    :', r.task_type)
    print('FACTOR       :', factor_code)
    print('STATUS       :', r.status)
    print('STAGE        :', r.stage)
    print('PERCENT      :', str(r.percent) + '%')
    print('MESSAGE      :', (r.message or '')[:250])
    print('CURRENT_ITEM :', r.current_item)
    sug = r.suggested_action or ''
    print('SUGGESTED_ACT:', sug[:200])
    print('CREATED_AT   :', r.created_at)
    print('FINISHED_AT  :', r.finished_at)
    print('WORKER_TID   :', r.worker_thread_id, ' | CANCEL:', r.cancel_requested)
    print()
    print('>>> ERRORS_JSON:')
    if isinstance(errors, list):
        for i, e in enumerate(errors):
            if isinstance(e, dict):
                code = e.get('code')
                sev = e.get('severity')
                cat = e.get('category')
                ev = e.get('evidence') or {}
                corr_id = ev.get('correlation_id') if isinstance(ev, dict) else None
                tzh = e.get('title_zh') or e.get('title')
                dzh = e.get('detail_zh') or e.get('detail') or ''
                fl = e.get('fix_link') or None
                print('  [' + str(i) + '] code=' + repr(code) + ' | sev=' + repr(sev) + ' | cat=' + repr(cat) + ' | corr_id=' + str(corr_id))
                print('      title  :', tzh)
                print('      detail :', str(dzh)[:320])
                if isinstance(fl, dict):
                    print('      fix    :', repr(fl))
            else:
                print('  [' + str(i) + '] NON_DICT:', str(e)[:200])
    elif isinstance(errors, dict):
        print('  OBJECT keys=', list(errors.keys())[:12])
        print('  PREVIEW:', str(errors)[:800])
    elif errors is None:
        print('  (EMPTY NULL)')
    else:
        print('  OTHER:', str(errors)[:400])
    print()
    print('>>> RESULT_JSON (keys preview):')
    if isinstance(result, dict):
        for k in list(result.keys())[:15]:
            v = result[k]
            if isinstance(v, (dict, list)):
                print('  ' + str(k) + ': ' + type(v).__name__ + ' len=' + str(len(v)))
            else:
                s = str(v)
                print('  ' + str(k) + ': ' + (s[:160] + ('...' if len(s) > 160 else '')))
    elif isinstance(result, list):
        print('  LIST len=', len(result))
    elif result is None:
        print('  (EMPTY NULL)')
    else:
        print('  OTHER:', str(result)[:400])

db.close()
