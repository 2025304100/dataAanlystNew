import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setLocale, t } from "../../../i18n";

const { mockApi } = vi.hoisted(() => ({ mockApi: {
  getMembers: vi.fn(), getPositions: vi.fn(), getPortfolioCandidates: vi.fn(),
  executeAutoTrade: vi.fn(), submitSimOrder: vi.fn(),
} }));
vi.mock("../../../api/client", () => ({ api: mockApi, requestJson: vi.fn() }));
vi.mock("../../../context/AppContext", () => ({ useApp: () => ({ showToast: vi.fn() }) }));
import PortfolioMembersTable from "../PortfolioMembersTable";

beforeEach(() => {
  vi.resetAllMocks();
  setLocale("zh-CN");
  mockApi.getMembers.mockResolvedValue([]);
  mockApi.getPositions.mockResolvedValue([{ symbol_id: 21, symbol: "600000", name: "浦发银行", quantity: 1000,
    position_pct: 0.1, target_weight_pct: 0.1, deviation_pct: 0, latest_price: 9, market_value: 9000 }]);
  mockApi.getPortfolioCandidates.mockResolvedValue([]);
});

describe("持仓筛选的退出路径", () => {
  it("偏离筛选没有结果时说明仍有持仓，临时查看全部后可以原样返回", async () => {
    render(<PortfolioMembersTable portfolioId={1} />);
    expect(await screen.findByText("共 1 个标的，当前条件下没有结果。")).toBeVisible();
    expect(screen.queryByText(t("portfolioTrading.members.emptyPositions"))).not.toBeInTheDocument();
    const filter = screen.getByRole("button", { name: t("portfolioTrading.members.deviationFilterLabel") });
    expect(filter).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "查看全部" }));
    expect(screen.getByText("浦发银行")).toBeVisible();
    expect(filter).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "恢复筛选" }));
    expect(screen.getByText("共 1 个标的，当前条件下没有结果。")).toBeVisible();
    expect(mockApi.executeAutoTrade).not.toHaveBeenCalled();
    expect(mockApi.submitSimOrder).not.toHaveBeenCalled();
  });

  it("查看全部不会清掉搜索内容，改搜索时立即按新内容筛选", async () => {
    render(<PortfolioMembersTable portfolioId={1} />);
    await screen.findByText("共 1 个标的，当前条件下没有结果。");
    const input = screen.getByPlaceholderText("搜索代码或名称");
    await userEvent.type(input, "不存在的股票");
    await userEvent.click(screen.getByRole("button", { name: "查看全部" }));
    expect(screen.getByText("浦发银行")).toBeVisible();
    expect(input).toHaveValue("不存在的股票");
    await userEvent.click(screen.getByRole("button", { name: "恢复筛选" }));
    expect(screen.queryByText("浦发银行")).not.toBeInTheDocument();
    expect(input).toHaveValue("不存在的股票");
    await userEvent.click(screen.getByRole("button", { name: "查看全部" }));
    await userEvent.type(input, "X");
    expect(screen.queryByText("浦发银行")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "恢复筛选" })).not.toBeInTheDocument();
  });

  it("确实没有持仓时不提供误导的查看全部", async () => {
    mockApi.getPositions.mockResolvedValue([]);
    render(<PortfolioMembersTable portfolioId={1} />);
    expect(await screen.findByText(t("portfolioTrading.members.emptyPositions"))).toBeVisible();
    expect(screen.queryByRole("button", { name: "查看全部" })).not.toBeInTheDocument();
  });
});
