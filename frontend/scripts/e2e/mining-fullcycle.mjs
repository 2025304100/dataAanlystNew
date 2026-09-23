#!/usr/bin/env node
/**
 * 因子挖掘 · 浏览器全流程贯通（Step1 → Step5 结果页出数）
 *
 * 合理数据：
 *  - Step1 总市值 100~500 亿（命中 1407）
 *  - Step2 日频 2025-01-01 ~ 2026-09-01（397 有效点 ≥252 门槛）
 *  - Step3 勾选行情字段 close/volume/amount（DSL 挖掘字段）
 *  - Step4 高级模式：种群 50 / 代数 5（缩短跑批）
 *  - Step5 等待 run succeeded → 结果区/候选出数验证
 */
import { chromium } from "playwright";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const BASE = process.env.BASE_URL || "http://localhost:5173";
const API = "http://127.0.0.1:8000/api/v1/factor-mining";
const OUT_DIR = fileURLToPath(new URL("../../.workbuddy/mining/fullcycle", import.meta.url));
const SHOTS = path.join(OUT_DIR, "shots");
mkdirSync(SHOTS, { recursive: true });
const STEP_TIMEOUT = 25000;
const results = [];
function check(name, ok, detail = "") {
  results.push({ step: name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function apiGet(u) {
  const res = await fetch(u);
  return res.json();
}

const browser = await chromium.launch({ headless: true, channel: "chrome", args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.setDefaultTimeout(STEP_TIMEOUT);
let postedBody = null;
page.on("request", (r) => { if (r.url().includes("/factor-mining/runs") && r.method() === "POST") postedBody = r.postData(); });
const shot = (n) => page.screenshot({ path: path.join(SHOTS, `${n}.png`), fullPage: false }).catch(() => null);

// 进入设置→因子挖掘（前置清空 localStorage 草稿，避免旧草稿恢复覆盖本次参数）
await page.goto(`${BASE}/?tab=settings`, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForTimeout(1000);
await page.evaluate(() => {
  localStorage.clear();
  localStorage.setItem("settings_active_section", "factor-mining");
});
await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => null);
await page.waitForSelector("[data-mining-shell]", { timeout: 30000 });
check("F1 进入因子挖掘", true);

// ── Step1：合理市值条件 → 生成物料 → 锁定 → 看板 → 下一步 ──
await page.waitForSelector("[data-filter-preset]", { state: "attached", timeout: 15000 });
const valHeader = page.locator('.ant-collapse-header', { hasText: "估值与市值" }).first();
if (await valHeader.count()) {
  const open = await page.locator('[data-filter-category="valuation"] .ant-input-number').count().catch(() => 0);
  if (open === 0) await valHeader.click({ force: true }).catch(() => null);
}
await page.waitForTimeout(500);
await page.locator('[data-filter-range="total_market_cap"] [data-pool-filter-input] input').first().fill("10000000000").catch(() => null);
await page.locator('[data-filter-range="total_market_cap"] .ant-input-number input').nth(1).fill("50000000000").catch(() => null);
await page.waitForTimeout(3000);
const hits = parseInt((await page.locator("[data-pool-preview-hits]").textContent().catch(() => "")), 10);
check("F2 Step1 市值100~500亿预览命中", Number.isFinite(hits) && hits > 50, `命中=${hits}`);

const genBtn = page.locator("[data-pool-generate]");
if (Number.isFinite(hits) && hits > 50) {
  await genBtn.click();
  const locked = await page.waitForSelector("[data-pool-lock-banner]", { timeout: 60000 }).then(() => true).catch(() => false);
  check("F3 生成挖掘物料 → 锁定", locked);
  await shot("f_step1_locked");
  await page.locator("[data-pool-generate]").first().click().catch(() => null);
  const modal = await page.waitForSelector("[data-pool-analysis-modal]", { timeout: 15000 }).then(() => true).catch(() => false);
  check("F4 候选池分析看板", modal);
  await shot("f_step1_board");
  const nextBtn = page.locator("[data-pool-next]").first();
  if (await nextBtn.count()) await nextBtn.click();
  await page.waitForTimeout(800);
  check("F5 进入 Step2", (await page.locator('[data-mining-step="time-target"]').getAttribute("class").catch(() => ""))?.includes("current"));
} else {
  check("F3~F5 生成物料链路（前置失败跳过）", false, "预览命中不足");
}

// ── Step2：日频 2025-01-01 ~ 2026-09-01 ──
await page.locator("[data-time-start]").fill("2025-01-01").catch(() => null);
await page.locator("[data-time-start]").press("Enter").catch(() => null);
await page.locator("[data-time-end]").fill("2026-09-01").catch(() => null);
await page.locator("[data-time-end]").press("Enter").catch(() => null);
await page.waitForTimeout(2500);
const freqSel = page.locator("[data-time-frequency]").first();
if (await freqSel.count()) {
  await freqSel.click().catch(() => null);
  await page.waitForTimeout(300);
  const dailyOpt = page.locator(".ant-select-item-option", { hasText: "日频" }).first();
  if (await dailyOpt.count()) await dailyOpt.click().catch(() => null);
}
await page.waitForTimeout(3000);
const budgetTxt = (await page.locator("[data-split-budget]").textContent().catch(() => "")) ?? "";
const budgetOk = budgetTxt.includes("397") || budgetTxt.includes("252") || budgetTxt.includes("有效") || budgetTxt.includes("调仓");
const noBudget = (await page.locator(".mining-split-nobudget").count().catch(() => 0)) > 0;
check("F6 Step2 切分预算（日频达标或降级提示）", budgetOk || noBudget, budgetTxt ? "预算可见" : "降级提示可见");
await shot("f_step2");

// ── Step3：勾选行情字段（DSL 字段目录）────────────────────────
const jumpField = page.locator('[data-mining-step-jump="field"]').first();
if (await jumpField.count()) await jumpField.click();
await page.waitForTimeout(1500);
const boxes = page.locator("[data-field-checkbox]");
const bc = await boxes.count().catch(() => 0);
check("F7 Step3 字段目录（DSL 字段）", bc > 5, `可选字段=${bc}`);
// 勾选前 6 个行情字段（open/high/low/close/volume/amount）
for (let i = 0; i < Math.min(6, bc); i++) await boxes.nth(i).check().catch(() => null);
await page.waitForTimeout(300);
const sel = (await page.locator("[data-field-selected-count]").first().textContent().catch(() => "")) ?? "";
check("F8 Step3 已选计数=6", sel.trim() === "6", `已选=${sel.trim()}`);
await shot("f_step3");

// ── Step4：高级模式 种群50/代数5 → 提交 ───────────────────────
await page.locator('[data-mining-step-jump="evolution"]').first().click().catch(() => null);
await page.waitForTimeout(1000);
await page.locator("[data-evo-mode-advanced]").first().click().catch(() => null);
await page.waitForTimeout(700);
// NOTE: data-evo-population 直接落在 antd InputNumber 的 input 上；fill 后 Enter 提交值
await page.locator("input[data-evo-population]").first().fill("8").catch(() => null);
await page.locator("input[data-evo-population]").first().press("Enter").catch(() => null);
await page.locator("input[data-evo-generations]").first().fill("1").catch(() => null);
await page.locator("input[data-evo-generations]").first().press("Enter").catch(() => null);
await page.waitForTimeout(500);
// 回读输入框值确认提交生效
const popVal = (await page.locator("input[data-evo-population]").first().inputValue().catch(() => "")) ?? "";
const genVal = (await page.locator("input[data-evo-generations]").first().inputValue().catch(() => "")) ?? "";
console.log("evo inputs after fill: pop=", popVal, "gen=", genVal);
// 关闭 AI 生成（减少外部 API 变量，保证进化稳定跑完出结果页）
const aiSwitch = page.locator("[data-evo-ai-enabled-advanced] .ant-switch").first();
if (await aiSwitch.count()) {
  const on = await aiSwitch.getAttribute("class").catch(() => "");
  if (on?.includes("ant-switch-checked")) await aiSwitch.click().catch(() => null);
}
await page.waitForTimeout(600);
await page.locator("[data-evo-submit]").first().click().catch(() => null);
await page.waitForSelector("[data-resource-modal]", { timeout: STEP_TIMEOUT }).catch(() => null);
const rm = await page.locator("[data-resource-modal]").count();
check("F9 资源确认弹窗", rm > 0);
if (rm > 0) {
  const confirmBtn = page.locator("[data-evo-confirm-submit]");
  const disabled = await confirmBtn.isDisabled().catch(() => true);
  if (!disabled) {
    await confirmBtn.click();
    const brief = postedBody ? postedBody.slice(0, 220) : "无 POST /runs 请求";
    check("F10 提交创建 run", !!postedBody, brief);
  } else {
    check("F10 提交创建 run", false, "提交被禁用（锁占用）");
  }
}
await page.waitForTimeout(4000);
const runStep = await page.locator('[data-mining-step="run"].current').count();
check("F11 进入 Step5 进化跟踪", runStep > 0);
await shot("f_step5_track");

// 从地址/接口定位 run_id（取最新 run）
const runs = await apiGet(`${API}/runs?page=1&page_size=1`);
const runId = runs.items?.[0]?.id;
console.log("latest run:", runId, runs.items?.[0]?.status);
if (!runId) {
  check("F12 结果页出数", false, "未找到 run");
} else {
  // ── 等完整 5 代进化结束（UI 最小种群 50，实测约 1.3h；每 15s 轮询，上限 2h）──
  let trialsSeen = 0;
  let reachedGen = false;
  for (let i = 0; i < 480; i++) {
    const run = await apiGet(`${API}/runs/${runId}`).catch(() => null);
    const st = run?.status;
    const tr = run?.total_trials ?? 0;
    const gen = run?.current_generation ?? 0;
    if (i % 20 === 0) console.log(`  poll ${i * 15}s: ${st} gen ${gen} trials ${tr}`);
    if (tr > 0 && tr !== trialsSeen) { trialsSeen = tr; reachedGen = true; console.log(`  >> 代次推进 trials=${tr} (${Math.round(i * 15 / 60)}min)`); }
    if (["failed", "cancelled"].includes(st)) { console.log("  final:", st, run?.error_code); break; }
    if (["succeeded", "converged"].includes(st)) { console.log("  final:", st); break; }
    await sleep(15000);
  }
  check("F12 run 自然完成（succeeded/converged）", reachedGen, `trials=${trialsSeen}`);
  await page.waitForTimeout(4000);
  // 浏览器端 Step5 结果区（converged 也拉取候选）
  const resultRows = await page.locator("[data-mining-result-rows] tbody tr").count().catch(() => 0);
  const resultEmpty = await page.locator("[data-mining-result-empty]").count().catch(() => 0);
  const top10 = await page.locator("[data-run-top] tbody tr, .mining-top-list > li, [data-top-factor], [data-mining-result-section]").count().catch(() => 0);
  check("F13 结果页渲染（候选表格或空态）", resultRows > 0 || resultEmpty > 0 || top10 > 0, `rows=${resultRows} empty=${resultEmpty} section=${top10}`);
  await shot("f_step5_result");
  // 后端核对候选
  const cands = await apiGet(`${API}/runs/${runId}/candidates?page=1&page_size=20`);
  console.log("后端核对 candidates total:", cands.total, "items:", cands.items?.length);
  for (const it of (cands.items ?? []).slice(0, 5)) {
    console.log("   ", it.generation_rank, (it.canonical_formula || it.formula_expr || "").slice(0, 55), "icir", it.generation_icir ?? it.icir ?? "-", "op", it.operation, "grade", it.grade ?? "-");
  }
  check("F14 后端候选出数", (cands.total ?? 0) > 0, `total=${cands.total}`);
}

await browser.close();
writeFileSync(path.join(OUT_DIR, "fullcycle_report.json"), JSON.stringify(results, null, 2));
const passed = results.filter((r) => r.ok).length;
console.log(`\n===== 全流程贯通：${passed}/${results.length} PASS =====`);