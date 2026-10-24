import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp, message } from "antd";
import HistoryInitSection from "../HistoryInitSection";
import { AppProvider } from "../../context/AppContext";
import { setLocale } from "../../i18n";

const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
const running = {
  task_id: "history-one", status: "running", preset: "1y", repair_mode: "bars", symbol_ids: [11],
  start_date: "2025-01-01", end_date: "2025-12-31", progress_pct: 40,
  message: "正在补充历史行情", stages: [], failed_items: [], recent_runs: [],
  summary: { symbols_total: 1, sync_ok_count: 0, sync_failed_count: 0, empty_count: 0, bars_rows: 0, score_days_total: 0, score_days_completed: 0 },
};
const context = { portfolioId: 1, symbolId: 11, symbolIds: [11], symbolLabel: "600010", repairMode: "bars" as const, startDate: "2025-01-01", endDate: "2025-12-31" };
const writes: string[] = [];
let readStatus: () => Promise<unknown>;

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); setLocale("zh-CN"); writes.length = 0;
  readStatus = async () => running;
  // Only HTTP is replaced. The component, application context, client and controls are real.
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if ((init?.method || "GET") !== "GET") { writes.push(url); throw new Error(`Unexpected write: ${url}`); }
    if (url.endsWith("/market-data/initialize-history/status")) return json(await readStatus());
    if (url.includes("/settings/scoring-configs/active")) return json(null);
    if (url === "/api/v1/portfolios") return json([{ id: 1, name: "本地测试组合", is_default: 1, currency: "CNY" }]);
    if (url.includes("/dashboard/workbench?")) return json({ portfolio: { id: 1, name: "本地测试组合" }, candidates: [], latest_scores: [], positions: [], actions: [], watchlists: [], active_rule: null });
    if (url.includes("/system/data-health")) return json(null);
    if (url.includes("/market-events")) return json({ events: [] });
    return json([]);
  }));
});

afterEach(async () => { cleanup(); await act(async () => { message.destroy(); }); vi.unstubAllGlobals(); });

function openHistory() {
  return render(<AntdApp><AppProvider><HistoryInitSection context={context} /></AppProvider></AntdApp>);
}

it("recovers a disconnected progress read in place without starting another preparation", async () => {
  let reads = 0;
  readStatus = async () => {
    reads += 1;
    if (reads === 1) return running;
    if (reads === 2) throw new TypeError("connection lost");
    if (reads === 3) return { ...running, progress_pct: 60, message: "继续补充原来的行情资料" };
    return { ...running, status: "completed", progress_pct: 100, message: "本次行情补充已完成" };
  };
  openHistory();
  expect((await screen.findAllByText("正在补充历史行情"))[0]).toBeVisible();
  expect(await screen.findByText("暂时读不到进度，任务可能仍在运行。", {}, { timeout: 4000 })).toBeVisible();
  expect(screen.getByText(/上次读取：/)).toBeVisible();
  expect(screen.getAllByText("正在补充历史行情")[0]).toBeVisible();
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "40");
  const retry = screen.getByRole("button", { name: "重新查看进度" });
  fireEvent.click(retry);
  await screen.findAllByText("继续补充原来的行情资料");
  await screen.findAllByText("本次行情补充已完成", {}, { timeout: 4000 });
  expect(screen.queryByText("暂时读不到进度，任务可能仍在运行。")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /补充所需资料/ })).toBeEnabled();
  expect(writes).toEqual([]);
});

it("keeps preparation disabled until a failed initial read is explicitly recovered", async () => {
  let finish!: (value: unknown) => void;
  let reads = 0;
  readStatus = async () => {
    reads += 1;
    if (reads === 1) throw new TypeError("initial connection lost");
    return new Promise((resolve) => { finish = resolve; });
  };
  openHistory();
  expect(await screen.findByText("历史资料任务读取失败，还不能确认是否已有任务在运行。")).toBeVisible();
  const start = screen.getByRole("button", { name: /补充所需资料/ });
  expect(start).toBeDisabled();
  fireEvent.click(start);
  fireEvent.click(screen.getByRole("button", { name: "重新查看进度" }));
  await waitFor(() => expect(reads).toBe(2));
  expect(start).toBeDisabled();
  await act(async () => finish({ ...running, task_id: undefined, status: "idle", progress_pct: 0, message: "" }));
  await waitFor(() => expect(start).toBeEnabled());
  expect(screen.queryByText("历史资料任务读取失败，还不能确认是否已有任务在运行。")).not.toBeInTheDocument();
  expect(writes).toEqual([]);
});
