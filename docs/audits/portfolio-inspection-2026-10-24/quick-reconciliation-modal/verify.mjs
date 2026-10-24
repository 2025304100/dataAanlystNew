import { chromium } from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';

const root = 'D:/ai_project/dataAanlystNew/tmp/inspection-quick-check-2026-10-24';
const mode = await fetch('http://127.0.0.1:8000/api/v1/inspection-acceptance/status').then(r => r.json());
assert.equal(mode.mode, 'inspection_acceptance');
for (const name of ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed']) assert.equal(mode[name], false);
const receipt = root + '/live-recheck.json';
const result = { errors: [], httpErrors: [], writes: [], blockedWrites: [], checks: [], scopedReads: [] };
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe' });
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
page.on('pageerror', error => result.errors.push(error.message));
page.on('response', response => { if (response.url().includes('/api/') && response.status() >= 400) result.httpErrors.push({ url: response.url(), status: response.status() }); });
let writeAllowed = !fs.existsSync(receipt);
await page.route('**/api/**', async route => {
  const req = route.request(), url = new URL(req.url());
  if (['GET','HEAD','OPTIONS'].includes(req.method())) {
    if (url.pathname.includes('reconciliation-preview') || url.pathname.includes('/inspection/history')) result.scopedReads.push(req.url());
    return route.continue();
  }
  const body = req.postDataJSON();
  const allowed = req.method() === 'POST' && url.pathname === '/api/v1/portfolios/1/reconcile' && body?.trade_date === '2026-09-30' && Object.keys(body).length === 1;
  if (allowed && writeAllowed) {
    writeAllowed = false;
    result.writes.push({ method: req.method(), url: req.url(), body });
    return route.continue();
  }
  result.blockedWrites.push({ method: req.method(), url: req.url() });
  return route.abort();
});
const modal = page.locator('.pi-quick-check');
const settleModal = async () => {
  await modal.waitFor({ state: 'visible' });
  await page.waitForFunction(() => {
    const node = document.querySelector('.pi-quick-check');
    return node && getComputedStyle(node).transform === 'none';
  });
};
const closeQuick = async () => { await modal.getByRole('button', { name: /^关\s*闭$/ }).click(); await modal.waitFor({ state: 'detached' }); };
try {
  await page.goto('http://127.0.0.1:8000/', { waitUntil: 'domcontentloaded' });
  await page.getByText('组合交易', { exact: true }).first().click({ timeout: 20000 });
  await page.getByText('检查与对账', { exact: true }).first().click({ timeout: 20000 });
  assert.equal(await page.locator('.pt-portfolio-name').innerText(), 'main');
  await page.locator('.pt-autotrade-pill').click();
  const drawer = page.getByRole('dialog', { name: '自动交易检查', exact: true });
  await drawer.getByText(/其他待处理事项/).click();
  const button = drawer.getByRole('button', { name: /2026-09-30.*快捷核对/ });
  await button.scrollIntoViewIfNeeded();
  const scrollBefore = await drawer.locator('.ant-drawer-body').evaluate(el => el.scrollTop);
  await page.screenshot({ path: root + '/card-entry.png', animations: 'disabled' });
  await button.click(); await settleModal();
  await modal.getByText('历史执行资料不完整', { exact: true }).waitFor();
  const text = await modal.innerText();
  assert(text.includes('2026-09-30') && !text.includes('2026-09-29'));
  assert(!/portfolio_id=|trade_date=|DecisionRun/.test(text));
  assert.equal(await modal.getByRole('table').count(), 0);
  const bounds = await modal.boundingBox();
  assert(Math.abs(bounds.x + bounds.width / 2 - 720) < 3);
  assert(Math.abs(bounds.y + bounds.height / 2 - 525) < 3);
  await modal.getByRole('button', { name: '查看缺口与查找方法', exact: true }).click();
  await modal.getByText('当天的自动运行记录与决策明细', { exact: true }).waitFor();
  await modal.getByText('当天生效的自动接管与调度配置', { exact: true }).waitFor();
  await modal.getByText('该交易日的持仓明细', { exact: true }).waitFor();
  await page.screenshot({ path: root + '/quick-check.png', animations: 'disabled' });
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press('Tab');
    assert(await modal.evaluate(node => node.contains(document.activeElement)), 'Tab escaped the quick-check dialog');
  }
  await page.keyboard.press('Escape'); await modal.waitFor({ state: 'detached' });
  assert(await drawer.isVisible());
  const scrollAfter = await drawer.locator('.ant-drawer-body').evaluate(el => el.scrollTop);
  assert(Math.abs(scrollBefore - scrollAfter) < 2, `Parent list moved: ${scrollBefore} -> ${scrollAfter}`);
  result.checks.push('居中显示09-30，三类材料统一折叠，无内部字段或空比较；12次Tab不离开弹窗；Escape返回并保留列表展开和滚动');
  await button.click(); await settleModal();
  await modal.getByText('历史执行资料不完整', { exact: true }).waitFor();
  if (!fs.existsSync(receipt)) {
    const responsePromise = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/portfolios/1/reconcile' && response.request().method() === 'POST');
    await modal.getByRole('button', { name: '重新核对本日', exact: true }).click();
    const response = await responsePromise;
    assert.equal(response.status(), 200);
    const report = await response.json();
    fs.writeFileSync(receipt, JSON.stringify(report, null, 2));
    assert.equal(report.trade_date, '2026-09-30');
    assert.equal(report.portfolio_id, 1);
    assert.equal(report.can_confirm, false);
    assert.equal(report.last_reconciled_trade_date, '2026-09-28');
    await modal.getByText('本次重检结果', { exact: true }).waitFor();
  }
  result.live_recheck = JSON.parse(fs.readFileSync(receipt, 'utf8'));
  result.checks.push('仅发起一次main/09-30真实重检，返回同日且仍不可确认，没有串到09-29');
  const historyResponse = page.waitForResponse(response => response.url().includes('/inspection/history?') && response.status() === 200);
  await modal.getByRole('button', { name: '查看本日处理记录', exact: true }).click();
  const historyBack = page.getByRole('button', { name: '返回快捷核对', exact: true });
  await historyBack.waitFor();
  await historyResponse;
  assert(result.scopedReads.some(value => value.includes('/inspection/history?') && value.includes('trade_date_from=2026-09-30') && value.includes('report_version=')));
  await page.screenshot({ path: root + '/quick-history.png', animations: 'disabled' });
  await historyBack.click(); await settleModal();
  await modal.getByRole('button', { name: '查看可关联原订单', exact: true }).click();
  await page.getByText('尚未找到满足条件的自动执行来源', { exact: true }).waitFor();
  await page.getByRole('button', { name: '返回快捷核对', exact: true }).first().click(); await settleModal();
  result.checks.push('本日历史按交易日与报告版本定位；无补证候选时如实显示，逐层返回原快捷核对');
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 900 }); await settleModal();
    const geometry = await page.evaluate(() => {
      const node = document.querySelector('.pi-quick-check');
      const box = node.getBoundingClientRect();
      return { viewport: innerWidth, doc: document.documentElement.scrollWidth, x: box.x, right: box.right };
    });
    assert(geometry.doc <= width && geometry.x >= 0 && geometry.right <= width, JSON.stringify(geometry));
    await page.screenshot({ path: root + `/quick-${width}.png`, animations: 'disabled' });
  }
  result.checks.push('390px与320px居中弹窗无文档横向溢出');
  await closeQuick();
  await page.setViewportSize({ width: 1440, height: 1050 });
  await drawer.getByRole('button', { name: /2026-10-08.*快捷核对/ }).click(); await settleModal();
  await modal.getByText('历史执行资料不完整', { exact: true }).waitFor();
  assert((await modal.innerText()).includes('2026-10-08') && !(await modal.innerText()).includes('2026-09-30'));
  await closeQuick();
  assert.equal(result.errors.length, 0);
  assert.equal(result.httpErrors.length, 0);
  assert.equal(result.blockedWrites.length, 0);
  assert(result.writes.length <= 1);
  result.passed = true;
} catch (error) {
  result.passed = false; result.error = error.message;
  result.body = (await page.locator('body').innerText()).slice(-6500);
  await page.screenshot({ path: root + '/browser-failure.png', animations: 'disabled' });
  process.exitCode = 1;
} finally {
  fs.writeFileSync(root + '/browser-result.json', JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
}
