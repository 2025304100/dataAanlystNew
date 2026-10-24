import {chromium} from '../../frontend/node_modules/playwright/index.mjs';
import {createHash} from 'node:crypto';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const base='http://127.0.0.1:8000',root='D:/ai_project/dataAanlystNew/tmp/inspection-durable-request-2026-10-24',profile=root+'/isolated-profile-'+Date.now();
const json=async path=>{const r=await fetch(base+path);assert(r.ok,`${r.status} ${path}`);return r.json();};
const state=async()=>{const mode=await json('/api/v1/inspection-acceptance/status'),rt=await json('/api/v1/portfolios/1/inspection/runtime'),h=await json('/api/v1/portfolios/1/inspection/history?page_size=1'),q=await json('/api/v1/portfolios/1/inspection/reconciliation-queue'),list=await json('/api/v1/portfolios');return {mode,permissions:rt.permissions,can_confirm:rt.recovery_summary.can_confirm,audit:h.snapshot_event_id,verified_through:q.verified_through,coverage_start:q.coverage_start,portfolio_ids:list.map(x=>x.id)};};
const out={before:await state(),checks:[],reads:[],simulatedWrites:[],blockedWrites:[],errors:[],httpErrors:[],faults:[],locks:[],boundary:'Actual main GETs with browser-only repair plans, task lists and accepted-task response. The only POST is recorded and held in the browser, never forwarded. Isolated Chrome persistent profile tests refresh, tab closure, new tab and full browser restart without accessing the user profile.'};
for(const key of ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed'])assert.equal(out.before.mode[key],false);
let context,releaseSubmit=null,accepted=null,showTask=false,failLookup=false,finished=false;const labels=new Map();
const originalTask=()=>({task_id:accepted.taskId,portfolio_id:1,status:finished?'done':'running',stage:'browser-fixture',percent:finished?100:10,total:1,processed:finished?1:0,ok_count:finished?1:0,failed_count:0,message:'原请求的已完成修复任务（仅浏览器）',objects:[],retryable_operation_ids:[],audit_refs:[]});
async function start(){
 context=await chromium.launchPersistentContext(profile,{headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe',viewport:{width:1440,height:950},serviceWorkers:'block'});
 context.on('page',p=>{p.setDefaultTimeout(25000);p.on('pageerror',e=>out.errors.push(e.message));p.on('response',r=>{if(r.url().includes('/api/')&&r.status()>=400&&!r.headers()['x-inspection-qa-fault'])out.httpErrors.push({url:r.url(),status:r.status()});});});
 await context.route('**/api/**',async route=>{
  const req=route.request(),url=new URL(req.url()),method=req.method(),tab=labels.get(req.frame().page());
  if(!['GET','HEAD','OPTIONS'].includes(method)){
   if(method==='POST'&&url.pathname==='/api/v1/portfolios/1/inspection/repairs'){
    const body=req.postDataJSON(),taskId=createHash('sha256').update(JSON.stringify({key:body.idempotency_key,portfolio_id:1})).digest('hex').slice(0,32);
    accepted={body,taskId};out.simulatedWrites.push({tab,forwarded:false,taskId,key:body.idempotency_key});await new Promise(r=>{releaseSubmit=r;});releaseSubmit=null;try{await route.abort('failed');}catch{}return;
   }
   out.blockedWrites.push({tab,method,url:req.url()});return route.abort();
  }
  out.reads.push({tab,path:url.pathname,at:Date.now()});
  if(url.pathname.endsWith('/inspection/data-diagnostics')){
   const r=await route.fetch(),v=await r.json(),s=v.symbols[0];s.evidence=[{...s.evidence[0],status:'MISSING',check_code:'DAILY_BAR',label:'浏览器验收行情',reason:'仅浏览器原请求恢复场景',depends_on:[]}];v.symbols=[s];v.repair_plan.operations=[{operation_id:'browser-only-op',kind:'sync_daily_bars',label:'仅浏览器模拟修复',symbol_id:s.symbol_id,symbol:s.symbol,trade_date:s.expected_trade_date,enabled:true,depends_on:[],config_id:null,config_version:null,target:'settings:data-center',resolves_evidence_ids:[],expected_effect:'仅浏览器，不写业务数据'}];return route.fulfill({response:r,json:v});
  }
  if(url.pathname==='/api/v1/portfolios/1/inspection/repairs')return route.fulfill({status:200,json:{portfolio_id:1,items:showTask?[{...originalTask(),task_id:'unrelated-newer-task',message:'第一页的其他任务',status:'running'}]:[],next_cursor:showTask?'more-history':null}});
  if(/\/inspection\/repairs\/[a-f0-9]{32}$/.test(url.pathname)){
   const id=url.pathname.split('/').pop();assert(accepted&&id===accepted.taskId,'must read exact original task');
   if(failLookup){out.faults.push({tab,status:503,path:url.pathname});return route.fulfill({status:503,headers:{'content-type':'application/json','x-inspection-qa-fault':'1'},json:{detail:'验收模拟：原任务暂不可读取'}});}
   if(!showTask){out.faults.push({tab,status:404,path:url.pathname});return route.fulfill({status:404,headers:{'content-type':'application/json','x-inspection-qa-fault':'1'},json:{detail:'验收模拟：原任务尚未可见'}});}
   return route.fulfill({status:200,json:originalTask()});
  }
  return route.continue();
 });
 await context.route('**/*',async route=>{const u=new URL(route.request().url());if(u.origin===base&&(u.pathname==='/'||u.pathname.startsWith('/assets/'))){const file=u.pathname==='/'?'D:/ai_project/dataAanlystNew/app/web/dist/index.html':`D:/ai_project/dataAanlystNew/app/web/dist${u.pathname}`;return route.fulfill({status:200,contentType:file.endsWith('.html')?'text/html':file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'application/octet-stream',body:fs.readFileSync(file)});}return route.fallback();});
}
const page=async name=>{const p=await context.newPage();labels.set(p,name);p.setDefaultTimeout(25000);return p;};
const click=(node,name)=>node.getByRole('button',{name,exact:true}).click();
const dialog=(p,title)=>p.getByRole('dialog',{name:new RegExp(title+'$')});
const check=text=>{out.checks.push(text);console.log(text);};
const until=async fn=>{const end=Date.now()+18000;while(!fn()){assert(Date.now()<end,'request not observed');await new Promise(r=>setTimeout(r,30));}};
async function workspace(p){
 await p.waitForLoadState('networkidle');
 if(await p.getByRole('region',{name:'组合数据批量修复工作区'}).isVisible().catch(()=>false))return;
 const existing=dialog(p,'整合数据缺口与批量修复');
 if(!await existing.isVisible().catch(()=>false)){
   await p.getByText('组合交易',{exact:true}).first().click();await p.getByText('检查与对账',{exact:true}).first().click();await p.getByText('新买入已暂停',{exact:true}).waitFor();await p.waitForLoadState('networkidle');
 }
 if(await existing.isVisible().catch(()=>false))await click(existing,'前往数据中心批量修复');
 else if(await p.getByRole('button',{name:'查看原请求与任务',exact:true}).isVisible().catch(()=>false))await click(p,'查看原请求与任务');
 else{await click(p,'打开数据修复');await click(dialog(p,'整合数据缺口与批量修复'),'前往数据中心批量修复');}
 await p.getByRole('region',{name:'组合数据批量修复工作区'}).waitFor();await p.waitForLoadState('networkidle');
}
const record=async p=>p.evaluate(()=>JSON.parse(localStorage.getItem('portfolio-inspection-repair-request-v1:1')));
let a,b,c;
try{
 await start();a=await page('sender');b=await page('other-tab');await a.goto(base+'/');await workspace(a);await b.goto(base+'/');await workspace(b);
 const all=a.getByRole('checkbox',{name:'全选筛选结果',exact:true});if(!await all.isChecked())await all.check();await click(a,'预览批量修复（1 步）');const confirm=dialog(a,'确认本批数据修复计划');await confirm.getByRole('checkbox').check();await click(confirm,'确认执行本批计划');await until(()=>releaseSubmit&&accepted);
 const saved=await record(b);assert(saved.request.idempotency_key===accepted.body.idempotency_key&&saved.taskId===accepted.taskId);assert(!('confirmed' in saved.request));
 await b.getByText(/暂未查到原任务/).waitFor();assert(await b.getByRole('button',{name:'预览批量修复（1 步）',exact:true}).isDisabled());
 const available=await b.evaluate(()=>navigator.locks.request('inspection-repair-submit:1',{ifAvailable:true},lock=>Boolean(lock)));out.locks.push({while_sender_pending:available});assert.equal(available,false);check('发送前原请求已持久保存，另一标签立即保留同一标识并暂停另起请求；真实Web Locks确认发送中互斥');
 await a.close();releaseSubmit?.();await until(()=>releaseSubmit===null);const released=await b.evaluate(()=>navigator.locks.request('inspection-repair-submit:1',{ifAvailable:true},lock=>Boolean(lock)));out.locks.push({after_sender_closed:released});assert.equal(released,true);
 await b.reload({waitUntil:'domcontentloaded'});await workspace(b);await b.getByText(/暂未查到原任务/).waitFor();assert((await record(b)).request.idempotency_key===accepted.body.idempotency_key);assert.equal(out.simulatedWrites.length,1);assert.equal(await b.getByRole('dialog').count(),0);check('发起页关闭后刷新另一页，仍按原标识精确GET；404不清除原记录、不恢复确认、不重提');
 failLookup=true;await click(b,'重新读取原请求对应任务');await b.getByText(/原任务暂时无法核实/).waitFor();assert((await record(b)).taskId===accepted.taskId);assert(await b.getByRole('button',{name:'预览批量修复（1 步）',exact:true}).isDisabled());await b.screenshot({path:root+'/unknown-original-request.png',animations:'disabled'});check('原任务503时原请求继续保留，不能换新标识；有明确只读重试入口');
 failLookup=false;showTask=true;finished=true;await click(b,'重新读取原请求对应任务');await b.getByRole('heading',{name:originalTask().message,exact:true}).waitFor();assert((await record(b)).state==='known');await b.screenshot({path:root+'/reloaded-original-task.png',animations:'disabled'});check('原任务恢复可见后精确找回已完成任务，即使最近任务第一页是其他任务；只GET、提交总数仍1');
 c=await page('new-tab');await c.goto(base+'/');await workspace(c);await c.getByRole('heading',{name:originalTask().message,exact:true}).waitFor();assert((await record(c)).taskId===accepted.taskId);assert.equal(await c.getByRole('dialog').count(),0);check('新标签页重新进入同一组合，恢复同一个原任务，不继承确认勾选，也不创建新请求');
 await c.waitForLoadState('networkidle');await b.waitForLoadState('networkidle');await context.unrouteAll({behavior:'wait'});await context.close();context=null;await start();const reopened=await page('reopened-browser');await reopened.goto(base+'/');await workspace(reopened);await reopened.getByRole('heading',{name:originalTask().message,exact:true}).waitFor();assert((await record(reopened)).request.idempotency_key===accepted.body.idempotency_key);await reopened.screenshot({path:root+'/browser-reopened-original-task.png',animations:'disabled'});check('关闭隔离浏览器并用同一持久配置重新打开，原标识与原任务仍可找回，期间没有重放POST');
 out.after=await state();assert.deepEqual(out.after,out.before);assert.equal(out.simulatedWrites.length,1);assert.equal(out.blockedWrites.length,0);assert.equal(out.errors.length,0);assert.equal(out.httpErrors.length,0);out.passed=true;
}catch(error){out.passed=false;out.error=error.stack;console.error(error);for(const [name,p] of [['sender',a],['other-tab',b],['new-tab',c]])if(p&&!p.isClosed())await p.screenshot({path:`${root}/failure-${name}.png`});process.exitCode=1;}
finally{releaseSubmit?.();if(context){await context.unrouteAll({behavior:'wait'});await context.close();}fs.writeFileSync(root+'/browser-result.json',JSON.stringify(out,null,2));}
