"""Verify scoring's actual local inputs; never sync or change stored bars."""
from pathlib import Path
from datetime import date
import hashlib
import json
import math
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd
from app.services.akshare_utils import request_proxy_bypass
from app.services.market_data_sources.sources import sina_history_http as transport

root=Path(__file__).parent
facts=json.loads((root/'preflight.json').read_text(encoding='utf-8'))
results=[]
for entry in facts['score_inputs']:
    code=entry['symbol']['symbol']
    bars=entry['bars'][-50:]
    record={'symbol':code,'name':entry['symbol']['name'],'config_id':entry['config']['id'],
        'required_window_start':bars[0]['trade_date'],'target_trade_date':facts['trade_date'],
        'required_bar_count':50,'price_basis':'qfq','database_writes':False,'network_is_verification_only':True}
    try:
        assert len(bars)==50 and bars[-1]['trade_date']==facts['trade_date']
        # The builtin engine uses MA50, momentum20, volatility20 and amount20;
        # earlier fetched bars do not contribute to these active presets.
        missing=[day for day in entry['missing_trade_dates'] if day>=bars[0]['trade_date']]
        assert not missing, 'required_window_has_trading_day_gaps'
        assert not entry['external_factor_keys'], 'external_factor_required'
        assert entry['invalid_input_count']==0
        if entry['symbol']['asset_type']=='etf':
            assert all(bar['source'] in {'baostock:cn-etf:v1:qfq','tencent:cn-etf:v1:qfq'} for bar in bars)
            record.update(status='VERIFIED',basis='retained_explicit_qfq_etf_units_and_prior_seven_day_continuity_review')
        else:
            directory=root/'rule-input-receipts'/code
            directory.mkdir(parents=True,exist_ok=False)
            original=transport._read_response
            receipts=[]
            def archived(url):
                payload=original(url)
                name='history' if 'hisdata_klc2' in url else 'shares' if 'getAmountBySymbol' in url else 'qfq'
                (directory/(name+'.response')).write_bytes(payload)
                receipts.append({'name':name,'url':url,'sha256':hashlib.sha256(payload).hexdigest()})
                return payload
            transport._read_response=archived
            try:
                with request_proxy_bypass():
                    frame=transport.fetch_sina_history_raw(symbol=entry['symbol']['market']+code,
                        start_date=bars[0]['trade_date'].replace('-',''),end_date=facts['trade_date'].replace('-',''),adjust='qfq')
            finally:transport._read_response=original
            reference={str(row['trade_date'])[:10]:row for row in frame.to_dict(orient='records')}
            assert set(reference)=={bar['trade_date'] for bar in bars}, 'verification_coverage_mismatch'
            price_difference=0.
            amount_difference=0.
            for index,bar in enumerate(bars):
                observed=reference[bar['trade_date']]
                difference=abs(bar['close']-observed['close'])
                price_difference=max(price_difference,difference)
                assert difference<=.00500001,'qfq_closing_price_mismatch'
                if index>=len(bars)-20:
                    difference=abs(bar['amount']-observed['amount'])
                    amount_difference=max(amount_difference,difference)
                    assert difference<=.010001,'amount_unit_or_value_mismatch'
            record.update(status='VERIFIED',basis='strict_sina_qfq_prices_and_CNY_amount_crosscheck',
                max_price_difference=price_difference,max_recent_amount_difference=amount_difference,
                history_response=frame.attrs['history_response'],receipts=receipts)
        record['input_bars_sha256']=entry['bars_sha256']
        record['configuration_sha256']=entry['config_sha256']
        record['point_in_time_certified']=False
    except Exception as error:
        record.update(status='NOT_SAFE_TO_RECALCULATE',reason=str(error),error_type=type(error).__name__)
    results.append(record)
    (root/'rule-input-verification.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False),flush=True)
