import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const { mockApi, mockToast } = vi.hoisted(() => ({
  mockApi: {
    getPortfolioBacktestSourceStatus: vi.fn(),
    getBacktestRuns: vi.fn(),
    runPortfolioBacktest: vi.fn(),
    getBacktestRun: vi.fn(),
    getBacktestTrades: vi.fn(),
    getBacktestEvidence: vi.fn(),
    getBacktestPositions: vi.fn(),
    getCurrentFactorUsage: vi.fn(),
    evaluatePortfolioDecision: vi.fn(),
  },
  mockToast: vi.fn(),
}));

vi.mock("../../../api/client", () => ({
  api: mockApi,
}));

vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ showToast: mockToast, setActiveTab: vi.fn() }),
}));

vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
}));

vi.mock("antd", () => ({
  DatePicker: {
    RangePicker: ({ "data-testid": testId }: { "data-testid"?: string }) => (
      <div data-testid={testId} />
    ),
  },
  App: {
    useApp: () => ({
      message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
    }),
  },
  Modal: { warning: vi.fn() },
}));

vi.mock("../DecisionEvidenceDrawer", () => ({
  default: ({ open, decisionRunId, selectedEvidenceId }: {
    open: boolean;
    decisionRunId?: string | null;
    selectedEvidenceId?: string | null;
  }) => (
    open ? (
      <div data-testid="decision-evidence-drawer">
        <span>{decisionRunId ?? ""}</span>
        <span data-testid="selected-evidence-id">{selectedEvidenceId ?? ""}</span>
      </div>
    ) : null
  ),
}));

import PortfolioBacktestCenter from "../PortfolioBacktestCenter";

const currentSummary = {
  run_id: 101,
  portfolio_id: 1,
  symbol_ids: [11],
  symbol_count: 1,
  start_date: "2025-01-01",
  end_date: "2025-12-31",
  initial_capital: 100000,
  status: "completed",
  run_name: "current-run",
  total_return: 1000,
  total_return_pct: 0.01,
  max_drawdown: -500,
  max_drawdown_pct: -0.005,
  sharpe_ratio: 1.2,
  win_rate: 0.6,
  profit_factor: 1.5,
  trade_count: 2,
  avg_holding_days: 5,
  equity_curve: [
    { date: "2025-01-01", equity: 100000 },
    { date: "2025-01-02", equity: 101000 },
  ],
  metrics: {},
  diagnostics: {},
  warnings: [],
  errors: [],
  data_cutoff_at: "2025-12-30T08:00:00Z",
  factor_data_cutoff_at: "2025-12-30T08:00:00Z",
  benchmark: "沪深300",
  benchmark_status: "BENCHMARK_INCOMPLETE",
  benchmark_gap_days: 2,
  pit_mode: "production_pit",
  source_type: "portfolio_members_snapshot",
  strategy_snapshot_id: "snap-42",
  snapshot_hash: "sha256:abc123",
  factor_model_run_id: "model-run-7",
  factor_set_id: "factor-set-3",
  data_snapshot: {
    data_cutoff_at: "2025-12-30T08:00:00Z",
    market_source: "akshare",
    warnings: [{ code: "BENCHMARK_GAP", gap_days: 2 }],
  },
};

const historicalRun = {
  id: 77,
  portfolio_id: 1,
  run_name: "historic-run",
  symbols_json: "[22]",
  rule_config_json: "{}",
  start_date: "2024-01-01",
  end_date: "2024-12-31",
  initial_capital: 200000,
  total_return: 2000,
  total_return_pct: 0.02,
  max_drawdown: -1000,
  max_drawdown_pct: -0.01,
  sharpe_ratio: 1.5,
  win_rate: 0.7,
  profit_factor: 1.8,
  trade_count: 1,
  avg_holding_days: 7,
  equity_curve_json: JSON.stringify([
    { date: "2024-01-01", equity: 200000 },
    { date: "2024-01-02", equity: 202000 },
  ]),
  status: "completed",
  created_at: "2025-01-01T00:00:00Z",
  finished_at: "2025-01-01T01:00:00Z",
  price_series: [],
  diagnostics: {},
  decision_run_ids: [],
  evidence_summary: {},
  rejected_count: 0,
  decision_snapshot: null,
};

describe("PortfolioBacktestCenter paged ledger contract", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, "", "/");
    mockApi.getPortfolioBacktestSourceStatus.mockResolvedValue({
      enabled: true,
      env_flag: "PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED",
      source_label: "members",
    });
    mockApi.getBacktestRuns.mockResolvedValue([historicalRun]);
    mockApi.runPortfolioBacktest.mockResolvedValue(currentSummary);
    mockApi.getBacktestRun.mockResolvedValue(historicalRun);
    mockApi.getBacktestTrades.mockImplementation((runId: number) => Promise.resolve({
      total: runId === 101 ? 2 : 1,
      page: 1,
      page_size: 20,
      items: [{
        id: runId,
        run_id: runId,
        symbol_id: runId === 101 ? 11 : 22,
        entry_date: "2025-01-02",
        entry_price: 10,
        intended_entry_price: 9.8,
        exit_date: "2025-01-03",
        exit_price: 10.4,
        exit_evidence_id: "exit-evidence",
        intended_exit_price: 10.2,
        quantity: 100,
        entry_cost: 5,
      }],
    }));
    mockApi.getBacktestEvidence.mockResolvedValue({ total: 0, items: [] });
    mockApi.getBacktestPositions.mockResolvedValue({
      total: 0,
      page: 1,
      page_size: 20,
      as_of_date: "2025-12-31",
      status: null,
      items: [],
    });
  });

  it("loads the ledger only after the transaction tab opens and replaces it for a selected historical run", async () => {
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);

    await waitFor(() => {
      expect(mockApi.getBacktestRuns).toHaveBeenCalledWith(1, 10);
    });

    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));
    await waitFor(() => {
      expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1);
    });
    expect(mockApi.getBacktestTrades).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: /交易流水 2/ }));
    await waitFor(() => {
      expect(mockApi.getBacktestTrades).toHaveBeenCalledWith(101, expect.objectContaining({
        page: 1,
        pageSize: 20,
      }));
    });

    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.historyBtn" }));
    fireEvent.click(await screen.findByText("historic-run"));
    await waitFor(() => {
      expect(mockApi.getBacktestRun).toHaveBeenCalledWith(77);
      expect(mockApi.getBacktestTrades).toHaveBeenCalledWith(77, expect.objectContaining({
        page: 1,
        pageSize: 20,
      }));
    });

    expect(screen.getByText("#22")).toBeInTheDocument();
    expect(screen.queryByText("#11")).not.toBeInTheDocument();
  });

  it("shows the order plan intended price in the transaction ledger", async () => {
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("tab", { name: /交易流水 2/ }));

    await waitFor(() => expect(screen.getByText("¥10.20")).toBeInTheDocument());
    expect(screen.getByText("计划价")).toBeInTheDocument();
  });

  it("sends sorting and filters to the paginated ledger endpoint and preserves them in the URL", async () => {
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("tab", { name: /交易流水 2/ }));
    await waitFor(() => expect(mockApi.getBacktestTrades).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("backtest-trade-action-filter"), {
      target: { value: "BUY" },
    });
    fireEvent.change(screen.getByTestId("backtest-trade-status-filter"), {
      target: { value: "filled" },
    });
    fireEvent.change(screen.getByTestId("backtest-trade-symbol-filter"), {
      target: { value: "11" },
    });
    fireEvent.change(screen.getByTestId("backtest-trade-sort-filter"), {
      target: { value: "quantity" },
    });
    fireEvent.click(screen.getByTestId("backtest-trade-sort-direction"));

    await waitFor(() => {
      expect(mockApi.getBacktestTrades).toHaveBeenLastCalledWith(101, {
        page: 1,
        pageSize: 20,
        action: "BUY",
        symbolId: 11,
        executionStatus: "filled",
        sortBy: "quantity",
        sortDir: "asc",
      });
    });
    expect(window.location.search).toContain("bt_action=BUY");
    expect(window.location.search).toContain("bt_sort=quantity");
  });

  it("opens the exact DecisionRun linked to the selected transaction leg", async () => {
    mockApi.runPortfolioBacktest.mockResolvedValue({
      ...currentSummary,
      decision_run_ids: ["decision-run-day-1", "decision-run-day-2"],
    });
    mockApi.getBacktestTrades.mockResolvedValue({
      total: 1,
      page: 1,
      page_size: 20,
      items: [{
        id: 101,
        run_id: 101,
        symbol_id: 11,
        entry_date: "2025-01-02",
        entry_price: 10,
        quantity: 100,
        entry_cost: 5,
        decision_evidence_id: "entry-evidence-day-2",
        entry_decision_run_id: "decision-run-day-2",
      }],
    });

    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("tab", { name: /交易流水 2/ }));

    const evidenceButton = await screen.findByRole("button", { name: "已关联 · 查看证据" });
    fireEvent.click(evidenceButton);

    expect(screen.getByTestId("decision-evidence-drawer")).toHaveTextContent("decision-run-day-2");
    expect(screen.getByTestId("selected-evidence-id")).toHaveTextContent("entry-evidence-day-2");
  });

  it("renders requested, filled and remaining quantities from the execution contract", async () => {
    mockApi.runPortfolioBacktest.mockResolvedValue(currentSummary);
    mockApi.getBacktestTrades.mockResolvedValue({
      total: 1,
      page: 1,
      page_size: 20,
      items: [{
        id: 101,
        run_id: 101,
        symbol_id: 11,
        entry_date: "2025-01-02",
        entry_price: 10,
        quantity: 600,
        entry_cost: 5,
        decision_evidence_id: "entry-evidence",
        entry_decision_run_id: "decision-run",
        entry_requested_quantity: 1000,
        entry_filled_quantity: 600,
        entry_remaining_quantity: 400,
        entry_order_plan_status: "PARTIAL_FILL_PENDING",
        entry_unfilled_reason: "VOLUME_LIMIT",
      }],
    });

    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("tab", { name: /交易流水 2/ }));

    expect(await screen.findByText("600 / 1000 · 余 400")).toBeInTheDocument();
    expect(screen.getByText("部分成交 · 待重试")).toBeInTheDocument();
  });

  it("retains a data-gate failure in the result area and exposes the data explanation", async () => {
    mockApi.runPortfolioBacktest.mockRejectedValueOnce({
      status_code: 422,
      error_code: "BACKTEST_MARKET_DATA_MISSING",
      user_message: "该区间缺少可执行行情数据",
      correlation_id: "gate-20260821",
      detail: {
        code: "BACKTEST_MARKET_DATA_MISSING",
        message: "close/open bars are missing",
        issues: [{ symbol_id: 11, missing_dates: ["2025-01-02"] }],
      },
    });

    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));

    const gate = await screen.findByTestId("backtest-gate-state");
    expect(gate).toHaveTextContent("回测被数据门禁阻断");
    expect(gate).toHaveTextContent("BACKTEST_MARKET_DATA_MISSING");
    expect(gate).toHaveTextContent("gate-20260821");
    fireEvent.click(screen.getByTestId("backtest-gate-data-link"));
    expect(await screen.findByTestId("backtest-data-explanation")).toHaveTextContent("当前回测未生成可执行结果");
    expect(screen.queryByTestId("backtest-decision-snapshot")).not.toBeInTheDocument();
  });

  it("keeps a historically persisted data pause fail-closed", async () => {
    window.history.replaceState({}, "", "/?bt_run=77&bt_tab=data");
    mockApi.getBacktestRun.mockResolvedValueOnce({
      ...historicalRun,
      status: "completed",
      blocking_status: "DATA_INCOMPLETE_PAUSED",
      blocking_reasons: [{
        code: "DATA_MARKET_MISSING",
        message: "缺少冻结快照中的行情数据",
      }],
    });

    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);

    const gate = await screen.findByTestId("backtest-gate-state");
    expect(gate).toHaveTextContent("回测被数据门禁阻断");
    expect(gate).toHaveTextContent("DATA_INCOMPLETE_PAUSED");
    expect(screen.queryByText("暂无回测结果，请配置参数后开始回测")).not.toBeInTheDocument();
  });

  it("shows the decision snapshot and execution parameters for a successful run", async () => {
    mockApi.runPortfolioBacktest.mockResolvedValueOnce({
      ...currentSummary,
      decision_snapshot: {
        id: "snap-42",
        snapshot_hash: "sha256:abc123",
        factor_model_run_id: "model-run-7",
        factor_set_id: "factor-set-3",
        rule_id: "rule-9",
        rule_version: 4,
        pit_mode: "strict_pit_safe",
        snapshot_type: "production",
        effective_from: "2025-01-01",
      },
      commission_rate: 0.0003,
      stamp_tax_rate: 0.001,
      slippage_bps: 5,
      price_type: "next_open",
    });

    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));

    const snapshot = await screen.findByTestId("backtest-decision-snapshot");
    expect(snapshot).toHaveTextContent("执行快照");
    expect(snapshot).toHaveTextContent("snap-42");
    expect(snapshot).toHaveTextContent("sha256:abc123");
    expect(snapshot).toHaveTextContent("model-run-7");
    expect(snapshot).toHaveTextContent("strict_pit_safe");
    expect(snapshot).toHaveTextContent("成本与撮合参数");
    expect(screen.queryByTestId("backtest-gate-state")).not.toBeInTheDocument();
  });

  it("loads the daily position ledger only when the positions tab opens and supports filters", async () => {
    mockApi.getBacktestPositions.mockResolvedValue({
      total: 1,
      page: 1,
      page_size: 20,
      as_of_date: "2025-12-31",
      status: "OPEN",
      items: [{
        run_id: 101,
        symbol_id: 11,
        trade_date: "2025-01-02",
        opening_quantity: 400,
        buy_quantity: 200,
        sell_quantity: 0,
        closing_quantity: 600,
        status: "OPEN",
        as_of_date: "2025-12-31",
        mark_price: 10.5,
        market_value: 6300,
        portfolio_equity: 100000,
        weight: 0.063,
        buy_evidence_ids: ["buy-evidence-a", "buy-evidence-b"],
        sell_evidence_ids: ["sell-evidence-a"],
      }],
    });

    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1));
    expect(mockApi.getBacktestPositions).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: "持仓变化" }));
    await waitFor(() => {
      expect(mockApi.getBacktestPositions).toHaveBeenCalledWith(101, {
        page: 1,
        pageSize: 20,
      });
    });
    expect(screen.getByText("交易日")).toBeInTheDocument();
    expect(screen.getByText("期初")).toBeInTheDocument();
    expect(screen.getByText("买入变化")).toBeInTheDocument();
    expect(screen.getByText("卖出变化")).toBeInTheDocument();
    expect(screen.getByText("期末")).toBeInTheDocument();
    expect(screen.getByText("组合权益")).toBeInTheDocument();
    expect(screen.getByText("权重")).toBeInTheDocument();
    const row = screen.getByTestId("backtest-position-row-11-2025-01-02");
    expect(row).toHaveTextContent("2025-01-02");
    expect(row).toHaveTextContent("11");
    expect(row).toHaveTextContent("400");
    expect(row).toHaveTextContent("200");
    expect(row).toHaveTextContent("600");
    expect(row).toHaveTextContent("6,300.00");
    expect(row).toHaveTextContent("100,000.00");
    expect(row).toHaveTextContent("6.3%");
    expect(row).toHaveTextContent("buy-evidence-a, buy-evidence-b");
    expect(row).toHaveTextContent("sell-evidence-a");

    fireEvent.change(screen.getByTestId("backtest-position-status-filter"), { target: { value: "CLOSED" } });
    fireEvent.change(screen.getByTestId("backtest-position-as-of-date"), { target: { value: "2025-06-30" } });
    await waitFor(() => {
      expect(mockApi.getBacktestPositions).toHaveBeenLastCalledWith(101, {
        page: 1,
        pageSize: 20,
        status: "CLOSED",
        asOfDate: "2025-06-30",
      });
    });
  });

  it("clearly labels a position ledger approximated from historical trades", async () => {
    mockApi.getBacktestPositions.mockResolvedValue({
      total: 1,
      page: 1,
      page_size: 20,
      as_of_date: "2025-12-31",
      status: "OPEN",
      ledger_mode: "LEGACY_TRADE_APPROXIMATION",
      items: [{
        run_id: 101,
        symbol_id: 11,
        trade_date: "2025-01-02",
        opening_quantity: 0,
        buy_quantity: 100,
        sell_quantity: 0,
        closing_quantity: 100,
        status: "OPEN",
        as_of_date: "2025-12-31",
        mark_price: 10,
        market_value: 1000,
        portfolio_equity: 100000,
        weight: 0.01,
        buy_evidence_ids: [],
        sell_evidence_ids: [],
      }],
    });

    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("tab", { name: "持仓变化" }));

    const notice = await screen.findByTestId("backtest-position-legacy-ledger-notice");
    expect(notice).toHaveTextContent("历史近似账本");
    expect(notice).toHaveTextContent("历史交易记录近似推导");
    expect(notice).toHaveTextContent("不能作为成交或执行审计依据");
  });

  it("restores a deep-linked historical run and position filters after refresh", async () => {
    window.history.replaceState(
      {},
      "",
      "/?bt_run=77&bt_tab=positions&bt_position_page=2&bt_position_status=CLOSED&bt_position_as_of=2024-06-30",
    );
    mockApi.getBacktestPositions.mockResolvedValue({
      total: 21,
      page: 2,
      page_size: 20,
      as_of_date: "2024-06-30",
      status: "CLOSED",
      items: [],
    });

    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);

    await waitFor(() => expect(mockApi.getBacktestRun).toHaveBeenCalledWith(77));
    await waitFor(() => {
      expect(mockApi.getBacktestPositions).toHaveBeenCalledWith(77, {
        page: 2,
        pageSize: 20,
        status: "CLOSED",
        asOfDate: "2024-06-30",
      });
    });
    expect(screen.getByRole("tab", { name: "持仓变化", selected: true })).toBeInTheDocument();
    expect(window.location.search).toContain("bt_run=77");
    expect(window.location.search).toContain("bt_tab=positions");
    expect(window.location.search).toContain("bt_position_status=CLOSED");
    expect(window.location.search).toContain("bt_position_as_of=2024-06-30");
  });

  it("renders data explanation from the persisted PIT and benchmark contract", async () => {
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("tab", { name: "数据说明" }));
    const explanation = screen.getByTestId("backtest-data-explanation");
    expect(explanation).toHaveTextContent("2025-12-30T08:00:00Z");
    expect(explanation).toHaveTextContent("production_pit");
    expect(explanation).toHaveTextContent("BENCHMARK_INCOMPLETE");
    expect(explanation).toHaveTextContent("缺口 2 天");
    expect(explanation).toHaveTextContent("akshare");
    expect(explanation).toHaveTextContent("snap-42");
    expect(explanation).toHaveTextContent("model-run-7");
    expect(explanation).not.toHaveTextContent("无信号");
  });
});
