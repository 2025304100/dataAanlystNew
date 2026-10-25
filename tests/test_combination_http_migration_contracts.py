"""Retain legacy schedule evidence while current HTTP writes remain usable."""
import importlib.util
from pathlib import Path
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.scheduled_task import ScheduledTask
from app.schemas.scheduled_task import ScheduledTaskCreate
from app.services.scheduled_tasks import create_schedule

pytestmark=pytest.mark.whitebox

def test_live_schedule_writes_preserve_legacy_names_and_all_old_rows():
    engine=sa.create_engine('sqlite:///:memory:')
    metadata=sa.MetaData()
    table=ScheduledTask.__table__.to_metadata(metadata)
    table.append_column(sa.Column('task_name',sa.String(128),nullable=False,server_default=''))
    table.append_column(sa.Column('cron_expression',sa.String(64),nullable=False,server_default=''))
    sa.Index('uq_legacy_task_name',table.c.task_name,unique=True)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values(name='original',task_type='auto_trade',frequency='daily',
            time_of_day='20:30',enabled=0,task_name='historic-name',cron_expression='30 20 * * 1-5'))
        original=dict(conn.execute(sa.select(table)).mappings().one())
        file=Path(__file__).resolve().parents[1]/'alembic/versions/2026_10_25_0084_http_auto_contracts.py'
        spec=importlib.util.spec_from_file_location('http_auto_migration',file)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade();module.upgrade()
        assert dict(conn.execute(sa.select(table)).mappings().one())==original
    with Session(engine) as db:
        for name in ('new-a','new-b'):
            create_schedule(db,ScheduledTaskCreate(name=name,task_type='portfolio_auto_trade',enabled=False,
                time_of_day='20:30',payload={}))
        db.commit()
        assert db.scalar(sa.select(sa.func.count()).select_from(ScheduledTask))==3
        assert db.execute(sa.text('SELECT task_name,cron_expression FROM scheduled_tasks WHERE id>1')).all()==[(None,None),(None,None)]
        assert dict(db.execute(sa.select(table).where(table.c.id==original['id'])).mappings().one())==original


def test_sqlite_fk_history_refuses_parent_rebuild_before_losing_any_row():
    engine=sa.create_engine('sqlite:///:memory:')
    with engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON');conn.commit()
        conn.exec_driver_sql("CREATE TABLE scheduled_tasks (id INTEGER PRIMARY KEY, task_name VARCHAR(128) NOT NULL DEFAULT '', cron_expression VARCHAR(64) NOT NULL DEFAULT '')")
        conn.exec_driver_sql('CREATE TABLE schedule_history (id INTEGER PRIMARY KEY, schedule_id INTEGER REFERENCES scheduled_tasks(id) ON DELETE CASCADE, evidence TEXT NOT NULL)')
        conn.exec_driver_sql("INSERT INTO scheduled_tasks VALUES (1,'original-name','30 20 * * 1-5')")
        conn.exec_driver_sql("INSERT INTO schedule_history VALUES (7,1,'historical-task-evidence')");conn.commit()
        before={t:conn.exec_driver_sql(f'SELECT * FROM {t} ORDER BY id').all() for t in ('scheduled_tasks','schedule_history')}
        file=Path(__file__).resolve().parents[1]/'alembic/versions/2026_10_25_0084_http_auto_contracts.py'
        spec=importlib.util.spec_from_file_location('http_auto_fk_migration',file)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError,match='preserve referenced schedule history'):module.upgrade()
        assert {t:conn.exec_driver_sql(f'SELECT * FROM {t} ORDER BY id').all() for t in before}==before
        assert sa.inspect(conn).get_columns('scheduled_tasks')[1]['nullable'] is False
