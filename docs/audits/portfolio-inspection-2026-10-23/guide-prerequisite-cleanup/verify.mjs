import { chromium } from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const root = 'D:/ai_project/dataAanlystNew/tmp/inspection-guide-cleanup';
const base = 'http://127.0.0.1:8000';
fs.mkdirSync(root, { recursive: true });
const result = { errors: [], httpErrors: [], writes: [], checks: [], widths: [] };
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe' });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.on('pageerror', error => result.errors.push(error.message));
page.on('response', response => {
  if (response.url().includes('/api/') && response.status() >= 400) result.httpErrors.push({ url: response.url(), status: response.status() });
});
await page.route('**/api/**', async route => {
  if (!['GET', 'HEAD', 'OPTIONS'].includes(route.request().method())) {
    result.writes.push({ url: route.request().url(), method: route.request().method() });
    return route.abort();
  }
  await route.continue();
});
const dialog = () => page.getByRole('dialog');
const nav = () => page.getByRole('navigation', { name: '恢复运行步骤' });
const stable = async () => {
  await page.waitForFunction(() => [...document.querySelectorAll('.ant-drawer-open .ant-drawer-content-wrapper')]
    .filter(element => getComputedStyle(element).visibility !== 'hidden')
    .every(element => Math.abs(element.getBoundingClientRect().right - innerWidth) < 1));
  await page.waitForFunction(() => [...document.querySelectorAll('[role=dialog]')]
    .filter(element => getComputedStyle(element).visibility !== 'hidden' && element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0).length === 1);
};
const read = async path => {
  const response = await page.request.get(base + path);
  if (!response.ok()) throw Error(`Read failed: ${response.status()} ${path}`);
  return response.json();
};
const safety = async () => {
  const mode = await read('/api/v1/inspection-acceptance/status');
  if (mode.background_jobs_enabled || mode.automatic_trading_enabled || mode.trading_mutations_allowed || mode.final_reviews_allowed) throw Error('Acceptance safety mode is not active');
  const runtime = await read('/api/v1/portfolios/1/inspection/runtime');
  const queue = await read('/api/v1/portfolios/1/inspection/reconciliation-queue');
  return { mode, current_state: runtime.current_state, permissions: runtime.permissions, can_confirm: runtime.recovery_summary.can_confirm,
    verified_through: queue.verified_through, pendingDays: queue.days.filter(day => !day.history_only && !['PASSED','NOT_DUE'].includes(day.status)).length };
};
const noBanner = async () => {
  if (await page.locator('.pi-external-condition').count() || await page.getByText('执行对账仍有审核前置条件', { exact: true }).count()) throw Error('Removed cross-module prerequisite banner is still rendered');
};
try {
  result.before = await safety();
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' });
  await page.getByText('组合交易', { exact: true }).first().click({ timeout: 20000 });
  await page.getByText('检查与对账', { exact: true }).first().click({ timeout: 20000 });
  await page.getByRole('heading', { name: '新买入已暂停', exact: true }).waitFor();
  await page.getByRole('button', { name: '运行状态审核', exact: true }).click();
  await stable();
  for (const label of ['批量修复数据', '确认运行绑定']) {
    await nav().getByRole('button', { name: new RegExp(label) }).click();
    await noBanner();
    if (await dialog().getByRole('button', { name: '查看执行对账', exact: true }).count()) throw Error('Execution link leaked into an intermediate step');
  }
  await page.screenshot({ path: root + '/binding-step.png', animations: 'disabled' });
  result.checks.push('数据与绑定步骤移除常驻执行对账卡片及跨模块按钮');
  await nav().getByRole('button', { name: /本人审核恢复/ }).click();
  await noBanner();
  if (!await page.getByRole('button', { name: '审核并确认恢复', exact: true }).isDisabled()) throw Error('Final review was enabled despite unresolved conditions');
  const check = dialog().locator('.pi-check-list > div').filter({ has: page.getByText('执行对账', { exact: true }) });
  result.reconciliationCheck = await check.innerText();
  if (!result.reconciliationCheck.includes('未通过') && !result.reconciliationCheck.includes('未知')) throw Error('Final reconciliation prerequisite is missing');
  await check.getByRole('button', { name: '查看执行对账', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: root + '/final-review.png', animations: 'disabled' });
  const originalScroll = await dialog().locator('.ant-drawer-body').evaluate(element => element.scrollTop);
  await check.getByRole('button', { name: '查看执行对账', exact: true }).click();
  await stable();
  await page.getByTestId('reconcile-panel').waitFor();
  await page.getByRole('listitem', { name: '当天的自动运行记录与决策明细' }).waitFor();
  await page.getByRole('button', { name: '返回状态审核并刷新', exact: true }).click();
  await stable();
  if (!(await nav().locator('[aria-current=step]').innerText()).includes('本人审核恢复')) throw Error('Execution return lost the original review step');
  const returnedScroll = await dialog().locator('.ant-drawer-body').evaluate(element => element.scrollTop);
  if (Math.abs(originalScroll - returnedScroll) > 5) throw Error('Execution return lost the original review scroll');
  if (!await page.getByRole('button', { name: '审核并确认恢复', exact: true }).isDisabled()) throw Error('Read-only navigation granted recovery');
  result.checks.push('最终审核保留未通过条件，进入独立执行对账抽屉后返回原审核步骤和位置');
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 920 });
    await check.getByRole('button', { name: '查看执行对账', exact: true }).scrollIntoViewIfNeeded();
    const bounds = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth,
      bodies: [...document.querySelectorAll('.ant-drawer-open .ant-drawer-body')].filter(element => getComputedStyle(element).visibility !== 'hidden').map(element => ({ width: element.clientWidth, scrollWidth: element.scrollWidth })) }));
    if (bounds.document > width || bounds.bodies.some(body => body.scrollWidth > body.width)) throw Error('Narrow final review has horizontal overflow');
    result.widths.push(bounds);
    await page.screenshot({ path: root + `/final-review-${width}.png`, animations: 'disabled' });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await dialog().locator('.ant-drawer-close').click();
  await page.getByRole('region', { name: '执行与对账模块' }).getByRole('button', { name: '每日对账', exact: true }).click();
  await stable();
  await page.getByTestId('reconcile-panel').waitFor();
  result.checks.push('首页独立执行与对账模块保持可用，窄屏审核条件与查看入口不溢出');
  result.after = await safety();
  if (JSON.stringify(result.before) !== JSON.stringify(result.after)) throw Error('Main safety state changed during read-only review');
  if (result.errors.length || result.httpErrors.length || result.writes.length) throw Error('Unexpected browser error, API error or write request');
  result.passed = true;
} catch (error) {
  result.passed = false;
  result.error = error.message;
  result.body = (await page.locator('body').innerText()).slice(-6500);
  await page.screenshot({ path: root + '/failure.png', animations: 'disabled' });
  process.exitCode = 1;
} finally {
  fs.writeFileSync(root + '/result.json', JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
}
