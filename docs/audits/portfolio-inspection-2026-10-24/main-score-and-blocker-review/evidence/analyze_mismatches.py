"""Analyze retained responses offline; no further fetch or database writes."""
from pathlib import Path
from datetime import date
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding='utf-8')
from app.services.market_data_sources.sources.sina_history_evidence import parse_sina_history_bundle

root=Path(__file__).parent
facts=json.loads((root/'preflight.json').read_text(encoding='utf-8'))
results=[]
for entry in facts['score_inputs']:
    code=entry['symbol']['symbol']
    if entry['symbol']['asset_type']!='stock':continue
    bars=entry['bars'][-50:]
    folder=root/'rule-input-receipts'/code
    frame=parse_sina_history_bundle(entry['symbol']['market']+code,
        (folder/'history.response').read_bytes(),(folder/'shares.response').read_bytes(),
        (folder/'qfq.response').read_bytes(),date.fromisoformat(bars[0]['trade_date']),
        date.fromisoformat(facts['trade_date']),adjust='qfq')
    references={str(row['trade_date'])[:10]:row for row in frame.to_dict(orient='records')}
    mismatches=[]
    for index,bar in enumerate(bars):
        observed=references[bar['trade_date']]
        differences={field:{'stored':bar[field],'reference':observed[field],
            'difference':observed[field]-bar[field]} for field in ['open','high','low','close','volume','amount']
            if abs(bar[field]-observed[field])>(.00500001 if field in ['open','high','low','close'] else .010001)}
        if differences:
            mismatches.append({'trade_date':bar['trade_date'],'source':bar['source'],
                'affects_current_rule_score':('close' in differences or (index>=len(bars)-20 and 'amount' in differences)),
                'differences':differences})
    result={'symbol':code,'name':entry['symbol']['name'],'verified_dates':50,
        'mismatch_dates':len(mismatches),'affecting_dates':sum(row['affects_current_rule_score'] for row in mismatches),
        'rows':mismatches,'stored_history_modified':False}
    results.append(result)
    print(json.dumps(result,ensure_ascii=False))
(root/'historical-bar-mismatches.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
