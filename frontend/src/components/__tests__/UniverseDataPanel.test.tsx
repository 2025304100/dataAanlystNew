import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach } from "vitest";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getUniverseStats: vi.fn(async () => ({
      total_symbols: 0,
      synced_symbols: 0,
      failed_symbols: 0,
      circuit_broken_symbols: 0,
      total_bars: 0,
      by_type: {},
      latest_synced_at: null,
      is_empty: true,
    })),
    getUniverseInitStatus: vi.fn(async () => null),
    getUniverseSmartSyncStatus: vi.fn(async () => null),
    getUniverseIncrementalSyncStatus: vi.fn(async () => null),
    getUniverseBackfillStatus: vi.fn(async () => null),
    getUniverseRangeRepairStatus: vi.fn(async () => null),
    getIndexPricesStatus: vi.fn(async () => ({
      items: [{
        symbol: "000300", name: "沪深300", bar_count: 0,
        first_date: null, last_date: null, freshness_days: null, linearity_dev_pct: null,
      }],
    })),
    syncIndexPrices: vi.fn(async () => ({
      id: "index-sync-1", task_type: "index_prices_sync", status: "queued", stage: "queued",
      percent: 0, message: "任务已创建", total: 1, processed: 0, ok_count: 0, failed_count: 0,
      current_item: null, result: null, errors: [], error_code: null,
      created_at: null, started_at: null, finished_at: null,
    })),
    getIndexPricesSyncTask: vi.fn(async () => ({
      id: "index-sync-1", task_type: "index_prices_sync", status: "running", stage: "sync",
      percent: 20, message: "正在同步", total: 1, processed: 0, ok_count: 0, failed_count: 0,
      current_item: "000300", result: null, errors: [], error_code: null,
      created_at: null, started_at: null, finished_at: null,
    })),
    syncAllBenchmarkIndices: vi.fn(async () => ({})),
    startUniverseInit: vi.fn(async () => ({})),
    retryUniverseInit: vi.fn(async () => ({})),
    cancelUniverseInit: vi.fn(async () => ({})),
    startUniverseSmartSync: vi.fn(async () => ({})),
    cancelUniverseSmartSync: vi.fn(async () => ({})),
    startUniverseIncrementalSync: vi.fn(async () => ({})),
    cancelUniverseIncrementalSync: vi.fn(async () => ({})),
    startUniverseBackfill: vi.fn(async () => ({})),
    cancelUniverseBackfill: vi.fn(async () => ({})),
    startUniverseRangeRepair: vi.fn(async () => ({})),
    cancelUniverseRangeRepair: vi.fn(async () => ({})),
  },
}));

vi.mock("../../i18n", () => ({
  t: (key: string) => key,
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
      open: vi.fn(),
    },
  };
});

vi.mock("../../api/client", () => ({
  api: mockApi,
}));

import UniverseDataPanel from "../UniverseDataPanel";

describe("UniverseDataPanel sync panel interaction", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    localStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("defaults to incremental sync and only renders the incremental panel", async () => {
    render(<UniverseDataPanel />);

    await waitFor(() => {
      expect(mockApi.getUniverseStats).toHaveBeenCalled();
      expect(mockApi.getUniverseIncrementalSyncStatus).toHaveBeenCalled();
    });

    expect(screen.getByText("universeViewingNow")).toBeInTheDocument();
    expect(screen.getByText("universeCurrentSelection")).toBeInTheDocument();
    expect(screen.getByText("universeIncrementalTitle")).toBeInTheDocument();
    expect(screen.queryByText("universeSmartTitle")).not.toBeInTheDocument();
    expect(screen.queryByText("universeBackfillTitle")).not.toBeInTheDocument();
    expect(screen.queryByText("universeRangeRepairTitle")).not.toBeInTheDocument();
  });

  it("switches daily cards so only the selected daily panel is rendered", async () => {
    const user = userEvent.setup();
    render(<UniverseDataPanel />);

    await waitFor(() => {
      expect(screen.getByText("universeIncrementalTitle")).toBeInTheDocument();
    });

    await user.click(screen.getByText("universeQuickSmartTitle"));

    await waitFor(() => {
      expect(screen.getByText("universeSmartTitle")).toBeInTheDocument();
    });

    expect(screen.queryByText("universeIncrementalTitle")).not.toBeInTheDocument();
  });

  it("starts incremental sync with the selected scope and rejects an empty scope", async () => {
    const user = userEvent.setup();
    render(<UniverseDataPanel />);

    await waitFor(() => {
      expect(screen.getByText("universeIncrementalTitle")).toBeInTheDocument();
    });

    const cnStockScope = screen.getByRole("checkbox", { name: "universeScopeCnStock" });
    const startButton = screen.getByRole("button", { name: /universeIncrementalStart/ });
    expect(cnStockScope).toBeChecked();

    await user.click(startButton);
    await waitFor(() => {
      expect(mockApi.startUniverseIncrementalSync).toHaveBeenCalledWith(5, ["cn-stock"]);
    });

    await user.click(cnStockScope);
    expect(startButton).toBeDisabled();
  });

  it("defaults to init on history tab and switches to backfill when the backfill card is clicked", async () => {
    const user = userEvent.setup();
    render(<UniverseDataPanel />);

    await user.click(screen.getByText("universeTabHistory"));

    await waitFor(() => {
      expect(screen.getByText("universeTaskTitle")).toBeInTheDocument();
    });

    expect(screen.queryByText("universeBackfillTitle")).not.toBeInTheDocument();

    await user.click(screen.getByText("universeQuickBackfillTitle"));

    await waitFor(() => {
      expect(screen.getByText("universeBackfillTitle")).toBeInTheDocument();
    });

    expect(screen.queryByText("universeTaskTitle")).not.toBeInTheDocument();
  });

  it("polls incremental status every second without refreshing full stats every second", async () => {
    vi.useFakeTimers();
    mockApi.getUniverseIncrementalSyncStatus.mockResolvedValue({
      id: "incr-running",
      status: "running",
      stage: "sync_incremental",
      percent: 10,
      message: "running",
      total: 100,
      processed: 10,
      ok_count: 10,
      failed_count: 0,
      result: null,
      errors: [],
      created_at: null,
      started_at: null,
      finished_at: null,
    } as never);

    render(<UniverseDataPanel />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    const initialStatusCalls = mockApi.getUniverseIncrementalSyncStatus.mock.calls.length;
    const initialStatsCalls = mockApi.getUniverseStats.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    expect(mockApi.getUniverseIncrementalSyncStatus.mock.calls.length).toBeGreaterThanOrEqual(initialStatusCalls + 3);
    expect(mockApi.getUniverseStats).toHaveBeenCalledTimes(initialStatsCalls);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(7000);
    });
    expect(mockApi.getUniverseStats.mock.calls.length).toBeGreaterThan(initialStatsCalls);
  });

  it("shows aggregate index-sync progress and submits the clicked row symbol", async () => {
    const user = userEvent.setup();
    render(<UniverseDataPanel />);

    await waitFor(() => {
      expect(screen.getByText("沪深300")).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /同步$/ }));

    await waitFor(() => {
      expect(mockApi.syncIndexPrices).toHaveBeenCalledWith({
        symbols: ["000300"],
        history_days: 1825,
      });
    });

    expect(screen.getByTestId("index-sync-total-progress")).toHaveTextContent("同步总进度");
    expect(screen.getByTestId("index-sync-total-progress")).toHaveTextContent("已处理 0 / 1 个指数");
  });
});
