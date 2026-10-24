import { chromium } from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const root='D:/ai_project/dataAanlystNew/tmp/inspection-execution-style-2026-10-24';
const browser=await chromium.launch({headless:true,executablePath:'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe'});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const writes=[];
await page.route('**/api/**',route=>{if(['GET','HEAD','OPTIONS'].includes(route.request().method()))return route.continue();writes.push(route.request().url());return route.abort();});
const records=[];
async function capture(name){
  const drawer=page.getByRole('dialog',{name:/自动对账作业与逐日记录$/});
  await drawer.getByText('仍有 14 个交易日待核对',{exact:true}).waitFor();
  await page.waitForFunction(node=>getComputedStyle(node.closest('.ant-drawer-content-wrapper')).transform==='none',await drawer.elementHandle());
  await drawer.evaluate(async node=>{node.scrollTop=0;node.querySelector('.ant-drawer-body').scrollTop=0;await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));});
  const metrics=await drawer.evaluate(node=>{
    const info=selector=>{const el=node.querySelector(selector),box=el.getBoundingClientRect(),style=getComputedStyle(el);return {top:box.top,bottom:box.bottom,left:box.left,right:box.right,scrollTop:el.scrollTop,position:style.position,overflow:style.overflow,visibility:style.visibility,opacity:style.opacity,transform:style.transform};};
    return {header:info('.ant-drawer-header'),title:info('.pi-drawer-title'),close:info('.ant-drawer-close'),extra:node.querySelector('.ant-drawer-extra')?info('.ant-drawer-extra'):null};
  });
  await page.screenshot({path:root+`/${name}-page.png`,animations:'disabled'});
  await drawer.screenshot({path:root+`/${name}.png`,animations:'disabled'});
  records.push({name,metrics});
  return drawer;
}
try{
  await page.goto('http://127.0.0.1:8000/',{waitUntil:'domcontentloaded'});
  await page.getByText('组合交易',{exact:true}).first().click();await page.getByText('检查与对账',{exact:true}).first().click();
  await page.getByText('新买入已暂停',{exact:true}).waitFor();
  await page.getByRole('button',{name:'查看作业',exact:true}).click();
  const job=await capture('job-main-entry');
  await job.locator('.ant-drawer-close').click();await job.waitFor({state:'hidden'});
  await page.getByRole('button',{name:'每日对账',exact:true}).click();
  const execution=page.getByRole('dialog',{name:/执行与对账$/});await execution.getByText('历史执行资料不完整',{exact:true}).waitFor();
  await execution.getByRole('button',{name:'自动核对作业',exact:true}).click();await capture('job-from-execution');
  assert.equal(writes.length,0);
  fs.writeFileSync(root+'/header-check.json',JSON.stringify({records,writes,passed:true},null,2));
  console.log(JSON.stringify({records,writes,passed:true},null,2));
}finally{await browser.close();}
