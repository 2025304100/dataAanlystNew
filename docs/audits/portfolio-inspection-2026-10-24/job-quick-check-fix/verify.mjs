import { chromium } from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const root='D:/ai_project/dataAanlystNew/tmp/inspection-job-quick-fix-2026-10-24';
const base='http://127.0.0.1:8000';
const json=async path=>{const r=await fetch(base+path);assert(r.ok,`${r.status} ${path}`);return r.json();};
const state=async()=>{const mode=await json('/api/v1/inspection-acceptance/status'),runtime=await json('/api/v1/portfolios/1/inspection/runtime'),history=await json('/api/v1/portfolios/1/inspection/history?page_size=1'),queue=await json('/api/v1/portfolios/1/inspection/reconciliation-queue');return {mode,permissions:runtime.permissions,can_confirm:runtime.recovery_summary.can_confirm,audit:history.snapshot_event_id,verified_through:queue.verified_through,coverage_start:queue.coverage_start};};
const before=await state();for(const key of ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed'])assert.equal(before.mode[key],false);
const result={before,reads:[],writes:[],errors:[],httpErrors:[],checks:[]};
const browser=await chromium.launch({headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe'});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
let delayNext=false;
page.on('pageerror',e=>result.errors.push(e.message));
page.on('response',r=>{if(r.url().includes('/api/')&&r.status()>=400)result.httpErrors.push({url:r.url(),status:r.status()});});
await page.route('**/api/**',async route=>{
 const req=route.request();if(!['GET','HEAD','OPTIONS'].includes(req.method())){result.writes.push(req.url());return route.abort();}
 if(/reconciliation-preview|inspection\/history/.test(req.url()))result.reads.push(req.url());
 if(delayNext&&req.url().includes('reconciliation-preview')&&new URL(req.url()).searchParams.get('trade_date')==='2026-09-29'){delayNext=false;await new Promise(resolve=>setTimeout(resolve,1800));}
 return route.continue();
});
const job=page.locator('.pi-inspection-drawer[role="dialog"]').filter({has:page.locator('.pi-job-panel')});
const modal=page.locator('.pi-quick-check');
async function settledModal(){await modal.waitFor({state:'visible'});await page.waitForFunction(()=>getComputedStyle(document.querySelector('.pi-quick-check')).transform==='none');}
async function checkDay(date){await settledModal();await modal.getByText('历史执行资料不完整',{exact:true}).waitFor();assert((await modal.innerText()).includes(date));assert.equal(await page.getByRole('combobox',{name:'核对交易日'}).count(),0);assert(await job.isVisible());assert.equal(await job.evaluate(node=>getComputedStyle(node).pointerEvents),'none');}
async function closeQuick(){await modal.getByRole('button',{name:/^关\s*闭$/}).click();await modal.waitFor({state:'detached'});await page.waitForFunction(()=>{const node=document.querySelector('.pi-job-panel')?.closest('[role="dialog"]');return node&&node.contains(document.activeElement)&&getComputedStyle(node).pointerEvents!=='none';});}
try{
 await page.goto(base+'/',{waitUntil:'domcontentloaded'});await page.getByText('组合交易',{exact:true}).first().click();await page.getByText('检查与对账',{exact:true}).first().click();await page.getByText('新买入已暂停',{exact:true}).waitFor();
 await page.getByRole('button',{name:'每日对账',exact:true}).click();const execution=page.getByRole('dialog',{name:/执行与对账$/});await execution.getByText('历史执行资料不完整',{exact:true}).waitFor();
 await execution.getByRole('button',{name:'自动核对作业',exact:true}).click();await job.getByRole('button',{name:'核查 2026-09-29',exact:true}).waitFor();
 delayNext=true;await job.getByRole('button',{name:'核查 2026-09-29',exact:true}).click();await settledModal();await modal.getByText('正在读取本日核对材料…',{exact:true}).waitFor();await page.screenshot({path:root+'/loading.png',animations:'disabled'});
 await checkDay('2026-09-29');await page.screenshot({path:root+'/fixed.png',animations:'disabled'});
 assert(result.reads.some(url=>url.includes('reconciliation-preview?trade_date=2026-09-29')));
 for(const width of [390,320]){await page.setViewportSize({width,height:900});await settledModal();const layout=await modal.evaluate(node=>{const b=node.getBoundingClientRect(),body=node.querySelector('.pi-quick-body');return{width:innerWidth,document:document.documentElement.scrollWidth,left:b.left,right:b.right,body:body.clientWidth,bodyScroll:body.scrollWidth};});assert(layout.document<=width&&layout.left>=0&&layout.right<=width&&layout.bodyScroll<=layout.body+1,JSON.stringify(layout));await page.screenshot({path:root+`/width-${width}.png`,animations:'disabled'});}
 await page.setViewportSize({width:1440,height:1000});await settledModal();await page.keyboard.press('Escape');await modal.waitFor({state:'detached'});assert(await job.isVisible());await page.waitForFunction(()=>document.querySelector('.pi-job-panel')?.closest('[role="dialog"]').contains(document.activeElement));
 result.checks.push('已有09-29执行抽屉时，同日核查仍新开居中弹窗；读取时有加载状态；Escape只关闭弹窗并返回原按钮，作业仍在');
 await job.locator('.pi-surface-disclosure > summary').filter({hasText:'逐日核对记录'}).click();const row=job.getByRole('cell',{name:'2026-09-30',exact:true}).locator('..');const rowButton=row.getByRole('button',{name:'核查此交易日',exact:true});await rowButton.scrollIntoViewIfNeeded();const scroll=await job.locator('.ant-drawer-body').evaluate(node=>node.scrollTop);
 await rowButton.click();await checkDay('2026-09-30');await modal.getByRole('button',{name:'查看缺口与查找方法',exact:true}).click();await modal.getByText('当天的自动运行记录与决策明细',{exact:true}).waitFor();
 await modal.getByRole('button',{name:'查看本日处理记录',exact:true}).click();const history=page.getByRole('dialog',{name:/本日处理记录/});await history.getByRole('region',{name:'历史查询结果'}).waitFor();assert(result.reads.some(url=>url.includes('/inspection/history?')&&url.includes('trade_date_from=2026-09-30')&&url.includes('report_version=')));await history.getByRole('button',{name:'返回快捷核对',exact:true}).click();await settledModal();await closeQuick();
 assert.equal(await job.locator('.pi-job-day-list').evaluate(node=>node.closest('details').open),true);assert(Math.abs(scroll-await job.locator('.ant-drawer-body').evaluate(node=>node.scrollTop))<2);
 await job.locator('.ant-drawer-close').click();await execution.getByRole('combobox',{name:'核对交易日'}).waitFor();assert((await execution.getByTestId('recon-verdict').innerText()).includes('2026-09-29'));
 await execution.locator('.ant-drawer-close').click();await execution.waitFor({state:'hidden'});await page.getByRole('button',{name:'查看作业',exact:true}).click();await job.getByRole('button',{name:'核查 2026-09-29',exact:true}).click();await checkDay('2026-09-29');await closeQuick();
 result.checks.push('逐日核查09-30有独立报告及相关历史，关闭保留作业展开和滚动；下层09-29报告不被改写；主页面直接进入作业也可核查');
 result.after=await state();assert.deepEqual(result.after,before);assert.equal(result.writes.length,0);assert.equal(result.errors.length,0);assert.equal(result.httpErrors.length,0);result.passed=true;
}catch(error){result.passed=false;result.error=error.message;await page.screenshot({path:root+'/failure.png',animations:'disabled'});process.exitCode=1;}
finally{fs.writeFileSync(root+'/browser-result.json',JSON.stringify(result,null,2));console.log(JSON.stringify({passed:result.passed,error:result.error,checks:result.checks,writes:result.writes,errors:result.errors,httpErrors:result.httpErrors},null,2));await browser.close();}
