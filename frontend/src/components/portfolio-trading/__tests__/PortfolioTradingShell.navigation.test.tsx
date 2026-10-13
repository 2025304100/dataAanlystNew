import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
vi.mock("antd", () => ({
  Tooltip: ({ children }: { children: ReactNode }) => <>{children}</>,
  Button: ({ children, onClick, disabled }: { children?: ReactNode; onClick?: () => void; disabled?: boolean }) => (
    <button type="button" onClick={onClick} disabled={disabled}>{children}</button>
  ),
  Modal: ({ open, children }: { open?: boolean; children?: ReactNode }) => open ? <div>{children}</div> : null,
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
  vi.clearAllMocks();
  setLocale("zh-CN");
  app.portfolioId = 1;
  app.activeSubTab = "governance";
  app.portfolios = [{ id: 1, name: "组合一", account_type: "simulated", auto_trade_enabled: 1, portfolio_status: "READY" }];
  app.loadWorkbench.mockResolvedValue(undefined);
  mockApi.getPortfolioStatus.mockImplementation(async (id: number) => ({ portfolio_id: id, current_state: "READY", allowed_transitions: ["ADMIN_PAUSED"] }));
  mockApi.getPortfolioActionItems.mockResolvedValue(readyItems);
  mockApi.getG7OperationalStatus.mockResolvedValue({ operational_blockers: [], resume_blockers: [], pending_orders_by_status: {} });
  mockApi.triggerPortfolioReconcile.mockResolvedValue({ items: [], differences_found: false, zero_sum_check_passed: true });
  mockApi.listPortfolioAuditEvents.mockResolvedValue({ items: [], total: 0 });
});
afterEach(cleanup);

describe("组合主壳与治理子页的真实导航", () => {
  it.each([
    ["去每日对账", "reconcile-panel"],
    ["调整运行状态", "g7-ops-disclosure"],
  ])("点击 %s 后渲染治理目标页", async (label, testId) => {
    render(<PortfolioTradingShell />);
    fireEvent.click(await screen.findByRole("button", { name: label }));
    expect(await screen.findByTestId(testId)).toBeInTheDocument();
    expect(screen.queryByTestId("portfolio-overview")).not.toBeInTheDocument();
  });

  it("点击查审计记录后渲染治理审计表", async () => {
    render(<PortfolioTradingShell />);
    fireEvent.click(await screen.findByRole("button", { name: "查审计记录" }));
    expect(await screen.findByRole("heading", { name: "治理审计事件" })).toBeInTheDocument();
    expect(screen.queryByTestId("portfolio-overview")).not.toBeInTheDocument();
  });

  it("从组合总览首次进入时，治理深链直接落到运行状态", async () => {
    app.activeSubTab = "overview";
    render(<PortfolioTradingShell />);
    expect(screen.getByTestId("portfolio-overview")).toBeInTheDocument();
    act(() => requestPortfolioSubtab("governance:state"));
    expect(await screen.findByTestId("g7-ops-disclosure")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "去每日对账" })).not.toBeInTheDocument();
  });

  it("治理已挂载时，后续深链仍能切到每日对账", async () => {
    render(<PortfolioTradingShell />);
    await screen.findByRole("button", { name: "调整运行状态" });
    act(() => requestPortfolioSubtab("governance:state"));
    await screen.findByTestId("g7-ops-disclosure");
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
    fireEvent.click(screen.getByRole("button", { name: "查看并确认变更" }));
    fireEvent.click(await screen.findByLabelText("我已查看变更，确认接受本组合的新绑定"));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "本人已核验策略快照和模型变更" } });
    fireEvent.click(screen.getByRole("button", { name: "确认接受变更" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "添加候选标的" })).toBeEnabled());
    expect(screen.getByRole("button", { name: /已就绪，等待调度/ })).toBeInTheDocument();
    expect(app.loadWorkbench).toHaveBeenCalledTimes(1);
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
