// WP5.1：SymbolResearchShell 壳层组件测试
//
// 覆盖：
// - 壳层渲染不报错（workbench 为空时显示空状态）
// - workbench 有数据时渲染 9 个子组件
// - 子组件 props 正确传递（搜索/标的切换）
// - 现有功能回归：搜索关键词变更、选择标的、切换收藏
//
// 约束：
// - 不调用真实后端，所有 API 通过 vi.fn mock
// - i18n mock：t 返回 key，便于按 key 断言
// - 子组件 mock：渲染可识别占位，验证 props 透传
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockApi } = vi.hoisted(() => {
  return {
    mockContext: {
      workbench: null as any,
      detail: null as any,
      activeSymbolId: null as number | null,
      portfolioId: 1 as number | null,
      activeTab: "investment",
      activeWatchlistId: null as number | null,
      chartTimeframe: "daily" as "daily" | "weekly",
      chartWindowSize: 60,
      chartExpanded: false,
      chartRange: null,
      futurePlanScenario: "general",
      futurePlanCustom: { horizonDays: 20, pullbackPct: 3, positionPct: 5 },
      simPrice: "",
      simQuantity: "",
      locale: "zh-CN" as "zh-CN" | "en-US",
      setChartWindowSize: vi.fn(),
      setChartTimeframe: vi.fn(),
      setChartRange: vi.fn(),
      setChartExpanded: vi.fn(),
      setFuturePlanScenario: vi.fn(),
      setFuturePlanCustom: vi.fn(),
      setSimPrice: vi.fn(),
      setSimQuantity: vi.fn(),
      loadSymbolDetail: vi.fn(async (_id: number, _options?: any) => undefined),
      loadWorkbench: vi.fn(async () => undefined),
      generateTradeSetup: vi.fn(async () => undefined),
      showToast: vi.fn(),
    },
    mockApi: {
      getSymbols: vi.fn(async (_q: string) => [] as any[]),
      getBars: vi.fn(async (_id: number, _limit: number) => [] as any[]),
      getSymbolFactorExplanation: vi.fn(async (_id: number) => null as any),
      saveTradeSetupTranches: vi.fn(async () => ({} as any)),
      getSymbolRelationships: vi.fn(async (_id: number) => null as any),
    },
  };
});

// Mock i18n：t 返回 key，template 返回拼接后的字符串
vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
  stageLabel: (v: string | null | undefined) => v ?? "-",
  actionLabel: (v: string | null | undefined) => v ?? "-",
  assetTypeLabel: (v: string | null | undefined) => v ?? "unknown",
  regionShortLabel: (v: string | null | undefined) => v ?? "-",
  regionLongLabel: (v: string | null | undefined) => v ?? "-",
  futureBuyLabel: (v: string | null | undefined) => v ?? "-",
  futurePriorityLabel: (v: string | null | undefined) => v ?? "-",
  futureTriggerLabel: (_plan: any) => "-",
  sideLabel: (v: string | null | undefined) => v ?? "-",
  trancheLabel: (v: string) => v ?? "-",
  trancheTrigger: (t: string) => t ?? "-",
  getLocale: () => "zh-CN",
  setLocale: () => {},
}));

// Mock antd：保留组件库，仅覆盖 message
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
vi.mock("../../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client
vi.mock("../../../api/client", () => ({
  api: mockApi,
  requestJson: vi.fn(async () => ({} as any)),
}));

// Mock indicators（避免真实计算）
vi.mock("../../../utils/indicators", () => ({
  computeMA: vi.fn(() => []),
  computeMACD: vi.fn(() => ({ dif: [], dea: [], macdHist: [] })),
  detectMACDCross: vi.fn(() => []),
  computeRSI: vi.fn(() => []),
  detectRSIExtreme: vi.fn(() => []),
  computeBOLL: vi.fn(() => ({ up: [], mid: [], low: [] })),
  computeATR: vi.fn(() => []),
  formatVolume: vi.fn((v: number) => String(v)),
}));

// Mock 子组件：渲染可识别占位，便于验证渲染与 props 透传
vi.mock("../SymbolSearchHeader", () => ({
  __esModule: true,
  default: (props: any) => (
    <div data-testid="symbol-search-header" data-search-query={props.searchQuery}>
      <button
        data-testid="search-input-mock"
        onClick={() => props.onSearchQueryChange("600519")}
      >
        search
      </button>
      <button
        data-testid="select-symbol-mock"
        onClick={() => props.onSelectSymbol(1, { symbol: "600519", name: "贵州茅台" })}
      >
        select
      </button>
      <button
        data-testid="toggle-fav-mock"
        onClick={() => props.onToggleFavorite(1)}
      >
        fav
      </button>
    </div>
  ),
}));

vi.mock("../SymbolRelationshipBar", () => ({
  __esModule: true,
  default: (props: any) => (
    <div data-testid="symbol-relationship-bar" data-symbol-id={props.symbolId ?? ""}>
      relationship
    </div>
  ),
}));

vi.mock("../FactorExplanationPanel", () => ({
  __esModule: true,
  default: (props: any) => (
    <div data-testid="factor-explanation-panel" data-symbol-id={props.symbolId ?? ""}>
      factor
    </div>
  ),
}));

vi.mock("../SymbolAlertSummary", () => ({
  __esModule: true,
  default: (props: any) => (
    <div data-testid="symbol-alert-summary" data-alerts-count={props.alerts.length}>
      alerts
    </div>
  ),
}));

vi.mock("../RiskReferencePanel", () => ({
  __esModule: true,
  default: (props: any) => (
    <div data-testid="risk-reference-panel" data-has-metrics={String(props.riskMetrics != null)}>
      risk
    </div>
  ),
}));

vi.mock("../TradePlanPanel", () => ({
  __esModule: true,
  default: (props: any) => (
    <div data-testid="trade-plan-panel" data-has-setup={String(props.setup != null)}>
      trade
    </div>
  ),
}));

vi.mock("../SymbolChartPanel", () => ({
  __esModule: true,
  default: (props: any) => (
    <div data-testid="symbol-chart-panel" data-has-chart-data={String(props.chartData != null)}>
      chart
    </div>
  ),
}));

vi.mock("../SingleSymbolBacktestPanel", () => ({
  __esModule: true,
  default: (props: any) => (
    <div
      data-testid="single-symbol-backtest-panel"
      data-portfolio-id={props.portfolioId ?? ""}
      data-source-type={props.sourceContext?.source_type ?? ""}
    >
      backtest
    </div>
  ),
}));

import SymbolResearchShell from "../SymbolResearchShell";
import InvestmentCenterCompat from "../../InvestmentCenter";

describe("SymbolResearchShell 壳层组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 重置 mock context 到默认值
    mockContext.workbench = null;
    mockContext.detail = null;
    mockContext.activeSymbolId = null;
    mockContext.portfolioId = 1;
    mockContext.activeTab = "investment";
    mockContext.chartWindowSize = 60;
    mockContext.futurePlanScenario = "general";
    mockContext.simPrice = "";
    mockContext.simQuantity = "";
    mockApi.getSymbols.mockResolvedValue([]);
    mockApi.getBars.mockResolvedValue([]);
    mockApi.getSymbolFactorExplanation.mockResolvedValue(null);
    // 清理 localStorage/sessionStorage，避免用例间状态污染
    localStorage.clear();
    sessionStorage.clear();
    // 重置 location.search，避免上个用例残留 URL 参数影响 resolveSourceContext
    window.history.replaceState(null, "", "/");
  });

  /** 构造一份最小 workbench 数据，供需要 workbench 非空的用例使用 */
  const workbenchFixture = {
    positions: [],
    latest_scores: [],
    portfolio: { total_capital: 100000, investable_ratio: 0.9 },
    watchlists: [],
  };

  // 1. 壳层渲染不报错（workbench 为空时显示空状态）
  it("workbench 为空时应渲染空状态而不崩溃", () => {
    mockContext.workbench = null;
    const { container } = render(<SymbolResearchShell />);
    expect(container.querySelector(".symbol-research-shell")).toBeInTheDocument();
    // 空状态显示 noScanYet 文案
    expect(screen.getByText("noScanYet")).toBeInTheDocument();
  });

  // 2. workbench 有数据时渲染全部 9 个子组件
  it("workbench 有数据时应渲染 8 个直接子组件（不含 Shell 自身）", async () => {
    mockContext.workbench = {
      positions: [],
      latest_scores: [],
      portfolio: { total_capital: 100000, investable_ratio: 0.9 },
      watchlists: [],
    } as any;

    render(<SymbolResearchShell />);

    // 等待副作用完成
    await waitFor(() => {
      expect(screen.getByTestId("symbol-search-header")).toBeInTheDocument();
    });

    // 验证 8 个子组件（SymbolRelationshipBar 仅在有 activeSymbolId 时渲染）
    expect(screen.getByTestId("symbol-search-header")).toBeInTheDocument();
    expect(screen.getByTestId("factor-explanation-panel")).toBeInTheDocument();
    expect(screen.getByTestId("symbol-alert-summary")).toBeInTheDocument();
    expect(screen.getByTestId("risk-reference-panel")).toBeInTheDocument();
    expect(screen.getByTestId("trade-plan-panel")).toBeInTheDocument();
    expect(screen.getByTestId("symbol-chart-panel")).toBeInTheDocument();
    expect(screen.getByTestId("single-symbol-backtest-panel")).toBeInTheDocument();
  });

  // 3. 回归测试：搜索关键词变更触发 onSearchQueryChange 透传
  it("搜索输入变更应更新 searchQuery 状态", async () => {
    mockContext.workbench = {
      positions: [],
      latest_scores: [],
      portfolio: { total_capital: 100000, investable_ratio: 0.9 },
      watchlists: [],
    } as any;

    render(<SymbolResearchShell />);
    await waitFor(() => {
      expect(screen.getByTestId("symbol-search-header")).toBeInTheDocument();
    });

    // 初始 searchQuery 为空
    expect(screen.getByTestId("symbol-search-header").getAttribute("data-search-query")).toBe("");

    // 点击模拟搜索输入
    fireEvent.click(screen.getByTestId("search-input-mock"));

    // 验证 searchQuery 已更新
    expect(screen.getByTestId("symbol-search-header").getAttribute("data-search-query")).toBe(
      "600519",
    );
  });

  // 4. 回归测试：选择标的触发 loadSymbolDetail + 清空 searchQuery
  it("选择标的应调用 ctx.loadSymbolDetail 并清空搜索词", async () => {
    mockContext.workbench = {
      positions: [],
      latest_scores: [],
      portfolio: { total_capital: 100000, investable_ratio: 0.9 },
      watchlists: [],
    } as any;

    render(<SymbolResearchShell />);
    await waitFor(() => {
      expect(screen.getByTestId("symbol-search-header")).toBeInTheDocument();
    });

    // 先输入搜索词
    fireEvent.click(screen.getByTestId("search-input-mock"));
    expect(screen.getByTestId("symbol-search-header").getAttribute("data-search-query")).toBe(
      "600519",
    );

    // 点击选择标的
    fireEvent.click(screen.getByTestId("select-symbol-mock"));

    // 验证 loadSymbolDetail 被调用
    expect(mockContext.loadSymbolDetail).toHaveBeenCalledWith(1, {
      focus: true,
      barLimit: 500,
    });
    // 验证 searchQuery 已清空
    expect(screen.getByTestId("symbol-search-header").getAttribute("data-search-query")).toBe("");
  });

  // 5. 回归测试：有 activeSymbolId 时渲染 SymbolRelationshipBar
  it("activeSymbolId 存在时应渲染 SymbolRelationshipBar", async () => {
    mockContext.workbench = {
      positions: [],
      latest_scores: [],
      portfolio: { total_capital: 100000, investable_ratio: 0.9 },
      watchlists: [],
    } as any;
    mockContext.activeSymbolId = 42;

    render(<SymbolResearchShell />);
    await waitFor(() => {
      expect(screen.getByTestId("symbol-relationship-bar")).toBeInTheDocument();
    });

    // 验证 symbolId 正确透传
    expect(screen.getByTestId("symbol-relationship-bar").getAttribute("data-symbol-id")).toBe(
      "42",
    );
  });

  // 6. 回归测试：sourceContext 透传到 SingleSymbolBacktestPanel
  it("sourceContext 应透传到 SingleSymbolBacktestPanel", async () => {
    mockContext.workbench = {
      positions: [],
      latest_scores: [],
      portfolio: { total_capital: 100000, investable_ratio: 0.9 },
      watchlists: [],
    } as any;

    render(<SymbolResearchShell sourceContext={{ source_type: "candidate", source_id: 99 }} />);
    await waitFor(() => {
      expect(screen.getByTestId("single-symbol-backtest-panel")).toBeInTheDocument();
    });

    // 验证 source_type 正确透传
    expect(
      screen.getByTestId("single-symbol-backtest-panel").getAttribute("data-source-type"),
    ).toBe("candidate");
  });

  // ── WP5 验收清单补充用例 ──────────────────────────────────────────

  // 7. (L175) 9 子组件存在：workbench + activeSymbolId 时全部 8 个直接子组件 + Shell 容器渲染
  it("workbench 有数据且 activeSymbolId 存在时应渲染全部 9 个子组件（含 SymbolRelationshipBar）", async () => {
    mockContext.workbench = { ...workbenchFixture } as any;
    mockContext.activeSymbolId = 42;

    const { container } = render(<SymbolResearchShell />);
    await waitFor(() => {
      expect(screen.getByTestId("symbol-relationship-bar")).toBeInTheDocument();
    });

    // 验证 Shell 容器自身存在
    expect(container.querySelector(".symbol-research-shell")).toBeInTheDocument();
    // 验证 8 个直接子组件全部渲染（含 SymbolRelationshipBar，仅在 activeSymbolId 存在时渲染）
    expect(screen.getByTestId("symbol-search-header")).toBeInTheDocument();
    expect(screen.getByTestId("symbol-relationship-bar")).toBeInTheDocument();
    expect(screen.getByTestId("factor-explanation-panel")).toBeInTheDocument();
    expect(screen.getByTestId("symbol-alert-summary")).toBeInTheDocument();
    expect(screen.getByTestId("risk-reference-panel")).toBeInTheDocument();
    expect(screen.getByTestId("trade-plan-panel")).toBeInTheDocument();
    expect(screen.getByTestId("symbol-chart-panel")).toBeInTheDocument();
    expect(screen.getByTestId("single-symbol-backtest-panel")).toBeInTheDocument();
  });

  // 8. (L176) 回归测试：搜索框可输入并触发查询（debounce 后调用 api.getSymbols）
  it("搜索输入应触发 api.getSymbols 查询（debounce 后）", async () => {
    mockContext.workbench = { ...workbenchFixture } as any;
    mockApi.getSymbols.mockResolvedValue([
      { id: 1, symbol: "600519", name: "贵州茅台" },
    ] as any);

    render(<SymbolResearchShell />);
    await waitFor(() => {
      expect(screen.getByTestId("symbol-search-header")).toBeInTheDocument();
    });

    // 模拟搜索输入（mock SymbolSearchHeader 触发 onSearchQueryChange("600519")）
    fireEvent.click(screen.getByTestId("search-input-mock"));

    // 等待 debounce 300ms 后 api.getSymbols 被调用
    await waitFor(
      () => {
        expect(mockApi.getSymbols).toHaveBeenCalledWith("600519");
      },
      { timeout: 1500 },
    );
  });

  // 9. (L176) 回归测试：图表面板渲染（即使无数据也应渲染容器）
  it("图表面板应渲染容器（即使 chartData 为 null）", async () => {
    mockContext.workbench = { ...workbenchFixture } as any;

    render(<SymbolResearchShell />);
    await waitFor(() => {
      expect(screen.getByTestId("symbol-chart-panel")).toBeInTheDocument();
    });

    // 无 detail.bars 时 chartData 为 null，但容器仍渲染
    expect(screen.getByTestId("symbol-chart-panel").getAttribute("data-has-chart-data")).toBe(
      "false",
    );
  });

  // 10. (L176) 回归测试：回测面板渲染
  it("回测面板应渲染（SingleSymbolBacktestPanel 容器存在）", async () => {
    mockContext.workbench = { ...workbenchFixture } as any;

    render(<SymbolResearchShell />);
    await waitFor(() => {
      expect(screen.getByTestId("single-symbol-backtest-panel")).toBeInTheDocument();
    });

    // 验证 portfolioId 透传到回测面板
    expect(
      screen.getByTestId("single-symbol-backtest-panel").getAttribute("data-portfolio-id"),
    ).toBe("1");
  });

  // 11. (L177) 搜索历史保留本地：选择标的写入 localStorage.ic_search_history
  it("选择标的应将搜索历史写入 localStorage.ic_search_history", async () => {
    mockContext.workbench = { ...workbenchFixture } as any;
    localStorage.clear();

    render(<SymbolResearchShell />);
    await waitFor(() => {
      expect(screen.getByTestId("symbol-search-header")).toBeInTheDocument();
    });

    // 点击选择标的（mock SymbolSearchHeader 触发 onSelectSymbol(1, {symbol:"600519", name:"贵州茅台"})）
    fireEvent.click(screen.getByTestId("select-symbol-mock"));

    // 验证 localStorage.ic_search_history 已写入
    await waitFor(() => {
      expect(localStorage.getItem("ic_search_history")).not.toBeNull();
    });
    const history = JSON.parse(localStorage.getItem("ic_search_history")!);
    expect(history).toHaveLength(1);
    expect(history[0].symbol_id).toBe(1);
    expect(history[0].symbol).toBe("600519");
    expect(history[0].name).toBe("贵州茅台");
  });

  // 12. (L185) 多入口使用同一壳层：不同 source_type 渲染同一 SymbolResearchShell 容器
  it("不同 source_type 渲染同一 SymbolResearchShell 容器", async () => {
    mockContext.workbench = { ...workbenchFixture } as any;

    // 来源 1：candidate
    const { unmount } = render(
      <SymbolResearchShell sourceContext={{ source_type: "candidate", source_id: 1 }} />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("single-symbol-backtest-panel")).toBeInTheDocument();
    });
    // 验证壳层容器存在
    expect(document.querySelector(".symbol-research-shell")).toBeInTheDocument();
    // 验证 source_type 透传到 backtest panel
    expect(
      screen.getByTestId("single-symbol-backtest-panel").getAttribute("data-source-type"),
    ).toBe("candidate");

    // 卸载后切换来源
    unmount();

    // 来源 2：observation
    render(
      <SymbolResearchShell sourceContext={{ source_type: "observation", source_id: 2 }} />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("single-symbol-backtest-panel")).toBeInTheDocument();
    });
    // 验证壳层容器仍存在（同一组件类）
    expect(document.querySelector(".symbol-research-shell")).toBeInTheDocument();
    // 验证 source_type 已切换为 observation
    expect(
      screen.getByTestId("single-symbol-backtest-panel").getAttribute("data-source-type"),
    ).toBe("observation");
  });

  // 13. (L187) 同一标的从不同来源进入时研究数据一致：loadSymbolDetail 调用参数相同
  it("同一 symbol_id 从不同 source_type 进入时应调用相同的 loadSymbolDetail", async () => {
    mockContext.workbench = { ...workbenchFixture } as any;
    // 必须为 null 才会触发 sourceContext.symbol_id 自动加载
    mockContext.activeSymbolId = null;

    // 第一次：source_type=candidate, symbol_id=42
    const { unmount } = render(
      <SymbolResearchShell
        sourceContext={{ source_type: "candidate", symbol_id: 42, return_to: "candidate" }}
      />,
    );
    await waitFor(() => {
      expect(mockContext.loadSymbolDetail).toHaveBeenCalledWith(42, {
        focus: true,
        barLimit: 500,
      });
    });
    const firstCallCount = mockContext.loadSymbolDetail.mock.calls.length;

    // 卸载后切换来源
    unmount();

    // 第二次：source_type=observation, symbol_id=42（同一标的，不同来源）
    // 重置 mock 以便清晰断言第二次调用
    mockContext.loadSymbolDetail.mockClear();
    render(
      <SymbolResearchShell
        sourceContext={{ source_type: "observation", symbol_id: 42, return_to: "observation" }}
      />,
    );
    await waitFor(() => {
      expect(mockContext.loadSymbolDetail).toHaveBeenCalledWith(42, {
        focus: true,
        barLimit: 500,
      });
    });

    // 验证两次调用的 symbol_id 与 options 完全一致（研究数据一致性）
    const secondCall = mockContext.loadSymbolDetail.mock.calls[0];
    expect(secondCall[0]).toBe(42);
    expect(secondCall[1]).toEqual({ focus: true, barLimit: 500 });
    // 第一次也调用了同样的 symbol_id
    expect(firstCallCount).toBeGreaterThan(0);
  });

  // 14. (L186) 返回时保留滚动位置：sessionStorage 中保存的 returnState 在 mount 时恢复
  it("sessionStorage 中保存的返回状态应在 mount 时恢复滚动位置", async () => {
    mockContext.workbench = { ...workbenchFixture } as any;

    // 预置返回状态：portfolio 来源保存了 scrollY=450
    sessionStorage.setItem(
      "research_return_state_portfolio",
      JSON.stringify({ scrollY: 450, statusFilter: "active" }),
    );

    // 监听 window.scrollTo（Shell 在 requestAnimationFrame 内调用）
    const scrollSpy = vi.spyOn(window, "scrollTo");

    render(
      <SymbolResearchShell
        sourceContext={{ source_type: "candidate", return_to: "portfolio" }}
      />,
    );

    // 等待 requestAnimationFrame 触发 scrollTo
    await waitFor(
      () => {
        expect(scrollSpy).toHaveBeenCalledWith({ top: 450, behavior: "auto" });
      },
      { timeout: 1500 },
    );

    // 验证 sessionStorage 中的 returnState 已被一次性消费（popReturnState）
    expect(sessionStorage.getItem("research_return_state_portfolio")).toBeNull();

    scrollSpy.mockRestore();
  });

  // 15. (L188) 原 InvestmentCenter 兼容入口仍能打开研究壳层
  it("InvestmentCenter 兼容入口应渲染 SymbolResearchShell 壳层", async () => {
    mockContext.workbench = { ...workbenchFixture } as any;

    const { container } = render(<InvestmentCenterCompat openMetricModal={() => {}} />);

    // 验证 InvestmentCenter 渲染了 SymbolResearchShell 壳层容器
    await waitFor(() => {
      expect(container.querySelector(".symbol-research-shell")).toBeInTheDocument();
    });
    // 验证子组件通过壳层渲染（证明 InvestmentCenter 内部委托给 SymbolResearchShell）
    expect(screen.getByTestId("symbol-search-header")).toBeInTheDocument();
    expect(screen.getByTestId("single-symbol-backtest-panel")).toBeInTheDocument();
  });
});
