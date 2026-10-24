import { chromium } from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';

const root = 'D:/ai_project/dataAanlystNew/tmp/inspection-drawer-style-2026-10-24';
const base = 'http://127.0.0.1:8000';
const json = async path => { const response = await fetch(base + path); assert(response.ok, `${response.status}: ${path}`); return response.json(); };
const state = async () => {
  const mode = await json('/api/v1/inspection-acceptance/status');
  const runtime = await json('/api/v1/portfolios/1/inspection/runtime');
  const history = await json('/api/v1/portfolios/1/inspection/history?page_size=1');
  return { mode, permissions: runtime.permissions, can_confirm: runtime.recovery_summary.can_confirm,
    portfolio_state: runtime.portfolio_state, audit_watermark: history.snapshot_event_id };
};
const before = await state();
assert.equal(before.mode.mode, 'inspection_acceptance');
for (const key of ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed']) assert.equal(before.mode[key], false);
const securities = (await json('/api/v1/portfolios/1/inspection/history/securities')).items;
const result = { before, writeAttempts: [], errors: [], httpErrors: [], checks: [], layouts: {}, historyQueries: [] };
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe' });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.on('pageerror', error => result.errors.push(error.message));
page.on('response', r => { if(r.url().includes('/api/') && r.status() >= 400) result.httpErrors.push({ url:r.url(), status:r.status() }); });
await page.route('**/api/**', async route => {
  const request = route.request();
  if (['GET','HEAD','OPTIONS'].includes(request.method())) {
    if (new URL(request.url()).pathname === '/api/v1/portfolios/1/inspection/history') result.historyQueries.push(request.url());
    return route.continue();
  }
  result.writeAttempts.push({ method:request.method(), url:request.url() });
  return route.abort();
});
async function settled(drawer) {
  await drawer.waitFor({ state:'visible' });
  const handle = await drawer.elementHandle();
  await page.waitForFunction(node => {
    const wrapper=node.closest('.ant-drawer-content-wrapper');
    return !wrapper || getComputedStyle(wrapper).transform === 'none';
  }, handle);
}
async function fit(drawer) {
  const geometry=await drawer.evaluate(node=>{
    const box=node.getBoundingClientRect(), body=node.querySelector('.ant-drawer-body'), close=node.querySelector('.ant-drawer-close').getBoundingClientRect();
    const footer=node.querySelector('.ant-drawer-footer')?.getBoundingClientRect();
    return {viewport:innerWidth,height:innerHeight,doc:document.documentElement.scrollWidth,left:box.left,right:box.right,
      bodyWidth:body.clientWidth,bodyScroll:body.scrollWidth,closeRight:close.right,closeTop:close.top,footerBottom:footer?.bottom};
  });
  assert(geometry.doc<=geometry.viewport && geometry.bodyScroll<=geometry.bodyWidth+1, JSON.stringify(geometry));
  assert(geometry.left>=-1 && geometry.right<=geometry.viewport+1 && geometry.closeRight<=geometry.viewport && geometry.closeTop>=0,JSON.stringify(geometry));
  if(geometry.footerBottom) assert(geometry.footerBottom<=geometry.height+1);
  return geometry;
}
async function screenshots(drawer, name) {
  for(const viewport of [{width:1440,height:1000},{width:1024,height:768},{width:390,height:844},{width:320,height:740}]) {
    await page.setViewportSize(viewport); await settled(drawer);
    await drawer.locator('.ant-drawer-body').evaluate(node=>{node.scrollTop=0;});
    result.layouts[`${name}-${viewport.width}`]=await fit(drawer);
    await drawer.screenshot({path:`${root}/${name}-${viewport.width}.png`,animations:'disabled'});
  }
  await page.setViewportSize({width:1440,height:1000}); await settled(drawer);
}
try {
  await page.goto(base+'/',{waitUntil:'domcontentloaded'});
  await page.getByText('组合交易',{exact:true}).first().click({timeout:20000});
  await page.getByText('检查与对账',{exact:true}).first().click({timeout:20000});
  assert.equal(await page.locator('.pt-portfolio-name').innerText(),'main');
  await page.getByText('新买入已暂停',{exact:true}).waitFor();

  await page.getByRole('button',{name:'运行状态审核',exact:true}).click();
  const runtime=page.getByRole('dialog',{name:/检查与对账.*运行状态审核/}); await settled(runtime);
  await runtime.getByText('仍有问题未解决，暂不能确认恢复',{exact:true}).waitFor();
  await runtime.getByRole('region',{name:'审核剩余问题',exact:true}).waitFor();
  assert(await runtime.getByRole('button',{name:'审核并确认恢复',exact:true}).isDisabled());
  const conclusion=await runtime.getByRole('region',{name:'当前审核结论'}).boundingBox();
  const steps=await runtime.getByRole('navigation',{name:'恢复运行步骤'}).boundingBox();
  assert(conclusion.y<steps.y);
  assert(await runtime.getByRole('region',{name:'确认后的影响'}).isVisible());
  await screenshots(runtime,'runtime');
  await runtime.getByRole('button',{name:'查看历史缺口',exact:true}).click();
  const gap=page.getByRole('dialog',{name:/检查与对账.*历史缺口/}); await settled(gap);
  await gap.getByRole('button',{name:'返回运行状态审核',exact:true}).click(); await settled(runtime);
  assert(await runtime.getByRole('button',{name:'审核并确认恢复',exact:true}).isDisabled());
  await runtime.locator('.ant-drawer-close').click(); await runtime.waitFor({state:'hidden'});
  result.checks.push('运行审核直接展示结论、剩余问题和确认影响；历史缺口可独立查看并返回；恢复按钮保持禁用');

  await page.getByRole('button',{name:'审计记录',exact:true}).click();
  const history=page.getByRole('dialog',{name:/检查与对账.*处理记录与审核历史/}); await settled(history);
  await history.locator('.pi-history-batch-title').first().waitFor();
  await screenshots(history,'history');
  await history.locator('.pi-history-batch-title').first().click();
  const detail=page.getByRole('dialog',{name:/处置批次.*详情/}); await settled(detail);
  await detail.getByRole('region',{name:'当时审核与执行结论'}).waitFor();
  await detail.screenshot({path:root+'/history-detail.png',animations:'disabled'});
  const reportLink=detail.locator('.pi-history-reference-link').filter({hasText:'原报告'}).first();
  if(await reportLink.count()) {
    await reportLink.click();
    const material=page.getByRole('dialog',{name:/原始关联材料预览/}); await settled(material);
    await material.getByRole('region',{name:'历史引用只读材料'}).waitFor();
    await material.screenshot({path:root+'/history-material.png',animations:'disabled'});
    await material.getByRole('button',{name:'返回处置详情',exact:true}).click(); await settled(detail);
  }
  await detail.getByRole('button',{name:'返回历史列表',exact:true}).click(); await settled(history);
  const chosen=securities.slice(0,2); assert.equal(chosen.length,2,'Need two real history securities for the batch query');
  const select=history.getByRole('combobox',{name:'标的名称或代码'});
  for(const security of chosen) {
    await select.click();
    const label=`${security.name||'名称未知'} · ${security.code||security.security_id} (${security.market||security.security_id.split(':')[0]})`;
    await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden)').getByText(label,{exact:true}).click();
  }
  await page.keyboard.press('Escape');
  await history.getByLabel('所属交易日起点',{exact:true}).fill('2026-09-30');
  await history.getByLabel('所属交易日终点',{exact:true}).fill('2026-10-23');
  const filtered=page.waitForResponse(response=>{
    const url=new URL(response.url());return url.pathname==='/api/v1/portfolios/1/inspection/history'&&url.searchParams.getAll('security_id').length===2&&url.searchParams.get('trade_date_from')==='2026-09-30';
  });
  await history.getByRole('button',{name:'查询',exact:true}).click();
  const filteredResponse=await filtered; assert.equal(filteredResponse.status(),200);
  const queryURL=new URL(filteredResponse.url());
  assert.deepEqual(queryURL.searchParams.getAll('security_id').sort(),chosen.map(item=>item.security_id).sort());
  assert.equal(queryURL.searchParams.get('trade_date_to'),'2026-10-23');
  await history.screenshot({path:root+'/history-filtered.png',animations:'disabled'});
  if(await history.locator('.pi-history-batch-title').count()) {
    await history.locator('.pi-history-batch-title').first().click(); await settled(detail);
    await detail.getByRole('button',{name:'返回历史列表',exact:true}).click(); await settled(history);
  }
  assert.equal(await history.getByLabel('所属交易日起点',{exact:true}).inputValue(),'2026-09-30');
  assert.equal(await history.getByLabel('所属交易日终点',{exact:true}).inputValue(),'2026-10-23');
  result.selectedSecurities=chosen;
  result.checks.push('历史列表、批次详情及原材料使用独立抽屉；真实双标的和交易日筛选成功，返回保留筛选');
  await history.locator('.ant-drawer-close').click(); await history.waitFor({state:'hidden'});

  await page.locator('.pt-autotrade-pill').click();
  const readiness=page.getByRole('dialog',{name:'自动交易检查',exact:true});
  await readiness.getByText(/其他待处理事项/).click();
  await readiness.getByRole('button',{name:/2026-09-30.*快捷核对/}).click();
  const quick=page.locator('.pi-quick-check'); await quick.waitFor({state:'visible'});
  await quick.getByRole('button',{name:'查看可关联原订单',exact:true}).click();
  const evidence=page.getByRole('dialog',{name:/本日原订单关联/}); await settled(evidence);
  await evidence.getByText('尚未找到满足条件的自动执行来源',{exact:true}).waitFor();
  assert(await evidence.getByRole('button',{name:'返回历史缺口，查看后续处理',exact:true}).isVisible());
  assert.equal(await evidence.getByRole('textbox').count(),0);
  await screenshots(evidence,'evidence');
  await evidence.getByRole('button',{name:'核查相关历史材料',exact:true}).click();
  const dayHistory=page.getByRole('dialog',{name:/本日处理记录/}); await settled(dayHistory);
  await dayHistory.getByRole('region',{name:'历史查询结果'}).waitFor();
  await dayHistory.screenshot({path:root+'/related-history.png',animations:'disabled'});
  assert(result.historyQueries.some(value=>value.includes('trade_date_from=2026-09-30')&&value.includes('report_version=')));
  await dayHistory.getByRole('button',{name:'返回原订单关联',exact:true}).click(); await settled(evidence);
  await evidence.getByRole('button',{name:'返回历史缺口，查看后续处理',exact:true}).click();
  await quick.waitFor({state:'visible'});
  await evidence.waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.querySelector('.pi-quick-check')?.contains(document.activeElement));
  assert(await quick.locator('.pi-quick-gaps').evaluate(node=>node.open));
  for(let i=0;i<12;i++) { await page.keyboard.press('Tab'); assert(await quick.evaluate(node=>node.contains(document.activeElement))); }
  await page.keyboard.press('Escape'); await quick.waitFor({state:'detached'});
  assert(await readiness.isVisible());
  result.checks.push('原订单关联先显示空来源结论和查找出口，无手工补单表；相关历史按当日和报告定位，逐层返回快捷核对，焦点与Escape正常');
  result.after=await state();
  assert.deepEqual(result.after,before);
  assert.equal(result.writeAttempts.length,0); assert.equal(result.errors.length,0); assert.equal(result.httpErrors.length,0);
  result.passed=true;
} catch(error) {
  result.passed=false; result.error=error.message;
  result.body=(await page.locator('body').innerText()).slice(-5500);
  await page.screenshot({path:root+'/failure.png',animations:'disabled'});
  process.exitCode=1;
} finally {
  fs.writeFileSync(root+'/browser-result.json',JSON.stringify(result,null,2));
  console.log(JSON.stringify(result,null,2));
  await browser.close();
}
