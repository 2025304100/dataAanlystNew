"""Owned candidate-only database principals; never modifies existing accounts.

MySQL 5.7 uses distinct accounts, not unsupported SQL ROLE syntax. SQLite uses
a physically read-only connection plus an authorizer; file ownership stays with
the isolated runner. This is an explicit verification/provisioning boundary.
"""
from contextlib import contextmanager
from uuid import uuid4
import secrets,sqlite3
from sqlalchemy import create_engine,event,inspect
from app.db.governance_migration import _candidate
from app.services.governance_write_boundary import metadata,VIEWS

SYNC_TABLES={'governance_sync_jobs','governance_sync_attempts'}
MUTABLE={'governance_master_control','fact_keys','fact_current','source_policy_heads','source_candidates','recovery_items','legacy_candidates','governance_outbox'}|SYNC_TABLES
APPEND_ONLY=(set(metadata.tables)|{'adjustment_set_inputs'})-MUTABLE-{'governance_schema_migrations','governance_boundary_control'}


def _sqlite_role(engine,role):
    path=engine.url.database
    def connect():
        db=sqlite3.connect(('file:'+path.replace('\\','/')+'?mode=ro') if role!='writer' else path,uri=role!='writer',check_same_thread=False)
        db.execute('PRAGMA foreign_keys=ON')
        def authorize(action,table,column,database,origin):
            if action in {sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE}:
                allowed=role=='writer' and (action==sqlite3.SQLITE_INSERT and table in MUTABLE|APPEND_ONLY or action==sqlite3.SQLITE_UPDATE and (table in MUTABLE or table=='acceptance_history' and column=='system_to' or table=='raw_evidence' and column=='raw_hash'))
                return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
            denied={sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_CREATE_INDEX,sqlite3.SQLITE_CREATE_VIEW,sqlite3.SQLITE_CREATE_TRIGGER,sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_DROP_INDEX,sqlite3.SQLITE_DROP_VIEW,sqlite3.SQLITE_DROP_TRIGGER,sqlite3.SQLITE_ALTER_TABLE,sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH}
            if action in denied:return sqlite3.SQLITE_DENY
            if action==sqlite3.SQLITE_PRAGMA and column is not None and table not in {'table_info','table_xinfo','index_list','index_info','index_xinfo','foreign_key_list'} and not (table=='foreign_keys' and str(column).upper() in {'ON','1'}):return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        return db
    return create_engine(engine.url,creator=connect,hide_parameters=True)


@contextmanager
def candidate_roles(admin):
    _candidate(admin)  # Guard before connecting or creating a principal.
    engines={};accounts=[]
    try:
        if admin.dialect.name=='sqlite':
            engines={role:_sqlite_role(admin,role) for role in ('writer','reader','legacy')}
        else:
            schema=admin.url.database
            for role,suffix in (('writer','w'),('reader','r'),('legacy','l')):
                name='codex_dsg_'+uuid4().hex[:12]+'_'+suffix;password=secrets.token_hex(32)
                with admin.begin() as db:
                    db.exec_driver_sql("CREATE USER %s@'127.0.0.1' IDENTIFIED BY %s",(name,password));accounts.append(name)
                    installed=set(inspect(admin).get_table_names())
                    tables=sorted((set(metadata.tables)|{'adjustment_set_inputs'}|SYNC_TABLES)&installed) if role!='legacy' else []
                    for table in tables:
                        privileges='SELECT'
                        if role=='writer':
                            if table in MUTABLE:privileges+=', INSERT, UPDATE'
                            elif table in APPEND_ONLY:privileges+=', INSERT'
                            if table=='acceptance_history':privileges+=', UPDATE (system_to)'
                            if table=='raw_evidence':privileges+=', UPDATE (raw_hash)'
                        db.exec_driver_sql(f"GRANT {privileges} ON `{schema}`.`{table}` TO %s@'127.0.0.1'",(name,))
                    for view in VIEWS:db.exec_driver_sql(f"GRANT SELECT, SHOW VIEW ON `{schema}`.`{view}` TO %s@'127.0.0.1'",(name,))
                engines[role]=create_engine(admin.url.set(username=name,password=password,host='127.0.0.1'),hide_parameters=True)
        yield engines
    finally:
        for engine in engines.values():engine.dispose()
        if accounts:
            with admin.begin() as db:
                for name in accounts:db.exec_driver_sql("DROP USER %s@'127.0.0.1'",(name,))
