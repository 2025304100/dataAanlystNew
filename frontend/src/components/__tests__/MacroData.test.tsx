import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";

const baseOverview = {
  region: "all",
  snapshot: {
    region: "all",
    market_score: 62.5,
    stance: "cautious",
    summary: "ok",
    growth_score: 3,
    inflation_score: 1,
    liquidity_score: 2,
    credit_score: 0,
    risk_score: -1,
    indicators_total: 3,
    failed_total: 0,
    created_at: "2026-07-11T10:00:00",
  },
  indicators: [
    {
      region: "us",
      indicator_key: "us_cpi_yoy",
      name: "US CPI YoY",
      category: "inflation",
      period: "2026-06",
      value: 2.1,
      previous_value: 2.0,
      delta: 0.1,
      unit: "%",
      frequency: "monthly",
      source: "macro-api",
      score: 0.5,
      status: "neutral",
    },
  ],
  brief: ["Macro is stable"],
  failed: [],
};

const { mockContext, mockApi } = vi.hoisted(() => ({
  mockContext: {
    locale: "en-US" as const,
    showToast: vi.fn((_type: string, _msg: string) => {}),
  },
  mockApi: {
    getMacroOverview: vi.fn(async () => baseOverview),
    getLatestMacroUpdateTask: vi.fn(async () => null),
    getMacroUpdateTask: vi.fn(async () => null),
    startMacroUpdateTask: vi.fn(async () => null),
    cancelMacroUpdateTask: vi.fn(async () => null),
    getMacroIndicatorHistory: vi.fn(async () => []),
  },
}));

vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

vi.mock("../../api/client", () => ({
  api: mockApi,
}));

vi.mock("echarts-for-react", () => ({
  default: () => <div data-testid="mock-chart" />,
}));

import MacroData from "../MacroData";

describe("MacroData", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    mockContext.locale = "en-US";
    mockApi.getMacroOverview.mockImplementation(async () => ({ ...baseOverview }));
    mockApi.getLatestMacroUpdateTask.mockImplementation(async () => null);
    mockApi.getMacroIndicatorHistory.mockImplementation(async () => []);
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("keeps partial failure details after background task finishes", async () => {
    mockApi.startMacroUpdateTask.mockImplementation(async () => ({
      id: "task-1",
      task_type: "macro_update",
      status: "queued",
      stage: "fetch",
      percent: 0,
      message: "queued",
      total: 1,
      processed: 0,
      ok_count: 0,
      failed_count: 0,
      current_item: null,
      result: null,
      errors: [],
    }));
    mockApi.getMacroUpdateTask.mockImplementation(async () => ({
      id: "task-1",
      task_type: "macro_update",
      status: "done",
      stage: "done",
      percent: 100,
      message: "Macro update completed",
      total: 1,
      processed: 1,
      ok_count: 0,
      failed_count: 1,
      current_item: null,
      result: {
        failed: [{ indicator_key: "us_cpi_yoy", name: "US CPI YoY", error: "source timeout" }],
      },
      errors: [],
    }));

    render(<MacroData />);

    await waitFor(() => {
      expect(mockApi.getMacroOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole("button", { name: "Update Macro" }));

    await act(async () => {
      vi.advanceTimersByTime(2600);
    });

    await waitFor(() => {
      expect(mockApi.getMacroUpdateTask).toHaveBeenCalledWith("task-1");
    });

    expect(screen.getByText(/partial failures/i)).toBeInTheDocument();
    expect(mockContext.showToast).toHaveBeenCalledWith("info", expect.stringMatching(/partial failures/i));

    fireEvent.click(screen.getAllByRole("button", { name: "View failure details" })[0]);

    expect(await screen.findByText("source timeout")).toBeInTheDocument();
    expect(screen.getByText("US CPI YoY")).toBeInTheDocument();
  });
});
