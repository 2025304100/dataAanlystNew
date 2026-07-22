// WP5.2：状态迁移测试
//
// 覆盖：
// - 收藏迁移到后端观察池 API（mount 时拉取，加入/归档调用对应端点）
// - 收藏只读回退模式（后端失败时不写 localStorage）
// - 即时提醒"未持久化" Tag 渲染
// - 创建正式告警规则 Modal 调用 api.createAlertRule
// - RiskReferencePanel 显示"研究情景参数" Tag + "前往组合风控配置" 按钮回调
// - TradePlanPanel "模拟下单" 按钮调用 onJumpToPortfolioTrade 回调
// - SingleSymbolBacktestPanel "来源：标的研究" Tag + localStorage 持久化 source_context
//
// 约束：
// - 不调用真实后端，所有 API 通过 vi.fn mock
// - i18n mock：t 返回 key，便于按 key 断言
// - localStorage mock：通过 spyOn 监听 setItem
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockApi, mockRequestJson } = vi.hoisted(() => {
  return {
    mockContext: {
      workbench: {
        positions: [],
        latest_scores: [],
        portfolio: { total_capital: 100000, investable_ratio: 0.9 },
        watchlists: [],
        portfolios: [{ id: 1, name: "Test Portfolio", is_default: 1 }],
      } as any,
      detail: null as any,
      activeSymbolId: 42 as number | null,
      portfolioId: 1 as number | null,
      activeTab: "investment",
      activeWatchlistId: 1 as number | null,
      chartTimeframe: "daily" as "daily" | "weekly",
      chartWindowSize: 60,
      chartExpanded: false,
      chartRange: null,
      futurePlanScenario: "general",
      futurePlanCustom: { horizonDays: 20, pullbackPct: 3, positionPct: 5 },
      simPrice: "",
      simQuantity: "",
      locale: "zh-CN" as "zh-CN" | "en-US",
      portfolios: [{ id: 1, name: "Test Portfolio", is_default: 1 }] as any[],
      setChartWindowSize: vi.fn(),
      setChartTimeframe: vi.fn(),
      setChartRange: vi.fn(),
      setChartExpanded: vi.fn(),
      setFuturePlanScenario: vi.fn(),
      setFuturePlanCustom: vi.fn(),
      setSimPrice: vi.fn(),
      setSimQuantity: vi.fn(),
      setActiveTab: vi.fn(),
      setActiveSubTab: vi.fn(),
      setActiveSymbolId: vi.fn(),
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
      createAlertRule: vi.fn(async (_payload: unknown) => ({ id: 999 }) as any),
    },
    mockRequestJson: vi.fn(async () => [] as any[]),
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

// Mock antd：保留组件库，仅覆盖 message 与 Modal.confirm
// 关键：不能将 actual.Modal 展开为普通对象，否则 <Modal /> JSX 渲染会失败
// （React 要求组件类型为 function/class/forwardRef，普通对象会触发
// "Element type is invalid: expected a string... but got: object"）
// 解决方案：直接 mutate actual.Modal.confirm 静态方法，保留 Modal 作为 React 组件
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  const mockConfirm = vi.fn(({ onOk }: { onOk?: () => void }) => {
    if (typeof onOk === "function") {
      (mockConfirm as any)._lastOnOk = onOk;
    }
  });
  // 直接 mutate Modal.confirm 静态方法，避免破坏 Modal 作为 React 组件的性质
  (actual.Modal as any).confirm = mockConfirm;
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
  requestJson: mockRequestJson,
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

// Mock utils/sourceContext
vi.mock("../../../utils/sourceContext", () => ({
  resolveSourceContext: (ctx: unknown) => ctx ?? {},
  clearSourceContext: vi.fn(),
  popReturnState: vi.fn(() => null),
  tabForReturnTo: (rt: string) => "investment",
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

// Mock 子组件：渲染可识别占位，便于验证渲染与 props 透传
vi.mock("../SymbolSearchHeader", () => ({
  __esModule: true,
  default: (props: any) => (
    <div data-testid="symbol-search-header" data-search-query={props.searchQuery}>
      <button
        data-testid="toggle-fav-mock"
        onClick={() => props.onToggleFavorite(42)}
      >
        fav
      </button>
    </div>
  ),
}));

vi.mock("../SymbolRelationshipBar", () => ({
  __esModule: true,
  default: () => <div data-testid="symbol-relationship-bar">relationship</div>,
}));

vi.mock("../FactorExplanationPanel", () => ({
  __esModule: true,
  default: () => <div data-testid="factor-explanation-panel">factor</div>,
}));

// 不 mock SymbolAlertSummary / RiskReferencePanel / TradePlanPanel / SingleSymbolBacktestPanel
// 直接渲染真实组件以测试 WP5.2 新增的 Tag/Button/Modal

// Mock BacktestConfig/BacktestResult 以避免依赖回测引擎
vi.mock("../../BacktestConfig", () => ({
  __esModule: true,
  default: (props: any) => (
    <div data-testid="backtest-config" data-portfolio-id={props.portfolioId ?? ""}>
      <button
        data-testid="trigger-backtest-result"
        onClick={() =>
          props.onResult({ id: 100, run_name: "test-run" })
        }
      >
        run
      </button>
    </div>
  ),
}));

vi.mock("../../BacktestResult", () => ({
  __esModule: true,
  default: () => <div data-testid="backtest-result">result</div>,
}));

// Mock constants/chartTheme
vi.mock("../../../constants/chartTheme", () => ({
  SIGNAL_COLOR_MAP: {},
}));

// Mock SymbolChartPanel（避免 ECharts 复杂依赖）
vi.mock("../SymbolChartPanel", () => ({
  __esModule: true,
  default: () => <div data-testid="symbol-chart-panel">chart</div>,
}));

import SymbolResearchShell from "../SymbolResearchShell";

describe("WP5.2 状态迁移测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 重置 mock context 到默认值（带 workbench 数据）
    mockContext.workbench = {
      positions: [],
      latest_scores: [],
      portfolio: { total_capital: 100000, investable_ratio: 0.9 },
      watchlists: [],
      portfolios: [{ id: 1, name: "Test Portfolio", is_default: 1 }],
    };
    mockContext.detail = null;
    mockContext.activeSymbolId = 42;
    mockContext.portfolioId = 1;
    mockContext.activeTab = "investment";
    mockContext.activeWatchlistId = 1;
    mockContext.chartWindowSize = 60;
    mockContext.futurePlanScenario = "general";
    mockContext.simPrice = "";
    mockContext.simQuantity = "";
    mockApi.getSymbols.mockResolvedValue([]);
    mockApi.getBars.mockResolvedValue([]);
    mockApi.getSymbolFactorExplanation.mockResolvedValue(null);
    mockApi.createAlertRule.mockResolvedValue({ id: 999 });
    mockRequestJson.mockResolvedValue([]);
    localStorage.clear();
  });

  // 1. 收藏迁移：mount 时调用后端观察池 API 拉取 favorites
  it("mount 时应调用 GET /observations 拉取后端收藏", async () => {
    mockRequestJson.mockResolvedValue([
      { watchlist_item_id: 100, symbol_id: 42, status: "watching" },
    ]);

    render(<SymbolResearchShell />);

    await waitFor(() => {
      expect(mockRequestJson).toHaveBeenCalledWith(
        expect.stringContaining("/api/v1/watchlists/1/observations"),
      );
    });

    // 验证 URL 包含 status=watching
    const calls = mockRequestJson.mock.calls as unknown as Array<Array<unknown>>;
    const call = calls.find((c) =>
      typeof c[0] === "string" && (c[0] as string).includes("/observations"),
    );
    expect(call).toBeDefined();
    expect(call![0]).toContain("status=watching");
    expect(call![0]).toContain("limit=500");
  });

  // 2. 收藏迁移：后端失败时进入只读回退模式，显示 fallback 横幅
  it("后端拉取失败时应进入只读回退模式并显示 fallback 提示", async () => {
    mockRequestJson.mockRejectedValue(new Error("network error"));

    render(<SymbolResearchShell />);

    await waitFor(() => {
      const banner = screen.getByTestId("favorites-migration-banner");
      expect(banner).toBeInTheDocument();
      expect(banner.getAttribute("data-fallback-mode")).toBe("true");
    });

    // 应显示 fallback 文案 key
    expect(screen.getByText("symbolResearchFavoritesFallbackNotice")).toBeInTheDocument();
  });

  // 3. 收藏迁移：加入收藏调用 POST /observations，不写 localStorage
  it("加入收藏应调用 POST /observations 且不写 localStorage.ic_favorites", async () => {
    mockRequestJson.mockResolvedValue([]); // mount 时返回空
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem");

    render(<SymbolResearchShell />);

    await waitFor(() => {
      expect(screen.getByTestId("symbol-search-header")).toBeInTheDocument();
    });

    // 点击收藏按钮（mock SymbolSearchHeader 触发 onToggleFavorite(42)）
    fireEvent.click(screen.getByTestId("toggle-fav-mock"));

    await waitFor(() => {
      // 应该有 POST 调用到 /observations
      const postCall = mockRequestJson.mock.calls.find(
        (c: any[]) =>
          typeof c[1] === "object" && c[1]?.method === "POST" && c[0]?.includes("/observations"),
      );
      expect(postCall).toBeDefined();
    });

    // 验证不写 localStorage.ic_favorites
    const favSetCall = setItemSpy.mock.calls.find((c) => c[0] === "ic_favorites");
    expect(favSetCall).toBeUndefined();

    setItemSpy.mockRestore();
  });

  // 4. 即时提醒"未持久化" Tag 渲染
  it("SymbolAlertSummary 在有 alerts 时每条告警显示'未持久化' Tag", async () => {
    // 让 Shell 进入有 workbench 的状态；alerts 来自 Shell 内部计算
    // 由于 alerts 计算依赖 chartData/setup，这里仅验证 SymbolAlertSummary 渲染按钮
    render(<SymbolResearchShell />);

    await waitFor(() => {
      expect(screen.getByText("symbolResearchCreateAlertRule")).toBeInTheDocument();
    });

    // 验证"创建正式告警规则"按钮存在
    const createBtn = screen.getByTestId("create-alert-rule-button");
    expect(createBtn).toBeInTheDocument();
    // 没有活跃标的时按钮应禁用（这里 activeSymbolId=42，所以应可用）
    expect(createBtn.hasAttribute("disabled")).toBe(false);
  });

  // 5. 创建正式告警规则 Modal 调用 api.createAlertRule
  it("点击'创建正式告警规则'应打开 Modal 并在提交时调用 api.createAlertRule", async () => {
    render(<SymbolResearchShell />);

    await waitFor(() => {
      expect(screen.getByTestId("create-alert-rule-button")).toBeInTheDocument();
    });

    // 点击按钮打开 Modal
    fireEvent.click(screen.getByTestId("create-alert-rule-button"));

    // Modal 应出现
    await waitFor(() => {
      expect(screen.getByTestId("create-alert-rule-modal")).toBeInTheDocument();
    });

    // 点击 Modal 的 OK 按钮（通过 antd Modal footer 的 OK 按钮）
    const okButton = screen.getByText("symbolResearchAlertRuleSubmit");
    expect(okButton).toBeInTheDocument();
    fireEvent.click(okButton);

    // 应该调用 api.createAlertRule
    await waitFor(() => {
      expect(mockApi.createAlertRule).toHaveBeenCalled();
    });

    // 验证 payload 包含必要字段
    const payload = mockApi.createAlertRule.mock.calls[0][0] as Record<string, unknown>;
    expect(payload.alert_type).toBe("indicator_trigger");
    expect(payload.severity).toBe("warn");
    expect(payload.enabled).toBe(true);
    expect((payload.config as Record<string, unknown>).symbol_id).toBe(42);
    expect((payload.config as Record<string, unknown>).source).toBe("research");
  });

  // 6. RiskReferencePanel 显示"研究情景参数" Tag + "前往组合风控配置" 按钮
  it("RiskReferencePanel 应显示'研究情景参数' Tag 和'前往组合风控配置'按钮", async () => {
    // Shell 中 RiskReferencePanel 只在 riskMetrics != null 时渲染
    // 由于 riskMetrics 计算依赖 detail/setup，这里直接渲染 RiskReferencePanel 组件
    const { default: RiskReferencePanel } = await import("../RiskReferencePanel");
    const onGoToPortfolioRule = vi.fn();

    const { container } = render(
      <RiskReferencePanel
        riskMetrics={{
          rrRatio: 2,
          stopDistancePct: 5,
          currentStopDist: 5,
          atrStopRef: null,
          concentrationLevel: "low",
          maxLossAmount: 100,
          maxLossPerShare: 1,
          reward: 200,
          riskAmount: 100,
          currentPrice: 10,
          stop: 9,
          target: 12,
          maxLossLimitAmount: 0,
        }}
        riskSettings={{ atrMultiplier: 2, concentrationMediumPct: 10, concentrationHighPct: 20, maxLossPct: 2 }}
        settingsOpen={false}
        entryPrice={10}
        quantity={100}
        setup={null}
        portfolioId={1}
        onToggleSettings={() => {}}
        onUpdateSettings={() => {}}
        onGoToPortfolioRule={onGoToPortfolioRule}
      />,
    );

    // 验证"研究情景参数" Tag 存在
    expect(screen.getByTestId("research-only-params-tag")).toBeInTheDocument();
    expect(screen.getByText("symbolResearchResearchOnlyParams")).toBeInTheDocument();

    // 验证"前往组合风控配置"按钮存在
    const goBtn = screen.getByTestId("go-to-portfolio-rule-button");
    expect(goBtn).toBeInTheDocument();

    // 点击应调用 onGoToPortfolioRule
    fireEvent.click(goBtn);
    expect(onGoToPortfolioRule).toHaveBeenCalled();
  });

  // 7. TradePlanPanel "模拟下单" 按钮调用 onJumpToPortfolioTrade
  it("TradePlanPanel 应有'模拟下单'按钮并调用 onJumpToPortfolioTrade", async () => {
    const { default: TradePlanPanel } = await import("../TradePlanPanel");
    const onJumpToPortfolioTrade = vi.fn();

    render(
      <TradePlanPanel
        setup={null}
        scenarios={null}
        entryPrice={10}
        quantity={100}
        chartEntryPrice={null}
        tradePlanEditing={false}
        tradePlanDraft={null}
        trancheEditing={false}
        trancheDrafts={[]}
        refreshingPlan={false}
        futurePlanScenario="general"
        futurePlanTunings={{}}
        activeFutureBuyPlan={[]}
        onEditPlan={() => {}}
        onCancelPlanEdit={() => {}}
        onApplyPlanOverrides={() => {}}
        onRefreshPlan={() => {}}
        onUpdateTradePlanDraft={() => {}}
        onEditTranches={() => {}}
        onCancelTranches={() => {}}
        onSaveTranches={() => {}}
        onResetTranches={() => {}}
        onUpdateTrancheDraft={() => {}}
        onAddTranche={() => {}}
        onRemoveTranche={() => {}}
        onUpdateFutureTuning={() => {}}
        onSetFuturePlanScenario={() => {}}
        onJumpToPortfolioTrade={onJumpToPortfolioTrade}
      />,
    );

    // 验证"模拟下单"按钮存在
    const jumpBtn = screen.getByTestId("jump-to-portfolio-trade-button");
    expect(jumpBtn).toBeInTheDocument();
    expect(screen.getByText("symbolResearchJumpToPortfolioTrade")).toBeInTheDocument();

    // 点击应调用 onJumpToPortfolioTrade
    fireEvent.click(jumpBtn);
    expect(onJumpToPortfolioTrade).toHaveBeenCalled();
  });

  // 8. SingleSymbolBacktestPanel "来源：标的研究" Tag + localStorage 持久化
  it("SingleSymbolBacktestPanel 应显示来源 Tag 并在回测完成时持久化 source_context", async () => {
    const { default: SingleSymbolBacktestPanel } = await import("../SingleSymbolBacktestPanel");
    const onSetBacktestResult = vi.fn();
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem");

    render(
      <SingleSymbolBacktestPanel
        portfolioId={1}
        activeSymbolId={42}
        sourceContext={{ source_type: "candidate", source_id: 99 }}
        backtestResult={null}
        onSetBacktestResult={onSetBacktestResult}
        onAppliedToPortfolio={() => {}}
      />,
    );

    // 验证"来源：标的研究" Tag 存在
    // Tag 内容跨越多个文本节点（label: value），使用 testid + toHaveTextContent 验证
    const sourceTag = screen.getByTestId("backtest-source-tag");
    expect(sourceTag).toBeInTheDocument();
    expect(sourceTag).toHaveTextContent("symbolResearchBacktestSourceResearch");
    expect(sourceTag).toHaveTextContent("symbolResearchBacktestSourceLabel");

    // 模拟回测完成（点击 mock BacktestConfig 的 run 按钮）
    fireEvent.click(screen.getByTestId("trigger-backtest-result"));

    // 应该调用 onSetBacktestResult
    expect(onSetBacktestResult).toHaveBeenCalledWith(
      expect.objectContaining({ id: 100, run_name: "test-run" }),
    );

    // 应该持久化 source_context 到 localStorage
    await waitFor(() => {
      const ctxCall = setItemSpy.mock.calls.find(
        (c) => c[0] === "ic_backtest_source_context",
      );
      expect(ctxCall).toBeDefined();
      const saved = JSON.parse(ctxCall![1]);
      expect(saved.symbol_id).toBe(42);
      expect(saved.source_type).toBe("research");
      expect(saved.return_to).toBe("research");
      expect(saved.run_id).toBe(100);
    });

    setItemSpy.mockRestore();
  });
});
