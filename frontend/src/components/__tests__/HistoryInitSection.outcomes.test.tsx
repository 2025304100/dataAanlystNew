import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ status: "partial" }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ detail: null, symbolDirectory: {}, fetchVisibleSymbols: async () => {} }) }));
vi.mock("../../api/client", () => ({ api: {
  getHistoryInitializationStatus: async () => ({ task_id: "isolated", status: state.status, preset: "1m", repair_mode: "bars", progress_pct: 100, message: "One supplier failed", stages: [], failed_items: [], recent_runs: [], summary: { symbols_total: 2, sync_ok_count: 1, sync_failed_count: 1, empty_count: 0, bars_rows: 1, score_days_total: 0, score_days_completed: 0 } }),
  getActiveScoringConfig: async () => null,
} }));

import HistoryInitSection from "../HistoryInitSection";

afterEach(cleanup);

describe("initialization terminal outcomes", () => {
  it("shows partial completion as a warning without success progress", async () => {
    state.status = "partial";
    const { container } = render(<HistoryInitSection />);
    await waitFor(() => expect(screen.getAllByText("部分完成，存在失败项").length).toBeGreaterThan(0));
    expect(container.querySelector(".history-init-progress-wrap .ant-progress-status-success")).toBeNull();
    expect(container.querySelector(".ant-alert-warning")).not.toBeNull();
  });

  it("shows complete failure as an error", async () => {
    state.status = "failed";
    const { container } = render(<HistoryInitSection />);
    await waitFor(() => expect(container.querySelector(".history-init-progress-wrap .ant-progress-status-exception")).not.toBeNull());
    expect(container.querySelector(".ant-alert-error")).not.toBeNull();
  });
});
