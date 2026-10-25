import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock, notices } = vi.hoisted(() => ({
  apiMock: {
    getAIDraft: vi.fn(), previewAIDraft: vi.fn(), confirmAIDraft: vi.fn(),
    executeAIDraft: vi.fn(), validateFactorFormula: vi.fn(), rejectAIDraft: vi.fn(),
  },
  notices: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
vi.mock("../../../api/client", () => ({ api: apiMock }));
vi.mock("../../../i18n", () => ({ t: (key: string) => key, template: (key: string) => key }));
vi.mock("antd", async () => ({ ...await vi.importActual<typeof import("antd")>("antd"), message: notices }));

import FactorDraftConfirmModal from "../FactorDraftConfirmModal";

function payload(code: string) {
  return { code, name: code, category: "trend", formula_expr: "mean(close,n)", params: { n: 20 },
    postprocess: { rank: { method: "percentile" }, missing_policy: "exclude" }, direction: "higher_better" as const,
    factor_kind: "continuous" as const, risk_level: "medium" as const };
}
function detail(id: number, code: string) {
  return { id, audit_id: id, action_type: "draft_factor", current_payload: payload(code), original_suggested_payload: payload(code) };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.getAIDraft.mockImplementation(async (id: number) => detail(id, `draft_${id}`));
  apiMock.previewAIDraft.mockResolvedValue({ is_valid: true, errors: [], changes: [] });
  apiMock.confirmAIDraft.mockResolvedValue({ success: true });
  apiMock.executeAIDraft.mockResolvedValue({ success: true, factor_code: "created_factor", factor_id: 99 });
});

describe("AI factor draft confirmation contract", () => {
  it("changing the audit target ignores the previous draft's late response", async () => {
    const old = deferred<ReturnType<typeof detail>>();
    apiMock.getAIDraft.mockImplementation((id: number) => id === 1 ? old.promise : Promise.resolve(detail(2, "draft_b")));
    const props = { open: true, onClose: vi.fn(), onApplyToEditor: vi.fn() };
    const view = render(<FactorDraftConfirmModal {...props} auditId={1} />);
    view.rerender(<FactorDraftConfirmModal {...props} auditId={2} />);
    await waitFor(() => expect(screen.getByPlaceholderText("factor_code")).toHaveValue("draft_b"));
    await act(async () => old.resolve(detail(1, "draft_a")));
    expect(screen.getByPlaceholderText("factor_code")).toHaveValue("draft_b");
    expect(screen.queryByDisplayValue("draft_a")).not.toBeInTheDocument();
  });

  it("a failed audited draft read does not substitute a suggested payload and offers retry", async () => {
    apiMock.getAIDraft.mockRejectedValueOnce(new Error("read unavailable"));
    render(<FactorDraftConfirmModal open auditId={1} suggestedPayload={payload("unverified_fallback")}
      onClose={vi.fn()} onApplyToEditor={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("aiDraftLoadFailed")).toBeVisible());
    expect(screen.queryByPlaceholderText("factor_code")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /aiDraftConfirmCreate$/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "retry" }));
    await waitFor(() => expect(screen.getByPlaceholderText("factor_code")).toHaveValue("draft_1"));
    expect(apiMock.confirmAIDraft).not.toHaveBeenCalled();
  });

  it("editing a draft invalidates any preview result still in flight", async () => {
    const preview = deferred<{ is_valid: boolean; errors: string[]; changes: [] }>();
    apiMock.previewAIDraft.mockReturnValueOnce(preview.promise);
    render(<FactorDraftConfirmModal open auditId={1} onClose={vi.fn()} onApplyToEditor={vi.fn()} />);
    await waitFor(() => expect(screen.getByPlaceholderText("factor_code")).toHaveValue("draft_1"));
    fireEvent.click(await screen.findByRole("button", { name: /aiDraftPreview$/ }));
    fireEvent.change(screen.getByDisplayValue("mean(close,n)"), { target: { value: "close / 0" } });
    await act(async () => preview.resolve({ is_valid: true, errors: [], changes: [] }));
    expect(screen.queryByText("aiDraftPreviewValid")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("close / 0")).toBeVisible();
  });

  it("switching draft targets during confirmation cannot confirm the old target or close the new one", async () => {
    const preview = deferred<{ is_valid: boolean; errors: string[]; changes: [] }>();
    apiMock.previewAIDraft.mockReturnValueOnce(preview.promise);
    const props = { open: true, onClose: vi.fn(), onApplyToEditor: vi.fn(), onCreated: vi.fn() };
    const view = render(<FactorDraftConfirmModal {...props} auditId={1} />);
    await waitFor(() => expect(screen.getByPlaceholderText("factor_code")).toHaveValue("draft_1"));
    fireEvent.click(await screen.findByRole("button", { name: /aiDraftConfirmCreate$/ }));
    expect(screen.getByPlaceholderText("factor_code")).toBeDisabled();
    expect(screen.getByRole("button", { name: "aiDraftApplyToEditor" })).toBeDisabled();
    view.rerender(<FactorDraftConfirmModal {...props} auditId={2} />);
    await waitFor(() => expect(screen.getByPlaceholderText("factor_code")).toHaveValue("draft_2"));
    await act(async () => preview.resolve({ is_valid: true, errors: [], changes: [] }));
    expect(apiMock.confirmAIDraft).not.toHaveBeenCalled();
    expect(apiMock.executeAIDraft).not.toHaveBeenCalled();
    expect(props.onClose).not.toHaveBeenCalled();
    expect(props.onCreated).not.toHaveBeenCalled();
  });

  it("confirmation retains parameters and postprocessing across a recoverable failure", async () => {
    const props = { open: true, auditId: 1, onClose: vi.fn(), onApplyToEditor: vi.fn(), onCreated: vi.fn() };
    apiMock.executeAIDraft.mockResolvedValueOnce({ success: false, message: "retryable failure" });
    render(<FactorDraftConfirmModal {...props} />);
    await waitFor(() => expect(screen.getByPlaceholderText("factor_code")).toHaveValue("draft_1"));
    fireEvent.change(screen.getByPlaceholderText("factor_code"), { target: { value: "edited_factor" } });
    fireEvent.click(await screen.findByRole("button", { name: /aiDraftConfirmCreate$/ }));
    await waitFor(() => expect(notices.error).toHaveBeenCalledWith("retryable failure"));
    expect(props.onClose).not.toHaveBeenCalled();
    const retry = screen.getByRole("button", { name: /aiDraftConfirmCreate$/ });
    await waitFor(() => expect(retry).toBeEnabled());
    fireEvent.click(retry);
    await waitFor(() => expect(props.onCreated).toHaveBeenCalledWith("created_factor", 99));
    const expected = { ...payload("draft_1"), code: "edited_factor" };
    expect(apiMock.confirmAIDraft).toHaveBeenNthCalledWith(1, 1, expected);
    expect(apiMock.confirmAIDraft).toHaveBeenNthCalledWith(2, 1, expected);
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("the review shows changed parameters instead of hiding non-text payload differences", async () => {
    const current = payload("draft_1");
    apiMock.getAIDraft.mockResolvedValueOnce({ ...detail(1, "draft_1"),
      original_suggested_payload: { ...current, params: { n: 5 } }, current_payload: current });
    render(<FactorDraftConfirmModal open auditId={1} onClose={vi.fn()} onApplyToEditor={vi.fn()} />);
    expect(await screen.findByText("aiDraftDiffCount")).toBeInTheDocument();
    expect(screen.getByText("aiDraftFieldParams")).toBeInTheDocument();
    expect(screen.getByText(/"n":5/)).toBeInTheDocument();
    expect(screen.getByText(/"n":20/)).toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
  });

  it.each([undefined, null, { rank: { method: "percentile" }, missing_policy: "exclude" }])("an unaudited preview validates the same postprocessing that will reach the editor: %j", async (postprocess) => {
    const suggestion = { ...payload("manual_review"), postprocess };
    apiMock.validateFactorFormula.mockResolvedValueOnce({ is_valid: true, errors: [] });
    const apply = vi.fn();
    render(<FactorDraftConfirmModal open suggestedPayload={suggestion} onClose={vi.fn()} onApplyToEditor={apply} />);
    fireEvent.click(await screen.findByRole("button", { name: /aiDraftPreview$/ }));
    await waitFor(() => expect(apiMock.validateFactorFormula).toHaveBeenCalledWith({
      formula_expr: suggestion.formula_expr, params: suggestion.params,
      direction: suggestion.direction, ...(postprocess !== undefined ? { postprocess } : {}),
    }));
    fireEvent.click(screen.getByRole("button", { name: "aiDraftApplyToEditor" }));
    expect(apply).toHaveBeenCalledWith(suggestion);
    expect(apiMock.confirmAIDraft).not.toHaveBeenCalled();
  });
});
