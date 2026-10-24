"""Direct HTTP probes: no browser interception and no proxy."""
import sys,json,uuid,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from migrate_and_annotate import engine,snapshot,safety,KEY,OUT
BASE='http://127.0.0.1:8000'
def request(path,method='GET',payload=None):
    data=json.dumps(payload).encode() if payload is not None else None
    req=urllib.request.Request(BASE+path,data=data,method=method,headers={'Content-Type':'application/json','X-Acceptance-Read-Only':'false'})
    try:
        with urllib.request.urlopen(req,timeout=30) as r:return r.status,json.load(r)
    except urllib.error.HTTPError as e:return e.code,json.load(e)
def main():
    label=sys.argv[1] if len(sys.argv)>1 else 'default-start'
    status,mode=request('/api/v1/inspection-acceptance/status');assert status==200 and mode['read_only'] is True
    for key in ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed','task_registration_allowed','task_dispatch_allowed','mutation_requests_allowed']:assert mode[key] is False
    attempts=[]
    probes=[('/api/v1/portfolios/1/inspection/reconciliation-job/recheck',{'request_key':KEY}),
        ('/api/v1/portfolios/1/inspection/reconciliation-job/recheck',{'request_key':str(uuid.uuid4())}),
        ('/api/v1/portfolios/1/inspection/reconciliation-job/recheck',{}),
        ('/api/v1/portfolios/1/inspection/reconciliation-job/recheck/',{'request_key':str(uuid.uuid4())}),
        ('/api/v1/portfolios/2/inspection/reconciliation-job/recheck',{'request_key':str(uuid.uuid4())}),
        ('/api/v1/portfolios/1/inspection/repairs',{}),('/api/v1/portfolios/1/inspection/evidence-supplements',{}),
        ('/api/v1/portfolios/1/inspection/material-investigations',{}),('/api/v1/portfolios/1/reconcile',{}),
        ('/api/v1/portfolios/1/inspection/guide/review',{}),('/api/v1/portfolios/1/inspection/coverage/accept',{}),
        ('/api/v1/portfolios/1/inspection/coverage/activate',{}),('/api/v1/async-tasks',{})]
    for path,payload in probes:
        code,result=request(path,'POST',payload)
        assert code==423 and result['request_registered'] is False and result['task_dispatched'] is False,(path,code,result)
        attempts.append({'method':'POST','path':path,'status':code})
    for method in ['PUT','PATCH','DELETE']:
        code,result=request('/api/v1/portfolios/1/inspection/reconciliation-job/recheck',method,{})
        assert code==423;attempts.append({'method':method,'status':code})
    reads=[]
    for path in ['/api/v1/portfolios','/api/v1/portfolios/1/inspection/runtime',
        '/api/v1/portfolios/1/inspection/reconciliation-queue','/api/v1/portfolios/1/inspection/reconciliation-job',
        '/api/v1/portfolios/1/inspection/data-diagnostics','/api/v1/portfolios/1/inspection/history?page_size=1',
        '/api/v1/portfolios/1/inspection/reconciliation-job/requests/'+KEY]:
        code,value=request(path);assert code==200,(path,code,value);reads.append({'path':path,'status':code})
        if '/requests/' in path:
            assert value['provenance_annotations'][0]['label']=='验收产生的请求关联'
            assert value['trading_resumed'] is False and value['reconciliation_passed'] is False
    current=safety();assert current==json.loads((OUT/'safety-before.json').read_text(encoding='utf8'))
    (OUT/('http-'+label+'.json')).write_text(json.dumps({'mode':mode,'rejected_mutations':attempts,'reads':reads,'safety':current},ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'phase':label,'rejected':len(attempts),'reads':len(reads),'safety_unchanged':True}))
if __name__=='__main__':main()
