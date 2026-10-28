import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ status: "partial", recentOnly: false, stages: [] as any[] }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ detail: null, symbolDirectory: {}, fetchVisibleSymbols: async () => {} }) }));
vi.mock("../../api/client", () => ({ api: {
  getHistoryInitializationStatus: async () => {
    const task = { task_id: "isolated", status: state.status, preset: "1m", repair_mode: "bars", progress_pct: 100, message: state.status === "cancelled" ? "Cancelled by user" : "One supplier failed", stages: state.stages, failed_items: [], recent_runs: [], summary: { symbols_total: 2, sync_ok_count: 1, sync_failed_count: 1, empty_count: 0, bars_rows: 1, score_days_total: 0, score_days_completed: 0 } };
    return state.recentOnly ? { ...task, task_id: null, status: "idle", message: null, recent_runs: [task] } : task;
  },
  getActiveScoringConfig: async () => null,
} }));

import HistoryInitSection from "../HistoryInitSection";

afterEach(cleanup);
beforeEach(() => {
  state.status = "partial";
  state.recentOnly = false;
  state.stages = [];
});

describe("initialization terminal outcomes", () => {
  it.each([
    { status: "partial", done: 5, total: 10, percent: 50 },
    { status: "failed", done: 0, total: 1, percent: 0 },
    { status: "cancelled", done: 0, total: 1, percent: 0 },
    { status: "partial", done: 0, total: 0, percent: 0 },
    { status: "completed", done: 10, total: 10, percent: 100 },
    { status: "running", done: 1, total: 8, percent: 13 },
  ])("derives overall and stage progress from counts for $status $done/$total", async ({ status, done, total, percent }) => {
    state.status = status;
    state.stages = [{ key: "calc_scores", status, done, total, percent: 100, message: "Count-based progress" }];
    const { container } = render(<HistoryInitSection />);
    await waitFor(() => expect(container.querySelector(".history-init-overall-count")).toHaveTextContent(`${done}/${total} · ${percent}%`));
    expect(container.querySelector(".history-init-progress-wrap .ant-progress-bg")).toHaveStyle({ width: `${percent}%` });
    expect(container.querySelector(".history-init-stage .ant-progress-bg")).toHaveStyle({ width: `${percent}%` });
  });

  it("keeps cancelled overall and stage progress out of the failure visual state", async () => {
    state.status = "cancelled";
    state.stages = [{ key: "sync_bars", status: "cancelled", done: 0, total: 1, percent: 0, message: "Cancelled by user" }];
    const { container } = render(<HistoryInitSection />);
    await screen.findByText("上一次执行已取消");
    expect(container.querySelector(".history-init-progress-wrap .ant-progress-status-exception")).toBeNull();
    expect(container.querySelector(".history-init-stage .ant-progress-status-exception")).toBeNull();
    expect(container.querySelector(".history-init-progress-wrap .ant-alert-error")).toBeNull();
    expect(container.querySelector(".history-init-stage .ant-progress-bg")).toHaveStyle({ background: "rgb(250, 173, 20)" });
  });

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
    expect(screen.getByText("最近一次执行失败").closest(".ant-alert")).toHaveClass("ant-alert-error");
  });

  it("shows a cancelled recent run as information without a failure title", async () => {
    state.status = "cancelled";
    state.recentOnly = true;
    render(<HistoryInitSection />);
    const title = await screen.findByText("上一次执行已取消");
    const alert = title.closest(".ant-alert");
    expect(alert).toHaveClass("ant-alert-info");
    expect(alert).not.toHaveTextContent("失败");
    expect(screen.queryByText("最近一次执行失败")).not.toBeInTheDocument();
  });

  it.each([
    ["failed", "rgb(207, 19, 34)"],
    ["partial", "rgb(212, 136, 6)"],
    ["cancelled", "rgb(212, 136, 6)"],
  ])("uses the expected execution-record message color for %s", async (status, color) => {
    state.status = status;
    state.recentOnly = true;
    const { container } = render(<HistoryInitSection />);
    await waitFor(() => expect(container.querySelector(".history-run-item__message")).not.toBeNull());
    expect(container.querySelector(".history-run-item__message")).toHaveStyle({ color });
  });

  it.each([
    { label: "近1个月", days: 21 },
    { label: "1个季度", days: 61 },
    { label: "近1年", days: 244 },
    { label: "3年", days: 732 },
  ])("shows approximately $days trading days for $label", async ({ label, days }) => {
    render(<HistoryInitSection />);
    await screen.findByText("约 244 个交易日");
    fireEvent.click(screen.getByRole("radio", { name: label }));
    expect(screen.getByText(`约 ${days} 个交易日`)).toBeVisible();
  });

  it("updates the trading-day hint whenever the preset changes", async () => {
    const { container } = render(<HistoryInitSection />);
    await screen.findByText("约 244 个交易日");
    for (const [label, days] of [["近1个月", 21], ["1个季度", 61], ["3年", 732], ["近1年", 244]] as const) {
      fireEvent.click(screen.getByRole("radio", { name: label }));
      expect(container.querySelector(".history-init-trading-days-hint")).toHaveTextContent(`约 ${days} 个交易日`);
      expect(container.querySelectorAll(".history-init-trading-days-hint")).toHaveLength(1);
    }
  });
});
