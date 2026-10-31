"""M06.4: candidate accepted-history membership, closed base, and calendar windows."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta

import pytest

from app.services.governance_fact_contracts import canonical, digest, WriterError
from app.services.governance_price_asof import AsOfPriceService
from tests.data_governance.test_event_ingestion import events
from tests.test_governance_writer import system, candidate, annual_page, approval, capture_request

pytestmark=pytest.mark.whitebox


@pytest.fixture
def history(events, system):
    from app.services.governance_writer import FactWriter, WriterContract
    from app.services.governance_master_data import MasterDataService
    from app.services.governance_prices import install_price_schema

    event_writer, contracts, action, _, _, clock = events
    event = event_writer.write(action,approval(event_writer,action))['data_version']
    old, template, _, east_contract = system
    sina_contract = WriterContract.daily_adapter(provider='sina',endpoint='stock_zh_a_daily',asset_type='stock',
        instrument_family='sh_main',definition={'name':'raw-price-CNY/share','version':1},
        evidence=b'SYNTHETIC isolated Sina raw/unit contract',publication_timezone='Asia/Shanghai')
    writer = FactWriter(old.engine,contracts=[east_contract,sina_contract],clock=clock)
    first=deepcopy(template)
    first['wire_record'].update({'开盘':'10','最高':'10.2','最低':'9.8','收盘':'10','成交量':'1','成交额':'1000'})
    first['gate_results']['P02']='pass'
    first['time'].update(publication_precision='exact',published_at='2026-06-02T16:00:00+08:00',
                         publication_evidence=b'SYNTHETIC raw public evidence at 16:00')
    raw1=writer.write(first,approval(writer,first))['data_version']
    second=deepcopy(first)
    second.update(key={'instrument_id':first['key']['instrument_id'],'trade_date':'2026-06-02'},
        contract_version=sina_contract.version,source_uri='fixture://sina/raw',
        raw_response=b'SYNTHETIC Sina June 2 raw bytes',batch_id='sina-day-two',
        wire_record={'open':'5','high':'5.1','low':'4.9','close':'5','volume':'100','amount':'500','turnover':'0.1'})
    second['time'].update(event_date='2026-06-02',valid_from='2026-06-02',valid_to='2026-06-03')
    capture_request(second)
    raw2=writer.write(second,approval(writer,second))['data_version']
    overlap=deepcopy(first)
    overlap.update(key=second['key'],raw_response=b'SYNTHETIC Eastmoney overlap raw bytes',batch_id='east-overlap')
    overlap['wire_record'].update({'开盘':'5','最高':'5.1','最低':'4.9','收盘':'5','成交量':'1','成交额':'500'})
    overlap['time'].update(event_date='2026-06-02',valid_from='2026-06-02',valid_to='2026-06-03')
    capture_request(overlap)
    overlap_version=writer.write(overlap)['data_version']  # Staged comparison candidate, never selected current.
    install_price_schema(writer.engine)
    calendar=MasterDataService(writer.engine)
    first_day=calendar.calendar_snapshot(first['calendar_version'])['start']
    days=calendar.trading_days(first['calendar_version'],'SSE',first_day,'2026-06-03')
    closes={d.isoformat():d.isoformat()+'T15:00:00+08:00' for d in days}
    args={'instrument_id':first['key']['instrument_id'],'instrument_version':first['instrument_version'],
          'calendar_version':first['calendar_version'],'as_of':'2026-06-02T09:00:00Z',
          'knowledge_mode':'public_known','window_size':2,'session_closes':closes,
          'schedule_evidence':canonical(closes).encode(),
          'boundary_comparisons':{'2026-06-02':{'older_source_version':overlap_version,'newer_source_version':raw2}}}
    return AsOfPriceService(writer),args,{'event':event,'raw1':raw1,'raw2':raw2,'overlap':overlap_version,
        'clock':clock,'event_writer':event_writer,'event_contracts':contracts,'action_request':action}


def test_public_reconstruction_selects_full_event_set_and_does_not_use_late_system_state(history):
    service,args,versions=history
    plan=service.plan(**args)
    assert plan['status']=='pass' and plan['base_date']=='2026-06-02'
    assert plan['event_version_ids']==[versions['event']]
    assert [r['data_version'] for r in plan['rows']]==[versions['raw1'],versions['raw2']]
    assert plan['source_breaks'][0]['status']=='pass'
    assert plan['reconstruction'] is True
    result=service.freeze_window(**args)
    assert result['gate_P07']['status']=='pass'
    assert [r['adj_close'] for r in result['rows']]==['5.00000000','5.00000000']
    assert result['adj_factor_version']
    assert service.price.factor_set(result['adj_factor_version'])['event_version_ids']==[versions['event']]
    system_args={**args,'knowledge_mode':'system_known'}
    system_plan=service.freeze_window(**system_args)
    assert system_plan['status']=='WINDOW_GAP' and system_plan['adj_factor_version'] is None
    assert system_plan['missing']==['2026-06-01','2026-06-02']


def test_session_close_schedule_prevents_lookahead_and_incomplete_calendar(history):
    service,args,versions=history
    before=service.plan(**{**args,'as_of':'2026-06-02T06:00:00Z','window_size':1})
    assert before['base_date']=='2026-06-01' and before['status']=='WINDOW_GAP'
    bad=deepcopy(args);bad['session_closes'].pop('2026-06-02');bad['schedule_evidence']=canonical(bad['session_closes']).encode()
    with pytest.raises(WriterError,match='SESSION_CLOSE_COVERAGE_INVALID'):
        service.plan(**bad)
    bad=deepcopy(args);bad['session_closes']['2026-06-02']='2026-06-03T15:00:00+08:00';bad['schedule_evidence']=canonical(bad['session_closes']).encode()
    with pytest.raises(WriterError,match='SESSION_CLOSE_DATE_INVALID'):
        service.plan(**bad)
    versions['clock'].now=datetime(2027,1,2,10,tzinfo=timezone.utc)
    with pytest.raises(WriterError,match='ASOF_CALENDAR_RANGE_UNKNOWN'):
        service.plan(**{**args,'as_of':'2027-01-02T09:00:00Z'})


def test_unverified_or_conflicting_cross_source_boundary_never_approves_window(history):
    service,args,versions=history
    unknown=service.freeze_window(**{**args,'boundary_comparisons':{}})
    assert unknown['status']=='SOURCE_BOUNDARY_UNVERIFIED' and unknown['adj_factor_version'] is None
    wrong_pair={**args,'boundary_comparisons':{'2026-06-02':{'older_source_version':versions['raw1'],
                                                             'newer_source_version':versions['raw2']}}}
    assert service.plan(**wrong_pair)['status']=='SOURCE_BOUNDARY_UNVERIFIED'


def test_missing_calendar_day_remains_null_instead_of_shortening_window(history, monkeypatch):
    service,args,versions=history
    original=service._accepted
    monkeypatch.setattr(service,'_accepted',lambda *a: [f for f in original(*a) if f['data_version']!=versions['raw2']])
    planned=service.freeze_window(**args)
    assert planned['status']=='WINDOW_GAP' and planned['adj_factor_version'] is None
    assert planned['rows'][1]=={'trade_date':'2026-06-02','data_version':None,'source_tag':None,'status':'missing'}
    assert planned['missing']==['2026-06-02']


def test_selected_event_requires_previous_trading_raw_not_a_nonadjacent_close(history, monkeypatch):
    service,args,versions=history
    original=service._accepted
    monkeypatch.setattr(service,'_accepted',lambda *a: [f for f in original(*a) if f['data_version']!=versions['raw1']])
    planned=service.plan(**args)
    assert 'previous_raw:2026-06-02' in planned['missing']
    assert planned['status']=='WINDOW_GAP'


def test_public_and_system_cutoffs_select_different_event_revisions_without_rewriting_old_factor(history):
    service,args,versions=history
    event_writer=versions['event_writer'];contracts=versions['event_contracts'];clock=versions['clock']
    raw=b'SYNTHETIC second official notice A002 at 16:00:01'
    notice=deepcopy(versions['action_request'])
    notice.update(dataset='announcement',key={'authority':'SSE','announcement_id':'A002'},
        contract_version=contracts['announcement'].version,profile='announcement/core',
        instrument_version=None,calendar_version=None,raw_response=raw,
        source_uri='https://www.sse.com.cn/disclosure/A002',
        wire_record={'authority':'SSE','announcement_id':'A002','content_hash':digest(raw),
                     'published_at':'2026-06-02T16:00:01+08:00'},flags={},gate_results={})
    notice['time'].update(published_at='2026-06-02T16:00:01+08:00',publication_evidence=raw)
    notice['revision']={'kind':'original','token':None,'order':None,'order_evidence':None,'supersedes_version':None}
    new_notice=event_writer.write(notice,approval(event_writer,notice))['data_version']
    corrected=deepcopy(versions['action_request'])
    corrected.update(raw_response=raw,source_uri='https://www.sse.com.cn/disclosure/A002',batch_id='event-revision-2')
    corrected['wire_record'].update(stock_ratio='2',announcement_id='A002',announcement_data_version=new_notice)
    corrected['time'].update(published_at='2026-06-02T16:00:01+08:00',publication_evidence=raw)
    corrected['revision'].update(kind='correction',token='notice-A002-rev2',order=2,order_evidence=raw,
                                 supersedes_version=versions['event'])
    clock.advance()
    new_event=event_writer.write(corrected,approval(event_writer,corrected,1,'T09'))['data_version']
    public_old=service.plan(**{**args,'as_of':'2026-06-02T08:00:00Z'})
    public_new=service.plan(**{**args,'as_of':'2026-06-02T08:00:02Z'})
    assert public_old['event_version_ids']==[versions['event']]
    assert public_new['event_version_ids']==[new_event]
    old_factor=service.freeze_window(**{**args,'as_of':'2026-06-02T08:00:00Z'})
    new_factor=service.freeze_window(**{**args,'as_of':'2026-06-02T08:00:02Z'})
    assert old_factor['adj_factor_version']!=new_factor['adj_factor_version']
    assert old_factor['rows'][0]['adj_close']=='5.00000000'
    assert new_factor['rows'][0]['adj_close']=='3.33333333'
    assert service.price.adjusted(versions['raw1'],old_factor['adj_factor_version'])['adj_close']=='5.00000000'
    system_old=service.plan(**{**args,'knowledge_mode':'system_known','as_of':'2026-06-02T10:00:00Z'})
    system_new=service.plan(**{**args,'knowledge_mode':'system_known','as_of':'2026-06-02T10:00:01Z'})
    assert system_old['event_version_ids']==[versions['event']]
    assert system_new['event_version_ids']==[new_event]
