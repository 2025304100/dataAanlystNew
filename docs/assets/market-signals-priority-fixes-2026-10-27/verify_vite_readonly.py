from datetime import datetime, timezone
from pathlib import Path
import json
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

folder=Path(__file__).resolve().parent/'vite-readonly-final'
folder.mkdir(exist_ok=True)
origin='http://127.0.0.1:5173'
errors, responses, blocked=[],[],[]
report={'passed':False,'origin':origin,'started_at':datetime.now(timezone.utc).isoformat(),'writes':0}
with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True)
    context=browser.new_context(viewport={'width':1512,'height':982},locale='zh-CN',timezone_id='Asia/Shanghai')
    def read_only(route):
        request=route.request
        if urlsplit(request.url).netloc!=urlsplit(origin).netloc or request.method not in ('GET','HEAD','OPTIONS'):
            blocked.append({'method':request.method,'path':urlsplit(request.url).path})
            route.abort()
        else:
            route.continue_()
    context.route('**/*',read_only)
    page=context.new_page()
    page.on('pageerror',lambda error: errors.append(str(error)))
    page.on('response',lambda response: responses.append({'path':urlsplit(response.url).path,'status':response.status})
        if '/api/' in response.url else None)
    try:
        page.goto(origin+'/?tab=news',wait_until='domcontentloaded')
        page.get_by_role('button',name='行情异动提醒',exact=True).click()
        expect(page.locator('.msc-page')).to_be_visible()
        expect(page.locator('.msc-page .ant-spin-spinning')).to_have_count(0)
        page.get_by_role('tab',name='关注设置',exact=True).click()
        expect(page.get_by_role('cell',name='验收专用-股票-20261027-235032',exact=False).first).to_be_visible()
        page.screenshot(path=str(folder/'vite-rules.png'),full_page=True)
        assert not errors and not blocked, {'errors':errors,'blocked':blocked}
        assert not [r for r in responses if r['status']>=400], responses
        report['passed']=True
    finally:
        report.update(finished_at=datetime.now(timezone.utc).isoformat(),responses=responses,page_errors=errors,blocked_requests=blocked)
        (folder/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()
print(json.dumps({'passed':report['passed'],'api_responses':len(responses),'writes':0}))
