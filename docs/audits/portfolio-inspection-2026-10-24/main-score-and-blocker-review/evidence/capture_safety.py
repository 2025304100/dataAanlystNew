"""Same protected-table capture as prior acceptance, plus existing scores/configs."""
from pathlib import Path
import json
import runpy
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
# Reuse the already reviewed read-only capture without changing its source.
source=ROOT/'tmp/inspection-etf-fallback-2026-10-24/capture_safety.py'
namespace={'__name__':'__main__','__file__':str(source)}
code=source.read_text(encoding='utf-8').replace('root = Path(__file__).parent',
    'root = Path('+repr(str(Path(__file__).parent))+')')
exec(compile(code,str(source),'exec'),namespace)

from sqlalchemy import create_engine,event,text
from app.core.config import build_mysql_url,load_db_config
engine=create_engine(build_mysql_url(load_db_config()))
@event.listens_for(engine,'before_cursor_execute')
def read_only(conn,cursor,statement,parameters,context,executemany):
    if statement.lstrip().split(None,1)[0].upper() not in {'SELECT','SHOW','DESCRIBE','SET'}:
        raise RuntimeError('Safety snapshot is read-only')
with engine.connect() as connection:
    extra={'scores':[dict(row) for row in connection.execute(text('SELECT * FROM scores WHERE symbol_id IN (1,2,3215) ORDER BY id')).mappings()],
        'scoring_configs':[dict(row) for row in connection.execute(text('SELECT * FROM scoring_configs ORDER BY id')).mappings()]}
path=Path(__file__).with_name(sys.argv[1]+'-scores.json')
if path.exists():raise RuntimeError('Snapshot exists')
path.write_text(json.dumps(extra,default=str,ensure_ascii=False,indent=2),encoding='utf-8')
engine.dispose()
