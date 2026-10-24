"""Read-only snapshots of protected business tables and all affected histories."""
from pathlib import Path
import hashlib
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding='utf-8')
from sqlalchemy import create_engine, event, inspect, text
from app.core.config import build_mysql_url, load_db_config

root = Path(__file__).parent
destination = root / (sys.argv[1] + '.json')
if destination.exists():
    raise SystemExit('Snapshot already exists; refusing to overwrite evidence')
engine = create_engine(build_mysql_url(load_db_config()), connect_args={'read_timeout':30})
@event.listens_for(engine, 'before_cursor_execute')
def readonly(conn, cursor, statement, parameters, context, executemany):
    if statement.lstrip().split(None, 1)[0].upper() not in {'SELECT','SHOW','DESCRIBE','SET'}:
        raise RuntimeError('Read-only safety capture')

protected = ['portfolios','positions','cash_ledger','sim_orders','sim_trades','sim_accounts',
    'g5_dual_run_reports','portfolio_account_baselines','portfolio_reconciliation_cursors',
    'inspection_evidence_requests','inspection_evidence_decisions','g5_admission_controls','g5_admission_events',
    'factor_model_runs','factor_model_members','factor_weight_snapshots','factor_runtime_state','portfolio_rules',
    'portfolio_factor_usages','strategy_execution_snapshots','portfolio_members','decision_runs',
    'decision_evidence','decision_order_plans','scheduled_tasks']
facts = {'tables':{}, 'market_rows':{}}
with engine.connect() as connection:
    inspector = inspect(connection)
    names = set(inspector.get_table_names())
    for table in protected:
        if table not in names: continue
        pk = inspector.get_pk_constraint(table)['constrained_columns']
        order = ' ORDER BY ' + ','.join('`'+col+'`' for col in pk) if pk else ''
        rows = [dict(row) for row in connection.execute(text('SELECT * FROM `'+table+'`'+order)).mappings()]
        facts['tables'][table] = {'rows':len(rows), 'sha256':hashlib.sha256(
            json.dumps(rows, default=str, sort_keys=True).encode()).hexdigest()}
    facts['main'] = dict(connection.execute(text('SELECT * FROM portfolios WHERE id=1')).mappings().one())
    facts['active_tasks'] = [dict(row) for row in connection.execute(text("SELECT id,task_type,status FROM async_tasks WHERE status IN ('queued','running') ORDER BY id")).mappings()]
    facts['auto_trade_tasks'] = [dict(row) for row in connection.execute(text("SELECT id,task_type,status,created_at,finished_at FROM async_tasks WHERE task_type LIKE '%trade%' OR task_type LIKE '%resume%' OR task_type LIKE '%execution%' ORDER BY id")).mappings()]
    facts['automatic_dispatches'] = [dict(row) for row in connection.execute(text("SELECT r.* FROM scheduled_task_runs r JOIN scheduled_tasks s ON s.id=r.schedule_id WHERE s.task_type='portfolio_auto_trade' ORDER BY r.id")).mappings()]
    facts['audit_max'] = connection.execute(text('SELECT MAX(id) FROM data_governance_audit_events')).scalar()
    for symbol_id in [1,2,3215]:
        facts['market_rows'][str(symbol_id)] = [dict(row) for row in connection.execute(text('SELECT * FROM daily_bars WHERE symbol_id=:id ORDER BY trade_date'), {'id':symbol_id}).mappings()]
    universe = connection.execute(text("SELECT id FROM universe_symbols WHERE symbol='510300'")).scalar()
    facts['universe_etf'] = ([dict(row) for row in connection.execute(text('SELECT * FROM universe_daily_bars WHERE universe_symbol_id=:id ORDER BY trade_date'), {'id':universe}).mappings()] if universe else [])
destination.write_text(json.dumps(facts, default=str, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'snapshot':destination.name,'protected_tables':len(facts['tables']),
    'active_tasks':facts['active_tasks'], 'auto_trade_tasks':len(facts['auto_trade_tasks']),
    'automatic_dispatches':len(facts['automatic_dispatches']), 'audit_max':facts['audit_max'],
    'market_counts':{key:len(rows) for key,rows in facts['market_rows'].items()}},ensure_ascii=False))
engine.dispose()
