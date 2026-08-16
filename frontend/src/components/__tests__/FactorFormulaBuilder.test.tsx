import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { FactorFormulaCatalog } from "../../api/client";

vi.mock("../../i18n", () => ({
  t: (key: string) => key,
}));

import FactorFormulaBuilder from "../factors/FactorFormulaBuilder";

const catalog: FactorFormulaCatalog = {
  dsl_version: "2.0",
  compiler_version: "wp2-1.0.0",
  fields: [{
    key: "close", label_zh: "收盘价", label_en: "Close", description: "当日收盘价", dtype: "float",
    source_table: "raw_daily_bars", layer: "A", point_in_time: false, snippet: "close", enabled: true,
    availability: "available", data_mode: "continuous", evaluation_enabled: true, preview_enabled: true,
    draft_enabled: true, status_reason: "字段有真实数据覆盖。", table_rows: 100, nonnull_rows: 100,
    distinct_symbols: 10, distinct_dates: 10, first_date: "2026-08-01", latest_date: "2026-08-14",
    derived: false, derived_from: [],
  }],
  functions: [{
    key: "sma", label_zh: "简单移动平均", label_en: "Simple Moving Average", description: "滚动均值", category: "rolling",
    signature: "sma(series, window)", snippet: "sma(close, 20)", params: ["series", "window"], min_args: 2, max_args: 2,
    window_arg_index: 1, enabled: true,
  }],
  disabled_functions: [{
    key: "rank_cs", label_zh: "截面排名", label_en: "Cross-section Rank", category: "cross_section",
    signature: "rank_cs(value)", snippet: "rank_cs(close)", enabled: false, disabled_reason: "Use post-processing instead",
  }],
  operators: [{ key: "and", label: "并且", snippet: " && ", description: "两个条件同时成立" }],
  templates: [{ key: "momentum", name_zh: "20 日动量", name_en: "20-day Momentum", formula: "pct_change(close, 20)" }],
};

describe("FactorFormulaBuilder", () => {
  const onInsert = vi.fn();
  const onUseExample = vi.fn();
  const onInspect = vi.fn();

  beforeEach(() => vi.clearAllMocks());

  it("renders the executable catalog supplied by the backend contract", () => {
    render(<FactorFormulaBuilder isZh catalog={catalog} onInsert={onInsert} onUseExample={onUseExample} onInspect={onInspect} />);

    expect(screen.getByRole("button", { name: "close 收盘价" })).toBeInTheDocument();
    expect(screen.getByText("可评价")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "函数 (2)" }));
    expect(screen.getByRole("button", { name: "sma 简单移动平均" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "rank_cs 截面排名" })).toHaveAttribute("aria-disabled", "true");
  });

  it("inserts only enabled field and function snippets at the caller cursor", () => {
    render(<FactorFormulaBuilder isZh catalog={catalog} onInsert={onInsert} onUseExample={onUseExample} onInspect={onInspect} />);

    fireEvent.click(screen.getByRole("button", { name: "close 收盘价" }));
    expect(onInsert).toHaveBeenLastCalledWith("close");

    fireEvent.click(screen.getByRole("tab", { name: "函数 (2)" }));
    fireEvent.click(screen.getByRole("button", { name: "sma 简单移动平均" }));
    expect(onInsert).toHaveBeenLastCalledWith("sma(close, 20)");

    fireEvent.click(screen.getByRole("button", { name: "rank_cs 截面排名" }));
    expect(onInsert).toHaveBeenCalledTimes(2);
  });

  it("surfaces templates and inspector context without creating a separate catalog", () => {
    render(<FactorFormulaBuilder isZh catalog={catalog} onInsert={onInsert} onUseExample={onUseExample} onInspect={onInspect} />);

    fireEvent.click(screen.getByRole("tab", { name: "模板" }));
    fireEvent.click(screen.getByRole("button", { name: "使用模板" }));
    expect(onUseExample).toHaveBeenCalledWith("pct_change(close, 20)");
  });
});
