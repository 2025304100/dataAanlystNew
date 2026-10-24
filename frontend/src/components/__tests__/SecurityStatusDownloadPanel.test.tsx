import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import SecurityStatusDownloadPanel from "../SecurityStatusDownloadPanel";

const scope = (id = 1) => ({ portfolio_id: id, portfolio_name: id === 1 ? "长期组合" : "备用组合", scope_sha256: String(id).repeat(64),
  symbols: [{ symbol_id: id, symbol: id === 1 ? "600165" : "601456", name: "组合成员", supported: true }],
  start_date: "2025-10-20", end_date: "2026-10-19", max_symbols: 20, max_days: 366, can_download: true, reasons: [], latest_task: null });
const task = (status = "queued", id = "task-one") => ({ id, task_type: "security_status_download", status, percent: 50,
  message: status === "done" ? "下载完成，历史资料仍不完整。" : "正在下载历史股票状态", cancel_requested: false });
const report = { portfolio_id: 1, portfolio_name: "长期组合", start_date: "2025-10-20", end_date: "2026-10-19", valid_daily_rows: 240,
  can_import: false, backtest_ready: false, calendar_complete: true, raw_files: [{ file: "daily_600165.json" }],
  source_error: null, errors: [], missing_fields: ["is_delisting_period", "daily_price_limits", "source_updated_at"],
  symbols: [{ symbol: "600165", display_name: "组合成员", valid_daily_rows: 240, st_days: 4, suspended_days: 1, missing_trade_dates: [], errors: [], days: [] }] };
let requests: Array<{ url: string; method: string; body?: any }>;
let handle: (url: string, method: string, body?: any) => Promise<any>;
beforeEach(() => {
  requests = [];
  window.localStorage.clear();
  handle = async (url, method) => url.includes("/download-scope") ? scope(url.includes("portfolio_id=2") ? 2 : 1)
    : method === "POST" ? { task: task(), report: null } : { task: task("done"), report };
  vi.stubGlobal("fetch", vi.fn(async (url: string, options: any = {}) => {
    const method = options.method || "GET", body = options.body ? JSON.parse(options.body) : undefined;
    requests.push({ url: String(url), method, body });
    const value = await handle(String(url), method, body);
    return { ok: true, json: async () => value };
  }));
});
afterEach(() => { cleanup(); window.localStorage.clear(); vi.unstubAllGlobals(); });

it("downloads the current portfolio with one action and names the remaining gaps", async () => {
  render(<SecurityStatusDownloadPanel portfolioId={1} />);
  expect(await screen.findByText(/长期组合.*1 个标的/)).toBeVisible();
  expect(requests.every(r => r.method === "GET")).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "下载当前组合资料" }));
  expect(await screen.findByText("下载完成，历史资料仍不完整。")).toBeVisible();
  expect(screen.getByText(/退市整理期的逐日状态/)).toBeVisible();
  expect(screen.getByText(/每天的涨跌停价格、无限制交易日和报价单位/)).toBeVisible();
  expect(screen.getByText(/资料原始更新时间/)).toBeVisible();
  expect(screen.getByText(/尚不能用于正式回测/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "导入记录" })).not.toBeInTheDocument();
  const writes = requests.filter(r => r.method === "POST");
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toMatchObject({ portfolio_id: 1, start_date: "2025-10-20", end_date: "2026-10-19", expected_scope_sha256: "1".repeat(64) });
  expect(writes[0].body.request_id).toMatch(/^[a-f0-9-]{36}$/);
});

it("can recover an unconfirmed submission after re-entry even when the first scope read fails", async () => {
  handle = async (url, method) => {
    if (method === "POST") throw new Error("connection lost after submission");
    return url.includes("/download-scope") ? scope() : { task: task("done"), report };
  };
  const first = render(<SecurityStatusDownloadPanel portfolioId={1} />);
  await screen.findByText(/长期组合.*1 个标的/);
  fireEvent.click(screen.getByRole("button", { name: "下载当前组合资料" }));
  await screen.findByText("尚未确认下载是否开始，请先查看下载结果。");
  expect(requests.filter(r => r.method === "POST")).toHaveLength(1);
  first.unmount();
  handle = async () => { throw new Error("scope unavailable"); };
  render(<SecurityStatusDownloadPanel portfolioId={1} />);
  await screen.findByText("下载范围或已有记录读取失败，请重新读取。");
  handle = async url => url.includes("/download-scope") ? scope() : { task: task("done"), report };
  fireEvent.click(screen.getByRole("button", { name: "查看下载结果" }));
  expect(await screen.findByText("下载完成，历史资料仍不完整。")).toBeVisible();
  expect(requests.filter(r => r.method === "POST")).toHaveLength(1);
});

it("ignores a scope read that finishes after switching portfolios", async () => {
  let finish!: (value: unknown) => void;
  handle = async url => url.includes("portfolio_id=1") ? new Promise(resolve => { finish = resolve; }) : scope(2);
  const view = render(<SecurityStatusDownloadPanel portfolioId={1} />);
  await waitFor(() => expect(requests).toHaveLength(1));
  view.rerender(<SecurityStatusDownloadPanel portfolioId={2} />);
  expect(await screen.findByText(/备用组合.*1 个标的/)).toBeVisible();
  await act(async () => finish(scope(1)));
  expect(screen.queryByText(/长期组合.*1 个标的/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "下载当前组合资料" })).toBeEnabled();
});

it("does not attach a late submission result to the newly selected portfolio", async () => {
  let finish!: (value: unknown) => void;
  handle = async (url, method, body) => method === "POST"
    ? body.portfolio_id === 1 ? new Promise(resolve => { finish = resolve; }) : { task: task("done", "task-two"), report: { ...report, portfolio_id: 2 } }
    : scope(url.includes("portfolio_id=2") ? 2 : 1);
  const view = render(<SecurityStatusDownloadPanel portfolioId={1} />);
  await screen.findByText(/长期组合.*1 个标的/);
  fireEvent.click(screen.getByRole("button", { name: "下载当前组合资料" }));
  await waitFor(() => expect(requests.some(r => r.method === "POST")).toBe(true));
  view.rerender(<SecurityStatusDownloadPanel portfolioId={2} />);
  await screen.findByText(/备用组合.*1 个标的/);
  await act(async () => finish({ task: task("done"), report }));
  expect(screen.queryByText("下载完成，历史资料仍不完整。")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下载当前组合资料" }));
  await screen.findByText("下载完成，历史资料仍不完整。");
  expect(requests.filter(r => r.method === "POST").map(r => r.body.portfolio_id)).toEqual([1, 2]);
});

it("keeps the same request when recovery finds no task and the user explicitly retries", async () => {
  let submitted = false;
  handle = async (url, method) => {
    if (method === "POST") {
      if (!submitted) { submitted = true; throw new Error("reply lost"); }
      return { task: task("done"), report };
    }
    return url.includes("/download-scope") ? scope() : { task: null, report: null };
  };
  render(<SecurityStatusDownloadPanel portfolioId={1} />);
  await screen.findByText(/长期组合.*1 个标的/);
  fireEvent.click(screen.getByRole("button", { name: "下载当前组合资料" }));
  await screen.findByText("尚未确认下载是否开始，请先查看下载结果。");
  expect(screen.getByRole("button", { name: "下载当前组合资料" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "查看下载结果" }));
  await screen.findByText("还没找到这次下载记录，可以重试同一次下载。");
  expect(requests.filter(r => r.method === "POST")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "重试下载" }));
  await screen.findByText("下载完成，历史资料仍不完整。");
  const writes = requests.filter(r => r.method === "POST");
  expect(writes).toHaveLength(2);
  expect(writes[1].body).toEqual(writes[0].body);
});

it("fills the allowed download dates without changing the original backtest range or starting a download", async () => {
  const requested = { start: "2023-01-01", end: "2026-10-22" };
  render(<SecurityStatusDownloadPanel portfolioId={1} requestedRange={requested} />);
  await screen.findByText("本次日期超出了来源可提供的范围");
  expect(screen.getByLabelText("历史资料开始日期")).toHaveValue("2023-01-01");
  expect(screen.getByLabelText("历史资料结束日期")).toHaveValue("2026-10-22");
  fireEvent.click(screen.getByRole("button", { name: "按可下载日期填写" }));
  expect(screen.getByLabelText("历史资料开始日期")).toHaveValue("2025-10-19");
  expect(screen.getByLabelText("历史资料结束日期")).toHaveValue("2026-10-19");
  expect(screen.getByLabelText("历史资料开始日期").closest("details")).toHaveAttribute("open");
  expect(screen.getByText(/原回测日期：2023-01-01 至 2026-10-22/)).toBeVisible();
  expect(screen.getByText(/2023-01-01 至 2025-10-18/)).toBeVisible();
  expect(screen.getByText(/2026-10-20 至 2026-10-22/)).toBeVisible();
  expect(screen.getByRole("button", { name: "下载当前组合资料" })).toBeEnabled();
  expect(requests.every(request => request.method === "GET")).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "下载当前组合资料" }));
  await waitFor(() => expect(requests.find(request => request.method === "POST")?.body).toMatchObject({ portfolio_id: 1, start_date: "2025-10-19", end_date: "2026-10-19" }));
});

it.each([
  ["2026-10-01", "2026-10-22", "2026-10-01", "2026-10-19"],
  ["2023-01-01", "2024-03-01", "2023-03-02", "2024-03-01"],
])("clips only the requested download interval %s to %s, including leap-day limits", async (start, end, filledStart, filledEnd) => {
  render(<SecurityStatusDownloadPanel portfolioId={1} requestedRange={{ start, end }} />);
  await screen.findByText("本次日期超出了来源可提供的范围");
  fireEvent.click(screen.getByRole("button", { name: "按可下载日期填写" }));
  expect(screen.getByLabelText("历史资料开始日期")).toHaveValue(filledStart);
  expect(screen.getByLabelText("历史资料结束日期")).toHaveValue(filledEnd);
  expect(screen.getByText(new RegExp(`原回测日期：${start} 至 ${end}`))).toBeVisible();
  expect(requests.every(request => request.method === "GET")).toBe(true);
});

it("keeps future-only dates when there is no overlap with the source range", async () => {
  render(<SecurityStatusDownloadPanel portfolioId={1} requestedRange={{ start: "2027-01-01", end: "2027-02-01" }} />);
  await screen.findByText("本次日期超出了来源可提供的范围");
  expect(screen.getByRole("button", { name: "按可下载日期填写" })).toBeDisabled();
  expect(screen.getByText(/所选日期目前没有可填写的下载范围/)).toBeVisible();
  expect(screen.getByLabelText("历史资料开始日期")).toHaveValue("2027-01-01");
  expect(screen.getByLabelText("历史资料结束日期")).toHaveValue("2027-02-01");
  expect(screen.getByRole("button", { name: "下载当前组合资料" })).toBeDisabled();
  expect(requests.every(request => request.method === "GET")).toBe(true);
});

it.each(["active", "unconfirmed"])("does not replace dates belonging to an %s download request", async state => {
  if (state === "unconfirmed") localStorage.setItem("security-status-download-request:1", JSON.stringify({
    portfolio_id: 1, request_id: "old-download", expected_scope_sha256: "1".repeat(64), start_date: "2025-10-20", end_date: "2026-10-19",
  }));
  handle = async url => {
    if (url.includes("/download-scope")) return { ...scope(), latest_task: state === "active" ? task("running") : null };
    if (state === "unconfirmed") throw new Error("unconfirmed request read failed");
    return { task: task("running"), report: null };
  };
  render(<SecurityStatusDownloadPanel portfolioId={1} requestedRange={{ start: "2023-01-01", end: "2026-10-22" }} />);
  await screen.findByText(state === "active" ? "正在下载历史股票状态…" : "下载范围或已有记录读取失败，请重新读取。");
  const fill = screen.getByRole("button", { name: "按可下载日期填写" });
  expect(fill).toBeDisabled(); fireEvent.click(fill);
  expect(screen.getByLabelText("历史资料开始日期")).toHaveValue("2023-01-01");
  expect(screen.getByLabelText("历史资料结束日期")).toHaveValue("2026-10-22");
  expect(requests.every(request => request.method === "GET")).toBe(true);
});

it("preserves an already valid requested download range without offering a replacement", async () => {
  render(<SecurityStatusDownloadPanel portfolioId={1} requestedRange={{ start: "2026-10-01", end: "2026-10-15" }} />);
  await screen.findByText(/长期组合.*2026-10-01 至 2026-10-15/);
  expect(screen.queryByRole("button", { name: "按可下载日期填写" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("历史资料开始日期")).toHaveValue("2026-10-01");
  expect(screen.getByLabelText("历史资料结束日期")).toHaveValue("2026-10-15");
  expect(requests.every(request => request.method === "GET")).toBe(true);
});

it("displays a scope failure and retries only the read", async () => {
  handle = async () => { throw new Error("read failed"); };
  render(<SecurityStatusDownloadPanel portfolioId={1} />);
  await screen.findByText("下载范围或已有记录读取失败，请重新读取。");
  expect(screen.getByRole("button", { name: "下载当前组合资料" })).toBeDisabled();
  expect(screen.queryByText(/0 个标的/)).not.toBeInTheDocument();
  handle = async () => scope();
  fireEvent.click(screen.getByRole("button", { name: "重新读取范围" }));
  await screen.findByText(/长期组合.*1 个标的/);
  expect(requests.every(r => r.method === "GET")).toBe(true);
});

it("recovers a failed progress read without posting another job", async () => {
  let readable = false;
  handle = async (url, method) => {
    if (url.includes("/download-scope")) return scope();
    if (method === "POST") return { task: task(), report: null };
    if (!readable) throw new Error("progress unavailable");
    return { task: task("done"), report };
  };
  render(<SecurityStatusDownloadPanel portfolioId={1} />);
  await screen.findByText(/长期组合.*1 个标的/);
  fireEvent.click(screen.getByRole("button", { name: "下载当前组合资料" }));
  await screen.findByText("下载进度读取失败，已取得的文件会保留，请查看下载结果。");
  readable = true;
  fireEvent.click(screen.getByRole("button", { name: "查看下载结果" }));
  await screen.findByText("下载完成，历史资料仍不完整。");
  expect(requests.filter(r => r.method === "POST")).toHaveLength(1);
});

it("restores the portfolio's previous report on entry without starting a download", async () => {
  handle = async url => url.includes("/download-scope") ? { ...scope(), latest_task: task("done") } : { task: task("done"), report };
  render(<SecurityStatusDownloadPanel portfolioId={1} />);
  await screen.findByText("下载完成，历史资料仍不完整。");
  expect(screen.getByRole("button", { name: "保存原始文件" })).toBeEnabled();
  expect(requests.every(r => r.method === "GET")).toBe(true);
});

it("uses daily Chinese for a task interrupted by an application restart", async () => {
  const interrupted = { ...task("failed"), stage: "interrupted", message: "Backend restarted before task completed" };
  handle = async url => url.includes("/download-scope") ? { ...scope(), latest_task: interrupted } : { task: interrupted, report: null };
  render(<SecurityStatusDownloadPanel portfolioId={1} />);
  expect(await screen.findByText("应用上次关闭时下载尚未完成，请重新下载。")).toBeVisible();
  expect(screen.queryByText("Backend restarted before task completed")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重新下载当前组合资料" })).toBeEnabled();
  expect(requests.every(r => r.method === "GET")).toBe(true);
});

it("does not turn a failed download into an empty history table or zero ST days", async () => {
  const failedReport = { ...report, valid_daily_rows: 0, source_error: "历史资料来源暂时无法连接，请稍后重试。",
    symbols: [{ ...report.symbols[0], valid_daily_rows: 0, st_days: 0, suspended_days: 0 }] };
  handle = async url => url.includes("/download-scope") ? { ...scope(), latest_task: task("failed") } : { task: task("failed"), report: failedReport };
  render(<SecurityStatusDownloadPanel portfolioId={1} />);
  expect(await screen.findByText("本次未取得可核对的逐日状态。请查看下方原因后重试，尚不能用于正式回测。")).toBeVisible();
  expect(screen.getByText("历史资料来源暂时无法连接，请稍后重试。")).toBeVisible();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重新下载当前组合资料" })).toBeEnabled();
});

it("keeps ETF status as awaiting verification rather than displaying source flags as ST days", async () => {
  const etfReport = { ...report, symbols: [...report.symbols, { symbol: "510300", display_name: "沪深300ETF", raw_daily_rows: 242,
    valid_daily_rows: 0, st_days: null, suspended_days: null, missing_trade_dates: null,
    errors: ["ETF的历史状态字段尚未核实，已保留原始文件，暂不按股票状态计算。"] }] };
  handle = async url => url.includes("/download-scope") ? { ...scope(), latest_task: task("partial") } : { task: task("partial"), report: etfReport };
  render(<SecurityStatusDownloadPanel portfolioId={1} />);
  const row = await screen.findByRole("row", { name: /510300/ });
  expect(within(row).getAllByText("待核对")).toHaveLength(2);
  expect(within(row).getByText("242")).toBeVisible();
  expect(screen.getByText("原始文件已保存，部分状态仍待核对。")).toBeVisible();
});
