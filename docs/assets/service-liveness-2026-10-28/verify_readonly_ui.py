from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import build_opener,ProxyHandler
import json
from playwright.sync_api import sync_playwright,expect

folder=Path(__file__).resolve().parent
target=folder/'readonly-ui'
target.mkdir(exist_ok=True)
opener=build_opener(ProxyHandler({}))
checks=[]
def get(origin,path):
    with opener.open(origin+path,timeout=10) as response:
        assert response.status==200 and response.headers.get('X-App-Validation-Mode')=='1'
        checks.append({'origin':origin,'path':path,'status':response.status,'validation_mode':'1'})
        return json.load(response)
backend='http://127.0.0.1:8000'
rules=get(backend,'/api/v1/market-signals/rules')
assert len(rules)==6 and all(not row['enabled'] and row['mode']=='shadow' for row in rules)
names=[row['name'] for row in rules[-2:]]
assert not get(backend,'/api/v1/market-signals/settings')['enabled']
for path in ('/health','/api/v1/market-events?limit=1','/api/v1/market-signals/metadata',
             '/api/v1/market-signals/evidence?limit=1','/api/v1/market-signals/assessments',
             '/api/v1/market-signals/predictions','/api/v1/market-signals/dashboard'):
    get(backend,path)
for row,count in zip(rules[-2:],(3,2)):
    assert len(get(backend,f"/api/v1/market-signals/rules/{row['id']}/versions"))==count
for path in ('/api/v1/market-signals/settings','/api/v1/market-signals/rules'):
    get('http://127.0.0.1:5173',path)
reports=[]
with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True)
    for label,origin in (('backend',backend),('frontend','http://127.0.0.1:5173')):
        context=browser.new_context(viewport={'width':1512,'height':1180},locale='zh-CN',timezone_id='Asia/Shanghai')
        context.tracing.start(screenshots=True,snapshots=True,sources=False)
        blocked,errors,responses=[],[],[]
        report={'passed':False,'origin':origin,'writes':0,'started_at':datetime.now(timezone.utc).isoformat()}
        def readonly(route):
            request=route.request
            if request.method not in ('GET','HEAD','OPTIONS') or urlsplit(request.url).netloc!=urlsplit(origin).netloc:
                blocked.append({'path':urlsplit(request.url).path,'method':request.method})
                route.abort()
            else:
                route.continue_()
        context.route('**/*',readonly)
        page=context.new_page()
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('response',lambda response:responses.append({'path':urlsplit(response.url).path,'status':response.status}) if '/api/' in response.url else None)
        try:
            page.goto(origin+'/?tab=news',wait_until='domcontentloaded')
            page.get_by_role('button',name='行情异动提醒',exact=True).click()
            expect(page.locator('.msc-page')).to_be_visible()
            expect(page.locator('.msc-page .ant-spin-spinning')).to_have_count(0)
            page.get_by_role('tab',name='关注设置',exact=True).click()
            for name in names:
                expect(page.get_by_role('cell',name=name,exact=False).first).to_be_visible()
            page.screenshot(path=str(target/(label+'-rules.png')))
            assert not errors and not blocked
            assert not [item for item in responses if item['status']>=400],responses
            report['passed']=True
        finally:
            report.update(responses=responses,page_errors=errors,blocked_requests=blocked,
                finished_at=datetime.now(timezone.utc).isoformat())
            reports.append(report)
            context.tracing.stop(path=str(target/(label+'-trace.zip')))
            context.close()
    browser.close()
result={'passed':all(r['passed'] for r in reports),'writes':0,'http':checks,'pages':reports}
(target/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'passed':result['passed'],'http_checks':len(checks),
    'api_responses':{r['origin']:len(r['responses']) for r in reports},'writes':0}))
