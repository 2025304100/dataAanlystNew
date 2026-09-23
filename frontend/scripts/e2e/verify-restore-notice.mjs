#!/usr/bin/env node
/** 验证「选项3」提示条：本地自动恢复 → 显示提示；点新建回 Step1；关闭留在 Step5；URL 直达不打扰。 */
import { chromium } from "playwright";
const BASE = "http://localhost:5173";
const RUN_ID = "fcd1ca4679264fbcb81804cdf12774d5";
const browser = await chromium.launch({ headless: true, channel: "chrome", args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.setDefaultTimeout(25000);

async function primeLocalRestore() {
  await page.evaluate((rid) => {
    localStorage.clear();
    localStorage.setItem("settings_active_section", "factor-mining");
    localStorage.setItem("mining_last_run_id", rid);
  }, RUN_ID);
}

let fail = 0;
const check = (name, ok, extra = "") => {
  console.log(`${ok ? "PASS" : "FAIL"} | ${name}${extra ? " | " + extra : ""}`);
  if (!ok) fail += 1;
};

// ── 场景1：本地自动恢复（无 URL）→ 显示提示条 ──
await page.goto(`${BASE}/?tab=settings`, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForTimeout(800);
await primeLocalRestore();
await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForSelector("[data-mining-shell]", { timeout: 30000 });
await page.waitForSelector("[data-mining-restored-notice]", { timeout: 15000 });
check("本地恢复 → 提示条可见", true);
const noticeText = (await page.locator("[data-mining-restored-notice]").innerText()).trim();
check("提示条文案含短号", /已恢复最近任务 #\w+/.test(noticeText), noticeText.replace(/\n/g, " | "));
check("提示条含「新建任务」按钮", (await page.locator("[data-mining-restored-new]").count()) === 1);
check("提示条含关闭按钮", (await page.locator("[data-mining-restored-close]").count()) === 1);
check("恢复后仍在 Step5", (await page.locator('[data-mining-step="run"].current').count()) === 1);

// ── 场景2：点「新建任务」→ 回 Step1 且提示消失 ──
await page.locator("[data-mining-restored-new]").click();
await page.waitForTimeout(300);
check("点新建 → 回到 Step1", (await page.locator('[data-mining-step="pool"].current').count()) === 1);
check("点新建 → 提示条消失", (await page.locator("[data-mining-restored-notice]").count()) === 0);

// ── 场景3：重新恢复后点关闭 → 留在 Step5、提示消失 ──
await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForSelector("[data-mining-restored-notice]", { timeout: 15000 });
await page.locator("[data-mining-restored-close]").click();
await page.waitForTimeout(300);
check("点关闭 → 提示条消失", (await page.locator("[data-mining-restored-notice]").count()) === 0);
check("点关闭 → 仍停留 Step5", (await page.locator('[data-mining-step="run"].current').count()) === 1);
await page.screenshot({ path: "restore_notice_s1.png", fullPage: false });

// ── 场景4：URL `?run=` 直达 → 不显示提示条（显式意图） ──
await page.goto(`${BASE}/?tab=settings&run=${RUN_ID}`, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForSelector("[data-mining-shell]", { timeout: 30000 });
await page.waitForTimeout(2500);
check("URL 直达 → 不显示提示条", (await page.locator("[data-mining-restored-notice]").count()) === 0);
check("URL 直达 → 仍在 Step5", (await page.locator('[data-mining-step="run"].current').count()) === 1);

await browser.close();
console.log(fail === 0 ? "\n全部通过" : `\n${fail} 项未通过`);
process.exit(fail === 0 ? 0 : 1);