// WP8.3：PortfolioPerformancePanel 组件测试
//
// 覆盖（至少 6 个用例）：
// - test_attribution_tabs_render：6 个归因 Tab 标签渲染
// - test_member_attribution_display：按成员归因表格展示成员/贡献/盈亏
// - test_sample_warning_display：样本不足时显示黄色 Alert，且隐藏 summary 稳定结论
// - test_benchmark_comparison：基准对比区块展示指标；无基准时显示提示
// - test_create_review_button：点击"创建复盘"展开表单，提交调用 createReview
// - test_review_history_display：复盘历史列表渲染
//
// 约束：
// - 不修改已稳定组件实现
// - mock api/client（api + requestJson），不调用真实后端
// - mock echarts-for-react，避免 canvas 副作用
// - i18n mock：t(key) 返回 key，便于按 key 断言
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockApi } = vi.hoisted(() => ({
  mockContext: {
    portfolioId: 1 as number | null,
  },
  mockApi: {
    getPortfolioPerformance: vi.fn(),
    getAttributionReport: vi.fn(),
    getReviews: vi.fn(),
    createReview: vi.fn(),
  },
}));

// Mock i18n：t 返回 key，template 替换 {var}
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
  stageLabel: (v: string | null | undefined) => v ?? "-",
  actionLabel: (v: string | null | undefined) => v ?? "-",
  assetTypeLabel: (v: string | null | undefined) => v ?? "unknown",
  regionShortLabel: (v: string | null | undefined) => v ?? "-",
  getLocale: () => "zh-CN",
  setLocale: () => {},
}));

// Mock antd：保留组件库，覆盖 message 静态方法
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: {
      ...actual.message,
      success: vi.fn(),
      error: vi.fn(),
      warning: vi.fn(),
      info: vi.fn(),
    },
  };
});

// Mock AppContext：仅提供 portfolioId
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client
vi.mock("../../api/client", () => ({
  requestJson: vi.fn(),
  api: mockApi,
}));

// Mock echarts-for-react：避免 canvas 副作用
vi.mock("echarts-for-react", () => ({
  default: () => <div data-testid="mock-chart" />,
}));

import PortfolioPerformancePanel from "../PortfolioPerformancePanel";

/** 构造一份空的绩效返回（snapshot_count=0 触发空状态，但不阻断归因面板渲染） */
const emptyPerformance = {
  portfolio_id: 1,
  initial_capital: 100000,
  snapshot_count: 0,
  date_range: { start: null, end: null },
  equity_curve: [],
  stats: {
    total_return: 0,
    total_return_pct: 0,
    max_drawdown: 0,
    max_drawdown_pct: 0,
    sharpe_ratio: 0,
    win_rate: 0,
    profit_factor: 0,
    trade_count: 0,
    avg_holding_days: 0,
  },
};

/** 构造一份完整的归因报告（默认样本充足、含基准） */
function makeAttribution(overrides: Partial<any> = {}): any {
  return {
    by_member: {
      items: [
        { label: "Member-A", contribution_pct: 0.6, pnl: 6000, trade_count: 8 },
        { label: "Member-B", contribution_pct: 0.4, pnl: 4000, trade_count: 4 },
      ],
      sample_warning: null,
      sample_size: 12,
      group_count: 2,
    },
    by_execution_mode: {
      items: [
        { label: "auto", contribution_pct: 0.5, pnl: 5000, trade_count: 6 },
        { label: "manual", contribution_pct: 0.5, pnl: 5000, trade_count: 6 },
      ],
      sample_warning: null,
    },
    by_source: { items: [], sample_warning: null },
    by_rule_signal: { items: [], sample_warning: null },
    backtest_vs_sim: {
      diff: { total_return_pct: 0.02, sharpe_ratio: 0.1, trade_count: 1 },
      explanation: "minor slippage",
    },
    cost_impact: {
      total_cost: 120,
      slippage_cost: 80,
      rejected_count: 3,
      risk_blocked_count: 1,
      impact_pct: 0.005,
    },
    summary: "stable annualized 20%",
    benchmark: {
      name: "CSI300",
      excess_return: 0.05,
      tracking_error: 0.08,
      information_ratio: 0.6,
    },
    ...overrides,
  };
}

describe("PortfolioPerformancePanel 绩效归因测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.portfolioId = 1;
    mockApi.getPortfolioPerformance.mockResolvedValue(emptyPerformance);
    mockApi.getAttributionReport.mockResolvedValue(makeAttribution());
    mockApi.getReviews.mockResolvedValue([]);
    mockApi.createReview.mockResolvedValue({ id: 10, portfolio_id: 1, note: "", created_at: "2026-07-22" });
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  // 1. 归因 Tab 渲染：6 个 Tab 标签均出现
  it("test_attribution_tabs_render：6 个归因 Tab 标签渲染", async () => {
    render(<PortfolioPerformancePanel />);
    await waitFor(() => {
      expect(screen.getByTestId("attribution-panel")).toBeInTheDocument();
    });
    // 6 个 Tab 标签（t 返回 key）
    expect(screen.getByText("portfolioAttribution.byMember")).toBeInTheDocument();
    expect(screen.getByText("portfolioAttribution.byExecutionMode")).toBeInTheDocument();
    expect(screen.getByText("portfolioAttribution.bySource")).toBeInTheDocument();
    expect(screen.getByText("portfolioAttribution.byRuleSignal")).toBeInTheDocument();
    expect(screen.getByText("portfolioAttribution.backtestVsSim")).toBeInTheDocument();
    expect(screen.getByText("portfolioAttribution.costImpact")).toBeInTheDocument();
  });

  // 2. 按成员归因展示：默认 Tab 展示成员表格
  it("test_member_attribution_display：按成员归因表格展示成员/贡献/盈亏", async () => {
    render(<PortfolioPerformancePanel />);
    await waitFor(() => {
      expect(screen.getByText("Member-A")).toBeInTheDocument();
    });
    expect(screen.getByText("Member-B")).toBeInTheDocument();
    // 样本数提示（template 替换后含 12 笔交易 / 2 个分组）
    expect(screen.getByText(/portfolioAttribution.basedOn/)).toBeInTheDocument();
    // 列标题
    expect(screen.getByText("portfolioAttribution.contribution")).toBeInTheDocument();
    expect(screen.getByText("portfolioAttribution.pnl")).toBeInTheDocument();
    expect(screen.getByText("portfolioAttribution.tradeCount")).toBeInTheDocument();
  });

  // 3. 样本不足提示：sample_warning 不为 null 时显示 Alert，且隐藏 summary 稳定结论
  it("test_sample_warning_display：样本不足时显示告警并隐藏稳定结论", async () => {
    const attr = makeAttribution({
      by_member: {
        items: [{ label: "Member-A", contribution_pct: 1, pnl: 1000, trade_count: 2 }],
        sample_warning: "样本不足，结论仅供参考",
        sample_size: 2,
        group_count: 1,
      },
    });
    mockApi.getAttributionReport.mockResolvedValue(attr);
    render(<PortfolioPerformancePanel />);
    await waitFor(() => {
      expect(screen.getByTestId("sample-warning-alert")).toBeInTheDocument();
    });
    // sampleWarning 文案出现
    expect(screen.getByText("portfolioAttribution.sampleWarning")).toBeInTheDocument();
    // summary 含"stable annualized 20%"稳定结论，样本不足时应被隐藏
    expect(screen.queryByText("stable annualized 20%")).not.toBeInTheDocument();
  });

  // 4. 基准对比：有基准时展示指标；无基准时显示"未配置基准"
  it("test_benchmark_comparison：基准对比区块展示指标，无基准时显示提示", async () => {
    // 有基准
    render(<PortfolioPerformancePanel />);
    await waitFor(() => {
      expect(screen.getByTestId("benchmark-block")).toBeInTheDocument();
    });
    expect(screen.getByText("CSI300")).toBeInTheDocument();
    expect(screen.getByText("portfolioAttribution.excessReturn")).toBeInTheDocument();
    expect(screen.getByText("portfolioAttribution.trackingError")).toBeInTheDocument();
    expect(screen.getByText("portfolioAttribution.informationRatio")).toBeInTheDocument();
  });

  it("test_benchmark_no_benchmark：无基准时显示未配置提示", async () => {
    mockApi.getAttributionReport.mockResolvedValue(makeAttribution({ benchmark: null }));
    render(<PortfolioPerformancePanel />);
    await waitFor(() => {
      expect(screen.getByText("portfolioAttribution.noBenchmark")).toBeInTheDocument();
    });
    expect(screen.queryByText("CSI300")).not.toBeInTheDocument();
  });

  // 5. 创建复盘按钮：点击展开表单，填写备注并提交调用 createReview（自动附归因快照）
  it("test_create_review_button：点击创建复盘展开表单并提交", async () => {
    render(<PortfolioPerformancePanel />);
    await waitFor(() => {
      expect(screen.getByTestId("create-review-button")).toBeInTheDocument();
    });
    // 点击展开表单
    fireEvent.click(screen.getByTestId("create-review-button"));
    expect(screen.getByTestId("review-form")).toBeInTheDocument();
    expect(screen.getByTestId("review-note-input")).toBeInTheDocument();
    // 填写备注
    fireEvent.change(screen.getByTestId("review-note-input"), { target: { value: "本次归因复盘" } });
    // 提交
    fireEvent.click(screen.getByTestId("review-submit-button"));
    await waitFor(() => {
      expect(mockApi.createReview).toHaveBeenCalledTimes(1);
    });
    // 提交 payload 含 note 与 attribution_snapshot
    const callArgs = mockApi.createReview.mock.calls[0];
    expect(callArgs[0]).toBe(1); // portfolioId
    expect(callArgs[1].note).toBe("本次归因复盘");
    expect(typeof callArgs[1].attribution_snapshot).toBe("string");
  });

  // 6. 复盘历史：历史复盘记录列表渲染
  it("test_review_history_display：复盘历史列表渲染", async () => {
    mockApi.getReviews.mockResolvedValue([
      { id: 1, portfolio_id: 1, note: "第一次复盘", attribution_snapshot: "{}", created_at: "2026-07-20" },
      { id: 2, portfolio_id: 1, note: "第二次复盘", attribution_snapshot: null, created_at: "2026-07-21" },
    ]);
    render(<PortfolioPerformancePanel />);
    await waitFor(() => {
      expect(screen.getByTestId("review-list")).toBeInTheDocument();
    });
    expect(screen.getByText("第一次复盘")).toBeInTheDocument();
    expect(screen.getByText("第二次复盘")).toBeInTheDocument();
    expect(screen.getByText("2026-07-20")).toBeInTheDocument();
  });

  // 7. 归因加载失败：显示错误信息
  it("test_attribution_load_error：归因加载失败显示错误", async () => {
    mockApi.getAttributionReport.mockRejectedValue(new Error("network error"));
    render(<PortfolioPerformancePanel />);
    await waitFor(() => {
      expect(screen.getByText(/portfolioAttribution.loadFailed/)).toBeInTheDocument();
    });
    expect(screen.getByText(/network error/)).toBeInTheDocument();
  });
});
