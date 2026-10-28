from pathlib import Path
from datetime import datetime,timezone
from urllib.parse import urlsplit
import json
from playwright.sync_api import sync_playwright,expect

folder=Path(__file__).resolve().parent
target=folder/'vite-readonly'
target.mkdir(exist_ok=True)
ui=json.loads((folder/'business-ui/ui-result.json').read_text(encoding='utf-8'))
names=[row['name'] for row in ui['created_rules']]
responses,errors,blocked=[],[],[]
origin='http://127.0.0.1:5173'
report={'passed':False,'origin':origin,'writes':0,'started_at':datetime.now(timezone.utc).isoformat()}
with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True)
    context=browser.new_context(viewport={'width':1512,'height':1180},locale='zh-CN',timezone_id='Asia/Shanghai')
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
    page.on('response',lambda response:responses.append({'path':urlsplit(response.url).path,'status':response.status})
        if '/api/' in response.url else None)
    try:
        page.goto(origin+'/?tab=news',wait_until='domcontentloaded')
        page.get_by_role('button',name='行情异动提醒',exact=True).click()
        expect(page.locator('.msc-page')).to_be_visible()
        expect(page.locator('.msc-page .ant-spin-spinning')).to_have_count(0)
        page.get_by_role('tab',name='关注设置',exact=True).click()
        for name in names:
            expect(page.get_by_role('cell',name=name,exact=False).first).to_be_visible()
        page.screenshot(path=str(target/'rules.png'))
        assert not errors and not blocked
        assert not [value for value in responses if value['status']>=400],responses
        report['passed']=True
    finally:
        report.update(responses=responses,page_errors=errors,blocked_requests=blocked,finished_at=datetime.now(timezone.utc).isoformat())
        (target/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()
print(json.dumps({'passed':report['passed'],'api_responses':len(responses),'writes':0}))
