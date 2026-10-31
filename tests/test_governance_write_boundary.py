"""Public authority/projection/permission seams, on disposable databases."""
from copy import deepcopy
from datetime import date,datetime
import pytest
from tests.test_governance_source_state import governed,system,candidate,annual_page
pytestmark=pytest.mark.whitebox


def test_checked_in_writer_inventory_matches_repository_through_public_cli(tmp_path):
    import json, subprocess, sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    output=tmp_path/'writer-audit.json'
    completed=subprocess.run([sys.executable,str(root/'scripts/check_governance_writers.py'),
                              '--output',str(output)],cwd=root,capture_output=True,text=True)
    assert completed.returncode==0,completed.stdout+completed.stderr
    report=json.loads(output.read_text(encoding='utf-8'))
    assert report['status']=='pass' and report['entry_count']>0 and report['violations']==[]


def test_both_compatibility_projections_use_one_approved_version_and_origin(governed):
    from app.services.governance_write_boundary import install_boundary,MarketWriteBoundary,CANONICAL_ORIGIN
    service,request,clock,_,_,policy,_=governed
    install_boundary(service.engine,instrument_version=request['instrument_version'])
    boundary=MarketWriteBoundary(service)
    result=boundary.submit(request,origin=CANONICAL_ORIGIN,policy_version=policy,expected_revision=0)
    left=boundary.projection('daily_bars');right=boundary.projection('universe_daily_bars')
    assert left['rows']==right['rows'] and left['rows'][0]['data_version']==result['data_version']
    assert left['rows'][0]['raw_close']=='1.05000000' and left['rows'][0]['volume_shares']=='1000.00000000'
    assert left['rows'][0]['source_origin']=='universe.fact_versions'
    assert left['rows'][0]['amount_cny'] is None


def test_legacy_and_reader_roles_cannot_write_and_writer_cannot_mutate_facts(governed,record_property):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from app.services.governance_write_boundary import install_boundary,MarketWriteBoundary,CANONICAL_ORIGIN
    from app.services.governance_database_roles import candidate_roles
    from app.services.governance_writer import FactWriter
    from app.services.governance_source_state import SourceStateService
    service,request,clock,_,_,policy,_=governed
    install_boundary(service.engine,instrument_version=request['instrument_version'])
    with candidate_roles(service.engine) as roles:
        if service.engine.dialect.name=='mysql':
            for role,engine in roles.items():
                with engine.connect() as db:identity=db.scalar(text('SELECT CURRENT_USER()'))
                assert identity.startswith('codex_dsg_')
                record_property('mysql_'+role+'_principal',identity)
        writer=FactWriter(roles['writer'],contracts=list(service.writer.contracts.values()),clock=clock)
        boundary=MarketWriteBoundary(SourceStateService(writer),reader_engine=roles['reader'])
        result=boundary.submit(request,origin=CANONICAL_ORIGIN,policy_version=policy,expected_revision=0)
        assert boundary.projection('daily_bars')['rows'][0]['data_version']==result['data_version']
        for role in ('reader','legacy'):
            with pytest.raises(DBAPIError):
                with roles[role].begin() as db:db.execute(text('DELETE FROM fact_current'))
            with roles[role].connect() as db:
                assert db.scalar(text('SELECT COUNT(*) FROM governance_daily_bars_projection'))==1
        for sql in ("UPDATE fact_versions SET payload_json='{}'",'DELETE FROM fact_versions','DROP TABLE fact_versions','UPDATE governance_boundary_control SET state=\'building\''):
            with pytest.raises(DBAPIError):
                with roles['writer'].begin() as db:db.execute(text(sql))
        assert boundary.projection('daily_bars')['rows'][0]['raw_close']=='1.05000000'


def test_legacy_business_mirror_cannot_overwrite_universe_or_advance_business_watermark(db_session,tmp_path):
    from app.models.symbol import Symbol
    from app.models.daily_bar import DailyBar
    from app.models.universe import UniverseSymbol,UniverseDailyBar
    from app.services.factors.bar_mirror import mirror_daily_bars
    from app.services.factors.store import FactorWarehouse
    business=Symbol(symbol='600001',name='synthetic fixture',asset_type='stock',market='sh')
    universe=UniverseSymbol(symbol='600001',asset_type='stock',market='sh',region='cn')
    db_session.add_all([business,universe]);db_session.flush()
    values=dict(trade_date=date(2026,6,1),open=1,high=2,low=1,volume=100,amount=None,source='sina_raw')
    db_session.add_all([DailyBar(symbol_id=business.id,close=1.8,**values),UniverseDailyBar(universe_symbol_id=universe.id,close=1.05,**values)])
    db_session.commit();warehouse=FactorWarehouse(tmp_path/'legacy.duckdb')
    result=mirror_daily_bars(db_session,warehouse=warehouse,adjust='',include_business=True,include_universe=True)
    assert result.business_rows==0 and result.universe_rows==1
    with warehouse.connection(read_only=True) as db:
        assert db.execute('SELECT close,source_origin FROM raw_daily_bars').fetchone()==(1.05,'universe_daily_bars')
    assert warehouse.get_watermark('sql.daily_bars')==0
    assert result.to_dict()['business_mirror_retired'] is True


def test_default_legacy_warehouse_denies_non_universe_bar_writes_before_checkpoint():
    from app.services.factors.store import FactorWarehouse
    warehouse=FactorWarehouse()
    with pytest.raises(ValueError,match='NON_CANONICAL_LEGACY_MIRROR'):
        warehouse.upsert_daily_bars([{'symbol':'600001','trade_date':date(2026,6,1),'adjust':'','open':1,'high':2,'low':1,'close':1.8,'source':'synthetic','source_origin':'daily_bars','source_row_id':1,'source_updated_at':datetime(2026,6,2),'ingested_at':datetime(2026,6,2),'batch_id':'synthetic'}],source_key='forged-source-key',watermark=99)
    assert warehouse.get_watermark('forged-source-key')==0


def test_new_or_changed_writer_is_rejected_even_if_it_claims_a_canonical_name(tmp_path):
    from app.services.governance_writer_audit import inventory_writes,audit_writes
    root=tmp_path/'workspace';(root/'app').mkdir(parents=True)
    source=root/'app/old.py';source.write_text("def read(db):\n    return db.execute('SELECT 1')\n",encoding='utf-8')
    baseline=inventory_writes(root)
    assert audit_writes(root,baseline)['status']=='pass'
    (root/'app/canonical_writer.py').write_text("def write(db):\n    db.execute(\"INSERT INTO fact_versions (data_version) VALUES ('forged')\")\n",encoding='utf-8')
    report=audit_writes(root,baseline)
    assert report['status']=='fail' and any(v['reason']=='UNREGISTERED_ENTRY' for v in report['violations'])
    source.write_text("def read(db):\n    return db.execute('DELETE FROM fact_current')\n",encoding='utf-8')
    assert any(v['reason']=='CHANGED_ENTRY' for v in audit_writes(root,baseline)['violations'])


@pytest.mark.parametrize('origin',['daily_bars','universe_daily_bars','raw_daily_bars','unknown'])
def test_non_authoritative_origins_cannot_create_a_fact(governed,origin):
    from app.services.governance_write_boundary import install_boundary,MarketWriteBoundary
    from app.services.governance_writer import WriterError
    service,request,_,_,_,policy,_=governed
    install_boundary(service.engine,instrument_version=request['instrument_version']);boundary=MarketWriteBoundary(service)
    with pytest.raises(WriterError,match='NON_CANONICAL_ORIGIN'):
        boundary.submit(request,origin=origin,policy_version=policy,expected_revision=0)
    assert boundary.projection('daily_bars')['rows']==[]


def test_projection_aliases_use_reviewed_namespaces_and_report_unmapped_ids(governed):
    from app.services.governance_master_data import MasterDataService
    from tests.test_governance_master_data import publish,member,code,alias
    from app.services.governance_write_boundary import install_boundary,MarketWriteBoundary,CANONICAL_ORIGIN
    service,request,_,_,_,policy,_=governed;identity=request['key']['instrument_id']
    master=MasterDataService(service.engine)
    request['instrument_version']=publish(master,[member(identity)],[code(identity,'600001')],[alias(identity,'symbols',71,'600001'),alias(identity,'universe_symbols',813,'600001')],expected_head=request['instrument_version'])
    install_boundary(service.engine,instrument_version=request['instrument_version']);boundary=MarketWriteBoundary(service)
    boundary.submit(request,origin=CANONICAL_ORIGIN,policy_version=policy,expected_revision=0)
    left=boundary.projection('daily_bars');right=boundary.projection('universe_daily_bars')
    assert left['rows']==right['rows']
    assert left['aliases'][0]['legacy_id']==71 and right['aliases'][0]['legacy_id']==813
    assert not left['missing_aliases'] and not right['missing_aliases']


def test_projection_rejects_a_different_master_pin_before_writing(governed):
    from app.services.governance_master_data import MasterDataService
    from tests.test_governance_master_data import publish,member,code
    from app.services.governance_write_boundary import install_boundary,MarketWriteBoundary,CANONICAL_ORIGIN
    from app.services.governance_writer import WriterError
    service,request,_,_,_,policy,_=governed
    install_boundary(service.engine,instrument_version=request['instrument_version']);boundary=MarketWriteBoundary(service)
    master=MasterDataService(service.engine);identity=request['key']['instrument_id']
    request['instrument_version']=publish(master,[member(identity)],[code(identity,'600001')],[],expected_head=request['instrument_version'],evidence=b'new proof for same identity')
    with pytest.raises(WriterError,match='BOUNDARY_PIN_MISMATCH'):
        boundary.submit(request,origin=CANONICAL_ORIGIN,policy_version=policy,expected_revision=0)
    assert not boundary.projection('daily_bars')['rows']


def test_boundary_installation_resumes_without_replacing_legacy_tables_and_views_detect_drift(governed):
    from sqlalchemy import event,inspect
    from app.services.governance_write_boundary import install_boundary,MarketWriteBoundary
    from app.services.governance_writer import WriterError
    service,request,_,_,_,_,_=governed
    def stop(db,cursor,statement,params,context,many):
        if statement.startswith('CREATE VIEW governance_daily_bars_projection') or statement.startswith('CREATE SQL SECURITY DEFINER VIEW governance_daily_bars_projection'):raise RuntimeError('SYNTHETIC interrupted view install')
    event.listen(service.engine,'before_cursor_execute',stop)
    try:
        with pytest.raises(RuntimeError,match='interrupted'):install_boundary(service.engine,instrument_version=request['instrument_version'])
    finally:event.remove(service.engine,'before_cursor_execute',stop)
    with pytest.raises(WriterError,match='BOUNDARY_NOT_READY'):MarketWriteBoundary(service)
    installed=install_boundary(service.engine,instrument_version=request['instrument_version'])
    assert install_boundary(service.engine,instrument_version=request['instrument_version'])==installed
    assert 'daily_bars' not in inspect(service.engine).get_table_names()
    with service.engine.begin() as db:
        db.exec_driver_sql('DROP VIEW governance_daily_bars_projection')
        db.exec_driver_sql('CREATE VIEW governance_daily_bars_projection AS SELECT 1 AS forged')
    with pytest.raises(WriterError,match='BOUNDARY_VIEW_DRIFT'):MarketWriteBoundary(service)


def test_both_projections_keep_backup_state_and_report_withdrawn_current_without_fallback(governed):
    from app.services.governance_write_boundary import install_boundary,MarketWriteBoundary,CANONICAL_ORIGIN
    from tests.test_governance_source_state import degrade
    service,request,clock,_,backup,policy,_=governed
    install_boundary(service.engine,instrument_version=request['instrument_version']);boundary=MarketWriteBoundary(service)
    old=degrade(service,request,backup,policy)
    assert boundary.projection('daily_bars')['rows'][0]['source_state']=='backup_temp'
    clock.advance();new=boundary.submit(request,origin=CANONICAL_ORIGIN,policy_version=policy,expected_revision=1)
    assert new['accepted'] and boundary.projection('universe_daily_bars')['rows'][0]['source_state']=='canonical'
    clock.advance();request['batch_id']='withdrawal'
    request['revision'].update(kind='withdrawal',token='r9',order=9,order_evidence=b'SYNTHETIC revision 9',supersedes_version=new['data_version'])
    request['wire_record']={'retraction_reason':'withdrawn'}
    boundary.submit(request,origin=CANONICAL_ORIGIN,policy_version=policy,expected_revision=2)
    left=boundary.projection('daily_bars');right=boundary.projection('universe_daily_bars')
    assert left['rows']==right['rows']==[] and left['missing']==right['missing']
    assert left['missing'][0]['reason']=='RETRACTED'
    assert service.writer.fact(old['data_version'])['payload']['raw_close']=='1.05000000'


@pytest.mark.parametrize('kind',['business','remote'])
def test_role_provisioning_rejects_non_candidate_before_connect(tmp_path,kind):
    from sqlalchemy import create_engine,event
    from app.services.governance_database_roles import candidate_roles
    from app.db.governance_migration import MigrationError
    url='sqlite:///'+(tmp_path/'business.sqlite').as_posix() if kind=='business' else 'mysql+pymysql://placeholder@192.0.2.1/codex_dsg_test'
    engine=create_engine(url)
    def forbid(*args):raise AssertionError('scope check must happen before connecting')
    event.listen(engine,'connect',forbid)
    try:
        with pytest.raises(MigrationError,match='CANDIDATE_SCOPE_REQUIRED'):
            with candidate_roles(engine):raise AssertionError('must not yield')
    finally:engine.dispose()
