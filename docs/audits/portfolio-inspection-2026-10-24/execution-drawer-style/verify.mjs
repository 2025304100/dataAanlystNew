import { chromium } from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';

const root='D:/ai_project/dataAanlystNew/tmp/inspection-execution-style-2026-10-24';
const base='http://127.0.0.1:8000';
const json=async path=>{const response=await fetch(base+path);assert(response.ok,`${response.status}: ${path}`);return response.json();};
const readState=async()=>{
  const mode=await json('/api/v1/inspection-acceptance/status');
  const runtime=await json('/api/v1/portfolios/1/inspection/runtime');
  const status=await json('/api/v1/portfolios/1/status');
  const history=await json('/api/v1/portfolios/1/inspection/history?page_size=1');
  const queue=await json('/api/v1/portfolios/1/inspection/reconciliation-queue');
  return {mode,permissions:runtime.permissions,can_confirm:runtime.recovery_summary.can_confirm,current_state:status.current_state,
    audit_watermark:history.snapshot_event_id,verified_through:queue.verified_through,coverage_start:queue.coverage_start,
    days:queue.days.map(day=>({trade_date:day.trade_date,status:day.status,history_only:day.history_only??false}))};
};
const before=await readState();
assert.equal(before.mode.mode,'inspection_acceptance');
for(const key of ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed'])assert.equal(before.mode[key],false);
const pending=before.days.filter(day=>!day.history_only&&!['PASSED','NOT_DUE'].includes(day.status));
assert(pending.length>=2);
const firstDate=pending[0].trade_date, selectedDate=pending[1].trade_date;
const result={before,writeAttempts:[],errors:[],httpErrors:[],checks:[],layouts:{},scopedReads:[]};
const browser=await chromium.launch({headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe'});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
page.on('pageerror',error=>result.errors.push(error.message));
page.on('response',response=>{if(response.url().includes('/api/')&&response.status()>=400)result.httpErrors.push({url:response.url(),status:response.status()});});
await page.route('**/api/**',async route=>{
  const request=route.request();
  if(['GET','HEAD','OPTIONS'].includes(request.method())){
    if(/reconciliation-preview|inspection\/history/.test(request.url()))result.scopedReads.push(request.url());
    return route.continue();
  }
  result.writeAttempts.push({method:request.method(),url:request.url()});return route.abort();
});
async function settled(drawer){
  await drawer.waitFor({state:'visible'});
  await page.waitForFunction(node=>{const wrapper=node.closest('.ant-drawer-content-wrapper');return !wrapper||getComputedStyle(wrapper).transform==='none';},await drawer.elementHandle());
}
async function fit(drawer){
  const geometry=await drawer.evaluate(node=>{
    const body=node.querySelector('.ant-drawer-body'),box=node.getBoundingClientRect(),close=node.querySelector('.ant-drawer-close').getBoundingClientRect(),footer=node.querySelector('.ant-drawer-footer')?.getBoundingClientRect();
    return {viewport:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth,left:box.left,right:box.right,bodyWidth:body.clientWidth,bodyScrollWidth:body.scrollWidth,closeTop:close.top,closeRight:close.right,footerBottom:footer?.bottom};
  });
  assert(geometry.documentWidth<=geometry.viewport&&geometry.bodyScrollWidth<=geometry.bodyWidth+1,JSON.stringify(geometry));
  assert(geometry.left>=-1&&geometry.right<=geometry.viewport+1&&geometry.closeTop>=0&&geometry.closeRight<=geometry.viewport,JSON.stringify(geometry));
  if(geometry.footerBottom)assert(geometry.footerBottom<=geometry.height+1);
  return geometry;
}
async function capture(drawer,path){
  const box=await drawer.boundingBox();
  await page.screenshot({path,clip:{x:box.x,y:0,width:box.width,height:page.viewportSize().height},animations:'disabled'});
}
async function screenshots(drawer,name){
  for(const size of [{width:1440,height:1000},{width:1024,height:768},{width:390,height:844},{width:320,height:740}]){
    await page.setViewportSize(size);await settled(drawer);
    await drawer.evaluate(node=>{node.scrollTop=0;node.querySelector('.ant-drawer-body').scrollTop=0;});
    result.layouts[`${name}-${size.width}`]=await fit(drawer);
    await capture(drawer,`${root}/${name}-${size.width}.png`);
    result.layouts[`${name}-${size.width}`].afterScreenshot=await drawer.evaluate(node=>{const header=node.querySelector('.ant-drawer-header').getBoundingClientRect(),box=node.getBoundingClientRect();return {top:box.top,bottom:box.bottom,scrollTop:node.scrollTop,headerTop:header.top,headerBottom:header.bottom};});
  }
  await page.setViewportSize({width:1440,height:1000});await settled(drawer);
}
const disclosure=(surface,title)=>surface.locator('.pi-surface-disclosure > summary').filter({hasText:title});
try{
  await page.goto(base+'/',{waitUntil:'domcontentloaded'});
  await page.getByText('组合交易',{exact:true}).first().click({timeout:20000});
  await page.getByText('检查与对账',{exact:true}).first().click({timeout:20000});
  assert.equal(await page.locator('.pt-portfolio-name').innerText(),'main');
  await page.getByText('新买入已暂停',{exact:true}).waitFor();
  await page.getByRole('button',{name:'每日对账',exact:true}).click();
  const execution=page.getByRole('dialog',{name:/执行与对账$/});await settled(execution);
  await execution.getByText('历史执行资料不完整',{exact:true}).waitFor();
  const verdict=execution.getByTestId('recon-verdict');
  assert((await verdict.innerText()).includes(firstDate));
  assert(!/portfolio_id=|trade_date=|DecisionRun|SUCCEEDED/.test(await execution.innerText()));
  assert.equal(await execution.getByRole('button',{name:'审核本次对账结果',exact:true}).count(),0);
  assert.equal(await execution.getByRole('table').count(),0);
  const gapAction=await execution.getByRole('button',{name:'查看并处理历史缺口',exact:true}).boundingBox();
  const known=await disclosure(execution,'查看已读取的核对结果').boundingBox();assert(gapAction.y<known.y);
  await screenshots(execution,'execution');
  await disclosure(execution,'实际资金核验').click();
  await disclosure(execution,'查看已读取的核对结果').click();
  await fit(execution);await capture(execution,root+'/execution-evidence.png');
  await disclosure(execution,'实际资金核验').click();
  await disclosure(execution,'查看已读取的核对结果').click();
  await execution.locator('.pi-execution-date-toolbar .ant-select-selector').click();
  const selectedResponse=page.waitForResponse(response=>response.url().includes('reconciliation-preview')&&new URL(response.url()).searchParams.get('trade_date')===selectedDate);
  await page.locator(`.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title^="${selectedDate}"]`).click();
  const response=await selectedResponse;assert.equal(response.status(),200);assert.equal((await response.json()).trade_date,selectedDate);
  await execution.getByRole('region',{name:'本日执行核对结论'}).getByText(selectedDate,{exact:true}).waitFor();
  result.checks.push('执行核对先显示本日结论与处理入口；缺证不铺空比较；资金和来源按需展开；切换交易日只读取得同日报告');

  await execution.getByRole('button',{name:'查看并处理历史缺口',exact:true}).click();
  const gap=page.getByRole('dialog',{name:/历史缺口$/});await settled(gap);
  await gap.getByRole('region',{name:'历史缺口详情'}).waitFor();
  const gapList=gap.getByRole('list',{name:'系统发现的历史缺口'});
  assert.equal(await gapList.locator(':scope > li').count(),3);
  assert.equal(await gap.getByRole('textbox').count(),0);assert.equal(await gap.getByRole('radio').count(),0);
  const nextAction=await gap.getByRole('button',{name:'查看可关联的原订单',exact:true}).boundingBox();
  assert(nextAction.y<(await gapList.boundingBox()).y);
  await screenshots(gap,'history-gap');
  const firstMethod=gapList.getByText('查看这项依据的来源与查找方法',{exact:true}).first();
  await firstMethod.click();
  const firstDetails=gapList.locator(':scope > li').first().locator('details').first();
  assert(await firstDetails.evaluate(node=>node.open));
  await capture(gap,root+'/history-gap-method.png');
  await gap.getByRole('button',{name:'查看该日查找与处理记录',exact:true}).click();
  const history=page.getByRole('dialog',{name:/处理记录与审核历史$/});await settled(history);
  await history.getByRole('region',{name:'历史查询结果'}).waitFor();
  assert(result.scopedReads.some(url=>url.includes('/inspection/history?')&&url.includes(`trade_date_from=${selectedDate}`)&&url.includes('report_version=')));
  await history.getByRole('button',{name:'返回原问题处理',exact:true}).click();await settled(gap);
  assert(await firstDetails.evaluate(node=>node.open));
  await gap.getByRole('button',{name:'查看可关联的原订单',exact:true}).click();
  const evidence=page.getByRole('dialog',{name:/补证关联与本人审核$/});await settled(evidence);
  await evidence.getByText('尚未找到满足条件的自动执行来源',{exact:true}).waitFor();
  await evidence.getByRole('button',{name:'返回历史缺口，查看后续处理',exact:true}).click();await settled(gap);
  await disclosure(gap,'资料确实无法恢复时').click();
  assert(await gap.getByRole('button',{name:'查看历史缺口处置条件',exact:true}).isVisible());
  await capture(gap,root+'/history-gap-exit.png');
  await gap.getByRole('button',{name:'返回执行与对账',exact:true}).click();await settled(execution);
  assert((await execution.getByTestId('recon-verdict').innerText()).includes(selectedDate));
  result.checks.push('历史缺口处理入口在前，三类依据及查找方法保留；相关历史携带同日/版本，返回保留展开；空原订单有出口，无法恢复说明不接受基线');

  await execution.getByRole('button',{name:'自动核对作业',exact:true}).click();
  const job=page.getByRole('dialog',{name:/自动对账作业与逐日记录$/});await settled(job);
  await job.getByText(`仍有 ${pending.length} 个交易日待核对`,{exact:true}).waitFor();
  assert.equal(await job.getByRole('table',{name:'逐日核对记录'}).count(),0);
  assert(await job.getByRole('button',{name:`核查 ${firstDate}`,exact:true}).isVisible());
  await screenshots(job,'job');
  await job.getByRole('button',{name:`核查 ${firstDate}`,exact:true}).click();await settled(execution);
  await execution.getByRole('region',{name:'本日执行核对结论'}).getByText(firstDate,{exact:true}).waitFor();
  await execution.locator('.ant-drawer-close').click();await settled(job);
  await disclosure(job,'逐日核对记录').click();
  const row=job.getByRole('cell',{name:selectedDate,exact:true}).locator('..');
  await row.getByRole('button',{name:'核查此交易日',exact:true}).click();await settled(execution);
  await execution.getByRole('region',{name:'本日执行核对结论'}).getByText(selectedDate,{exact:true}).waitFor();
  await execution.locator('.ant-drawer-close').click();await settled(job);
  assert(await job.getByRole('table',{name:'逐日核对记录'}).isVisible());
  await capture(job,root+'/job-days.png');
  for(const size of [{width:390,height:844},{width:320,height:740}]){
    await page.setViewportSize(size);await settled(job);await fit(job);
    await capture(job,`${root}/job-days-${size.width}.png`);
  }
  await page.setViewportSize({width:1440,height:1000});await settled(job);
  await disclosure(job,'逐日核对记录').click();
  const work=disclosure(job,'持久核对工作单');
  if(await work.count()){await work.click();await fit(job);await capture(job,root+'/job-work.png');await work.click();}
  const events=disclosure(job,'来源事件待消费');
  if(await events.count()){await events.click();await fit(job);await capture(job,root+'/job-sources.png');await events.click();}
  await disclosure(job,'覆盖范围与核对说明').click();await fit(job);
  await capture(job,root+'/job-scope.png');
  await job.locator('.ant-drawer-close').click();await settled(execution);
  assert((await execution.getByTestId('recon-verdict').innerText()).includes(selectedDate));
  await execution.locator('.ant-drawer-close').click();await execution.waitFor({state:'hidden'});
  result.checks.push('自动作业先显示待核对日期和进度；逐日/工作单/来源折叠可用；顶部及逐日入口只读核对正确日期，关闭返回原作业，窄屏展开不溢出');
  result.after=await readState();assert.deepEqual(result.after,before);
  assert.equal(result.writeAttempts.length,0);assert.equal(result.errors.length,0);assert.equal(result.httpErrors.length,0);
  result.passed=true;
}catch(error){
  result.passed=false;result.error=error.message;
  result.body=(await page.locator('body').innerText()).slice(-5500);
  await page.screenshot({path:root+'/failure.png',animations:'disabled'});process.exitCode=1;
}finally{
  fs.writeFileSync(root+'/browser-result.json',JSON.stringify(result,null,2));
  console.log(JSON.stringify({passed:result.passed,error:result.error,checks:result.checks,writeAttempts:result.writeAttempts,errors:result.errors,httpErrors:result.httpErrors},null,2));
  await browser.close();
}
