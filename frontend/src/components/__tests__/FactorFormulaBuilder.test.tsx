import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n", () => ({
  t: (key: string) => key,
}));

import FactorFormulaBuilder, {
  FACTOR_FORMULA_FIELDS,
  FACTOR_FORMULA_FUNCTIONS,
} from "../factors/FactorFormulaBuilder";

describe("FactorFormulaBuilder", () => {
  const onInsert = vi.fn();
  const onUseExample = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the complete factor DSL field and function catalogs", () => {
    render(
      <FactorFormulaBuilder isZh onInsert={onInsert} onUseExample={onUseExample} />,
    );

    expect(FACTOR_FORMULA_FIELDS).toHaveLength(15);
    expect(FACTOR_FORMULA_FUNCTIONS).toHaveLength(17);
    expect(screen.getByRole("button", { name: "close 收盘价" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "pe_ttm 市盈率 TTM" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "factorFormulaBuilderFunctions (17)" }));
    expect(screen.getByRole("button", { name: "sma 简单移动平均" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "pct_change 区间涨跌幅" })).toBeInTheDocument();
  });

  it("inserts fields and valid function snippets", () => {
    render(
      <FactorFormulaBuilder isZh onInsert={onInsert} onUseExample={onUseExample} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "close 收盘价" }));
    expect(onInsert).toHaveBeenLastCalledWith("close");

    fireEvent.click(screen.getByRole("tab", { name: "factorFormulaBuilderFunctions (17)" }));
    fireEvent.click(screen.getByRole("button", { name: "sma 简单移动平均" }));
    expect(onInsert).toHaveBeenLastCalledWith("sma(close, 20)");
  });

  it("applies a complete example formula", () => {
    render(
      <FactorFormulaBuilder isZh onInsert={onInsert} onUseExample={onUseExample} />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "factorFormulaBuilderExamples" }));
    const applyButtons = screen.getAllByRole("button", { name: "factorFormulaBuilderUse" });
    expect(applyButtons).toHaveLength(6);
    fireEvent.click(applyButtons[1]);

    expect(onUseExample).toHaveBeenCalledWith("pct_change(close, 20)");
  });
});
