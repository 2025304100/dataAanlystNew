import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
const { api, notice } = vi.hoisted(() => ({ api: {
  getFactorFormulaCatalog: vi.fn(), validateFactorFormula: vi.fn(), previewFactorFormula: vi.fn(),
  createFactorDraft: vi.fn(), createFactorVersion: vi.fn(), streamAIChat: vi.fn(),
}, notice: { error: vi.fn(), warning: vi.fn(), success: vi.fn() } }));
vi.mock("../../api/client", () => ({ api }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN" }) }));
vi.mock("../../i18n", () => ({ t: (key: string) => key, factorLabel: (code: string, name?: string) => name || code,
  factorCategoryLabel: (value: string) => value, factorDirectionLabel: (value: string) => value }));
vi.mock("antd", async () => {
  const original = await vi.importActual<typeof import("antd")>("antd");
  return { ...original, App: { ...original.App, useApp: () => ({ message: notice }) } };
});
import FactorEditor from "../factors/FactorEditor";

const initialPayload = { code: "workflow_factor", name: "流程因子", category: "technical", formula_expr: "close" };
beforeEach(() => {
  vi.clearAllMocks();
  api.getFactorFormulaCatalog.mockResolvedValue({ dsl_version: "2.0", compiler_version: "test", fields: [], functions: [], disabled_functions: [], operators: [], templates: [] });
  api.validateFactorFormula.mockResolvedValue({ is_valid: true, errors: [], warnings: [], execution_plan: null, data_dependencies: null });
  api.previewFactorFormula.mockResolvedValue({ is_valid: true, errors: [], values: [{ symbol: "000001", trade_date: "2026-10-14", raw_value: 0, winsorized_value: null, normalized_value: 0, eligible: true }],
    selected_trade_date: "2026-10-14", data_cutoff_at: null, complete_trade_day_evidence: null, data_dependencies: null,
    attempted_count: 1, valid_count: 1, missing_count: 0, coverage_rate: 1, missing_rate: 0, distribution: {}, outlier_count: 0,
    missing_reasons: {}, data_fix_links: [], readiness_warnings: [], evaluation_supported: true, evaluation_mode: "continuous", blocking_fields: [], elapsed_ms: 12, data_readiness: null, execution_plan: null });
  api.createFactorDraft.mockResolvedValue({ code: "workflow_factor" });
  api.createFactorVersion.mockResolvedValue({ version: 1 });
  api.streamAIChat.mockImplementation(async (_payload, handlers) => { handlers.onDone({ ok: true, reply: "```formula\nclose + volume\n```" }); });
});
async function openFormula() {
  await screen.findByText("close");
  fireEvent.click(screen.getAllByRole("button", { name: /factorFormulaModalOpen/ })[0]);
  const dialog = await screen.findByRole("dialog", { name: /factorFormulaModalTitle/ });
  await waitFor(() => expect(api.getFactorFormulaCatalog).toHaveBeenCalled());
  return dialog;
}
function newEditor(onSaved = vi.fn()) { return render(<FactorEditor factorCode={null} initialPayload={initialPayload} onSaved={onSaved} onBack={vi.fn()} />); }

describe("Factor editor complete user workflow", () => {
  it("formats, undoes, redoes and clears formula edits without writing a version", async () => {
    newEditor();
    const dialog = await openFormula();
    const formula = within(dialog).getByDisplayValue("close");
    fireEvent.change(formula, { target: { value: "  volume  " } });
    fireEvent.click(within(dialog).getByRole("button", { name: /格式化/ }));
    expect(formula).toHaveValue("volume");
    fireEvent.click(within(dialog).getByRole("button", { name: /撤销/ }));
    expect(formula).toHaveValue("  volume  ");
    fireEvent.click(within(dialog).getByRole("button", { name: /重做/ }));
    expect(formula).toHaveValue("volume");
    fireEvent.click(within(dialog).getByRole("button", { name: /清空/ }));
    expect(formula).toHaveValue("");
    expect(within(dialog).getByRole("button", { name: /factorEditorValidate/ })).toBeDisabled();
    expect(api.createFactorVersion).not.toHaveBeenCalled();
  });

  it("invalidates prior validation after a postprocess change and sends the configured rank policy", async () => {
    newEditor();
    await screen.findByText("close");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /factorEditorSave$/ })).toBeEnabled());
    const postprocess = document.querySelector(".factor-editor-postprocess-card") as HTMLElement;
    fireEvent.mouseDown(within(postprocess).getAllByRole("combobox")[1]);
    fireEvent.click(await screen.findByText("factorEditorNormRank"));
    fireEvent.click(within(postprocess).getByRole("switch"));
    expect(screen.getByRole("button", { name: /factorEditorSave$/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await waitFor(() => expect(api.validateFactorFormula).toHaveBeenLastCalledWith(expect.objectContaining({ postprocess: { rank: { ascending: false }, missing_policy: "exclude" } })));
  });

  it("validates and previews edited params, then cancelling restores the original formula and validation state", async () => {
    newEditor();
    const dialog = await openFormula();
    fireEvent.change(within(dialog).getByDisplayValue("close"), { target: { value: "volume" } });
    fireEvent.change(within(dialog).getByPlaceholderText("{}"), { target: { value: '{"lookback":20}' } });
    fireEvent.click(within(dialog).getByRole("button", { name: /factorEditorValidate/ }));
    await waitFor(() => expect(api.validateFactorFormula).toHaveBeenCalledWith(expect.objectContaining({ formula_expr: "volume", params: { lookback: 20 } })));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "cancel" })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole("button", { name: /factorEditorPreview/ }));
    await waitFor(() => expect(api.previewFactorFormula).toHaveBeenCalledWith(expect.objectContaining({ formula_expr: "volume", params: { lookback: 20 } })));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "cancel" })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole("button", { name: "cancel" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /factorEditorSave$/ })).toBeDisabled());
    expect(document.querySelector(".factor-formula-summary__code code")?.textContent).toBe("close");
    expect(api.createFactorDraft).not.toHaveBeenCalled();
  });

  it("rejects array-shaped params before validating or previewing", async () => {
    newEditor();
    const dialog = await openFormula();
    fireEvent.change(within(dialog).getByPlaceholderText("{}"), { target: { value: "[1,2]" } });
    fireEvent.click(within(dialog).getByRole("button", { name: /factorEditorValidate/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: /factorEditorPreview/ }));
    expect(api.validateFactorFormula).not.toHaveBeenCalled();
    expect(api.previewFactorFormula).not.toHaveBeenCalled();
    expect(notice.error).toHaveBeenCalledWith("params 必须是 JSON 对象");
  });

  it("keeps AI output as a manual draft until inserted, validated and explicitly saved", async () => {
    const onSaved = vi.fn();
    newEditor(onSaved);
    const dialog = await openFormula();
    fireEvent.click(within(dialog).getByTestId("factor-modal-ask-ai"));
    const aiInput = await screen.findByPlaceholderText("aiChatPlaceholder");
    const aiDialog = aiInput.closest('[role="dialog"]') as HTMLElement;
    fireEvent.change(aiInput, { target: { value: "写一个量价因子" } });
    fireEvent.click(within(aiDialog).getByRole("button", { name: /aiSend/ }));
    const insert = await within(aiDialog).findByText("aiInsertFormula");
    expect(document.querySelector(".factor-formula-summary__code code")?.textContent).toBe("close");
    expect(api.createFactorDraft).not.toHaveBeenCalled();
    fireEvent.click(insert);
    await waitFor(() => expect(within(dialog).getByDisplayValue("close + volume")).toBeInTheDocument());
    fireEvent.click(within(dialog).getByRole("button", { name: "factorFormulaModalApply" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /factorEditorSave$/ })).toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /factorEditorSave$/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /factorEditorSave$/ }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith("workflow_factor"));
    expect(api.createFactorVersion).toHaveBeenCalledWith("workflow_factor", expect.objectContaining({ formula_expr: "close + volume" }));
  });

  it("returns without writing when the user leaves an unsaved draft", async () => {
    const onBack = vi.fn();
    render(<FactorEditor factorCode={null} initialPayload={initialPayload} onSaved={vi.fn()} onBack={onBack} />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: /factorEditorBack/ }));
    expect(onBack).toHaveBeenCalledTimes(1);
    expect(api.createFactorDraft).not.toHaveBeenCalled();
    expect(api.createFactorVersion).not.toHaveBeenCalled();
  });
});
