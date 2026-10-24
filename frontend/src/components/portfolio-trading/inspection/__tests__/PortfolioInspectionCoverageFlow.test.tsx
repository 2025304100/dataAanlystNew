import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import PortfolioInspectionWorkbench from "../PortfolioInspectionWorkbench";

const { mockApi, toast } = vi.hoisted(() => ({ mockApi: {
  getPortfolioActionItems: vi.fn(), getPortfolioStatus: vi.fn(), getPortfolioInspectionRuntime: vi.fn(),
  getPortfolioInspectionDiagnostics: vi.fn(), getPortfolioModelBindingReview: vi.fn(), previewPortfolioReconciliation: vi.fn(),
  getPortfolioInspectionCoverage: vi.fn(), previewPortfolioInspectionCoverage: vi.fn(), acceptPortfolioInspectionCoverage: vi.fn(),
  getPortfolioCoverageActivationReview: vi.fn(), activatePortfolioCoverage: vi.fn(),
  confirmPortfolioReconciliation: vi.fn(), transitionPortfolioState: vi.fn(),
  getPortfolioInspectionRepairTasks: vi.fn(), getPortfolioInspectionJob: vi.fn(),
}, toast: vi.fn() }));
vi.mock("../../../../api/client", () => ({ api: mockApi }));
vi.mock("../../../../context/AppContext", () => ({ useApp: () => ({ showToast: toast, setActiveTab: vi.fn() }) }));

let accepted = false, activated = false;
const fingerprint = "a".repeat(64);
const gap = { trade_date_from: "2026-10-01", trade_date_to: "2026-10-21", reason: "历史执行配置经过恢复尝试仍无法还原",
  attempted_sources: ["原始备份"], evidence_refs: ["backup-job:real-ref"], verdict: "HISTORICAL_EVIDENCE_UNKNOWN", closure_type: "ARCHIVED_UNVERIFIABLE" };
function runtime() {
  return { portfolio_id: 1, current_state: activated ? "READY" : "RECONCILIATION_BLOCKED", generated_at: "2026-10-22T09:00:00Z", dimensions: {},
    permissions: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: !activated },
    capability_reasons: { allow_new_buys: [activated ? "新范围从2026-10-23开始，今日不在执行范围" : "新范围尚待本人启用"], allow_auto_recovery: ["按当前范围独立审核"], allow_risk_exits: [] },
    recovery_summary: { can_confirm: accepted && !activated, confirmation_action: accepted && !activated ? "INITIAL_COVERAGE_REVIEW" : null,
      blockers: accepted ? [] : ["旧期证据无法核验，需先登记缺口并核查当前资产"], checks: [
        { key: "reconciliation", passed: accepted, reason: accepted ? "已接受当前账户来源，新范围尚未核验任何日期" : "历史证据未知" },
        { key: "data", passed: true, reason: "当前数据已核验" }, { key: "binding", passed: true, reason: "当前绑定已接受" },
        { key: "runtime", passed: true, reason: "可审核启用新的范围，旧期继续未知" },
      ] } };
}
const activationReview = (allowed = true) => ({ portfolio_id: 1, can_confirm: allowed, baseline_id: "baseline-real", generation: 1,
  cursor_row_version: 1, evidence_fingerprint: fingerprint, coverage_start_trade_date: "2026-10-23", already_activated: false,
  blockers: allowed ? [] : [{ code: "ACCOUNT_EVIDENCE_CHANGED", message: "接受后账户资金来源已经变化，需重新核验" }],
  checks: [{ key: "account_sources", passed: allowed, reason: allowed ? "当前来源仍匹配本人接受快照" : "账户来源已变化" }] });

beforeEach(() => {
  sessionStorage.clear();
  vi.resetAllMocks();
  vi.spyOn(window,"scrollTo").mockImplementation(()=>{});
  mockApi.getPortfolioInspectionRepairTasks.mockResolvedValue({ portfolio_id: 1, items: [], next_cursor: null });
  mockApi.getPortfolioInspectionJob.mockResolvedValue({ portfolio_id: 1, enabled: true, reason: "按真实证据扫描", queue: { portfolio_id:1, days:[], limitations:[], verified_through:null } }); accepted = false; activated = false;
  mockApi.getPortfolioActionItems.mockResolvedValue({ portfolio_id: 1, items: [], summary: {} });
  mockApi.getPortfolioStatus.mockImplementation(() => Promise.resolve({ portfolio_id: 1, current_state: activated ? "READY" : "RECONCILIATION_BLOCKED", allowed_transitions: [] }));
  mockApi.getPortfolioInspectionRuntime.mockImplementation(() => Promise.resolve(runtime()));
  mockApi.getPortfolioInspectionDiagnostics.mockResolvedValue({ portfolio_id: 1, snapshot_id: "diagnostics-real", summary: {}, symbols: [], evidence: [],
    repair_plan: { plan_id: "no-repair-required", snapshot_id: "diagnostics-real", generated_at: "2026-10-22T09:00:00Z", operations: [], limitations: [] } });
  mockApi.getPortfolioModelBindingReview.mockResolvedValue({ portfolio_id: 1, can_confirm: false, blockers: [], before: null, after: null, change_summary: "当前绑定已核实" });
  // Old history remains incomplete throughout the entire acceptance/activation.
  mockApi.previewPortfolioReconciliation.mockResolvedValue({ portfolio_id: 1, trade_date: "2026-10-21", report_id: "old-unknown-report", report_version: "old-evidence",
    read_only: true, zero_sum_check_passed: false, items: [], missing_evidence: [{ code: "NOT_CHECKED", detail: "旧期历史证据未知" }] });
  mockApi.getPortfolioInspectionCoverage.mockImplementation(() => Promise.resolve({ portfolio_id: 1, cursor: accepted ? { id: "cursor-real", generation: 1,
    baseline_id: "baseline-real", coverage_start_trade_date: "2026-10-23", verified_through_trade_date: null, row_version: 1 } : null, baselines: [], historical_coverage_unknown: true }));
  mockApi.previewPortfolioInspectionCoverage.mockResolvedValue({ portfolio_id: 1, as_of_at: "2026-10-22T09:00:00Z", can_accept: true,
    coverage_start_trade_date: "2026-10-23", evidence_fingerprint: fingerprint, cursor_row_version: 0, current_generation: 0,
    snapshot: { currency: "CNY", cash: "8999.00", independent_expected_cash: "8999.00", positions: [], unresolved_obligation_count: 0 },
    source_refs: [{ source: "cash_ledger", available: true, row_count: 2, content_hash: fingerprint }], history_gaps: [gap], blockers: [], calendar_version: "calendar-real", policy_version: "sim-account-baseline-v1" });
  mockApi.acceptPortfolioInspectionCoverage.mockImplementation(() => { accepted = true; return Promise.resolve({ portfolio_id: 1, baseline_id: "baseline-real", cursor_id: "cursor-real",
    generation: 1, coverage_start_trade_date: "2026-10-23", verified_through_trade_date: null, audit_event_id: 9, content_hash: "b".repeat(64), history_gaps: [gap], runtime_enabled: false, trading_resumed: false }); });
  mockApi.getPortfolioCoverageActivationReview.mockResolvedValue(activationReview());
  mockApi.activatePortfolioCoverage.mockImplementation(() => { activated = true; return Promise.resolve({ portfolio_id: 1, baseline_id: "baseline-real", generation: 1, historical_coverage_unknown: true, reconciliation_passed: false }); });
});
afterEach(cleanup);

async function acceptBaseline() {
  render(<PortfolioInspectionWorkbench portfolioId={1} renderDualRun={() => null} />);
  fireEvent.click(await screen.findByRole("button", { name: "每日对账" }));
  fireEvent.click(screen.getByRole("button",{name:"查看并处理历史缺口"}));
  fireEvent.click(screen.getByText("资料确实无法恢复时"));
  fireEvent.click(screen.getByRole("button", { name: "查看历史缺口处置条件" }));
  fireEvent.click(screen.getByRole("button", { name: "已核查仍无法恢复，登记历史缺口并申请新基线" }));
  fireEvent.change(screen.getByLabelText("历史缺口起点"), { target: { value: "2026-10-01" } });
  fireEvent.change(screen.getByLabelText("历史缺口终点"), { target: { value: "2026-10-21" } });
  fireEvent.change(screen.getByLabelText("不可恢复原因"), { target: { value: gap.reason } });
  fireEvent.change(screen.getByLabelText("已尝试的证据来源"), { target: { value: "原始备份" } });
  fireEvent.change(screen.getByLabelText("保留的材料或任务引用"), { target: { value: "backup-job:real-ref" } });
  fireEvent.click(screen.getByRole("button", { name: "核查账户来源并预览" }));
  await screen.findByText("当前账户核验快照");
  fireEvent.change(screen.getByLabelText("新覆盖本人审核理由"), { target: { value: "本人已核实当前资金持仓与未决义务" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我确认当前材料/ }));
  fireEvent.click(screen.getByRole("button", { name: "本人接受新覆盖起点" }));
  await screen.findByRole("button", { name: "审核并启用新范围" });
}

it("moves baseline acceptance directly to step four and uses the separate initial-scope activation review", async () => {
  await acceptBaseline();
  const steps = screen.getByRole("navigation", { name: "恢复运行步骤" });
  expect(within(steps).getByRole("button", { name: /本人启用新范围/ })).toHaveAttribute("aria-current", "step");
  expect(mockApi.acceptPortfolioInspectionCoverage).toHaveBeenCalledTimes(1);
  expect(mockApi.activatePortfolioCoverage).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "审核并启用新范围" }));
  await waitFor(() => expect(mockApi.getPortfolioCoverageActivationReview).toHaveBeenCalledTimes(1));
  const modalTitle = await screen.findByText("审核并启用账户新覆盖范围");
  const modal = modalTitle.closest('[role="dialog"]') as HTMLElement;
  await waitFor(() => expect(within(modal).getByText("仅启用第 1 代 · 从 2026-10-23 开始")).toBeVisible());
  expect(within(modal).getByText("账户基线 / 指纹")).toBeVisible();
  expect(within(modal).queryByText("old-unknown-report")).not.toBeInTheDocument();
  expect(within(modal).getByText("旧期间保持历史未知。本次不会生成历史通过记录，也不会推进任何已核验日期。")).toBeVisible();
  const submit = within(modal).getByRole("button", { name: "确认本次审核" });
  expect(submit).toBeDisabled();
  fireEvent.change(within(modal).getByRole("textbox"), { target: { value: "本人审核从新覆盖起点启用并保留旧期间未知" } });
  fireEvent.click(within(modal).getByRole("checkbox")); fireEvent.click(submit);
  await waitFor(() => expect(mockApi.activatePortfolioCoverage).toHaveBeenCalledWith(1, expect.objectContaining({
    baseline_id: "baseline-real", generation: 1, expected_cursor_row_version: 1,
    expected_evidence_fingerprint: fingerprint, acknowledge_new_scope: true,
  })));
  expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
  expect(mockApi.transitionPortfolioState).not.toHaveBeenCalled();
  await waitFor(() => expect(toast).toHaveBeenCalledWith("success", expect.stringContaining("已保存本次审核")));
});

it("blocks the separate activation if revalidated account sources changed after baseline acceptance", async () => {
  mockApi.getPortfolioCoverageActivationReview.mockResolvedValue(activationReview(false));
  await acceptBaseline();
  fireEvent.click(screen.getByRole("button", { name: "审核并启用新范围" }));
  await waitFor(() => expect(mockApi.getPortfolioCoverageActivationReview).toHaveBeenCalledTimes(1));
  const modalTitle = await screen.findByText("审核并启用账户新覆盖范围");
  const modal = modalTitle.closest('[role="dialog"]') as HTMLElement;
  await waitFor(() => expect(within(modal).getByText("接受后账户资金来源已经变化，需重新核验")).toBeVisible());
  fireEvent.change(within(modal).getByRole("textbox"), { target: { value: "即使写够理由也不能沿用已经变化的账户来源" } });
  fireEvent.click(within(modal).getByRole("checkbox"));
  expect(within(modal).getByRole("button", { name: "确认本次审核" })).toBeDisabled();
  expect(mockApi.activatePortfolioCoverage).not.toHaveBeenCalled();
});
