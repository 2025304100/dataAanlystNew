import { chromium } from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const root='D:/ai_project/dataAanlystNew/tmp/inspection-job-quick-fix-2026-10-24';
const browser=await chromium.launch({headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe'});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const reads=[],writes=[];
await page.route('**/api/**',route=>{const req=route.request();if(['GET','HEAD','OPTIONS'].includes(req.method())){if(req.url().includes('reconciliation-preview'))reads.push(req.url());return route.continue();}writes.push(req.url());return route.abort();});
try{
 await page.goto('http://127.0.0.1:8000/',{waitUntil:'domcontentloaded'});await page.getByText('组合交易',{exact:true}).first().click();await page.getByText('检查与对账',{exact:true}).first().click();await page.getByText('新买入已暂停',{exact:true}).waitFor();
 await page.getByRole('button',{name:'每日对账',exact:true}).click();
 const day=page.getByRole('dialog',{name:/执行与对账$/});await day.getByText('历史执行资料不完整',{exact:true}).waitFor();
 await day.getByRole('button',{name:'自动核对作业',exact:true}).click();
 const job=page.getByRole('dialog',{name:/自动对账作业与逐日记录$/});await job.getByRole('button',{name:'核查 2026-09-29',exact:true}).waitFor();
 await page.screenshot({path:root+'/before.png',animations:'disabled'});
 const count=reads.length;
 await job.getByRole('button',{name:'核查 2026-09-29',exact:true}).click();
 await day.getByRole('region',{name:'本日执行核对结论'}).getByText('2026-09-29',{exact:true}).waitFor();
 await page.screenshot({path:root+'/after.png',animations:'disabled'});
 const result={job_visible:await job.isVisible(),day_visible:await day.isVisible(),centered_quick_modal:await page.locator('.pi-quick-check').count(),new_reads:reads.slice(count),writes};
 fs.writeFileSync(root+'/reproduced.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result));assert.equal(writes.length,0);
}finally{await browser.close();}
