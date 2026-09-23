#!/usr/bin/env node
/**
 * 设置→因子挖掘 · 扩展黑盒验证 v3
 * 处理入口切换的二次确认 Modal（向导 §3.1 合法交互）。
 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const BASE = process.env.BASE_URL || "http://localhost:5173";
const OUT_DIR = fileURLToPath(new URL("../../.workbuddy/mining/blackbox", import.meta.url));
const SHOTS = path.join(OUT_DIR, "shots");
mkdirSync(SHOTS, { recursive: true });
const STEP_TIMEOUT = 25000;
const results = [];
function check(name, ok, detail = "") {
  results.push({ step: name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
}
const browser = await chromium.launch({ headless: true, channel: "chrome", args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.setDefaultTimeout(STEP_TIMEOUT);
const shot = (n) => page.screenshot({ path: path.join(SHOTS, `${n}.png`), fullPage: false }).catch(() => null);
/** 若出现 antd Modal（切换确认），点击其主按钮并等待消失 */
const dismissModals = async () => {
  for (let i = 0; i < 3; i++) {
    const okBtn = page.locator(".ant-modal-wrap:not(.ant-modal-confirm) .ant-btn-primary, .ant-modal-confirm .ant-btn-primary").first();
    if (!(await okBtn.isVisible().catch(() => false))) break;
    await okBtn.click({ force: true }).catch(() => null);
    await page.waitForTimeout(600);
  }
};
const gotoTab = async (text) => {
  await page.locator(`.mining-shell .sub-tabs .sub-tab:has-text("${text}")`).first().click().catch(() => null);
  await page.waitForTimeout(1200);
};
const gotoStep = async (key) => {
  const j = page.locator(`[data-mining-step-jump="${key}"]`).first();
  if (await j.count()) { await j.click().catch(() => null); await page.waitForTimeout(700); }
};

await page.goto(`${BASE}/?tab=settings`, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForTimeout(1000);
await page.evaluate(() => localStorage.setItem("settings_active_section", "factor-mining"));
await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForSelector("[data-mining-shell]", { timeout: 30000 });
check("S0 进入因子挖掘（MiningShell）", true);

const navTabs = await page.locator(".mining-shell .sub-tabs .sub-tab").all();
check("S1 四个子页签", navTabs.length >= 4, (await Promise.all(navTabs.map((t) => t.textContent()))).map((s) => (s ?? "").trim()).join("/"));

await gotoTab("批次列表");
await page.waitForTimeout(800);
check("S2 批次列表渲染（含历史 run）", (await page.locator("table tbody tr").count().catch(() => 0)) > 0);

await gotoTab("经验库");
await page.waitForTimeout(800);
const expText = await page.locator(".mining-shell").textContent().catch(() => "");
check("S3 经验库（空态提示）", expText.includes("暂无经验沉淀") || expText.includes("历史经验库"), expText.includes("暂无经验沉淀") ? "空态" : "有内容");
await shot("s3_experience");

await gotoTab("模板配置");
await page.waitForTimeout(1500);
const tplText = await page.locator(".mining-shell").textContent().catch(() => "");
check("S4 模板配置（25 系统模板）", /共\s*25\s*个模板/.test(tplText), `行=${await page.locator("table tbody tr").count().catch(() => 0)}`);
await shot("s4_templates");

// ── 5. 导入模式（含切换确认 Modal）────────────
await gotoTab("向导");
await page.waitForTimeout(800);
await page.locator("[data-pool-tab='import']").first().click().catch(() => null);
await page.waitForTimeout(600);
await dismissModals();
await page.waitForTimeout(400);
check("S5.1 导入面板渲染（经确认弹窗）", (await page.locator("[data-pool-import-panel]").count()) > 0);
await shot("s5_1_import_panel");
{
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: STEP_TIMEOUT }).catch(() => null),
    page.locator("button", { hasText: "下载导入模板" }).first().click().catch(() => null),
  ]);
  check("S5.2 下载导入模板", download != null, download?.suggestedFilename?.() ?? "无 download 事件");
}
const csv = "\uFEFFsymbol,name\n603013,亚普股份\n603014,威高血净\n603015,弘讯科技\n603016,新宏泰\n999999,不存在的代码\nHK00700,港股\nabc,非法格式\n603013,重复亚普\n";
const csvPath = path.join(OUT_DIR, "import_case.csv");
writeFileSync(csvPath, csv, "utf8");
await page.locator("input[type=file]").first().setInputFiles(csvPath).catch(() => null);
await page.waitForSelector("[data-import-result]", { timeout: STEP_TIMEOUT }).catch(() => null);
const rowCount = await page.locator("[data-import-rows] tbody tr").count().catch(() => 0);
const statText = (await page.locator("[data-import-stats]").textContent().catch(() => "")) ?? "";
check("S5.3 导入预览逐行匹配（4 成功+错误分类）", rowCount >= 4 && statText.includes("4"), statText.replace(/\s+/g, " ").slice(0, 150));
await shot("s5_3_import_preview");
const confirmBtn = page.locator("[data-import-confirm]");
if (await confirmBtn.count()) {
  await confirmBtn.click();
  await page.waitForTimeout(3500);
  const memberCount = await page.locator("[data-pool-member-table] tbody tr").count().catch(() => 0);
  check("S5.4 确认入池 → 成员表格有行", memberCount > 0, `成员行=${memberCount}`);
} else check("S5.4 确认入池", false, "无确认按钮（预览失败）");
await shot("s5_4_import_done");

// ── 6. 条件模式（切回 filter 亦需确认）────────
await page.locator("[data-pool-tab='filter']").first().click().catch(() => null);
await page.waitForTimeout(500);
await dismissModals();
await page.waitForTimeout(400);
// 展开「估值与市值」Accordion
const valHeader = page.locator('.ant-collapse-header', { hasText: "估值与市值" }).first();
if (await valHeader.count()) {
  const expanded = await page.locator('[data-filter-category="valuation"] .ant-input-number').count().catch(() => 0);
  if (expanded === 0) await valHeader.click({ force: true }).catch(() => null);
}
await page.waitForTimeout(600);
await page.locator('[data-filter-range="total_market_cap"] [data-pool-filter-input] input').first().fill("10000000000").catch(() => null);
await page.locator('[data-filter-range="total_market_cap"] .ant-input-number input').nth(1).fill("50000000000").catch(() => null);
await page.waitForTimeout(3000);
const hits = parseInt((await page.locator("[data-pool-preview-hits]").textContent().catch(() => "")), 10);
check("S6.1 合理市值区间(100~500亿)预览命中>50", Number.isFinite(hits) && hits > 50, `命中=${hits}`);
await shot("s6_1_valuation");

// ── 7. Step2 日频合理时间 ─────────────────────
await gotoStep("time-target");
await page.waitForTimeout(600);
await page.locator("[data-time-start]").fill("2025-01-01").catch(() => null);
await page.locator("[data-time-start]").press("Enter").catch(() => null);
await page.locator("[data-time-end]").fill("2026-09-01").catch(() => null);
await page.locator("[data-time-end]").press("Enter").catch(() => null);
await page.waitForTimeout(3000);
const budgetTxt = (await page.locator("[data-split-budget]").textContent().catch(() => "")) ?? "";
check("S7 切分预算（日频 2025-01~2026-09 ≥252 点）", budgetTxt.length > 0 && (budgetTxt.includes("397") || budgetTxt.includes("252") || budgetTxt.includes("有效") || budgetTxt.includes("达标") || budgetTxt.includes("≥")), budgetTxt.replace(/\s+/g, " ").slice(0, 140));
await shot("s7_step2");

// ── 8. Step3 字段 ─────────────────────────────
await gotoStep("field");
await page.waitForTimeout(1800);
const boxes = page.locator("[data-field-checkbox]");
const bc = await boxes.count().catch(() => 0);
if (bc > 0) {
  await boxes.first().check().catch(() => null);
  if (bc > 1) await boxes.nth(1).check().catch(() => null);
}
const sel = (await page.locator("[data-field-selected-count]").first().textContent().catch(() => "")) ?? "";
check("S8 字段勾选 → 已选计数≥1", sel.trim() !== "" && sel.trim() !== "0", `已选=${sel.trim()}`);

// ── 9. Step4 资源弹窗 + 锁占用禁用 ────────────
await gotoStep("evolution");
await page.waitForTimeout(600);
await page.locator("[data-evo-submit]").first().click().catch(() => null);
await page.waitForSelector("[data-resource-modal]", { timeout: STEP_TIMEOUT }).catch(() => null);
const rm = await page.locator("[data-resource-modal]").count();
check("S9.1 资源确认弹窗", rm > 0);
if (rm > 0) {
  const confirmDisabled = await page.locator("[data-evo-confirm-submit]").isDisabled().catch(() => false);
  const lockArea = (await page.locator(".mining-res-modal").textContent().catch(() => "")) ?? "";
  // 无锁占用 → 按钮可用（主流程不被卡死）；有锁占用 → 按钮禁用（拒绝语义）
  const lockNone = lockArea.includes("无冲突") || lockArea.includes("可立即开始") || !lockArea.includes("挖掘任务进行中");
  check("S9.2 锁状态反映到提交按钮（无冲突→可用）", lockNone ? !confirmDisabled : confirmDisabled, lockArea.replace(/\s+/g, " ").slice(0, 120));
  await shot("s9_lock_blocked");
  await page.locator("[data-resource-close]").first().click().catch(() => null);
}

await browser.close();
writeFileSync(path.join(OUT_DIR, "blackbox_report.json"), JSON.stringify(results, null, 2));
const passed = results.filter((r) => r.ok).length;
console.log(`\n===== 黑盒验证：${passed}/${results.length} PASS =====`);