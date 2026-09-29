// 成员执行诊断（补回旧 AutoTradePanel 的 member-status 能力，体检报告 §二十六）
// 关注点：
// 1. 诊断是只读入口，HG1 买卖权限全关时也必须可用（否则锁着就查不动，等于没补）；
// 2. 点击后拉 member-status 并把拒绝原因 / 数据健康 / 风控阻断如实展示；
// 3. 接口失败要显式报错，不能静默空白（静默空白会被当成"这个成员没问题"）。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const { mockApi, mockToast } = vi.hoisted(() => ({
  mockApi: {
    getMembers: vi.fn(),
    getPositions: vi.fn(),
    getPortfolioCandidates: vi.fn(),
    getAutoTradeMemberStatus: vi.fn(),
  },
  mockToast: vi.fn(),
}));

vi.mock("../../../api/client", () => ({ api: mockApi, requestJson: vi.fn() }));
vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ showToast: mockToast, setActiveTab: vi.fn() }),
}));
vi.mock("../../../i18n", () => ({ t: (key: string) => key, sideLabel: (v: string) => v }));

import PortfolioMembersTable from "../PortfolioMembersTable";

const POSITION_ROW = {
  id: 77,
  portfolio_id: 1,
  symbol_id: 21,
  symbol: "600000",
  name: "浦发银行",
  quantity: 1000,
  avg_cost: 8.0,
  latest_price: 9.0,
  market_value: 9000,
  position_pct: 0.1,
  target_weight_pct: 0.02, // 与 10% 偏离 8%，能通过"仅显示偏离>2%"过滤器
  asset_type: "stock",
  theme: null,
  unrealized_pnl: 1000,
  unrealized_pnl_pct: 0.125,
};

const DIAGNOSTICS = {
  portfolio_id: 1,
  members: [
    {
      member_id: 11,
      symbol_id: 21,
      symbol: "600000",
      name: "浦发银行",
      status: "active",
      execution_mode: "auto",
      manual_lock: false,
      has_position: true,
      position_quantity: 1000,
      latest_order: {
        order_id: 9001,
        side: "BUY",
        status: "REJECTED",
        created_at: "2026-09-29T06:00:00Z",
        source_type: "decision_engine",
        signal_id: "sig-1",
        execution_mode: "auto",
        client_order_key: "key-1",
        rejection_code: "SCORE_STALE",
        rejection_detail: "今日评分覆盖率不足",
      },
      data_health: { healthy: false, reason: "score 落后 2 个交易日" },
      risk_blocked: true,
      data_expired: true,
    },
  ],
};

const renderTable = (perm: Record<string, boolean> = { allow_new_buys: false, allow_risk_exits: false }) =>
  render(
    <PortfolioMembersTable
      portfolioId={1}
      onNavigate={vi.fn()}
      portfolioStatus={{ current_state: "RECONCILIATION_BLOCKED" } as any}
      perm={perm as any}
    />,
  );

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.getMembers.mockResolvedValue([
    { id: 11, portfolio_id: 1, symbol_id: 21, symbol: "600000", name: "浦发银行", status: "active", execution_mode: "auto" },
  ]);
  mockApi.getPositions.mockResolvedValue([POSITION_ROW]);
  mockApi.getPortfolioCandidates.mockResolvedValue([]);
  mockApi.getAutoTradeMemberStatus.mockResolvedValue(DIAGNOSTICS);
});

describe("PortfolioMembersTable 成员执行诊断", () => {
  it("HG1 买卖权限全关时，诊断入口仍然可用（只读不受门禁限制）", async () => {
    renderTable({ allow_new_buys: false, allow_risk_exits: false });
    const btn = await screen.findByTestId("member-diagnostics-button");
    expect(btn).toBeEnabled();
    // 同一行的写操作此时是锁着的，用来证明"锁"与"只读"确实分开
    expect(screen.getAllByRole("button").some((b) => /🔒/.test(b.textContent ?? ""))).toBe(true);
  });

  it("点击诊断后展示拒绝码、数据健康与风控/过期标签", async () => {
    renderTable();
    fireEvent.click(await screen.findByTestId("member-diagnostics-button"));

    await waitFor(() => expect(mockApi.getAutoTradeMemberStatus).toHaveBeenCalledWith(1));
    await screen.findByTestId("member-diagnostics-panel");

    expect(screen.getByTestId("diag-rejection").textContent).toContain("SCORE_STALE");
    expect(screen.getByTestId("diag-rejection").textContent).toContain("今日评分覆盖率不足");
    expect(screen.getByTestId("diag-data-health").textContent).toContain("NG");
    expect(screen.getByTestId("diag-data-health").textContent).toContain("score 落后 2 个交易日");
    expect(screen.getByTestId("diag-latest-order").textContent).toContain("REJECTED");
    expect(screen.getByTestId("diag-tag-risk-blocked")).toBeInTheDocument();
    expect(screen.getByTestId("diag-tag-data-expired")).toBeInTheDocument();
    expect(screen.queryByTestId("diag-tag-archived")).not.toBeInTheDocument();
  });

  it("组合级接口只拉一次，切换行复用缓存", async () => {
    renderTable();
    fireEvent.click(await screen.findByTestId("member-diagnostics-button"));
    await screen.findByTestId("member-diagnostics-panel");
    fireEvent.click(screen.getByTestId("member-diagnostics-close"));
    fireEvent.click(screen.getByTestId("member-diagnostics-button"));

    await waitFor(() => expect(screen.getByTestId("member-diagnostics-panel")).toBeInTheDocument());
    expect(mockApi.getAutoTradeMemberStatus).toHaveBeenCalledTimes(1);
  });

  it("诊断接口失败时显式报错，不静默留白", async () => {
    mockApi.getAutoTradeMemberStatus.mockRejectedValueOnce(new Error("upstream 500"));
    renderTable();
    fireEvent.click(await screen.findByTestId("member-diagnostics-button"));

    const panel = await screen.findByTestId("member-diagnostics-panel");
    expect(panel).toBeInTheDocument();
    expect(screen.getByTestId("member-diagnostics-error").textContent).toContain("upstream 500");
    expect(screen.queryByTestId("diag-rejection")).not.toBeInTheDocument();
  });

  it("名单里查不到该标的时给出明确解释（而不是空白）", async () => {
    mockApi.getAutoTradeMemberStatus.mockResolvedValueOnce({ portfolio_id: 1, members: [] });
    renderTable();
    fireEvent.click(await screen.findByTestId("member-diagnostics-button"));

    await screen.findByTestId("member-diagnostics-empty");
    expect(screen.getByTestId("member-diagnostics-empty").textContent)
      .toContain("portfolioTrading.members.diagnosticsEmpty");
  });
});
