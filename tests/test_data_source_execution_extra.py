"""Additional real transport checks against the same M04 candidate code."""
import pytest
from tests.test_data_source_execution import controller, http_supplier

pytestmark=pytest.mark.whitebox


def test_fred_public_csv_uses_one_accounted_http_response(tmp_path, monkeypatch, http_supplier):
    from app.services import fred_source
    url,state=http_supplier
    state['body']=b'observation_date,UNRATE\n2026-08-01,4.1\n2026-09-01,4.2\n'
    c=controller(tmp_path,'fred:UNRATE',transport='requests')
    monkeypatch.setattr(fred_source,'CSV_URL',url)
    with c.scope('job'):
        frame=fred_source.fetch_fred_csv('UNRATE')
    assert frame.to_dict('records')==[
        {'observation_date':'2026-08-01','UNRATE':4.1},
        {'observation_date':'2026-09-01','UNRATE':4.2}]
    assert state['hits']==['/?id=UNRATE']
    stats=c.statistics()['totals']
    assert stats['logical_calls']==stats['attempts']==stats['success']==1


def test_gateway_does_not_multiply_shared_supplier_attempts(tmp_path, monkeypatch, db_session, http_supplier):
    import asyncio,requests
    from datetime import timedelta
    from uuid import uuid4
    from app.services import external_data_gateway as gateway
    from app.services.akshare_utils import call_akshare_with_retry
    from app.schemas.external_data import StaleDataError
    c=controller(tmp_path,transport='requests',max_attempts=1)
    monkeypatch.setenv('DATA_SOURCE_CONTROL_DB',c.path)
    url,state=http_supplier
    state['status']=503
    key='m045.no-double-retry.'+uuid4().hex
    gateway.register_l4_fetcher(key,lambda p:(call_akshare_with_retry(lambda:requests.get(url).json(),api_key='stock_zh_a_hist'),'synthetic'))
    with pytest.raises(StaleDataError):
        asyncio.run(gateway.fetch(key,{},timedelta(seconds=1),allow_stale=False))
    assert state['hits']==['/']
    stats=c.statistics()['totals']
    assert stats['logical_calls']==stats['attempts']==stats['supplier_error']==1
    assert stats['retry_count']==0


def test_native_news_sdk_is_routed_to_one_controlled_http_request(tmp_path, monkeypatch, http_supplier):
    import akshare,json,requests
    from urllib.parse import urlsplit
    from curl_cffi import requests as native_requests
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import RequestPolicy
    c=controller(tmp_path,'stock_news_em',transport='requests')
    c.set_endpoint('stock_news_em','provider',RequestPolicy(max_attempts=1),transport='requests',
                   allowed_hosts=('search-api-web.eastmoney.com',),expected_version=1)
    url,state=http_supplier
    body={'result':{'cmsArticleWebOld':[{'date':'2026-10-29 10:00:00','mediaName':'Synthetic publisher',
        'code':'202610290000001','title':'<em>银行</em>公告','content':'第一行\r\n第二行','image':None}]}}
    state['body']=('dataSourceNews('+json.dumps(body,ensure_ascii=False)+')').encode('utf-8')
    monkeypatch.setattr(native_requests,'get',lambda *a,**k:pytest.fail('Unaccounted native SDK transport invoked'))
    original=requests.adapters.HTTPAdapter.send
    def local_transport(adapter, request, **kwargs):
        # Rewire only the physical test server. The real adapter must retain
        # the production hostname/contract until the accounting boundary.
        request=request.copy()
        request.url=url+'?'+urlsplit(request.url).query
        kwargs['proxies']={}
        return original(adapter,request,**kwargs)
    monkeypatch.setattr(requests.adapters.HTTPAdapter,'send',local_transport)
    with c.scope('job'):
        frame=call_akshare_with_retry(akshare.stock_news_em,symbol='600000',api_key='stock_news_em')
    assert frame.to_dict('records')==[{'关键词':'600000','新闻标题':'银行公告','新闻内容':'第一行 第二行',
        '发布时间':'2026-10-29 10:00:00','文章来源':'Synthetic publisher',
        '新闻链接':'http://finance.eastmoney.com/a/202610290000001.html'}]
    assert len(state['hits'])==1
    stats=c.statistics(include_records=True)
    assert stats['totals']['logical_calls']==stats['totals']['attempts']==stats['totals']['success']==1
