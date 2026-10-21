import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import SecurityStatusImportPanel from "../SecurityStatusImportPanel";

const template = { csv_text: "symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date\n", required_columns: [], instructions: ["每一行只表示当日状态。"], max_bytes: 20000000, max_rows: 50000 };
const coverage = { total_records: 0, verified_records: 0, first_date: null, latest_date: null, coverage_ratio: null, calendar_available: null, sources: [], message: "尚无可用的历史股票状态资料。" };
const preview = { can_import: true, content_sha256: "a".repeat(64), batch_id: "file-a", total_rows: 1, insert_count: 1, replace_unknown_count: 0, unchanged_count: 0, execution_constraint_count: 0, start_date: "2026-09-01", end_date: "2026-09-01", symbols: 1, errors: [], sample: [{ symbol: "600000", trade_date: "2026-09-01", status: "LISTED" }], warnings: [] };
let requests: Array<{ url: string; method: string; body?: any }>;
let previewResponse: () => Promise<any>;
let importResponse: () => Promise<any>;

beforeEach(() => {
  requests = [];
  previewResponse = async () => preview;
  importResponse = async () => ({ inserted: 1, replaced_unknown: 0, unchanged: 0, message: "已导入 1 条历史状态记录。" });
  vi.stubGlobal("fetch", vi.fn(async (url: string, options: any = {}) => {
    requests.push({ url: String(url), method: options.method || "GET", body: options.body ? JSON.parse(options.body) : undefined });
    const value = String(url).endsWith("/template") ? template : String(url).endsWith("/coverage") ? coverage : String(url).endsWith("/preview") ? await previewResponse() : await importResponse();
    return { ok: true, json: async () => value };
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function fillFile() {
  fireEvent.change(screen.getByLabelText("历史股票状态文件"), { target: { files: [new File([template.csv_text + "600000,2026-09-01,0,0,0,1,1999-11-10,\n"], "history.csv", { type: "text/csv" })] } });
  fireEvent.change(await screen.findByLabelText("资料来源"), { target: { value: "交易所历史文件" } });
  fireEvent.change(screen.getByLabelText("资料版本或文件批次"), { target: { value: "2026-09" } });
  fireEvent.change(screen.getByLabelText("资料注明的更新时间（北京时间）"), { target: { value: "2026-09-02T08:00" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "检查文件" })).toBeEnabled());
}

it("shows missing history honestly and only reads local coverage on entry", async () => {
  render(<SecurityStatusImportPanel />);
  expect(await screen.findByText("尚无可用的历史股票状态资料。")).toBeVisible();
  expect(screen.getByRole("button", { name: "选择CSV文件" })).toBeEnabled();
  expect(screen.queryByText(/覆盖率.*100/)).not.toBeInTheDocument();
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});

it("requires a reviewed file and explicit confirmation before importing", async () => {
  render(<SecurityStatusImportPanel />);
  await screen.findByText(coverage.message);
  await fillFile();
  fireEvent.click(screen.getByRole("button", { name: "检查文件" }));
  expect(await screen.findByText(/文件检查通过/)).toBeVisible();
  expect(screen.getByText("其中 0 条提供了当日限价资料。其余记录只保存股票状态，不会估算涨跌停价格。")).toBeVisible();
  expect(screen.getByRole("button", { name: "导入记录" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "我已核对资料来源和历史日期" }));
  fireEvent.click(screen.getByRole("button", { name: "导入记录" }));
  expect(await screen.findByText("已导入 1 条历史状态记录。")).toBeVisible();
  const write = requests.find((r) => r.url.endsWith("/import"));
  expect(write?.body).toMatchObject({ filename: "history.csv", source_reference: "history.csv", source_version: "2026-09", source_updated_at: "2026-09-02T00:00:00.000Z", confirmed: true, preview_sha256: "a".repeat(64) });
  expect(requests.filter((r) => r.url.endsWith("/import"))).toHaveLength(1);
});

it("invalidates a reviewed result when its source changes", async () => {
  render(<SecurityStatusImportPanel />);
  await screen.findByText(coverage.message);
  await fillFile();
  fireEvent.click(screen.getByRole("button", { name: "检查文件" }));
  await screen.findByText(/文件检查通过/);
  fireEvent.click(screen.getByRole("checkbox", { name: "我已核对资料来源和历史日期" }));
  fireEvent.change(screen.getByLabelText("资料版本或文件批次"), { target: { value: "corrected" } });
  expect(screen.queryByRole("button", { name: "导入记录" })).not.toBeInTheDocument();
  expect(requests.some((r) => r.url.endsWith("/import"))).toBe(false);
});

it("ignores a file check that finishes after the source was edited", async () => {
  let finish!: (value: unknown) => void;
  previewResponse = () => new Promise((resolve) => { finish = resolve; });
  render(<SecurityStatusImportPanel />);
  await screen.findByText(coverage.message);
  await fillFile();
  fireEvent.click(screen.getByRole("button", { name: "检查文件" }));
  await waitFor(() => expect(requests.some((r) => r.url.endsWith("/preview"))).toBe(true));
  fireEvent.change(screen.getByLabelText("资料来源"), { target: { value: "另一份原始资料" } });
  await act(async () => finish(preview));
  expect(screen.queryByText(/文件检查通过/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "导入记录" })).not.toBeInTheDocument();
  expect(requests.some((r) => r.url.endsWith("/import"))).toBe(false);
});

it("keeps an invalid file out of the source form so it can be replaced directly", async () => {
  render(<SecurityStatusImportPanel />);
  await screen.findByText(coverage.message);
  fireEvent.change(screen.getByLabelText("历史股票状态文件"), { target: { files: [new File(["bad"], "history.txt")] } });
  expect(await screen.findByText("请选择CSV文件。可以先下载表头，核对文件格式。")).toBeVisible();
  expect(screen.queryByLabelText("资料来源")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "选择CSV文件" })).toBeEnabled();
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});

it("shows row errors and never offers to import a rejected file", async () => {
  previewResponse = async () => ({ ...preview, can_import: false, errors: [{ line: 2, message: "上市日期缺失。" }] });
  render(<SecurityStatusImportPanel />);
  await screen.findByText(coverage.message);
  await fillFile();
  fireEvent.click(screen.getByRole("button", { name: "检查文件" }));
  expect(await screen.findByText("第 2 行：上市日期缺失。")).toBeVisible();
  expect(screen.queryByRole("button", { name: "导入记录" })).not.toBeInTheDocument();
  expect(requests.some((r) => r.url.endsWith("/import"))).toBe(false);
});

it("preserves a checked file after a failed import without retrying the write automatically", async () => {
  importResponse = async () => { throw new Error("连接已断开"); };
  render(<SecurityStatusImportPanel />);
  await screen.findByText(coverage.message);
  await fillFile();
  fireEvent.click(screen.getByRole("button", { name: "检查文件" }));
  await screen.findByText(/文件检查通过/);
  fireEvent.click(screen.getByRole("checkbox", { name: "我已核对资料来源和历史日期" }));
  fireEvent.click(screen.getByRole("button", { name: "导入记录" }));
  expect(await screen.findByText(/暂时无法确认导入结果：连接已断开/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "重新读取状态" }));
  await waitFor(() => expect(requests.filter((r) => r.url.endsWith("/coverage"))).toHaveLength(2));
  expect(requests.filter((r) => r.url.endsWith("/import"))).toHaveLength(1);
  importResponse = async () => ({ inserted: 0, unchanged: 1, message: "已有相同记录，无需重复导入。" });
  fireEvent.click(screen.getByRole("button", { name: "导入记录" }));
  expect(await screen.findByText("已有相同记录，无需重复导入。")).toBeVisible();
  expect(requests.filter((r) => r.url.endsWith("/import"))).toHaveLength(2);
});
