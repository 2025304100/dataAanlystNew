import { chromium } from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const root = 'D:/ai_project/dataAanlystNew/tmp/inspection-history-gap-review';
const base = 'http://127.0.0.1:8000';
fs.mkdirSync(root, { recursive: true });
const result = { errors: [], httpErrors: [], writes: [], checks: [], widths: [] };
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe' });
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
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
const close = () => dialog().locator('.ant-drawer-close').click();
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
const noRegistration = async () => {
  if (await dialog().getByRole('textbox', { name: '材料核查说明' }).count() || await dialog().getByRole('radio').count()
    || await dialog().getByRole('button', { name: '保存核查说明，保留待核实' }).count()) throw Error('Per-material registration form is still exposed');
};
try {
  result.before = await safety();
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' });
  await page.getByText('组合交易', { exact: true }).first().click({ timeout: 20000 });
  await page.getByText('检查与对账', { exact: true }).first().click({ timeout: 20000 });
  await page.getByRole('heading', { name: '新买入已暂停', exact: true }).waitFor();
  await page.getByRole('button', { name: '每日对账', exact: true }).click();
  await stable();
  await page.getByRole('button', { name: '查看并处理历史缺口', exact: true }).waitFor();
  if (await dialog().getByRole('list', { name: '系统发现的历史缺口' }).count()) throw Error('Three materials are still laid out in the reconciliation summary');
  result.tradeDate = (await page.locator('.pi-execution-toolbar .ant-select-selection-item').innerText()).match(/\d{4}-\d{2}-\d{2}/)?.[0];
  await page.screenshot({ path: root + '/reconciliation-summary.png', animations: 'disabled' });
  await page.getByRole('button', { name: '查看并处理历史缺口', exact: true }).click();
  await stable();
  const gap = () => page.getByRole('region', { name: '历史缺口详情', exact: true });
  await gap().waitFor();
  for (const title of ['当天的自动运行记录与决策明细', '当天生效的自动接管与调度配置', '该交易日的持仓明细']) {
    await gap().getByRole('listitem', { name: title, exact: true }).waitFor();
  }
  await noRegistration();
  await page.screenshot({ path: root + '/history-gap.png', animations: 'disabled' });
  const sourceRow = () => gap().getByRole('listitem', { name: '当天的自动运行记录与决策明细', exact: true });
  await sourceRow().getByText('查看这项依据的来源与查找方法', { exact: true }).click();
  await gap().getByRole('button', { name: '查看该日查找与处理记录', exact: true }).scrollIntoViewIfNeeded();
  const gapScroll = await dialog().locator('.ant-drawer-body').evaluate(element => element.scrollTop);
  await gap().getByRole('button', { name: '查看该日查找与处理记录', exact: true }).click();
  await stable();
  if (await page.getByLabel('所属交易日起点', { exact: true }).inputValue() !== result.tradeDate) throw Error('Gap history did not retain the current trade date');
  await page.locator('.pi-history-batch-title').first().waitFor();
  await page.screenshot({ path: root + '/history-return-scope.png', animations: 'disabled' });
  await page.getByRole('button', { name: '返回原问题处理', exact: true }).click();
  await stable();
  if (Math.abs(await dialog().locator('.ant-drawer-body').evaluate(element => element.scrollTop) - gapScroll) > 5) throw Error('History return lost gap scroll');
  if (!await sourceRow().locator('details').first().evaluate(element => element.open)) throw Error('History return lost the expanded source');
  await gap().getByRole('button', { name: '查看可关联的原订单', exact: true }).click();
  await stable();
  await page.getByRole('button', { name: '返回历史缺口，查看后续处理', exact: true }).click();
  await stable();
  await noRegistration();
  await gap().getByText('资料确实无法恢复时', { exact: true }).click();
  await page.getByRole('button', { name: '查看历史缺口处置条件', exact: true }).waitFor();
  await page.getByRole('button', { name: '查看历史缺口处置条件', exact: true }).click();
  await stable();
  await page.getByRole('button', { name: '已核查仍无法恢复，登记历史缺口并申请新基线', exact: true }).waitFor();
  await close(); await stable();
  await gap().getByRole('button', { name: '重新读取现有来源', exact: true }).click();
  await gap().getByRole('heading', { name: '当日执行资料尚不完整', exact: true }).waitFor();
  result.checks.push('按日结论→统一历史缺口→按日相关历史→原缺口；原订单候选为空与无法恢复均有返回路径，无逐项登记');
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 920 });
    await dialog().locator('.ant-drawer-body').evaluate(element => { element.scrollTop = 0; });
    const bounds = await page.evaluate(() => ({ surface: 'history-gap', viewport: innerWidth, document: document.documentElement.scrollWidth,
      bodies: [...document.querySelectorAll('.ant-drawer-open .ant-drawer-body')].filter(element => getComputedStyle(element).visibility !== 'hidden').map(element => ({ width: element.clientWidth, scrollWidth: element.scrollWidth })) }));
    if (bounds.document > width || bounds.bodies.some(body => body.scrollWidth > body.width)) throw Error('Narrow history-gap drawer has horizontal overflow');
    result.widths.push(bounds);
    await page.screenshot({ path: root + `/history-gap-${width}.png`, animations: 'disabled' });
  }
  await page.setViewportSize({ width: 1440, height: 1050 });
  await close(); await stable(); await close();
  await page.getByRole('button', { name: '运行状态审核', exact: true }).click(); await stable();
  const nav = () => page.getByRole('navigation', { name: '恢复运行步骤' });
  await nav().getByRole('button', { name: /本人审核恢复/ }).click();
  for (const name of ['当前审核结论', '审核剩余问题', '确认后的影响']) await page.getByRole('region', { name, exact: true }).waitFor();
  if (await dialog().locator('.pi-runtime-review > section').count() !== 3) throw Error('Final review contains extra review modules');
  if (await dialog().getByRole('list', { name: '系统发现的历史缺口' }).count() || await dialog().getByText('引擎灰度与高级运行信息', { exact: true }).count()) throw Error('Final review still contains material or engine maintenance modules');
  if (!await page.getByRole('button', { name: '审核并确认恢复', exact: true }).isDisabled()) throw Error('Final review was enabled despite unresolved conditions');
  await noRegistration();
  await page.screenshot({ path: root + '/runtime-review.png', animations: 'disabled' });
  await page.getByRole('button', { name: '查看历史缺口', exact: true }).click(); await stable();
  await close(); await stable();
  if (!(await nav().locator('[aria-current=step]').innerText()).includes('本人审核恢复')) throw Error('Gap close lost the original review step');
  if (!await page.getByRole('button', { name: '审核并确认恢复', exact: true }).isDisabled()) throw Error('Viewing history granted recovery');
  result.checks.push('运行审核仅结论、剩余问题、确认影响；历史缺口按一个问题汇总，抽屉返回原步骤，审核仍禁用');
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 920 });
    const bounds = await page.evaluate(() => ({ surface: 'runtime-review', viewport: innerWidth, document: document.documentElement.scrollWidth,
      bodies: [...document.querySelectorAll('.ant-drawer-open .ant-drawer-body')].filter(element => getComputedStyle(element).visibility !== 'hidden').map(element => ({ width: element.clientWidth, scrollWidth: element.scrollWidth })) }));
    if (bounds.document > width || bounds.bodies.some(body => body.scrollWidth > body.width)) throw Error('Narrow runtime review has horizontal overflow');
    result.widths.push(bounds);
    await page.screenshot({ path: root + `/runtime-review-${width}.png`, animations: 'disabled' });
  }
  result.after = await safety();
  if (JSON.stringify(result.before) !== JSON.stringify(result.after)) throw Error('Main safety state changed during read-only review');
  if (result.errors.length || result.httpErrors.length || result.writes.length) throw Error('Unexpected browser error, API error or write request');
  result.passed = true;
} catch (error) {
  result.passed = false;
  result.error = error.message;
  result.body = (await page.locator('body').innerText()).slice(-6000);
  await page.screenshot({ path: root + '/failure.png', animations: 'disabled' });
  process.exitCode = 1;
} finally {
  fs.writeFileSync(root + '/result.json', JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
}
