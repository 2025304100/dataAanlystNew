#!/usr/bin/env node
/** 验证会话 id 直达：/settings/factor-mining?run=<id> 进入即恢复该 run 动态与结果。 */
import { chromium } from "playwright";

const BASE = "http://localhost:5173";
const RUN_ID = "fcd1ca4679264fbcb81804cdf12774d5";
const browser = await chromium.launch({ headless: true, channel: "chrome", args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.setDefaultTimeout(25000);

await page.goto(`${BASE}/?tab=settings&run=${RUN_ID}`, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForTimeout(1000);
await page.evaluate(() => localStorage.setItem("settings_active_section", "factor-mining"));
await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForSelector("[data-mining-shell]", { timeout: 30000 });
await page.waitForTimeout(4000);

const step5 = (await page.locator('[data-mining-step="run"].current').count()) > 0;
const rows = await page.locator("[data-mining-result-rows] tbody tr").count().catch(() => 0);
const track = await page.locator("[data-run-track]").count();
console.log("Step5 current:", step5, "| 结果区行数:", rows, "| 跟踪容器:", track);
const pageText = (await page.locator(".mining-shell").textContent().catch(() => "")) ?? "";
console.log("含状态标签:", /succeeded|成功/.test(pageText));
if (rows > 0) {
  const first = (await page.locator("[data-mining-result-rows] tbody tr").first().textContent().catch(() => "")) ?? "";
  console.log("首行候选:", first.replace(/\s+/g, " ").slice(0, 130));
}
await page.screenshot({ path: "session_direct.png", fullPage: false });
await browser.close();