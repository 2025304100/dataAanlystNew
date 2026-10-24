import { chromium } from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const root='D:/ai_project/dataAanlystNew/tmp/inspection-etf-repair-2026-10-24';
const retry=JSON.parse(fs.readFileSync(root+'/retry-result.json','utf8'));
const browser=await chromium.launch({headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe'});
const page=await browser.newPage({viewport:{width:1440,height:1050}});
const result={errors:[],httpErrors:[],writes:[],checks:[],task_id:retry.retry.task_id};
page.on('pageerror',error=>result.errors.push(error.message));
page.on('response',response=>{if(response.url().includes('/api/')&&response.status()>=400)result.httpErrors.push({url:response.url(),status:response.status()});});
await page.route('**/api/**',async route=>{if(!['GET','HEAD','OPTIONS'].includes(route.request().method())){result.writes.push({method:route.request().method(),url:route.request().url()});return route.abort();}await route.continue();});
const dialog=()=>page.getByRole('dialog');
const stable=async()=>{
  await page.waitForFunction(()=>[...document.querySelectorAll('.ant-drawer-open .ant-drawer-content-wrapper')].filter(element=>getComputedStyle(element).visibility!=='hidden').every(element=>Math.abs(element.getBoundingClientRect().right-innerWidth)<1));
  await page.waitForFunction(()=>[...document.querySelectorAll('[role=dialog]')].filter(element=>getComputedStyle(element).visibility!=='hidden'&&element.getBoundingClientRect().width>0&&element.getBoundingClientRect().height>0).length===1);
};
try {
  await page.goto('http://127.0.0.1:8000/',{waitUntil:'domcontentloaded'});
  await page.getByText('组合交易',{exact:true}).first().click({timeout:20000});
  await page.getByText('检查与对账',{exact:true}).first().click({timeout:20000});
  await page.getByRole('button',{name:'打开数据修复',exact:true}).click();await stable();
  await page.getByRole('button',{name:'前往数据中心批量修复',exact:true}).click();
  const workspace=page.getByRole('region',{name:'组合数据批量修复工作区',exact:true});await workspace.waitFor();
  await page.getByText('已持久保存的修复任务 · 2 条',{exact:true}).click();
  const row=workspace.locator('.pi-recent-task').filter({hasText:retry.retry.task_id});
  await row.getByRole('button',{name:'查看原任务',exact:true}).click();await stable();
  await dialog().getByRole('button',{name:'查看失败详情',exact:true}).click();await stable();
  const detail=page.getByRole('region',{name:'修复步骤失败详情',exact:true});await detail.waitFor();
  if(!await detail.getByText('东方财富 · ETF前复权日K',{exact:true}).isVisible())throw Error('Specific ETF source was lost');
  if(!(await detail.innerText()).includes('行情服务连接中断'))throw Error('Actual source failure was replaced with generic copy');
  if(await dialog().count()!==1)throw Error('Saved task and failure detail are both active');
  await page.screenshot({path:root+'/failure-detail.png',animations:'disabled'});
  await dialog().locator('.ant-drawer-close').focus();
  for(let index=0;index<6;index++){await page.keyboard.press('Tab');if(!await dialog().evaluate(element=>element.closest('.ant-drawer')?.contains(document.activeElement)))throw Error('Focus escaped the failure drawer');}
  await page.setViewportSize({width:390,height:920});
  const bounds=await page.evaluate(()=>({viewport:innerWidth,document:document.documentElement.scrollWidth,body:[...document.querySelectorAll('.ant-drawer-body')].filter(element=>getComputedStyle(element).visibility!=='hidden').map(element=>({width:element.clientWidth,scrollWidth:element.scrollWidth}))}));
  if(bounds.document>390||bounds.body.some(body=>body.scrollWidth>body.width))throw Error('Failure detail overflows a narrow screen');result.narrow=bounds;
  await page.screenshot({path:root+'/failure-detail-390.png',animations:'disabled'});
  await page.setViewportSize({width:1440,height:1050});
  await page.getByRole('button',{name:'返回原任务详情',exact:true}).click();await stable();
  await page.getByRole('button',{name:'继续处理此原任务',exact:true}).click();
  const results=workspace.locator('.pi-task-results');await results.waitFor();
  if((await results.innerText()).includes('等待任务更新'))throw Error('Terminal failure still displays waiting copy');
  if(!await results.getByRole('button',{name:'仅重试失败与依赖受阻步骤',exact:true}).isEnabled())throw Error('The failed source has no retry exit');
  await results.scrollIntoViewIfNeeded();await page.screenshot({path:root+'/retry-result.png',animations:'disabled'});
  const originalRow=workspace.locator('.pi-recent-task').filter({hasText:retry.original.task_id});
  await originalRow.getByRole('button',{name:'查看原任务',exact:true}).click();await stable();
  await page.getByRole('button',{name:'继续处理此原任务',exact:true}).click();
  if(await results.getByText('重检通过',{exact:true}).count()!==2)throw Error('The two successful symbols were lost');
  if((await results.innerText()).includes('等待任务更新'))throw Error('Completed success still displays waiting copy');
  await results.scrollIntoViewIfNeeded();await page.screenshot({path:root+'/original-partial-result.png',animations:'disabled'});
  result.checks.push('原批成功两只保留；失败ETF有来源、日期、原因及下一步；已保存任务显示2条，无终态等待提示');
  result.checks.push('任务详情→失败详情→原任务逐层返回，仅顶层接管焦点，390px无横向溢出');
  if(result.errors.length||result.httpErrors.length||result.writes.length)throw Error('Unexpected browser error, API error or write attempt');
  result.passed=true;
} catch(error) {
  result.passed=false;result.error=error.message;result.body=(await page.locator('body').innerText()).slice(-5500);
  await page.screenshot({path:root+'/browser-failure.png',animations:'disabled'});process.exitCode=1;
} finally {
  fs.writeFileSync(root+'/browser-result.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));await browser.close();
}
