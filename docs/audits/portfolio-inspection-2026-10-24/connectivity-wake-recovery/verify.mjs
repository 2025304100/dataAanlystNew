import {chromium} from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const base='http://127.0.0.1:8000',root='D:/ai_project/dataAanlystNew/tmp/inspection-connectivity-recovery-2026-10-24';
const json=async path=>{const r=await fetch(base+path);assert(r.ok,`${r.status} ${path}`);return r.json();};
const state=async()=>{const mode=await json('/api/v1/inspection-acceptance/status'),rt=await json('/api/v1/portfolios/1/inspection/runtime'),h=await json('/api/v1/portfolios/1/inspection/history?page_size=1'),q=await json('/api/v1/portfolios/1/inspection/reconciliation-queue'),list=await json('/api/v1/portfolios');return {mode,permissions:rt.permissions,can_confirm:rt.recovery_summary.can_confirm,audit:h.snapshot_event_id,verified_through:q.verified_through,coverage_start:q.coverage_start,portfolio_ids:list.map(x=>x.id)};};
const out={before:await state(),checks:[],reads:[],simulatedWrites:[],blockedWrites:[],errors:[],httpErrors:[],faults:[],networkFailures:[],boundary:'Real main GETs; browser-only offline mode, CDP page freeze plus five-minute browser clock offset, and synthetic lost repair response. Host network/clock/sleep and backend services are unchanged. All actual business writes are blocked.'};
for(const key of ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed'])assert.equal(out.before.mode[key],false);
const browser=await chromium.launch({headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe'}),context=await browser.newContext({viewport:{width:1440,height:950}}),labels=new Map();
let backendFail=false,holdRuntime=false,releaseOld=null,repairAccepted=false;
const originalTask=()=>({task_id:'browser-only-accepted-repair',portfolio_id:1,status:'running',stage:'browser-fixture',percent:10,total:1,processed:0,ok_count:0,failed_count:0,message:'已接收的原修复任务（仅浏览器）',objects:[],retryable_operation_ids:[],audit_refs:[]});
context.on('page',page=>{page.setDefaultTimeout(25000);page.on('pageerror',e=>out.errors.push(e.message));page.on('requestfailed',r=>out.networkFailures.push({tab:labels.get(page),url:r.url(),reason:r.failure()?.errorText}));page.on('response',r=>{if(r.url().includes('/api/')&&r.status()>=400&&!r.headers()['x-inspection-qa-fault'])out.httpErrors.push({url:r.url(),status:r.status()});});});
await context.route('**/api/**',async route=>{
 const req=route.request(),url=new URL(req.url()),method=req.method(),tab=labels.get(req.frame().page());
 if(!['GET','HEAD','OPTIONS'].includes(method)){
  if(method==='POST'&&tab==='repair-origin'&&url.pathname==='/api/v1/portfolios/1/inspection/repairs'){
   repairAccepted=true;out.simulatedWrites.push({method,url:req.url(),forwarded:false,idempotency_key:req.postDataJSON().idempotency_key});return route.abort('failed');
  }
  out.blockedWrites.push({tab,method,url:req.url()});return route.abort();
 }
 out.reads.push({tab,path:url.pathname,at:Date.now()});
 if(tab==='receiver'&&url.pathname.endsWith('/inspection/runtime')){
  if(backendFail){out.faults.push({tab,status:503,path:url.pathname});return route.fulfill({status:503,headers:{'content-type':'application/json','x-inspection-qa-fault':'1'},json:{detail:'验收模拟：后端暂时不可用'}});}
  if(holdRuntime){holdRuntime=false;const r=await route.fetch(),v=await r.json();v.guide.snapshot_id='browser-old-pre-disconnect';await new Promise(resolve=>{releaseOld=resolve;});releaseOld=null;return route.fulfill({response:r,json:v});}
 }
 if(tab==='repair-origin'&&url.pathname.endsWith('/inspection/data-diagnostics')){
  const r=await route.fetch(),v=await r.json(),s=v.symbols[0];s.evidence=[{...s.evidence[0],status:'MISSING',check_code:'DAILY_BAR',label:'浏览器验收行情',reason:'仅浏览器提交结果丢失场景',depends_on:[]}];v.symbols=[s];v.repair_plan.operations=[{operation_id:'browser-only-op',kind:'sync_daily_bars',label:'仅浏览器模拟修复',symbol_id:s.symbol_id,symbol:s.symbol,trade_date:s.expected_trade_date,enabled:true,depends_on:[],config_id:null,config_version:null,target:'settings:data-center',resolves_evidence_ids:[],expected_effect:'仅浏览器，不写业务数据'}];return route.fulfill({response:r,json:v});
 }
 if(tab==='repair-origin'&&url.pathname==='/api/v1/portfolios/1/inspection/repairs')return route.fulfill({status:200,json:{portfolio_id:1,items:repairAccepted?[originalTask()]:[],next_cursor:null}});
 if(tab==='repair-origin'&&url.pathname.endsWith('/inspection/repairs/browser-only-accepted-repair'))return route.fulfill({status:200,json:originalTask()});
 return route.continue();
});
await context.route('**/*',async route=>{const url=new URL(route.request().url());if(url.origin===base&&(url.pathname==='/'||url.pathname.startsWith('/assets/'))){const file=url.pathname==='/'?'D:/ai_project/dataAanlystNew/app/web/dist/index.html':`D:/ai_project/dataAanlystNew/app/web/dist${url.pathname}`;return route.fulfill({status:200,contentType:file.endsWith('.html')?'text/html':file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'application/octet-stream',body:fs.readFileSync(file)});}return route.fallback();});
const page=async name=>{const p=await context.newPage();labels.set(p,name);return p;};
const b=await page('receiver');let a;
const click=(node,name)=>node.getByRole('button',{name,exact:true}).click();
const dialog=(p,title)=>p.getByRole('dialog',{name:new RegExp(title+'$')});
const check=text=>{out.checks.push(text);console.log(text);};
const until=async predicate=>{const end=Date.now()+18000;while(!predicate()){assert(Date.now()<end,'expected request was not observed');await new Promise(r=>setTimeout(r,30));}};
async function home(p){await p.goto(base+'/',{waitUntil:'domcontentloaded'});await p.getByText('组合交易',{exact:true}).first().click();await p.getByText('检查与对账',{exact:true}).first().click();await p.getByText('新买入已暂停',{exact:true}).waitFor();}
async function reread(node){await click(node,'重新读取当前依据');await node.getByTestId('inspection-stale-notice').waitFor({state:'hidden'});}
try{
 await home(b);await b.evaluate(()=>{window.__realNow=Date.now;window.__qaClockOffset=0;Date.now=()=>window.__realNow()+window.__qaClockOffset;});
 await click(b,'每日对账');const bn=dialog(b,'执行与对账');await bn.getByTestId('recon-verdict').waitFor();
 holdRuntime=true;await b.evaluate(()=>window.dispatchEvent(new Event('focus')));await until(()=>releaseOld);
 await context.setOffline(true);await bn.getByTestId('inspection-stale-notice').waitFor();assert.equal(await b.evaluate(()=>navigator.onLine),false);await b.screenshot({path:root+'/offline-expired.png',animations:'disabled'});
 const disconnected=Date.now();await b.waitForTimeout(1300);assert.equal(out.reads.filter(r=>r.tab==='receiver'&&r.at>disconnected+200&&/inspection\/(runtime|reconciliation-job)$/.test(r.path)).length,0);check('浏览器真实离线事件立即使旧依据失效，离线期间暂停独立检查，不等待请求超时');
 const beforeOnline=out.reads.filter(r=>r.tab==='receiver'&&r.path.endsWith('/inspection/runtime')).length;
 await context.setOffline(false);await until(()=>out.reads.filter(r=>r.tab==='receiver'&&r.path.endsWith('/inspection/runtime')).length>beforeOnline);await b.waitForTimeout(400);releaseOld();await until(()=>releaseOld===null);await b.waitForTimeout(200);assert(await bn.getByTestId('inspection-stale-notice').isVisible());
 await reread(bn);check('重连立即只读核实，断网前挂起的旧成功晚返回不能解除过期状态；手动完整读取成功后可继续查看');

 backendFail=true;await b.evaluate(()=>window.dispatchEvent(new Event('focus')));await bn.getByTestId('inspection-stale-notice').waitFor();await click(bn,'重新读取当前依据');await b.waitForTimeout(500);assert(await bn.getByTestId('inspection-stale-notice').isVisible());
 backendFail=false;const recoveredReads=out.reads.filter(r=>r.tab==='receiver'&&r.path.endsWith('/inspection/runtime')).length;await b.evaluate(()=>window.dispatchEvent(new Event('focus')));await until(()=>out.reads.filter(r=>r.tab==='receiver'&&r.path.endsWith('/inspection/runtime')).length>recoveredReads);assert(await bn.getByTestId('inspection-stale-notice').isVisible());await reread(bn);check('后端持续503及失败重读保持旧审核暂停；服务恢复后核实和重新读取成功，不自动恢复同意');

 const cdp=await context.newCDPSession(b);await b.evaluate(()=>{window.__qaClockOffset+=300_000;});await cdp.send('Page.setWebLifecycleState',{state:'frozen'});await new Promise(r=>setTimeout(r,250));await cdp.send('Page.setWebLifecycleState',{state:'active'});await b.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));await bn.getByTestId('inspection-stale-notice').waitFor();await b.screenshot({path:root+'/wake-expired.png',animations:'disabled'});await reread(bn);check('CDP冻结/恢复页面并模拟五分钟时钟停顿后，旧依据必须重新读取；未改变电脑系统时间或使电脑实际休眠');

 a=await page('repair-origin');await home(a);await click(a,'打开数据修复');await click(dialog(a,'整合数据缺口与批量修复'),'前往数据中心批量修复');await a.getByRole('region',{name:'组合数据批量修复工作区'}).waitFor();await a.waitForLoadState('networkidle');const all=a.getByRole('checkbox',{name:'全选筛选结果',exact:true});if(!await all.isChecked())await all.check();await click(a,'预览批量修复（1 步）');const confirm=dialog(a,'确认本批数据修复计划');await confirm.getByRole('checkbox').check();await click(confirm,'确认执行本批计划');await confirm.getByText('提交结果暂时无法确认，请先读取原任务记录，确认是否已接收。系统不会自动重提任务。',{exact:true}).waitFor();assert.equal(await confirm.getByRole('checkbox').isChecked(),false);
 await context.setOffline(true);await context.setOffline(false);await a.evaluate(()=>{window.dispatchEvent(new Event('online'));window.dispatchEvent(new Event('pageshow'));window.dispatchEvent(new Event('focus'));});await a.waitForTimeout(1200);assert.equal(out.simulatedWrites.length,1);
 await click(confirm,'读取原任务记录');await a.getByRole('heading',{name:'已接收的原修复任务（仅浏览器）',exact:true}).waitFor();await a.waitForTimeout(1300);assert.equal(out.simulatedWrites.length,1);await a.screenshot({path:root+'/original-task-after-lost-response.png',animations:'disabled'});check('任务已接收但响应丢失的浏览器模拟：重连/页面恢复未重提，确认勾选清除，通过GET原任务记录找回同一任务，提交总数始终1');
 await b.bringToFront();if(await bn.getByTestId('inspection-stale-notice').count())await reread(bn);await b.screenshot({path:root+'/recovered-main.png',animations:'disabled'});
 out.after=await state();assert.deepEqual(out.after,out.before);assert.equal(out.blockedWrites.length,0);assert.equal(out.errors.length,0);assert.equal(out.httpErrors.length,0);assert.equal(out.simulatedWrites.length,1);out.passed=true;
}catch(error){out.passed=false;out.error=error.stack;console.error(error);await b.screenshot({path:root+'/failure-main.png'});if(a)await a.screenshot({path:root+'/failure-task.png'});process.exitCode=1;}
finally{releaseOld?.();fs.writeFileSync(root+'/browser-result.json',JSON.stringify(out,null,2));await browser.close();}
