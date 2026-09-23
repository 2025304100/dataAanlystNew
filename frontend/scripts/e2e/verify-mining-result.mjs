#!/usr/bin/env node
/** 验证：从批次列表点击「查看」打开已完成 run → Step5 结果区出数（P6 闭环路径）。 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const BASE = "http://localhost:5173";
const RUN_ID = "fcd1ca4679264fbcb81804cdf12774d5";
const OUT = fileURLToPath(new URL("../../.workbuddy/mining/resultview", import.meta.url));
mkdirSync(OUT, { recursive: true });
const results = [];
const check = (n, ok, d = "") => { results.push({ step: n, ok, detail: d }); console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`); };

const browser = await chromium.launch({ headless: true, channel: "chrome", args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.setDefaultTimeout(25000);
await page.goto(`${BASE}/?tab=settings`, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForTimeout(1000);
await page.evaluate(() => { localStorage.clear(); localStorage.setItem("settings_active_section", "factor-mining"); });
await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForSelector("[data-mining-shell]", { timeout: 30000 });
check("V1 进入因子挖掘", true);

// 切到「批次列表」
await page.locator('.mining-shell .sub-tabs .sub-tab:has-text("批次列表")').first().click().catch(() => null);
await page.waitForTimeout(1500);
const runsRows = await page.locator("[data-mining-runs-table] tbody tr").count().catch(() => 0);
check("V2 批次列表渲染", runsRows > 0, `行=${runsRows}`);

// 点击该 succeeded run 的「查看」
const viewBtn = page.locator(`[data-mining-run-view="${RUN_ID}"]`).first();
check("V3 目标 run 存在「查看」按钮", (await viewBtn.count()) > 0);
if (await viewBtn.count()) {
  await viewBtn.click();
  await page.waitForTimeout(2500);
}
const step5 = (await page.locator('[data-mining-step="run"].current').count()) > 0;
check("V4 跳转 Step5", step5);
await page.waitForTimeout(2500);
const rows = await page.locator("[data-mining-result-rows] tbody tr").count().catch(() => 0);
const empty = await page.locator("[data-mining-result-empty]").count().catch(() => 0);
const section = await page.locator("[data-mining-result-section]").count().catch(() => 0);
check("V5 结果区出数（候选表格）", rows > 0, `rows=${rows} section=${section}`);
await page.screenshot({ path: path.join(OUT, "result_view.png"), fullPage: false });
// 读首行内容佐证
if (rows > 0) {
  const first = (await page.locator("[data-mining-result-rows] tbody tr").first().textContent().catch(() => "")) ?? "";
  console.log("首行候选:", first.replace(/\s+/g, " ").slice(0, 140));
}
await browser.close();
writeFileSync(path.join(OUT, "resultview_report.json"), JSON.stringify(results, null, 2));
console.log(`\n===== 结果查看验证：${results.filter((r) => r.ok).length}/${results.length} PASS =====`);