import {chromium} from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const base='http://127.0.0.1:8000',root='D:/ai_project/dataAanlystNew/tmp/inspection-background-completion-2026-10-24';
const json=async path=>{const r=await fetch(base+path);assert(r.ok,`${r.status} ${path}`);return r.json();};
const state=async()=>{const mode=await json('/api/v1/inspection-acceptance/status'),rt=await json('/api/v1/portfolios/1/inspection/runtime'),h=await json('/api/v1/portfolios/1/inspection/history?page_size=1'),q=await json('/api/v1/portfolios/1/inspection/reconciliation-queue'),list=await json('/api/v1/portfolios');return {mode,permissions:rt.permissions,can_confirm:rt.recovery_summary.can_confirm,audit:h.snapshot_event_id,verified_through:q.verified_through,coverage_start:q.coverage_start,portfolio_ids:list.map(x=>x.id)};};
const out={before:await state(),checks:[],reads:[],simulatedWrites:[],blockedWrites:[],errors:[],httpErrors:[],faults:[],boundary:'Real main GETs with browser-only task/snapshot fixtures. Repair and reconciliation-job submit POSTs are fulfilled directly in the browser and never forwarded. No business tasks, trading, reviews, baseline or admission mutations run on the backend.'};
for(const key of ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed'])assert.equal(out.before.mode[key],false);
const browser=await chromium.launch({headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe'}),context=await browser.newContext({viewport:{width:1440,height:950}}),labels=new Map();
let phase='idle',failRuntime=false;const active=()=>['repair-running','job-running'].includes(phase);
const task=()=>({task_id:'browser-only-repair',portfolio_id:1,status:phase==='repair-running'?'running':'done',stage:'browser-fixture',percent:phase==='repair-running'?10:100,total:1,processed:phase==='repair-running'?0:1,ok_count:phase==='repair-running'?0:1,failed_count:0,message:'仅浏览器验收任务',objects:[],retryable_operation_ids:[],audit_refs:[]});
context.on('page',page=>{page.setDefaultTimeout(25000);page.on('pageerror',e=>out.errors.push(e.message));page.on('response',r=>{if(r.url().includes('/api/')&&r.status()>=400&&!r.headers()['x-inspection-qa-fault'])out.httpErrors.push({url:r.url(),status:r.status()});});});
await context.route('**/api/**',async route=>{
 const req=route.request(),url=new URL(req.url()),method=req.method(),tab=labels.get(req.frame().page());
 if(!['GET','HEAD','OPTIONS'].includes(method)){
  if(method==='POST'&&tab==='origin-repair'&&url.pathname==='/api/v1/portfolios/1/inspection/repairs'){
   phase='repair-running';out.simulatedWrites.push({tab,url:req.url(),forwarded:false,phase,at:Date.now()});return route.fulfill({status:200,json:task()});
  }
  if(method==='POST'&&tab==='origin-job'&&url.pathname==='/api/v1/portfolios/1/inspection/reconciliation-job/recheck'){
   phase='job-running';out.simulatedWrites.push({tab,url:req.url(),forwarded:false,phase,at:Date.now()});return route.fulfill({status:200,json:{portfolio_id:1,reconciliation_passed:false,dispatch_status:'DISPATCHED',task_id:'browser-only-job'}});
  }
  out.blockedWrites.push({tab,method,url:req.url()});return route.abort();
 }
 out.reads.push({tab,path:url.pathname,at:Date.now(),phase});
 if(tab==='origin-repair'&&url.pathname==='/api/v1/portfolios/1/inspection/repairs')return route.fulfill({status:200,json:{portfolio_id:1,items:[],next_cursor:null}});
 if(url.pathname==='/api/v1/portfolios/1/inspection/runtime'){
  if(tab==='receiver'&&failRuntime){failRuntime=false;out.faults.push({tab,path:url.pathname,status:503});return route.fulfill({status:503,headers:{'content-type':'application/json','x-inspection-qa-fault':'1'},json:{detail:'验收模拟：后台最新依据暂不可读取'}});}
  const r=await route.fetch(),v=await r.json();if(v.guide){v.guide.snapshot_id=`browser-${phase}-${v.guide.snapshot_id}`;if(active()){const step=v.guide.steps.find(s=>s.key===(phase==='repair-running'?'data':'reconciliation'));step.status='RUNNING';step.tasks=[{task_id:'browser-only-task',task_type:phase==='repair-running'?'portfolio_data_repair':'portfolio_auto_simulation',status:'running'}];}}
  return route.fulfill({response:r,json:v});
 }
 if(url.pathname==='/api/v1/portfolios/1/inspection/reconciliation-job'){
  const r=await route.fetch(),v=await r.json();v.status=phase==='job-running'?'running':phase==='job-failed'?'failed':'idle';v.task_id=phase.startsWith('job')?'browser-only-job':null;
  if(phase.startsWith('job'))v.work_items=[{trade_date:'2026-09-30',work_state:phase==='job-running'?'running':phase==='job-failed'?'failed':'done',requested_evidence_version:2,processed_evidence_version:phase==='job-running'?1:2,attempt_count:1,last_error_code:phase==='job-failed'?'BROWSER_FIXTURE_MISSING_EVIDENCE':null}];
  if(tab==='origin-job')v.source_events={count:1,status:'pending',waiting_for_coverage:false,events:[{id:'browser-only-event',source_table:'decision_runs',source_key:'browser-only-source',operation:'update',status:'pending',created_at:'2026-10-24T06:00:00Z'}]};
  return route.fulfill({response:r,json:v});
 }
 if(url.pathname==='/api/v1/portfolios/1/inspection/data-diagnostics'&&tab==='origin-repair'){
  const r=await route.fetch(),v=await r.json(),s=v.symbols[0];assert(s);s.evidence=[{...s.evidence[0],status:'MISSING',check_code:'DAILY_BAR',label:'浏览器验收行情',reason:'仅浏览器任务提交场景',depends_on:[]}];v.symbols=[s];
  v.repair_plan.operations=[{operation_id:'browser-only-repair-op',kind:'sync_daily_bars',label:'仅浏览器模拟修复',symbol_id:s.symbol_id,symbol:s.symbol,trade_date:s.expected_trade_date,enabled:true,depends_on:[],config_id:null,config_version:null,target:'settings:data-center',resolves_evidence_ids:[],expected_effect:'仅浏览器验收，不写业务数据'}];
  return route.fulfill({response:r,json:v});
 }
 if(url.pathname==='/api/v1/portfolios/1/inspection/repairs/browser-only-repair')return route.fulfill({status:200,json:task()});
 return route.continue();
});
const page=async name=>{const p=await context.newPage();labels.set(p,name);return p;};
// The backend currently proxies Vite when it is available. Pin this acceptance to the built assets.
await context.route('**/*',async route=>{
 const url=new URL(route.request().url());
 if(url.origin===base&&(url.pathname==='/'||url.pathname.startsWith('/assets/'))){
  const file=url.pathname==='/'?'D:/ai_project/dataAanlystNew/app/web/dist/index.html':`D:/ai_project/dataAanlystNew/app/web/dist${url.pathname}`;
  return route.fulfill({status:200,contentType:file.endsWith('.html')?'text/html':file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'application/octet-stream',body:fs.readFileSync(file)});
 }
 return route.fallback();
});
const a=await page('origin-repair'),b=await page('receiver');let a2;
const click=(node,name)=>node.getByRole('button',{name,exact:true}).click();
const dialog=(p,title)=>p.getByRole('dialog',{name:new RegExp(title+'$')});
const check=text=>{out.checks.push(text);console.log(text);};
async function home(p){await p.goto(base+'/',{waitUntil:'domcontentloaded'});await p.getByText('组合交易',{exact:true}).first().click();await p.getByText('检查与对账',{exact:true}).first().click();await p.getByText('新买入已暂停',{exact:true}).waitFor();}
async function daily(p){await click(p,'每日对账');const d=dialog(p,'执行与对账');await d.getByTestId('recon-verdict').waitFor();return d;}
async function stale(node){await node.getByTestId('inspection-stale-notice').waitFor({timeout:16000});}
async function reread(node){await click(node,'重新读取当前依据');await node.getByTestId('inspection-stale-notice').waitFor({state:'hidden'});}
try{
 await home(b);const bn=await daily(b);await home(a);await click(a,'打开数据修复');await click(dialog(a,'整合数据缺口与批量修复'),'前往数据中心批量修复');await a.getByRole('region',{name:'组合数据批量修复工作区'}).waitFor();await a.waitForLoadState('networkidle');const all=a.getByRole('checkbox',{name:'全选筛选结果',exact:true});if(!await all.isChecked())await all.check();await click(a,'预览批量修复（1 步）');const confirm=a.getByRole('dialog',{name:'确认本批数据修复计划'});await confirm.getByRole('checkbox').check();await click(confirm,'确认执行本批计划');await a.getByRole('heading',{name:'仅浏览器验收任务'}).waitFor();await a.close();out.origin_closed_at=Date.now();await b.bringToFront();await stale(bn);await reread(bn);
 const doneAt=Date.now();phase='repair-done';await stale(bn);out.repair_detection_ms=Date.now()-doneAt;assert.equal(out.reads.filter(r=>r.tab==='origin-repair'&&r.at>doneAt).length,0);
 await b.screenshot({path:root+'/repair-completed-origin-closed.png',animations:'disabled'});check('异步修复从真实入口仅浏览器模拟提交，发起页关闭后，接收页独立GET发现完成并提示过期，发起页无后续轮询');await reread(bn);

 a2=await page('origin-job');await home(a2);await daily(a2);await click(dialog(a2,'执行与对账'),'自动核对作业');const job=dialog(a2,'自动对账作业与逐日记录');await job.locator('summary').filter({hasText:'来源事件待消费'}).click();await click(job,'处理来源事件并登记重检');await a2.getByText('已提交证据核对任务，请查看逐日进度；不会执行交易。',{exact:true}).waitFor();await job.locator('.ant-drawer-close').click();await job.waitFor({state:'hidden'});const originExecution=dialog(a2,'执行与对账');await originExecution.locator('.ant-drawer-close').click();await originExecution.waitFor({state:'hidden'});await a2.getByText('设置',{exact:true}).first().click();await a2.waitForLoadState('networkidle');out.origin_left_at=Date.now();await b.bringToFront();await stale(bn);await reread(bn);
 const failedAt=Date.now();phase='job-failed';await stale(bn);out.job_failure_detection_ms=Date.now()-failedAt;
 assert.equal(out.reads.filter(r=>r.tab==='origin-job'&&r.at>failedAt&&/inspection\/(runtime|reconciliation-job)$/.test(r.path)).length,0);check('自动对账任务从真实入口仅浏览器模拟提交，发起页切到设置后，接收页独立发现失败/结果变化，未将任务结束当作核对通过');await reread(bn);

 await b.evaluate(()=>{window.__qaVisible=false;Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>window.__qaVisible?'visible':'hidden'});document.dispatchEvent(new Event('visibilitychange'));});const suspendedAt=Date.now();phase='job-done';await b.waitForTimeout(5500);
 assert.equal(out.reads.filter(r=>r.tab==='receiver'&&r.at>suspendedAt+300&&/inspection\/(runtime|reconciliation-job)$/.test(r.path)).length,0);
 const returnedAt=Date.now();await b.evaluate(()=>{window.__qaVisible=true;document.dispatchEvent(new Event('visibilitychange'));window.dispatchEvent(new Event('focus'));});await stale(bn);out.resume_detection_ms=Date.now()-returnedAt;check('接收页挂起时停止轮询，返回后立即独立核实，发现挂起期间的结果变化；不依赖发起页通知');await reread(bn);

 failRuntime=true;await b.evaluate(()=>window.dispatchEvent(new Event('focus')));await stale(bn);assert(!failRuntime);await b.screenshot({path:root+'/backend-check-failed.png',animations:'disabled'});check('独立后台核验遇到503时按未知处理，暂停旧审核并提供重新读取；不会复用旧结论继续审核');await reread(bn);

 const idleDoneAt=Date.now();phase='silent-completion';await bn.getByTestId('inspection-stale-notice').waitFor({timeout:40000});out.idle_detection_ms=Date.now()-idleDoneAt;
 assert.equal(out.reads.filter(r=>r.tab==='origin-job'&&r.at>idleDoneAt&&/inspection\/(runtime|reconciliation-job)$/.test(r.path)).length,0);check('没有运行中任务提示、没有发起页通知时，接收页仍通过平时30秒独立检查发现新依据');await reread(bn);await b.screenshot({path:root+'/receiver-reread.png',animations:'disabled'});
 out.after=await state();assert.deepEqual(out.after,out.before);assert.equal(out.simulatedWrites.length,2);assert.equal(out.blockedWrites.length,0);assert.equal(out.errors.length,0);assert.equal(out.httpErrors.length,0);out.passed=true;
}catch(error){out.passed=false;out.error=error.stack;console.error(error);if(!a.isClosed())await a.screenshot({path:root+'/failure-a.png'});await b.screenshot({path:root+'/failure-b.png'});if(a2&&!a2.isClosed())await a2.screenshot({path:root+'/failure-origin-job.png'});process.exitCode=1;}
finally{fs.writeFileSync(root+'/browser-result.json',JSON.stringify(out,null,2));await browser.close();}
