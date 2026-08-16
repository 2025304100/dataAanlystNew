import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { TextAreaRef } from "antd/es/input/TextArea";
import type { FactorFormulaCatalog } from "../../api/client";

vi.mock("../../i18n", () => ({
  t: (key: string) => key,
}));

import FactorFormulaEditorModal from "../factors/FactorFormulaEditorModal";

const catalog: FactorFormulaCatalog = {
  dsl_version: "2.0", compiler_version: "wp2-1.0.0",
  fields: [{ key: "close", label_zh: "收盘价", label_en: "Close", description: "当日收盘价", dtype: "float", source_table: "raw_daily_bars", layer: "A", point_in_time: false, snippet: "close", enabled: true, availability: "available", data_mode: "continuous", evaluation_enabled: true, preview_enabled: true, draft_enabled: true, status_reason: "字段有真实数据覆盖。", table_rows: 100, nonnull_rows: 100, distinct_symbols: 10, distinct_dates: 10, first_date: "2026-08-01", latest_date: "2026-08-14", derived: false, derived_from: [] }],
  functions: [], disabled_functions: [], operators: [], templates: [],
};

function renderModal(overrides: Record<string, unknown> = {}) {
  const props = {
    open: true, isZh: true, factorCode: "turnover_z20", formulaExpr: "sma(turnover_rate, 20)",
    formulaTextAreaRef: createRef<TextAreaRef>(), versionDirection: "higher_better" as const, changeNote: "", paramsText: "{}",
    catalog, catalogLoading: false, catalogError: null,
    validateResult: null, previewResult: null, validating: false, previewing: false,
    validationState: "idle" as const, previewCount: null, directionOptions: [{ value: "higher_better" as const, label: "Higher" }],
    onCancel: vi.fn(), onApply: vi.fn(), onAskAi: vi.fn(), onValidate: vi.fn(), onPreview: vi.fn(), onInsert: vi.fn(),
    onUseExample: vi.fn(), onRetryCatalog: vi.fn(), onFormulaChange: vi.fn(), onRememberSelection: vi.fn(),
    onDirectionChange: vi.fn(), onChangeNoteChange: vi.fn(), onParamsTextChange: vi.fn(), ...overrides,
  };
  render(<FactorFormulaEditorModal {...props} />);
  return props;
}

describe("FactorFormulaEditorModal", () => {
  it("keeps the catalog, editor, and contextual help in the existing modal", () => {
    renderModal();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("公式能力库")).toBeInTheDocument();
    expect(screen.getByText("上下文帮助")).toBeInTheDocument();
    expect(screen.getByDisplayValue("sma(turnover_rate, 20)")).toBeInTheDocument();
  });

  it("forwards catalog inserts and existing action callbacks", () => {
    const props = renderModal();
    fireEvent.click(screen.getByRole("button", { name: "close 收盘价" }));
    expect(props.onInsert).toHaveBeenCalledWith("close");
    fireEvent.click(screen.getByTestId("factor-modal-ask-ai"));
    expect(props.onAskAi).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate/ }));
    expect(props.onValidate).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /factorEditorPreview/ }));
    expect(props.onPreview).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "factorFormulaModalApply" }));
    expect(props.onApply).toHaveBeenCalledTimes(1);
  });

  it("renders actual compiler diagnostics and preview metrics", () => {
    renderModal({
      validationState: "invalid",
      validateResult: { is_valid: false, execution_plan: null, data_dependencies: null, errors: [{ error_code: "field_not_in_catalog", message: "Unknown field", detail: {}, start: 0, end: 4, line: 1, column: 1, token: "oops" }] },
      previewResult: { is_valid: true, execution_plan: null, errors: [], data_cutoff_at: null, selected_trade_date: "2026-08-14", complete_trade_day_evidence: null, data_readiness: null, data_dependencies: { fields: ["close"], functions: ["sma"], max_lookback: 20 }, values: [], missing_reasons: {}, attempted_count: 20, valid_count: 18, missing_count: 2, coverage_rate: 0.9, missing_rate: 0.1, distribution: {}, outlier_count: 1, elapsed_ms: 21, data_fix_links: [], evaluation_supported: true, evaluation_mode: "continuous", blocking_fields: [], readiness_warnings: [] },
    });
    expect(screen.getByText("field_not_in_catalog")).toBeInTheDocument();
    expect(screen.getByText("预览摘要")).toBeInTheDocument();
    expect(screen.getByText("18/20")).toBeInTheDocument();
  });
});
