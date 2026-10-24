"""Capture current main inspection without any database or API writes."""
from pathlib import Path
import json
from urllib.request import urlopen

root=Path(__file__).parent
base='http://127.0.0.1:8000/api/v1'
paths={'mode':'/inspection-acceptance/status',
       'diagnostics':'/portfolios/1/inspection/data-diagnostics',
       'runtime':'/portfolios/1/inspection/runtime',
       'guide':'/portfolios/1/inspection/guide',
       'job':'/portfolios/1/inspection/reconciliation-job'}
results={}
for name,path in paths.items():
    try:
        with urlopen(base+path,timeout=30) as response:
            value=json.load(response)
        results[name]=value
        (root/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as error:
        results[name]={'error':str(error)}
report={}
for name,value in results.items():
    if name=='diagnostics':
        report[name]={'summary':value['summary'],'binding':value['binding'],
            'symbols':[{'symbol':s['symbol'],'name':s['name'],'scoring':s['scoring'],
                'evidence':[e for e in s['evidence'] if e['status'] not in ['PASSED','NOT_APPLICABLE','NOT_DUE']]}
                for s in value['symbols']],
            'evidence':value['evidence'],'repair_plan':value['repair_plan']}
    elif name=='guide':
        report[name]=value
    elif name=='job':
        report[name]=value
    else:report[name]=value
print(json.dumps(report,ensure_ascii=False))
