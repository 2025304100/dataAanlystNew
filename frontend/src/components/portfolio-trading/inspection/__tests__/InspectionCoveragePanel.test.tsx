import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import InspectionCoveragePanel from "../InspectionCoveragePanel";
import type { CoverageAccepted, CoveragePreview, InspectionCoverageApi } from "../coverageTypes";

afterEach(cleanup);

const gap = { trade_date_from: "2026-10-01", trade_date_to: "2026-10-21", reason: "历史配置证据经过恢复尝试仍无法还原",
  attempted_sources: ["旧备份"], evidence_refs: ["backup-job:1"], verdict: "HISTORICAL_EVIDENCE_UNKNOWN" as const, closure_type: "ARCHIVED_UNVERIFIABLE" as const };
const preview = (canAccept = true): CoveragePreview => ({ portfolio_id: 1, as_of_at: "2026-10-22T09:00:00Z", can_accept: canAccept,
  coverage_start_trade_date: "2026-10-23", evidence_fingerprint: "a".repeat(64), cursor_row_version: 0, current_generation: 0,
  snapshot: { currency: "CNY", cash: "8999.00", independent_expected_cash: "8999.00", positions: [{ symbol_id: 1, security_id: "CN:000001", name: "平安银行", quantity: "100", avg_cost: "10.00" }], unresolved_obligation_count: canAccept ? 0 : null },
  source_refs: [{ source: "cash_ledger", available: true, row_count: 2, content_hash: "b".repeat(64) }],
  history_gaps: [gap], blockers: canAccept ? [] : [{ code: "OUTSTANDING_ORDER", message: "存在未完成订单，当前义务尚未核清" }],
  calendar_version: "c".repeat(64), policy_version: "sim-account-baseline-v1", runtime_enabled: false, acceptance_does_not_resume_trading: true,
});
const accepted: CoverageAccepted = { portfolio_id: 1, baseline_id: "baseline-1", cursor_id: "cursor-1", generation: 1,
  coverage_start_trade_date: "2026-10-23", verified_through_trade_date: null, audit_event_id: 7, content_hash: "d".repeat(64),
  history_gaps: [gap], idempotent: false, runtime_enabled: false, trading_resumed: false };
function fixture(): InspectionCoverageApi {
  return { getPortfolioInspectionCoverage: vi.fn().mockImplementation(pid => Promise.resolve({ portfolio_id: pid, cursor: null, baselines: [], historical_coverage_unknown: true })),
    previewPortfolioInspectionCoverage: vi.fn().mockResolvedValue(preview()), acceptPortfolioInspectionCoverage: vi.fn().mockResolvedValue(accepted) };
}
function fillForm() {
  fireEvent.click(screen.getByRole("button", { name: "已核查仍无法恢复，登记历史缺口并申请新基线" }));
  fireEvent.change(screen.getByLabelText("历史缺口起点"), { target: { value: "2026-10-01" } });
  fireEvent.change(screen.getByLabelText("历史缺口终点"), { target: { value: "2026-10-21" } });
  fireEvent.change(screen.getByLabelText("不可恢复原因"), { target: { value: gap.reason } });
  fireEvent.change(screen.getByLabelText("已尝试的证据来源"), { target: { value: "旧备份" } });
  fireEvent.change(screen.getByLabelText("保留的材料或任务引用"), { target: { value: "backup-job:1" } });
}

it("keeps a coverage application in a drawer and preserves its unsubmitted draft when closed",async()=>{
  const coverageApi=fixture();
  render(<InspectionCoveragePanel portfolioId={1} coverageApi={coverageApi}/>);
  fillForm();
  expect(screen.getByRole("heading",{name:"登记历史缺口与核验新覆盖申请"})).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"返回历史证据核查"}));
  expect(screen.queryByRole("button",{name:"核查账户来源并预览"})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"已核查仍无法恢复，登记历史缺口并申请新基线"}));
  expect(screen.getByLabelText("历史缺口起点")).toHaveValue("2026-10-01");
  expect(screen.getByLabelText("不可恢复原因")).toHaveValue(gap.reason);
  expect(coverageApi.previewPortfolioInspectionCoverage).not.toHaveBeenCalled();
  expect(coverageApi.acceptPortfolioInspectionCoverage).not.toHaveBeenCalled();
});

it("shows the verified asset snapshot and requires personal reason and acknowledgment before accepting", async () => {
  const coverageApi = fixture(), onAccepted = vi.fn();
  render(<InspectionCoveragePanel portfolioId={1} coverageApi={coverageApi} onAccepted={onAccepted} />);
  fillForm(); fireEvent.click(screen.getByRole("button", { name: "核查账户来源并预览" }));
  expect(await screen.findByText("当前账户核验快照")).toBeVisible();
  expect(screen.getByText("平安银行")).toBeVisible();
  const accept = screen.getByRole("button", { name: "本人接受新覆盖起点" });
  expect(accept).toBeDisabled(); expect(coverageApi.acceptPortfolioInspectionCoverage).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("新覆盖本人审核理由"), { target: { value: "本人逐项核验当前资产并承担后续核对责任" } });
  fireEvent.click(screen.getByRole("checkbox"));
  expect(accept).toBeEnabled(); fireEvent.click(accept);
  expect(await screen.findByText("已接受第 1 代覆盖起点 2026-10-23")).toBeVisible();
  expect(coverageApi.acceptPortfolioInspectionCoverage).toHaveBeenCalledWith(1, expect.objectContaining({
    coverage_start_trade_date: "2026-10-23", expected_evidence_fingerprint: "a".repeat(64), expected_cursor_row_version: 0,
    acknowledge_history_unknown: true, history_gaps: [gap],
  }));
  expect(onAccepted).toHaveBeenCalledWith(accepted);
  expect(screen.getByText(/新范围尚未核验任何交易日，旧期间仍为历史证据未知/)).toBeVisible();
});

it("keeps acceptance unavailable when current account obligations cannot be verified", async () => {
  const coverageApi = fixture(), repair = vi.fn();
  vi.mocked(coverageApi.previewPortfolioInspectionCoverage).mockResolvedValue(preview(false));
  render(<InspectionCoveragePanel portfolioId={1} coverageApi={coverageApi} onRepairAccount={repair} />);
  fillForm(); fireEvent.click(screen.getByRole("button", { name: "核查账户来源并预览" }));
  expect(await screen.findByText("暂不能重建基线")).toBeVisible();
  expect(screen.getByText("存在未完成订单，当前义务尚未核清")).toBeVisible();
  expect(screen.queryByRole("button", { name: "本人接受新覆盖起点" })).not.toBeInTheDocument();
  expect(coverageApi.acceptPortfolioInspectionCoverage).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "返回账户证据修复" })); expect(repair).toHaveBeenCalledTimes(1);
});

it("clears the old preview on portfolio change and ignores its late response", async () => {
  const coverageApi = fixture(); let resolve!: (value: CoveragePreview) => void;
  vi.mocked(coverageApi.previewPortfolioInspectionCoverage).mockReturnValue(new Promise(res => { resolve = res; }));
  const view = render(<InspectionCoveragePanel portfolioId={1} coverageApi={coverageApi} />);
  fillForm(); fireEvent.click(screen.getByRole("button", { name: "核查账户来源并预览" }));
  await waitFor(() => expect(coverageApi.previewPortfolioInspectionCoverage).toHaveBeenCalledTimes(1));
  view.rerender(<InspectionCoveragePanel portfolioId={2} coverageApi={coverageApi} />);
  await act(async () => resolve(preview()));
  expect(screen.queryByText("当前账户核验快照")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "已核查仍无法恢复，登记历史缺口并申请新基线" }));
  expect(screen.getByLabelText("历史缺口起点")).toHaveValue("");
  expect(coverageApi.acceptPortfolioInspectionCoverage).not.toHaveBeenCalled();
});
