import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getWatchlists: vi.fn(async () => [{ id: 1, name: "观察池", list_type: "watch" }]),
    getExternalDataOverview: vi.fn(async () => ({
      datasets: [], total_records: 0, covered_symbols: 0,
      available_datasets: 0, running_tasks: 0, refreshed_at: "2026-10-14T08:00:00",
    })),
    getExternalDataCoverage: vi.fn(async () => ({ generated_at: "2026-10-14T08:00:00", datasets: [] })),
    getExternalDataSyncCapabilities: vi.fn(async () => ({
      fundamental: { modes: ["incremental", "backfill"], history_limit_days: null, reason: "可读取真实历史" },
      capital_flow: { modes: ["incremental", "backfill"], history_limit_days: 100, reason: "真实历史", northbound: { available: true, reason: "可用" } },
    })),
    scoringCreateTask: vi.fn(),
    startExternalDataSync: vi.fn(),
  },
}));

vi.mock("../../../api/client", () => ({ api: mockApi }));

import FactorPipelinePrepareStep from "../FactorPipelinePrepareStep";
import ExternalDataSync from "../../ExternalDataSync";

const createProps = () => ({
  loading: false,
  featureEnabled: true,
  warehouseAvailable: true,
  coverage: 0.323,
  latestDate: "2026-10-12",
  onEnable: vi.fn(),
  onInitialize: vi.fn(),
  onRefresh: vi.fn(),
  onViewChange: vi.fn(),
});

async function chooseInputSelect(index: number, label: string) {
  fireEvent.mouseDown(document.querySelectorAll(".ant-select-selector")[index]);
  const option = await waitFor(() => {
    const item = Array.from(document.querySelectorAll(".ant-select-item-option"))
      .find((candidate) => candidate.textContent === label);
    expect(item).toBeDefined();
    return item!;
  });
  fireEvent.click(option);
}

async function setInputDraft() {
  const user = userEvent.setup();
  const settings = screen.getByRole("button", { name: /日期和更新设置/ });
  if (settings.getAttribute("aria-expanded") === "false") await user.click(settings);
  await chooseInputSelect(0, "已保存行情的全部股票和 ETF");
  const northbound = screen.getByRole("checkbox", { name: "一并更新北向资金" });
  await waitFor(() => expect(northbound).toBeEnabled());
  await user.click(northbound);
  await chooseInputSelect(1, "指定历史日期");
  await user.clear(screen.getByPlaceholderText("开始日期"));
  await user.type(screen.getByPlaceholderText("开始日期"), "2026-08-01");
  await user.keyboard("{Enter}");
  await user.clear(screen.getByPlaceholderText("结束日期"));
  await user.type(screen.getByPlaceholderText("结束日期"), "2026-08-13");
  await user.keyboard("{Enter}");
}

async function expectInputDraft() {
  const settings = screen.getByRole("button", { name: /日期和更新设置/ });
  if (settings.getAttribute("aria-expanded") === "false") fireEvent.click(settings);
  expect(screen.getAllByText("已保存行情的全部股票和 ETF").length).toBeGreaterThan(0);
  expect(screen.getAllByText("指定历史日期").length).toBeGreaterThan(0);
  expect(screen.getByPlaceholderText("开始日期")).toHaveValue("2026-08-01");
  expect(screen.getByPlaceholderText("结束日期")).toHaveValue("2026-08-13");
  expect(screen.getByText("自定义")).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "一并更新北向资金" })).not.toBeChecked();
}

describe("factor pipeline input preparation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  it("opens input operations as a separate page and returns to the refreshed input check", async () => {
    const user = userEvent.setup();
    const props = createProps();
    render(<FactorPipelinePrepareStep {...props} />);

    expect(screen.getByRole("heading", { name: "检查因子输入" })).toBeInTheDocument();
    expect(screen.queryByText("财报和其他数据")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "准备因子输入" }));

    expect(await screen.findByText("财报和其他数据")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "检查因子输入" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "刷新评分" })).not.toBeInTheDocument();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
    expect(mockApi.startExternalDataSync).not.toHaveBeenCalled();
    expect(props.onViewChange).toHaveBeenLastCalledWith("inputs");

    await user.click(screen.getByRole("button", { name: "返回输入检查" }));
    expect(screen.getByRole("heading", { name: "检查因子输入" })).toBeInTheDocument();
    expect(screen.queryByText("财报和其他数据")).not.toBeInTheDocument();
    expect(props.onRefresh).toHaveBeenCalledOnce();
    expect(props.onViewChange).toHaveBeenLastCalledWith("check");
  });

  it("keeps unknown input state distinct from confirmed missing data", async () => {
    const props = { ...createProps(), loading: true, featureEnabled: false, warehouseAvailable: false, coverage: null, latestDate: null };
    const { rerender } = render(<FactorPipelinePrepareStep {...props} />);

    expect(screen.getByRole("button", { name: "准备因子输入" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "开启因子功能" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "初始化因子仓库" })).not.toBeInTheDocument();
    expect(screen.queryByText("尚无交易日记录")).not.toBeInTheDocument();

    rerender(<FactorPipelinePrepareStep {...props} loading={false} error="状态服务暂不可用" />);
    expect(screen.getByText("输入状态暂时无法确认")).toBeInTheDocument();
    expect(screen.queryByText("需要初始化")).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "重试检查" }));
    expect(props.onRefresh).toHaveBeenCalledOnce();
  });

  it("offers the required setup action only after a missing prerequisite is confirmed", async () => {
    const props = { ...createProps(), featureEnabled: false, warehouseAvailable: false };
    render(<FactorPipelinePrepareStep {...props} />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "开启因子功能" }));
    await user.click(screen.getByRole("button", { name: "初始化因子仓库" }));
    expect(props.onEnable).toHaveBeenCalledOnce();
    expect(props.onInitialize).toHaveBeenCalledOnce();
  });

  it("preserves the input scope and explicit dates when returning to input checks and reopening operations", async () => {
    const user = userEvent.setup();
    render(<FactorPipelinePrepareStep {...createProps()} />);
    await user.click(screen.getByRole("button", { name: "准备因子输入" }));
    await setInputDraft();
    await expectInputDraft();

    await user.click(screen.getByRole("button", { name: "返回输入检查" }));
    expect(screen.queryByPlaceholderText("开始日期")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "准备因子输入" }));

    await expectInputDraft();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
    expect(mockApi.startExternalDataSync).not.toHaveBeenCalled();
  });

  it("preserves only the input form draft when another wizard step unmounts preparation", async () => {
    const user = userEvent.setup();
    const firstStep = render(<FactorPipelinePrepareStep {...createProps()} />);
    await user.click(screen.getByRole("button", { name: "准备因子输入" }));
    await setInputDraft();
    firstStep.unmount();

    render(<FactorPipelinePrepareStep {...createProps()} />);
    expect(screen.getByRole("heading", { name: "检查因子输入" })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("开始日期")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "准备因子输入" }));

    await expectInputDraft();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
    expect(mockApi.startExternalDataSync).not.toHaveBeenCalled();
  });

  it.each([
    ["invalid JSON", "{broken"],
    ["unexpected field types", JSON.stringify({ version: 1, source: ["all"], includeNorthbound: false, syncMode: "backfill", datePreset: "custom", dateRange: ["2026-08-01", "2026-08-13"] })],
    ["an impossible date", JSON.stringify({ version: 1, source: "all", includeNorthbound: false, syncMode: "backfill", datePreset: "custom", dateRange: ["2026-02-31", "2026-08-13"] })],
  ])("uses safe defaults when the input draft contains %s", async (_label, serialized) => {
    sessionStorage.setItem("factor-pipeline:inputs:v1", serialized);
    render(<FactorPipelinePrepareStep {...createProps()} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "准备因子输入" }));

    expect(screen.getByText("当前观察池")).toBeInTheDocument();
    expect(screen.getByText("更新最近数据 · 只补未更新的部分")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("开始日期")).not.toBeInTheDocument();
    expect(mockApi.startExternalDataSync).not.toHaveBeenCalled();
  });

  it("keeps the ordinary data center independent from a wizard input draft", async () => {
    sessionStorage.setItem("factor-pipeline:inputs:v1", JSON.stringify({
      version: 1, source: "all", includeNorthbound: false, syncMode: "backfill",
      datePreset: "custom", dateRange: ["2026-08-01", "2026-08-13"],
    }));
    render(<ExternalDataSync />);

    expect(await screen.findByText("当前观察池")).toBeInTheDocument();
    expect(screen.getByText("更新最近数据 · 只补未更新的部分")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("开始日期")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新评分" })).toBeInTheDocument();
  });

  it("retains an explicit custom preset even when its saved dates match a day preset", async () => {
    sessionStorage.setItem("factor-pipeline:inputs:v1", JSON.stringify({
      version: 1, source: "all", includeNorthbound: false, syncMode: "backfill",
      datePreset: "custom", dateRange: ["2026-08-01", "2026-08-14"],
    }));
    render(<FactorPipelinePrepareStep {...createProps()} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "准备因子输入" }));

    fireEvent.click(screen.getByRole("button", { name: /日期和更新设置/ }));
    expect(screen.getByText("自定义")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("开始日期")).toHaveValue("2026-08-01");
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue("2026-08-14");
  });

  it("applies the live date guard to a restored historical draft", async () => {
    sessionStorage.setItem("factor-pipeline:inputs:v1", JSON.stringify({
      version: 1, source: "all", includeNorthbound: false, syncMode: "backfill",
      datePreset: "custom", dateRange: ["2099-01-01", "2099-01-14"],
    }));
    render(<FactorPipelinePrepareStep {...createProps()} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "准备因子输入" }));

    expect(screen.getByText("结束日期不能晚于今天")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /同步股票估值/ })).toBeDisabled();
    expect(mockApi.startExternalDataSync).not.toHaveBeenCalled();
  });
});
