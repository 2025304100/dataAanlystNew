import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

/**
 * T27 前端入口测试（设计 §9.1 / 裁决 D1）
 *
 * 覆盖 artifacts「菜单可见 + 跳转生效」与 pitfalls「最易漏 L76 白名单」：
 *   1. 左侧导航出现「因子挖掘」一栏（菜单可见）
 *   2. 点击该栏 → 挂载 MiningShell 内容块（点击可达）
 *   3. window 派发 `settings:navigate` detail="factor-mining" → 跳转生效
 *      ← **L76 白名单守卫**：白名单不加该项则本用例失败（本卡最易漏）
 *   4. 白名单之外的值（如 "bogus"）不得误切换
 *   5. 切换后 localStorage 持久化（刷新后仍在因子挖掘栏）
 *
 * Settings.tsx 直接 import 20+ 业务组件（含 AppContext），此处全部桩掉，
 * 只保留被测的导航/白名单/内容块逻辑与真实 MiningShell（验证其已挂载）。
 */

// ── 桩：AppContext ──────────────────────────────────────────────────
const ruleContext = vi.hoisted(() => ({ rule: null as any, save: vi.fn(async () => undefined) }));
vi.mock("../../context/AppContext", () => ({
  useApp: () => ({
    signalRule: ruleContext.rule,
    signalRulePresets: [],
    signalRulePreview: null,
    activeSymbolId: null,
    loadSignalRulePreview: vi.fn(async () => undefined),
    saveSignalRule: ruleContext.save,
  }),
}));

// ── 桩：mining API（MiningShell 挂载时会拉取批次列表）────────────────
vi.mock("../../api/factorMining", () => ({
  factorMiningApi: {
    listRuns: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    getLockStatus: vi.fn(async () => ({})),
  },
}));

// ── 桩：其余业务组件 ────────────────────────────────────────────────
// vi.mock 工厂被提升到文件顶部，工厂内不得引用外层变量（TDZ），逐条内联。
vi.mock("../DbConfigSection", () => ({ __esModule: true, default: () => null }));
vi.mock("../CustomIndicatorSettings", () => ({ __esModule: true, default: () => null }));
vi.mock("../DiscoveryPlanSettings", () => ({ __esModule: true, default: () => null }));
vi.mock("../HistoryInitSection", () => ({ __esModule: true, default: () => null }));
vi.mock("../DataDiagnosticPanel", () => ({ __esModule: true, default: () => null }));
vi.mock("../TaskCenter", () => ({ __esModule: true, default: () => null }));
vi.mock("../AlertCenter", () => ({ __esModule: true, default: () => null }));
vi.mock("../ScoringConfigSettings", () => ({ __esModule: true, default: () => null }));
vi.mock("../AkshareApiManager", () => ({ __esModule: true, default: () => null }));
vi.mock("../DataCenter", () => ({ __esModule: true, default: () => null }));
vi.mock("../FactorModelSettings", () => ({ __esModule: true, default: () => null }));
vi.mock("../factors/FactorCenter", () => ({ __esModule: true, default: () => null }));
vi.mock("../ScheduledTaskManager", () => ({ __esModule: true, default: () => null }));
vi.mock("../AiConfigSection", () => ({ __esModule: true, default: () => null }));
vi.mock("../ai/AISettings", () => ({ __esModule: true, default: () => null }));
vi.mock("../notifications/ChannelConfig", () => ({ __esModule: true, default: () => null }));
vi.mock("../notifications/PolicyEditor", () => ({ __esModule: true, default: () => null }));
vi.mock("../notifications/TemplateEditor", () => ({ __esModule: true, default: () => null }));
vi.mock("../notifications/DeliveryLog", () => ({ __esModule: true, default: () => null }));

import Settings from "../Settings";

const MINING_CONTENT = '[data-settings-content="settings-factor-mining"]';

function navigateTo(detail: string) {
  // 原生 dispatchEvent 不自带 act：不包裹则 React 状态更新不 flush，
  // DOM 与 localStorage 断言都看不到切换结果（fireEvent.click 内部自带 act）。
  act(() => {
    window.dispatchEvent(new CustomEvent("settings:navigate", { detail }));
  });
}

beforeEach(() => {
  ruleContext.rule = null;
  ruleContext.save.mockClear();
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe("Settings 因子挖掘入口", () => {
  it("点击保存规则提交当前规则", async () => {
    ruleContext.rule = { rule_name: "自定义规则", mode: "expert", quality_tolerance: 12, timing_tolerance: 12, min_sample_count: 3, max_samples: 60, same_region: true, same_asset_type: true, same_stage: true, same_action: true };
    render(<Settings />);
    fireEvent.click(screen.getByRole("button", { name: "保存规则" }));
    await waitFor(() => expect(ruleContext.save).toHaveBeenCalledOnce());
  });
  it("左侧导航出现「因子挖掘」一栏（菜单可见）", () => {
    render(<Settings />);
    const buttons = Array.from(document.querySelectorAll("button.settings-nav-item"));
    const labels = buttons.map((b) => b.textContent ?? "");
    expect(labels.some((x) => x.includes("因子挖掘"))).toBe(true);
  });

  it("点击「因子挖掘」导航 → 挂载 MiningShell 内容块", () => {
    render(<Settings />);
    const target = Array.from(document.querySelectorAll("button.settings-nav-item")).find(
      (b) => (b.textContent ?? "").includes("因子挖掘"),
    );
    expect(target).toBeTruthy();
    fireEvent.click(target as HTMLElement);
    expect(document.querySelector(MINING_CONTENT)).toBeTruthy();
    expect(document.querySelector("[data-mining-shell]")).toBeTruthy();
  });

  it("settings:navigate 派发 factor-mining → 跳转生效（L76 白名单守卫）", () => {
    render(<Settings />);
    expect(document.querySelector(MINING_CONTENT)).toBeNull();
    navigateTo("factor-mining");
    expect(document.querySelector(MINING_CONTENT)).toBeTruthy();
  });

  it("白名单之外的值不得误切换", () => {
    render(<Settings />);
    navigateTo("bogus-section");
    expect(document.querySelector(MINING_CONTENT)).toBeNull();
    // 默认栏仍为 rules
    expect(document.querySelector('[data-settings-content="settings-rules"]')).toBeTruthy();
  });

  it("切换后 localStorage 持久化 settings_active_section", () => {
    render(<Settings />);
    navigateTo("factor-mining");
    expect(window.localStorage.getItem("settings_active_section")).toBe("factor-mining");
  });

  it("旧值 factor-basic 收敛到因子与模型分区", () => {
    window.localStorage.setItem("settings_active_section", "factor-basic");
    render(<Settings />);
    expect(document.querySelector('[data-settings-content="settings-factor-model"]')).toBeTruthy();
    expect(document.querySelector('[data-settings-content="settings-factor-basic"]')).toBeNull();
  });

  it("因子中心跳转按钮方案下不新增顶级 Tab（not_do 守卫）", () => {
    render(<Settings />);
    // 全部导航项仍在 settings-sidebar 内，未新增 App 级容器
    const sidebars = document.querySelectorAll("nav.settings-sidebar");
    expect(sidebars.length).toBe(1);
  });

  it.each(["factor-model", "api-management"])("数据中心 coverage remediation 导航 %s 可切换设置分区", (target) => {
    render(<Settings />);
    navigateTo(target);
    expect(window.localStorage.getItem("settings_active_section")).toBe(target === "api-management" ? "api-mgmt" : target);
    expect(document.querySelector(`[data-settings-content="settings-${target === "api-management" ? "api-mgmt" : target}"]`)).toBeTruthy();
  });
});
