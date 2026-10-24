import { chromium } from '../../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import assert from 'node:assert/strict';

const root = 'D:/ai_project/dataAanlystNew/tmp/inspection-quick-style-2026-10-24';
const readMode = () => fetch('http://127.0.0.1:8000/api/v1/inspection-acceptance/status').then(r => r.json());
const beforeMode = await readMode();
assert.equal(beforeMode.mode, 'inspection_acceptance');
for (const name of ['background_jobs_enabled', 'automatic_trading_enabled', 'trading_mutations_allowed', 'final_reviews_allowed']) assert.equal(beforeMode[name], false);
const result = { errors: [], httpErrors: [], writeAttempts: [], checks: [], beforeMode };
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Users/demo/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe' });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.on('pageerror', e => result.errors.push(e.message));
page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) result.httpErrors.push({ url: r.url(), status: r.status() }); });
await page.route('**/api/**', async route => {
  const request = route.request();
  if (['GET', 'HEAD', 'OPTIONS'].includes(request.method())) return route.continue();
  result.writeAttempts.push({ method: request.method(), url: request.url() });
  return route.abort();
});
const modal = page.locator('.pi-quick-check');
const settle = async () => {
  await modal.waitFor({ state: 'visible' });
  await page.waitForFunction(() => {
    const node = document.querySelector('.pi-quick-check');
    return node && getComputedStyle(node).transform === 'none';
  });
};
const fit = async () => {
  const layout = await modal.evaluate(node => {
    const box = node.getBoundingClientRect();
    const content = node.querySelector('.ant-modal-content').getBoundingClientRect();
    const body = node.querySelector('.pi-quick-body');
    const footer = node.querySelector('.ant-modal-footer').getBoundingClientRect();
    return { width: innerWidth, height: innerHeight, doc: document.documentElement.scrollWidth,
      x: box.x, right: box.right, top: content.top, bottom: content.bottom, bodyWidth: body.clientWidth,
      bodyScrollWidth: body.scrollWidth, footerTop: footer.top, footerBottom: footer.bottom };
  });
  assert(layout.doc <= layout.width && layout.bodyScrollWidth <= layout.bodyWidth + 1, JSON.stringify(layout));
  assert(layout.x >= 0 && layout.right <= layout.width && layout.top >= 0 && layout.bottom <= layout.height, JSON.stringify(layout));
  assert(layout.footerBottom <= layout.height && layout.footerTop >= 0);
  return layout;
};
try {
  await page.goto('http://127.0.0.1:8000/', { waitUntil: 'domcontentloaded' });
  await page.getByText('组合交易', { exact: true }).first().click({ timeout: 20000 });
  await page.getByText('检查与对账', { exact: true }).first().click({ timeout: 20000 });
  assert.equal(await page.locator('.pt-portfolio-name').innerText(), 'main');
  await page.locator('.pt-autotrade-pill').click();
  const drawer = page.getByRole('dialog', { name: '自动交易检查', exact: true });
  await drawer.getByText(/其他待处理事项/).click();
  const entry = drawer.getByRole('button', { name: /2026-09-30.*快捷核对/ });
  await entry.scrollIntoViewIfNeeded();
  const scrollBefore = await drawer.locator('.ant-drawer-body').evaluate(el => el.scrollTop);
  await entry.click(); await settle();
  await modal.getByText('历史执行资料不完整', { exact: true }).waitFor();
  assert((await modal.innerText()).includes('2026-09-30'));
  assert(!/portfolio_id=|trade_date=|DecisionRun/.test(await modal.innerText()));
  const bounds = await modal.boundingBox();
  assert.equal(bounds.width, 760);
  assert(Math.abs(bounds.x + bounds.width / 2 - 720) < 2);
  assert.equal(await modal.getByRole('table').count(), 0);
  result.desktop = await fit();
  await page.screenshot({ path: root + '/desktop.png', animations: 'disabled' });
  await modal.screenshot({ path: root + '/modal.png', animations: 'disabled' });
  await modal.getByRole('button', { name: '查看缺口与查找方法', exact: true }).click();
  await modal.getByText('当天的自动运行记录与决策明细', { exact: true }).waitFor();
  assert(await modal.locator('.pi-quick-gaps').evaluate(node => node.open));
  await page.screenshot({ path: root + '/expanded.png', animations: 'disabled' });
  const summary = modal.locator('.pi-quick-gaps > summary');
  await summary.click();
  assert.equal(await summary.evaluate(node => getComputedStyle(node).outlineStyle), 'none');
  await page.keyboard.press('Tab');
  await page.keyboard.press('Shift+Tab');
  assert(await summary.evaluate(node => node === document.activeElement && node.matches(':focus-visible')));
  assert.equal(await summary.evaluate(node => getComputedStyle(node).outlineWidth), '2px');
  await page.keyboard.press('Enter');
  assert(await modal.locator('.pi-quick-gaps').evaluate(node => node.open));
  const material = modal.locator('.pi-quick-material').first();
  await material.locator('summary').click();
  assert(await material.evaluate(node => node.open));
  await page.screenshot({ path: root + '/material.png', animations: 'disabled' });
  await material.locator('summary').click();
  await summary.click();
  result.checks.push('760px居中；结论和主入口前置；鼠标操作无白色焦点框，键盘Tab/Enter保留可见焦点与折叠操作');
  for (const viewport of [{ width: 1024, height: 700 }, { width: 390, height: 844 }, { width: 320, height: 740 }]) {
    await page.setViewportSize(viewport); await settle();
    result[`layout-${viewport.width}`] = await fit();
    await page.screenshot({ path: root + `/width-${viewport.width}.png`, animations: 'disabled' });
    await modal.getByRole('button', { name: '查看缺口与查找方法', exact: true }).click();
    await fit();
    await page.screenshot({ path: root + `/width-${viewport.width}-expanded.png`, animations: 'disabled' });
    await summary.click();
  }
  result.checks.push('1024/390/320px折叠与展开均无横向溢出，关闭和底部重检入口在视口内');
  await page.setViewportSize({ width: 1440, height: 1000 }); await settle();
  await modal.getByRole('button', { name: '查看本日处理记录', exact: true }).click();
  const historyDrawer = page.getByRole('dialog', { name: /本日处理记录/ });
  await historyDrawer.getByRole('button', { name: '返回快捷核对', exact: true }).click(); await settle();
  await historyDrawer.waitFor({ state: 'hidden' });
  await modal.getByRole('button', { name: '查看可关联原订单', exact: true }).click();
  await page.getByText('尚未找到满足条件的自动执行来源', { exact: true }).waitFor();
  const evidenceDrawer = page.getByRole('dialog', { name: /本日原订单关联/ });
  await evidenceDrawer.getByRole('button', { name: '返回快捷核对', exact: true }).first().click(); await settle();
  await evidenceDrawer.waitFor({ state: 'hidden' });
  await page.waitForFunction(() => document.querySelector('.pi-quick-check')?.contains(document.activeElement));
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press('Tab');
    assert(await modal.evaluate(node => node.contains(document.activeElement)));
  }
  await page.keyboard.press('Escape'); await modal.waitFor({ state: 'detached' });
  assert(await drawer.isVisible());
  assert(Math.abs(scrollBefore - await drawer.locator('.ant-drawer-body').evaluate(node => node.scrollTop)) < 2);
  result.checks.push('历史和原订单独立浮层可返回；Tab焦点不逃逸，Escape仅关闭快捷弹窗，父列表滚动保留');
  result.afterMode = await readMode();
  assert.deepEqual(result.afterMode, beforeMode);
  assert.equal(result.writeAttempts.length, 0);
  assert.equal(result.errors.length, 0);
  assert.equal(result.httpErrors.length, 0);
  result.passed = true;
} catch (e) {
  result.passed = false; result.error = e.message;
  await page.screenshot({ path: root + '/failure.png', animations: 'disabled' });
  process.exitCode = 1;
} finally {
  fs.writeFileSync(root + '/browser-result.json', JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
}
