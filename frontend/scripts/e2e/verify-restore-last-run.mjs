#!/usr/bin/env node
/** 验证：提交/查看后退出前台重新进来（无 URL 参数）→ 自动恢复最近 run 的 Step5 视图。 */
import { chromium } from "playwright";
const BASE = "http://localhost:5173";
const RUN_ID = "fcd1ca4679264fbcb81804cdf12774d5";
const browser = await chromium.launch({ headless: true, channel: "chrome", args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.setDefaultTimeout(25000);

await page.goto(`${BASE}/?tab=settings`, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForTimeout(800);
// 模拟「提交过 run」留下的最近会话记录
await page.evaluate((rid) => {
  localStorage.clear();
  localStorage.setItem("settings_active_section", "factor-mining");
  localStorage.setItem("mining_last_run_id", rid);
}, RUN_ID);
await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForSelector("[data-mining-shell]", { timeout: 30000 });
await page.waitForTimeout(3000);

const step5 = (await page.locator('[data-mining-step="run"].current').count()) > 0;
const rows = await page.locator("[data-mining-result-rows] tbody tr").count().catch(() => 0);
const track = await page.locator("[data-run-track]").count();
console.log("无 URL 参数重进 → Step5:", step5, "| 跟踪容器:", track, "| 结果区行数:", rows);
await page.screenshot({ path: "restore_last_run.png", fullPage: false });
await browser.close();