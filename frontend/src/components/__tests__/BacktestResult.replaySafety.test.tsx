import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "antd";
import { setLocale } from "../../i18n";
import type { BacktestRun } from "../../types";

const { mockApi } = vi.hoisted(() => ({
  mockApi: { applyBacktestToPortfolio: vi.fn() },
}));

vi.mock("../../api/client", () => ({ api: mockApi }));
// 第三方 Canvas 渲染器不属于重放交互边界；其余组件使用真实实现。
vi.mock("echarts-for-react", () => ({ default: () => null }));

import BacktestResult from "../BacktestResult";

const run: BacktestRun = {
  id: 71,
  portfolio_id: 3,
  run_name: "止损验证",
  symbols_json: "[1]",
  rule_config_json: "{}",
  start_date: "2026-01-05",
  end_date: "2026-10-16",
  initial_capital: 100000,
  status: "done",
  created_at: "2026-10-19T08:00:00Z",
  trades: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  setLocale("zh-CN");
  mockApi.applyBacktestToPortfolio.mockResolvedValue({ applied_trades: 0, open_positions: 0, final_cash: 100000 });
});

afterEach(() => {
  Modal.destroyAll();
  cleanup();
});

describe("BacktestResult 历史重放保护", () => {
  it("用日常中文说明简化规则的结果限制，并保留具体原因供查看", () => {
    render(<BacktestResult result={{ ...run, production_fidelity: false, non_fidelity_reason: "suspension_filter_disabled" } as BacktestRun} />);
    expect(screen.getByText("仅供研究参考")).toBeVisible();
    expect(screen.getByText("这次回测未确认已采用全部交易限制，不能与完整规则下的回测直接比较。")).toBeVisible();
    const details = screen.getByText("查看具体原因").closest("details");
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText("suspension_filter_disabled")).not.toBeVisible();
    expect(mockApi.applyBacktestToPortfolio).not.toHaveBeenCalled();
  });

  it("默认保留当前模拟账本，普通确认不会请求覆盖", async () => {
    render(<BacktestResult result={run} portfolioId={3} />);
    await userEvent.click(screen.getByRole("button", { name: "应用到模拟组合" }));
    const dialog = screen.getByRole("dialog", { name: "应用到模拟组合" });

    expect(within(dialog).getByRole("switch")).not.toBeChecked();
    await userEvent.click(within(dialog).getByRole("button", { name: "确认应用" }));
    expect(mockApi.applyBacktestToPortfolio).toHaveBeenCalledWith(71, 3, false);
  });

  it("选择清空后必须明确确认覆盖的目标账本，才允许提交覆盖请求", async () => {
    render(<BacktestResult result={run} portfolioId={3} />);
    await userEvent.click(screen.getByRole("button", { name: "应用到模拟组合" }));
    const dialog = screen.getByRole("dialog", { name: "应用到模拟组合" });
    await userEvent.click(within(dialog).getByRole("switch"));

    const apply = within(dialog).getByRole("button", { name: "确认应用" });
    expect(apply).toBeDisabled();
    await userEvent.click(apply);
    expect(mockApi.applyBacktestToPortfolio).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole("checkbox", { name: "我确认覆盖模拟组合 #3 的全部订单、成交、持仓和资金流水" }));
    expect(apply).toBeEnabled();
    await userEvent.click(apply);
    expect(mockApi.applyBacktestToPortfolio).toHaveBeenCalledWith(71, 3, true);
  });

  it("取消后重新打开重放，不沿用上次的清空选项和覆盖授权", async () => {
    render(<BacktestResult result={run} portfolioId={3} />);
    await userEvent.click(screen.getByRole("button", { name: "应用到模拟组合" }));
    const dialog = screen.getByRole("dialog", { name: "应用到模拟组合" });
    await userEvent.click(within(dialog).getByRole("switch"));
    await userEvent.click(within(dialog).getByRole("checkbox"));
    await userEvent.click(within(dialog).getByRole("button", { name: /取\s*消/ }));
    await userEvent.click(screen.getByRole("button", { name: "应用到模拟组合" }));

    const reopened = screen.getByRole("dialog", { name: "应用到模拟组合" });
    expect(within(reopened).getByRole("switch")).not.toBeChecked();
    expect(within(reopened).queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it.each([
    { change: "目标组合", portfolioId: 4, runId: 71 },
    { change: "回测记录", portfolioId: 3, runId: 72 },
  ])("$change 变化后关闭重放弹窗，不沿用对原账本的覆盖授权", async ({ portfolioId, runId }) => {
    const { rerender } = render(<BacktestResult result={run} portfolioId={3} />);
    await userEvent.click(screen.getByRole("button", { name: "应用到模拟组合" }));
    const dialog = screen.getByRole("dialog", { name: "应用到模拟组合" });
    await userEvent.click(within(dialog).getByRole("switch"));
    await userEvent.click(within(dialog).getByRole("checkbox"));
    expect(within(dialog).getByRole("button", { name: "确认应用" })).toBeEnabled();

    rerender(<BacktestResult result={{ ...run, id: runId }} portfolioId={portfolioId} />);

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "应用到模拟组合" })).not.toBeInTheDocument());
    expect(mockApi.applyBacktestToPortfolio).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "应用到模拟组合" }));
    const reopened = screen.getByRole("dialog", { name: "应用到模拟组合" });
    expect(within(reopened).getByRole("switch")).not.toBeChecked();
    expect(within(reopened).queryByRole("checkbox")).not.toBeInTheDocument();
  });
});
