// WP7.4：PortfolioBacktestPanel 组件测试
//
// 覆盖（至少 6 个用例）：
// - test_source_label_legacy_scan：detail.source_type="legacy_scan" → "旧临时标的集" Tag
// - test_source_label_member：detail.source_type="member" → "历史成员集" Tag
// - test_excluded_members_display：excluded_members_json 渲染排除成员表
// - test_only_auto_checkbox：勾选 only_auto 后点击运行，请求体含 only_auto=true
// - test_snapshot_collapsible：快照分区可折叠展开
// - test_compare_engines_button：点击对比按钮调用 comparePortfolioBacktestEngines API
//
// 约束：
// - 不修改已稳定组件实现
// - mock api/client（api + requestJson），不调用真实后端
// - mock BacktestResult 子组件，避免 ECharts/canvas 副作用
// - i18n mock：t(key) 返回 key，便于按 key 断言
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockApi, mockRequestJson } = vi.hoisted(() => ({
  mockContext: {
    portfolioId: 1 as number | null,
    portfolios: [
      {
        id: 1,
        account_type: "simulated",
        auto_trade_enabled: 1,
        auto_trade_last_run_at: null,
      },
    ],
    showToast: vi.fn(),
    loadWorkbench: vi.fn().mockResolvedValue(undefined),
  },
  mockApi: {
    runPortfolioBacktest: vi.fn(),
    getBacktestRun: vi.fn(),
    getPortfolioBacktestSourceStatus: vi.fn(),
    comparePortfolioBacktestEngines: vi.fn(),
  },
  mockRequestJson: vi.fn(),
}));

// Mock i18n：t 返回 key，template 替换 {var}
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
  stageLabel: (v: string | null | undefined) => v ?? "-",
  actionLabel: (v: string | null | undefined) => v ?? "-",
  enumLabel: (_prefix: string, v: string | null | undefined) => v ?? "-",
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

// Mock AppContext
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client
vi.mock("../../api/client", () => ({
  requestJson: mockRequestJson,
  api: mockApi,
}));

// Mock BacktestResult 子组件：仅渲染占位，避免 ECharts 副作用
vi.mock("../BacktestResult", () => ({
  default: ({ result }: { result: { id: number } }) => (
    <div data-testid="backtest-result-stub">BacktestResult #{result.id}</div>
  ),
}));

// Mock ExplainButton：避免渲染依赖 useAIAssistant 的真实组件
vi.mock("../ai/ExplainButton", () => ({
  __esModule: true,
  default: () => <div data-testid="explain-button-mock" />,
}));

import PortfolioBacktestPanel from "../PortfolioBacktestPanel";

/** 构造 BacktestRun 详情（含 WP7.2 快照字段） */
function makeBacktestRun(overrides: Partial<any> = {}): any {
  return {
    id: 100,
    portfolio_id: 1,
    run_name: "test run",
    symbols_json: "[]",
    rule_config_json: "{}",
    cost_config_json: null,
    start_date: "2025-01-01",
    end_date: "2025-12-31",
    initial_capital: 100000,
    total_return: 1000,
    total_return_pct: 0.01,
    max_drawdown: -500,
    max_drawdown_pct: -0.005,
    sharpe_ratio: 1.2,
    trade_count: 5,
    status: "completed",
    error_message: null,
    created_at: "2026-07-21T00:00:00Z",
    started_at: null,
    finished_at: null,
    // WP7.2 快照字段
    member_snapshot_json: null,
    symbol_ids_json: null,
    excluded_members_json: null,
    portfolio_rule_version_id: null,
    score_mode: null,
    data_cutoff_at: null,
    engine_name: null,
    engine_version: null,
    source_type: null,
    ...overrides,
  };
}

/** 构造 PortfolioBacktestResult summary */
function makeSummary(overrides: Partial<any> = {}): any {
  return {
    run_id: 100,
    portfolio_id: 1,
    symbol_ids: [1, 2, 3],
    symbol_count: 3,
    start_date: "2025-01-01",
    end_date: "2025-12-31",
    initial_capital: 100000,
    status: "completed",
    run_name: "test run",
    symbol_source: "legacy",
    source_type: "legacy_scan",
    excluded_member_count: 0,
    ...overrides,
  };
}

describe("PortfolioBacktestPanel WP7.4 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.portfolioId = 1;
    // 默认 mock：来源状态为 legacy 关闭
    mockApi.getPortfolioBacktestSourceStatus.mockResolvedValue({
      enabled: false,
      env_flag: "PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED",
      source_label: "legacy",
    });
    mockApi.runPortfolioBacktest.mockResolvedValue(makeSummary());
    mockApi.getBacktestRun.mockResolvedValue(makeBacktestRun());
    mockApi.comparePortfolioBacktestEngines.mockResolvedValue({
      old: {
        run_id: 1,
        symbol_ids: [1, 2],
        source_type: "legacy_scan",
        trades: [],
        metrics: {
          total_return: 100,
          total_return_pct: 0.01,
          max_drawdown: -50,
          max_drawdown_pct: -0.005,
          sharpe_ratio: 1.0,
          trade_count: 3,
        },
      },
      new: {
        run_id: 2,
        symbol_ids: [1, 2, 3],
        source_type: "member",
        trades: [],
        metrics: {
          total_return: 200,
          total_return_pct: 0.02,
          max_drawdown: -30,
          max_drawdown_pct: -0.003,
          sharpe_ratio: 1.5,
          trade_count: 4,
        },
      },
      diff: {
        symbol_ids_added: [3],
        symbol_ids_removed: [],
        metrics_diff: {
          total_return: { old: 100, new: 200, delta: 100 },
          max_drawdown: { old: -50, new: -30, delta: 20 },
          sharpe_ratio: { old: 1.0, new: 1.5, delta: 0.5 },
        },
        explanation: "新引擎比旧引擎多了 1 个标的",
      },
    });
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  // 1. legacy_scan 来源 Tag
  it("test_source_label_legacy_scan: 显示'旧临时标的集' Tag", async () => {
    // 直接预置 detail 状态：runPortfolioBacktest 返回 summary，getBacktestRun 返回 legacy_scan 详情
    mockApi.runPortfolioBacktest.mockResolvedValue(
      makeSummary({ source_type: "legacy_scan", symbol_source: "legacy" }),
    );
    mockApi.getBacktestRun.mockResolvedValue(
      makeBacktestRun({ source_type: "legacy_scan" }),
    );
    render(<PortfolioBacktestPanel />);

    // 等待来源状态加载完成
    await waitFor(() => {
      expect(mockApi.getPortfolioBacktestSourceStatus).toHaveBeenCalledWith(1);
    });

    // 点击"运行组合回测"按钮
    const runButton = screen.getByText("portBtRun").closest("button")!;
    await act(async () => {
      await userEvent.setup().click(runButton);
    });

    // 等待 summary 渲染并出现 source-label-tag
    await waitFor(() => {
      const tag = screen.getByTestId("source-label-tag");
      expect(tag).toBeInTheDocument();
      // t("portfolioBacktest.sourceLabel") + ": " + t("portfolioBacktest.legacySource")
      expect(tag.textContent).toContain("portfolioBacktest.legacySource");
    });
  });

  // 2. member 来源 Tag
  it("test_source_label_member: 显示'历史成员集' Tag", async () => {
    mockApi.runPortfolioBacktest.mockResolvedValue(
      makeSummary({ source_type: "member", symbol_source: "members" }),
    );
    mockApi.getBacktestRun.mockResolvedValue(
      makeBacktestRun({ source_type: "member" }),
    );
    render(<PortfolioBacktestPanel />);

    await waitFor(() => {
      expect(mockApi.getPortfolioBacktestSourceStatus).toHaveBeenCalledWith(1);
    });

    const runButton = screen.getByText("portBtRun").closest("button")!;
    await act(async () => {
      await userEvent.setup().click(runButton);
    });

    await waitFor(() => {
      const tag = screen.getByTestId("source-label-tag");
      expect(tag).toBeInTheDocument();
      expect(tag.textContent).toContain("portfolioBacktest.memberSource");
    });
  });

  // 3. 排除成员表渲染
  it("test_excluded_members_display: 渲染排除成员列表", async () => {
    const excludedJson = JSON.stringify([
      { member_id: 10, symbol_id: 100, reason: "execution_mode is 'manual', expected 'auto'" },
      { member_id: 11, symbol_id: 200, reason: "execution_mode is 'confirm', expected 'auto'" },
    ]);
    mockApi.getBacktestRun.mockResolvedValue(
      makeBacktestRun({ excluded_members_json: excludedJson }),
    );
    render(<PortfolioBacktestPanel />);

    const runButton = screen.getByText("portBtRun").closest("button")!;
    await act(async () => {
      await userEvent.setup().click(runButton);
    });

    // 等待排除成员分区出现
    await waitFor(() => {
      expect(screen.getByTestId("excluded-members-section")).toBeInTheDocument();
    });

    // 两条排除记录都应渲染（member_id 列）
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getByText("11")).toBeInTheDocument();
    // manual/confirm 警告应显示
    expect(screen.getByText("portfolioBacktest.manualMembersWarning")).toBeInTheDocument();
  });

  // 4. only_auto 复选框触发请求
  it("test_only_auto_checkbox: 勾选 only_auto 后运行请求含 only_auto=true", async () => {
    const user = userEvent.setup();
    render(<PortfolioBacktestPanel />);

    await waitFor(() => {
      expect(mockApi.getPortfolioBacktestSourceStatus).toHaveBeenCalledWith(1);
    });

    // 勾选 only_auto 复选框：antd Checkbox 渲染为 <label class="ant-checkbox-wrapper">
    // 点击 label wrapper 触发 onChange
    const checkboxWrapper = screen.getByTestId("only-auto-checkbox");
    await act(async () => {
      await user.click(checkboxWrapper);
    });

    // 点击运行
    const runButton = screen.getByText("portBtRun").closest("button")!;
    await act(async () => {
      await user.click(runButton);
    });

    await waitFor(() => {
      expect(mockApi.runPortfolioBacktest).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ only_auto: true }),
      );
    });
  });

  // 5. 快照分区可折叠展开
  it("test_snapshot_collapsible: 快照分区默认展开并显示快照内容", async () => {
    const memberSnapshotJson = JSON.stringify([
      {
        member_id: 20,
        symbol_id: 300,
        effective_from: "2025-01-01",
        effective_to: null,
        execution_mode: "auto",
        entry_rule_version_id: 5,
      },
    ]);
    const symbolIdsJson = JSON.stringify([300, 400]);
    mockApi.getBacktestRun.mockResolvedValue(
      makeBacktestRun({
        member_snapshot_json: memberSnapshotJson,
        symbol_ids_json: symbolIdsJson,
        score_mode: "auto_trade_signal",
        portfolio_rule_version_id: 7,
        engine_name: "event_driven",
        engine_version: "1.0.0",
        data_cutoff_at: "2025-12-30T00:00:00",
      }),
    );
    render(<PortfolioBacktestPanel />);

    const runButton = screen.getByText("portBtRun").closest("button")!;
    await act(async () => {
      await userEvent.setup().click(runButton);
    });

    // 快照分区应存在（Collapse 默认展开，便于审计历史可复现性）
    await waitFor(() => {
      expect(screen.getByTestId("snapshot-section")).toBeInTheDocument();
    });

    // 默认展开 + forceRender：score_mode 与 symbolList 内容应直接可见
    // 使用 data-testid 定位，避免 <strong>text:</strong> 后跟冒号导致 getByText 精确匹配失败
    await waitFor(() => {
      const scoreModeEl = screen.getByTestId("snapshot-score-mode");
      expect(scoreModeEl).toBeInTheDocument();
      // 内容应包含 score_mode 值
      expect(scoreModeEl.textContent).toContain("auto_trade_signal");
    });
    const symbolListEl = screen.getByTestId("snapshot-symbol-list");
    expect(symbolListEl).toBeInTheDocument();
    // 标的列表应包含 300 和 400
    expect(symbolListEl.textContent).toContain("300");
    expect(symbolListEl.textContent).toContain("400");

    // 规则版本也应渲染
    const ruleVersionEl = screen.getByTestId("snapshot-rule-version");
    expect(ruleVersionEl).toBeInTheDocument();
    expect(ruleVersionEl.textContent).toContain("7");

    // 折叠面板 header 存在，点击可折叠（验证 Collapse 结构）
    const collapseHeader = screen.getByTestId("snapshot-section").querySelector(".ant-collapse-header");
    expect(collapseHeader).toBeTruthy();
    // 点击 header 折叠（不验证隐藏后的 DOM，仅验证点击不报错）
    await act(async () => {
      if (collapseHeader) fireEvent.click(collapseHeader);
    });
  });

  // 6. 对比按钮调用 API
  it("test_compare_engines_button: 点击对比按钮调用 comparePortfolioBacktestEngines", async () => {
    const user = userEvent.setup();
    render(<PortfolioBacktestPanel />);

    await waitFor(() => {
      expect(mockApi.getPortfolioBacktestSourceStatus).toHaveBeenCalledWith(1);
    });

    // 点击对比按钮
    const compareButton = screen.getByTestId("compare-engines-button").closest("button")!;
    expect(compareButton).not.toBeNull();
    await act(async () => {
      await user.click(compareButton);
    });

    await waitFor(() => {
      expect(mockApi.comparePortfolioBacktestEngines).toHaveBeenCalledWith(
        1,
        expect.objectContaining({
          start_date: expect.any(String),
          end_date: expect.any(String),
        }),
      );
    });

    // 对比结果应渲染
    await waitFor(() => {
      expect(screen.getByTestId("compare-result-section")).toBeInTheDocument();
    });
    // 差异说明文本应出现
    expect(screen.getByText("portfolioBacktest.explanation")).toBeInTheDocument();
  });
});
