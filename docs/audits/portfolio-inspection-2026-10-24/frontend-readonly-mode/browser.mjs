import {chromium} from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const root='D:/ai_project/dataAanlystNew/tmp/inspection-frontend-readonly-2026-10-24',base='http://127.0.0.1:8000',phase=process.argv[2]??'before-restart';
let profile;if(phase==='before-restart'){profile=root+'/isolated-profile-'+Date.now();fs.writeFileSync(root+'/profile.txt',profile);}else profile=fs.readFileSync(root+'/profile.txt','utf8');
const out={phase,checks:[],writes:[],errors:[],httpErrors:[],fixtures:[],boundary:'Actual main reads and actual server mode. GET-only virtual portfolio 2 for switching; GET-only mode 503 injection. No mutation interception, no proxy, no database writes.'};
let modeFailure=false;
const ctx=await chromium.launchPersistentContext(profile,{headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe',viewport:{width:1440,height:1000},serviceWorkers:'block'});
ctx.on('page',p=>{p.setDefaultTimeout(25000);p.on('pageerror',e=>out.errors.push(e.message));p.on('request',r=>{if(r.url().includes('/api/')&&!['GET','HEAD','OPTIONS'].includes(r.method()))out.writes.push({method:r.method(),path:new URL(r.url()).pathname});});p.on('response',r=>{if(r.url().includes('/api/')&&r.status()>=400&&!r.url().includes('/reconciliation-job/requests/')&&!r.headers()['x-inspection-qa-fault'])out.httpErrors.push({path:new URL(r.url()).pathname,status:r.status()});});});
const clone=(v)=>{if(Array.isArray(v))return v.map(clone);if(v&&typeof v==='object')return Object.fromEntries(Object.entries(v).map(([k,x])=>[k,k==='portfolio_id'&&x===1?2:clone(x)]));return v;};
await ctx.route('**/api/**',async route=>{
 const req=route.request(),u=new URL(req.url());if(req.method()!=='GET')return route.continue();
 if(u.pathname==='/api/v1/inspection-acceptance/status'&&modeFailure){out.fixtures.push('mode503');return route.fulfill({status:503,headers:{'x-inspection-qa-fault':'1'},json:{message:'browser-only mode-read failure'}});}
 if(u.pathname==='/api/v1/portfolios'){const r=await route.fetch(),v=await r.json();assert(v.some(x=>x.id===1));return route.fulfill({response:r,json:[...v,{...v.find(x=>x.id===1),id:2,name:'只读切换验收组合',is_default:0}]});}
 if(u.searchParams.get('portfolio_id')==='2'&&!u.pathname.startsWith('/api/v1/portfolios/2')){out.fixtures.push(u.pathname+'?portfolio_id=2');u.searchParams.set('portfolio_id','1');const r=await route.fetch({url:u.href});return route.fulfill({response:r,json:clone(await r.json())});}
 if(/^\/api\/v1\/portfolios\/2(?:\/|$)/.test(u.pathname)){out.fixtures.push(u.pathname);const r=await route.fetch({url:base+u.pathname.replace('/portfolios/2','/portfolios/1')+u.search});let value=clone(await r.json());if(u.pathname==='/api/v1/portfolios/2')value={...value,id:2,name:'只读切换验收组合'};return route.fulfill({response:r,json:value});}
 return route.continue();
});
await ctx.route(base+'/',r=>r.fulfill({contentType:'text/html',body:fs.readFileSync('D:/ai_project/dataAanlystNew/app/web/dist/index.html')}));
await ctx.route(base+'/assets/**',r=>{const path=new URL(r.request().url()).pathname;return r.fulfill({contentType:path.endsWith('.js')?'application/javascript':path.endsWith('.css')?'text/css':'application/octet-stream',body:fs.readFileSync('D:/ai_project/dataAanlystNew/app/web/dist'+path)});});
await ctx.addInitScript(({origin})=>{if(location.origin!==origin)return;const key='portfolio-inspection-recheck-request-v1:1';if(!localStorage.getItem(key))localStorage.setItem(key,JSON.stringify({version:1,portfolioId:1,requestKey:'readonly-view-only-original-request',tradeDate:null,createdAt:Date.now()}));},{origin:base});
const check=s=>{out.checks.push(s);console.log(s);};
async function enter(p){await p.waitForLoadState('networkidle');if(!await p.getByText('新买入已暂停',{exact:true}).isVisible().catch(()=>false)){await p.getByText('组合交易',{exact:true}).first().click();await p.getByText('检查与对账',{exact:true}).first().click();}await p.getByText('新买入已暂停',{exact:true}).waitFor();await p.getByText('只读验收模式',{exact:true}).first().waitFor();}
async function disabled(p){const buttons=p.locator('button[data-acceptance-mutation]:visible');await buttons.first().waitFor();const n=await buttons.count();assert(n>0);for(let i=0;i<n;i++){assert(await buttons.nth(i).isDisabled());assert((await buttons.nth(i).getAttribute('title')).includes('只读验收'));}return n;}
async function closeLayers(p){for(let i=0;i<6;i++){await p.keyboard.press('Escape');await p.waitForTimeout(260);}}
async function job(p){if(!await p.getByRole('region',{name:'自动核对作业详情'}).isVisible().catch(()=>false))await p.getByRole('button',{name:'查看作业',exact:true}).click();await p.getByRole('region',{name:'自动核对作业详情'}).waitFor();}
let p;
try{
 p=await ctx.newPage();await p.goto(base+'/');await enter(p);
 if(phase==='before-restart'){
  await p.screenshot({path:root+'/readonly-overview.png',animations:'disabled'});check('页面明确显示只读验收模式，服务状态来自真实后端。');
  await job(p);const original=p.getByRole('region',{name:'原对账请求恢复'});await original.getByRole('button',{name:'使用原请求重新核对'}).waitFor();await disabled(p);assert(await original.getByRole('button',{name:'重新读取原对账请求'}).isEnabled());await original.getByRole('button',{name:'重新读取原对账请求'}).click();await p.screenshot({path:root+'/readonly-job.png',animations:'disabled'});check('原请求重试已禁用并说明原因，原请求只读重读保持可用，不发POST。');
  await p.getByRole('button',{name:'核查 2026-09-29',exact:true}).click();await p.getByRole('button',{name:'重新核对本日'}).waitFor();await disabled(p);assert(await p.getByRole('button',{name:'查看本日处理记录'}).isEnabled());await p.getByRole('button',{name:'查看本日处理记录'}).click();await p.getByRole('heading',{name:'本日处理记录',exact:true}).waitFor();check('快捷核对的重检按钮禁用，查看处理历史仍能打开抽屉。');await closeLayers(p);
  await p.getByRole('button',{name:'打开数据修复',exact:true}).click();await p.getByRole('button',{name:'前往数据中心批量修复',exact:true}).click();await p.getByRole('region',{name:'组合数据批量修复工作区'}).waitFor();await disabled(p);assert(await p.getByRole('button',{name:'重新读取数据诊断'}).isEnabled());await p.getByRole('button',{name:'重新读取数据诊断'}).click();await p.screenshot({path:root+'/readonly-data-repair.png',animations:'disabled'});check('独立数据修复工作区的修复入口也禁用，数据诊断读取和查看入口继续可用。');
  await p.getByText('组合交易',{exact:true}).first().click();await closeLayers(p);await p.getByText('检查与对账',{exact:true}).first().click();await closeLayers(p);await p.locator('.pt-portfolio-selector').click();await p.getByRole('option').filter({hasText:'只读切换验收组合'}).click();await p.getByText('新买入已暂停',{exact:true}).waitFor();await job(p);assert.equal(await p.getByRole('region',{name:'原对账请求恢复'}).count(),0);await p.getByRole('button',{name:'核查 2026-09-29',exact:true}).click();await p.getByRole('button',{name:'重新核对本日'}).waitFor();await disabled(p);check('切换到GET模拟的第二组合后共享只读状态，不串入main的原请求；没有创建真实组合。');
  await closeLayers(p);await p.locator('.pt-portfolio-selector').click();await p.getByRole('option').filter({hasText:'main'}).click();await p.getByText('新买入已暂停',{exact:true}).waitFor();await job(p);await disabled(p);await p.reload({waitUntil:'domcontentloaded'});await enter(p);await job(p);await disabled(p);check('切回main并刷新后，原请求及只读禁用状态一致。');
  modeFailure=true;await p.getByRole('status',{name:'验收服务模式'}).last().getByRole('button',{name:'重新读取模式'}).click();await p.getByText('服务暂不可读，继续保持只读保护。',{exact:true}).first().waitFor();await disabled(p);modeFailure=false;await p.getByRole('status',{name:'验收服务模式'}).last().getByRole('button',{name:'重新读取模式'}).click();await p.waitForLoadState('networkidle');await disabled(p);check('模式GET失败时继续禁用，恢复后只读状态保持，不自动执行任何任务。');
 }else{
  await job(p);await p.getByRole('region',{name:'原对账请求恢复'}).getByRole('button',{name:'使用原请求重新核对'}).waitFor();await disabled(p);assert(await p.getByRole('button',{name:'重新读取原对账请求'}).isEnabled());await p.screenshot({path:root+'/readonly-after-restart.png',animations:'disabled'});check('后端与隔离浏览器重启后，页面仍明确只读，修改按钮禁用，原请求与历史读取入口可用。');
 }
 assert.deepEqual(out.writes,[]);assert.deepEqual(out.errors,[]);assert.deepEqual(out.httpErrors,[]);out.passed=true;
}catch(e){out.passed=false;out.error=e.stack;console.error(e);if(p&&!p.isClosed())await p.screenshot({path:root+'/failure-'+phase+'.png'});process.exitCode=1;}
finally{await ctx.unrouteAll({behavior:'wait'});await ctx.close();fs.writeFileSync(root+'/browser-'+phase+'.json',JSON.stringify(out,null,2));}




