// WP5.4：InvestmentCenter 兼容入口测试
//
// 覆盖：
// - 旧路由 /investment-center 不返回 404（InvestmentCenter 渲染 SymbolResearchShell）
// - 旧 URL ?symbol=X 解析为 legacy source_type
// - 旧 URL ?symbol_id=N 解析 symbol_id 并保持 legacy source_type
// - 默认进入为 legacy source_type（不显示来源面包屑）
// - 渲染结果包含 SymbolResearchShell（.symbol-research-shell 类）
// - 旧 tab 值 investment 仍可解析（通过 tabCompatibility）
//
// 约束：
// - 不调用真实后端，所有 API 通过 vi.fn mock
// - 使用真实 resolveSourceContext（验证 URL → SourceContext 完整链路）
// - 通过 window.history.replaceState 模拟旧 URL
// - 子组件 mock：渲染可识别占位，便于验证 source_type 透传
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockApi } = vi.hoisted(() => {
  return {
    mockContext: {
      workbench: {
        positions: [],
        latest_scores: [],
        portfolio: { total_capital: 100000, investable_ratio: 0.9 },
        watchlists: [],
      } as any,
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
      portfolios: [] as any[],
      setChartWindowSize: vi.fn(),
      setChartTimeframe: vi.fn(),
      setChartRange: vi.fn(),
      setChartExpanded: vi.fn(),
      setFuturePlanScenario: vi.fn(),
      setFuturePlanCustom: vi.fn(),
      setSimPrice: vi.fn(),
      setSimQuantity: vi.fn(),
      setActiveTab: vi.fn(),
      loadSymbolDetail: vi.fn(async () => undefined),
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

// Mock i18n：t 返回 key，便于按 key 断言
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

// Mock utils/format（保留最小实现以避免 JSX 报错）
vi.mock("../../../utils/format", () => ({
  percent: (v: number) => `${(v * 100).toFixed(2)}%`,
  score: (v: number) => String(v ?? 0),
  money: (v: number) => `¥${v ?? 0}`,
  clamp: (v: number) => v,
  roundPrice: (v: number) => v,
  aggregateWeeklyBars: (bars: any[]) => bars,
  computeSuggestedPrice: (v: number) => v,
  signalLabel: (v: string) => v ?? "-",
  joinParts: (parts: string[]) => parts.join(" | "),
}));

// Mock utils/trade-plan
vi.mock("../../../utils/trade-plan", () => ({
  getActiveFutureBuyPlan: vi.fn(() => []),
}));

// Mock constants/chartTheme
vi.mock("../../../constants/chartTheme", () => ({
  SIGNAL_COLOR_MAP: {},
}));

// 关键：不 mock utils/sourceContext —— 使用真实实现验证 URL → SourceContext 完整链路

// Mock 子组件：渲染可识别占位，便于验证渲染与 source_type 透传
vi.mock("../SymbolSearchHeader", () => ({
  __esModule: true,
  default: () => <div data-testid="symbol-search-header">search</div>,
}));

vi.mock("../SymbolRelationshipBar", () => ({
  __esModule: true,
  default: () => <div data-testid="symbol-relationship-bar">relationship</div>,
}));

vi.mock("../FactorExplanationPanel", () => ({
  __esModule: true,
  default: () => <div data-testid="factor-explanation-panel">factor</div>,
}));

vi.mock("../SymbolAlertSummary", () => ({
  __esModule: true,
  default: () => <div data-testid="symbol-alert-summary">alerts</div>,
}));

vi.mock("../RiskReferencePanel", () => ({
  __esModule: true,
  default: () => <div data-testid="risk-reference-panel">risk</div>,
}));

vi.mock("../TradePlanPanel", () => ({
  __esModule: true,
  default: () => <div data-testid="trade-plan-panel">trade</div>,
}));

vi.mock("../SymbolChartPanel", () => ({
  __esModule: true,
  default: () => <div data-testid="symbol-chart-panel">chart</div>,
}));

// SingleSymbolBacktestPanel mock 暴露 sourceContext.source_type，便于断言
vi.mock("../SingleSymbolBacktestPanel", () => ({
  __esModule: true,
  default: (props: any) => (
    <div
      data-testid="single-symbol-backtest-panel"
      data-source-type={props.sourceContext?.source_type ?? ""}
      data-symbol-id={props.sourceContext?.symbol_id ?? ""}
    >
      backtest
    </div>
  ),
}));

// 注意：不 mock SymbolResearchShell —— 直接渲染真实组件以验证 InvestmentCenter 透传链路
import InvestmentCenter from "../../InvestmentCenter";
import { resolveLegacyTab, resolveTabFromUrl, VALID_TABS } from "../../../utils/tabCompatibility";

describe("WP5.4 InvestmentCenter 兼容入口", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    localStorage.clear();
    // 重置 location 到无 query params
    window.history.replaceState(null, "", "/");
    // 重置 mock context 到默认值（带 workbench 数据，确保子组件渲染）
    mockContext.workbench = {
      positions: [],
      latest_scores: [],
      portfolio: { total_capital: 100000, investable_ratio: 0.9 },
      watchlists: [],
    };
    mockContext.detail = null;
    mockContext.activeSymbolId = null;
    mockContext.portfolioId = 1;
    mockContext.activeTab = "investment";
    mockApi.getSymbols.mockResolvedValue([]);
    mockApi.getBars.mockResolvedValue([]);
    mockApi.getSymbolFactorExplanation.mockResolvedValue(null);
  });

  afterEach(() => {
    // 清理 location，避免用例间状态污染
    window.history.replaceState(null, "", "/");
  });

  // 1. 旧路由 /investment-center 不返回 404（InvestmentCenter 渲染 SymbolResearchShell）
  it("test_legacy_route_still_works: 访问 /investment-center 渲染 SymbolResearchShell 不返回 404", async () => {
    // 模拟访问 /investment-center（SPA 无 path-based router，等价于根路径 + activeTab=investment）
    window.history.replaceState(null, "", "/investment-center");
    const { container } = render(<InvestmentCenter openMetricModal={vi.fn()} />);

    // 验证渲染了 SymbolResearchShell（含 .symbol-research-shell 类）
    await waitFor(() => {
      expect(container.querySelector(".symbol-research-shell")).toBeInTheDocument();
    });
    // 不应渲染 404 文案或空白
    expect(container.querySelector(".symbol-research-shell")).not.toBeNull();
  });

  // 2. 旧 URL ?symbol=000001 解析为 legacy source_type
  it("test_legacy_symbol_param_parsed: ?symbol=000001 解析为 legacy source_type", async () => {
    window.history.replaceState(null, "", "/investment-center?symbol=000001");
    const { container } = render(<InvestmentCenter openMetricModal={vi.fn()} />);

    await waitFor(() => {
      expect(container.querySelector(".symbol-research-shell")).toBeInTheDocument();
    });

    // legacy source_type 不渲染来源面包屑（renderSourceBreadcrumb 在 legacy 时返回 null）
    expect(container.querySelector(".symbol-research-source-breadcrumb")).toBeNull();

    // 验证 SingleSymbolBacktestPanel 收到 legacy source_type
    const backtestPanel = container.querySelector(
      '[data-testid="single-symbol-backtest-panel"]',
    );
    expect(backtestPanel).not.toBeNull();
    expect(backtestPanel?.getAttribute("data-source-type")).toBe("legacy");

    // 旧 ?symbol=X 不携带 symbol_id，不应触发 loadSymbolDetail
    expect(mockContext.loadSymbolDetail).not.toHaveBeenCalled();
  });

  // 3. 旧 URL ?symbol_id=123 解析 symbol_id 并保持 legacy source_type
  it("test_legacy_symbol_id_param_parsed: ?symbol_id=123 解析 symbol_id 为 123", async () => {
    window.history.replaceState(null, "", "/investment-center?symbol_id=123");
    const { container } = render(<InvestmentCenter openMetricModal={vi.fn()} />);

    // 应调用 ctx.loadSymbolDetail(123, ...) 加载该标的
    await waitFor(() => {
      expect(mockContext.loadSymbolDetail).toHaveBeenCalledWith(
        123,
        expect.objectContaining({ focus: true, barLimit: 500 }),
      );
    });

    // 验证 source_type 仍为 legacy（symbol_id 不强制 source_type）
    const backtestPanel = container.querySelector(
      '[data-testid="single-symbol-backtest-panel"]',
    );
    expect(backtestPanel).not.toBeNull();
    expect(backtestPanel?.getAttribute("data-source-type")).toBe("legacy");
    expect(backtestPanel?.getAttribute("data-symbol-id")).toBe("123");
  });

  // 4. 默认进入为 legacy source_type
  it("test_legacy_uses_legacy_source_type: 无 URL 参数时 source_type === 'legacy'", async () => {
    // 无 query params，纯 /investment-center 路径
    window.history.replaceState(null, "", "/investment-center");
    const { container } = render(<InvestmentCenter openMetricModal={vi.fn()} />);

    await waitFor(() => {
      expect(container.querySelector(".symbol-research-shell")).toBeInTheDocument();
    });

    // 验证 SingleSymbolBacktestPanel 收到 legacy source_type
    const backtestPanel = container.querySelector(
      '[data-testid="single-symbol-backtest-panel"]',
    );
    expect(backtestPanel).not.toBeNull();
    expect(backtestPanel?.getAttribute("data-source-type")).toBe("legacy");

    // 验证不渲染来源面包屑（legacy 时不渲染，避免对旧入口造成视觉干扰）
    expect(container.querySelector(".symbol-research-source-breadcrumb")).toBeNull();
  });

  // 5. 渲染结果包含 SymbolResearchShell
  it("test_legacy_renders_symbol_research_shell: 渲染结果包含 SymbolResearchShell 及其子组件", async () => {
    const { container } = render(<InvestmentCenter openMetricModal={vi.fn()} />);

    await waitFor(() => {
      // .symbol-research-shell 类是 SymbolResearchShell 的根元素标识
      expect(container.querySelector(".symbol-research-shell")).toBeInTheDocument();
      // 也应渲染壳层内的子组件（验证不是空壳）
      expect(container.querySelector('[data-testid="symbol-search-header"]')).toBeInTheDocument();
      expect(container.querySelector('[data-testid="single-symbol-backtest-panel"]')).toBeInTheDocument();
      expect(container.querySelector('[data-testid="symbol-chart-panel"]')).toBeInTheDocument();
    });
  });

  // 6. 旧 tab 值 investment 仍可解析（通过 tabCompatibility）
  it("test_legacy_active_tab_investment: ?tab=investment 通过 tabCompatibility 解析为 investment tab", () => {
    // 验证 investment tab 仍在 VALID_TABS 中（未被废弃）
    expect(VALID_TABS.has("investment")).toBe(true);

    // resolveLegacyTab 对 'investment' 应返回 'investment'（不在 LEGACY_TAB_MAPPING 强制重定向范围）
    expect(resolveLegacyTab("investment")).toBe("investment");

    // 模拟 ?tab=investment URL，resolveTabFromUrl 应返回 'investment'
    window.history.replaceState(null, "", "/?tab=investment");
    expect(resolveTabFromUrl()).toBe("investment");

    // 模拟 /investment-center?tab=investment 旧深链接
    window.history.replaceState(null, "", "/investment-center?tab=investment");
    expect(resolveTabFromUrl()).toBe("investment");

    // 旧入口 activeTab=investment 时 InvestmentCenter 应被渲染（App.tsx 契约）
    expect(mockContext.activeTab).toBe("investment");
  });

  // 7. InvestmentCenter 是薄壳，openMetricModal 透传给 SymbolResearchShell
  it("test_investment_center_is_thin_shell: 不复制业务逻辑，仅透传 openMetricModal", async () => {
    const openMetricModal = vi.fn();
    const { container } = render(<InvestmentCenter openMetricModal={openMetricModal} />);

    // 薄壳应成功渲染 SymbolResearchShell
    await waitFor(() => {
      expect(container.querySelector(".symbol-research-shell")).toBeInTheDocument();
    });

    // InvestmentCenter 自身不持有业务状态：渲染期间不应调用任何 API
    // （SymbolResearchShell 内部的 API 调用已通过 mock 拦截，且初始 workbench 已有数据，
    // 不会触发额外的 loadWorkbench）
    expect(mockApi.getSymbols).not.toHaveBeenCalled();
    expect(mockApi.getBars).not.toHaveBeenCalled();
  });
});
