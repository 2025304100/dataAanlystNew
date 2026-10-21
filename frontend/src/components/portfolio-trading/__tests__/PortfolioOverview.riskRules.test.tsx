import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setLocale } from "../../../i18n";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getPortfolios: vi.fn(),
    getPortfolioPerformance: vi.fn(),
    getAllocation: vi.fn(),
    getPositions: vi.fn(),
    getSignalRule: vi.fn(),
    getPortfolioDetail: vi.fn(),
  },
}));

vi.mock("../../../api/client", () => ({ api: mockApi }));

import PortfolioOverview from "../PortfolioOverview";

beforeEach(() => {
  vi.clearAllMocks();
  setLocale("zh-CN");
  mockApi.getPortfolios.mockResolvedValue([{ id: 3, total_capital: 100000, auto_trade_enabled: 0 }]);
  mockApi.getPortfolioPerformance.mockResolvedValue({ stats: {}, equity_curve: [] });
  mockApi.getAllocation.mockResolvedValue({ cash_amount: 100000, used_amount: 0 });
  mockApi.getPositions.mockResolvedValue([]);
  mockApi.getSignalRule.mockResolvedValue(null);
  mockApi.getPortfolioDetail.mockResolvedValue({ active_rule: null });
});

describe("PortfolioOverview 风险规则说明", () => {
  it("未接入执行的风险说明不能冒充已启用保护，并可前往实际策略规则", async () => {
    const onNavigate = vi.fn();
    render(<PortfolioOverview portfolioId={3} onNavigate={onNavigate} />);

    await screen.findByRole("heading", { name: "风险规则" });
    expect(screen.queryByText(/条已启用/)).not.toBeInTheDocument();
    const rules = await screen.findByRole("region", { name: "风险规则说明" });
    expect(within(rules).getByText("只读说明 · 尚未接入执行")).toBeVisible();
    expect(within(rules).getByText(/不代表当前组合已启用止损、回撤熔断或自动减仓/)).toBeVisible();
    expect(within(rules).queryByRole("button", { pressed: true })).not.toBeInTheDocument();
    expect(within(rules).queryByRole("button", { pressed: false })).not.toBeInTheDocument();

    await userEvent.click(within(rules).getByRole("button", { name: "查看实际策略规则" }));
    expect(onNavigate).toHaveBeenCalledWith("strategy");
  });
});
