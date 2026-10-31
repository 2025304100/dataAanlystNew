// 对账守恒子 Tab 的渲染契约。
//
// 这里**不 mock i18n**：本次修复的核心问题就是「界面上到底有没有给人看的话」——
// 把 t() 换成"返回键名"的假实现，就永远验不到字典是否真的补齐了
// （返回 reconDimDecisionRunNotFound 同样算泄露）。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

const { mockApi, mockToast } = vi.hoisted(() => ({
  mockApi: {
    getPortfolioStatus: vi.fn(),
    triggerPortfolioReconcile: vi.fn(),
    previewPortfolioReconciliation: vi.fn(),
    getPortfolioActionItems: vi.fn().mockResolvedValue({
      items: [],
      summary: { blockers: 0, warnings: 0, info: 0, ready: true },
      checks: null,
    }),
  },
  mockToast: vi.fn(),
}));

vi.mock("../../../api/client", () => ({ api: mockApi }));
vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ showToast: mockToast }),
}));


import { setLocale } from "../../../i18n";
import PortfolioGovernanceTab from "../PortfolioGovernanceTab";

const NO_DECISION_RESP = {
  portfolio_id: 1,
  trade_date: "2026-09-29",
  as_of_at: "2026-10-01T07:41:00",
  differences_found: true,
  zero_sum_check_passed: false,
  last_reconciled_trade_date: "2026-09-28",
  correlation_id: "corr-1",
  items: [
    {
      dimension: "DECISION_RUN_NOT_FOUND",
      expected_value: null,
      actual_value: null,
      diff_value: null,
      explain_note:
        "portfolio_id=1 trade_date=2026-09-29 无 SUCCEEDED auto_simulation DecisionRun（未接管/当日仍未执行）",
    },
  ],
};

/** 面板内的可见文本；用函数取值，避免 React 换节点后拿到过期快照。 */
const panelText = (): string => screen.getByTestId("reconcile-panel").textContent ?? "";

const openReconcileTab = () => {
  // 每日核对入口统一在「对账连续性」分段：切段后走空态兜底入口
  fireEvent.click(screen.getByRole("tab", { name: "对账连续性" }));
  fireEvent.click(screen.getByRole("button", { name: "打开对账" }));
};

describe("对账守恒子 Tab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setLocale("zh-CN");
    mockApi.getPortfolioStatus.mockResolvedValue({
      current_state: "READY",
      allowed_transitions: ["ADMIN_PAUSED"],
    });
  });

  it("缺少执行记录时仍展示真实现金差额，刷新只读核对不执行写入对账", async () => {
    mockApi.previewPortfolioReconciliation.mockResolvedValue({ ...NO_DECISION_RESP, read_only: true,
      cash_audit: { status: "BROKEN", expected_cash: 434752.27, actual_cash: 434752.01, delta: -.26,
        differences: [{ledger_id:13,expected:174714.37,actual:174714.19,delta:-.18}] },
      items: [...NO_DECISION_RESP.items, {dimension:"NAV_BROKEN",expected_value:{cash:434752.27},
        actual_value:{cash:434752.01},diff_value:null,explain_note:"历史余额不连续"}] });
    render(<PortfolioGovernanceTab portfolioId={1} />);openReconcileTab();
    expect(await screen.findByTestId("recon-verdict")).toHaveTextContent("发现账实差异");
    expect(panelText()).toContain("434,752.27");
    expect(panelText()).toContain("-0.26");
    expect(panelText()).toContain("只读预览");
    await act(async () => { fireEvent.click(screen.getByRole('button',{name:'刷新只读核对'})); });
    expect(mockApi.previewPortfolioReconciliation).toHaveBeenCalledTimes(2);
    expect(mockApi.triggerPortfolioReconcile).not.toHaveBeenCalled();
  });

  it("当日无决策时说清「无账可对」，不再说成「加减和 ≠ 0」，也不泄露后端枚举", async () => {
    mockApi.previewPortfolioReconciliation.mockResolvedValue(NO_DECISION_RESP);

    render(<PortfolioGovernanceTab portfolioId={1} />);
    openReconcileTab();

    const card = await screen.findByTestId("recon-verdict");
    expect(mockApi.previewPortfolioReconciliation).toHaveBeenCalledWith(1);
    expect(mockApi.triggerPortfolioReconcile).not.toHaveBeenCalled();
    expect(card).toHaveTextContent("当天执行情况尚未核实");

    // 走查里最刺眼的那一行（「自定义维度 (DECISION_RUN_NOT_FOUND)」）必须说人话
    expect(panelText()).toContain("当日无决策运行");
    expect(panelText()).not.toContain("DECISION_RUN_NOT_FOUND");
    expect(panelText()).not.toContain("自定义维度");
    // 告警文案不得再把「缺基准」说成「算出来不等于零」
    expect(panelText()).not.toContain("加减和");

    expect(mockApi.triggerPortfolioReconcile).not.toHaveBeenCalled();
  });

  it("证据缺失时不提供跳过校验或确认通过", async () => {
    mockApi.previewPortfolioReconciliation.mockResolvedValue(NO_DECISION_RESP);
    render(<PortfolioGovernanceTab portfolioId={1} />);
    openReconcileTab();
    await screen.findByTestId("recon-verdict");
    expect(screen.queryByRole("button", { name: "审核本次对账结果" })).toBeNull();
    expect(screen.queryByText(/强制跳过/)).toBeNull();
    expect(mockApi.triggerPortfolioReconcile).not.toHaveBeenCalled();
  });

  it("对账通过时给正向结论，且不出现人工解锁入口", async () => {
    mockApi.previewPortfolioReconciliation.mockResolvedValue({
      portfolio_id: 1,
      trade_date: "2026-09-29",
      as_of_at: "2026-10-01T07:41:00",
      differences_found: false,
      zero_sum_check_passed: true,
      last_reconciled_trade_date: "2026-09-29",
      correlation_id: "corr-2",
      items: [],
    });

    render(<PortfolioGovernanceTab portfolioId={1} />);
    openReconcileTab();

    const card = await screen.findByTestId("recon-verdict");
    expect(card).toHaveTextContent("当天对账已通过");
    expect(screen.queryByTestId("recon-manual-toggle")).toBeNull();
  });

  it("真实差异时报「账实不符，自动交易已暂停」并给出中文维度名", async () => {
    mockApi.previewPortfolioReconciliation.mockResolvedValue({
      portfolio_id: 1,
      trade_date: "2026-09-29",
      as_of_at: "2026-10-01T07:41:00",
      differences_found: true,
      zero_sum_check_passed: false,
      last_reconciled_trade_date: "2026-09-28",
      correlation_id: "corr-3",
      items: [
        {
          dimension: "POSITION_MISMATCH",
          expected_value: 100,
          actual_value: 80,
          diff_value: -20,
          explain_note: "100: 目标持仓 100 ≠ 实际 80",
        },
      ],
    });

    render(<PortfolioGovernanceTab portfolioId={1} />);
    openReconcileTab();

    const card = await screen.findByTestId("recon-verdict");
    expect(card).toHaveTextContent("发现账实差异");
    expect(panelText()).toContain("持仓不符");
    expect(panelText()).not.toContain("POSITION_MISMATCH");
  });
});
