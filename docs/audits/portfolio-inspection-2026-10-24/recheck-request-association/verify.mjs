import {chromium} from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import http from 'node:http';
const upstream='http://127.0.0.1:8000',base='http://127.0.0.1:8018',root='D:/ai_project/dataAanlystNew/tmp/inspection-recheck-request-2026-10-24',profile=root+'/isolated-profile-'+Date.now(),proxyBlocked=[];
// Independent transport barrier: even Playwright route teardown/page close
// cannot forward a mutation to the real application.
const proxy=http.createServer(async(req,res)=>{
 if(!['GET','HEAD','OPTIONS'].includes(req.method)){proxyBlocked.push({method:req.method,path:req.url});res.writeHead(423,{'Content-Type':'application/json','x-inspection-qa-fault':'1'});res.end(JSON.stringify({detail:'QA proxy permits reads only'}));return;}
 try{const r=await fetch(upstream+req.url,{method:req.method});res.writeHead(r.status,{'Content-Type':r.headers.get('content-type')??'application/json'});res.end(Buffer.from(await r.arrayBuffer()));}
 catch{res.writeHead(503,{'Content-Type':'application/json'});res.end('{}');}
});
await new Promise(resolve=>proxy.listen(8018,'127.0.0.1',resolve));
const json=async path=>{const r=await fetch(base+path);assert(r.ok,`${r.status} ${path}`);return r.json();};
async function state(){const mode=await json('/api/v1/inspection-acceptance/status'),rt=await json('/api/v1/portfolios/1/inspection/runtime'),h=await json('/api/v1/portfolios/1/inspection/history?page_size=1'),q=await json('/api/v1/portfolios/1/inspection/reconciliation-queue');return {mode,permissions:rt.permissions,can_confirm:rt.recovery_summary.can_confirm,audit:h.snapshot_event_id,verified_through:q.verified_through,coverage_start:q.coverage_start};}
const out={before:await state(),checks:[],reads:[],simulatedWrites:[],blockedWrites:[],errors:[],httpErrors:[],boundary:'Real main GET only. Source recheck POST and request receipts intercepted in an isolated persistent browser. No actual jobs submitted.'};
for(const key of ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed'])assert.equal(out.before.mode[key],false);
let context,release=null,accepted=null,visible=false,unavailable=false;const labels=new Map();
const receipt=()=>({portfolio_id:1,request_key:accepted.request_key,trade_date:accepted.trade_date??null,task_id:'ab'.repeat(16),dispatch_status:'QUEUED',task_status:'running',reconciliation_passed:false,trading_resumed:false});
async function start(){
 context=await chromium.launchPersistentContext(profile,{headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe',viewport:{width:1440,height:950},serviceWorkers:'block'});
 context.on('page',p=>{p.setDefaultTimeout(25000);p.on('pageerror',e=>out.errors.push(e.message));p.on('response',r=>{if(r.url().includes('/api/')&&r.status()>=400&&!r.headers()['x-inspection-qa-fault'])out.httpErrors.push({url:r.url(),status:r.status()});});});
 await context.route('**/api/**',async route=>{
  const req=route.request(),u=new URL(req.url()),tab=labels.get(req.frame().page());
  if(!['GET','HEAD','OPTIONS'].includes(req.method())){
   if(req.method()==='POST'&&u.pathname==='/api/v1/portfolios/1/inspection/reconciliation-job/recheck'){
    accepted=req.postDataJSON();assert(accepted.request_key);out.simulatedWrites.push({tab,body:accepted,forwarded:false});await new Promise(r=>{release=r;});release=null;try{await route.abort('failed');}catch{}return;
   }out.blockedWrites.push({tab,path:u.pathname,method:req.method()});return route.abort();
  }
  out.reads.push({tab,path:u.pathname});
  if(u.pathname.includes('/inspection/reconciliation-job/requests/')){
   if(accepted)assert(u.pathname.endsWith('/'+accepted.request_key),'must look up the exact original request');
   if(unavailable)return route.fulfill({status:503,headers:{'x-inspection-qa-fault':'1'},json:{detail:{message:'验收模拟：原请求暂不可读取'}}});
   if(!visible)return route.fulfill({status:404,headers:{'x-inspection-qa-fault':'1'},json:{detail:{error_code:'RECHECK_REQUEST_NOT_FOUND',message:'验收模拟：原请求回执尚不可见'}}});
   return route.fulfill({status:200,json:receipt()});
  }
  if(u.pathname.endsWith('/inspection/reconciliation-job')){const r=await route.fetch(),v=await r.json();v.source_events={...(v.source_events??{count:0,events:[]}),waiting_for_coverage:false};return route.fulfill({response:r,json:v});}
  return route.continue();
 });
 await context.route('**/*',async route=>{const u=new URL(route.request().url());if(u.origin===base&&(u.pathname==='/'||u.pathname.startsWith('/assets/'))){const file=u.pathname==='/'?'D:/ai_project/dataAanlystNew/app/web/dist/index.html':`D:/ai_project/dataAanlystNew/app/web/dist${u.pathname}`;return route.fulfill({status:200,contentType:file.endsWith('.html')?'text/html':file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'application/octet-stream',body:fs.readFileSync(file)});}return route.fallback();});
}
const page=async name=>{const p=await context.newPage();labels.set(p,name);return p;};
const check=text=>{out.checks.push(text);console.log(text);};
async function job(p){
 await p.waitForLoadState('networkidle');
 if(await p.getByRole('region',{name:'自动核对作业详情',exact:true}).isVisible().catch(()=>false))return;
 await p.getByText('组合交易',{exact:true}).first().click();await p.getByText('检查与对账',{exact:true}).first().click();await p.getByText('新买入已暂停',{exact:true}).waitFor();await p.waitForLoadState('networkidle');
 if(!await p.getByRole('region',{name:'自动核对作业详情',exact:true}).isVisible().catch(()=>false))await p.getByRole('button',{name:'查看作业',exact:true}).click();
 await p.getByRole('region',{name:'自动核对作业详情',exact:true}).waitFor();
}
async function submitButton(p){const region=p.getByRole('region',{name:'自动核对作业详情',exact:true});const button=region.getByRole('button',{name:'处理来源事件并登记重检',exact:true});if(!await button.isVisible())await region.locator('summary').filter({hasText:'来源事件待消费'}).click();return button;}
const record=async p=>p.evaluate(()=>JSON.parse(localStorage.getItem('portfolio-inspection-recheck-request-v1:1')));
let a,b,c;
try{
 await start();a=await page('sender');b=await page('receiver');await a.goto(base+'/');await job(a);await b.goto(base+'/');await job(b);
 await (await submitButton(a)).click();const deadline=Date.now()+15000;while(!release){assert(Date.now()<deadline);await new Promise(r=>setTimeout(r,30));}
 await b.getByRole('region',{name:'原对账请求恢复'}).waitFor();assert((await record(b)).requestKey===accepted.request_key);assert.equal(await b.evaluate(()=>navigator.locks.request('inspection-recheck-submit:1',{ifAvailable:true},lock=>Boolean(lock))),false);
 check('提交前持久保存原请求与日期；同源另一标签按原键读取，真实 Web Locks 阻止并发新键。');
 await a.close();release?.();await b.reload({waitUntil:'domcontentloaded'});await job(b);const original=b.getByRole('region',{name:'原对账请求恢复'});await original.getByRole('alert').waitFor();assert((await record(b)).requestKey===accepted.request_key);assert.equal(out.simulatedWrites.length,1);
 await b.screenshot({path:root+'/unknown-original-request.png',animations:'disabled'});check('发起页关闭、另一页刷新后仍保留原键；404 不另起请求、不自动重放。');
 unavailable=true;await original.getByRole('button',{name:'重新读取原对账请求',exact:true}).click();await original.getByText('验收模拟：原请求暂不可读取',{exact:true}).waitFor();assert((await record(b)).requestKey===accepted.request_key);assert.equal(await original.getByRole('button',{name:'使用原请求重新核对'}).count(),0);check('后端 503 时只读恢复失败保留原请求，不提供换键提交。');
 unavailable=false;visible=true;await original.getByRole('button',{name:'重新读取原对账请求',exact:true}).click();await original.getByText(/已找回原请求对应任务/).waitFor();await b.screenshot({path:root+'/recovered-original-request.png',animations:'disabled'});
 await (await submitButton(b)).click();await b.waitForLoadState('networkidle');assert.equal(out.simulatedWrites.length,1);check('精确找回后再次点登记只读取原任务；提交总数仍为 1，未按最近作业猜测。');
 c=await page('new-tab');await c.goto(base+'/');await job(c);await c.getByRole('region',{name:'原对账请求恢复'}).getByText(/已找回原请求对应任务/).waitFor();assert((await record(c)).requestKey===accepted.request_key);check('新标签进入同一组合后按同一个原请求恢复，期间不发送 POST。');
 await context.unrouteAll({behavior:'wait'});await context.close();context=null;await start();c=await page('reopened-browser');await c.goto(base+'/');await job(c);await c.getByRole('region',{name:'原对账请求恢复'}).getByText(/已找回原请求对应任务/).waitFor();assert((await record(c)).requestKey===accepted.request_key);await c.screenshot({path:root+'/browser-reopened-original-request.png',animations:'disabled'});check('隔离浏览器整体关闭重开后，持久原标识和原任务仍能只读恢复。');
 out.after=await state();assert.deepEqual(out.after,out.before);assert.equal(out.simulatedWrites.length,1);assert.equal(out.blockedWrites.length,0);assert.deepEqual(out.errors,[]);assert.deepEqual(out.httpErrors,[]);out.passed=true;
}catch(e){out.passed=false;out.error=e.stack;console.error(e);for(const [name,p] of [['sender',a],['receiver',b],['reopen',c]])if(p&&!p.isClosed())await p.screenshot({path:root+'/failure-'+name+'.png'});process.exitCode=1;}
finally{release?.();if(context){await context.unrouteAll({behavior:'wait'});await context.close();}out.proxyBlocked=proxyBlocked;fs.writeFileSync(root+'/browser-result.json',JSON.stringify(out,null,2));await new Promise(resolve=>proxy.close(resolve));}


