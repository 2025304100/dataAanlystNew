import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { message } from "antd";

const { mockApi, makeTask, mockOverview } = vi.hoisted(() => {
  const datasets = ["fundamental", "financial", "lhb", "hot_rank", "tail_proxy", "capital_flow", "etf"] as const;
  const makeTask = (dataset: typeof datasets[number], status: "running" | "done" | "failed" = "done") => ({
    id: `task-${dataset}`,
    task_type: `external_sync_${dataset}`,
    status,
    stage: status === "done" ? "done" : status === "failed" ? "interrupted" : "sync",
    percent: status === "done" ? 100 : status === "failed" ? 25 : 25,
    message: status === "done" ? "completed" : status === "failed" ? "Backend restarted before task completed" : "syncing",
    total: 8,
    processed: status === "done" ? 8 : 2,
    ok_count: status === "done" ? 7 : 2,
    failed_count: status === "done" ? 0 : 0,
    current_item: status === "running" ? "600000" : null,
    result: status === "done" ? {
      dataset,
      total: 8,
      success: 7,
      skipped: 1,
      failed: 0,
      records: 7,
      errors: [],
    } : null,
    errors: status === "failed" ? [{ code: "BACKEND_RESTART_INTERRUPTED" }] : [],
    batch_recovery: status === "failed" ? { last_symbol_id: 123, processed: 2, total: 8 } : null,
    created_at: "2026-08-02T08:00:00",
    started_at: "2026-08-02T08:00:01",
    finished_at: status === "running" ? null : "2026-08-02T08:00:05",
    updated_at: "2026-08-02T08:00:05",
  });
  const mockOverview = {
    datasets: datasets.map((dataset, index) => ({
      dataset,
      records: (index + 1) * 10,
      symbols: index + 1,
      latest_date: "2026-08-02",
      last_updated_at: "2026-08-02T08:00:00",
      latest_task: null,
    })),
    total_records: 280,
    covered_symbols: 28,
    available_datasets: 6,
    running_tasks: 0,
    refreshed_at: "2026-08-02T08:00:00",
  };
  return {
    makeTask,
    mockOverview,
    mockApi: {
      getWatchlists: vi.fn(async () => [] as Array<{ id: number; name: string; list_type: string }>),
      scoringCreateTask: vi.fn(async () => ({})),
      scoringGetTask: vi.fn(async () => ({})),
      importTailProxyMinutes: vi.fn(async () => ({})),
      getExternalDataOverview: vi.fn(async () => mockOverview),
      getExternalDataSyncCoverage: vi.fn(async () => ({ datasets: [] as Array<{
        dataset: typeof datasets[number]; earliest_date: string | null; latest_date: string | null;
        first_synced_at: string | null; last_synced_at: string | null; covered_points: number; completed_points?: number;
      }> })),
      scoringGetOverviewAsFactor: vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, latest_trade_date: null, factor_coverage: [] })),
      scoringListTasks: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
      getInboxNotifications: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
      scoringGetOverview: vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, factor_coverage: [] })),
      scoringGetFactorDefinition: vi.fn(async () => ({})),
      scoringGetFactorModelListAsFactor: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
      getEvaluationTaskHeartbeat: vi.fn(async () => ({})),
      getExternalDataCoverage: vi.fn(async () => ({
        generated_at: "2026-08-02T08:00:00",
        datasets: datasets.map((dataset) => ({
          dataset,
          readiness: dataset === "fundamental" ? "blocked" : "available",
          reason: "test",
          fields: dataset === "etf" ? [] : [{
            field: `${dataset}_field`,
            availability: "available",
            evaluation_enabled: true,
            first_date: "2026-07-01",
            latest_date: "2026-08-02",
            nonnull_rows: 8,
            table_rows: 10,
            distinct_symbols: 2,
            distinct_dates: 5,
            continuity_days: 5,
            daily_coverage_p50: 0.8,
            daily_coverage_p90: 0.9,
            latest_daily_coverage: 0.8,
            reason: "test",
          }],
        })),
      })),
      getExternalDataGaps: vi.fn(async (dataset: "fundamental" | "capital_flow") => ({
        dataset,
        start_date: "2026-06-04",
        end_date: "2026-08-02",
        total_missing: 0,
        truncated: false,
        gaps: [] as Array<{ symbol_id: number; symbol: string; trade_date: string }>,
      })),
      repairExternalDataGaps: vi.fn(async () => makeTask("fundamental", "running")),
      getExternalDataSyncCapabilities: vi.fn(async () => ({
        fundamental: { modes: ["incremental", "backfill"], history_limit_days: null, reason: "history" },
        financial: { modes: ["incremental", "backfill"], history_limit_days: null, reason: "history" },
        lhb: { modes: ["incremental", "backfill"], history_limit_days: 31, reason: "history" },
        hot_rank: { modes: ["incremental"], history_limit_days: 0, reason: "snapshot" },
        tail_proxy: { modes: ["incremental"], history_limit_days: 0, reason: "snapshot" },
        capital_flow: { modes: ["incremental", "backfill"], history_limit_days: 100, reason: "history", northbound: { available: true, reason: "available", api_key: "northbound" } },
        etf: { modes: ["incremental"], history_limit_days: 0, reason: "snapshot" },
      })),
      previewExternalDataSyncPlan: vi.fn(async (payload: any) => ({
        dataset: payload.dataset,
        mode: payload.mode,
        sync_policy: payload.sync_policy,
        requested_start_date: "2026-08-02",
        requested_end_date: "2026-08-02",
        requested_span_days: 1,
        provider_history_limit_days: 0,
        provider_reason: "snapshot",
        partition_strategy: "symbol_batches",
        symbol_batch_size: 20,
      })),
      cancelExternalDataSyncTask: vi.fn(async () => makeTask("fundamental")),
      retryExternalDataSyncTask: vi.fn(async () => makeTask("fundamental", "running")),
      startExternalDataSync: vi.fn(async (payload: { dataset: typeof datasets[number] }) => makeTask(payload.dataset)),
      getExternalDataSyncTask: vi.fn(async (_taskId: string) => makeTask("fundamental", "running")),
      getExternalDataSyncTaskPartitions: vi.fn(async () => ({
        task_id: "task-fundamental",
        plan: null,
        partitions: [],
      })),
      createFactorPipelineTask: vi.fn(async () => ({
        id: "factor-pipeline-1", status: "queued", stage: "mirror", percent: 0,
        message: "queued", task_type: "factor_pipeline", total: 0, processed: 0,
        ok_count: 0, failed_count: 0, current_item: null, result: null, errors: [],
        created_at: null, started_at: null, finished_at: null, updated_at: null,
      })),
      getFactorPipelineTask: vi.fn(async () => ({
        id: "factor-pipeline-1", status: "done", stage: "done", percent: 100,
        message: "completed", task_type: "factor_pipeline", total: 0, processed: 0,
        ok_count: 0, failed_count: 0, current_item: null, result: null, errors: [],
        created_at: null, started_at: null, finished_at: null, updated_at: null,
      })),
    },
  };
});

vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    `${key}${Object.keys(params).length ? `:${JSON.stringify(params)}` : ""}`,
  DOT: " | ",
}));

vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: {
      ...actual.message,
      success: vi.fn(),
      error: vi.fn(),
      warning: vi.fn(),
      info: vi.fn(),
    },
  };
});

vi.mock("../../api/client", () => ({ api: mockApi }));

import ExternalDataSync, { formatDate } from "../ExternalDataSync";
import { AppContext } from "../../context/AppContext";

function renderWithWatchlist() {
  return render(<AppContext.Provider value={{ activeWatchlistId: 22, portfolioId: 44, workbench: { watchlists: [{ id: 11, name: "core", list_type: "watch" }, { id: 22, name: "my-list", list_type: "watch" }] } } as any}><ExternalDataSync /></AppContext.Provider>);
}

async function openUpdateSettings() {
  const toggle = screen.getByRole("button", { name: /日期和更新设置/ });
  if (toggle.getAttribute("aria-expanded") === "false") fireEvent.click(toggle);
}

async function chooseSelect(index: number, text: string) {
  if (index > 0) await openUpdateSettings();
  fireEvent.mouseDown(document.querySelectorAll(".ant-select-selector")[index]);
  const option = await waitFor(() => {
    const found = Array.from(document.querySelectorAll(".ant-select-item-option"))
      .find((item) => item.textContent?.includes(text));
    expect(found).toBeDefined();
    return found!;
  });
  fireEvent.click(option);
}

async function chooseDatePreset(label: string) {
  const select = screen.getByRole("combobox", { name: "日期跨度预设" });
  const user = userEvent.setup();
  await user.click(select);
  // rc-select virtualizes its list: keyboard navigation scrolls the later presets into view.
  for (let step = 0; step <= 14; step += 1) {
    const option = Array.from(document.querySelectorAll(".ant-select-item-option"))
      .find((item) => item.textContent === label);
    if (option) { fireEvent.click(option); return; }
    const activeOption = document.getElementById(select.getAttribute("aria-activedescendant") ?? "");
    if (activeOption?.getAttribute("aria-label") === label) {
      fireEvent.keyDown(select, { key: "Enter", code: "Enter", keyCode: 13, which: 13 });
      return;
    }
    fireEvent.keyDown(select, { key: "ArrowDown", code: "ArrowDown", keyCode: 40, which: 40 });
  }
  throw new Error(`Date preset is not available to click or keyboard navigation: ${label}`);
}

async function chooseSyncPolicy(label: string) {
  await openUpdateSettings();
  const select = screen.getByRole("combobox", { name: "更新方式" });
  fireEvent.mouseDown(select.closest(".ant-select")!.querySelector(".ant-select-selector")!);
  const option = await waitFor(() => {
    const found = Array.from(document.querySelectorAll(".ant-select-item-option"))
      .find((item) => item.textContent === label);
    expect(found).toBeDefined();
    return found!;
  });
  fireEvent.click(option);
}

async function enterHistoricalRange(start: string, end: string) {
  const user = userEvent.setup();
  await user.clear(screen.getByPlaceholderText("开始日期"));
  await user.type(screen.getByPlaceholderText("开始日期"), start);
  await user.keyboard("{Enter}");
  await user.clear(screen.getByPlaceholderText("结束日期"));
  await user.type(screen.getByPlaceholderText("结束日期"), end);
  await user.keyboard("{Enter}");
}

describe("ExternalDataSync dashboard", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  beforeEach(() => {
    mockApi.getWatchlists.mockReset();
    mockApi.getWatchlists.mockResolvedValue([]);
    vi.mocked(message.error).mockClear();
    vi.mocked(message.success).mockClear();
    vi.mocked(message.warning).mockClear();
    vi.mocked(message.info).mockClear();
    mockApi.getExternalDataOverview.mockReset();
    mockApi.getExternalDataOverview.mockResolvedValue(mockOverview);
    mockApi.getExternalDataSyncCoverage.mockReset();
    mockApi.getExternalDataSyncCoverage.mockResolvedValue({ datasets: [] });
    mockApi.getExternalDataSyncCapabilities.mockReset();
    mockApi.getExternalDataSyncCapabilities.mockResolvedValue({
      fundamental: { modes: ["incremental", "backfill"], history_limit_days: null, reason: "history" },
      financial: { modes: ["incremental", "backfill"], history_limit_days: null, reason: "history" },
      lhb: { modes: ["incremental", "backfill"], history_limit_days: 31, reason: "history" },
      hot_rank: { modes: ["incremental"], history_limit_days: 0, reason: "snapshot" },
      tail_proxy: { modes: ["incremental"], history_limit_days: 0, reason: "snapshot" },
      capital_flow: { modes: ["incremental", "backfill"], history_limit_days: 100, reason: "history", northbound: { available: true, reason: "available", api_key: "northbound" } },
      etf: { modes: ["incremental"], history_limit_days: 0, reason: "snapshot" },
    });
    mockApi.getExternalDataCoverage.mockReset();
    mockApi.getExternalDataCoverage.mockResolvedValue({
      generated_at: "2026-08-02T08:00:00",
      datasets: (["fundamental", "financial", "lhb", "hot_rank", "tail_proxy", "capital_flow", "etf"] as const).map((dataset) => ({
        dataset,
        readiness: dataset === "fundamental" ? "blocked" : "available",
        reason: "test",
        fields: dataset === "etf" ? [] : [{
          field: `${dataset}_field`,
          availability: "available",
          evaluation_enabled: true,
          first_date: "2026-07-01",
          latest_date: "2026-08-02",
          nonnull_rows: 8,
          table_rows: 10,
          distinct_symbols: 2,
          distinct_dates: 5,
          continuity_days: 5,
          daily_coverage_p50: 0.8,
          daily_coverage_p90: 0.9,
          latest_daily_coverage: 0.8,
          reason: "test",
        }],
      })),
    });
    mockApi.getExternalDataGaps.mockReset();
    mockApi.getExternalDataGaps.mockImplementation(async (dataset: "fundamental" | "capital_flow") => ({
      dataset,
      start_date: "2026-06-04",
      end_date: "2026-08-02",
      total_missing: 0,
      truncated: false,
      gaps: [],
    }));
    mockApi.previewExternalDataSyncPlan.mockReset();
    mockApi.previewExternalDataSyncPlan.mockImplementation(async (payload: any) => ({
      dataset: payload.dataset,
      mode: payload.mode,
      sync_policy: payload.sync_policy,
      requested_start_date: "2026-08-02",
      requested_end_date: "2026-08-02",
      requested_span_days: 1,
      provider_history_limit_days: 0,
      provider_reason: "snapshot",
      partition_strategy: "symbol_batches",
      symbol_batch_size: 20,
    }));
    mockApi.startExternalDataSync.mockReset();
    mockApi.startExternalDataSync.mockImplementation(async (payload: any) => makeTask(payload.dataset));
    mockApi.getExternalDataSyncTask.mockReset();
    mockApi.getExternalDataSyncTask.mockResolvedValue(makeTask("fundamental", "running"));
    mockApi.getExternalDataSyncTaskPartitions.mockReset();
    mockApi.getExternalDataSyncTaskPartitions.mockResolvedValue({
      task_id: "task-fundamental",
      plan: null,
      partitions: [],
    });
    mockApi.repairExternalDataGaps.mockReset();
    mockApi.repairExternalDataGaps.mockResolvedValue(makeTask("fundamental", "running"));
    mockApi.cancelExternalDataSyncTask.mockReset();
    mockApi.cancelExternalDataSyncTask.mockResolvedValue(makeTask("fundamental"));
    mockApi.retryExternalDataSyncTask.mockReset();
    mockApi.retryExternalDataSyncTask.mockResolvedValue(makeTask("fundamental", "running"));
    mockApi.scoringCreateTask.mockClear();
    mockApi.scoringCreateTask.mockResolvedValue({ id: "score-audit", status: "done", stage: "done", percent: 100 });
    mockApi.scoringGetTask.mockReset();
    mockApi.scoringGetTask.mockResolvedValue({ id: "score-audit", status: "done", stage: "done", percent: 100 });
    mockApi.getExternalDataGaps.mockReset();
    mockApi.getExternalDataGaps.mockImplementation(async (dataset: any) => ({
      dataset, start_date: "2026-07-01", end_date: "2026-08-02", total_missing: 0,
      truncated: false, gaps: [],
    }));
    mockApi.importTailProxyMinutes.mockReset();
    mockApi.importTailProxyMinutes.mockResolvedValue({ written_sessions: 1, rejected_sessions: 0 });
    mockApi.getFactorPipelineTask.mockClear();
  });

  it("interprets naive audit timestamps as UTC while preserving explicit zones and calendar dates", () => {
    const locale = vi.spyOn(Date.prototype, "toLocaleString").mockImplementation(function (this: Date) {
      return this.toISOString();
    });
    try {
      expect(formatDate("2026-08-02T08:00:01", true)).toBe("2026-08-02T08:00:01.000Z");
      expect(formatDate("2026-08-02T08:00:01.123456", true)).toBe("2026-08-02T08:00:01.123Z");
      expect(formatDate("2026-08-02T08:00:01Z", true)).toBe("2026-08-02T08:00:01.000Z");
      expect(formatDate("2026-08-02T08:00:01+08:00", true)).toBe("2026-08-02T00:00:01.000Z");
      expect(formatDate("2026-08-02T08:00:01-04:00", true)).toBe("2026-08-02T12:00:01.000Z");
      expect(formatDate("2026-08-02")).toBe(new Date("2026-08-02").toLocaleDateString());
    } finally {
      locale.mockRestore();
    }
  });

  it("renders the real inventory dashboard for six supported datasets", async () => {
    render(<ExternalDataSync />);
    await waitFor(() => expect(mockApi.getExternalDataOverview).toHaveBeenCalled());

    expect(screen.getByText("280")).toBeInTheDocument();
    expect(screen.getByText("28")).toBeInTheDocument();
    expect(screen.getByText("6/6")).toBeInTheDocument();
    expect(screen.getAllByText("extSyncFundamental").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncFinancial").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncLhb").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncHotRank").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("extSyncTailProxy")).not.toBeInTheDocument();
    expect(screen.getAllByText("extSyncCapitalFlow").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncEtf").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByRole("button", { name: "检查缺失数据" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "extFullValuationHistory" })).not.toBeInTheDocument();
    await openUpdateSettings();
    expect(screen.getAllByRole("button", { name: "检查缺失数据" })).toHaveLength(3);
    const coverageHeading = screen.getAllByText("本地数据完整情况")[0];
    await userEvent.setup().hover(coverageHeading);
    expect(await screen.findByText(/A 股日覆盖率和连续天数按交易日计算，剔除周末及交易所节假日休市/)).toBeInTheDocument();
  });

  it("keeps input synchronization usable when the wizard hides scoring operations", async () => {
    const user = userEvent.setup();
    render(<ExternalDataSync showScoringRefresh={false} />);

    const syncInput = await screen.findByRole("button", { name: /extSyncFundamental/ });
    expect(screen.queryByRole("button", { name: "刷新评分" })).not.toBeInTheDocument();
    expect(screen.queryByText(/当前评分仅支持 A 股股票/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "extCoverageActionModel" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "extCoverageActionApi" })).toBeInTheDocument();
    await user.click(syncInput);

    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({ dataset: "fundamental" })));
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  });

  it("renders coverage readiness as a user-facing label instead of an internal enum", async () => {
    render(<ExternalDataSync />);

    expect((await screen.findAllByText("extReadinessAvailable")).length).toBeGreaterThan(0);
    expect(screen.getByText("extReadinessBlocked")).toBeInTheDocument();
  });

  it("blocks sync actions when the dashboard inventory cannot be loaded", async () => {
    mockApi.getExternalDataOverview.mockRejectedValue(new Error("overview unavailable"));
    render(<ExternalDataSync />);

    expect(await screen.findByText("extOverviewUnavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /extSyncFundamental/ })).toBeDisabled();
  });

  it("offers cursor recovery for an interrupted historical task after the page is reopened", async () => {
    mockApi.getExternalDataOverview.mockResolvedValue({
      ...mockOverview,
      datasets: mockOverview.datasets.map((dataset) =>
        dataset.dataset === "fundamental" ? { ...dataset, latest_task: makeTask("fundamental", "failed") } : dataset,
      ),
    } as any);
    render(<ExternalDataSync />);

    const resume = await screen.findByRole("button", { name: "extResumeFromCursor" });
    await userEvent.setup().click(resume);

    await waitFor(() => expect(mockApi.retryExternalDataSyncTask).toHaveBeenCalledWith("task-fundamental"));
  });

  it("starts valuation as an observable task with the selected scope", async () => {
    render(<ExternalDataSync />);
    const button = await screen.findByRole("button", { name: /extSyncFundamental/ });
    fireEvent.click(button);
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({
      dataset: "fundamental",
      source: "watchlist",
      include_northbound: true,
      mode: "incremental",
      lookback_days: 1,
      limit: 20,
    })));
  });

  it("passes the changed source to financial-report synchronization", async () => {
    const user = userEvent.setup();
    render(<ExternalDataSync />);
    const selector = document.querySelector(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(selector);
    await waitFor(() => expect(document.querySelectorAll(".ant-select-item-option").length).toBeGreaterThan(0));
    const option = Array.from(document.querySelectorAll(".ant-select-item-option"))
      .find((item) => item.textContent?.includes("extSourcePositions"));
    expect(option).toBeDefined();
    fireEvent.click(option!);

    await user.click(screen.getByRole("button", { name: /extSyncFinancial/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({
      dataset: "financial",
      source: "positions",
    })));
  });

  it("passes the northbound toggle to capital-flow synchronization", async () => {
    const user = userEvent.setup();
    render(<ExternalDataSync />);
    await openUpdateSettings();
    const checkbox = screen.getByRole("checkbox", { name: "extIncludeNorthbound" });
    await user.click(checkbox);
    await user.click(screen.getByRole("button", { name: /extSyncCapitalFlow/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({
      dataset: "capital_flow",
      include_northbound: false,
    })));
  });

  it("shows real task percent, counts and current symbol while running", async () => {
    mockApi.startExternalDataSync.mockResolvedValue(makeTask("fundamental", "running"));
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: /extSyncFundamental/ }));

    await waitFor(() => {
      const bars = screen.getAllByRole("progressbar");
      expect(bars.some((bar) => bar.getAttribute("aria-valuenow") === "25")).toBe(true);
    });
    expect(screen.getByText("2/8")).toBeInTheDocument();
    expect(screen.getByText(/600000/)).toBeInTheDocument();
    expect(mockApi.getExternalDataSyncTaskPartitions).not.toHaveBeenCalled();
  });

  it("shows the closed market skip reason as successful completion", async () => {
    const task = makeTask("fundamental", "done");
    const reason = "2026-10-17 休市，无需更新";
    mockApi.startExternalDataSync.mockResolvedValue({
      ...task, message: reason, total: 5555, processed: 5555, ok_count: 0, failed_count: 0,
      result: { dataset: "fundamental", total: 5555, success: 0, skipped: 5555,
        failed: 0, records: 0, errors: [], skip_reason: "market_closed" },
    } as any);
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: /extSyncFundamental/ }));
    await waitFor(() => expect(message.success).toHaveBeenCalledWith(reason));
    expect(mockApi.getExternalDataSyncTaskPartitions).not.toHaveBeenCalled();
    expect(mockApi.getExternalDataOverview).toHaveBeenCalledTimes(1);
    expect(mockApi.getExternalDataCoverage).toHaveBeenCalledTimes(1);
    expect(message.error).not.toHaveBeenCalled();
  });

  it.each([
    ["financial", "no_new_data", "所选公告日期没有新增或变更财报", /extSyncFinancial/],
    ["lhb", "no_events", "所选区间没有龙虎榜机构事件，无新增数据", /extSyncLhb/],
  ])("shows %s no-data completion without reloading coverage", async (dataset, skip_reason, reason, button) => {
    mockApi.startExternalDataSync.mockResolvedValue({
      ...makeTask(dataset as any, "done"), message: reason,
      result: { dataset, total: 2, success: 0, skipped: 2, failed: 0, records: 0, errors: [], skip_reason },
    } as any);
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: button }));
    await waitFor(() => expect(message.success).toHaveBeenCalledWith(reason));
    expect(mockApi.getExternalDataOverview).toHaveBeenCalledTimes(1);
    expect(mockApi.getExternalDataCoverage).toHaveBeenCalledTimes(1);
  });

  it("cancels the active external sync from the progress card", async () => {
    mockApi.startExternalDataSync.mockResolvedValue(makeTask("fundamental", "running"));
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: /extSyncFundamental/ }));
    const cancelButton = await screen.findByRole("button", { name: "停止更新" });
    fireEvent.click(cancelButton);
    await waitFor(() => expect(mockApi.cancelExternalDataSyncTask).toHaveBeenCalledWith("task-fundamental"));
  });

  it("shows completion feedback and the structured result", async () => {
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: /extSyncFundamental/ }));

    await waitFor(() => expect(message.success).toHaveBeenCalledWith("extTaskCompleted"));
    expect(screen.getByText(/extSyncFundamental: extSyncResult/)).toBeInTheDocument();
  });
  it("submits a score-only factor pipeline after input synchronization", async () => {
    const user = userEvent.setup();
    renderWithWatchlist();
    await user.click(await screen.findByRole("button", { name: "刷新评分" }));

    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(expect.objectContaining({
      train_model: false,
      materialize_scores: true,
      full_refresh: false,
    })));
  });

  it.each([
    ["watchlist", null, { universe_source: "watchlist", watchlist_id: 22 }],
    ["positions", "extSourcePositions", { universe_source: "positions", portfolio_id: 44 }],
    ["all", "extSourceAllMarket", { universe_source: "all" }],
  ] as const)("submits the selected %s score universe without unrelated identifiers", async (_, option, scope) => {
    renderWithWatchlist();
    await screen.findByText("280");
    if (option) await chooseSelect(0, option);
    expect(screen.getByText("当前评分仅支持 A 股股票。为保持评分可比性，仍使用全市场数据作为计算参照，仅为所选范围生成评分。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新评分" }));
    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith({
      start_date: undefined, end_date: undefined, full_refresh: false,
      train_model: false, materialize_scores: true, ...scope,
    }));
  });

  it.each([
    ["watchlist", null, "当前观察池尚未加载或不存在，无法刷新评分；请先选择有效观察池"],
    ["positions", "extSourcePositions", "当前组合尚未加载或未选择，无法刷新持仓评分；请先选择组合"],
  ] as const)("blocks %s scoring without its identity instead of falling back to all symbols", async (_, option, reason) => {
    render(<ExternalDataSync />);
    await screen.findByText("280");
    if (option) await chooseSelect(0, option);
    expect(screen.getByRole("button", { name: "刷新评分" })).toBeDisabled();
    expect(screen.getByText(reason)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新评分" }));
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
    await chooseSelect(0, "extSourceAllMarket");
    expect(screen.getByRole("button", { name: "刷新评分" })).toBeEnabled();
  });

  it("keeps a completed scoring task's frozen universe when the selector changes", async () => {
    mockApi.scoringCreateTask.mockResolvedValue({ id: "score-scoped", status: "done", stage: "done", percent: 100, result: { score_universe: { source: "watchlist", watchlist_id: 22, portfolio_id: null, symbol_count: 3, normalization: "full_market" } } } as any);
    renderWithWatchlist();
    fireEvent.click(await screen.findByRole("button", { name: "刷新评分" }));
    expect(await screen.findByText("本次评分范围：观察池 #22 · 3 只股票；使用全市场数据作为计算参照")).toBeInTheDocument();
    await chooseSelect(0, "extSourceAllMarket");
    expect(screen.getByText("本次评分范围：观察池 #22 · 3 只股票；使用全市场数据作为计算参照")).toBeInTheDocument();
    expect(screen.queryByText(/本次评分范围：全量已同步标的/)).not.toBeInTheDocument();
  });

  it("shows the queued task's frozen scope from its payload before results exist", async () => {
    const task = { id: "score-queued", status: "queued", stage: "queued", percent: 0, result: null, payload_json: JSON.stringify({ universe_source: "positions", portfolio_id: 44, score_symbols: ["600000", "000001"] }) };
    mockApi.scoringCreateTask.mockResolvedValue(task as any);
    mockApi.scoringGetTask.mockResolvedValue(task as any);
    renderWithWatchlist();
    await screen.findByText("280");
    await chooseSelect(0, "extSourcePositions");
    fireEvent.click(screen.getByRole("button", { name: "刷新评分" }));
    expect(await screen.findByText("本次评分范围：组合 #44 持仓 · 2 只股票；使用全市场数据作为计算参照")).toBeInTheDocument();
    await chooseSelect(0, "extSourceAllMarket");
    expect(screen.getByText("本次评分范围：组合 #44 持仓 · 2 只股票；使用全市场数据作为计算参照")).toBeInTheDocument();
  });

  it("loads the actual core watchlist independently when the workbench is unavailable", async () => {
    mockApi.getWatchlists.mockResolvedValue([{ id: 1, name: "core", list_type: "watch" }]);
    render(<AppContext.Provider value={{ activeWatchlistId: 1, portfolioId: 1, workbench: null } as any}><ExternalDataSync /></AppContext.Provider>);
    const button = await screen.findByRole("button", { name: "刷新评分" });
    await waitFor(() => expect(button).toBeEnabled());
    expect(mockApi.getWatchlists).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/core中的有效观察项/)).toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(expect.objectContaining({ universe_source: "watchlist", watchlist_id: 1 })));
  });

  it("honors the active watchlist in an independently loaded list", async () => {
    mockApi.getWatchlists.mockResolvedValue([{ id: 1, name: "core", list_type: "watch" }, { id: 22, name: "selected-list", list_type: "watch" }]);
    render(<AppContext.Provider value={{ activeWatchlistId: 22, workbench: null } as any}><ExternalDataSync /></AppContext.Provider>);
    const button = await screen.findByRole("button", { name: "刷新评分" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(expect.objectContaining({ universe_source: "watchlist", watchlist_id: 22 })));
  });

  it("uses available context watchlists without a duplicate list request", async () => {
    renderWithWatchlist();
    await screen.findByText("280");
    expect(mockApi.getWatchlists).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "刷新评分" }));
    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(expect.objectContaining({ watchlist_id: 22 })));
  });

  it("blocks a failed standalone watchlist read and recovers through its retry", async () => {
    mockApi.getWatchlists.mockRejectedValueOnce(new Error("watchlists offline")).mockResolvedValue([{ id: 1, name: "core", list_type: "watch" }]);
    render(<ExternalDataSync />);
    expect(await screen.findByText("观察池列表读取失败，无法刷新评分：watchlists offline；请重试读取")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新评分" })).toBeDisabled();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "重试读取观察池" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "刷新评分" })).toBeEnabled());
    expect(mockApi.getWatchlists).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/观察池列表读取失败/)).not.toBeInTheDocument();
  });

  it("keeps a genuinely empty standalone watchlist response blocked", async () => {
    render(<ExternalDataSync />);
    expect(await screen.findByText("当前观察池尚未加载或不存在，无法刷新评分；请先选择有效观察池")).toBeInTheDocument();
    expect(mockApi.getWatchlists).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "刷新评分" })).toBeDisabled();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  });

  it.each(["fundamental", "financial", "lhb", "hot_rank", "capital_flow", "etf"] as const)(
    "submits the %s dataset through preview and the real external task API",
    async (dataset) => {
      const names = { fundamental: "extSyncFundamental", financial: "extSyncFinancial", lhb: "extSyncLhb", hot_rank: "extSyncHotRank", tail_proxy: "extSyncTailProxy", capital_flow: "extSyncCapitalFlow", etf: "extSyncEtf" };
      render(<ExternalDataSync />);
      await waitFor(() => expect(screen.getByText("280")).toBeInTheDocument());
      fireEvent.click(screen.getByRole("button", { name: new RegExp(names[dataset]) }));
      await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalled());
      expect(mockApi.previewExternalDataSyncPlan).toHaveBeenCalledWith(expect.objectContaining({ dataset, mode: "incremental", lookback_days: 1 }));
      expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(mockApi.previewExternalDataSyncPlan.mock.calls[0][0]);
    },
  );

  it("preserves the actual selected watchlist identity in task payloads", async () => {
    render(<AppContext.Provider value={{ activeWatchlistId: 22, workbench: { watchlists: [{ id: 11, name: "core", list_type: "watch" }, { id: 22, name: "my-list", list_type: "watch" }] } } as any}><ExternalDataSync /></AppContext.Provider>);
    await waitFor(() => expect(screen.getByText("280")).toBeInTheDocument());
    expect(screen.getByText(/my-list中的有效观察项/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /extSyncFundamental/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({ source: "watchlist", watchlist_id: 22 })));
  });

  it("uses all-market 1990-to-today history for the dedicated valuation history action", async () => {
    render(<ExternalDataSync />);
    await openUpdateSettings();
    const button = await screen.findByRole("button", { name: "extFullValuationHistory" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({ dataset: "fundamental", source: "all", mode: "backfill", sync_policy: "force", start_date: "1990-01-01", end_date: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/), max_workers: 4 })));
  });

  it("defaults ordinary sync to normal policy without changing the snapshot date mode", async () => {
    renderWithWatchlist();
    await screen.findByText("280");
    fireEvent.click(screen.getByRole("button", { name: /extSyncCapitalFlow/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({
      dataset: "capital_flow", mode: "incremental", sync_policy: "normal", lookback_days: 1,
    })));
    expect(mockApi.previewExternalDataSyncPlan.mock.calls[0][0]).toEqual(mockApi.startExternalDataSync.mock.calls[0][0]);
  });

  it("lets the user update current data without first choosing dates or an update policy", async () => {
    renderWithWatchlist();
    await screen.findByText("280");
    expect(screen.queryByRole("combobox", { name: "更新方式" })).not.toBeInTheDocument();
    expect(screen.getByText("更新最近数据 · 只补未更新的部分")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /日期和更新设置/ })).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(screen.getByRole("button", { name: /extSyncFinancial/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({
      dataset: "financial", source: "watchlist", watchlist_id: 22, mode: "incremental", sync_policy: "normal", lookback_days: 1,
    })));
    expect(mockApi.previewExternalDataSyncPlan.mock.calls[0][0]).toEqual(mockApi.startExternalDataSync.mock.calls[0][0]);
  });

  it("keeps calculation checks out of the daily update path while preserving their original entry points", async () => {
    renderWithWatchlist();
    await screen.findByText("280");
    expect(screen.getByRole("button", { name: /extSyncFinancial/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: "inputImport.open" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "inputReplay.open" })).not.toBeInTheDocument();
    const checks = screen.getByRole("button", { name: /检查数据计算过程/ });
    expect(checks).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(checks);
    expect(screen.getByText("inputReplay.title")).toBeVisible();
    expect(screen.getByRole("button", { name: "inputImport.open" })).toBeVisible();
    expect(screen.getByRole("button", { name: "inputReplay.open" })).toBeVisible();
    expect(mockApi.startExternalDataSync).not.toHaveBeenCalled();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  });

  it.each([["只补缺失数据", "missing"], ["重新获取并更新已有数据", "force"]] as const)("submits %s with the chosen historical range and keeps snapshots disabled", async (label, policy) => {
    renderWithWatchlist();
    await screen.findByText("280");
    await chooseSelect(1, "指定历史日期");
    await chooseDatePreset("7 日");
    await chooseSyncPolicy(label);
    const start = (screen.getByPlaceholderText("开始日期") as HTMLInputElement).value;
    const end = (screen.getByPlaceholderText("结束日期") as HTMLInputElement).value;
    expect(screen.getByRole("button", { name: /extSyncEtf/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /extSyncHotRank/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /extSyncFinancial/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({
      dataset: "financial", mode: "backfill", sync_policy: policy, start_date: start, end_date: end, lookback_days: 7,
    })));
    expect(mockApi.previewExternalDataSyncPlan.mock.calls[0][0]).toEqual(mockApi.startExternalDataSync.mock.calls[0][0]);
  });

  it("shows confirmed sync coverage independently from warehouse coverage", async () => {
    mockApi.getExternalDataSyncCoverage.mockResolvedValue({ datasets: [{
      dataset: "capital_flow", earliest_date: "2026-07-03", latest_date: "2026-08-01",
      first_synced_at: "2026-07-03T18:00:00", last_synced_at: "2026-08-02T09:00:00", covered_points: 123, completed_points: 100,
    }] });
    render(<ExternalDataSync />);
    const row = (await screen.findByRole("button", { name: /extSyncCapitalFlow/ })).closest("tr")!;
    await waitFor(() => expect(within(row).getByText("观测范围：2026-07-03 ~ 2026-08-01")).toBeInTheDocument());
    expect(within(row).getByText("已记录 123 个标的日期 · 完整 100 个")).toBeInTheDocument();
    expect(within(row).getByText(/首次同步：/)).toHaveTextContent(new Date("2026-07-03T18:00:00Z").toLocaleString());
    expect(within(row).getByText(/最近同步：/)).toHaveTextContent(new Date("2026-08-02T09:00:00Z").toLocaleString());
  });

  it("keeps legal sync available when the sync ledger cannot be read", async () => {
    mockApi.getExternalDataSyncCoverage.mockRejectedValue(new Error("ledger offline"));
    render(<ExternalDataSync />);
    await screen.findByText("更新记录暂时读不到，仍可更新数据，开始前会检查所选范围");
    const button = screen.getByRole("button", { name: /extSyncCapitalFlow/ });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({ sync_policy: "normal" })));
  });

  it("displays a task's frozen policy, data range and actual execution times after form changes", async () => {
    const task = { ...makeTask("financial"), batch_recovery: { plan: {
      dataset: "financial", mode: "backfill", sync_policy: "missing", requested_start_date: "2026-07-01", requested_end_date: "2026-08-02",
    } } };
    mockApi.getExternalDataOverview.mockResolvedValue({ ...mockOverview, datasets: mockOverview.datasets.map((item) => item.dataset === "financial" ? { ...item, latest_task: task } : item) } as any);
    render(<ExternalDataSync />);
    await screen.findByText("280");
    await chooseSyncPolicy("重新获取并更新已有数据");
    const row = screen.getByRole("button", { name: /extSyncFinancial/ }).closest("tr")!;
    expect(within(row).getByText("只补缺失数据 · 2026-07-01 ~ 2026-08-02")).toBeInTheDocument();
    const times = within(row).getByText(/执行开始：/);
    expect(times).toHaveTextContent(new Date(`${task.started_at!}Z`).toLocaleString());
    expect(times).toHaveTextContent(new Date(`${task.finished_at!}Z`).toLocaleString());
  });

  it("disables unsupported snapshot-only datasets in historical mode", async () => {
    render(<ExternalDataSync />);
    await waitFor(() => expect(screen.getByText("280")).toBeInTheDocument());
    await chooseSelect(1, "指定历史日期");
    expect(screen.getByRole("button", { name: /extSyncHotRank/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /extSyncTailProxy/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /extSyncEtf/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /extSyncFinancial/ })).toBeEnabled();
  });

  it("fills the inclusive default 60-day range when historical mode is selected", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-14T12:00:00+08:00"));
    render(<ExternalDataSync />);
    await screen.findByText("280");

    await chooseSelect(1, "指定历史日期");

    expect(screen.getByPlaceholderText("开始日期")).toHaveValue("2026-08-16");
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue("2026-10-14");
    fireEvent.click(screen.getByRole("button", { name: /extSyncFinancial/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({
      dataset: "financial", mode: "backfill", start_date: "2026-08-16", end_date: "2026-10-14", lookback_days: 60,
    })));
  });

  it("links a short day preset to both date inputs and the historical scoring request", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-14T12:00:00+08:00"));
    renderWithWatchlist();
    await screen.findByText("280");
    await chooseSelect(1, "指定历史日期");

    await chooseDatePreset("7 日");

    expect(screen.getByPlaceholderText("开始日期")).toHaveValue("2026-10-08");
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue("2026-10-14");
    fireEvent.click(screen.getByRole("button", { name: "刷新评分" }));
    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith({
      start_date: "2026-10-08", end_date: "2026-10-14", full_refresh: true,
      train_model: false, materialize_scores: true,
      universe_source: "watchlist", watchlist_id: 22,
    }));
  });

  it.each([
    ["14 日", "2026-10-01"],
    ["30 日", "2026-09-15"],
    ["90 日", "2026-07-17"],
    ["100 日", "2026-07-07"],
    ["180 日", "2026-04-18"],
    ["1 年", "2025-10-15"],
    ["2 年", "2024-10-15"],
    ["3 年", "2023-10-15"],
    ["5 年", "2021-10-15"],
  ])("makes the %s preset visible in the linked date inputs", async (label, expectedStart) => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-14T12:00:00+08:00"));
    render(<ExternalDataSync />);
    await screen.findByText("280");
    await chooseSelect(1, "指定历史日期");

    await chooseDatePreset(label);

    expect(screen.getByPlaceholderText("开始日期")).toHaveValue(expectedStart);
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue("2026-10-14");
  });

  it("keeps the selected end date when changing from a custom range to a day preset", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-14T12:00:00+08:00"));
    render(<ExternalDataSync />);
    await screen.findByText("280");
    await chooseSelect(1, "指定历史日期");
    await enterHistoricalRange("2026-07-01", "2026-08-02");
    expect(screen.getByText("自定义", { selector: ".ant-select-selection-item" })).toBeInTheDocument();

    await chooseDatePreset("14 日");

    expect(screen.getByPlaceholderText("开始日期")).toHaveValue("2026-07-20");
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue("2026-08-02");
    fireEvent.click(screen.getByRole("button", { name: /extSyncFinancial/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({
      dataset: "financial", start_date: "2026-07-20", end_date: "2026-08-02", lookback_days: 14,
    })));
  });

  it.each([
    ["2026-10-08", "2026-10-14", "7 日"],
    ["2023-03-01", "2024-02-29", "1 年"],
  ])("matches a manually entered %s to %s range back to the %s preset", async (start, end, label) => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-14T12:00:00+08:00"));
    render(<ExternalDataSync />);
    await screen.findByText("280");
    await chooseSelect(1, "指定历史日期");

    await enterHistoricalRange(start, end);

    expect(screen.getByText(label, { selector: ".ant-select-selection-item" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("开始日期")).toHaveValue(start);
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue(end);
  });

  it("uses a calendar year anchored to a leap-day end date instead of subtracting 365 days", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-14T12:00:00+08:00"));
    render(<ExternalDataSync />);
    await screen.findByText("280");
    await chooseSelect(1, "指定历史日期");
    await enterHistoricalRange("2024-02-01", "2024-02-29");

    await chooseDatePreset("1 年");

    expect(screen.getByPlaceholderText("开始日期")).toHaveValue("2023-03-01");
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue("2024-02-29");
    fireEvent.click(screen.getByRole("button", { name: /extSyncFinancial/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({
      start_date: "2023-03-01", end_date: "2024-02-29", lookback_days: 366,
    })));
  });

  it.each([
    ["本月至今", "2026-10-01"],
    ["今年至今", "2026-01-01"],
  ])("anchors %s to today even after a past end date was entered", async (label, expectedStart) => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-14T12:00:00+08:00"));
    render(<ExternalDataSync />);
    await screen.findByText("280");
    await chooseSelect(1, "指定历史日期");
    await enterHistoricalRange("2026-07-01", "2026-08-02");

    await chooseDatePreset(label);

    expect(screen.getByPlaceholderText("开始日期")).toHaveValue(expectedStart);
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue("2026-10-14");
  });

  it("keeps explicit dates when custom is selected and blocks cleared historical actions until a preset restores them", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-14T12:00:00+08:00"));
    renderWithWatchlist();
    await screen.findByText("280");
    await chooseSelect(1, "指定历史日期");
    await chooseDatePreset("自定义");
    expect(screen.getByPlaceholderText("开始日期")).toHaveValue("2026-08-16");
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue("2026-10-14");

    fireEvent.click(document.querySelector(".ant-picker-clear")!);

    expect(screen.getByText("自定义", { selector: ".ant-select-selection-item" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("开始日期")).toHaveValue("");
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue("");
    expect(screen.getByRole("button", { name: /extSyncFinancial/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "刷新评分" })).toBeDisabled();
    for (const button of screen.getAllByRole("button", { name: "检查缺失数据" })) {
      expect(button).toBeDisabled();
    }
    expect(mockApi.startExternalDataSync).not.toHaveBeenCalled();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
    expect(mockApi.getExternalDataGaps).not.toHaveBeenCalled();

    await chooseDatePreset("7 日");

    expect(screen.getByPlaceholderText("开始日期")).toHaveValue("2026-10-08");
    expect(screen.getByPlaceholderText("结束日期")).toHaveValue("2026-10-14");
    expect(screen.getByRole("button", { name: /extSyncFinancial/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: "刷新评分" })).toBeEnabled();
  });

  it("applies each provider's history limit without limiting valuation and financial-report presets", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-14T12:00:00+08:00"));
    render(<ExternalDataSync />);
    await screen.findByText("280");
    await chooseSelect(1, "指定历史日期");

    expect(screen.getByRole("button", { name: /extSyncLhb/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /extSyncCapitalFlow/ })).toBeEnabled();
    await chooseDatePreset("30 日");
    expect(screen.getByRole("button", { name: /extSyncLhb/ })).toBeEnabled();
    await chooseDatePreset("100 日");
    expect(screen.getByRole("button", { name: /extSyncLhb/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /extSyncCapitalFlow/ })).toBeEnabled();
    await chooseDatePreset("180 日");
    expect(screen.getByRole("button", { name: /extSyncCapitalFlow/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /extSyncFundamental/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /extSyncFinancial/ })).toBeEnabled();
  });

  it("uses snapshot dates and known gap coverage instead of a retained historical draft after switching modes", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-14T12:00:00+08:00"));
    renderWithWatchlist();
    await screen.findByText("280");
    await chooseSelect(1, "指定历史日期");
    await enterHistoricalRange("2026-01-01", "2026-01-23");

    await chooseSelect(1, "最近数据");
    fireEvent.click(screen.getByRole("button", { name: /extSyncFinancial/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({
      dataset: "financial", mode: "incremental", start_date: undefined, end_date: undefined, lookback_days: 1,
    })));
    fireEvent.click(screen.getByRole("button", { name: "刷新评分" }));
    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith({
      start_date: undefined, end_date: undefined, full_refresh: false,
      train_model: false, materialize_scores: true,
      universe_source: "watchlist", watchlist_id: 22,
    }));
    await openUpdateSettings();
    fireEvent.click(screen.getAllByRole("button", { name: "检查缺失数据" })[0]);
    await waitFor(() => expect(mockApi.getExternalDataGaps).toHaveBeenCalledWith("fundamental", "2026-07-01", "2026-08-02"));
  });

  it("uses known historical coverage for gap scanning rather than the recent-mode 60-day default", async () => {
    render(<ExternalDataSync />);
    await screen.findByText("extReadinessBlocked");
    await openUpdateSettings();
    fireEvent.click(screen.getAllByRole("button", { name: "检查缺失数据" })[0]);
    await waitFor(() => expect(mockApi.getExternalDataGaps).toHaveBeenCalledWith("fundamental", "2026-07-01", "2026-08-02"));
    expect(mockApi.repairExternalDataGaps).not.toHaveBeenCalled();
  });

  it("dispatches both settings navigation targets from coverage remediation", async () => {
    const listener = vi.fn();
    window.addEventListener("settings:navigate", listener);
    try {
      render(<ExternalDataSync />);
      fireEvent.click(await screen.findByRole("button", { name: "extCoverageActionModel" }));
      fireEvent.click(screen.getByRole("button", { name: "extCoverageActionApi" }));
      expect(listener.mock.calls.map(([event]) => (event as CustomEvent).detail)).toEqual(["factor-model", "api-management"]);
    } finally {
      window.removeEventListener("settings:navigate", listener);
    }
  });

  it("hides retired tail proxy collection, import and cached coverage warnings", async () => {
    render(<ExternalDataSync />);
    await waitFor(() => expect(mockApi.getExternalDataOverview).toHaveBeenCalled());
    expect(screen.queryByText("extSyncTailProxy")).not.toBeInTheDocument();
    expect(screen.queryByText("extSyncTailProxyDesc")).not.toBeInTheDocument();
    expect(screen.queryByText(/尾盘代理补充导入/)).not.toBeInTheDocument();
    expect(document.querySelector('input[type="file"]')).toBeNull();
    expect(mockApi.importTailProxyMinutes).not.toHaveBeenCalled();
  });

  it("reports preview errors and prevents starting a task on preview failure", async () => {
    mockApi.previewExternalDataSyncPlan.mockRejectedValue(new Error("provider unavailable"));
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: /extSyncFinancial/ }));
    await waitFor(() => expect(message.error).toHaveBeenCalledWith(expect.stringContaining("provider unavailable")));
    expect(mockApi.startExternalDataSync).not.toHaveBeenCalled();
  });

  it("keeps a failed update visible and retries its original range while preserving a changed draft", async () => {
    mockApi.previewExternalDataSyncPlan.mockRejectedValueOnce(new Error("数据来源暂时没有响应"));
    renderWithWatchlist();
    fireEvent.click(await screen.findByRole("button", { name: /extSyncFinancial/ }));
    expect(await screen.findByText("这次更新遇到问题")).toBeInTheDocument();
    expect(screen.getByText("数据来源暂时没有响应")).toBeInTheDocument();
    expect(mockApi.startExternalDataSync).not.toHaveBeenCalled();
    await chooseSelect(0, "extSourcePositions");
    fireEvent.click(screen.getByRole("button", { name: "重试这次更新" }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({
      dataset: "financial", source: "watchlist", watchlist_id: 22, mode: "incremental", sync_policy: "normal",
    })));
    expect(screen.getByText(/当前组合（44）/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("这次更新遇到问题")).not.toBeInTheDocument());
  });

  it("shows coverage fetch errors and recovers them through an explicit read retry", async () => {
    mockApi.getExternalDataCoverage.mockRejectedValue(new Error("warehouse busy"));
    render(<ExternalDataSync />);
    await waitFor(() => expect(screen.getByText("280")).toBeInTheDocument());
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByText(/warehouse busy/)).toBeInTheDocument();
    expect(screen.queryAllByText("读取中")).toHaveLength(0);
    mockApi.getExternalDataCoverage.mockResolvedValue({ generated_at: "2026-08-02T08:00:00", datasets: [] });
    fireEvent.click(screen.getByRole("button", { name: "重试读取检查结果" }));
    await waitFor(() => expect(screen.queryByText(/warehouse busy/)).not.toBeInTheDocument());
  });

  it("keeps legal snapshot synchronization available while blocking unverified history", async () => {
    mockApi.getExternalDataOverview.mockImplementation(() => new Promise(() => {}));
    mockApi.getExternalDataSyncCapabilities.mockImplementation(() => new Promise(() => {}));
    render(<ExternalDataSync />);
    expect(screen.getByRole("button", { name: /extSyncFundamental/ })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "导入 CSV" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /extSyncFundamental/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalled());
    await chooseSelect(1, "指定历史日期");
    expect(screen.getByRole("button", { name: /extSyncEtf/ })).toBeDisabled();
  });

  it("keeps the retired proxy hidden when switching to the all-market scope", async () => {
    render(<ExternalDataSync />);
    await waitFor(() => expect(screen.getByText("280")).toBeInTheDocument());
    await chooseSelect(0, "extSourceAllMarket");
    expect(screen.queryByRole("button", { name: /extSyncTailProxy/ })).not.toBeInTheDocument();
  });

  it("tracks gap repair tasks immediately before overview catches up", async () => {
    mockApi.getExternalDataGaps.mockResolvedValue({ dataset: "fundamental", start_date: "2026-07-01", end_date: "2026-08-02", total_missing: 1, truncated: false, gaps: [{ symbol_id: 1, symbol: "600000", trade_date: "2026-07-10" }] });
    mockApi.getExternalDataOverview.mockResolvedValue(mockOverview);
    render(<ExternalDataSync />);
    await screen.findByText("extReadinessBlocked");
    await openUpdateSettings();
    fireEvent.click(screen.getAllByRole("button", { name: "检查缺失数据" })[0]);
    await waitFor(() => expect(mockApi.repairExternalDataGaps).toHaveBeenCalled());
    expect(mockApi.repairExternalDataGaps).toHaveBeenCalledWith(expect.objectContaining({ sync_policy: "missing" }));
    await waitFor(() => expect(mockApi.getExternalDataOverview).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("button", { name: "停止更新" })).toBeInTheDocument();
    expect(mockApi.getExternalDataSyncTask).toHaveBeenCalledWith("task-fundamental");
    expect(screen.getByRole("button", { name: "刷新评分" })).toBeDisabled();
  });

  it("updates completed tasks independently when another task's polling fails", async () => {
    mockApi.getExternalDataOverview.mockResolvedValue({ ...mockOverview, running_tasks: 2, datasets: mockOverview.datasets.map((row) => ["fundamental", "financial"].includes(row.dataset) ? { ...row, latest_task: makeTask(row.dataset, "running") } : row) } as any);
    mockApi.getExternalDataSyncTask.mockImplementation(async (id: string) => {
      if (id === "task-fundamental") throw new Error("one task failed to fetch");
      return makeTask("financial", "done");
    });
    render(<ExternalDataSync />);
    await waitFor(() => expect(message.error).toHaveBeenCalledWith(expect.stringContaining("one task failed to fetch")));
    expect(screen.getAllByRole("button", { name: "停止更新" })).toHaveLength(1);
    expect(message.success).toHaveBeenCalledWith("extTaskCompleted");
  });

  it("does not overlap slow task polls and shares the read when another task starts", async () => {
    const interval = vi.spyOn(window, "setInterval");
    let resolveTask!: (task: ReturnType<typeof makeTask>) => void;
    mockApi.getExternalDataOverview.mockResolvedValue({ ...mockOverview, running_tasks: 1,
      datasets: mockOverview.datasets.map((row) => row.dataset === "fundamental"
        ? { ...row, latest_task: makeTask("fundamental", "running") } : row) } as any);
    mockApi.getExternalDataSyncTask.mockImplementation((id: string) => id === "task-fundamental"
      ? new Promise((resolve) => { resolveTask = resolve; })
      : Promise.resolve(makeTask("financial", "running")));
    mockApi.startExternalDataSync.mockResolvedValue(makeTask("financial", "running"));
    const view = render(<ExternalDataSync />);
    try {
      await waitFor(() => expect(mockApi.getExternalDataSyncTask).toHaveBeenCalledTimes(1));
      const poll = interval.mock.calls.find(([, delay]) => delay === 1000)![0] as () => void;
      await act(async () => { poll(); poll(); });
      expect(mockApi.getExternalDataSyncTask).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByRole("button", { name: /extSyncFinancial/ }));
      await waitFor(() => expect(mockApi.getExternalDataSyncTask).toHaveBeenCalledWith("task-financial"));
      expect(mockApi.getExternalDataSyncTask.mock.calls.filter(([id]) => id === "task-fundamental")).toHaveLength(1);
      await act(async () => { resolveTask(makeTask("fundamental", "running")); });
    } finally {
      view.unmount();
      interval.mockRestore();
    }
  });

  it("refreshes overview and coverage once when several tasks complete in one poll", async () => {
    const runningOverview = { ...mockOverview, running_tasks: 2,
      datasets: mockOverview.datasets.map((row) => ["fundamental", "financial"].includes(row.dataset)
        ? { ...row, latest_task: makeTask(row.dataset, "running") } : row) };
    mockApi.getExternalDataOverview.mockResolvedValueOnce(runningOverview as any).mockResolvedValue(mockOverview);
    mockApi.getExternalDataSyncTask.mockImplementation(async (id: string) => makeTask(id === "task-fundamental" ? "fundamental" : "financial", "done"));
    render(<ExternalDataSync />);
    await waitFor(() => expect(message.success).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(mockApi.getExternalDataCoverage).toHaveBeenCalledTimes(2));
    expect(mockApi.getExternalDataOverview).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "停止更新" })).not.toBeInTheDocument();
  });

  it("labels a cancelled scoring task as cancelled and stops its poll", async () => {
    mockApi.scoringCreateTask.mockResolvedValue({ id: "score-cancelled", status: "cancelled", stage: "cancelled", percent: 20 });
    renderWithWatchlist();
    fireEvent.click(await screen.findByRole("button", { name: "刷新评分" }));
    expect(await screen.findByText(/评分：已取消/)).toBeInTheDocument();
    expect(mockApi.scoringGetTask).not.toHaveBeenCalled();
  });

  it("passes an explicit historical date range to external sync and scoring refresh", async () => {
    renderWithWatchlist();
    await waitFor(() => expect(screen.getByText("280")).toBeInTheDocument());
    await chooseSelect(1, "指定历史日期");
    await enterHistoricalRange("2026-07-01", "2026-08-02");
    await userEvent.setup().click(screen.getByRole("button", { name: /extSyncCapitalFlow/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({ dataset: "capital_flow", mode: "backfill", start_date: "2026-07-01", end_date: "2026-08-02" })));
    await userEvent.setup().click(screen.getByRole("button", { name: "刷新评分" }));
    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith({ start_date: "2026-07-01", end_date: "2026-08-02", full_refresh: true, train_model: false, materialize_scores: true, universe_source: "watchlist", watchlist_id: 22 }));
  });

  it("reports partial synchronization as warning with expandable result errors", async () => {
    const task = makeTask("fundamental");
    mockApi.startExternalDataSync.mockResolvedValue({ ...task, failed_count: 1, result: { ...task.result!, failed: 1, errors: ["600000 provider error"] } } as any);
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: /extSyncFundamental/ }));
    await waitFor(() => expect(message.warning).toHaveBeenCalledWith("extTaskCompletedWithErrors"));
    fireEvent.click(screen.getByText("extErrorsTitle (1)"));
    expect(await screen.findByText("600000 provider error")).toBeVisible();
  });

  it("exposes an actionable scoring failure message", async () => {
    mockApi.scoringCreateTask.mockResolvedValue({ id: "score-failed", status: "failed", stage: "materialize", percent: 70, message: "真实模型权重缺失，请重新训练并激活" });
    renderWithWatchlist();
    fireEvent.click(await screen.findByRole("button", { name: "刷新评分" }));
    expect(await screen.findByText(/评分：失败 · 生成模型评分/)).toBeInTheDocument();
    expect(screen.queryByText(/真实模型权重缺失/)).toBeInTheDocument();
  });

  it("blocks historical actions when capability loading fails and provides an explicit retry", async () => {
    mockApi.getExternalDataSyncCapabilities.mockRejectedValue(new Error("capabilities unavailable"));
    render(<ExternalDataSync />);
    await waitFor(() => expect(screen.getByText("280")).toBeInTheDocument());
    await chooseSelect(1, "指定历史日期");
    expect(screen.getByRole("button", { name: /extSyncEtf/ })).toBeDisabled();
    expect(screen.getByPlaceholderText("开始日期")).not.toHaveValue("");
    expect(screen.getByPlaceholderText("结束日期")).not.toHaveValue("");
    for (const repair of screen.getAllByRole("button", { name: "检查缺失数据" })) {
      expect(repair).toBeDisabled();
    }
    expect(mockApi.getExternalDataGaps).not.toHaveBeenCalled();
    expect(screen.getByText("capabilities unavailable")).toBeInTheDocument();
    mockApi.getExternalDataSyncCapabilities.mockResolvedValue({ financial: { modes: ["incremental", "backfill"], history_limit_days: null, reason: "history" }, capital_flow: { modes: ["incremental"], history_limit_days: 100, reason: "history" } } as any);
    fireEvent.click(screen.getByRole("button", { name: "重试读取可用范围" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /extSyncFinancial/ })).toBeEnabled());
    const financialRow = screen.getByRole("button", { name: /extSyncFinancial/ }).closest("tr")!;
    expect(within(financialRow).getByRole("button", { name: "检查缺失数据" })).toBeEnabled();
    expect(screen.queryByText("capabilities unavailable")).not.toBeInTheDocument();
  });

  it("keeps cooperative cancellation pending until the worker reports a cancelled terminal state", async () => {
    mockApi.startExternalDataSync.mockResolvedValue(makeTask("fundamental", "running"));
    mockApi.cancelExternalDataSyncTask.mockResolvedValue({ ...makeTask("fundamental", "running"), cancel_requested: true } as any);
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: /extSyncFundamental/ }));
    fireEvent.click(await screen.findByRole("button", { name: "停止更新" }));
    await waitFor(() => expect(mockApi.cancelExternalDataSyncTask).toHaveBeenCalledWith("task-fundamental"));
    await act(async () => { await Promise.resolve(); });
    expect(message.error).not.toHaveBeenCalled();
    expect(message.info).toHaveBeenCalledWith("已请求取消，等待当前步骤结束");
    expect(screen.getByRole("button", { name: "正在停止" })).toBeDisabled();
    mockApi.getExternalDataSyncTask.mockResolvedValue({ ...makeTask("fundamental", "running"), status: "cancelled", stage: "cancelled", message: "Task cancelled by user" } as any);
    await waitFor(() => expect(message.info).toHaveBeenCalledWith("同步已取消，已完成的数据保留，可继续上次更新"), { timeout: 2500 });
    expect(screen.queryByRole("button", { name: "正在停止" })).not.toBeInTheDocument();
  });

  it.each([
    ["AWAITING_MODEL_ACTIVATION", "SKIPPED", 0, "数据已算好，还需启用评分模型"],
    ["PARTIAL_SCORE_PUBLICATION", "PARTIAL", 12, "评分部分生成"],
    ["PARTIAL_SCORE_PUBLICATION", "PARTIAL", 0, "输入不足，未生成评分"],
    ["COMPLETED", "DONE", 25, "已生成评分（25 条）"],
    ["COMPLETED", "DONE", 0, "计算完成，未生成评分"],
  ] as const)("uses score publication evidence for the %s outcome", async (completion, status, count, label) => {
    mockApi.scoringCreateTask.mockResolvedValue({ id: "score-outcome", status: "done", stage: "done", percent: 100, result: { completion_status: completion, scoring_bridge: { status, materialized_count: count } } });
    renderWithWatchlist();
    fireEvent.click(await screen.findByRole("button", { name: "刷新评分" }));
    expect(await screen.findByText(`评分：${label} · 任务结束 · 100%`)).toBeInTheDocument();
    expect(screen.queryByText("评分：已完成")).not.toBeInTheDocument();
  });

  it("sends the active portfolio identity for position scope", async () => {
    render(<AppContext.Provider value={{ portfolioId: 44, workbench: { watchlists: [] } } as any}><ExternalDataSync /></AppContext.Provider>);
    await waitFor(() => expect(screen.getByText("280")).toBeInTheDocument());
    await chooseSelect(0, "extSourcePositions");
    expect(screen.getByText(/当前组合（44）/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /extSyncCapitalFlow/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({ dataset: "capital_flow", source: "positions", portfolio_id: 44 })));
  });

  it("explains unavailable northbound input without silently treating it as zero", async () => {
    mockApi.getExternalDataSyncCapabilities.mockResolvedValue({ capital_flow: { modes: ["incremental", "backfill"], history_limit_days: 100, reason: "history", northbound: { available: false, reason: "配置的北向接口不可用", api_key: "northbound" } } } as any);
    render(<ExternalDataSync />);
    await openUpdateSettings();
    expect(await screen.findByText(/北向资金不可用：配置的北向接口不可用/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "extIncludeNorthbound" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "extIncludeNorthbound" })).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: /extSyncCapitalFlow/ }));
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({ include_northbound: false })));
  });

  it("starts capital flow without northbound while its capability is still unknown", async () => {
    mockApi.getExternalDataSyncCapabilities.mockImplementation(() => new Promise(() => {}));
    render(<ExternalDataSync />);
    await waitFor(() => expect(screen.getByText("280")).toBeInTheDocument());
    await openUpdateSettings();
    expect(screen.getByRole("checkbox", { name: "extIncludeNorthbound" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "extIncludeNorthbound" })).not.toBeChecked();
    expect(screen.getByText("北向资金能力正在确认，仅同步个股资金流")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: /extSyncCapitalFlow/ });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith(expect.objectContaining({ dataset: "capital_flow", include_northbound: false })));
  });

  it("shows ETF metric counts and unsupported tracking error as partial completion", async () => {
    const summary = "ETF 快照：溢折价 3/3，规模估算 3/3，份额 3/3；跟踪误差尚未接入";
    const task = { ...makeTask("etf"), message: summary, result: {
      ...makeTask("etf").result!, warnings: ["跟踪误差尚未接入对应基准及同日收益序列"],
      field_coverage: { etf_premium_discount: { available: 3, total: 3 } },
    } };
    mockApi.startExternalDataSync.mockResolvedValue(task as any);
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: /extSyncEtf/ }));
    await waitFor(() => expect(message.warning).toHaveBeenCalledWith(summary));
    expect(message.success).not.toHaveBeenCalled();
    expect(screen.getAllByText(summary).length).toBeGreaterThan(0);
    expect(screen.getAllByText("extStatusPartial").length).toBeGreaterThan(0);
  });

  it("keeps valid ETF premium inventory while identifying only missing or policy-blocked fields", async () => {
    const emptyField = { availability: "blocked", evaluation_enabled: false, first_date: null, latest_date: null, table_rows: 236, nonnull_rows: 0, distinct_symbols: 0, distinct_dates: 0, continuity_days: 0, daily_coverage_p50: 0, daily_coverage_p90: 0, latest_daily_coverage: 0, reason: "源表为空或字段没有非空记录。" };
    mockApi.getExternalDataCoverage.mockResolvedValue({ generated_at: "2026-10-14T10:00:00", datasets: [
      { dataset: "etf", readiness: "blocked", reason: "field_data_unavailable", fields: [
        { ...emptyField, field: "etf_premium_discount", availability: "snapshot", nonnull_rows: 228, first_date: "2026-10-01", latest_date: "2026-10-13", continuity_days: 8, distinct_symbols: 228, distinct_dates: 8, latest_daily_coverage: 0.966, reason: "ETF 当日指标快照，仅用于 ETF 快照评价。" },
        { ...emptyField, field: "etf_tracking_error" },
        { ...emptyField, field: "etf_fund_size" },
      ] },
      { dataset: "tail_proxy", readiness: "blocked", reason: "field_data_unavailable", fields: [
        { ...emptyField, field: "proxy_score", table_rows: 40, nonnull_rows: 30, reason: "尾盘分钟代理当前维持 blocked，未接入可评价的分钟历史。" },
      ] },
    ] } as any);
    render(<ExternalDataSync />);
    const etfRow = await screen.findByRole("row", { name: /extSyncEtfDesc/ });
    await waitFor(() => expect(etfRow).toHaveTextContent("非空 96.6%"));
    expect(etfRow).toHaveTextContent("etf_tracking_error：源表为空或字段没有非空记录。");
    expect(etfRow).toHaveTextContent("etf_fund_size：源表为空或字段没有非空记录。");
    expect(etfRow).not.toHaveTextContent("extCoverageReasonFieldDataUnavailableDetail");
    const notices = screen.getAllByRole("alert");
    const coverageNotice = notices.find((item) => item.textContent?.includes("extCoverageActionTitle"))!;
    expect(coverageNotice).toHaveTextContent("etf_tracking_error：源表为空或字段没有非空记录。");
    expect(coverageNotice).toHaveTextContent("etf_fund_size：源表为空或字段没有非空记录。");
    expect(within(coverageNotice).queryByText(/extCoverageReasonUnknown/)).not.toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /extSyncTailProxyDesc/ })).not.toBeInTheDocument();
    expect(coverageNotice).not.toHaveTextContent("proxy_score");
  });

  it("shows a legacy completed task with every attempt failed as failed without inventing its unknown total", async () => {
    const legacy = { ...makeTask("lhb"), total: 0, processed: 1, ok_count: 0, failed_count: 1, message: "机构接口不可用", result: { dataset: "lhb", total: 0, success: 0, skipped: 0, failed: 1, records: 0, errors: ["机构接口不可用"] } };
    mockApi.getExternalDataOverview.mockResolvedValue({ ...mockOverview, datasets: mockOverview.datasets.map((item) => item.dataset === "lhb" ? { ...item, latest_task: legacy } : item) } as any);
    render(<ExternalDataSync />);
    const row = await screen.findByRole("row", { name: /extSyncLhbDesc/ });
    expect(await within(row).findByText("extStatusFailed")).toBeInTheDocument();
    expect(row).toHaveTextContent("已处理 1 · 总数未知");
    expect(row).not.toHaveTextContent("1/0");
    expect(within(row).queryByText("extStatusPartial")).not.toBeInTheDocument();
    expect(row).toHaveTextContent("机构接口不可用");
  });
});
