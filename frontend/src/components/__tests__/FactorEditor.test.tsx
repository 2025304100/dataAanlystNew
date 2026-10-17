import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

const { mockApi, mockMessage } = vi.hoisted(() => ({
  mockApi: {
    getFactorDefinition: vi.fn(),
    scoringGetOverviewAsFactor: vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, latest_trade_date: null, factor_coverage: [] })),
    scoringGetOverview: vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, factor_coverage: [] })),
    scoringGetFactorDefinition: vi.fn(async () => ({})),
    scoringGetFactorModelListAsFactor: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    getEvaluationTaskHeartbeat: vi.fn(async () => ({})),
    listFactorVersions: vi.fn(),
    validateFactorFormula: vi.fn(async () => ({ is_valid: true, errors: [], warnings: [], data_dependencies: null, execution_plan: null })),
    createFactorVersion: vi.fn(async () => ({})),
    createFactorDraft: vi.fn(async (payload: { code: string }) => ({ code: payload.code })),
  },
  mockMessage: { error: vi.fn(), success: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return { ...actual, App: { ...actual.App, useApp: () => ({ message: mockMessage }) } };
});

vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN" }) }));
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  factorLabel: (_code: string, value?: string) => value || "-",
  factorCategoryLabel: (value: string) => value,
  factorDirectionLabel: (value: string) => value,
}));
vi.mock("../factors/FactorFormulaEditorModal", () => ({ default: () => null }));
vi.mock("../AiChatDrawer", () => ({ default: () => null }));

import FactorEditor from "../factors/FactorEditor";

describe("FactorEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.getFactorDefinition.mockResolvedValue({
      id: 9,
      code: "ep_demo",
      name: "资金跟庄",
      direction: "higher_better",
      formula_expr: null,
      default_missing_policy: "exclude",
      lifecycle_status: "draft",
    });
    mockApi.scoringGetFactorDefinition.mockResolvedValue({ code: "ep_demo", name: "资金跟庄", direction: "higher_better", default_missing_policy: "exclude" });
    mockApi.listFactorVersions.mockResolvedValue([{
      factor_code: "ep_demo",
      version: 1,
      formula_expr: "sum(main_net_inflow, 20) / max(sum(amount, 20), 1)",
      params: {},
      direction: "higher_better",
      change_note: "验证",
      postprocess: null,
      is_latest: true,
    }]);
  });

  it("restores the latest immutable version formula when definition formula is empty", async () => {
    render(<FactorEditor factorCode="ep_demo" onSaved={vi.fn()} onBack={vi.fn()} />);

    await waitFor(() => expect(mockApi.listFactorVersions).toHaveBeenCalledWith("ep_demo"));
    expect(await screen.findByText("sum(main_net_inflow, 20) / max(sum(amount, 20), 1)")).toBeInTheDocument();
  });

  it("preserves the existing version's nondefault postprocess when saving a new version", async () => {
    const postprocess = { winsorize: { method: "mad", mad_multiplier: 4.5 }, rank: { ascending: false }, missing_policy: "impute_zero" };
    mockApi.listFactorVersions.mockResolvedValueOnce([{ version: 2, is_latest: true, formula_expr: "close", params: {}, direction: "higher_better", postprocess }]);
    render(<FactorEditor factorCode="ep_demo" onSaved={vi.fn()} onBack={vi.fn()} />);
    await screen.findByText("close");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await screen.findAllByText("factorEditorValid");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorSave$/ }));
    await waitFor(() => expect(mockApi.createFactorVersion).toHaveBeenCalledWith("ep_demo", expect.objectContaining({ postprocess })));
  });

  it("preserves AI draft postprocess through validation and version creation", async () => {
    const postprocess = { winsorize: { method: "quantile", lower_quantile: 0.02, upper_quantile: 0.98 }, zscore: { ddof: 1 }, missing_policy: "ignore" };
    render(<FactorEditor factorCode={null} initialPayload={{ code: "ai_factor", name: "AI factor", category: "technical", formula_expr: "close", postprocess }} onSaved={vi.fn()} onBack={vi.fn()} />);
    await screen.findByText("close");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await screen.findAllByText("factorEditorValid");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorSave$/ }));
    await waitFor(() => expect(mockApi.createFactorVersion).toHaveBeenCalledWith("ai_factor", expect.objectContaining({ postprocess })));
  });

  it("uses the selected new-draft direction consistently in validation and both save payloads", async () => {
    render(<FactorEditor factorCode={null} initialPayload={{ code: "new_factor", name: "新因子", category: "technical", formula_expr: "close" }} onSaved={vi.fn()} onBack={vi.fn()} />);
    await screen.findByText("close");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await screen.findAllByText("factorEditorValid");
    fireEvent.mouseDown(screen.getAllByRole("combobox")[0]);
    fireEvent.click(await screen.findByText("factorEditorDirLowerBetter"));
    fireEvent.click(screen.getByRole("button", { name: /factorEditorSave$/ }));
    expect(mockApi.createFactorDraft).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await waitFor(() => expect(mockApi.validateFactorFormula).toHaveBeenLastCalledWith(expect.objectContaining({ direction: "lower_better" })));
    await screen.findAllByText("factorEditorValid");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorSave$/ }));
    await waitFor(() => expect(mockApi.createFactorVersion).toHaveBeenCalledWith("new_factor", expect.objectContaining({ direction: "lower_better" })));
    expect(mockApi.createFactorDraft).toHaveBeenCalledWith(expect.objectContaining({ direction: "lower_better" }));
  });

  it("offers the created draft as a recovery target after version creation fails", async () => {
    mockApi.createFactorVersion.mockRejectedValueOnce(new Error("version unavailable"));
    const onSaved = vi.fn();
    render(<FactorEditor factorCode={null} initialPayload={{ code: "partial_factor", name: "待恢复因子", category: "technical", formula_expr: "close" }} onSaved={onSaved} onBack={vi.fn()} />);
    await screen.findByText("close");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await screen.findAllByText("factorEditorValid");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorSave$/ }));
    const recovery = await screen.findByRole("button", { name: "factorLabEditorOpenCreatedDraft" });
    fireEvent.click(screen.getByRole("button", { name: /factorEditorSave$/ }));
    expect(mockApi.createFactorDraft).toHaveBeenCalledTimes(1);
    fireEvent.click(recovery);
    expect(onSaved).toHaveBeenCalledWith("partial_factor");
  });

  it("clears previous processing and validation when reusing the editor for another factor", async () => {
    mockApi.listFactorVersions.mockResolvedValueOnce([{ version: 1, is_latest: true, formula_expr: "close", params: {}, postprocess: { rank: { ascending: false }, missing_policy: "impute_zero" } }]);
    const editor = render(<FactorEditor factorCode="A" onSaved={vi.fn()} onBack={vi.fn()} />);
    await screen.findByText("close");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await screen.findAllByText("factorEditorValid");
    mockApi.listFactorVersions.mockResolvedValueOnce([{ version: 1, is_latest: true, formula_expr: "volume", params: {}, postprocess: null }]);
    editor.rerender(<FactorEditor factorCode="B" onSaved={vi.fn()} onBack={vi.fn()} />);
    await screen.findByText("volume");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await screen.findAllByText("factorEditorValid");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorSave$/ }));
    await waitFor(() => expect(mockApi.createFactorVersion).toHaveBeenCalledWith("B", expect.objectContaining({ postprocess: null, params: {} })));
  });

  it("does not use a late validation from another factor to authorize saving the current version", async () => {
    let resolveValidation!: (value: unknown) => void;
    mockApi.validateFactorFormula.mockReturnValueOnce(new Promise((resolve) => { resolveValidation = resolve; }) as any);
    mockApi.listFactorVersions.mockResolvedValueOnce([{ version: 1, is_latest: true, formula_expr: "close", params: {}, postprocess: null }]);
    const editor = render(<FactorEditor factorCode="A" onSaved={vi.fn()} onBack={vi.fn()} />);
    await screen.findByText("close");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    mockApi.listFactorVersions.mockResolvedValueOnce([{ version: 1, is_latest: true, formula_expr: "volume", params: {}, postprocess: null }]);
    editor.rerender(<FactorEditor factorCode="B" onSaved={vi.fn()} onBack={vi.fn()} />);
    await screen.findByText("volume");
    await act(async () => resolveValidation({ is_valid: true, errors: [], warnings: [] }));
    fireEvent.click(screen.getByRole("button", { name: /factorEditorSave$/ }));
    expect(mockApi.createFactorVersion).not.toHaveBeenCalled();
  });

  it("does not close a new editor session when a previous version save finishes", async () => {
    let resolveSave!: (value: unknown) => void;
    mockApi.createFactorVersion.mockReturnValueOnce(new Promise((resolve) => { resolveSave = resolve; }) as any);
    const onSaved = vi.fn();
    mockApi.listFactorVersions.mockResolvedValueOnce([{ version: 1, is_latest: true, formula_expr: "close", params: {}, postprocess: null }]);
    const editor = render(<FactorEditor factorCode="A" onSaved={onSaved} onBack={vi.fn()} />);
    await screen.findByText("close");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await screen.findAllByText("factorEditorValid");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorSave$/ }));
    mockApi.listFactorVersions.mockResolvedValueOnce([{ version: 1, is_latest: true, formula_expr: "volume", params: {}, postprocess: null }]);
    editor.rerender(<FactorEditor factorCode="B" onSaved={onSaved} onBack={vi.fn()} />);
    await screen.findByText("volume");
    await act(async () => resolveSave({ version: 2 }));
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByText("volume")).toBeInTheDocument();
  });

  it("can validate a new AI draft while the previous factor is still loading", async () => {
    mockApi.listFactorVersions.mockReturnValueOnce(new Promise(() => {}));
    const editor = render(<FactorEditor factorCode="pending-A" onSaved={vi.fn()} onBack={vi.fn()} />);
    editor.rerender(<FactorEditor factorCode={null} initialPayload={{ code: "new-draft", name: "New", category: "technical", formula_expr: "close" }} onSaved={vi.fn()} onBack={vi.fn()} />);
    await screen.findByText("close");
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await screen.findAllByText("factorEditorValid");
  });

  it("refuses to save when an older backend omits the existing postprocess field", async () => {
    mockApi.listFactorVersions.mockResolvedValueOnce([{ version: 1, is_latest: true, formula_expr: "close", params: {} }]);
    render(<FactorEditor factorCode="old-backend" onSaved={vi.fn()} onBack={vi.fn()} />);
    await waitFor(() => expect(mockApi.listFactorVersions).toHaveBeenCalledWith("old-backend"));
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: /factorEditorValidate$/ }));
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: /factorEditorSave$/ }));
    expect(mockApi.createFactorVersion).not.toHaveBeenCalled();
    expect(mockMessage.error).toHaveBeenCalledWith(expect.stringContaining("factorEditorPostprocessUnavailable"));
  });

  it("provides an inline retry when an existing factor cannot be loaded", async () => {
    mockApi.listFactorVersions.mockRejectedValueOnce(new Error("version service unavailable"));
    render(<FactorEditor factorCode="ep_demo" onSaved={vi.fn()} onBack={vi.fn()} />);
    expect(await screen.findByText("version service unavailable")).toBeVisible();
    expect(screen.getByRole("button", { name: /factorEditorSave$/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "factorLabRetryCheck" }));
    expect(await screen.findByText("sum(main_net_inflow, 20) / max(sum(amount, 20), 1)")).toBeInTheDocument();
  });
});
