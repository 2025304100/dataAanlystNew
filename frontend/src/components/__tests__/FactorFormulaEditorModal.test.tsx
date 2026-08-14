import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { TextAreaRef } from "antd/es/input/TextArea";

vi.mock("../../i18n", () => ({
  t: (key: string) => key,
}));

import FactorFormulaEditorModal from "../factors/FactorFormulaEditorModal";

function renderModal(overrides: Record<string, unknown> = {}) {
  const props = {
    open: true,
    isZh: true,
    factorCode: "turnover_z20",
    formulaExpr: "sma(turnover_rate, 20)",
    formulaTextAreaRef: createRef<TextAreaRef>(),
    versionDirection: "higher_better" as const,
    changeNote: "",
    paramsText: "{}",
    templates: [{ key: "ep", name: "EP", formula: "1 / pe_ttm", category: "valuation" }],
    validating: false,
    previewing: false,
    validationState: "idle" as const,
    previewCount: null,
    directionOptions: [{ value: "higher_better" as const, label: "Higher" }],
    onCancel: vi.fn(),
    onApply: vi.fn(),
    onAskAi: vi.fn(),
    onValidate: vi.fn(),
    onPreview: vi.fn(),
    onInsert: vi.fn(),
    onUseExample: vi.fn(),
    onApplyTemplate: vi.fn(),
    onFormulaChange: vi.fn(),
    onRememberSelection: vi.fn(),
    onDirectionChange: vi.fn(),
    onChangeNoteChange: vi.fn(),
    onParamsTextChange: vi.fn(),
    ...overrides,
  };

  render(<FactorFormulaEditorModal {...props} />);
  return props;
}

describe("FactorFormulaEditorModal", () => {
  it("keeps the expression catalog and formula editor in one modal", () => {
    renderModal();

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("factorFormulaModalExpressionPanel")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "close 收盘价" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("sma(turnover_rate, 20)")).toBeInTheDocument();
  });

  it("forwards expression catalog inserts to the existing cursor insertion flow", () => {
    const props = renderModal();

    fireEvent.click(screen.getByRole("button", { name: "close 收盘价" }));
    expect(props.onInsert).toHaveBeenCalledWith("close");
  });

  it("prevents closing or applying while validation is running", () => {
    renderModal({ validating: true });

    expect(screen.getByRole("button", { name: "cancel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "factorFormulaModalApply" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument();
  });

  it("reuses AI, template, validation, preview, apply, and cancel callbacks", () => {
    const props = renderModal();

    fireEvent.click(screen.getByTestId("factor-modal-ask-ai"));
    expect(props.onAskAi).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "EP" }));
    expect(props.onApplyTemplate).toHaveBeenCalledWith(props.templates[0]);

    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate/ }));
    expect(props.onValidate).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /factorEditorPreview/ }));
    expect(props.onPreview).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "factorFormulaModalApply" }));
    expect(props.onApply).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "cancel" }));
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });
});
