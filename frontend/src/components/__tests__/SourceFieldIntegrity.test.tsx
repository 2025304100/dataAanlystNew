import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import SourceFieldIntegrity from "../SourceFieldIntegrity";

const mocks = vi.hoisted(() => ({ getSourceFieldIntegrity: vi.fn() }));
vi.mock("../../api/client", () => ({ api: mocks }));
afterEach(() => { cleanup(); mocks.getSourceFieldIntegrity.mockReset(); });

it("shows the selected audit date, independent source dates and stale-cache warning", async () => {
  mocks.getSourceFieldIntegrity.mockResolvedValue({ as_of: "2026-10-22", timezone: "Asia/Shanghai",
    expected_latest_date: "2026-10-22", sources: [{ table: "universe_daily_bars", label: "境内 ETF 行情",
      latest_date: "2026-10-23", latest_date_as_of: "2026-10-21", cache_latest_date: "2026-10-01",
      warnings: ["cache_date_mismatch", "source_behind_calendar"] }], fields: [] });
  render(<SourceFieldIntegrity />);
  fireEvent.change(screen.getByLabelText("验收截止日期（可选）"), { target: { value: "2026-10-22" } });
  fireEvent.click(screen.getByRole("button", { name: "检查源表关键字段" }));
  expect(mocks.getSourceFieldIntegrity).toHaveBeenCalledWith("2026-10-22");
  expect(await screen.findByText(/验收截止：2026-10-22/)).toBeVisible();
  expect(screen.getByText(/源表最新日期 2026-10-23；截止日内最新日期 2026-10-21/)).toBeVisible();
  expect(screen.getByText(/登记缓存与源表最新日期不一致/)).toBeVisible();
  expect(screen.getByText(/源表最新日期落后于已确认交易日/)).toBeVisible();
});

it("checks source fields explicitly and reports missing values without marking an empty table complete", async () => {
  mocks.getSourceFieldIntegrity.mockResolvedValue({ as_of: "2026-10-23", fields: [
    { table: "stock_financial_reports", field: "net_profit", total: 100, nonnull: 2, missing: 98,
      coverage_pct: 2, start_date: null, end_date: null, note: "全部已落库财报" },
    { table: "universe_daily_bars", field: "turnover_rate", total: 0, nonnull: 0, missing: 0,
      coverage_pct: null, start_date: "2026-09-24", end_date: "2026-10-23", note: "ETF" },
  ] });
  render(<SourceFieldIntegrity />);
  expect(mocks.getSourceFieldIntegrity).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "检查源表关键字段" }));
  expect(await screen.findByText("stock_financial_reports.net_profit")).toBeVisible();
  expect(screen.getByText("98")).toBeVisible();
  expect(screen.getByText("无记录，无法判断")).toBeVisible();
  mocks.getSourceFieldIntegrity.mockRejectedValueOnce(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "检查源表关键字段" }));
  expect(await screen.findByText("源表检查未更新，已有结果仅供参考")).toBeVisible();
  expect(screen.getByText("98")).toBeVisible();
});
