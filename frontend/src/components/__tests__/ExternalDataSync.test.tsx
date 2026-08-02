import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { message } from "antd";

const { mockApi, makeTask, mockOverview } = vi.hoisted(() => {
  const datasets = ["fundamental", "financial", "lhb", "hot_rank", "tail_proxy", "capital_flow", "etf"] as const;
  const makeTask = (dataset: typeof datasets[number], status: "running" | "done" = "done") => ({
    id: `task-${dataset}`,
    task_type: `external_sync_${dataset}`,
    status,
    stage: status === "done" ? "done" : "sync",
    percent: status === "done" ? 100 : 25,
    message: status === "done" ? "completed" : "syncing",
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
    errors: [],
    created_at: "2026-08-02T08:00:00",
    started_at: "2026-08-02T08:00:01",
    finished_at: status === "done" ? "2026-08-02T08:00:05" : null,
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
      startExternalDataSync: vi.fn(async (payload: { dataset: typeof datasets[number] }) => makeTask(payload.dataset)),
      getExternalDataSyncTask: vi.fn(async () => makeTask("fundamental", "running")),
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
  });

  it("starts valuation as an observable task with the selected scope", async () => {
    render(<ExternalDataSync />);
    const button = await screen.findByRole("button", { name: /extSyncFundamental/ });
    fireEvent.click(button);
    await waitFor(() => expect(mockApi.startExternalDataSync).toHaveBeenCalledWith({
      dataset: "fundamental",
      source: "watchlist",
      include_northbound: true,
      lookback_days: 30,
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

  it("shows completion feedback and the structured result", async () => {
    render(<ExternalDataSync />);
    fireEvent.click(await screen.findByRole("button", { name: /extSyncFundamental/ }));

    await waitFor(() => expect(message.success).toHaveBeenCalledWith("extTaskCompleted"));
    expect(screen.getByText(/extSyncFundamental: extSyncResult/)).toBeInTheDocument();
  });
});
