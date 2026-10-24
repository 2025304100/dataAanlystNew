"""Independently verify disputed old price inputs; do not sync any bars."""
from pathlib import Path
import json
import hashlib
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding='utf-8')
import requests
from app.services.akshare_utils import request_proxy_bypass
from app.services.market_data_sources.sources.tencent_history import fetch_tencent_history_raw

root=Path(__file__).parent
facts=json.loads((root/'preflight.json').read_text(encoding='utf-8'))
result=[]
for entry in facts['score_inputs']:
    if entry['symbol']['asset_type']!='stock':continue
    code=entry['symbol']['symbol']
    directory=root/'second-source-receipts'/code
    directory.mkdir(parents=True,exist_ok=False)
    original=requests.get
    def capture(url,**kwargs):
        assert url.startswith('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get')
        response=original(url,**kwargs)
        (directory/'qfq.response').write_bytes(response.content)
        return response
    requests.get=capture
    try:
        with request_proxy_bypass():
            frame=fetch_tencent_history_raw(symbol=entry['symbol']['market']+code,start_date='20260807',end_date='20261023',adjust='qfq')
    finally:requests.get=original
    by_day={row['date']:row for row in frame.to_dict(orient='records')}
    conflicts=[]
    for bar in entry['bars'][-50:]:
        reference=by_day[bar['trade_date']]
        difference=float(reference['close'])-bar['close']
        if abs(difference)>.00500001:
            conflicts.append({'trade_date':bar['trade_date'],'stored_close':bar['close'],
                'tencent_qfq_close':float(reference['close']),'difference':difference,'stored_source':bar['source']})
    value={'symbol':code,'verified_dates':50,'conflicts':conflicts,'response':frame.attrs['history_response'],
        'database_writes':False,'response_sha256':hashlib.sha256((directory/'qfq.response').read_bytes()).hexdigest()}
    result.append(value)
    (root/'second-source-review.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(value,ensure_ascii=False),flush=True)
