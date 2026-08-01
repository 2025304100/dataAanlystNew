import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

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
    },
  };
});

vi.mock("../../api/client", () => ({
  api: mockApi,
}));

import UniverseDataPanel from "../UniverseDataPanel";

describe("UniverseDataPanel sync panel interaction", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
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
});
