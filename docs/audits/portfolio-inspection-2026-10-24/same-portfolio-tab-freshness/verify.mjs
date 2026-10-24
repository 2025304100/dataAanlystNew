import {chromium} from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const base='http://127.0.0.1:8000',root='D:/ai_project/dataAanlystNew/tmp/inspection-same-portfolio-tabs-2026-10-24';
const json=async path=>{const r=await fetch(base+path);assert(r.ok,`${r.status} ${path}`);return r.json();};
const state=async()=>{const mode=await json('/api/v1/inspection-acceptance/status'),rt=await json('/api/v1/portfolios/1/inspection/runtime'),h=await json('/api/v1/portfolios/1/inspection/history?page_size=1'),q=await json('/api/v1/portfolios/1/inspection/reconciliation-queue'),list=await json('/api/v1/portfolios');return {mode,permissions:rt.permissions,can_confirm:rt.recovery_summary.can_confirm,audit:h.snapshot_event_id,verified_through:q.verified_through,coverage_start:q.coverage_start,portfolio_ids:list.map(x=>x.id)};};
const out={before:await state(),checks:[],simulatedWrites:[],blockedWrites:[],errors:[],httpErrors:[],faults:[],boundary:'Both tabs use actual main. Reconcile POST responses are fulfilled only in the browser; no write reaches the backend. Shared-input notifications and changed report versions are browser fixtures; actual fresh GETs still read main. Review confirmation safety is additionally covered by isolated tests.'};
for(const key of ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed'])assert.equal(out.before.mode[key],false);
const browser=await chromium.launch({headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe'});
const context=await browser.newContext({viewport:{width:1440,height:950}});
let pendingWrite=null,writeNo=0,failRuntime=false,holdRuntime=false,pendingRead=null;
const versions=new Map();
context.on('page',page=>{page.setDefaultTimeout(20000);page.on('pageerror',e=>out.errors.push(e.message));page.on('response',r=>{if(r.url().includes('/api/')&&r.status()>=400&&!r.headers()['x-inspection-qa-fault'])out.httpErrors.push({url:r.url(),status:r.status()});});});
await context.route('**/api/**',async route=>{
 const req=route.request(),url=new URL(req.url()),method=req.method();
 if(!['GET','HEAD','OPTIONS'].includes(method)){
  if(method==='POST'&&url.pathname==='/api/v1/portfolios/1/reconcile'){
   const payload=req.postDataJSON(),date=payload.trade_date;assert(date);out.simulatedWrites.push({method,url:req.url(),date,forwarded:false});
   const response=await json(`/api/v1/portfolios/1/reconciliation-preview?trade_date=${date}`);
   await new Promise(resolve=>{pendingWrite=resolve;});pendingWrite=null;versions.set(date,`browser-v${++writeNo}`);
   return route.fulfill({status:200,json:{...response,report_version:versions.get(date)}});
  }
  out.blockedWrites.push({method,url:req.url()});return route.abort();
 }
 if(url.pathname==='/api/v1/portfolios/1/inspection/runtime'){
  if(failRuntime){failRuntime=false;out.faults.push({status:503,path:url.pathname});return route.fulfill({status:503,headers:{'content-type':'application/json','x-inspection-qa-fault':'1'},json:{detail:'验收模拟：当前依据读取暂时失败'}});}
  if(holdRuntime){holdRuntime=false;const response=await route.fetch();await new Promise(resolve=>{pendingRead=resolve;});pendingRead=null;return route.fulfill({response});}
 }
 if(url.pathname.endsWith('/reconciliation-preview')){const response=await route.fetch(),value=await response.json();return route.fulfill({response,json:{...value,report_version:versions.get(value.trade_date)??value.report_version}});}
 return route.continue();
});
const a=await context.newPage(),b=await context.newPage();
const dialog=(page,title)=>page.getByRole('dialog',{name:new RegExp(title+'$')});
const click=(parent,name)=>parent.getByRole('button',{name,exact:true}).click();
const check=text=>{out.checks.push(text);console.log(text);};
const until=async predicate=>{const end=Date.now()+20000;while(!predicate()){assert(Date.now()<end,'held request not reached');await new Promise(r=>setTimeout(r,30));}};
async function home(page){await page.goto(base+'/',{waitUntil:'domcontentloaded'});await page.getByText('组合交易',{exact:true}).first().click();await page.getByText('检查与对账',{exact:true}).first().click();await page.getByText('新买入已暂停',{exact:true}).waitFor();}
async function daily(page){await click(page,'每日对账');const node=dialog(page,'执行与对账');await node.getByTestId('recon-verdict').waitFor();return node;}
async function choose(page,date){const node=dialog(page,'执行与对账');await node.locator('.ant-select-selector').click();await node.getByRole('combobox',{name:'核对交易日'}).fill(date);await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option').filter({hasText:date}).click();await node.getByTestId('recon-verdict').getByText(date,{exact:true}).waitFor();}
async function updated(page,pid=null){await page.evaluate(pid=>{localStorage.setItem(`portfolio-inspection-change-v1:${pid??'all'}`,JSON.stringify({version:1,id:crypto.randomUUID(),source:'browser-shared-input-update',portfolioId:pid,at:Date.now()}));},pid);}
try{
 await home(a);await home(b);const an=await daily(a),bn=await daily(b);await choose(a,'2026-09-29');await choose(b,'2026-09-30');
 // Force this first notification to travel via BroadcastChannel without localStorage.
 await a.evaluate(()=>{window.__restoreStorageSet=Storage.prototype.setItem;Storage.prototype.setItem=function(key,value){if(key.startsWith('portfolio-inspection-change-v1:'))throw new Error('QA storage unavailable');return window.__restoreStorageSet.call(this,key,value);};});
 await click(an,'重跑当前交易日核对');await until(()=>pendingWrite);await bn.getByTestId('inspection-stale-notice').waitFor();assert((await bn.getByRole('combobox',{name:'核对交易日'}).locator('..').locator('..').innerText()).includes('2026-09-30'));
 await b.screenshot({path:root+'/main-b-expired.png',animations:'disabled'});check('两个真实main标签页：A开始重检时，B立即提示旧依据过期并保留09-30日期；A的localStorage通知故障时BroadcastChannel仍送达');
 pendingWrite();await an.getByRole('button',{name:'重跑当前交易日核对',exact:true}).waitFor();await until(()=>pendingWrite===null);await a.evaluate(()=>{Storage.prototype.setItem=window.__restoreStorageSet;});
 failRuntime=true;await click(bn,'重新读取当前依据');await until(()=>!failRuntime);await bn.getByRole('button',{name:'重新读取当前依据',exact:true}).waitFor();await b.waitForTimeout(200);assert(await bn.getByTestId('inspection-stale-notice').isVisible());check('B重新读取遇到受控503后，过期提示继续保留，未恢复旧报告审核资格');
 holdRuntime=true;await click(bn,'重新读取当前依据');await until(()=>pendingRead);await click(an,'重跑当前交易日核对');await until(()=>pendingWrite);await bn.getByTestId('inspection-stale-notice').waitFor();pendingRead();await until(()=>pendingRead===null);await b.waitForTimeout(200);assert(await bn.getByTestId('inspection-stale-notice').isVisible());pendingWrite();await until(()=>pendingWrite===null);await b.waitForTimeout(250);check('B刷新未完成时A再次重检；B的旧刷新晚返回不能消除过期状态，更新完成通知继续要求重读');
 await click(bn,'重新读取当前依据');await bn.getByTestId('inspection-stale-notice').waitFor({state:'hidden'});await bn.getByTestId('recon-verdict').getByText('2026-09-30',{exact:true}).waitFor();
 await click(bn,'本日处理记录');const history=dialog(b,'处理记录与审核历史');await history.getByRole('region',{name:'历史查询结果'}).waitFor();await history.locator('.ant-drawer-close').click();await history.waitFor({state:'hidden'});check('最新依据完整读取成功后解除前端过期标记，原日期与本日历史入口正常；没有提交任何审核');
 await bn.locator('.ant-drawer-close').click();await bn.waitFor({state:'hidden'});await click(b,'打开数据修复');const data=dialog(b,'整合数据缺口与批量修复');await data.getByRole('textbox',{name:'搜索待核实标的'}).fill('510300');
 await updated(a,2);await b.waitForTimeout(200);assert.equal(await data.getByTestId('inspection-stale-notice').count(),0);
 await updated(a);await data.getByTestId('inspection-stale-notice').waitFor();assert.equal(await data.getByRole('textbox',{name:'搜索待核实标的'}).inputValue(),'510300');await click(data,'重新读取当前依据');await data.getByTestId('inspection-stale-notice').waitFor({state:'hidden'});assert.equal(await data.getByRole('textbox',{name:'搜索待核实标的'}).inputValue(),'510300');check('共享数据更新通知使当前数据依据过期，刷新保留510300筛选；其他组合专属通知不干扰main');
 await data.locator('.ant-drawer-close').click();await data.waitFor({state:'hidden'});await daily(b);await b.screenshot({path:root+'/main-b-reread.png',animations:'disabled'});
 // New context with BroadcastChannel unavailable: real storage-event fallback.
 const fallback=await browser.newContext({viewport:{width:1440,height:950}});await fallback.addInitScript(()=>{window.BroadcastChannel=undefined;});
 fallback.on('page',page=>{page.setDefaultTimeout(20000);page.on('pageerror',e=>out.errors.push(e.message));});
 await fallback.route('**/api/**',async route=>{const req=route.request();if(['GET','HEAD','OPTIONS'].includes(req.method()))return route.continue();out.blockedWrites.push({method:req.method(),url:req.url()});return route.abort();});
 const fa=await fallback.newPage(),fb=await fallback.newPage();await home(fa);await home(fb);const fbn=await daily(fb);await updated(fa,1);await fbn.getByTestId('inspection-stale-notice').waitFor();await click(fbn,'重新读取当前依据');await fbn.getByTestId('inspection-stale-notice').waitFor({state:'hidden'});check('BroadcastChannel不可用时，两个main标签页通过storage事件完成过期通知与只读刷新');await fallback.close();
 out.after=await state();assert.deepEqual(out.after,out.before);assert.equal(out.blockedWrites.length,0);assert.equal(out.errors.length,0);assert.equal(out.httpErrors.length,0);out.passed=true;
}catch(error){out.passed=false;out.error=error.stack;console.error(error);await a.screenshot({path:root+'/failure-a.png'});await b.screenshot({path:root+'/failure-b.png'});process.exitCode=1;}
finally{pendingRead?.();pendingWrite?.();fs.writeFileSync(root+'/browser-result.json',JSON.stringify(out,null,2));await browser.close();}
