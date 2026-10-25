import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { renderWithApp } from "../../../test/renderWithApp";
import { makeMockContext } from "../../../test/factories";
import { AppContext } from "../../../context/AppContext";
import Settings from "../../Settings";
import PortfolioStrategyRules from "../PortfolioStrategyRules";

const { mockApi, toast } = vi.hoisted(() => ({
  mockApi: {
    getPortfolioDetail: vi.fn(), getSignalRule: vi.fn(),
    scoringListModels: vi.fn(), scoringListFactorSets: vi.fn(),
    getFactorUsageOptions: vi.fn(), getCurrentFactorUsage: vi.fn(),
    savePortfolioStrategyConfiguration: vi.fn(),
    upsertPortfolioRule: vi.fn(), saveSignalRule: vi.fn(), updatePortfolio: vi.fn(),
  },
  toast: vi.fn(),
}));
vi.mock("../../../api/client", () => ({ api: mockApi }));
vi.mock("../../FactorModelSettings", () => ({ default: () => <div data-testid="real-training-pipeline">真实训练流水线</div> }));

const OPTIONS = {
  global_active_model_run_id: "real-model", global_weight_mode: "ridge", runtime_version: 9,
  factor_models: [{ factor_model_run_id: "real-model", factor_set_id: "frozen-set", model_name: "真实模型", production_eligible: true }, { factor_model_run_id: "demo-model", factor_set_id: "demo-set", model_name: "演示模型", production_eligible: false }],
  factor_sets: [{ factor_set_id: "frozen-set", name: "冻结集合", status: "frozen", member_count: 1, frozen_at: null }],
  portfolio_rules: [], defaults: {}, blocking_reasons: [],
};
const USAGE = { current: { factor_model_run_id: "old-model", factor_set_id: "old-set" }, history: [], latest_snapshot_id: "old-snapshot" };

function renderStrategy() {
  return renderWithApp(<PortfolioStrategyRules portfolioId={7} />, {
    contextValue: { portfolios: [{ id: 7, auto_trade_enabled: 0 } as never], portfolioId: 7, loadPortfolios: vi.fn().mockResolvedValue(undefined), showToast: toast },
    withAIAssistant: false,
  });
}

it.each([
  {sample_count: 25000, data_cutoff_at: "2026-10-20T08:00:00", expected: /样本=25,000，数据截止=2026-10-20 08:00/},
  {sample_count: undefined, data_cutoff_at: undefined, expected: /样本=—，数据截止=—/},
])("模型摘要保留真实训练证据，缺失证据不显示伪造零样本 $sample_count", async ({sample_count,data_cutoff_at,expected}) => {
  mockApi.getFactorUsageOptions.mockResolvedValue({...OPTIONS, factor_models: [{...OPTIONS.factor_models[0],sample_count,data_cutoff_at}]});
  renderStrategy();
  await pickOption(/validatedFactorModelLabel|已验证因子模型/, /真实模型/);
  expect(await screen.findByText(expected)).toBeInTheDocument();
});

/** antd Select：通过 combobox 的可访问名定位，mousedown 打开后按文本点选 */
async function pickOption(comboboxName: RegExp, optionMatch: RegExp | string) {
  const combobox = screen.getByRole("combobox", { name: comboboxName });
  fireEvent.mouseDown(combobox.closest(".ant-select-selector") as HTMLElement);
  await waitFor(() => {
    expect(document.querySelectorAll(".ant-select-item-option").length).toBeGreaterThan(0);
  });
  const isMatch = (txt: string) =>
    typeof optionMatch === "string" ? txt === optionMatch : optionMatch.test(txt);
  const target = Array.from(document.querySelectorAll(".ant-select-item-option")).find((o) =>
    isMatch(o.textContent ?? ""),
  ) as HTMLElement | undefined;
  if (!target) throw new Error(`未找到选项：${String(optionMatch)}`);
  fireEvent.click(target);
}

/** antd Select allowClear：hover 出清除按钮后点击清空 */
async function clearSelect(comboboxName: RegExp) {
  const combobox = screen.getByRole("combobox", { name: comboboxName });
  const wrapper = combobox.closest(".ant-select") as HTMLElement;
  fireEvent.mouseMove(wrapper);
  await waitFor(() => expect(wrapper.querySelector(".ant-select-clear")).toBeTruthy());
  // rc-select 清除按钮监听 mouseDown（非 click）
  fireEvent.mouseDown(wrapper.querySelector(".ant-select-clear") as HTMLElement);
}

/** 打开某 Select，返回当前弹层全部选项文本（用于断言可选项集合） */
async function openOptions(comboboxName: RegExp) {
  const combobox = screen.getByRole("combobox", { name: comboboxName });
  fireEvent.mouseDown(combobox.closest(".ant-select-selector") as HTMLElement);
  await waitFor(() => {
    expect(document.querySelectorAll(".ant-select-item-option").length).toBeGreaterThan(0);
  });
  return Array.from(document.querySelectorAll(".ant-select-item-option")).map((o) => o.textContent ?? "");
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.removeItem("settings_active_section");
  mockApi.getPortfolioDetail.mockResolvedValue({ portfolio: { auto_trade_enabled: 0 }, active_rule: null });
  mockApi.getSignalRule.mockResolvedValue(null);
  mockApi.scoringListModels.mockResolvedValue([{ id: "demo-model", name: "演示模型", status: "validated", factorset_id: "demo-set" }]);
  mockApi.scoringListFactorSets.mockResolvedValue([]);
  mockApi.getFactorUsageOptions.mockResolvedValue(OPTIONS);
  mockApi.getCurrentFactorUsage.mockResolvedValue(USAGE);
  mockApi.savePortfolioStrategyConfiguration.mockResolvedValue({ rule_id: 42, signal_rule_id: 8, applied: true, binding_result: { factor_usage: { factor_model_run_id: "real-model", factor_set_id: "frozen-set" }, strategy_snapshot_id: "new-snapshot", snapshot_hash: "new-hash" }, auto_trade_enabled: false, requires_confirmation: true });
});

describe("组合策略配置的真实运行绑定", () => {
  it("设置页尚未挂载时点训练新模型，首次进入实际落到流水线页", async () => {
    function NavigationHarness() {
      const [activeTab, setActiveTab] = useState("portfolio");
      const value = makeMockContext({ activeTab, setActiveTab, portfolioId: 7, portfolios: [{ id: 7, auto_trade_enabled: 0 } as never], loadPortfolios: vi.fn().mockResolvedValue(undefined), showToast: toast });
      return <AppContext.Provider value={value}>{activeTab === "settings" ? <Settings /> : <PortfolioStrategyRules portfolioId={7} />}</AppContext.Provider>;
    }
    renderWithApp(<NavigationHarness />, { contextValue: {}, withAIAssistant: false });
    fireEvent.click(await screen.findByRole("button", { name: /newModel|训练新模型/ }));
    expect(await screen.findByTestId("real-training-pipeline")).toBeInTheDocument();
    expect(document.querySelector('[data-settings-content="settings-factor-model"]')).toBeInTheDocument();
  });
  it("只提供正式可用模型，并将当前执行快照与编辑选择分别显示", async () => {
    renderStrategy();
    // 打开「已验证因子模型」下拉：只列正式可用模型（真实模型），不含演示模型
    const labels = await openOptions(/validatedFactorModelLabel|已验证因子模型/);
    expect(labels.some((l) => /真实模型/.test(l))).toBe(true);
    expect(labels.some((l) => /演示模型/.test(l))).toBe(false);
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.getByTestId("current-strategy-binding")).toHaveTextContent("old-snapshot");
    expect(screen.getByTestId("current-strategy-binding")).toHaveTextContent("old-model");
  });
  it("一次正式应用保存全部配置，显示新快照并保持关闭的自动接管", async () => {
    renderStrategy();
    await pickOption(/validatedFactorModelLabel|已验证因子模型/, /真实模型/);
    fireEvent.click(screen.getByRole("button", { name: /saveApply|保存并应用/ }));
    await waitFor(() => expect(mockApi.savePortfolioStrategyConfiguration).toHaveBeenCalledWith(7, expect.objectContaining({
      rule: expect.objectContaining({ is_active: true, factor_model_run_id: "real-model", factor_set_id: "frozen-set", max_loss_per_trade_pct: 0.05, stage_limits_json: expect.objectContaining({ timing_signal: "模型综合分", price_stop_loss_pct: 0.05 }) }),
      signal_rule: expect.any(Object),
      binding: { factor_model_run_id: "real-model", factor_set_id: "frozen-set", run_mode: "production_sim", pit_mode: "strict_pit_safe" },
    })));
    expect(mockApi.savePortfolioStrategyConfiguration.mock.calls[0][1]).not.toHaveProperty("auto_trade_enabled");
    expect(mockApi.upsertPortfolioRule).not.toHaveBeenCalled();
    expect(mockApi.saveSignalRule).not.toHaveBeenCalled();
    expect(mockApi.updatePortfolio).not.toHaveBeenCalled();
    expect(screen.getByTestId("current-strategy-binding")).toHaveTextContent("new-snapshot");
    expect(screen.getByTestId("current-strategy-binding")).toHaveTextContent("本人确认");
  });
  it("买入依据只读模型综合分，价格止损编辑独立于既有单笔风险预算", async () => {
    mockApi.getPortfolioDetail.mockResolvedValueOnce({ portfolio: { auto_trade_enabled: 0 }, active_rule: { max_loss_per_trade_pct: 0.012, factor_model_run_id: "real-model", factor_set_id: "frozen-set", stage_limits_json: { price_stop_loss_pct: 0.08, timing_signal: "MACD" } } });
    renderStrategy();
    const basis = await screen.findByRole("textbox", { name: "买入依据" });
    expect(basis).toHaveValue("模型综合分");
    expect(basis).toHaveAttribute("readonly");
    // 买入依据固定为模型综合分，不提供 MACD 等择时选项
    expect((basis as HTMLInputElement).value).not.toContain("MACD");
    // 止损当前 8%，改为 5%
    await pickOption(/持仓价格止损/, "5%");
    expect(screen.getByText(/持仓平均成本/)).toBeInTheDocument();
    expect(screen.getByText(/暂停新买.*保留卖出/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /saveApply|保存并应用/ }));
    await waitFor(() => expect(mockApi.savePortfolioStrategyConfiguration).toHaveBeenCalledWith(7, expect.objectContaining({ rule: expect.objectContaining({ max_loss_per_trade_pct: 0.012, stage_limits_json: expect.objectContaining({ price_stop_loss_pct: 0.05, drawdown_circuit_pct: 0.10, timing_signal: "模型综合分" }) }) })));
  });
  it("清空模型后同时清空因子集，草稿不携带接管或正式绑定意图", async () => {
    renderStrategy();
    await pickOption(/validatedFactorModelLabel|已验证因子模型/, /真实模型/);
    await screen.findByText("frozen-set");
    await clearSelect(/validatedFactorModelLabel|已验证因子模型/);
    await waitFor(() => expect(screen.queryByText("frozen-set")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: /saveApply|保存并应用/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /saveDraft|保存草稿/ }));
    await waitFor(() => expect(mockApi.savePortfolioStrategyConfiguration).toHaveBeenCalled());
    const payload = mockApi.savePortfolioStrategyConfiguration.mock.calls[0][1];
    expect(payload.rule).toMatchObject({ is_active: false, factor_set_id: null, factor_model_run_id: null });
    expect(payload).not.toHaveProperty("binding");
    expect(payload).not.toHaveProperty("auto_trade_enabled");
    expect(screen.getByTestId("current-strategy-binding")).toHaveTextContent("old-snapshot");
  });
  it("原子应用失败时保留旧执行快照并显示失败，不继续独立保存", async () => {
    mockApi.savePortfolioStrategyConfiguration.mockRejectedValueOnce(new Error("模型评分覆盖不足"));
    renderStrategy();
    await pickOption(/validatedFactorModelLabel|已验证因子模型/, /真实模型/);
    fireEvent.click(screen.getByRole("button", { name: /saveApply|保存并应用/ }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("error", "模型评分覆盖不足"));
    expect(screen.getByTestId("current-strategy-binding")).toHaveTextContent("old-snapshot");
    expect(toast).not.toHaveBeenCalledWith("success", expect.any(String));
    expect(mockApi.upsertPortfolioRule).not.toHaveBeenCalled();
    expect(mockApi.saveSignalRule).not.toHaveBeenCalled();
    expect(mockApi.updatePortfolio).not.toHaveBeenCalled();
  });
  it.each(["getPortfolioDetail", "getSignalRule"] as const)("%s 失败时不使用默认配置覆盖正式策略", async (method) => {
    mockApi[method].mockRejectedValueOnce(new Error("读取失败"));
    renderStrategy();
    await pickOption(/validatedFactorModelLabel|已验证因子模型/, /真实模型/);
    expect(screen.getByRole("button", { name: /saveApply|保存并应用/ })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("配置读取失败");
    expect(mockApi.savePortfolioStrategyConfiguration).not.toHaveBeenCalled();
  });
});
