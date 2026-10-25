// 治理「待办总览」渲染契约：行动项来自与修复抽屉同一个接口；动作只做导航。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

const { mockApi, mockSetActiveTab } = vi.hoisted(() => ({
  mockApi: {
    getPortfolioActionItems: vi.fn(),
    getPortfolioStatus: vi.fn(),
  },
  mockSetActiveTab: vi.fn(),
}));

vi.mock("../../../api/client", () => ({ api: mockApi }));
vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ setActiveTab: mockSetActiveTab }),
}));
vi.mock("antd", () => ({
  Tooltip: ({ children }: { children: ReactNode }) => <>{children}</>,
  Button: ({ children, onClick, type }: { children?: ReactNode; onClick?: () => void; type?: string }) => (
    <button type="button" data-type={type} onClick={onClick}>{children}</button>
  ),
}));

import { setLocale } from "../../../i18n";
import GovernanceOverviewPanel from "../GovernanceOverviewPanel";

const PAYLOAD = {
  items: [
    {
      id: "hg1_status:RECONCILIATION_BLOCKED",
      source: "hg1_status",
      code: "RECONCILIATION_BLOCKED",
      severity: "blocker",
      title: "对账差异阻塞",
      impact: "对账未平，已暂停交易，需你本人确认",
      detail: null,
      family: "reconciliation",
      primary_action: { action_type: "guided", target: "governance:reconciliation" },
      sources: ["hg1_status"],
    },
    {
      id: "data-freshness",
      source: "readiness",
      code: "MARKET_DATA_STALE",
      severity: "blocker",
      title: "行情或评分过期",
      impact: "今天不会真实下单",
      detail: null,
      family: "data-freshness",
      primary_action: { action_type: "redirect", target: "settings:data-center" },
      sources: ["readiness"],
    },
  ],
  summary: { blockers: 2, warnings: 0, info: 0, ready: false },
  checks: { enabled: true, account_ready: true, data_ready: false, source_ready: true, schedule_ready: true },
  generated_at: "2026-10-13T10:00:00",
};

describe("治理 · 待办总览", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setLocale("zh-CN");
    mockApi.getPortfolioActionItems.mockResolvedValue(PAYLOAD);
    mockApi.getPortfolioStatus.mockResolvedValue({ current_state: "RECONCILIATION_BLOCKED" });
  });

  it("拉取行动项并渲染阻断卡，主文案不裸显英文枚举", async () => {
    render(<GovernanceOverviewPanel portfolioId={1} onNavigate={() => {}} />);
    expect(await screen.findByText("对账差异阻塞")).toBeTruthy();
    expect(screen.getByText("行情或评分过期")).toBeTruthy();
    expect(screen.queryByText("MARKET_DATA_STALE")).toBeNull();
    expect(screen.queryByText("RECONCILIATION_BLOCKED")).toBeNull();
  });

  it("对账行动项点击跳到「每日对账」子页", async () => {
    const onNavigate = vi.fn();
    render(<GovernanceOverviewPanel portfolioId={1} onNavigate={onNavigate} />);
    await screen.findByText("对账差异阻塞");
    fireEvent.click(screen.getByRole("button", { name: /去处理对账/ }));
    expect(onNavigate).toHaveBeenCalledWith("governance:reconciliation");
  });

  it("跨页行动项点击写 settings 区并切到设置主 Tab", async () => {
    window.localStorage.clear();
    const onNavigate = vi.fn();
    render(<GovernanceOverviewPanel portfolioId={1} onNavigate={onNavigate} />);
    await screen.findByText("行情或评分过期");
    fireEvent.click(screen.getAllByRole("button", { name: /前往处理/ })[0]);
    expect(window.localStorage.getItem("settings_active_section")).toBe("data-center");
    expect(mockSetActiveTab).toHaveBeenCalledWith("settings");
  });

  it("快捷动作：去每日对账 / 调整运行状态 / 查看操作记录", async () => {
    const onNavigate = vi.fn();
    render(<GovernanceOverviewPanel portfolioId={1} onNavigate={onNavigate} />);
    await screen.findByText("对账差异阻塞");
    fireEvent.click(screen.getByRole("button", { name: "去每日对账" }));
    fireEvent.click(screen.getByRole("button", { name: "调整运行状态" }));
    fireEvent.click(screen.getByRole("button", { name: "查看操作记录" }));
    expect(onNavigate.mock.calls.flat()).toEqual(
      expect.arrayContaining(["governance:reconciliation", "governance:state", "governance:audit"]),
    );
  });

  it("无行动项时显示全部就绪空态", async () => {
    mockApi.getPortfolioActionItems.mockResolvedValue({
      items: [],
      summary: { blockers: 0, warnings: 0, info: 0, ready: true },
      checks: { enabled: true, account_ready: true, data_ready: true, source_ready: true, schedule_ready: true },
      generated_at: "t",
    });
    mockApi.getPortfolioStatus.mockResolvedValue({ current_state: "READY" });
    render(<GovernanceOverviewPanel portfolioId={1} onNavigate={() => {}} />);
    expect(await screen.findByText("已就绪，等待调度")).toBeTruthy();
  });
});
