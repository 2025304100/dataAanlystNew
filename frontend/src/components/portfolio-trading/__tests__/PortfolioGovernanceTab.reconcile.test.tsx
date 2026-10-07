// 对账守恒子 Tab 的渲染契约。
//
// 这里**不 mock i18n**：本次修复的核心问题就是「界面上到底有没有给人看的话」——
// 把 t() 换成"返回键名"的假实现，就永远验不到字典是否真的补齐了
// （返回 reconDimDecisionRunNotFound 同样算泄露）。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

const { mockApi, mockToast } = vi.hoisted(() => ({
  mockApi: {
    getPortfolioStatus: vi.fn(),
    triggerPortfolioReconcile: vi.fn(),
  },
  mockToast: vi.fn(),
}));

vi.mock("../../../api/client", () => ({ api: mockApi }));
vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ showToast: mockToast }),
}));
vi.mock("antd", () => ({
  Tooltip: ({ children }: { children: ReactNode }) => <>{children}</>,
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
  fireEvent.click(screen.getByRole("button", { name: "对账守恒" }));
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

  it("当日无决策时说清「无账可对」，不再说成「加减和 ≠ 0」，也不泄露后端枚举", async () => {
    mockApi.triggerPortfolioReconcile.mockResolvedValue(NO_DECISION_RESP);

    render(<PortfolioGovernanceTab portfolioId={1} />);
    openReconcileTab();

    const card = await screen.findByTestId("recon-verdict");
    expect(card).toHaveTextContent("当日没有决策，无账可对");

    // 走查里最刺眼的那一行（「自定义维度 (DECISION_RUN_NOT_FOUND)」）必须说人话
    expect(panelText()).toContain("当日无决策运行");
    expect(panelText()).not.toContain("DECISION_RUN_NOT_FOUND");
    expect(panelText()).not.toContain("自定义维度");
    // 告警文案不得再把「缺基准」说成「算出来不等于零」
    expect(panelText()).not.toContain("加减和");

    // toast 与结论卡必须说同一句话，不能一个说「无账可对」、另一个说「账实不符」
    const toastMsgs = mockToast.mock.calls.map((call) => String(call[1])).join(" | ");
    expect(toastMsgs).toContain("当日没有决策，无账可对");
    expect(toastMsgs).not.toContain("加减和");
  });

  it("人工解锁区默认收起，点开后才出现勾选框（避免一进来就被推着点确认）", async () => {
    mockApi.triggerPortfolioReconcile.mockResolvedValue(NO_DECISION_RESP);

    render(<PortfolioGovernanceTab portfolioId={1} />);
    openReconcileTab();
    await screen.findByTestId("recon-verdict");

    expect(screen.queryByText("确认对账修复完成")).toBeNull();
    fireEvent.click(screen.getByTestId("recon-manual-toggle"));
    expect(await screen.findByText("确认对账修复完成")).toBeTruthy();
  });

  it("对账通过时给正向结论，且不出现人工解锁入口", async () => {
    mockApi.triggerPortfolioReconcile.mockResolvedValue({
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
    expect(card).toHaveTextContent("对账通过");
    expect(screen.queryByTestId("recon-manual-toggle")).toBeNull();
  });

  it("真实差异时报「账实不符，自动交易已暂停」并给出中文维度名", async () => {
    mockApi.triggerPortfolioReconcile.mockResolvedValue({
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
    expect(card).toHaveTextContent("账实不符");
    expect(panelText()).toContain("持仓不符");
    expect(panelText()).not.toContain("POSITION_MISMATCH");
  });
});
