﻿﻿﻿﻿﻿import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const { mockApi, mockToast, mockSetActiveTab } = vi.hoisted(() => ({
  mockApi: {
    getPortfolioBacktestSourceStatus: vi.fn(),
    getBacktestRuns: vi.fn(),
    runPortfolioBacktest: vi.fn(),
    runBacktest: vi.fn(),
    getSymbols: vi.fn(),
    getBacktestRun: vi.fn(),
    getBacktestTrades: vi.fn(),
    getBacktestEvidence: vi.fn(),
    getBacktestPositions: vi.fn(),
    getCurrentFactorUsage: vi.fn(),
    evaluatePortfolioDecision: vi.fn(),
    postBacktestPrecheck: vi.fn(),
  },
  mockToast: vi.fn(),
  mockSetActiveTab: vi.fn(),
}));

vi.mock("../../../api/client", () => ({
  api: mockApi,
}));

vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ showToast: mockToast, setActiveTab: mockSetActiveTab }),
}));

vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
}));

vi.mock("antd", () => ({
  // 单日期 DatePicker：渲染原生 date input，onChange 回传带 format 的 dayjs 形对象
  DatePicker: Object.assign(
    function AntDatePickerMock({ "data-testid": testId, value, onChange, ...rest }: any) {
      return (
        <input
          type="date"
          data-testid={testId}
          value={value && typeof value.format === "function" ? value.format("YYYY-MM-DD") : ""}
          onChange={(e) => onChange?.(e.target.value ? { format: () => e.target.value } : null)}
          {...rest}
        />
      );
    },
    {
      RangePicker: ({ "data-testid": testId, value }: { "data-testid"?: string; value?: unknown }) => (
        <div data-testid={testId} data-value={JSON.stringify(value ?? null)} />
      ),
    },
  ),
  Select: ({ "data-testid": testId, value, onChange, options, ...rest }: any) => (
    <select
      data-testid={testId}
      value={value ?? ""}
      onChange={(e) => onChange?.(e.target.value)}
      {...rest}
    >
      <option value="">全部状态</option>
      {(options ?? []).map((o: { value: string; label: string }) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  ),
  App: {
    useApp: () => ({
      message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
    }),
  },
  Modal: Object.assign(function AntModalMock({ open, title, children, onCancel }: any) { return open ? (<div data-testid="antd-modal" data-title={title ?? ""}>{children}</div>) : null; }, { confirm: vi.fn(), warning: vi.fn() }),
  Input: ({ value, onChange, placeholder }: { value?: string; onChange?: (e: { target: { value: string } }) => void; placeholder?: string }) => (
    <input value={value ?? ""} placeholder={placeholder} onChange={(e) => onChange?.({ target: { value: e.target.value } })} />
  ),
  Space: ({ children }: { children?: React.ReactNode; size?: unknown; direction?: unknown; wrap?: boolean; style?: React.CSSProperties }) => (
    <>{children}</>
  ),
  Tag: ({ children, color }: { children?: React.ReactNode; color?: string; style?: React.CSSProperties }) => (
    <span data-antd-tag={color ?? "default"}>{children}</span>
  ),
  Tooltip: ({ children, title }: { children?: React.ReactNode; title?: React.ReactNode }) => (
    <span data-antd-tooltip={typeof title === "string" ? title : undefined}>{children}</span>
  ),
  Table: ({ columns, dataSource, rowKey }: any) => (
    <table data-testid="antd-table" data-columns={JSON.stringify(columns?.length ?? 0)} data-rowkey={String(rowKey ?? "")}>
      <tbody>
        {(dataSource ?? []).map((row: any, i: number) => (
          <tr key={i} data-row={JSON.stringify(row)} />
        ))}
      </tbody>
    </table>
  ),
  Button: ({ children, onClick, disabled, "data-testid": testId }: any) => (
    <button type="button" data-testid={testId} disabled={disabled} onClick={onClick}>
      {children}
    </button>
  ),
  Progress: ({ percent, showInfo, status, format, size }: any) => (
    <div data-testid="antd-progress" data-percent={percent ?? 0} data-status={status ?? "normal"} data-size={size ?? "default"}>
      <span data-testid="antd-progress-label">{typeof format === "function" ? format(percent ?? 0, undefined) : ""}</span>
    </div>
  ),
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

const passingPrecheck = {
  requested_trade_days: 365,
  usable_trade_days: 310,
  minimum_trade_days: 300,
  excluded_symbol_days: {},
  warnings: [],
  blocking_reasons: [],
  filter_config_hash: "cfg-hash-ok",
  production_fidelity: true,
};

describe("PortfolioBacktestCenter paged ledger contract", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.localStorage.clear();
    window.history.replaceState({}, "", "/");
    mockApi.getPortfolioBacktestSourceStatus.mockResolvedValue({
      enabled: true,
      env_flag: "PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED",
      source_label: "members",
    });
    mockApi.getBacktestRuns.mockResolvedValue([historicalRun]);
    mockApi.getSymbols.mockResolvedValue([{ id: 11, symbol: "600000" }]);
    mockApi.runBacktest.mockResolvedValue(historicalRun);
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
    mockApi.postBacktestPrecheck.mockResolvedValue(passingPrecheck);
  });

  it("submits single-stock commissions without overriding execution-date stamp duty", async () => {
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled={false} />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.modeSingle" }));
    fireEvent.change(screen.getByPlaceholderText("portfolioTrading.backtest.symbolPlaceholder"), {
      target: { value: "600000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startSingleRun" }));

    await waitFor(() => expect(mockApi.runBacktest).toHaveBeenCalledOnce());
    const payload = mockApi.runBacktest.mock.calls[0][0];
    expect(payload.cost_config.commission_rate).toBe(0.0003);
    expect(payload.cost_config).not.toHaveProperty("stamp_tax_rate");
  });

  it("keeps optional settings out of the way and preserves their current values when reopened", async () => {
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);

    const summary = screen.getByText("portfolioTrading.backtest.flow.moreSettings");
    const settings = summary.closest("details");
    expect(settings).not.toHaveAttribute("open");
    expect(summary.parentElement).toHaveTextContent("沪深300");
    expect(summary.parentElement).toHaveTextContent("0.03%");
    fireEvent.click(summary);
    const commission = screen.getByRole("textbox", { name: "portfolioTrading.backtest.commission" });
    fireEvent.change(commission, { target: { value: "0.05" } });
    fireEvent.click(summary);
    expect(settings).not.toHaveAttribute("open");
    expect(summary.parentElement).toHaveTextContent("0.05%");
    fireEvent.click(summary);
    expect(screen.getByRole("textbox", { name: "portfolioTrading.backtest.commission" })).toHaveValue("0.05");
    await waitFor(() => expect(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledOnce());
    expect(mockApi.runPortfolioBacktest.mock.calls[0][1]).toMatchObject({ commission_rate: 0.0005, benchmark: "沪深300", pit_mode: "production_pit" });
  });

  it("explains a failed data check in place and lets the user retry without changing dates", async () => {
    mockApi.postBacktestPrecheck.mockRejectedValueOnce(new Error("网络连接失败"));
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);

    const retry = await screen.findByRole("button", { name: "portfolioTrading.backtest.flow.checkAgain" });
    expect(screen.getByTestId("precheck-blockers-inline")).toHaveTextContent("网络连接失败");
    expect(screen.getByRole("button", { name: "portfolioTrading.backtest.flow.needsAttention" })).toBeDisabled();
    expect(mockApi.runPortfolioBacktest).not.toHaveBeenCalled();
    fireEvent.click(retry);
    const start = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(start).toBeEnabled());
    expect(mockApi.postBacktestPrecheck.mock.calls[1][0]).toEqual(mockApi.postBacktestPrecheck.mock.calls[0][0]);
    expect(mockApi.runPortfolioBacktest).not.toHaveBeenCalled();
  });

  it("keeps manual calculation tools collapsed and explains that they save records without placing orders", async () => {
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    const summary = screen.getByText("portfolioTrading.backtest.flow.calculationRecords");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "portfolioTrading.backtest.flow.createRecords" })).not.toBeVisible();
    fireEvent.click(summary);
    expect(screen.getByPlaceholderText("portfolioTrading.backtest.flow.snapshotNumber")).toBeVisible();
    expect(screen.getByText("portfolioTrading.backtest.flow.calculationRecordsHint")).toBeVisible();
    expect(mockApi.evaluatePortfolioDecision).not.toHaveBeenCalled();
    await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
  });

  it.each([
    { missing: "bars_date", combined: "2026-09-01", expected: null },
    { missing: "status_date", combined: "2026-09-01", expected: null },
    { missing: "factors_date", combined: "2026-09-01", expected: null },
    { missing: "none", combined: null, expected: null },
    { missing: "none", combined: "2026-08-30", expected: "2026-08-30" },
  ])("does not infer usable data from incomplete or rejected cutoffs: $missing / $combined", async ({ missing, combined, expected }) => {
    const dates = { bars_date: "2026-09-02", status_date: "2026-09-01", factors_date: "2026-09-03", unified_earliest: combined };
    mockApi.postBacktestPrecheck.mockResolvedValue({ ...passingPrecheck, data_cutoff: { ...dates, ...(missing === "none" ? {} : { [missing]: null }) } });
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    const capsule = await screen.findByTestId("data-cutoff-capsule");
    await waitFor(() => expect(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" })).toBeEnabled());
    await waitFor(() => expect(capsule).toHaveTextContent(missing === "bars_date" ? "行情日期: 未提供" : "行情截至: 2026-09-02"));
    if (expected) expect(capsule).toHaveTextContent(`可用数据截至: ${expected}`);
    else {
      expect(capsule).toHaveTextContent("可用数据日期: 未提供");
      expect(capsule).not.toHaveTextContent("可用数据截至:");
    }
    expect(mockApi.runPortfolioBacktest).not.toHaveBeenCalled();
  });

  it("opens data management for a missing portfolio trading range that can be managed there", async () => {
    mockApi.postBacktestPrecheck.mockResolvedValue({
      ...passingPrecheck,
      blocking_reasons: [{
        code: "NO_BACKTEST_SYMBOLS",
        title_zh: "无可回测标的",
        detail_zh: "当前组合没有可以回测的股票",
        fix_link: { tab: "settings-data-center", label_zh: "检查基础行情" },
      }],
    });
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    const notice = await screen.findByTestId("precheck-blockers-inline");
    expect(notice).toHaveTextContent("portfolioTrading.backtest.flow.noSymbols");
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.flow.openData" }));
    expect(mockSetActiveTab).toHaveBeenCalledWith("settings");
    expect(mockApi.runPortfolioBacktest).not.toHaveBeenCalled();
  });

  it.each([
    { code: "STATUS_DATA_UNAVAILABLE", key: "statusMissing", tab: "portfolio-backtest", detail: "security_status_daily coverage < 70%" },
    { code: "TRADE_CALENDAR_UNAVAILABLE", key: "calendarMissing", tab: "portfolio-backtest", detail: "trade_calendar is_trading_day unavailable" },
    { code: "STATUS_DATA_UNAVAILABLE", key: "statusMissing", tab: "settings-data-center", detail: "security_status_daily coverage < 70%" },
    { code: "PRICE_LIMIT_DATA_UNAVAILABLE", key: "priceLimitsMissing", tab: "settings-data-center", detail: "daily exchange bounds unavailable" },
    { code: "TRADE_CALENDAR_UNAVAILABLE", key: "calendarMissing", tab: "settings", detail: "trade_calendar is_trading_day unavailable" },
  ])("offers an existing historical inputs repair only for $code with $tab", async ({ code, key, tab, detail }) => {
    mockApi.postBacktestPrecheck.mockResolvedValue({
      ...passingPrecheck,
      blocking_reasons: [{ code, title_zh: `${code} fail-closed`, detail_zh: detail,
        evidence: { start_date: "2023-01-01", coverage: 0.4 },
        fix_link: { tab, label_zh: `补齐 ${detail}` },
      }],
    });
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByTestId("portfolio-backtest-range-3y"));
    fireEvent.change(screen.getByPlaceholderText("1,000,000"), { target: { value: "123456" } });
    const dates = screen.getByTestId("portfolio-backtest-range-picker").getAttribute("data-value");
    const notice = await screen.findByTestId("precheck-blockers-inline");
    expect(notice).toHaveTextContent(`portfolioTrading.backtest.flow.${key}`);
    expect(screen.queryByRole("button", { name: "portfolioTrading.backtest.flow.openData" })).not.toBeInTheDocument();
    if (code === "STATUS_DATA_UNAVAILABLE" || code === "PRICE_LIMIT_DATA_UNAVAILABLE") {
      expect(screen.getByRole("button", { name: "portfolioTrading.backtest.flow.importStatus" })).toBeVisible();
    } else {
      expect(screen.queryByRole("button", { name: "portfolioTrading.backtest.flow.importStatus" })).not.toBeInTheDocument();
    }

    fireEvent.click(screen.getByTestId("precheck-blockers-open"));
    const dialog = screen.getByTestId("antd-modal");
    expect(within(dialog).getByText(`portfolioTrading.backtest.flow.${key}`)).toBeVisible();
    const technical = within(dialog).getByText("查看错误详情").closest("details");
    expect(technical).not.toHaveAttribute("open");
    expect(technical).toHaveTextContent(code);
    expect(technical).toHaveTextContent(detail);
    expect(technical).toHaveTextContent('"coverage":0.4');
    const original = technical?.querySelector("pre");
    expect(original).not.toBeVisible();
    fireEvent.click(within(dialog).getByText("查看错误详情"));
    expect(original).toBeVisible();
    expect(screen.getByTestId("portfolio-backtest-range-picker")).toHaveAttribute("data-value", dates);
    expect(screen.getByPlaceholderText("1,000,000")).toHaveValue("123456");
    expect(mockSetActiveTab).not.toHaveBeenCalled();
    expect(mockApi.runPortfolioBacktest).not.toHaveBeenCalled();
    expect(mockApi.evaluatePortfolioDecision).not.toHaveBeenCalled();
    if (code === "STATUS_DATA_UNAVAILABLE" || code === "PRICE_LIMIT_DATA_UNAVAILABLE") {
      fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.flow.importStatus" }));
      expect(mockSetActiveTab).toHaveBeenCalledWith("settings");
      expect(window.localStorage.getItem("settings_active_section")).toBe("data-center");
      expect(window.localStorage.getItem("settings_data_center_tab")).toBe("coverage");
    }
  });

  it.each(["UNRECOGNIZED_DATA_ERROR", "PRECHECK_FAILED"])("does not guess a data repair route for %s even when a generic settings link is present", async (code) => {
    mockApi.postBacktestPrecheck.mockResolvedValue({ ...passingPrecheck, blocking_reasons: [{
      code, title_zh: "暂时无法完成检查", detail_zh: "请重试。", fix_link: { tab: "settings", label_zh: "检查设置" },
    }] });
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    await screen.findByTestId("precheck-blockers-inline");
    expect(screen.queryByRole("button", { name: "portfolioTrading.backtest.flow.openData" })).not.toBeInTheDocument();
    expect(mockApi.runPortfolioBacktest).not.toHaveBeenCalled();
    expect(mockApi.evaluatePortfolioDecision).not.toHaveBeenCalled();
  });

  it("lets a failed submission retry from the result area with the same settings and a fresh data check", async () => {
    mockApi.runPortfolioBacktest.mockRejectedValueOnce(new Error("网络连接中断"));
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    const start = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    const retry = await screen.findByRole("button", { name: "portfolioTrading.backtest.flow.runAgain" });
    expect(screen.getByTestId("backtest-gate-state")).toHaveTextContent("网络连接中断");
    const firstPayload = mockApi.runPortfolioBacktest.mock.calls[0][1];
    const checksBeforeRetry = mockApi.postBacktestPrecheck.mock.calls.length;
    fireEvent.click(retry);
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(2));
    expect(mockApi.postBacktestPrecheck.mock.calls.length).toBeGreaterThan(checksBeforeRetry);
    expect(mockApi.runPortfolioBacktest.mock.calls[1][1]).toEqual(firstPayload);
    await waitFor(() => expect(screen.queryByTestId("backtest-gate-state")).not.toBeInTheDocument());
  });

  it("can research the saved portfolio strategy while automatic trading stays off", async () => {
    const onNavigate = vi.fn();
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled={false} onNavigate={onNavigate} />);
    const start = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(start).toBeEnabled());
    expect(screen.queryByText("portfolioTrading.backtest.autoTradeRequiredHint")).not.toBeInTheDocument();
    fireEvent.click(start);
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledOnce());
    expect(mockApi.runPortfolioBacktest.mock.calls[0][1]).toMatchObject({ pit_mode: "production_pit" });
    expect(onNavigate).not.toHaveBeenCalled();
    expect(mockApi.evaluatePortfolioDecision).not.toHaveBeenCalled();
  });

  it("recovers a temporary progress read failure by querying the same run without submitting another backtest", async () => {
    mockApi.runPortfolioBacktest.mockResolvedValue({ ...currentSummary, status: "running" });
    mockApi.getBacktestRun.mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValue({ ...historicalRun, id: 101, status: "running", stage: "running", progress_pct: 65 });
    const view = render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled={false} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" })).toBeEnabled());
    vi.useFakeTimers();
    try {
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" })); });
      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
      expect(screen.getByText("暂时读不到回测进度，正在重试。")).toBeVisible();
      await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
      expect(screen.getByTestId("antd-progress-label")).toHaveTextContent("65%");
      expect(screen.queryByText("暂时读不到回测进度，正在重试。")).not.toBeInTheDocument();
      expect(mockApi.getBacktestRun.mock.calls.every(([id]) => id === 101)).toBe(true);
      expect(mockApi.runPortfolioBacktest).toHaveBeenCalledOnce();
      expect(mockApi.evaluatePortfolioDecision).not.toHaveBeenCalled();
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it("keeps the original task after repeated progress failures and can resume reading it in place", async () => {
    mockApi.runPortfolioBacktest.mockResolvedValue({ ...currentSummary, status: "running" });
    mockApi.getBacktestRun.mockRejectedValue(new Error("network down"));
    const view = render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled={false} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" })).toBeEnabled());
    vi.useFakeTimers();
    try {
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" })); });
      await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
      expect(screen.getByText("暂时读不到回测进度，任务可能仍在运行。")).toBeVisible();
      expect(screen.getByRole("button", { name: "portfolioTrading.backtest.flow.running" })).toBeDisabled();
      const attempts = mockApi.getBacktestRun.mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
      expect(mockApi.getBacktestRun.mock.calls.length).toBe(attempts);
      expect(screen.queryByText("任务疑似卡住")).not.toBeInTheDocument();
      mockApi.getBacktestRun.mockResolvedValue({ ...historicalRun, id: 101, status: "running", stage: "running", progress_pct: 85 });
      fireEvent.click(screen.getByRole("button", { name: "重新查看进度" }));
      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
      expect(screen.getByTestId("antd-progress-label")).toHaveTextContent("85%");
      expect(screen.queryByRole("button", { name: "重新查看进度" })).not.toBeInTheDocument();
      expect(mockApi.runPortfolioBacktest).toHaveBeenCalledOnce();
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it("shows the finished result of the original task when its progress reaches completion", async () => {
    mockApi.runPortfolioBacktest.mockResolvedValue({ ...currentSummary, status: "running" });
    mockApi.getBacktestRun.mockResolvedValue({ ...historicalRun, id: 101, status: "completed", stage: "success", total_return_pct: 0.1234 });
    const view = render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled={false} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" })).toBeEnabled());
    vi.useFakeTimers();
    try {
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" })); });
      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
      expect(screen.getAllByText("+12.3%").length).toBeGreaterThan(0);
      expect(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" })).toBeEnabled();
      expect(mockApi.runPortfolioBacktest).toHaveBeenCalledOnce();
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it("aborts a progress request and stops polling when the backtest page is left", async () => {
    let resolveProgress!: (value: unknown) => void;
    mockApi.runPortfolioBacktest.mockResolvedValue({ ...currentSummary, status: "running" });
    mockApi.getBacktestRun.mockImplementation(() => new Promise((resolve) => { resolveProgress = resolve; }));
    const view = render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled={false} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" })).toBeEnabled());
    vi.useFakeTimers();
    try {
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" })); });
      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
      const signal = mockApi.getBacktestRun.mock.calls[0][1] as AbortSignal;
      expect(signal.aborted).toBe(false);
      view.unmount();
      expect(signal.aborted).toBe(true);
      await act(async () => { resolveProgress({ ...historicalRun, id: 101 }); await vi.advanceTimersByTimeAsync(10000); });
      expect(mockApi.getBacktestRun).toHaveBeenCalledOnce();
      expect(mockApi.runPortfolioBacktest).toHaveBeenCalledOnce();
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it.each([
    { name: "网络中断", error: new Error("网络连接中断") },
    { name: "502", error: { status_code: 502, message: "Bad Gateway" } },
    { name: "404", error: { status_code: 404, message: "Not Found" } },
    { name: "501", error: { status_code: 501, message: "NOT_IMPLEMENTED" } },
  ])("does not submit when the final data check fails with $name and requires another explicit start after recovery", async ({ error }) => {
    mockApi.postBacktestPrecheck
      .mockResolvedValueOnce(passingPrecheck)
      .mockRejectedValueOnce(error)
      .mockResolvedValue(passingPrecheck);
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    const start = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);

    const retry = await screen.findByRole("button", { name: "portfolioTrading.backtest.flow.checkAgain" });
    expect(screen.getByRole("button", { name: "portfolioTrading.backtest.flow.needsAttention" })).toBeDisabled();
    expect(mockApi.runPortfolioBacktest).not.toHaveBeenCalled();
    expect(mockApi.evaluatePortfolioDecision).not.toHaveBeenCalled();
    fireEvent.click(retry);
    const recoveredStart = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(recoveredStart).toBeEnabled());
    expect(mockApi.runPortfolioBacktest).not.toHaveBeenCalled();
    fireEvent.click(recoveredStart);
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledOnce());
    expect(mockApi.postBacktestPrecheck.mock.calls[1][0]).toEqual(mockApi.postBacktestPrecheck.mock.calls[0][0]);
  });

  it.each(["response", "error"])("keeps a new portfolio submission running when the old portfolio %s arrives", async (lateKind) => {
    let resolveOld!: (value: unknown) => void;
    let rejectOld!: (error: Error) => void;
    let resolveCurrent!: (value: unknown) => void;
    mockApi.runPortfolioBacktest
      .mockImplementationOnce(() => new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveCurrent = resolve; }));
    const view = render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    const firstRun = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(firstRun).toBeEnabled());
    fireEvent.click(firstRun);
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1));

    view.rerender(<PortfolioBacktestCenter portfolioId={2} autoTradeEnabled />);
    const nextRun = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(nextRun).toBeEnabled());
    fireEvent.click(nextRun);
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(2));
    mockToast.mockClear();
    await act(async () => {
      if (lateKind === "response") resolveOld({ ...currentSummary, run_id: 701 });
      else rejectOld(new Error("old portfolio submission failed"));
    });
    expect(screen.getByRole("button", { name: "portfolioTrading.backtest.flow.submitting" })).toBeDisabled();
    expect(mockToast).not.toHaveBeenCalled();
    expect(new URLSearchParams(window.location.search).get("bt_run")).toBeNull();

    await act(async () => { resolveCurrent({ ...currentSummary, portfolio_id: 2, run_id: 702 }); });
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("bt_run")).toBe("702"));
  });

  it("does not let an old run's delayed completion unlock a new portfolio submission", async () => {
    let resolveCurrent!: (value: unknown) => void;
    let resolveOldProgress!: (value: unknown) => void;
    mockApi.getBacktestRun.mockImplementationOnce(() => new Promise((resolve) => { resolveOldProgress = resolve; }));
    mockApi.runPortfolioBacktest.mockResolvedValueOnce({ ...currentSummary, status: "running" })
      .mockImplementationOnce(() => new Promise((resolve) => { resolveCurrent = resolve; }));
    const view = render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    const firstRun = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(firstRun).toBeEnabled());
    fireEvent.click(firstRun);
    await waitFor(() => expect(mockApi.getBacktestRun).toHaveBeenCalledWith(101, expect.any(AbortSignal)));
    view.rerender(<PortfolioBacktestCenter portfolioId={2} autoTradeEnabled />);
    const nextRun = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(nextRun).toBeEnabled());
    fireEvent.click(nextRun);
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(2));
    await act(async () => { resolveOldProgress({ ...historicalRun, id: 101 }); });
    expect(screen.getByRole("button", { name: "portfolioTrading.backtest.flow.submitting" })).toBeDisabled();
    await act(async () => { resolveCurrent({ ...currentSummary, portfolio_id: 2, run_id: 702 }); });
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("bt_run")).toBe("702"));
  });

  it.each([
    ["symbol", "response"], ["symbol", "error"],
    ["submission", "response"], ["submission", "error"],
    ["detail", "response"], ["detail", "error"],
  ])("ignores an old portfolio single-stock %s %s while the new submission is running", async (stage, lateKind) => {
    let resolveOld!: (value: unknown) => void;
    let rejectOld!: (error: Error) => void;
    let resolveCurrent!: (value: unknown) => void;
    const oldRead = new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; });
    const currentRun = new Promise((resolve) => { resolveCurrent = resolve; });
    mockApi.getSymbols.mockImplementation((code: string) => stage === "symbol" && code === "600000"
      ? oldRead : Promise.resolve([{ id: 11, symbol: code }]));
    mockApi.runBacktest.mockImplementation((payload: { portfolio_id: number }) => payload.portfolio_id === 2
      ? currentRun : stage === "submission" ? oldRead : Promise.resolve(historicalRun));
    mockApi.getBacktestRun.mockImplementation((runId: number) => runId === 77
      ? stage === "detail" ? oldRead : Promise.resolve(historicalRun)
      : Promise.resolve({ ...historicalRun, id: 88, portfolio_id: 2, total_return_pct: 0.1234 }));
    const view = render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.modeSingle" }));
    fireEvent.change(screen.getByPlaceholderText("portfolioTrading.backtest.symbolPlaceholder"), { target: { value: "600000" } });
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startSingleRun" }));
    await waitFor(() => {
      if (stage === "symbol") expect(mockApi.getSymbols).toHaveBeenCalledWith("600000", { pageSize: 50 });
      else if (stage === "submission") expect(mockApi.runBacktest).toHaveBeenCalledTimes(1);
      else expect(mockApi.getBacktestRun).toHaveBeenCalledWith(77);
    });
    view.rerender(<PortfolioBacktestCenter portfolioId={2} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.modeSingle" }));
    fireEvent.change(screen.getByPlaceholderText("portfolioTrading.backtest.symbolPlaceholder"), { target: { value: "000001" } });
    fireEvent.click(await screen.findByRole("button", { name: "portfolioTrading.backtest.startSingleRun" }));
    await waitFor(() => expect(mockApi.runBacktest).toHaveBeenCalledWith(expect.objectContaining({ portfolio_id: 2 })));
    mockToast.mockClear();
    await act(async () => {
      if (lateKind === "error") rejectOld(new Error("old single-stock request failed"));
      else resolveOld(stage === "symbol" ? [{ id: 11, symbol: "600000" }] : historicalRun);
    });
    expect(screen.getByRole("button", { name: "portfolioTrading.backtest.running" })).toBeDisabled();
    expect(mockToast).not.toHaveBeenCalled();
    await act(async () => { resolveCurrent({ ...historicalRun, id: 88, portfolio_id: 2 }); });
    expect(await screen.findByText("+12.3%")).toBeInTheDocument();
  });

  it("shows a retryable history read failure and only calls a successful empty list empty", async () => {
    mockApi.getBacktestRuns.mockRejectedValueOnce(new Error("history offline"));
    mockApi.getBacktestRuns.mockResolvedValue([]);
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.historyBtn" }));

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("portfolioTrading.backtest.readRecovery.historyFailed");
    expect(failure).toHaveTextContent("history offline");
    expect(screen.queryByText("portfolioTrading.backtest.historyEmpty")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.readRecovery.retry" }));
    expect(await screen.findByText("portfolioTrading.backtest.historyEmpty")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each(["response", "error"])("ignores a late history %s after switching portfolios", async (lateKind) => {
    let resolveOld!: (value: unknown) => void;
    let rejectOld!: (error: Error) => void;
    mockApi.getBacktestRuns.mockImplementationOnce(() => new Promise((resolve, reject) => {
      resolveOld = resolve; rejectOld = reject;
    }));
    mockApi.getBacktestRuns.mockResolvedValueOnce([{ ...historicalRun, id: 88, portfolio_id: 2, run_name: "second portfolio history" }]);
    const view = render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    await waitFor(() => expect(mockApi.getBacktestRuns).toHaveBeenCalledWith(1, 10));
    view.rerender(<PortfolioBacktestCenter portfolioId={2} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.historyBtn" }));
    expect(await screen.findByText("second portfolio history")).toBeInTheDocument();
    await act(async () => {
      if (lateKind === "response") resolveOld([historicalRun]);
      else rejectOld(new Error("old portfolio offline"));
    });
    expect(screen.getByText("second portfolio history")).toBeInTheDocument();
    expect(screen.queryByText("historic-run")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps a ledger read failure distinct from loading and a successful empty ledger", async () => {
    let resolveRetry!: (value: unknown) => void;
    mockApi.getBacktestTrades.mockRejectedValueOnce(new Error("ledger offline"));
    mockApi.getBacktestTrades.mockImplementationOnce(() => new Promise((resolve) => { resolveRetry = resolve; }));
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    const start = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    fireEvent.click(await screen.findByRole("tab", { name: /交易流水 2/ }));

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("portfolioTrading.backtest.readRecovery.tradesFailed");
    expect(failure).toHaveTextContent("ledger offline");
    expect(screen.queryByText("本次回测没有形成交易计划")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.readRecovery.retry" }));
    expect(await screen.findByText("正在加载交易流水…")).toBeInTheDocument();
    expect(screen.queryByText("本次回测没有形成交易计划")).not.toBeInTheDocument();
    await act(async () => resolveRetry({ items: [], total: 0 }));
    expect(await screen.findByText("本次回测没有形成交易计划")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not claim there are no rejected decisions when evidence reading fails", async () => {
    mockApi.getBacktestEvidence.mockRejectedValueOnce(new Error("evidence offline"));
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    const start = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    fireEvent.click(await screen.findByRole("tab", { name: "拒绝记录" }));

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("portfolioTrading.backtest.readRecovery.evidenceFailed");
    expect(failure).toHaveTextContent("evidence offline");
    expect(screen.queryByText("本次回测没有 REJECTED / DATA_BLOCKED 记录。")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.readRecovery.retry" }));
    expect(await screen.findByText("本次回测没有 REJECTED / DATA_BLOCKED 记录。")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows history result loading and a persistent retryable read error instead of old empty conclusions", async () => {
    let rejectRead!: (error: Error) => void;
    mockApi.getBacktestRun.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectRead = reject; }));
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.historyBtn" }));
    fireEvent.click(await screen.findByText("historic-run"));
    expect(screen.getByRole("status")).toHaveTextContent("portfolioTrading.backtest.readRecovery.runLoading");
    expect(screen.queryByText("portfolioTrading.backtest.noResultTip")).not.toBeInTheDocument();
    await act(async () => rejectRead(new Error("history result offline")));
    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("portfolioTrading.backtest.readRecovery.runFailed");
    expect(failure).toHaveTextContent("history result offline");
    expect(screen.queryByText("portfolioTrading.backtest.noResultTip")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.readRecovery.retry" }));
    fireEvent.click(await screen.findByRole("tab", { name: /交易流水 1/ }));
    expect(await screen.findByText("#22")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each(["response", "error"])("does not let a late historical result %s replace a newly submitted run", async (lateKind) => {
    let resolveOld!: (value: unknown) => void;
    let rejectOld!: (error: Error) => void;
    mockApi.getBacktestRun.mockImplementationOnce(() => new Promise((resolve, reject) => {
      resolveOld = resolve; rejectOld = reject;
    }));
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.historyBtn" }));
    fireEvent.click(await screen.findByText("historic-run"));
    const start = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    fireEvent.click(await screen.findByRole("tab", { name: /交易流水 2/ }));
    expect(await screen.findByText("#11")).toBeInTheDocument();
    await act(async () => {
      if (lateKind === "response") resolveOld(historicalRun);
      else rejectOld(new Error("old history failure"));
    });
    expect(screen.getByText("#11")).toBeInTheDocument();
    expect(screen.queryByText("#22")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each(["response", "error"])("keeps the latest selected history run after an older %s arrives", async (lateKind) => {
    let resolveOld!: (value: unknown) => void;
    let rejectOld!: (error: Error) => void;
    const newer = { ...historicalRun, id: 88, run_name: "newer history run" };
    mockApi.getBacktestRuns.mockResolvedValue([historicalRun, newer]);
    mockApi.getBacktestRun.mockImplementationOnce(() => new Promise((resolve, reject) => {
      resolveOld = resolve; rejectOld = reject;
    }));
    mockApi.getBacktestRun.mockResolvedValue(newer);
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.historyBtn" }));
    fireEvent.click(await screen.findByText("historic-run"));
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.historyBtn" }));
    fireEvent.click(screen.getByText("newer history run"));
    await screen.findByRole("tab", { name: /交易流水 1/ });
    await act(async () => {
      if (lateKind === "response") resolveOld(historicalRun);
      else rejectOld(new Error("stale history detail error"));
    });
    expect(new URLSearchParams(window.location.search).get("bt_run")).toBe("88");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("clears an obsolete history loading state when the active portfolio changes", async () => {
    let rejectOld!: (error: Error) => void;
    mockApi.getBacktestRun.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOld = reject; }));
    const view = render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.historyBtn" }));
    fireEvent.click(await screen.findByText("historic-run"));
    expect(screen.getByRole("status")).toBeInTheDocument();
    view.rerender(<PortfolioBacktestCenter portfolioId={2} autoTradeEnabled />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await act(async () => rejectOld(new Error("prior portfolio run failed")));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("portfolioTrading.backtest.noResultTip")).toBeInTheDocument();
  });

  it.each(["response", "error"])("ignores an older ledger filter %s while keeping the current query", async (lateKind) => {
    let resolveOld!: (value: unknown) => void;
    let rejectOld!: (error: Error) => void;
    mockApi.getBacktestTrades.mockImplementationOnce(() => new Promise((resolve, reject) => {
      resolveOld = resolve; rejectOld = reject;
    }));
    mockApi.getBacktestTrades.mockResolvedValueOnce({ total: 1, items: [
      { id: 22, run_id: 101, symbol_id: 22, entry_date: "2025-01-02", entry_price: 10, quantity: 100 },
    ] });
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    const start = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    fireEvent.click(await screen.findByRole("tab", { name: /交易流水 2/ }));
    await waitFor(() => expect(mockApi.getBacktestTrades).toHaveBeenCalled());
    fireEvent.change(screen.getByRole("textbox", { name: "证券 ID 筛选" }), { target: { value: "22" } });
    expect(await screen.findByText("#22")).toBeInTheDocument();
    await act(async () => {
      if (lateKind === "response") resolveOld({ total: 0, items: [] });
      else rejectOld(new Error("stale filtered ledger error"));
    });
    expect(screen.getByText("#22")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("本次回测没有形成交易计划")).not.toBeInTheDocument();
  });

  it.each(["response", "error"])("ignores prior-run rejected evidence %s after choosing another run", async (lateKind) => {
    let resolveOld!: (value: unknown) => void;
    let rejectOld!: (error: Error) => void;
    mockApi.getBacktestEvidence.mockImplementationOnce(() => new Promise((resolve, reject) => {
      resolveOld = resolve; rejectOld = reject;
    }));
    mockApi.getBacktestEvidence.mockResolvedValueOnce({ total: 1, items: [
      { id: "new-evidence", decision_run_id: "new-decision", symbol_id: 22,
        trade_date: "2024-01-02", action: "REJECTED", rejection_reason: "new run risk constraint" },
    ] });
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    const start = await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    fireEvent.click(await screen.findByRole("tab", { name: "拒绝记录" }));
    await waitFor(() => expect(mockApi.getBacktestEvidence).toHaveBeenCalledWith(101, expect.anything()));
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.historyBtn" }));
    fireEvent.click(await screen.findByText("historic-run"));
    expect(await screen.findByText("new run risk constraint")).toBeInTheDocument();
    await act(async () => {
      if (lateKind === "response") resolveOld({ total: 0, items: [] });
      else rejectOld(new Error("stale rejected evidence error"));
    });
    expect(screen.getByText("new run risk constraint")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("本次回测没有 REJECTED / DATA_BLOCKED 记录。")).not.toBeInTheDocument();
  });

  it("loads the ledger only after the transaction tab opens and replaces it for a selected historical run", async () => {
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);

    await waitFor(() => {
      expect(mockApi.getBacktestRuns).toHaveBeenCalledWith(1, 10);
    });

    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" });
      expect(btn).toBeEnabled();
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
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" });
      expect(btn).toBeEnabled();
    });
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));
    await waitFor(() => expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("tab", { name: /交易流水 2/ }));

    await waitFor(() => expect(screen.getByText("¥10.20")).toBeInTheDocument());
    expect(screen.getByText("计划价")).toBeInTheDocument();
  });

  it("sends sorting and filters to the paginated ledger endpoint and preserves them in the URL", async () => {
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" });
      expect(btn).toBeEnabled();
    });
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
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" });
      expect(btn).toBeEnabled();
    });
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
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" });
      expect(btn).toBeEnabled();
    });
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
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" });
      expect(btn).toBeEnabled();
    });
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));

    const gate = await screen.findByTestId("backtest-gate-state");
    expect(gate).toHaveTextContent("portfolioTrading.backtest.flow.dataNotReady");
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
    expect(gate).toHaveTextContent("portfolioTrading.backtest.flow.dataNotReady");
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
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" });
      expect(btn).toBeEnabled();
    });
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
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" });
      expect(btn).toBeEnabled();
    });
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
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" });
      expect(btn).toBeEnabled();
    });
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
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" });
      expect(btn).toBeEnabled();
    });
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
  // ========== 新增 4 个预检场景 ==========

  it("precheck loading - 预检中提示 + 按钮 disabled", async () => {
    let resolvePrecheck: ((v: any) => void) | null = null;
    const pendingPrecheck = new Promise<any>((resolve) => { resolvePrecheck = resolve; });
    mockApi.postBacktestPrecheck.mockReturnValueOnce(pendingPrecheck);
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    // T5.1: 首进自动预检 300ms debounce 后发起；pending 下按钮文案 = "预检中..." 且 disabled
    const startBtn = await screen.findByRole("button", { name: "portfolioTrading.backtest.flow.checking" });
    expect(startBtn).toBeInTheDocument();
    expect(startBtn).toBeDisabled();
    // loading 中不允许调用 run
    expect(mockApi.runPortfolioBacktest).not.toHaveBeenCalled();
    (resolvePrecheck as unknown as (v: any) => void)?.(passingPrecheck);
    // 预检通过后按钮自动解锁并恢复 startRun 文案，此测试不点击 run
    await screen.findByRole("button", { name: "portfolioTrading.backtest.startRun" });
  });

  it("预检阻断 INSUFFICIENT 显缺口 - 缺口 39 天 + 按钮 disabled + 不发起 run", async () => {
    mockApi.postBacktestPrecheck.mockResolvedValueOnce({
      requested_trade_days: 365,
      usable_trade_days: 261,
      minimum_trade_days: 300,
      excluded_symbol_days: {},
      warnings: [],
      blocking_reasons: [{
        code: "INSUFFICIENT_TRADE_DAYS",
        severity: "error",
        evidence: { actual_usable: 261, minimum_required: 300, gap_days: 39 },
      }],
      filter_config_hash: "cfg-hash-insufficient",
      production_fidelity: false,
    });
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    // T5.1 自动预检完成后出现阻断结果；T5.3 按钮变成 "已阻断..." 且 disabled
    await waitFor(() => { expect(mockApi.postBacktestPrecheck).toHaveBeenCalledTimes(1); });
    // T5.1 新语义：自动预检**不得**弹盖住整页的模态，只能给不遮挡的行内提示。
    // （旧行为会让新装机一进「组合交易」就被一份未索取的报告锁在原地，
    // 并表现为端到端用例点不到其他子导航。）
    expect(screen.queryByTestId("antd-modal")).not.toBeInTheDocument();
    expect(screen.getByTestId("precheck-blockers-inline")).toBeInTheDocument();
    expect(screen.getByTestId("precheck-blockers-count").textContent).toBe("1");
    // 缺口详情仍然可看，但必须由用户主动打开
    fireEvent.click(screen.getByTestId("precheck-blockers-open"));
    const gapText = await screen.findByText(/39 天/);
    expect(gapText).toBeInTheDocument();
    const submitBtn = await screen.findByRole("button", { name: "portfolioTrading.backtest.flow.needsAttention" });
    expect(submitBtn).toBeInTheDocument();
    expect(submitBtn).toBeDisabled();
    expect(mockApi.runPortfolioBacktest).not.toHaveBeenCalled();
  });

  it("预检 200 + 阻断项不是错误信封 - 不亮 UNKNOWN_ERROR/裸 JSON，并摊开下一步指引", async () => {
    // PT-DEF-25a/c：预检正常返回 200 带 blocking_reasons 时，过去会被塞进
    // normalizeBackendError → 回退成 UNKNOWN_ERROR 并把整个响应体当正文打成 JSON。
    // 现在只渲染阻断项列表，且每条要给出自带的修复指引。
    mockApi.postBacktestPrecheck.mockResolvedValueOnce({
      requested_trade_days: 261,
      usable_trade_days: 261,
      minimum_trade_days: 240,
      excluded_symbol_days: {},
      warnings: [],
      blocking_reasons: [{
        code: "INSUFFICIENT_TRADE_DAYS",
        severity: "error",
        title_zh: "回测区间交易日不足（阻断）",
        detail_zh: "当前可用交易日 261 天，低于最低要求 300 天。",
        evidence: { actual_usable: 261, minimum_required: 300, gap_days: 39 },
        fix_link: { tab: "portfolio-backtest", label_zh: "调整回测区间参数" },
      }],
      filter_config_hash: "cfg-hash-25a",
      production_fidelity: true,
    });
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    await waitFor(() => { expect(mockApi.postBacktestPrecheck).toHaveBeenCalledTimes(1); });

    // T5.1：自动预检不再弹模态；下面的断言验证的是“用户主动点开”后的内容渲染，
    // 所以先确认不弹、再点入口，原PT-DEF-25a 的保护全部保留。
    expect(screen.queryByTestId("antd-modal")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("precheck-blockers-open"));

    // 注意：本文件的 antd Modal mock 把 data-testid 固定成 "antd-modal"，
    // 所以这里按内容断言，而不是查 blocking-error-modal。
    expect(await screen.findByTestId("antd-modal")).toBeInTheDocument();
    expect(screen.queryByTestId("structured-error-7-fields")).not.toBeInTheDocument();
    expect(screen.queryByText(/UNKNOWN_ERROR/)).not.toBeInTheDocument();
    // 响应体字段名不能被当成正文糊出来（那就是"裸 JSON"的症状）
    expect(screen.queryByText(/usable_trade_days/)).not.toBeInTheDocument();
    expect(await screen.findByText(/调整回测区间参数/)).toBeInTheDocument();
  });

  it("预检阻断项不把后端 tab 常量泄到界面（报告 §三十四）", async () => {
    // 已登记的去处→显中文名；未登记的→宁可不显示。两种情况下都不能出现常量本身。
    mockApi.postBacktestPrecheck.mockResolvedValueOnce({
      requested_trade_days: 261,
      usable_trade_days: 261,
      minimum_trade_days: 240,
      excluded_symbol_days: {},
      warnings: [],
      blocking_reasons: [
        {
          code: "NO_BACKTEST_SYMBOLS", severity: "error", category: "universe",
          title_zh: "无可回测标的（阻断）", detail_zh: "当前范围内没有任何可回测标的。",
          evidence: { symbol_count: 0 },
          fix_link: { tab: "settings-data-center", label_zh: "去同步基础数据" },
        },
        {
          code: "UNMAPPED_FIX_TAB_SAMPLE", severity: "error", category: "data",
          title_zh: "未登记去处（回归用）", detail_zh: "tab 是后端新加的常量。",
          evidence: {},
          fix_link: { tab: "brand_new_internal_tab", label_zh: "去看运行日志" },
        },
      ],
      filter_config_hash: "cfg-tab-leak",
      production_fidelity: true,
    });
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    await waitFor(() => { expect(mockApi.postBacktestPrecheck).toHaveBeenCalledTimes(1); });

    // T5.1：不自动弹模态，详情靠行内入口主动打开
    expect(screen.queryByTestId("antd-modal")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("precheck-blockers-open"));

    const fixes = await screen.findAllByTestId("precheck-blocker-fix");
    expect(fixes.length).toBe(2);
    // 本文件把 t mock 成返回 key，所以能确认“走了映射”而不是透传常量
    expect(fixes[0].textContent).toContain("precheckFixTab.dataCenter");
    // 两条都不能把后端常量留在界面上
    expect(fixes[0].textContent).not.toContain("settings-data-center");
    expect(fixes[1].textContent).not.toContain("brand_new_internal_tab");
    // 未登记的去处：指引文字保留，括号里的去处宁可不显示
    expect(fixes[1].textContent).toContain("去看运行日志");
    expect(fixes[1].querySelector("[data-field='fix_tab_label']")).toBeNull();
  });

  it("预检通过点击提交成功 - POST 调用 + run_id 2031 + 日期保留", async () => {
    mockApi.postBacktestPrecheck.mockResolvedValueOnce({ ...passingPrecheck });
    const run2031 = { ...currentSummary, run_id: 2031, run_name: "run-2031-success" };
    mockApi.runPortfolioBacktest.mockResolvedValueOnce(run2031);
    mockApi.getBacktestRuns.mockResolvedValueOnce([{ ...historicalRun, id: 2031, run_name: "run-2031-success" }]);
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" });
      expect(btn).toBeEnabled();
    });
    fireEvent.click(screen.getByRole("button", { name: "portfolioTrading.backtest.startRun" }));
    await waitFor(() => {
      // T5.1 首进自动预检 1 次 + onRun 二次预检 1 次 = 至少 2 次
      expect(mockApi.postBacktestPrecheck).toHaveBeenCalledTimes(2);
      expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      const successCalls = mockToast.mock.calls.filter((cc) => cc[0] === "success").length;
      const runRefreshed = mockApi.getBacktestRuns.mock.calls.length >= 1;
      expect(successCalls + (runRefreshed ? 1 : 0)).toBeGreaterThanOrEqual(1);
    });
    expect(mockApi.runPortfolioBacktest).toHaveBeenCalledTimes(1);
  });

  it("预检 500 fail-closed - 按钮 disabled + 不发起 run + error toast", async () => {
    mockApi.postBacktestPrecheck.mockRejectedValueOnce({
      status_code: 500,
      message: "500 Internal Server Error",
    });
    render(<PortfolioBacktestCenter portfolioId={1} autoTradeEnabled />);
    // T5.1 自动预检 500 -> T1 fail-closed 注入 PRECHECK_500 blocker -> T5.3 按钮 "已阻断..." 且 disabled
    await waitFor(() => {
      const errorCalls = mockToast.mock.calls.filter((cc) => cc[0] === "error");
      expect(errorCalls.length).toBeGreaterThanOrEqual(1);
    });
    const submitBtn = await screen.findByRole("button", { name: "portfolioTrading.backtest.flow.needsAttention" });
    expect(submitBtn).toBeDisabled();
    expect(mockApi.runPortfolioBacktest).not.toHaveBeenCalled();
  });
});
