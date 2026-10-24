import {chromium} from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const base='http://127.0.0.1:8000',root='D:/ai_project/dataAanlystNew/tmp/inspection-refresh-isolation-2026-10-24',B=900001,bName='隔离组合 B（仅浏览器验收）';
const json=async p=>{const r=await fetch(base+p);assert(r.ok,`${r.status} ${p}`);return r.json();};
const state=async()=>{const mode=await json('/api/v1/inspection-acceptance/status'),rt=await json('/api/v1/portfolios/1/inspection/runtime'),h=await json('/api/v1/portfolios/1/inspection/history?page_size=1'),q=await json('/api/v1/portfolios/1/inspection/reconciliation-queue'),portfolios=await json('/api/v1/portfolios');return{mode,permissions:rt.permissions,can_confirm:rt.recovery_summary.can_confirm,audit:h.snapshot_event_id,verified_through:q.verified_through,coverage_start:q.coverage_start,portfolio_ids:portfolios.map(x=>x.id)};};
const out={before:await state(),checks:[],writes:[],errors:[],httpErrors:[],reads:[],fixture_reads:[],fixture_note:'本地仅main。B仅在浏览器GET响应中提供，所有B请求映射到main只读响应并替换组合身份；未新建或修改任何业务组合。'};
for(const k of ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed'])assert.equal(out.before.mode[k],false);
assert(!out.before.portfolio_ids.includes(B));
const browser=await chromium.launch({headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe'}),page=await browser.newPage({viewport:{width:1440,height:950}});page.setDefaultTimeout(20000);
let held=null,release=null;
page.on('pageerror',e=>out.errors.push(e.message));page.on('response',r=>{if(r.url().includes('/api/')&&r.status()>=400)out.httpErrors.push({url:r.url(),status:r.status()});});
const replaceId=value=>{if(Array.isArray(value))return value.map(replaceId);if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,k==='portfolio_id'&&v===1?B:replaceId(v)]));return value;};
await page.route('**/api/**',async route=>{
 const req=route.request(),url=new URL(req.url());if(!['GET','HEAD','OPTIONS'].includes(req.method())){out.writes.push({method:req.method(),url:req.url()});return route.abort();}
 out.reads.push(req.url());
 if(held&&url.pathname.endsWith('/portfolios/1/reconciliation-preview')&&url.searchParams.get('trade_date')===held){held=null;await new Promise(r=>{release=r;});}
 if(url.pathname==='/api/v1/portfolios'){
  const r=await route.fetch(),items=await r.json();const main=items.find(x=>x.id===1);return route.fulfill({status:r.status(),json:[...items,{...main,id:B,name:bName,is_test:1,is_default:0,auto_trade_enabled:0}]});
 }
 const isB=url.pathname.includes(`/portfolios/${B}`)||url.searchParams.get('portfolio_id')===String(B);
 if(isB){out.fixture_reads.push(req.url());url.pathname=url.pathname.replace(`/portfolios/${B}`, '/portfolios/1');if(url.searchParams.get('portfolio_id')===String(B))url.searchParams.set('portfolio_id','1');const r=await route.fetch({url:url.toString()});let body=replaceId(await r.json());if(url.pathname==='/api/v1/portfolios/1')body={...body,id:B,name:bName,auto_trade_enabled:0};return route.fulfill({status:r.status(),json:body});}
 return route.continue();
});
const d=title=>page.getByRole('dialog',{name:new RegExp(title+'$')});
const click=(x,name)=>x.getByRole('button',{name,exact:true}).click();
const settled=async x=>{await x.waitFor({state:'visible'});await page.waitForFunction(n=>{const w=n.closest('.ant-drawer-content-wrapper');return !w||getComputedStyle(w).transform==='none';},await x.elementHandle());};
const close=async(x,parent)=>{await x.locator('.ant-drawer-close').click();if(parent)await settled(parent);else await x.waitFor({state:'hidden'});};
const check=t=>{out.checks.push(t);console.log(t);};
const top=x=>x.locator('.ant-drawer-body').evaluate(n=>n.scrollTop);
const sameTop=async(x,target)=>{await settled(x);await page.waitForFunction(({n,target})=>Math.abs(n.scrollTop-target)<3,{n:await x.locator('.ant-drawer-body').elementHandle(),target});};
const choose=async(date,wait=true)=>{const e=d('执行与对账');await e.locator('.ant-select-selector').click();await e.getByRole('combobox',{name:'核对交易日'}).fill(date);await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option').filter({hasText:date}).click();if(wait)await e.getByTestId('recon-verdict').getByText(date,{exact:true}).waitFor();};
const switchTo=async name=>{await page.locator('.pt-portfolio-selector').click();await page.locator('.pt-dropdown').getByRole('option').filter({hasText:name}).click();await page.waitForFunction(name=>document.querySelector('.pt-portfolio-name')?.textContent===name,name);};
try{
 await page.goto(base+'/',{waitUntil:'domcontentloaded'});await page.getByText('组合交易',{exact:true}).first().click();await page.getByText('检查与对账',{exact:true}).first().click();await page.getByText('新买入已暂停',{exact:true}).waitFor();
 await click(page,'打开数据修复');let data=d('整合数据缺口与批量修复');await settled(data);await data.getByRole('textbox',{name:'搜索待核实标的'}).fill('510300');await close(data);
 await click(page,'每日对账');let execution=d('执行与对账');await execution.getByTestId('recon-verdict').waitFor();await choose('2026-09-30');await close(execution);
 await click(page,'审计记录');let history=d('处理记录与审核历史');await settled(history);await history.getByLabel('所属交易日起点',{exact:true}).fill('2026-09-30');await history.getByLabel('所属交易日终点',{exact:true}).fill('2026-10-23');await click(history,'查询');await history.getByRole('region',{name:'历史查询结果'}).waitFor();await close(history);
 await switchTo(bName);assert.equal(await page.getByRole('textbox',{name:'搜索待核实标的'}).count(),0);await click(page,'打开数据修复');data=d('整合数据缺口与批量修复');await settled(data);assert.equal(await data.getByRole('textbox',{name:'搜索待核实标的'}).inputValue(),'');await data.getByRole('textbox',{name:'搜索待核实标的'}).fill('600428');await close(data);await click(page,'每日对账');execution=d('执行与对账');await settled(execution);await execution.getByTestId('recon-verdict').waitFor();await choose('2026-10-20');await close(execution);await click(page,'审计记录');history=d('处理记录与审核历史');await settled(history);await history.getByLabel('所属交易日起点',{exact:true}).fill('2026-10-20');await click(history,'查询');await history.getByRole('region',{name:'历史查询结果'}).waitFor();await close(history);
 await switchTo('main');await click(page,'打开数据修复');data=d('整合数据缺口与批量修复');await settled(data);assert.equal(await data.getByRole('textbox',{name:'搜索待核实标的'}).inputValue(),'510300');await close(data);await click(page,'审计记录');history=d('处理记录与审核历史');await settled(history);assert.equal(await history.getByLabel('所属交易日起点',{exact:true}).inputValue(),'2026-09-30');await close(history);check('浏览器隔离B与main往返：各自搜索和历史交易日恢复，不把A的筛选带到B');
 await click(page,'每日对账');execution=d('执行与对账');await settled(execution);await execution.getByTestId('recon-verdict').waitFor();held='2026-10-21';await choose('2026-10-21',false);await page.waitForFunction(()=>document.querySelector('.pi-execution-panel [role=status]'));await close(execution);await switchTo(bName);await click(page,'每日对账');execution=d('执行与对账');await execution.getByTestId('recon-verdict').getByText('2026-10-20',{exact:true}).waitFor();assert(release);release();release=null;await page.waitForTimeout(600);assert((await execution.getByTestId('recon-verdict').innerText()).includes('2026-10-20'));assert.equal(await page.locator('.pt-portfolio-name').innerText(),bName);check('延迟A的10-21响应，在切到B后放回，B仍为自己的10-20且不会被旧A重新弹窗或覆盖');
 await page.reload({waitUntil:'domcontentloaded'});execution=d('执行与对账');await settled(execution);await execution.getByTestId('recon-verdict').getByText('2026-10-20',{exact:true}).waitFor();assert.equal(await page.locator('.pt-portfolio-name').innerText(),bName);await page.screenshot({path:root+'/fixture-b-after-reload.png',animations:'disabled'});await close(execution);await switchTo('main');await click(page,'每日对账');execution=d('执行与对账');await execution.getByTestId('recon-verdict').getByText('2026-10-21',{exact:true}).waitFor();await close(execution);check('刷新浏览器隔离B仍回B和10-20；返回main回A的10-21，刷新恢复不切回默认组合');
 out.after=await state();assert.deepEqual(out.after,out.before);assert.equal(out.writes.length,0);assert.equal(out.errors.length,0);assert.equal(out.httpErrors.length,0);out.passed=true;
}catch(e){out.passed=false;out.error=e.stack;out.debug=await page.evaluate(()=>({stored:Object.fromEntries(Object.entries(sessionStorage).filter(([k])=>k.includes('browse'))),drawers:[...document.querySelectorAll('.pi-inspection-drawer')].map(n=>({title:n.querySelector('h2')?.textContent,style:n.closest('.ant-drawer')?.getAttribute('style'),root:n.closest('.ant-drawer')?.className}))}));await page.screenshot({path:root+'/isolation-failure.png',animations:'disabled'});console.error(e);process.exitCode=1;}
finally{if(release)release();fs.writeFileSync(root+'/browser-isolation-result.json',JSON.stringify(out,null,2));await browser.close();}
