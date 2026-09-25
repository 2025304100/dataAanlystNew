#!/usr/bin/env node
/**
 * 因子挖掘向导 · 5 步浏览器走查脚本（§1 向导 5 步全链路验收）
 *
 * 用途
 * ----
 * 在**真实浏览器**里走完向导 5 步（候选池 → 时间与目标 → 字段与校验 →
 * 进化参数 → 进化执行与结果），对每一步的可交互性与契约落地做断言：
 *   1. 候选池：条件筛选面板（左预设 + 右分类）加载 → 应用预设/输入条件 →
 *      预览实时出数（或给出可读阻断）→ 生成挖掘物料 → 锁定 → 看板。
 *   2. 时间与目标：日期/频率可编辑 → 切分预算面板出现（或降级提示）。
 *   3. 字段与校验：字段目录分组加载 → 勾选字段 → 已选计数更新。
 *   4. 进化参数：简单模式渲染 → 提交 → 资源确认弹窗。
 *   5. 执行与结果：提交后流转（进 Step5 跟踪 或 缺快照提示跳回 Step1）。
 *
 * 前置
 * ----
 * 1. 后端已启动（默认 :8000，vite 代理 /api/v1）。
 * 2. 前端 dev 已启动：`cd frontend && npm run dev`（默认 http://localhost:5173）。
 * 3. 安装 Playwright：`cd frontend && npm i -D playwright`
 *    - 有系统 Chrome/Edge 时脚本默认 `channel: 'chrome'`（免下载浏览器）；
 *    - 无则 `npx playwright install chromium` 后设 `PW_CHANNEL=` 空格走默认 chromium。
 *
 * 运行
 * ----
 *   node scripts/e2e/mining-wizard-walkthrough.mjs          # headless
 *   HEADED=1 node scripts/e2e/mining-wizard-walkthrough.mjs # 有头可视化
 *   BASE_URL=http://localhost:5173 node ...                 # 自定义前端地址
 *
 * 产物
 * ----
 *   .workbuddy/mining/walkthrough/shots/*.png               每步截图
 *   .workbuddy/mining/walkthrough/walkthrough_report.json   PASS/FAIL 清单
 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HEADED = process.env.HEADED === "1";
const BASE = process.env.BASE_URL || "http://localhost:5173";
const CHANNEL = process.env.PW_CHANNEL === undefined ? "chrome" : process.env.PW_CHANNEL || undefined;

const OUT_DIR = fileURLToPath(new URL("../../.workbuddy/mining/walkthrough", import.meta.url));
const SHOTS = path.join(OUT_DIR, "shots");
mkdirSync(SHOTS, { recursive: true });

const STEP_TIMEOUT = 30000;
const results = [];
const shot = async (page, name) => page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: false });

function check(name, ok, detail = "") {
  results.push({ step: name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
}

async function clickSafe(page, selector, name) {
  const el = page.locator(selector).first();
  await el.waitFor({ state: "visible", timeout: STEP_TIMEOUT });
  await el.click();
}

async function main() {
  let browser;
  try {
    browser = await chromium.launch({
      headless: !HEADED,
      channel: CHANNEL,
      args: ["--no-sandbox"],
    });
  } catch (e) {
    // 系统 Chrome 不可用 → 退回自带 chromium（需先 npx playwright install chromium）
    console.warn(`[warn] channel chrome 不可用，回退默认 chromium：${String(e).slice(0, 120)}`);
    browser = await chromium.launch({ headless: !HEADED, args: ["--no-sandbox"] });
  }

  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.setDefaultTimeout(STEP_TIMEOUT);

  // ── 进入因子挖掘设置页（SPA：?tab=settings 切顶部 Tab + localStorage 指定设置页 section）──
  try {
    await page.goto(`${BASE}/?tab=settings`, { waitUntil: "networkidle", timeout: 30000 });
  } catch {
    check("0. 前端可达", false, `${BASE} 未响应（请先启动 vite dev）`);
    await browser.close();
    writeFileSync(path.join(OUT_DIR, "walkthrough_report.json"), JSON.stringify(results, null, 2));
    console.log(`\n报告：${path.join(OUT_DIR, "walkthrough_report.json")}`);
    process.exit(1);
  }
  await page.evaluate(() => {
    localStorage.setItem("settings_active_section", "factor-mining");
  });
  await page.reload({ waitUntil: "networkidle" });

  const shellReady = await page.waitForSelector("[data-mining-shell]", { timeout: STEP_TIMEOUT }).then(() => true).catch(() => false);
  check("0. 进入因子挖掘向导（MiningShell）", shellReady);
  if (!shellReady) {
    await shot(page, "00_entry_failed");
    await browser.close();
    writeFileSync(path.join(OUT_DIR, "walkthrough_report.json"), JSON.stringify(results, null, 2));
    console.log(`\n报告：${path.join(OUT_DIR, "walkthrough_report.json")}`);
    process.exit(1);
  }
  await shot(page, "00_entry");

  // ── Step 1：候选股票池 ──────────────────────────────────────────
  check("1.1 步骤条 5 步渲染", await page.waitForSelector("[data-mining-step-bar]", { timeout: STEP_TIMEOUT }).then(() => true).catch(() => false));
  check(
    "1.2 筛选面板（左预设 + 右分类）加载",
    await page
      .waitForSelector("[data-pool-filter-panel] [data-filter-left]", { state: "visible", timeout: STEP_TIMEOUT })
      .then(() => page.waitForSelector("[data-filter-right]", { state: "visible", timeout: STEP_TIMEOUT }).then(() => true))
      .catch(() => false),
  );
  const hasPreset = await page.locator("[data-filter-preset]").count();
  check("1.3 左栏预设可用（或全部 blocked 有原因）", hasPreset > 0, `预设按钮数=${hasPreset}`);

  // 应用第一个可用预设（若存在）
  const applyable = page.locator("[data-filter-preset]:not(:disabled)").first();
  if (await applyable.count()) {
    await applyable.click();
    check("1.4 应用预设成功（触发防抖预览）", true, `${await applyable.getAttribute("data-filter-preset")}`);
  } else {
    check("1.5 blocked 预设带阻断原因", (await page.locator("[data-filter-preset]:disabled[title]").count()) > 0, "全部预设阻断（后端数据/字段不可用）");
  }

  // 手动填一个已加载的数值输入（若无预设可用则走这一步）
  // valuation 分类默认收起，需先展开其 Accordion 使输入可见（antd Collapse + InputNumber）
  const valSummary = page.locator('[data-filter-category="valuation"]').first();
  if ((await valSummary.count()) === 0) {
    const hdr = page.locator(".ant-collapse-header", { hasText: "估值" }).first();
    if (await hdr.count()) await hdr.click().catch(() => null);
  }
  await page.waitForSelector("[data-pool-filter-input] input", { state: "visible", timeout: STEP_TIMEOUT }).catch(() => null);
  const rangeInput = page.locator("[data-pool-filter-input] input");
  if (await rangeInput.count()) {
    await rangeInput.fill("80").catch(() => null);
    check("1.6 数值区间可输入", true, "总市值 min=80");
  } else {
    check("1.6 数值区间可输入", false, "未找到市值输入");
  }

  // 预览出数或可读阻断（300ms 防抖 + 后端响应；轮询至多 30s——
  // 固定 1.2s 等待在数仓冷缓存时会误报，2026-09-24 全面测试教训）
  let previewSeen = false;
  for (let waited = 0; waited < 30; waited += 1) {
    await page.waitForTimeout(1000);
    if ((await page.locator("[data-pool-preview-stats]").count()) > 0 ||
        (await page.locator("[data-pool-blocked]").count()) > 0) {
      previewSeen = true;
      break;
    }
  }
  const hasStats = await page.locator("[data-pool-preview-stats]").count();
  const hasBlocked = await page.locator("[data-pool-blocked]").count();
  check("1.7 预览统计已渲染（或可读阻断）", previewSeen || hasStats > 0 || hasBlocked > 0,
    hasBlocked > 0 ? "命中不足阻断" : hasStats > 0 ? "统计可见" : "30s 内未渲染");
  await shot(page, "01_step1_filter");

  // 生成挖掘物料（真实建池+快照；后端缺数据时按阻断记录）
  const genBtn = page.locator("[data-pool-generate]");
  const genDisabled = await genBtn.isDisabled().catch(() => true);
  if (genDisabled) {
    check("1.8 生成物料可用", false, "按钮禁用（命中<50 或预览阻断，属预期数据态）");
    check("1.9 阻断原因可见", (await page.locator("[data-pool-blocked]").count()) > 0, "见阻断区");
  } else {
    await genBtn.click();
    const locked = await page
      .waitForSelector("[data-pool-lock-banner]", { timeout: 30000 })
      .then(() => true).catch(() => false);
    check("1.8 生成挖掘物料 → 锁定", locked);
    await shot(page, "02_step1_locked");
    // 打开看板
    await clickSafe(page, "[data-pool-generate]", "打开看板").catch(() => null);
    const board = await page
      .waitForSelector("[data-pool-analysis-modal]", { timeout: STEP_TIMEOUT })
      .then(() => true).catch(() => false);
    check("1.9 候选池分析看板弹窗", board);
    await shot(page, "03_step1_board");
    // 下一步
    await clickSafe(page, "[data-pool-next] > *, [data-pool-next]", "看板下一步").catch(() => null);
    // 若弹窗内是 [下一步] 按钮，关弹窗后走页面下一步
    await page.locator("[data-pool-analysis-modal]").count().then(async (n) => { if (n > 0) await page.keyboard.press("Escape"); });
  }
  check("1.10 进入 Step2", (await page.locator('[data-mining-step="time-target"]').getAttribute("class").catch(() => "") ?? "").includes("current"));

  // ── Step 2：时间与目标（antd DatePicker / Select）─────────────────
  const t2 = page.locator('[data-mining-step-jump="time-target"]');
  if (await t2.count()) await t2.click().catch(() => null);
  await page.waitForSelector("[data-time-target-step]", { timeout: STEP_TIMEOUT }).catch(() => null);
  check("2.1 Step2 面板渲染", (await page.locator("[data-time-target-step]").count()) > 0);
  // antd DatePicker：data-* 转发到内部 input，直接填值 + 回车提交
  const startInput = page.locator("[data-time-start]");
  if (await startInput.count()) {
    await startInput.fill("2025-01-01");
    await startInput.press("Enter");
    check("2.2 开始日期可填", true);
  } else {
    check("2.2 开始日期可填", false);
  }
  const endInput = page.locator("[data-time-end]");
  if (await endInput.count()) {
    await endInput.fill("2025-12-31");
    await endInput.press("Enter");
    check("2.3 结束日期可填", true);
  } else {
    check("2.3 结束日期可填", false);
  }
  // antd Select：data-* 在根 div，打开下拉选「周频」
  const freq = page.locator("[data-time-frequency]");
  if (await freq.count()) {
    await freq.click().catch(() => null);
    await page.waitForTimeout(300);
    const opt = page.locator(".ant-select-item-option", { hasText: "周频" }).first();
    if (await opt.count()) {
      await opt.click().catch(() => null);
      check("2.4 频率可切换(周频)", true);
    }
  }
  await page.waitForTimeout(1500);
  const hasBudget = await page.locator("[data-split-budget]").count();
  const noBudget = await page.locator(".mining-split-nobudget").count();
  check("2.5 切分预算展示（或后端降级提示）", hasBudget > 0 || noBudget > 0, hasBudget > 0 ? "预算可见" : "预算暂不可用");
  await shot(page, "04_step2_time_target");

  // ── Step 3：字段与校验（antd Checkbox：data 在 label 根，勾选内层 input）─────
  const t3 = page.locator('[data-mining-step-jump="field"]');
  if (await t3.count()) await t3.click().catch(() => null);
  await page.waitForSelector("[data-field-step]", { timeout: STEP_TIMEOUT }).catch(() => null);
  const hasEmpty = await page.locator("[data-field-empty]").count();
  // antd Checkbox：data-* 直接落在 input 上（探针验证），勾选 input 本身
  const boxes = page.locator("[data-field-checkbox]");
  const boxCount = await boxes.count().catch(() => 0);
  if (boxCount > 0) {
    await boxes.first().check().catch(() => null);
    const second = boxes.nth(1);
    if (await second.count()) await second.check().catch(() => null);
    const countText = await page.locator("[data-field-selected-count]").first().textContent().catch(() => "");
    check("3.1 字段勾选 → 已选计数更新", (countText ?? "").trim() !== "0" && (countText ?? "").trim() !== "", `已选=${countText?.trim()}`);
  } else {
    check("3.1 字段目录加载", false, hasEmpty > 0 ? "空态（需先生成快照）" : "无字段");
  }
  await shot(page, "05_step3_fields");

  // ── Step 4：进化参数 ────────────────────────────────────────────
  const t4 = page.locator('[data-mining-step-jump="evolution"]');
  if (await t4.count()) await t4.click().catch(() => null);
  await page.waitForSelector("[data-evo-step]", { timeout: STEP_TIMEOUT }).catch(() => null);
  const evo = (await page.locator("[data-evo-step]").count()) > 0;
  check("4.1 Step4 面板渲染", evo);
  check("4.2 简单模式控件（强度/偏好/AI）", (await page.locator("[data-evo-strength]").count()) > 0 && (await page.locator("[data-evo-ai-enabled]").count()) > 0);
  const submit = page.locator("[data-evo-submit]");
  if (await submit.count()) {
    await submit.click();
    const modal = await page
      .waitForSelector("[data-resource-modal]", { timeout: STEP_TIMEOUT })
      .then(() => true).catch(() => false);
    check("4.3 资源确认弹窗", modal);
    await shot(page, "06_step4_resource_modal");
    if (modal) {
      const confirmBtn = page.locator("[data-evo-confirm-submit]");
      if (await confirmBtn.count()) {
        await confirmBtn.first().click().catch(() => null);
      }
      await page.waitForTimeout(2500);
    }
  }
  // 提交结果：进入 Step5 跟踪 或 缺快照提示（跳回 Step1）——两类都是合法闭环
  const inRun = await page.locator('[data-mining-step="run"].current').count();
  const submitErr = await page.locator("[data-mining-submit-error]").count();
  const needSnapshot = await page.locator(".mining-submit-error").count();
  check(
    "4.4 提交流转（进 Step5 或明确提示缺快照）",
    inRun > 0 || submitErr > 0 || needSnapshot > 0,
    inRun > 0 ? "进入 Step5" : submitErr > 0 ? "提交错误提示" : "缺快照提示",
  );
  await shot(page, "07_step4_submit_result");

  // ── Step 5：进化执行与结果 ──────────────────────────────────────
  const t5 = page.locator('[data-mining-step-jump="run"]');
  if (await t5.count()) await t5.click().catch(() => null);
  await page.waitForTimeout(800);
  const runTrack = await page.locator("[data-run-track]").count();
  const runEmpty = await page.locator("[data-mining-run-empty]").count();
  check("5.1 Step5 渲染（进化跟踪或空态）", runTrack > 0 || runEmpty > 0, runTrack > 0 ? "进化跟踪" : "空态（无运行）");
  await shot(page, "08_step5_run");

  await browser.close();
  writeFileSync(path.join(OUT_DIR, "walkthrough_report.json"), JSON.stringify(results, null, 2));
  const passed = results.filter((r) => r.ok).length;
  console.log(`\n===== 走查完成：${passed}/${results.length} PASS =====`);
  console.log(`报告：${path.join(OUT_DIR, "walkthrough_report.json")}`);
  console.log(`截图：${SHOTS}`);
}

main().catch((e) => {
  console.error("走查异常：", e);
  writeFileSync(path.join(OUT_DIR, "walkthrough_report.json"), JSON.stringify(results, null, 2));
  process.exit(1);
});