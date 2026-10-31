import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const { mockApi, app } = vi.hoisted(() => ({
  mockApi: {
    getPortfolioStatus: vi.fn(),
    getPortfolioActionItems: vi.fn(),
    getG7OperationalStatus: vi.fn(),
    triggerPortfolioReconcile: vi.fn(),
    listPortfolioAuditEvents: vi.fn(),
    getPortfolioModelBindingReview: vi.fn(),
    confirmPortfolioModelBinding: vi.fn(),
    getPortfolioInspectionRuntime: vi.fn(),
    getPortfolioInspectionJob: vi.fn(),
    getPortfolioInspectionDiagnostics: vi.fn(),
    previewPortfolioReconciliation: vi.fn(),
    getPortfolioInspectionHistory: vi.fn(),
    getPortfolioInspectionHistorySecurities: vi.fn(),
  },
  app: {
    portfolios: [{ id: 1, name: "组合一", account_type: "simulated", auto_trade_enabled: 1, portfolio_status: "READY" }],
    portfolioId: 1,
    activeSubTab: "governance",
    setActiveSubTab: vi.fn(),
    switchPortfolio: vi.fn(),
    setActiveTab: vi.fn(),
    loadWorkbench: vi.fn(),
    showToast: vi.fn(),
  },
}));

vi.mock("../../../api/client", () => ({ api: mockApi }));
vi.mock("../../../context/AppContext", () => ({ useApp: () => app }));
vi.mock("antd", async () => ({
  ...await vi.importActual<typeof import("antd")>("antd"),
  ConfigProvider: ({ children }: { children?: ReactNode }) => <>{children}</>,
  Tooltip: ({ children }: { children: ReactNode }) => <>{children}</>,
  Button: ({ children, onClick, disabled, loading: _l, danger: _d, icon: _i, size: _s, block: _b, type: _t, ...rest }: any) => (
    <button type="button" onClick={onClick} disabled={disabled} {...rest}>{children}</button>
  ),
}));
vi.mock("../PortfolioOverview", () => ({ default: () => <div data-testid="portfolio-overview">组合总览</div> }));
vi.mock("../PortfolioMembersTable", () => ({ default: () => <div>持仓成员</div> }));
vi.mock("../PortfolioStrategyRules", () => ({ default: () => <div>组合策略</div> }));
vi.mock("../PortfolioBacktestCenter", () => ({ default: () => <div>回测中心</div> }));
vi.mock("../CreatePortfolioModal", () => ({ default: () => null }));
vi.mock("../AddCandidateModal", () => ({ default: () => null }));
vi.mock("../PortfolioRankingDrawer", () => ({ default: () => null }));
vi.mock("../PortfolioReviewDrawer", () => ({ default: () => null }));
vi.mock("../PortfolioSelectorDropdown", () => ({ default: () => null }));
vi.mock("../ReadinessRepairDrawer", () => ({
  default: ({ open, onRefresh }: { open: boolean; onRefresh: () => void }) => open ? (
    <button onClick={onRefresh}>模拟修复完成</button>
  ) : null,
}));

import PortfolioTradingShell from "../PortfolioTradingShell";
import { setLocale } from "../../../i18n";
import { requestPortfolioSubtab } from "../../../utils/navigate";

const readyItems = {
  portfolio_id: 1,
  items: [],
  summary: { blockers: 0, warnings: 0, info: 0, ready: true, waiting_for_signal: false },
  checks: { enabled: true, account_ready: true, data_ready: true, source_ready: true, schedule_ready: true },
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  sessionStorage.clear();
  vi.clearAllMocks();
  setLocale("zh-CN");
  app.portfolioId = 1;
  app.activeSubTab = "governance";
  app.portfolios = [{ id: 1, name: "组合一", account_type: "simulated", auto_trade_enabled: 1, portfolio_status: "READY" }];
  app.loadWorkbench.mockResolvedValue(undefined);
  mockApi.getPortfolioStatus.mockImplementation(async (id: number) => ({ portfolio_id: id, current_state: "READY", allowed_transitions: ["ADMIN_PAUSED"] }));
  mockApi.getPortfolioActionItems.mockResolvedValue(readyItems);
  mockApi.getPortfolioInspectionJob.mockImplementation(async (id:number)=>({portfolio_id:id,enabled:false,status:"idle",task_id:null,queue:{portfolio_id:id,days:[],limitations:[]}}));
  mockApi.getG7OperationalStatus.mockResolvedValue({ operational_blockers: [], resume_blockers: [], pending_orders_by_status: {} });
  mockApi.triggerPortfolioReconcile.mockResolvedValue({ items: [], differences_found: false, zero_sum_check_passed: true });
  mockApi.listPortfolioAuditEvents.mockResolvedValue({ items: [], total: 0 });
  mockApi.getPortfolioInspectionRuntime.mockImplementation(async (id: number) => ({
    portfolio_id: id, generated_at: "2026-10-22T09:00:00+08:00", current_state: "READY",
    permissions: { allow_new_buys: true, allow_risk_exits: true, allow_auto_recovery: true, requires_manual_ack: false },
    capability_reasons: {}, dimensions: {},
    recovery_summary: { can_confirm: false, blockers: [], review_required: false, checks: [
      { key: "reconciliation", passed: true, reason: "当前对账已核实" },
      { key: "data", passed: true, reason: "数据已核实" },
      { key: "binding", passed: true, reason: "当前绑定已核实" },
    ] },
  }));
  mockApi.getPortfolioInspectionDiagnostics.mockImplementation(async (id: number) => ({
    portfolio_id: id, snapshot_id: `diagnostics-${id}`, version: 1, generated_at: "2026-10-22T09:00:00+08:00",
    policy_version: "test-1", read_only: true, usable_for_admission: false, summary: {}, binding: {}, calendars: [],
    symbols: [], evidence: [], limitations: [],
    repair_plan: { portfolio_id: id, plan_id: `plan-${id}`, snapshot_id: `diagnostics-${id}`, version: 1,
      generated_at: "2026-10-22T09:00:00+08:00", expires_at: "2026-10-22T10:00:00+08:00",
      executable_count: 0, disabled_count: 0, operations: [], confirmation_required: true,
      completion_criteria: [], limitations: [] },
  }));
  mockApi.previewPortfolioReconciliation.mockImplementation(async (id: number) => ({
    portfolio_id: id, trade_date: "2026-10-21", read_only: true, items: [],
    differences_found: false, zero_sum_check_passed: true,
  }));
  mockApi.getPortfolioModelBindingReview.mockResolvedValue({
    can_confirm: false, requires_confirmation: false, before: null, after: null, blockers: [], change_summary: "当前没有待审核的绑定变更",
  });
  mockApi.getPortfolioInspectionHistory.mockResolvedValue({
    items: [], total: 0, page: 1, page_size: 20, page_count: 0, has_more: false,
    snapshot_event_id: 0, filters: {}, relation_status: "not_requested", read_only: true,
  });
  mockApi.getPortfolioInspectionHistorySecurities.mockResolvedValue({ items: [], total: 0 });
});
afterEach(cleanup);

describe("组合主壳与治理子页的真实导航", () => {
  it("点击 运行状态审核 后渲染治理目标页", async () => {
    render(<PortfolioTradingShell />);
    fireEvent.click(await screen.findByRole("button", { name: "运行状态审核" }));
    expect(await screen.findByRole("heading", { name: "运行状态审核", level: 2 })).toBeInTheDocument();
    expect(screen.queryByTestId("portfolio-overview")).not.toBeInTheDocument();
  });

  it("从「对账连续性」进入执行与对账", async () => {
    render(<PortfolioTradingShell />);
    // 每日核对入口已统一到「对账连续性」：无逐日行时走空态兜底，有行时点第一行
    fireEvent.click(await screen.findByRole("tab", { name: "对账连续性" }));
    const fallback = screen.queryByRole("button", { name: "打开对账" });
    fireEvent.click(fallback ?? screen.getAllByRole("button", { name: /每日对账$/ })[0]);
    expect(await screen.findByRole("heading", { name: "执行与对账", level: 2 })).toBeInTheDocument();
    expect(screen.queryByTestId("portfolio-overview")).not.toBeInTheDocument();
  });

  it("点击审计记录后读取并显示当前组合的处理历史", async () => {
    render(<PortfolioTradingShell />);
    fireEvent.click(await screen.findByRole("button", { name: "审计记录" }));
    expect(await screen.findByRole("heading", { name: "处理记录与审核历史" })).toBeInTheDocument();
    await waitFor(() => expect(mockApi.getPortfolioInspectionHistory).toHaveBeenCalledWith(1, expect.any(Object)));
    expect(screen.queryByTestId("portfolio-overview")).not.toBeInTheDocument();
  });

  it("从组合总览首次进入时，治理深链直接落到运行状态审核引导", async () => {
    app.activeSubTab = "overview";
    render(<PortfolioTradingShell />);
    expect(screen.getByTestId("portfolio-overview")).toBeInTheDocument();
    act(() => requestPortfolioSubtab("governance:state"));
    expect(await screen.findByRole("heading", { name: "运行状态审核", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "恢复运行步骤" })).toBeInTheDocument();
    expect(screen.queryByTestId("portfolio-overview")).not.toBeInTheDocument();
  });

  it("治理已挂载时，后续深链仍能切到每日对账", async () => {
    render(<PortfolioTradingShell />);
    await screen.findByRole("button", { name: "运行状态审核" });
    act(() => requestPortfolioSubtab("governance:state"));
    await screen.findByRole("heading", { name: "运行状态审核", level: 2 });
    act(() => requestPortfolioSubtab("governance:reconciliation"));
    expect(await screen.findByTestId("reconcile-panel")).toBeInTheDocument();
  });
});

describe("修复后的状态同步", () => {
  it("治理总览本人接受模型绑定后，主壳权限和顶栏立即更新", async () => {
    let accepted = false;
    mockApi.getPortfolioStatus.mockImplementation(async () => ({ portfolio_id: 1, current_state: accepted ? "READY" : "MODEL_INACTIVE" }));
    mockApi.getPortfolioActionItems.mockImplementation(async () => accepted ? readyItems : ({
      ...readyItems,
      items: [{
        id: "model-binding",
        source: "hg1_status",
        code: "MODEL_INACTIVE",
        severity: "blocker",
        title: "模型绑定等待本人核验",
        impact: "本人接受变更后才能恢复新买入",
        sources: ["hg1_status"],
        primary_action: { action_type: "open-modal", target: "model-binding-review" },
      }],
      summary: { ...readyItems.summary, ready: false, blockers: 1 },
    }));
    mockApi.getPortfolioModelBindingReview.mockResolvedValue({
      can_confirm: true, requires_confirmation: true, snapshot_id: "snapshot-new", model_run_id: "model-new",
      change_summary: "策略和模型绑定已变更，请本人核验", before: null,
      after: { snapshot_id: "snapshot-new", snapshot_no: 2, model_run_id: "model-new", model_name: "已验证模型" },
      blockers: [],
    });
    mockApi.confirmPortfolioModelBinding.mockImplementation(async () => {
      accepted = true;
      return { model_confirmed: true };
    });
    render(<PortfolioTradingShell />);
    await screen.findByText("模型绑定等待本人核验");
    expect(screen.getByRole("button", { name: "添加候选标的" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /1 项待处理/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "审核策略绑定" }));
    fireEvent.click(await screen.findByRole("button", { name: "审核并确认当前绑定" }));
    const confirmation = await screen.findByRole("dialog");
    await waitFor(() => expect(within(confirmation).getByText("确认本次实际运行绑定")).toBeVisible());
    fireEvent.click(within(confirmation).getByRole("checkbox", { name: "我已核对当前范围、报告与版本，确认本次处理。材料变化后需重新审核。" }));
    fireEvent.change(within(confirmation).getByRole("textbox", { name: "审核理由（至少 10 个字）" }), { target: { value: "本人已核验策略快照和模型变更" } });
    fireEvent.click(within(confirmation).getByRole("button", { name: "确认本次审核" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "添加候选标的" })).toBeEnabled());
    expect(screen.getByRole("button", { name: /已就绪，等待调度/ })).toBeInTheDocument();
    expect(app.loadWorkbench).toHaveBeenCalledTimes(1);
    expect(mockApi.confirmPortfolioModelBinding).toHaveBeenCalledWith(1, {
      snapshot_id: "snapshot-new", model_run_id: "model-new", ack: true, review_note: "本人已核验策略快照和模型变更",
    });
  });

  it("修复完成刷新行动项、HG1权限和组合列表", async () => {
    app.activeSubTab = "overview";
    mockApi.getPortfolioStatus.mockResolvedValueOnce({ portfolio_id: 1, current_state: "RECONCILIATION_BLOCKED" });
    mockApi.getPortfolioActionItems.mockResolvedValueOnce({ ...readyItems, summary: { ...readyItems.summary, ready: false, blockers: 1 } });
    render(<PortfolioTradingShell />);
    await waitFor(() => expect(screen.getByRole("button", { name: "添加候选标的" })).toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: /自动接管/ }));
    fireEvent.click(await screen.findByRole("button", { name: "模拟修复完成" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "添加候选标的" })).toBeEnabled());
    expect(mockApi.getPortfolioActionItems).toHaveBeenCalledTimes(2);
    expect(mockApi.getPortfolioStatus).toHaveBeenCalledTimes(2);
    expect(app.loadWorkbench).toHaveBeenCalledTimes(1);
  });

  it("切换组合后，旧组合的状态和行动项响应不能覆盖新组合", async () => {
    app.activeSubTab = "overview";
    const oldStatus = deferred<unknown>();
    const oldItems = deferred<unknown>();
    mockApi.getPortfolioStatus.mockReturnValueOnce(oldStatus.promise);
    mockApi.getPortfolioActionItems.mockReturnValueOnce(oldItems.promise);
    const view = render(<PortfolioTradingShell />);
    app.portfolioId = 2;
    app.portfolios = [...app.portfolios, { id: 2, name: "组合二", account_type: "simulated", auto_trade_enabled: 1, portfolio_status: "READY" }];
    mockApi.getPortfolioActionItems.mockResolvedValue({ ...readyItems, portfolio_id: 2 });
    view.rerender(<PortfolioTradingShell />);
    await waitFor(() => expect(screen.getByRole("button", { name: "添加候选标的" })).toBeEnabled());
    await act(async () => {
      oldStatus.resolve({ portfolio_id: 1, current_state: "RECONCILIATION_BLOCKED" });
      oldItems.resolve({ ...readyItems, summary: { ...readyItems.summary, ready: false, blockers: 3 } });
    });
    expect(screen.getByRole("button", { name: "添加候选标的" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /3 项待处理/ })).not.toBeInTheDocument();
    expect(screen.getByText("组合二")).toBeInTheDocument();
  });

  it("同一组合先发出的慢请求不能覆盖修复后的新结果", async () => {
    app.activeSubTab = "overview";
    const oldStatus = deferred<unknown>();
    const oldItems = deferred<unknown>();
    mockApi.getPortfolioStatus.mockReturnValueOnce(oldStatus.promise);
    mockApi.getPortfolioActionItems.mockReturnValueOnce(oldItems.promise);
    render(<PortfolioTradingShell />);
    fireEvent.click(screen.getByRole("button", { name: /自动接管/ }));
    fireEvent.click(await screen.findByRole("button", { name: "模拟修复完成" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "添加候选标的" })).toBeEnabled());
    await act(async () => {
      oldStatus.resolve({ portfolio_id: 1, current_state: "RECONCILIATION_BLOCKED" });
      oldItems.resolve({ ...readyItems, summary: { ...readyItems.summary, ready: false, blockers: 3 } });
    });
    expect(screen.getByRole("button", { name: "添加候选标的" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /3 项待处理/ })).not.toBeInTheDocument();
  });
});

describe("自动接管胶囊展示口径", () => {
  it("就绪的空闲组合显示等待调度", async () => {
    app.activeSubTab = "overview";
    render(<PortfolioTradingShell />);
    expect(await screen.findByRole("button", { name: /已就绪，等待调度/ })).toBeInTheDocument();
  });

  it("无买入信号是正常等待，不声称门禁已经通过", async () => {
    app.activeSubTab = "overview";
    mockApi.getPortfolioActionItems.mockResolvedValue({ ...readyItems, summary: { ...readyItems.summary, ready: false, waiting_for_signal: true } });
    render(<PortfolioTradingShell />);
    expect(await screen.findByRole("button", { name: /等待买入信号/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /待处理|未就绪/ })).not.toBeInTheDocument();
  });

  it("只有实际自动执行态显示执行中", async () => {
    app.activeSubTab = "overview";
    mockApi.getPortfolioStatus.mockResolvedValue({ portfolio_id: 1, current_state: "RUNNING_AUTO_SIMULATION" });
    mockApi.getPortfolioActionItems.mockResolvedValue({ ...readyItems, summary: { ...readyItems.summary, ready: false, waiting_for_signal: true, execution_state: "running" } });
    render(<PortfolioTradingShell />);
    expect(await screen.findByRole("button", { name: /执行中/ })).toBeInTheDocument();
  });

  it("即使ready误报，阻断项也优先显示待处理", async () => {
    app.activeSubTab = "overview";
    mockApi.getPortfolioActionItems.mockResolvedValue({ ...readyItems, summary: { ...readyItems.summary, blockers: 2 } });
    render(<PortfolioTradingShell />);
    expect(await screen.findByRole("button", { name: /2 项待处理/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /已就绪，等待调度/ })).not.toBeInTheDocument();
  });
});


it("持久化 READY 但当日评分过期时，顶部与检查页都暂停新买入",async()=>{
  mockApi.getPortfolioInspectionRuntime.mockImplementation(async(id:number)=>({
    portfolio_id:id,current_state:"READY",generated_at:new Date().toISOString(),dimensions:{},
    permissions:{allow_new_buys:false,allow_auto_recovery:false,allow_risk_exits:true,requires_manual_ack:false},
    capability_reasons:{allow_new_buys:["评分落后两个交易日"]},
    recovery_summary:{can_confirm:false,phase:"BLOCKED",recommended_action:"VIEW_BLOCKERS",review_required:false,
      blockers:["评分落后两个交易日"],checks:[{key:"data",passed:false,reason:"评分落后两个交易日"}]}
  }));
  render(<PortfolioTradingShell/>);
  expect(await screen.findByRole("heading",{name:"新买入已暂停",level:1})).toBeInTheDocument();
  expect(screen.getByRole("button",{name:/^组合一 模拟/})).toHaveTextContent("新买入已暂停");
  expect(screen.queryByText("可以运行")).not.toBeInTheDocument();
});
