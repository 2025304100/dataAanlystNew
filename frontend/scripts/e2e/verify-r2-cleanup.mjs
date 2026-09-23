#!/usr/bin/env node
/** R2 验证：无效 last_run_id → getRun 404 → localStorage 残留被清除，页面保持空态。 */
import { chromium } from "playwright";
const BASE = "http://localhost:5173";
const browser = await chromium.launch({ headless: true, channel: "chrome", args: ["--no-sandbox"] });
const page = await browser.newPage();
page.setDefaultTimeout(20000);
await page.goto(`${BASE}/?tab=settings`, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForTimeout(800);
await page.evaluate(() => {
  localStorage.clear();
  localStorage.setItem("settings_active_section", "factor-mining");
  localStorage.setItem("mining_last_run_id", "nonexistent-run-zzz");
});
await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForSelector("[data-mining-shell]", { timeout: 30000 });
await page.waitForTimeout(2500);
const stepStill0 = (await page.locator('[data-mining-step="pool"].current').count()) > 0;
const leftover = await page.evaluate(() => localStorage.getItem("mining_last_run_id"));
console.log("仍是 Step1:", stepStill0, "| 残留 key:", JSON.stringify(leftover));
await browser.close();