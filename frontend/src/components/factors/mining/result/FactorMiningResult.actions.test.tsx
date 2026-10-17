import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";

const { getGradeEvidence, restoreAutoGrade } = vi.hoisted(() => ({ getGradeEvidence: vi.fn(), restoreAutoGrade: vi.fn() }));
vi.mock("../../../../api/factorMining", () => ({ factorMiningApi: { getGradeEvidence, restoreAutoGrade, manualGrade: vi.fn() } }));
import FactorMiningResult from "./FactorMiningResult";

beforeEach(() => {
  vi.clearAllMocks();
  getGradeEvidence.mockResolvedValue({ candidate_id: "c1", grade: "A", formula: "close", manual_adjusted: true });
  restoreAutoGrade.mockResolvedValue({});
});

describe("挖掘结果等级操作", () => {
  it("A的恢复自动操作迟到，不能清空B或发起A的回读覆盖B", async () => {
    let resolveRestore!: (value: unknown) => void;
    restoreAutoGrade.mockImplementationOnce(() => new Promise((resolve) => { resolveRestore = resolve; }));
    getGradeEvidence.mockImplementation(async (candidateId: string) => ({ candidate_id: candidateId, grade: "B", formula: `factor-${candidateId}`, manual_adjusted: true }));
    const onChanged = vi.fn();
    render(<FactorMiningResult rows={[
      { candidate_id: "c1", grade: "A", formula: "factor-A", icir: 0.4, icir_adjusted: null },
      { candidate_id: "c2", grade: "B", formula: "factor-B", icir: 0.3, icir_adjusted: null },
    ]} onCandidateChanged={onChanged} />);
    fireEvent.click(document.querySelector('[data-result-evidence="c1"]') as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-evidence-restore-auto]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-evidence-restore-auto]') as HTMLElement);
    fireEvent.click(document.querySelector('[data-result-evidence="c2"]') as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-evidence-drawer]')?.textContent).toContain("factor-c2"));
    await act(async () => { resolveRestore({}); });
    expect(getGradeEvidence.mock.calls.map((call) => call[0])).toEqual(["c1", "c2"]);
    expect(document.querySelector('[data-evidence-drawer]')?.textContent).toContain("factor-c2");
    // 实际已完成的A操作仍刷新同一批次列表，不伪装为取消；抽屉仍停留B。
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("已换到另一批结果后，旧mutation完成不能触发新批次刷新或回读", async () => {
    let resolveRestore!: (value: unknown) => void;
    restoreAutoGrade.mockImplementationOnce(() => new Promise((resolve) => { resolveRestore = resolve; }));
    getGradeEvidence.mockResolvedValueOnce({ candidate_id: "c1", grade: "A", formula: "old-A", manual_adjusted: true });
    const onChanged = vi.fn();
    const { rerender } = render(<FactorMiningResult rows={[{ candidate_id: "c1", grade: "A", formula: "A", icir: 0.4, icir_adjusted: null }]} onCandidateChanged={onChanged} />);
    fireEvent.click(document.querySelector('[data-result-evidence="c1"]') as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-evidence-restore-auto]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-evidence-restore-auto]') as HTMLElement);
    rerender(<FactorMiningResult rows={[{ candidate_id: "c2", grade: "B", formula: "B", icir: 0.3, icir_adjusted: null }]} onCandidateChanged={onChanged} />);
    await act(async () => { resolveRestore({}); });
    expect(document.querySelector('[data-evidence-drawer]')).toBeNull();
    expect(getGradeEvidence).toHaveBeenCalledTimes(1);
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("API没有可用评级时显示未定级，不把缺失归一化成D", async () => {
    getGradeEvidence.mockResolvedValueOnce({ candidate_id: "c1", grade: null, formula: "ungraded", reason_zh: "参考指标未计算" });
    render(<FactorMiningResult rows={[{ candidate_id: "c1", grade: null, formula: "ungraded", icir: 0.4, icir_adjusted: null }]} />);
    fireEvent.click(document.querySelector('[data-result-evidence="c1"]') as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-evidence-grade]')?.textContent).toBe("未定级"));
  });

  it("旧证据关闭后重新打开同一候选，旧错误不能盖住新证据", async () => {
    let rejectOld!: (error: unknown) => void;
    getGradeEvidence.mockImplementationOnce(() => new Promise((_, reject) => { rejectOld = reject; }));
    getGradeEvidence.mockResolvedValueOnce({ candidate_id: "c1", grade: "A", formula: "fresh-evidence", manual_adjusted: true });
    render(<FactorMiningResult rows={[{ candidate_id: "c1", grade: "A", formula: "close", icir: 0.4, icir_adjusted: null }]} />);
    fireEvent.click(document.querySelector('[data-result-evidence="c1"]') as HTMLElement);
    fireEvent.click(document.querySelector('[data-evidence-close]') as HTMLElement);
    fireEvent.click(document.querySelector('[data-result-evidence="c1"]') as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-evidence-drawer]')?.textContent).toContain("fresh-evidence"));
    await act(async () => { rejectOld(new Error("late-old-error")); });
    expect(document.querySelector('[data-evidence-drawer]')?.textContent).toContain("fresh-evidence");
    expect(document.querySelector('[data-evidence-drawer]')?.textContent).not.toContain("late-old-error");
  });

  it("先打开A再打开B，A的迟到证据不能覆盖B或把A显示成可改级的B", async () => {
    let resolveA!: (value: unknown) => void;
    getGradeEvidence.mockImplementationOnce(() => new Promise((resolve) => { resolveA = resolve; }));
    getGradeEvidence.mockResolvedValueOnce({ candidate_id: "c2", grade: "B", formula: "factor-B", manual_adjusted: true });
    render(<FactorMiningResult rows={[
      { candidate_id: "c1", grade: "A", formula: "factor-A", icir: 0.4, icir_adjusted: null },
      { candidate_id: "c2", grade: "B", formula: "factor-B", icir: 0.3, icir_adjusted: null },
    ]} />);
    fireEvent.click(document.querySelector('[data-result-evidence="c1"]') as HTMLElement);
    fireEvent.click(document.querySelector('[data-result-evidence="c2"]') as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-evidence-drawer]')?.textContent).toContain("factor-B"));
    await act(async () => { resolveA({ candidate_id: "c1", grade: "A", formula: "factor-A", manual_adjusted: true }); });
    expect(document.querySelector('[data-evidence-drawer]')?.textContent).toContain("factor-B");
    expect(document.querySelector('[data-evidence-drawer]')?.textContent).not.toContain("factor-A");
    fireEvent.click(document.querySelector('[data-evidence-restore-auto]') as HTMLElement);
    await waitFor(() => expect(restoreAutoGrade).toHaveBeenCalledWith("c2"));
  });

  it("恢复自动评级后请求刷新结果行，保持抽屉和列表评级一致", async () => {
    const onChanged = vi.fn();
    render(<FactorMiningResult rows={[{ candidate_id: "c1", grade: "A", formula: "close", icir: 0.4, icir_adjusted: null }]} onCandidateChanged={onChanged} />);
    fireEvent.click(document.querySelector('[data-result-evidence="c1"]') as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-evidence-restore-auto]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-evidence-restore-auto]') as HTMLElement);
    await waitFor(() => expect(restoreAutoGrade).toHaveBeenCalledWith("c1"));
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  });
});
