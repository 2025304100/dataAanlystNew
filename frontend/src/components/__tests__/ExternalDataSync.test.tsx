import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    available_datasets: 7,
    running_tasks: 0,
    refreshed_at: "2026-08-02T08:00:00",
  };
  return {
    makeTask,
    mockOverview,
    mockApi: {
      getExternalDataOverview: vi.fn(async () => mockOverview),
      getExternalDataCoverage: vi.fn(async () => ({
        generated_at: "2026-08-02T08:00:00",
        datasets: datasets.map((dataset) => ({
          dataset,
          readiness: dataset === "tail_proxy" ? "blocked" : "available",
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
        gaps: [],
      })),
      repairExternalDataGaps: vi.fn(async () => makeTask("fundamental", "running")),
      getExternalDataSyncCapabilities: vi.fn(async () => ({
        fundamental: { modes: ["incremental", "backfill"], history_limit_days: null, reason: "history" },
        financial: { modes: ["incremental", "backfill"], history_limit_days: null, reason: "history" },
        lhb: { modes: ["incremental", "backfill"], history_limit_days: 31, reason: "history" },
        hot_rank: { modes: ["incremental"], history_limit_days: 0, reason: "snapshot" },
        tail_proxy: { modes: ["incremental"], history_limit_days: 0, reason: "snapshot" },
        capital_flow: { modes: ["incremental", "backfill"], history_limit_days: 100, reason: "history" },
        etf: { modes: ["incremental"], history_limit_days: 0, reason: "snapshot" },
      })),
      previewExternalDataSyncPlan: vi.fn(async (payload: any) => ({
        dataset: payload.dataset,
        mode: payload.mode,
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
      getExternalDataSyncTask: vi.fn(async () => makeTask("fundamental", "running")),
      getExternalDataSyncTaskPartitions: vi.fn(async () => ({
        task_id: "task-fundamental",
        plan: null,
        partitions: [],
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
    },
  };
});

vi.mock("../../api/client", () => ({ api: mockApi }));

import ExternalDataSync from "../ExternalDataSync";

describe("ExternalDataSync dashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.getExternalDataOverview.mockResolvedValue(mockOverview);
    mockApi.getExternalDataSyncCapabilities.mockClear();
    mockApi.previewExternalDataSyncPlan.mockClear();
    mockApi.startExternalDataSync.mockImplementation(async (payload: any) => makeTask(payload.dataset));
    mockApi.getExternalDataSyncTask.mockResolvedValue(makeTask("fundamental", "running"));
  });

  it("renders the real inventory dashboard for all seven datasets", async () => {
    render(<ExternalDataSync />);
    await waitFor(() => expect(mockApi.getExternalDataOverview).toHaveBeenCalled());

    expect(screen.getByText("280")).toBeInTheDocument();
    expect(screen.getByText("28")).toBeInTheDocument();
    expect(screen.getByText("7/7")).toBeInTheDocument();
    expect(screen.getAllByText("extSyncFundamental").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncFinancial").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncLhb").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncHotRank").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncTailProxy").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncCapitalFlow").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncEtf").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByRole("button", { name: "检查并修复缺口" })).toHaveLength(3);
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
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith({
      dataset: "fundamental",
      source: "watchlist",
      include_northbound: true,
      mode: "incremental",
      lookback_days: 1,
      limit: 20,
    }));
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

    await waitFor(() => expect(screen.getByText("25%")).toBeInTheDocument());
    expect(screen.getByText("2/8")).toBeInTheDocument();
    expect(screen.getByText(/600000/)).toBeInTheDocument();
  });

  it("cancels the active external sync from the progress card", async () => {
    mockApi.startExternalDataSync.mockResolvedValue(makeTask("fundamental", "running"));
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: /extSyncFundamental/ }));
    const cancelButton = await screen.findByRole("button", { name: "取消同步" });
    fireEvent.click(cancelButton);
    await waitFor(() => expect(mockApi.cancelExternalDataSyncTask).toHaveBeenCalledWith("task-fundamental"));
  });

  it("shows completion feedback and the structured result", async () => {
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: /extSyncFundamental/ }));

    await waitFor(() => expect(message.success).toHaveBeenCalledWith("extTaskCompleted"));
    expect(screen.getByText(/extSyncFundamental: extSyncResult/)).toBeInTheDocument();
  });
});
