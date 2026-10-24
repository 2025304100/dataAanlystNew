import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

const { mockApi, mockToast } = vi.hoisted(() => ({
  mockApi: {
    getPortfolioStatus: vi.fn(),
    getG7OperationalStatus: vi.fn(),
    getG6Readiness:vi.fn(),startG6Rollout:vi.fn(),
    getPortfolioActionItems: vi.fn(),
  },
  mockToast: vi.fn(),
}));

vi.mock("../../../api/client", () => ({ api: mockApi }));
vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ showToast: mockToast }),
}));
vi.mock("../../../i18n", () => ({
  t: (key: string) => ({
    "portfolioTrading.governance.subtab.state": "运行状态",
    "governance.g7.title": "运行保障",
    "governance.g7.readyForExpansion": "可扩大范围",
    "governance.g7.newBuysStopped": "新买入已停止",
    "governance.g7.canResume": "可解除暂停",
    "governance.g7.pendingOrders": "待处理订单",
    "governance.g7.activeAlerts": "未确认告警",
    "governance.g7.blockers": "扩大范围前须处理",
    "governance.g7.resumeBlockers": "恢复前须处理",
    "governance.g7.status.partial": "部分成交待处理",
    "governance.g7.status.pending_confirmation": "待人工确认",
    "common.refresh": "刷新",
  }[key] ?? key),
  template: (key: string) => key,
}));


import PortfolioGovernanceTab from "../PortfolioGovernanceTab";

describe("PortfolioGovernanceTab G7 operations", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.getPortfolioStatus.mockResolvedValue({
      current_state: "READY",
      allowed_transitions: ["ADMIN_PAUSED"],
    });
    mockApi.getPortfolioActionItems.mockResolvedValue({
      items: [],
      summary: { blockers: 0, warnings: 0, info: 0, ready: true },
      checks: null,
    });
    mockApi.getG7OperationalStatus.mockResolvedValue({
      portfolio_id: 7,
      current_state: "ADMIN_PAUSED",
      g6_eligible: false,
      g5_report_id: 12,
      can_stop_new_buys: false,
      new_buys_stopped: true,
      pending_order_count: 2,
      pending_orders_by_status: { partial: 1, pending_confirmation: 1 },
      active_alert_count: 1,
      ready_for_expansion: false,
      can_resume: false,
      operational_blockers: ["缺少已归档 G5 双跑报告"],
      resume_blockers: ["存在 1 条未确认告警"],
    });
  });

  it("loads the G7 control-room facts and translates pending-order statuses", async () => {
    render(<PortfolioGovernanceTab portfolioId={7} />);

    fireEvent.click(screen.getByText("引擎专项验收 · 切换引擎前使用"));
    fireEvent.click(screen.getByRole("button", { name: "引擎运行与切换" }));

    await waitFor(() => {
      expect(mockApi.getG7OperationalStatus).toHaveBeenCalledWith(7);
    });
    const panel = await screen.findByTestId("g7-operational-status");
    expect(panel).toHaveTextContent("新买入已停止");
    expect(panel).toHaveTextContent("部分成交待处理");
    expect(panel).toHaveTextContent("待人工确认");
    expect(panel).toHaveTextContent("缺少已归档 G5 双跑报告");
    expect(panel).toHaveTextContent("存在 1 条未确认告警");
    expect(panel).not.toHaveTextContent("pending_confirmation");
  });

  it("provides a read-only retry when the initial engine status request fails",async()=>{
    mockApi.getG7OperationalStatus.mockRejectedValueOnce(new Error("引擎运行状态暂时不可读"));
    render(<PortfolioGovernanceTab portfolioId={7}/>);fireEvent.click(screen.getByText("引擎专项验收 · 切换引擎前使用"));fireEvent.click(screen.getByRole("button",{name:"引擎运行与切换"}));
    fireEvent.click(await screen.findByRole("button",{name:"重新读取引擎运行状态"}));
    expect(await screen.findByTestId("g7-operational-status")).toHaveTextContent("新买入已停止");expect(mockApi.startG6Rollout).not.toHaveBeenCalled();
  });

  it("opens engine switching review in a modal and retains its reason after cancellation without applying",async()=>{
    mockApi.getG7OperationalStatus.mockResolvedValue({portfolio_id:7,current_state:"READY",g6_eligible:true,ready_for_expansion:true,operational_blockers:[],resume_blockers:[],pending_orders_by_status:{},pending_order_count:0,active_alert_count:0});
    mockApi.getG6Readiness.mockResolvedValue({eligible:true,current_source_mode:"old",g5_report_id:12,g5_report_hash:"verified-hash",blockers:[]});
    render(<PortfolioGovernanceTab portfolioId={7}/>);
    fireEvent.click(screen.getByText("引擎专项验收 · 切换引擎前使用"));
    fireEvent.click(screen.getByRole("button",{name:"引擎运行与切换"}));
    const launch=await screen.findByRole("button",{name:"governance.g7.startG6"});
    await waitFor(()=>expect(launch).toBeEnabled());fireEvent.click(launch);
    const modal=(await screen.findByText("本人审核引擎切换材料")).closest('[role="dialog"]');
    await waitFor(()=>expect(modal).toBeVisible());
    fireEvent.change(screen.getByRole("textbox",{name:"引擎切换审核理由"}),{target:{value:"此为保留草稿，尚未决定切换实际引擎"}});
    fireEvent.click(screen.getByRole("button",{name:"取消本次切换"}));
    fireEvent.click(screen.getByRole("button",{name:"governance.g7.startG6"}));
    expect(await screen.findByRole("textbox",{name:"引擎切换审核理由"})).toHaveValue("此为保留草稿，尚未决定切换实际引擎");
    expect(screen.getByRole("button",{name:"确认切换当前组合引擎"})).toBeDisabled();
    expect(mockApi.startG6Rollout).not.toHaveBeenCalled();
  });
});
