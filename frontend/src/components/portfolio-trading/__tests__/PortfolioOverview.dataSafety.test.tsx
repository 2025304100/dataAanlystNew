import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { message } from "antd";
import { setLocale } from "../../../i18n";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getPortfolios: vi.fn(), getPortfolioPerformance: vi.fn(), getAllocation: vi.fn(),
    getPositions: vi.fn(), getSignalRule: vi.fn(), getPortfolioDetail: vi.fn(), updatePortfolio: vi.fn(),
  },
}));
vi.mock("../../../api/client", () => ({ api: mockApi }));

import PortfolioOverview from "../PortfolioOverview";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.resetAllMocks();
  setLocale("zh-CN");
  mockApi.getPortfolios.mockResolvedValue([
    { id: 1, total_capital: 111000, auto_trade_enabled: 0 },
    { id: 2, total_capital: 222000, auto_trade_enabled: 0 },
  ]);
  mockApi.getPortfolioPerformance.mockResolvedValue({ stats: {}, equity_curve: [] });
  mockApi.getAllocation.mockImplementation(async (id: number) => ({
    cash_amount: id === 1 ? 111000 : 222000, used_amount: 0, position_count: 1, total_position_pct: 0.1,
  }));
  mockApi.getPositions.mockImplementation(async (id: number) => [{
    symbol_id: id, symbol: id === 1 ? "600001" : "600002", name: id === 1 ? "A组合持仓" : "B组合持仓", position_pct: 0.1,
  }]);
  mockApi.getSignalRule.mockImplementation(async (id: number) => ({ rule_name: id === 1 ? "A策略" : "B策略" }));
  mockApi.getPortfolioDetail.mockResolvedValue({ active_rule: null });
  mockApi.updatePortfolio.mockResolvedValue({});
});

afterEach(async () => { await act(async () => { message.destroy(); }); });

describe("PortfolioOverview 数据可信与组合隔离", () => {
  it("现金显示到分，已开启但受阻时说明下一步，查看原因不会改变开关", async () => {
    mockApi.getPortfolios.mockResolvedValue([{ id: 1, auto_trade_enabled: 1 }]);
    mockApi.getAllocation.mockResolvedValue({ cash_amount: 434752.27, used_amount: 8021.08 });
    const onNavigate = vi.fn();
    render(<PortfolioOverview portfolioId={1} onNavigate={onNavigate}
      portfolioStatus={{ current_state: "RECONCILIATION_BLOCKED" } as any} />);

    expect(await screen.findByText("¥434,752.27")).toBeVisible();
    expect(screen.getByText("¥442,773.35")).toBeVisible();
    expect(screen.getByText("自动交易开关已打开，当前仍不能下单。")).toBeVisible();
    expect(screen.getByText("请到“检查与对账”核对差异，处理后再继续。")).toBeVisible();
    expect(screen.queryByText(/HG1/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "查看待处理事项" }));
    expect(onNavigate).toHaveBeenCalledWith("governance");
    expect(mockApi.updatePortfolio).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "自动接管开关" })).toHaveAttribute("aria-pressed", "true");
  });

  it("未选择组合时给出明确空态，不能永久停留在加载上一组合", () => {
    render(<PortfolioOverview portfolioId={0} />);
    expect(screen.getByText("暂无组合")).toBeVisible();
    expect(screen.queryByRole("status", { name: "正在加载当前组合总览" })).not.toBeInTheDocument();
    expect(mockApi.getPortfolios).not.toHaveBeenCalled();
  });

  it("已知的样本匹配方式显示已有中文含义，未知方式不猜测也不冒充调仓频率", async () => {
    mockApi.getSignalRule.mockResolvedValueOnce({ rule_name: "balanced", mode: "balanced" });
    const { rerender } = render(<PortfolioOverview portfolioId={1} />);
    await screen.findByText("A组合持仓");
    expect(screen.getAllByText("均衡模式")).toHaveLength(2);
    expect(screen.queryByText("balanced")).not.toBeInTheDocument();
    mockApi.getSignalRule.mockResolvedValueOnce({ rule_name: "我的策略", mode: "unrecognized_mode" });
    rerender(<PortfolioOverview portfolioId={2} />);
    await screen.findByText("B组合持仓");
    expect(screen.getByText("我的策略")).toBeVisible();
    expect(screen.getByText("请到策略规则查看")).toBeVisible();
    expect(screen.queryByText("unrecognized_mode")).not.toBeInTheDocument();
  });

  it("持仓读取失败显示独立错误和重试，不能伪装成空仓且保留其他成功数据", async () => {
    mockApi.getPositions.mockRejectedValueOnce(new Error("持仓数据源 502"));
    render(<PortfolioOverview portfolioId={1} />);

    const error = await screen.findByRole("alert", { name: "持仓分布加载失败" });
    expect(error).toHaveTextContent("持仓数据源 502");
    expect(screen.queryByText("暂无持仓")).not.toBeInTheDocument();
    expect(screen.getAllByText("¥111,000.00").length).toBeGreaterThan(0);
    await userEvent.click(within(error).getByRole("button", { name: "重试持仓分布" }));
    expect(await screen.findByText("A组合持仓")).toBeVisible();
    expect(screen.queryByRole("alert", { name: "持仓分布加载失败" })).not.toBeInTheDocument();
  });

  it.each(["成功", "失败"] as const)("A 组合迟到的%s响应不能覆盖已显示的 B 组合", async (outcome) => {
    const oldPositions = deferred<Array<{ symbol_id: number; symbol: string; name: string; position_pct: number }>>();
    mockApi.getPositions.mockImplementationOnce(() => oldPositions.promise);
    const { rerender } = render(<PortfolioOverview portfolioId={1} />);
    rerender(<PortfolioOverview portfolioId={2} />);
    expect(await screen.findByText("B组合持仓")).toBeVisible();

    await act(async () => {
      if (outcome === "成功") oldPositions.resolve([{ symbol_id: 1, symbol: "600001", name: "A组合持仓", position_pct: 0.1 }]);
      else oldPositions.reject(new Error("A组合已过期的错误"));
    });

    expect(screen.getByText("B组合持仓")).toBeVisible();
    expect(screen.queryByText("A组合持仓")).not.toBeInTheDocument();
    expect(screen.queryByText(/A组合已过期的错误/)).not.toBeInTheDocument();
    expect(screen.queryByText("¥111,000.00")).not.toBeInTheDocument();
    expect(screen.getAllByText("¥222,000.00").length).toBeGreaterThan(0);
  });

  it.each(["成功", "失败"] as const)("A 组合自动接管写入迟到%s，不能更改 B 的开关或发出误归属提示", async (outcome) => {
    mockApi.getPortfolios.mockResolvedValue([
      { id: 1, total_capital: 111000, auto_trade_enabled: 1 },
      { id: 2, total_capital: 222000, auto_trade_enabled: 0 },
    ]);
    const write = deferred<Record<string, unknown>>();
    mockApi.updatePortfolio.mockReturnValueOnce(write.promise);
    const { rerender } = render(<PortfolioOverview portfolioId={1} />);
    await screen.findByText("A组合持仓");
    await userEvent.click(screen.getByRole("button", { name: "自动接管开关" }));
    expect(mockApi.updatePortfolio).toHaveBeenCalledWith(1, { auto_trade_enabled: 0 });
    rerender(<PortfolioOverview portfolioId={2} />);
    await screen.findByText("B组合持仓");

    await act(async () => {
      if (outcome === "成功") write.resolve({});
      else write.reject(new Error("A组合写入失败"));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByRole("button", { name: "自动接管开关" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByText("已关闭自动接管")).not.toBeInTheDocument();
    expect(screen.queryByText(/A组合写入失败/)).not.toBeInTheDocument();
  });

  it("各数据源即使没有错误详情也要独立报错，重试资金不抹掉其他错误或显示健康的零值", async () => {
    for (const method of [mockApi.getPortfolios, mockApi.getPortfolioPerformance, mockApi.getAllocation, mockApi.getPositions, mockApi.getSignalRule, mockApi.getPortfolioDetail]) {
      method.mockRejectedValueOnce(new Error(""));
    }
    render(<PortfolioOverview portfolioId={1} />);
    expect(await screen.findByRole("alert", { name: "账户状态加载失败" })).toBeVisible();
    expect(screen.getAllByRole("alert")).toHaveLength(6);
    expect(screen.queryByText("暂无持仓")).not.toBeInTheDocument();
    expect(screen.queryByText("暂无数据")).not.toBeInTheDocument();
    expect(screen.queryByText("¥0.00")).not.toBeInTheDocument();
    expect(screen.queryByText("¥111,000.00")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "自动接管开关" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "重试资金分配" }));
    expect((await screen.findAllByText("¥111,000.00")).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("alert")).toHaveLength(5);
    expect(screen.getByRole("alert", { name: "持仓分布加载失败" })).toBeVisible();
  });

  it("切换到尚未加载的组合时立即隐藏旧金额、持仓和开关", async () => {
    const { rerender } = render(<PortfolioOverview portfolioId={1} />);
    await screen.findByText("A组合持仓");
    const nextPositions = deferred<Array<{ symbol_id: number; symbol: string; name: string; position_pct: number }>>();
    mockApi.getPositions.mockReturnValueOnce(nextPositions.promise);

    rerender(<PortfolioOverview portfolioId={2} />);

    expect(screen.getByRole("status", { name: "正在加载当前组合总览" })).toBeVisible();
    expect(screen.queryByText("A组合持仓")).not.toBeInTheDocument();
    expect(screen.queryByText("A策略")).not.toBeInTheDocument();
    expect(screen.queryByText("¥111,000.00")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "自动接管开关" })).not.toBeInTheDocument();
    await act(async () => { nextPositions.resolve([{ symbol_id: 2, symbol: "600002", name: "B组合持仓", position_pct: 0.1 }]); });
    expect(screen.getByText("B组合持仓")).toBeVisible();
  });

  it.each(["成功", "失败"] as const)("当前组合自动接管写入%s仍应给出正确反馈并解除处理中状态", async (outcome) => {
    mockApi.getPortfolios.mockResolvedValue([{ id: 1, total_capital: 111000, auto_trade_enabled: 1 }]);
    const write = deferred<Record<string, unknown>>();
    mockApi.updatePortfolio.mockReturnValueOnce(write.promise);
    render(<PortfolioOverview portfolioId={1} />);
    await screen.findByText("A组合持仓");
    const toggle = screen.getByRole("button", { name: "自动接管开关" });
    await userEvent.click(toggle);
    expect(toggle).toBeDisabled();

    await act(async () => {
      if (outcome === "成功") write.resolve({});
      else write.reject(new Error("当前组合写入失败"));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(toggle).toBeEnabled();
    expect(toggle).toHaveAttribute("aria-pressed", outcome === "成功" ? "false" : "true");
    expect(screen.getByText(outcome === "成功" ? "已关闭自动接管" : "自动接管切换失败: 当前组合写入失败")).toBeVisible();
  });
});

