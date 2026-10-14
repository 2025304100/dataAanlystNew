import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";

const { getGradeEvidence, restoreAutoGrade } = vi.hoisted(() => ({ getGradeEvidence: vi.fn(), restoreAutoGrade: vi.fn() }));
vi.mock("../../../../api/factorMining", () => ({ factorMiningApi: { getGradeEvidence, restoreAutoGrade, manualGrade: vi.fn() } }));
import FactorMiningResult from "./FactorMiningResult";

beforeEach(() => {
  vi.clearAllMocks();
  getGradeEvidence.mockResolvedValue({ candidate_id: "c1", grade: "A", formula: "close", manual_adjusted: true });
  restoreAutoGrade.mockResolvedValue({});
});

describe("挖掘结果等级操作", () => {
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
