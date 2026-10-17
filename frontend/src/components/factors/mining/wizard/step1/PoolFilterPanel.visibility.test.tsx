import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";
import { setLocale } from "../../../../../i18n";

vi.mock("./poolApi", () => ({
  fetchFilterPresets: vi.fn(async () => ({ groups: [
    { group: "market_cap", label_zh: "市值" }, { group: "listing", label_zh: "上市时间" },
  ], presets: [
    { preset_code: "cap_large", group: "market_cap", group_label_zh: "市值", label_zh: "大盘", field: "total_market_cap", operator: "ge", min_value: 500, max_value: null, applyable: true },
    { preset_code: "listing_future", group: "listing", group_label_zh: "上市时间", label_zh: "上市满一年", field: "min_listed_trading_days", operator: "ge", min_value: 250, max_value: null, applyable: false, blocked_reason_zh: "上市日期数据暂缺" },
  ] })),
  fetchFilterFields: vi.fn(async () => ({ min_pool_size: 50, fields: [
    { field: "total_market_cap", label_zh: "总市值", category: "valuation", availability: "available" },
    { field: "dividend_yield", label_zh: "股息率", category: "valuation", availability: "blocked", blocked_reason_zh: "股息数据暂缺" },
    { field: "avg_amount", label_zh: "日均成交额", category: "liquidity", availability: "available", data_mode: "blocked", blocked_reason_zh: "历史成交额暂缺" },
    { field: "exclude_st", label_zh: "排除 ST", category: "risk", availability: "blocked", blocked_reason_zh: "状态数据暂缺" },
    { field: "future_metric", label_zh: "未来指标", category: "experimental", availability: "available" },
  ], categories: [
    { category: "market_status", label_zh: "市场与交易状态" },
    { category: "valuation", label_zh: "估值与市值" },
    { category: "liquidity", label_zh: "流动性" },
    { category: "risk", label_zh: "风险标记" },
    { category: "experimental", label_zh: "实验指标" },
  ] })),
}));
import { fetchFilterFields, fetchFilterPresets } from "./poolApi";
import PoolFilterPanel from "./PoolFilterPanel";

const showToggle = () => document.querySelector("[data-filter-show-unsupported]") as HTMLElement;
function expand(label: string) {
  const header = Array.from(document.querySelectorAll(".ant-collapse-header")).find((el) => el.textContent?.includes(label));
  if (header && header.getAttribute("aria-expanded") !== "true") fireEvent.click(header);
}
async function ready() {
  await waitFor(() => expect(document.querySelector('[data-filter-preset="cap_large"]')).toBeTruthy());
}
beforeEach(() => { localStorage.clear(); setLocale("zh-CN"); vi.clearAllMocks(); });

describe("候选池不可用功能展示偏好", () => {
  it("默认隐藏不可用字段、未实现字段以及空分类与空预设组", async () => {
    render(<PoolFilterPanel locked={false} value={{}} onChange={vi.fn()} />);
    await ready();
    expand("估值与市值");
    expect(document.querySelector('[data-filter-range="total_market_cap"]')).toBeTruthy();
    expect(document.querySelector('[data-filter-range="dividend_yield"]')).toBeNull();
    expect(document.querySelector('[data-filter-preset="listing_future"]')).toBeNull();
    const text = document.querySelector("[data-pool-filter-panel]")?.textContent ?? "";
    expect(text).not.toContain("实验指标");
    expect(text).not.toContain("流动性");
    expect(text).not.toContain("上市时间");
  });

  it("开关仅改展示且会记住偏好，显示后仍禁用并给原因，未知字段不冒充可用", async () => {
    const onChange = vi.fn();
    const first = render(<PoolFilterPanel locked={false} value={{}} onChange={onChange} />);
    await ready();
    fireEvent.click(showToggle());
    expand("估值与市值"); expand("风险标记"); expand("实验指标"); expand("流动性");
    expect((document.querySelector('[data-filter-range="dividend_yield"] input') as HTMLInputElement).disabled).toBe(true);
    expect((document.querySelector('[data-filter-bool="exclude_st"] input') as HTMLInputElement).disabled).toBe(true);
    expect((document.querySelector('[data-filter-preset="listing_future"]') as HTMLButtonElement).disabled).toBe(true);
    expect(document.querySelector('[data-filter-blocked="future_metric"]')?.textContent).toContain("暂不可用");
    expect(document.querySelector('[data-filter-range="avg_amount"]')?.textContent).toContain("历史成交额暂缺");
    expect(onChange).not.toHaveBeenCalled();
    expect(fetchFilterFields).toHaveBeenCalledTimes(1);
    expect(fetchFilterPresets).toHaveBeenCalledTimes(1);
    first.unmount();
    render(<PoolFilterPanel locked={false} value={{}} onChange={onChange} />);
    await ready();
    expect(showToggle().getAttribute("aria-checked")).toBe("true");
  });

  it("锁定时可调整展示但业务控件仍锁定，已保存不可用条件不被清除", async () => {
    const onChange = vi.fn();
    const value = { valuation: { dividend_yield: { min: 2 } }, future_condition: { enabled: true } };
    render(<PoolFilterPanel locked value={value} onChange={onChange} />);
    await ready();
    expect(document.querySelector("[data-filter-hidden-selected]")?.textContent).toContain("保留");
    fireEvent.click(document.querySelector("[data-filter-hidden-selected] button") as HTMLElement);
    expect(document.querySelector("[data-filter-saved-details]")?.textContent).toContain("future_condition");
    expect(showToggle().getAttribute("aria-checked")).toBe("true");
    fireEvent.click(showToggle());
    expand("估值与市值");
    expect((document.querySelector('[data-filter-range="total_market_cap"] input') as HTMLInputElement).disabled).toBe(true);
    expect((document.querySelector('[data-filter-range="dividend_yield"] input') as HTMLInputElement).value).toBe("2");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("清除分类通过完整配置真正删除该分类，而不是合并回旧条件", async () => {
    const onChange = vi.fn();
    render(<PoolFilterPanel locked={false} value={{ markets: ["sh"], valuation: { total_market_cap: { min: 10 } } }} onChange={onChange} />);
    await ready();
    fireEvent.click(document.querySelector('[data-filter-clear="valuation"]') as HTMLElement);
    expect(onChange).toHaveBeenCalledWith({ markets: ["sh"] });
  });

  it("真实支持的流动性窗口参数不误报为不可用条件", async () => {
    vi.mocked(fetchFilterFields).mockResolvedValueOnce({ min_pool_size: 50, fields: [
      { field: "avg_amount", label_zh: "日均成交额", category: "liquidity", availability: "available" },
    ], categories: [{ category: "liquidity", label_zh: "流动性" }] });
    const onChange = vi.fn();
    render(<PoolFilterPanel locked={false} value={{ liquidity: { window_days: 20, avg_amount: { min: 1 } } }} onChange={onChange} />);
    await ready();
    expect(document.querySelector("[data-filter-hidden-selected]")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("旧配置顶层不可用条件仍可见，清本分类后真实删除并保留其他分类", async () => {
    vi.mocked(fetchFilterFields).mockResolvedValueOnce({ min_pool_size: 50, fields: [
      { field: "exclude_suspended", label_zh: "排除停牌", category: "market_status", availability: "blocked", blocked_reason_zh: "停牌状态暂缺" },
    ], categories: [{ category: "market_status", label_zh: "市场与交易状态" }] });
    const onChange = vi.fn();
    render(<PoolFilterPanel locked={false} value={{ markets: ["sh"], exclude_suspended: true, valuation: { total_market_cap: { min: 10 } } }} onChange={onChange} />);
    await ready();
    expect(document.querySelector('[data-filter-blocked="exclude_suspended"]')?.textContent).toContain("已选但不可用");
    expect(document.querySelector("[data-filter-hidden-selected]")).toBeTruthy();
    fireEvent.click(document.querySelector('[data-filter-clear="market_status"]') as HTMLElement);
    expect(onChange).toHaveBeenCalledWith({ valuation: { total_market_cap: { min: 10 } } });
  });

  it.each([
    { category: "market_status", value: { markets: ["sh"], boards: ["sh_main"], exclude_delisting: true, valuation: { total_market_cap: { min: 10 } } } },
    { category: "risk", value: { exclude_st: true, valuation: { total_market_cap: { min: 10 } } } },
  ])("清除 $category 真正移除市场、板块或风险条件并保留其他分类", async ({ category, value }) => {
    const onChange = vi.fn();
    render(<PoolFilterPanel locked={false} value={value} onChange={onChange} />);
    await ready();
    fireEvent.click(document.querySelector(`[data-filter-clear="${category}"]`) as HTMLElement);
    expect(onChange).toHaveBeenCalledWith({ valuation: { total_market_cap: { min: 10 } } });
  });
});
