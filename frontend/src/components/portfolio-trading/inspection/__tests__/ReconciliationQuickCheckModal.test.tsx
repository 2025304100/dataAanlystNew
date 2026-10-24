import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api } from "../../../../api/client";
import type { ReconciliationResponse } from "../../../../types";
import ReconciliationQuickCheckModal from "../ReconciliationQuickCheckModal";
import {INSPECTION_CHANGE_KEY} from "../../../../utils/inspectionFreshness";

vi.mock("../../../../api/client", () => ({ api: {
  previewPortfolioReconciliation: vi.fn(), triggerPortfolioReconcile: vi.fn(), confirmPortfolioReconciliation: vi.fn(),
} }));
vi.mock("../InspectionHistoryPanel", () => ({ default: (props: any) => <section data-testid="history-scope">{JSON.stringify(props.context)}<button onClick={props.onReturn}>返回原核对</button></section> }));
vi.mock("../InspectionEvidencePanel", () => ({ default: (props: any) => <section data-testid="evidence-scope">原订单交易日 {props.tradeDate}<button onClick={props.onHistory}>查看关联历史</button><button onClick={props.onBack}>返回原核对</button></section> }));

function report(over: Partial<ReconciliationResponse> = {}): ReconciliationResponse {
  return { portfolio_id: 7, trade_date: "2026-09-30", as_of_at: "2026-10-24T01:00:00Z", read_only: true,
    differences_found: true, zero_sum_check_passed: false, report_id: "report-day30", report_version: "v30",
    last_reconciled_trade_date: "2026-09-28", verdict: "EVIDENCE_INCOMPLETE",
    items: [{ dimension: "DECISION_RUN_NOT_FOUND", expected_value: null, actual_value: null, diff_value: null,
      explain_note: "portfolio_id=7 trade_date=2026-09-30 DecisionRun missing" }],
    missing_evidence: [{ code: "DECISION_RUN_NOT_FOUND", detail: "portfolio_id=7 trade_date=2026-09-30" }],
    execution_expected_evidence: [{ status: "UNKNOWN" }], checks: [{ check_code: "POSITIONS", status: "UNKNOWN" }],
    ...over };
}
function open(over: Partial<Parameters<typeof ReconciliationQuickCheckModal>[0]> = {}) {
  const onChanged = vi.fn(), onClose = vi.fn();
  return { onChanged, onClose, ...render(<ReconciliationQuickCheckModal portfolioId={7} tradeDate="2026-09-30" onChanged={onChanged} onClose={onClose} {...over}/>) };
}
beforeEach(() => { sessionStorage.clear();vi.clearAllMocks(); vi.mocked(api.previewPortfolioReconciliation).mockResolvedValue(report()); });

describe("按交易日快捷核对", () => {
  it("invalidates an open quick check and rereads the same date without rechecking or confirming",async()=>{
    open();await screen.findByText("历史执行资料不完整");
    fireEvent(window,new StorageEvent("storage",{key:`${INSPECTION_CHANGE_KEY}7`,newValue:JSON.stringify({version:1,id:crypto.randomUUID(),source:"remote-quick",portfolioId:7,at:Date.now()})}));
    await waitFor(()=>expect(screen.getByTestId("inspection-stale-notice")).toBeVisible());expect(screen.getByRole("button",{name:"重新核对本日"})).toBeDisabled();
    fireEvent.click(screen.getByRole("button",{name:"重新读取当前依据"}));await waitFor(()=>expect(screen.queryByTestId("inspection-stale-notice")).not.toBeInTheDocument());
    expect(api.previewPortfolioReconciliation).toHaveBeenLastCalledWith(7,"2026-09-30");expect(api.triggerPortfolioReconcile).not.toHaveBeenCalled();expect(api.confirmPortfolioReconciliation).not.toHaveBeenCalled();
  });
  it("只读打开居中弹窗，缺证据时不平铺空比较或裸露内部字段", async () => {
    open();
    await screen.findByText("历史执行资料不完整");
    expect(api.previewPortfolioReconciliation).toHaveBeenCalledWith(7, "2026-09-30");
    expect(api.triggerPortfolioReconcile).not.toHaveBeenCalled();
    expect(api.confirmPortfolioReconciliation).not.toHaveBeenCalled();
    expect(document.querySelector(".ant-modal-centered")).toBeTruthy();
    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(dialog).toBeVisible());
    expect(dialog.textContent).not.toContain("portfolio_id=");
    expect(dialog.textContent).not.toContain("DecisionRun");
    expect(within(dialog).queryByRole("table")).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "查看缺口与查找方法" }));
    expect(within(dialog).getByText("当天的自动运行记录与决策明细")).toBeVisible();
    expect(within(dialog).getByText(/确实找不到时，保留该日历史未知/)).toBeVisible();
  });

  it("重检始终携带所选日期，重复点击只发一次，不确认或恢复", async () => {
    vi.mocked(api.triggerPortfolioReconcile).mockResolvedValue(report({ read_only: false }));
    const view = open();
    await screen.findByText("历史执行资料不完整");
    const button = screen.getByRole("button", { name: "重新核对本日" });
    fireEvent.click(button); fireEvent.click(button);
    await screen.findByText("本次重检结果");
    expect(api.triggerPortfolioReconcile).toHaveBeenCalledTimes(1);
    expect(api.triggerPortfolioReconcile).toHaveBeenCalledWith(7, { trade_date: "2026-09-30" });
    expect(view.onChanged).toHaveBeenCalledTimes(1);
    expect(api.confirmPortfolioReconciliation).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /确认.*恢复|确认并解除/ })).toBeNull();
  });

  it.each([{ portfolio_id: 8 }, { trade_date: "2026-09-29" }])("拒绝其他组合或日期的报告 %j", async over => {
    vi.mocked(api.previewPortfolioReconciliation).mockResolvedValue(report(over));
    open();
    await screen.findByRole("alert");
    await waitFor(() => expect(screen.getByText(/返回结果不属于所选组合与交易日/)).toBeVisible());
    expect(screen.getByRole("button", { name: "重新核对本日" })).toBeDisabled();
    expect(screen.queryByText("历史执行资料不完整")).toBeNull();
  });

  it("读取失败可原日期重试，旧结果不能当新结论", async () => {
    vi.mocked(api.previewPortfolioReconciliation).mockRejectedValueOnce(new Error("portfolio_id=7 READ_ERROR"));
    open();
    await screen.findByRole("alert");
    expect(screen.getByRole("dialog").textContent).not.toContain("portfolio_id=");
    fireEvent.click(screen.getByRole("button", { name: "重新读取本日材料" }));
    await screen.findByText("历史执行资料不完整");
    expect(api.previewPortfolioReconciliation).toHaveBeenLastCalledWith(7, "2026-09-30");
  });

  it("组合切换时忽略未完成的旧请求", async () => {
    let finishOld!: (value: ReconciliationResponse) => void;
    vi.mocked(api.previewPortfolioReconciliation).mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }));
    const view = open();
    await waitFor(() => expect(api.previewPortfolioReconciliation).toHaveBeenCalledTimes(1));
    vi.mocked(api.previewPortfolioReconciliation).mockResolvedValue(report({ portfolio_id: 8 }));
    view.rerender(<ReconciliationQuickCheckModal portfolioId={8} tradeDate="2026-09-30" onChanged={view.onChanged} onClose={view.onClose}/>);
    await screen.findByText("历史执行资料不完整");
    finishOld(report({ trade_date: "2026-09-29" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(api.previewPortfolioReconciliation).toHaveBeenLastCalledWith(8, "2026-09-30");
  });

  it("正常等待不创建补材料待办，通过也只说明本日结论", async () => {
    vi.mocked(api.previewPortfolioReconciliation).mockResolvedValue(report({ verdict: "WAITING_EXECUTION", work_state: "waiting_execution" }));
    const view = open();
    await screen.findByText("等待本日执行结果");
    expect(screen.queryByRole("button", { name: "查看可关联原订单" })).toBeNull();
    expect(screen.queryByText(/历史缺口 ·/)).toBeNull();
    view.unmount();
    vi.mocked(api.previewPortfolioReconciliation).mockResolvedValue(report({ verdict: "PASSED", zero_sum_check_passed: true,
      differences_found: false, items: [], missing_evidence: [], checks: [], execution_expected_evidence: [] }));
    open();
    await screen.findByText("本日核对已通过");
    await waitFor(() => expect(screen.getByText(/其他日期、模型与策略条件仍需独立核验/)).toBeVisible());
    expect(api.confirmPortfolioReconciliation).not.toHaveBeenCalled();
  });

  it("通过标记与缺证据冲突时拒绝显示通过", async () => {
    vi.mocked(api.previewPortfolioReconciliation).mockResolvedValue(report({ zero_sum_check_passed: true, differences_found: false }));
    open();
    await screen.findByRole("alert");
    expect(screen.queryByText("本日核对已通过")).toBeNull();
  });

  it("本日历史及原订单保持同日期、报告版本，逐层返回", async () => {
    open();
    await screen.findByText("历史执行资料不完整");
    const evidenceButton = screen.getByRole("button", { name: "查看可关联原订单" });
    evidenceButton.focus();
    fireEvent.click(evidenceButton);
    await waitFor(() => expect(screen.getByTestId("evidence-scope")).toBeVisible());
    expect(screen.getByTestId("evidence-scope")).toHaveTextContent("2026-09-30");
    fireEvent.click(within(screen.getByTestId("evidence-scope")).getByRole("button", { name: "查看关联历史" }));
    await waitFor(() => expect(screen.getByTestId("history-scope")).toBeVisible());
    expect(screen.getByTestId("history-scope")).toHaveTextContent('"report_ref":"report-day30"');
    expect(screen.getByTestId("history-scope")).toHaveTextContent('"report_version":"v30"');
    fireEvent.click(within(screen.getByTestId("history-scope")).getByRole("button", { name: "返回原核对" }));
    await waitFor(() => expect(screen.getByTestId("evidence-scope")).toBeVisible());
    fireEvent.click(within(screen.getByTestId("evidence-scope")).getByRole("button", { name: "返回原核对" }));
    expect(evidenceButton).toHaveFocus();
    await waitFor(() => expect(screen.getByText("历史执行资料不完整")).toBeVisible());
    await waitFor(() => expect(evidenceButton).toHaveFocus());
  });
});
