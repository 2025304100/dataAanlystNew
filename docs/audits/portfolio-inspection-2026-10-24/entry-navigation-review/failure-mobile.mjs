import {chromium} from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const root='D:/ai_project/dataAanlystNew/tmp/inspection-entry-audit-2026-10-24';
const browser=await chromium.launch({headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe'}),page=await browser.newPage({viewport:{width:1440,height:850}});
page.setDefaultTimeout(15000);
const out={checks:[],injected:[],writes:[],errors:[],layouts:[]};let fault=null;
page.on('pageerror',e=>out.errors.push(e.message));
await page.route('**/api/**',async r=>{const req=r.request();if(!['GET','HEAD','OPTIONS'].includes(req.method())){out.writes.push(req.url());return r.abort();}if(fault&&req.url().includes(fault)){out.injected.push(req.url());fault=null;return r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'验收模拟：本次材料读取暂时失败'})});}return r.continue();});
const d=title=>page.getByRole('dialog',{name:new RegExp(title+'$')});
const settle=async x=>{await x.waitFor({state:'visible'});await page.waitForFunction(n=>{const w=n.closest('.ant-drawer-content-wrapper');return !w||getComputedStyle(w).transform==='none';},await x.elementHandle());};
const click=(x,name)=>x.getByRole('button',{name,exact:true}).click();
const close=async x=>{await x.locator('.ant-drawer-close').click();await x.waitFor({state:'hidden'});};
try{
 await page.goto('http://127.0.0.1:8000/',{waitUntil:'domcontentloaded'});await page.getByText('组合交易',{exact:true}).first().click();await page.getByText('检查与对账',{exact:true}).first().click();await page.getByText('新买入已暂停',{exact:true}).waitFor();
 fault='reconciliation-preview?trade_date=2026-09-29';await click(page,'每日对账');const execution=d('执行与对账');await settle(execution);await execution.getByRole('alert').waitFor();await page.screenshot({path:root+'/read-failure.png',animations:'disabled'});await click(execution,'重新读取核对证据');await execution.getByText('历史执行资料不完整',{exact:true}).waitFor();assert((await execution.getByTestId('recon-verdict').innerText()).includes('2026-09-29'));out.checks.push('模拟所选交易日503，抽屉保留明确错误与原日期重试入口，真实GET重试后恢复结果');
 await click(execution,'自动核对作业');const job=d('自动对账作业与逐日记录');await settle(job);fault='/reconciliation-queue';await click(job,'重新读取作业与逐日记录');await job.getByRole('alert').waitFor();await click(job,'重新读取作业与逐日记录');await job.getByRole('alert').waitFor({state:'hidden'});await close(job);await settle(execution);out.checks.push('模拟作业逐日记录503，错误可见、旧记录保留、重读可恢复');
 await click(execution,'查看并处理历史缺口');const gap=d('历史缺口');await settle(gap);fault='/evidence-supplements?';await click(gap,'查看可关联的原订单');const evidence=d('补证关联与本人审核');await settle(evidence);await evidence.getByRole('alert').waitFor();await click(evidence,'刷新实际补证来源');await evidence.getByText('尚未找到满足条件的自动执行来源',{exact:true}).waitFor();await evidence.getByRole('alert').waitFor({state:'hidden'});await close(evidence);await settle(gap);await close(gap);await settle(execution);await close(execution);out.checks.push('模拟原订单来源503，有可见错误和刷新入口；来源为空也能回历史缺口，不形成死结');
 for(const width of [390,320]){
  await page.setViewportSize({width,height:850});
  for(const [entry,title] of [['打开数据修复','整合数据缺口与批量修复'],['运行状态审核','运行状态审核'],['每日对账','执行与对账'],['查看作业','自动对账作业与逐日记录'],['审计记录','处理记录与审核历史']]){
   await click(page,entry);const surface=d(title);await settle(surface);const size=await surface.evaluate(n=>{const body=n.querySelector('.ant-drawer-body'),close=n.querySelector('.ant-drawer-close').getBoundingClientRect();return {width:innerWidth,document:document.documentElement.scrollWidth,body:body.clientWidth,content:body.scrollWidth,closeRight:close.right,closeTop:close.top};});assert(size.document<=width&&size.content<=size.body+1&&size.closeRight<=width&&size.closeTop>=0,JSON.stringify({title,...size}));out.layouts.push({title,...size});if(width===320&&entry==='运行状态审核')await page.screenshot({path:root+'/review-320.png',animations:'disabled'});await close(surface);
  }
 }
 assert.equal(out.writes.length,0);assert.equal(out.errors.length,0);assert.equal(out.injected.length,3);out.passed=true;
}catch(e){out.passed=false;out.error=e.stack;await page.screenshot({path:root+'/failure-mobile-debug.png',animations:'disabled'});console.error(e);process.exitCode=1;}
finally{fs.writeFileSync(root+'/failure-mobile-result.json',JSON.stringify(out,null,2));console.log(JSON.stringify({passed:out.passed,checks:out.checks,layouts:out.layouts.length,writes:out.writes}));await browser.close();}
