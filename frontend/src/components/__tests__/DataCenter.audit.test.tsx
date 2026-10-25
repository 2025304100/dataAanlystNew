import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../ExternalDataSync", () => ({ default: () => <div data-testid="input-content">factor input content</div> }));
vi.mock("../TaskCenter", () => ({ default: () => <div>task content</div> }));
vi.mock("../UniverseDataPanel", () => ({ default: () => <div>market content</div> }));
vi.mock("../factors/mining/config/DataMirrorPage", () => ({ default: () => <div>mirror content</div> }));
const mockApi = vi.hoisted(() => ({
  getExternalDataCoverage: vi.fn(async () => ({ datasets: [] as any[] })),
  getExternalDataQualitySnapshots: vi.fn(async () => ({ fields: [] })),
  refreshExternalDataQualitySnapshots: vi.fn(async () => ({ fields: 0 })),
  getExternalDataQualityHistory: vi.fn(),
  getSecurityStatusCoverage: vi.fn(async () => ({ total_records: 0, verified_records: 0, message: "尚无可用的历史股票状态资料。" })),
  getSecurityStatusTemplate: vi.fn(async () => ({ csv_text: "symbol,trade_date\n", required_columns: [], instructions: [], max_bytes: 20000000, max_rows: 50000 })),
}));
vi.mock("../../api/client", () => ({ api: mockApi }));

import DataCenter from "../DataCenter";
import { enterReadOnlyMode } from "../../test/acceptance";

describe("data-center factor-input entry and tab persistence", () => {
  it("disables a persisted recheck in read-only mode while leaving read-only checks available", async () => {
    const restore = await enterReadOnlyMode();
    localStorage.setItem("settings_data_center_tab", "coverage");
    const view = render(<DataCenter />);
    try {
      const button = await screen.findByRole("button", { name: "重新检查本地数据" });
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute("title", expect.stringContaining("只读验收"));
      fireEvent.click(button);
      expect(mockApi.refreshExternalDataQualitySnapshots).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "检查源表关键字段" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "重新读取状态" })).toBeEnabled();
    } finally { view.unmount(); await restore(); }
  });
  beforeEach(() => {
    window.localStorage.clear();
    mockApi.getExternalDataCoverage.mockReset().mockResolvedValue({ datasets: [] });
    mockApi.getExternalDataQualitySnapshots.mockReset().mockResolvedValue({ fields: [] });
    mockApi.refreshExternalDataQualitySnapshots.mockReset().mockResolvedValue({ fields: 0 });
  });
  afterEach(() => { cleanup(); window.localStorage.clear(); });

  it("exposes all five data-center tabs and reaches the input component", async () => {
    render(<DataCenter />);
    expect(screen.getAllByRole("tab")).toHaveLength(5);
    expect(screen.getByRole("tab", { name: "行情数据" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("平时在“行情数据”更新价格；财报、资金流等资料在“其他数据”更新。遇到问题时，可查看“更新记录”。")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "数据检查" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "更新记录" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "本地副本" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "其他数据" }));
    expect(await screen.findByTestId("input-content")).toBeVisible();
    await waitFor(() => expect(window.localStorage.getItem("settings_data_center_tab")).toBe("inputs"));
  });

  it("restores the input tab from the previous visit", async () => {
    window.localStorage.setItem("settings_data_center_tab", "inputs");
    render(<DataCenter />);
    expect(screen.getByRole("tab", { name: "其他数据" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByTestId("input-content")).toBeVisible();
  });

  it("reaches historical file preparation from the existing data-check tab", async () => {
    render(<DataCenter />);
    fireEvent.click(screen.getByRole("tab", { name: "数据检查" }));
    expect(await screen.findByRole("region", { name: "历史股票状态" })).toBeVisible();
    expect(await screen.findByRole("button", { name: "选择CSV文件" })).toBeEnabled();
    expect(window.localStorage.getItem("settings_data_center_tab")).toBe("coverage");
  });

  it("falls back to the market tab for an invalid persisted tab", () => {
    window.localStorage.setItem("settings_data_center_tab", "untrusted-invalid-tab");
    render(<DataCenter />);
    expect(screen.getByRole("tab", { name: "行情数据" })).toHaveAttribute("aria-selected", "true");
    expect(window.localStorage.getItem("settings_data_center_tab")).toBe("market");
  });

  it("recovers an initial coverage read failure through a read-only retry", async () => {
    mockApi.getExternalDataCoverage.mockRejectedValue(new Error("warehouse busy"));
    window.localStorage.setItem("settings_data_center_tab", "coverage");
    render(<DataCenter />);
    expect(await screen.findByText("warehouse busy")).toBeInTheDocument();
    mockApi.getExternalDataCoverage.mockResolvedValue({ datasets: [] });
    fireEvent.click(screen.getByRole("button", { name: "重试读取" }));
    await waitFor(() => expect(screen.queryByText("warehouse busy")).not.toBeInTheDocument());
    expect(mockApi.refreshExternalDataQualitySnapshots).not.toHaveBeenCalled();
  });

  it("keeps real coverage visible when only quality snapshot loading fails", async () => {
    mockApi.getExternalDataCoverage.mockResolvedValue({ datasets: [{ dataset: "fundamental", fields: [{ field: "pe_ttm", availability: "available", first_date: "2026-07-01", latest_date: "2026-08-02", table_rows: 10, nonnull_rows: 10, continuity_days: 2, evaluation_enabled: true }] }] });
    mockApi.getExternalDataQualitySnapshots.mockRejectedValue(new Error("quality snapshot busy"));
    window.localStorage.setItem("settings_data_center_tab", "coverage");
    render(<DataCenter />);
    expect(await screen.findByText("市盈率（PE-TTM）")).toBeInTheDocument();
    expect(screen.getByText("quality snapshot busy")).toBeInTheDocument();
  });

  it("clears a stale quality error after an explicit successful refresh", async () => {
    window.localStorage.setItem("settings_data_center_tab", "coverage");
    render(<DataCenter />);
    const button = await screen.findByRole("button", { name: "重新检查本地数据" });
    mockApi.refreshExternalDataQualitySnapshots.mockRejectedValueOnce(new Error("temporary quality error"));
    fireEvent.click(button);
    expect(await screen.findByText("temporary quality error")).toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() => expect(screen.queryByText("temporary quality error")).not.toBeInTheDocument());
  });

  it("excludes retired tail proxy rows returned by an older coverage cache", async () => {
    mockApi.getExternalDataCoverage.mockResolvedValue({ datasets: [
      { dataset: "fundamental", fields: [{ field: "pe_ttm", table_rows: 1, nonnull_rows: 1 }] },
      { dataset: "tail_proxy", fields: [{ field: "proxy_score", table_rows: 1, nonnull_rows: 1 }] },
    ] });
    localStorage.setItem("settings_data_center_tab", "coverage");
    render(<DataCenter />);
    expect(await screen.findByText("市盈率（PE-TTM）")).toBeInTheDocument();
    expect(screen.queryByText("候选尾盘代理")).not.toBeInTheDocument();
    expect(screen.queryByText("尾盘代理分数")).not.toBeInTheDocument();
  });

  it("keeps a long failure reason out of the table and shows it after clicking the detail button", async () => {
    mockApi.getExternalDataCoverage.mockResolvedValue({ datasets: [{ dataset: "financial", fields: [{ field: "roe_ttm", table_rows: 1, nonnull_rows: 1 }] }] });
    mockApi.getExternalDataQualitySnapshots.mockResolvedValue({ fields: [{ field: "roe_ttm", captured_at: "2026-10-19T06:49:16Z", failure_reason: "'NoneType' object is not subscriptable" }] } as any);
    localStorage.setItem("settings_data_center_tab", "coverage");
    render(<DataCenter />);
    const trigger = await screen.findByRole("button", { name: "最近失败 · 查看原因" });
    expect(screen.queryByText("'NoneType' object is not subscriptable")).not.toBeInTheDocument();
    fireEvent.click(trigger);
    expect(await screen.findByText("'NoneType' object is not subscriptable")).toBeInTheDocument();
  });

  it("does not reopen a dismissed quality history when its request completes", async () => {
    let resolveHistory!: (value: unknown) => void;
    mockApi.getExternalDataQualityHistory.mockImplementation(() => new Promise((resolve) => { resolveHistory = resolve; }));
    mockApi.getExternalDataCoverage.mockResolvedValue({ datasets: [{ dataset: "fundamental", fields: [{ field: "pe_ttm", table_rows: 1, nonnull_rows: 1 }] }] });
    mockApi.getExternalDataQualitySnapshots.mockResolvedValue({ fields: [{ field: "pe_ttm" }] } as any);
    localStorage.setItem("settings_data_center_tab", "coverage");
    render(<DataCenter />);
    fireEvent.click(await screen.findByRole("button", { name: "查看趋势" }));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible());
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await act(async () => resolveHistory({ field: "pe_ttm", snapshots: [] }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
