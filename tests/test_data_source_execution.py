"""M04.5 production admission and real task/transport boundaries."""
import pytest

pytestmark = pytest.mark.whitebox


def controller(tmp_path, endpoint='stock_zh_a_hist', *, transport='callable', **limits):
    from app.services.data_source_control import RequestController, RequestPolicy
    c = RequestController(tmp_path / 'control.sqlite')
    policy = RequestPolicy(failure_threshold=20, **limits)
    c.set_provider('provider', policy)
    c.set_endpoint(endpoint, 'provider', policy, transport=transport,
                   allowed_hosts=('127.0.0.1',) if transport in {'requests', 'sdk'} else ())
    c.start_task('job', lane='incremental', max_requests=20, timeout_s=60)
    return c


def test_production_rejects_enabled_endpoint_without_admission_before_supplier(tmp_path, monkeypatch):
    from app.services.data_source_control import PolicyDenied
    c = controller(tmp_path)
    monkeypatch.setenv('DATA_SOURCE_EXECUTION_MODE', 'production')
    contacted = []
    with pytest.raises(PolicyDenied, match='PRODUCTION_NOT_ADMITTED'):
        c.execute('stock_zh_a_hist', lambda: contacted.append(True), task_id='job')
    assert contacted == []
    assert c.statistics()['totals']['policy_denied'] == 1
    assert c.statistics()['totals']['attempts'] == 0


def test_fred_csv_disabled_before_url_reader_through_macro_service(tmp_path, monkeypatch, db_session):
    import pandas as pd
    from app.services import macro
    from app.services.data_source_control import RequestPolicy
    c = controller(tmp_path, 'fred:UNRATE')
    c.set_provider('provider', RequestPolicy(enabled=False), expected_version=1)
    spec = macro.MacroSpec('us', 'risk', 'us_unemployment', 'Unemployment', 'fred:UNRATE', 1, '%', 'monthly', 'lower', neutral=4.2)
    monkeypatch.setattr(macro, 'SPECS', [spec])
    contacted = []
    def csv_boundary(*args, **kwargs):
        contacted.append(args[0])
        return pd.DataFrame({'observation_date': ['2026-08-01','2026-09-01'], 'UNRATE': [4.1,4.2]})
    monkeypatch.setattr(pd, 'read_csv', csv_boundary)
    with c.scope('job'):
        result = macro.update_macro_data(db_session, region='us')
    assert contacted == []
    assert result.failed[0]['error'] == 'DISABLED'
    assert c.statistics()['totals']['attempts'] == 0


def test_manual_entry_creates_bounded_shared_task_without_process_cache(tmp_path, monkeypatch):
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_execution import data_task
    c = controller(tmp_path)
    monkeypatch.setenv('DATA_SOURCE_CONTROL_DB', c.path)
    with data_task():
        assert call_akshare_with_retry(lambda: 17, api_key='stock_zh_a_hist') == 17
    calls = c.statistics(include_records=True)['calls']
    assert len(calls) == 1
    assert calls[0]['task_id'].startswith('source:')
    assert calls[0]['lane'] == 'incremental'


def test_tdx_disabled_policy_prevents_local_file_read(tmp_path):
    import struct
    from datetime import date
    from types import SimpleNamespace
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    from app.services.market_data_sources.sources.tdx_source import TDXHistorySource
    c = controller(tmp_path, 'TDX:day')
    c.set_provider('provider', RequestPolicy(enabled=False), expected_version=1)
    path = tmp_path / 'vipdoc/sz/lday/sz000001.day'
    path.parent.mkdir(parents=True)
    path.write_bytes(struct.pack('<IIIIIfII', 20240816, 1234, 1356, 1112, 1298, 12345678., 1234567, 0))
    source = TDXHistorySource()
    source.tdx_path = str(tmp_path)
    with c.scope('job'), pytest.raises(PolicyDenied, match='DISABLED'):
        source.fetch(SimpleNamespace(symbol='000001', market='sz', asset_type='stock'), date(2024,8,16), date(2024,8,16), '')
    assert c.statistics()['totals']['attempts'] == 0


def test_async_manual_entry_preserves_shared_task_and_its_budget(tmp_path, monkeypatch):
    import asyncio
    from app.core.async_utils import run_sync
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import PolicyDenied
    c = controller(tmp_path)
    c.start_task('manual-one', lane='backfill', max_requests=1, timeout_s=30)
    monkeypatch.setenv('DATA_SOURCE_CONTROL_DB', c.path)
    with c.scope('manual-one'):
        assert asyncio.run(run_sync(call_akshare_with_retry, lambda: 11, api_key='stock_zh_a_hist')) == 11
        with pytest.raises(PolicyDenied, match='TASK_QUOTA'):
            asyncio.run(run_sync(call_akshare_with_retry, lambda: 12, api_key='stock_zh_a_hist'))
    calls = c.statistics(include_records=True)['calls']
    assert {row['task_id'] for row in calls} == {'manual-one'}
    assert {row['lane'] for row in calls} == {'backfill'}


def test_disabled_adapter_does_not_turn_policy_denial_into_source_failover(tmp_path):
    from datetime import date
    from types import SimpleNamespace
    from app.services.market_data_sources.chain import SourceChain
    from app.services.market_data_sources.sources.akshare_source import EMStockSource
    from app.services.data_source_control import PolicyDenied, RequestPolicy
    c = controller(tmp_path)
    c.set_provider('provider', RequestPolicy(enabled=False), expected_version=1)
    class Backup:
        name = 'backup'
        def available(self): return True
        def supports(self, symbol): return True
        def fetch(self, *args): pytest.fail('Policy denial silently selected a backup')
    with c.scope('job'), pytest.raises(PolicyDenied, match='DISABLED'):
        SourceChain([EMStockSource(), Backup()]).fetch(
            SimpleNamespace(symbol='600000', market='sh', asset_type='stock'), date(2026,1,1), date(2026,1,2), '')


def admission_fixture(tmp_path, endpoint='stock_zh_a_hist'):
    """Synthetic acceptance inputs, never a claim about a real supplier."""
    import hashlib
    import time
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    refs = {}
    for role in ('contract', 'control', 'review'):
        path = tmp_path / (role + '-synthetic.txt')
        path.write_text(json.dumps({'endpoint': endpoint, 'kind': role, 'status': 'pass',
            'package_version':'1.4.4', 'reviewer_id':'synthetic-test-reviewer',
            'source_hashes': {'app/services/data_source_control.py': hashlib.sha256((root/'app/services/data_source_control.py').read_bytes()).hexdigest()}}), encoding='utf-8')
        refs[role] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return dict(implementation_status='validated', auth_required=False, auth_status='not_required',
                permission_proof_id=None, permission_valid_until=None, admission_status='ready',
                production_enabled=True, capacity_ok=True, valid_until=time.time()+300, evidence=refs)


def test_production_requires_current_evidence_and_rechecks_revocation(tmp_path, monkeypatch):
    from app.services.data_source_control import PolicyDenied
    c = controller(tmp_path)
    evidence = admission_fixture(tmp_path)
    assert c.publish_admission('stock_zh_a_hist', evidence, expected_version=0) == 1
    monkeypatch.setenv('DATA_SOURCE_EXECUTION_MODE', 'production')
    assert c.execute('stock_zh_a_hist', lambda: 8, task_id='job') == 8
    from pathlib import Path
    Path(evidence['evidence']['control']['path']).write_text('changed after review', encoding='utf-8')
    with pytest.raises(PolicyDenied, match='PRODUCTION_NOT_ADMITTED'):
        c.execute('stock_zh_a_hist', lambda: pytest.fail('Revoked evidence used'), task_id='job')
    assert c.statistics()['totals']['attempts'] == 1


@pytest.fixture
def http_supplier():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    state = {'hits': [], 'body': {'data': []}, 'status': 200}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['hits'].append(self.path)
            if state.get('entered') is not None:
                state['entered'].set()
                assert state['release'].wait(5)
            if state.get('trickle'):
                import time
                self.send_response(200)
                self.send_header('Content-Length', '100')
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                        time.sleep(.015)
                except (OSError, ConnectionError):
                    state['peer_closed'] = True
                return
            body = state.get('by_path', {}).get(self.path, state['body'])
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(state['status'])
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        do_POST = do_GET
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield 'http://127.0.0.1:' + str(server.server_port), state
    server.shutdown()
    server.server_close()
    thread.join(3)


@pytest.mark.reap_workers
def test_external_sync_worker_keeps_business_task_id_at_real_http_boundary(tmp_path, monkeypatch, db_session, http_supplier):
    import time
    import requests
    from datetime import date
    from app.services import external_data_sync_task as sync
    from app.services import hot_rank_data
    class TradingDate(date):
        @classmethod
        def today(cls): return cls(2026, 6, 2)
    # Both the planner and current-snapshot adapter must see the same weekday;
    # requested_days deliberately filters real weekends independently of the
    # market_session predicate below. This is a transport/task-context test.
    monkeypatch.setattr(sync, 'date', TradingDate)
    monkeypatch.setattr(hot_rank_data, 'date', TradingDate)
    c = controller(tmp_path, 'stock_hot_rank_em', transport='requests')
    monkeypatch.setenv('DATA_SOURCE_CONTROL_DB', c.path)
    monkeypatch.setattr('app.services.market_session.is_cn_trading_day', lambda *args: True)
    url, state = http_supplier
    # Only the remote transport URL is replaced; real request, controller,
    # task worker, task state and business transaction all execute normally.
    original = requests.post
    monkeypatch.setattr(requests, 'post', lambda remote, **kwargs: original(url, **kwargs))
    task = sync.start_external_data_sync('hot_rank', {'mode': 'incremental', 'source': 'all'})
    deadline = time.monotonic() + 20
    while True:
        result = sync.get_external_sync_task(task.id)
        if result.status in {'done', 'failed', 'cancelled', 'partial'}: break
        assert time.monotonic() < deadline
        time.sleep(.03)
    assert state['hits'] == ['/']
    calls = c.statistics(include_records=True)['calls']
    assert len(calls) == 1
    assert calls[0]['task_id'] == task.id


def test_http_trickle_cannot_outlive_logical_deadline(tmp_path, http_supplier):
    import requests
    import time
    from app.services.data_source_control import PolicyDenied
    url, state = http_supplier
    state['trickle'] = True
    c = controller(tmp_path, transport='requests', timeout_s=.15, max_attempts=1)
    started = time.monotonic()
    with c.scope('job'), pytest.raises(PolicyDenied, match='DEADLINE'):
        c.execute('stock_zh_a_hist', lambda: requests.get(url, timeout=(5, 10)))
    assert time.monotonic() - started < .8
    assert len(state['hits']) == 1
    assert c.statistics()['totals']['success'] == 0


def test_direct_sina_adapter_counts_three_bounded_responses(tmp_path, monkeypatch, http_supplier):
    from pathlib import Path
    from datetime import date
    import requests
    from app.services.market_data_sources.sources.sina_history_http import fetch_sina_history_raw
    url, state = http_supplier
    fixtures = Path(__file__).parent / 'fixtures/sina_000425_history_20261016'
    state['by_path'] = {'/' + name: (fixtures / (name+'.response')).read_bytes() for name in ('history','shares','qfq')}
    original = requests.get
    def remote_boundary(remote, **kwargs):
        name = 'shares' if 'getAmountBySymbol' in remote else 'qfq' if remote.endswith('/qfq.js') else 'history'
        return original(url+'/'+name, **kwargs)
    monkeypatch.setattr(requests, 'get', remote_boundary)
    c = controller(tmp_path, 'stock_zh_a_daily', transport='requests')
    with c.scope('job'):
        result = fetch_sina_history_raw(symbol='sz000425', start_date='20261013', end_date='20261016', adjust='')
    assert not result.empty
    assert state['hits'] == ['/history','/shares','/qfq']
    assert c.statistics()['totals']['logical_calls'] == 1
    assert c.statistics()['totals']['attempts'] == 3


def test_disabled_baostock_never_launches_sdk_process(tmp_path, monkeypatch):
    from app.services.security_status.baostock_source import capture_history
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    c = controller(tmp_path, 'BAO:query_trade_dates')
    c.set_endpoint('BAO:query_stock_basic', 'provider', RequestPolicy())
    c.set_endpoint('BAO:query_history_k_data_plus', 'provider', RequestPolicy())
    c.set_provider('provider', RequestPolicy(enabled=False), expected_version=1)
    monkeypatch.setattr('subprocess.Popen', lambda *a, **k: pytest.fail('Disabled SDK process launched'))
    with c.scope('job'), pytest.raises(PolicyDenied, match='DISABLED'):
        capture_history({'start_date':'2026-01-01','end_date':'2026-01-02','symbols':[]}, tmp_path/'capture', lambda *a: None)


@pytest.fixture
def fake_baostock_sdk(tmp_path, monkeypatch, http_supplier):
    """External SDK substitute in a real child process and real TCP boundary."""
    import os
    from urllib.parse import urlsplit
    url, state = http_supplier
    state['body'] = b'ok'
    package = tmp_path / 'sdk/baostock'
    (package / 'util').mkdir(parents=True)
    (package / 'common').mkdir()
    for name in ('util/__init__.py','common/__init__.py','common/context.py'):
        (package / name).write_text('', encoding='utf-8')
    (package / 'util/socketutil.py').write_text('''import socket, os
import baostock.common.context as context
class SocketUtil:
 def connect(self, key):
  context.default_socket=socket.socket()
  context.default_socket.connect(('127.0.0.1', int(os.environ['SYNTHETIC_SDK_PORT'])))
def send_msg(message):
 sock=context.default_socket
 sock.send(('GET /sdk/'+message+' HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n').encode())
 data=b''
 while True:
  chunk=sock.recv(4096)
  if not chunk: break
  data+=chunk
 sock.close()
 return 'ok'
''', encoding='utf-8')
    (package / '__init__.py').write_text('''import os,time
from .util import socketutil as sock
class Result:
 error_code='0'
 error_msg='success'
 fields=['code']
 def __init__(self): self.done=False
 def next(self):
  if os.environ.get('SYNTHETIC_SDK_HANG')=='1':
   while True: time.sleep(1)
  if self.done:return False
  self.done=True
  return True
 def get_row_data(self):return ['sh600000']
def wire(name):
 sock.SocketUtil().connect('anonymous')
 sock.send_msg(name)
 return Result()
def login():return wire('login')
def logout():return wire('logout')
def query_trade_dates(**kwargs):return wire('calendar')
def query_stock_basic(**kwargs):return wire('basic')
def query_history_k_data_plus(**kwargs):return wire('history')
''', encoding='utf-8')
    monkeypatch.setenv('PYTHONPATH', str(package.parent) + os.pathsep + os.environ.get('PYTHONPATH',''))
    monkeypatch.setenv('SYNTHETIC_SDK_PORT', str(urlsplit(url).port))
    return state


def test_baostock_child_shares_task_budget_and_observes_protocol(tmp_path, monkeypatch, fake_baostock_sdk):
    import json
    from app.services.security_status.baostock_source import capture_history
    from app.services.data_source_control import RequestPolicy
    c = controller(tmp_path, 'BAO:query_trade_dates', transport='sdk')
    for endpoint in ('BAO:query_stock_basic', 'BAO:query_history_k_data_plus'):
        c.set_endpoint(endpoint, 'provider', RequestPolicy(), transport='sdk', allowed_hosts=('127.0.0.1',))
    request = {'start_date':'2026-01-01','end_date':'2026-01-02', 'symbols':[{'symbol':'600000','provider_code':'sh600000'}]}
    directory = tmp_path/'capture'
    with c.scope('job'):
        capture_history(request, directory, lambda *args: None, timeout_seconds=15)
    assert json.loads((directory/'capture.json').read_text(encoding='utf-8'))['complete'] is True
    assert fake_baostock_sdk['hits'] == ['/sdk/login','/sdk/calendar','/sdk/basic','/sdk/history']
    stats = c.statistics(include_records=True)
    assert stats['totals']['logical_calls'] == 3
    assert stats['totals']['attempts'] == 8  # 4 TCP connects + 4 protocol messages.
    assert {call['task_id'] for call in stats['calls']} == {'job'}


def test_inline_index_failover_is_one_selection_not_an_endpoint_retry(tmp_path, monkeypatch, db_session):
    import pandas as pd
    import requests
    from datetime import date
    from app.services import index_data
    from app.services.data_source_control import RequestPolicy
    c = controller(tmp_path, 'index_zh_a_hist', max_attempts=1)
    c.set_endpoint('index_zh_a_daily', 'provider', RequestPolicy(max_attempts=1))
    monkeypatch.setattr(index_data.ak, 'index_zh_a_hist', lambda **k: (_ for _ in ()).throw(requests.ConnectionError('synthetic failure')))
    monkeypatch.setattr(index_data.ak, 'stock_zh_index_daily', lambda **k: pd.DataFrame([
        dict(date='2026-01-05', open=100., high=102., low=99., close=101., volume=1000.)]))
    with c.scope('job'):
        result = index_data.sync_index_daily(db_session, '000300', start_date=date(2026,1,5), end_date=date(2026,1,5))
    assert result.written == 1
    stats = c.statistics(include_records=True)
    assert stats['totals']['selections'] == stats['totals']['fallbacks'] == 1
    assert stats['totals']['retry_count'] == 0
    assert stats['selections'][0]['selected_source'] == 'index_zh_a_daily'
    assert stats['selections'][0]['failed_sources'] == ['index_zh_a_hist']


def test_admission_evidence_for_other_endpoint_cannot_open_production(tmp_path, monkeypatch):
    from app.services.data_source_control import PolicyDenied
    c = controller(tmp_path)
    c.publish_admission('stock_zh_a_hist', admission_fixture(tmp_path, 'fund_etf_hist_em'))
    monkeypatch.setenv('DATA_SOURCE_EXECUTION_MODE', 'production')
    with pytest.raises(PolicyDenied, match='PRODUCTION_NOT_ADMITTED'):
        c.execute('stock_zh_a_hist', lambda: pytest.fail('Wrong endpoint proof used'), task_id='job')


@pytest.mark.parametrize('stop', ['deadline','disable','cancel'])
def test_noncooperative_baostock_child_is_stopped_without_late_success(tmp_path, monkeypatch, fake_baostock_sdk, stop):
    import subprocess
    import time
    from concurrent.futures import ThreadPoolExecutor
    from app.services.security_status.baostock_source import capture_history
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    from app.services.data_source_execution import submit_with_context
    policy = RequestPolicy(timeout_s=.4 if stop == 'deadline' else 10, max_attempts=1)
    c = controller(tmp_path, 'BAO:query_trade_dates', transport='sdk', timeout_s=policy.timeout_s, max_attempts=1)
    for endpoint in ('BAO:query_stock_basic','BAO:query_history_k_data_plus'):
        c.set_endpoint(endpoint,'provider',policy,transport='sdk',allowed_hosts=('127.0.0.1',))
    monkeypatch.setenv('SYNTHETIC_SDK_HANG','1')
    processes = []
    original = subprocess.Popen
    def spawn(*a, **kw):
        process = original(*a, **kw)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    started = time.monotonic()
    with c.scope('job'), ThreadPoolExecutor(1) as pool:
        future = submit_with_context(pool, capture_history,
            {'start_date':'2026-01-01','end_date':'2026-01-02','symbols':[]}, tmp_path/'capture', lambda *a: None, timeout_seconds=15)
        if stop != 'deadline':
            while '/sdk/calendar' not in fake_baostock_sdk['hits']:
                assert time.monotonic() - started < 6
                time.sleep(.01)
            if stop == 'disable':
                c.set_provider('provider', RequestPolicy(enabled=False), expected_version=1)
            else:
                c.cancel_task('job')
        with pytest.raises(PolicyDenied, match='DEADLINE|DISABLED|CANCELLED'):
            future.result(timeout=8)
    assert time.monotonic() - started < 6
    assert processes and all(p.poll() is not None for p in processes)
    stats = c.statistics()['totals']
    assert stats['success'] == stats['running'] == 0
    # Disable/cancel can land between SDK calls after the HTTP hit: the next
    # admission is denied rather than interrupting an in-flight observation.
    assert stats['interrupted'] + stats['policy_denied'] == 1


def test_sdk_cannot_bypass_request_accounting_using_urllib(tmp_path, http_supplier):
    import urllib.request
    from app.services.data_source_control import PolicyDenied
    c = controller(tmp_path, transport='requests')
    url, state = http_supplier
    with c.scope('job'), pytest.raises(PolicyDenied, match='UNSUPPORTED_TRANSPORT'):
        c.execute('stock_zh_a_hist', lambda: urllib.request.urlopen(url).read())
    assert state['hits'] == []
    assert c.statistics()['totals']['attempts'] == 0


@pytest.mark.parametrize('planned', ['PLAN:Fuyao','PLAN:iFinD','PLAN:BSE'])
def test_planned_source_cannot_enter_production_selection_even_with_local_frame(tmp_path, monkeypatch, planned):
    import pandas as pd
    from datetime import date
    from types import SimpleNamespace
    from app.services.market_data_sources.chain import SourceChain
    from app.services.data_source_control import PolicyDenied
    c = controller(tmp_path)
    monkeypatch.setenv('DATA_SOURCE_EXECUTION_MODE','production')
    class Planned:
        name = planned
        def available(self): return True
        def supports(self, symbol): return True
        def fetch(self, *args): return pd.DataFrame([{'close':10}])
    with c.scope('job'), pytest.raises(PolicyDenied, match='OUT_OF_RELEASE'):
        SourceChain([Planned()]).fetch(SimpleNamespace(symbol='600000'), date(2026,1,1), date(2026,1,2),'')
    stats = c.statistics(include_records=True)
    assert stats['totals']['success'] == stats['totals']['attempts'] == 0
    assert stats['selections'][0]['selected_source'] is None


def test_early_provider_enablement_check_is_a_shared_policy_denial(tmp_path):
    from app.services.capital_flow_provider import fetch_capital_flow_quotes
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    c = controller(tmp_path,'stock_individual_fund_flow_rank',transport='requests')
    c.set_provider('provider',RequestPolicy(enabled=False),expected_version=1)
    with c.scope('job'), pytest.raises(PolicyDenied,match='DISABLED'):
        fetch_capital_flow_quotes(None,['1.600000'])
    assert c.statistics()['totals']['policy_denied'] == 1
    assert c.statistics()['totals']['attempts'] == 0


def test_calendar_refresh_entry_creates_one_task_for_its_direct_http(tmp_path, monkeypatch, db_session, http_supplier):
    from app.services import trade_calendar_sync
    url, state = http_supplier
    state['body'] = ('<h2>2026年休市安排</h2><table>' + ''.join(
        '<tr><td>'+name+'</td><td>'+period+'休市</td></tr>' for name,period in (
        ('元旦','1月1日至1月3日'),('春节','2月15日至2月23日'),('清明节','4月4日至4月6日'),
        ('劳动节','5月1日至5月5日'),('端午节','6月19日至6月21日'),('中秋节','9月25日至9月27日'),
        ('国庆节','10月1日至10月7日'))) + '</table>').encode('utf-8')
    c = controller(tmp_path,'CAL:official',transport='requests')
    monkeypatch.setenv('DATA_SOURCE_CONTROL_DB', c.path)
    monkeypatch.setattr(trade_calendar_sync,'SOURCE_URL',url)
    assert trade_calendar_sync.refresh_cn_trade_calendar(force=True)['trading_days'] == 242
    assert state['hits'] == ['/']
    calls = c.statistics(include_records=True)['calls']
    assert len(calls) == 1 and calls[0]['task_id'].startswith('source:')


@pytest.mark.parametrize('field,value', [
    ('production_enabled',False), ('capacity_ok',False), ('implementation_status','implemented'),
    ('admission_status','not_reviewed'), ('valid_until',0), ('auth_required',True),
    ('auth_status','verified'), ('permission_proof_id','unexpected-public-auth-proof'), ('permission_valid_until',9999999999),
])
def test_each_admit_condition_fails_closed_without_supplier_request(tmp_path, monkeypatch, field, value):
    from app.services.data_source_control import PolicyDenied
    c = controller(tmp_path)
    evidence = admission_fixture(tmp_path)
    evidence[field] = value
    c.publish_admission('stock_zh_a_hist', evidence)
    monkeypatch.setenv('DATA_SOURCE_EXECUTION_MODE','production')
    with pytest.raises(PolicyDenied,match='PRODUCTION_NOT_ADMITTED'):
        c.execute('stock_zh_a_hist',lambda: pytest.fail('Incomplete ADMIT-01 contacted supplier'),task_id='job')
    assert c.statistics()['totals']['attempts'] == 0


@pytest.mark.parametrize('mutation',['package','source','malformed'])
def test_hashed_receipt_still_requires_review_scope_and_current_source(tmp_path, monkeypatch, mutation):
    import hashlib,json
    from pathlib import Path
    from app.services.data_source_control import PolicyDenied
    c=controller(tmp_path)
    evidence=admission_fixture(tmp_path)
    ref=evidence['evidence']['review']
    path=Path(ref['path'])
    value=json.loads(path.read_text(encoding='utf-8'))
    if mutation=='package': value['package_version']='1.4.3'
    elif mutation=='source': value['source_hashes']['app/services/data_source_control.py']='0'*64
    if mutation=='malformed': path.write_text('unstructured approval text',encoding='utf-8')
    else: path.write_text(json.dumps(value),encoding='utf-8')
    ref['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    c.publish_admission('stock_zh_a_hist',evidence)
    monkeypatch.setenv('DATA_SOURCE_EXECUTION_MODE','production')
    with pytest.raises(PolicyDenied,match='PRODUCTION_NOT_ADMITTED'):
        c.execute('stock_zh_a_hist',lambda: pytest.fail('Invalid review receipt accepted'),task_id='job')
    assert c.statistics()['totals']['attempts'] == 0


def test_gateway_l4_keeps_task_identity_and_shared_attempt_budget(tmp_path, monkeypatch, db_session, http_supplier):
    import asyncio,requests
    from datetime import timedelta
    from uuid import uuid4
    from app.services import external_data_gateway as gateway
    from app.services.akshare_utils import call_akshare_with_retry
    c=controller(tmp_path,transport='requests',max_attempts=1)
    monkeypatch.setenv('DATA_SOURCE_CONTROL_DB',c.path)
    url,state=http_supplier
    state['body']={'value':23}
    key='m045.gateway.'+uuid4().hex
    def supplier(params):
        return call_akshare_with_retry(lambda:requests.get(url).json(),api_key='stock_zh_a_hist'), 'synthetic'
    gateway.register_l4_fetcher(key,supplier)
    result=asyncio.run(gateway.fetch(key,{},timedelta(seconds=1),allow_stale=False,
                                  task_context={'task_id':'gateway-task','task_type':'backfill'}))
    assert result.data == {'value':23}
    assert state['hits']==['/']
    stats=c.statistics(include_records=True)
    assert stats['totals']['attempts']==1
    assert stats['calls'][0]['task_id']=='gateway-task'
    assert stats['calls'][0]['lane']=='backfill'


def test_cancelled_gateway_waiter_does_not_cancel_shared_leader(tmp_path, monkeypatch, db_session, http_supplier):
    import asyncio,requests,threading
    from datetime import timedelta
    from uuid import uuid4
    from app.services import external_data_gateway as gateway
    from app.services.akshare_utils import call_akshare_with_retry
    c=controller(tmp_path,transport='requests',max_attempts=1)
    monkeypatch.setenv('DATA_SOURCE_CONTROL_DB',c.path)
    url,state=http_supplier
    state.update(body={'value':31},entered=threading.Event(),release=threading.Event())
    key='m045.gateway-cancel.'+uuid4().hex
    gateway.register_l4_fetcher(key,lambda p:(call_akshare_with_retry(lambda:requests.get(url).json(),api_key='stock_zh_a_hist'),'synthetic'))
    async def scenario():
        leader=asyncio.create_task(gateway.fetch(key,{},timedelta(seconds=1),allow_stale=False))
        follower=None
        try:
            assert await asyncio.to_thread(state['entered'].wait,3)
            follower=asyncio.create_task(gateway.fetch(key,{},timedelta(seconds=1),allow_stale=False))
            await asyncio.sleep(.05)
            follower.cancel()
            with pytest.raises(asyncio.CancelledError): await follower
            state['release'].set()
            return await asyncio.wait_for(leader,3)
        finally:
            state['release'].set()
            if not leader.done(): leader.cancel()
            if follower and not follower.done(): follower.cancel()
    result=asyncio.run(scenario())
    assert result.data=={'value':31}
    assert state['hits']==['/']
    assert c.statistics()['totals']['success']==1


def test_business_worker_origin_reuses_budget_and_cancel_state_for_data_subtasks(tmp_path, monkeypatch, db_session):
    from app.services.async_tasks import create_async_task, cancel_async_task
    from app.services.data_source_execution import business_task_context, data_task
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import PolicyDenied
    c=controller(tmp_path)
    monkeypatch.setenv('DATA_SOURCE_CONTROL_DB',c.path)
    task=create_async_task('m045_origin_test',{'mode':'backfill'},force_new=True)
    with business_task_context(task.id):
        with data_task(): assert call_akshare_with_retry(lambda:1,api_key='stock_zh_a_hist')==1
        with data_task(): assert call_akshare_with_retry(lambda:2,api_key='stock_zh_a_hist')==2
        cancel_async_task(task.id)
        with data_task(), pytest.raises(PolicyDenied,match='CANCELLED'):
            call_akshare_with_retry(lambda:pytest.fail('Business cancellation lost'),api_key='stock_zh_a_hist')
    stats=c.statistics(include_records=True)
    assert stats['totals']['attempts']==2
    assert {call['task_id'] for call in stats['calls']}=={task.id}
    assert {call['lane'] for call in stats['calls']}=={'backfill'}


@pytest.mark.parametrize('active',[False,True])
def test_business_entry_cannot_attach_to_probe_budget(tmp_path, monkeypatch, active):
    from app.services.data_source_execution import data_task
    from app.services.data_source_control import PolicyDenied
    from contextlib import nullcontext
    c=controller(tmp_path)
    c.start_task('probe-job',lane='probe',max_requests=2,timeout_s=30)
    monkeypatch.setenv('DATA_SOURCE_CONTROL_DB',c.path)
    with c.scope('probe-job') if active else nullcontext():
        with pytest.raises(PolicyDenied,match='TASK_LANE_MISMATCH'):
            with data_task(task_id=None if active else 'probe-job'): pass


def test_source_control_flag_preserves_unrelated_application_http(tmp_path, monkeypatch, http_supplier):
    import requests
    from app.services.akshare_utils import ensure_controlled_http
    ensure_controlled_http()
    c=controller(tmp_path,transport='requests')
    monkeypatch.setenv('DATA_SOURCE_CONTROL_DB',c.path)
    url,state=http_supplier
    # Source binding is exactly 127.0.0.1; this independent application URL is
    # intentionally not a configured source. Both remain local test servers.
    result=requests.get(url.replace('127.0.0.1','localhost'),timeout=1)
    assert result.json()=={'data':[]}
    assert state['hits']==['/']
    assert c.statistics()['totals']['logical_calls']==0


@pytest.mark.parametrize('endpoint',['PLAN:CSI_constituents','PLAN:Baostock_valuation','stock_lrb_em'])
def test_known_unimplemented_catalog_entry_cannot_use_legacy_default_enabled(monkeypatch,endpoint):
    from app.services import akshare_registry
    from app.services.akshare_utils import call_akshare_with_retry, AkshareApiDisabledError
    monkeypatch.setattr(akshare_registry,'_config_cache_loaded',False)
    monkeypatch.setattr(akshare_registry,'_config_cache',{})
    with pytest.raises(AkshareApiDisabledError):
        call_akshare_with_retry(lambda:pytest.fail('Unimplemented endpoint used legacy default'),api_key=endpoint)


def test_aggregate_endpoint_cannot_register_planned_bse_upstream(tmp_path):
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    c=controller(tmp_path,'stock_info_a_code_name',transport='requests')
    with pytest.raises(PolicyDenied,match='UPSTREAM_OUT_OF_RELEASE'):
        c.set_endpoint('stock_info_a_code_name','provider',RequestPolicy(),transport='requests',
                       allowed_hosts=('www.bse.cn',),expected_version=1)


def test_planned_bse_host_is_blocked_even_through_legacy_aggregate_http(monkeypatch):
    import requests
    from app.services.akshare_utils import ensure_controlled_http
    from app.services.data_source_control import PolicyDenied
    ensure_controlled_http()
    sent=[]
    def transport(self, request, **kwargs):
        sent.append(request.url)
        response=requests.Response()
        response.status_code=200
        response._content=b'[]'
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter,'send',transport)
    with pytest.raises(PolicyDenied,match='UPSTREAM_OUT_OF_RELEASE'):
        requests.get('https://www.bse.cn/nqxxController/nqxxCnzq.do')
    assert sent==[]


def test_old_aggregate_binding_cannot_bypass_upstream_scope_or_mislead_panel(tmp_path):
    import sqlite3,json
    from app.services.data_source_control import PolicyDenied
    from app.services.data_source_management import metadata
    c=controller(tmp_path,'stock_info_a_code_name',transport='requests')
    # A legacy control file is an input at the SQLite boundary, not a private
    # controller mutation. Observe enforcement and UI facts through public APIs.
    with sqlite3.connect(c.path) as db:
        db.execute("UPDATE policies SET body=json_set(body,'$.allowed_hosts',json(?)) WHERE key=?",
                   (json.dumps(['www.bse.cn']), 'endpoint:stock_info_a_code_name'))
    facts=metadata('stock_info_a_code_name',registered=True,snapshot=c.management_snapshot())
    assert facts['effective_enabled'] is False and facts['can_probe'] is False
    assert 'UPSTREAM_OUT_OF_RELEASE' in facts['block_reasons']
    with c.scope('job'),pytest.raises(PolicyDenied,match='UPSTREAM_OUT_OF_RELEASE'):
        c.execute('stock_info_a_code_name',lambda:pytest.fail('Old upstream binding executed'))
    assert c.statistics()['totals']['attempts']==0
