import { vi } from "vitest";
import type { AppContextValue } from "../context/AppContext";
import type {
  AkshareApiStatus,
  AkshareStrategyInfo,
} from "../api/client";

/**
 * Mock 工厂：6 个组件测试共享的工厂函数。
 * - makeMockApi: 返回 api 对象的 mock，包含所有组件用到的 API 方法
 * - makeMockContext: 返回 useApp context 的 mock（含所有字段的默认值）
 * - mockI18n: mock i18n 模块，t(key) 返回 key，template 返回拼接后的字符串
 */

/** 默认 AkshareApiStatus 列表 */
export function makeMockAkshareApis(overrides: Partial<AkshareApiStatus>[] = []): AkshareApiStatus[] {
  const base: AkshareApiStatus[] = [
    {
      key: "stock_info_sz_name_code",
      name: "深交所股票列表",
      category: "discovery",
      module: "akshare",
      description: "深交所股票代码列表",
      default_strategy: "standard",
      enabled: true,
      anti_risk_strategy: "standard",
      delay_min_ms: 200,
      delay_max_ms: 500,
      last_probe_at: "2026-07-04T10:00:00Z",
      last_probe_success: true,
      last_probe_latency_ms: 120,
      last_probe_error: null,
      last_call_at: "2026-07-04T11:00:00Z",
      last_call_success: true,
      last_call_error: null,
      total_calls: 10,
      total_failures: 0,
    },
    {
      key: "stock_zh_a_spot_em",
      name: "东财实时行情",
      category: "realtime",
      module: "akshare",
      description: "东方财富实时行情",
      default_strategy: "conservative",
      enabled: true,
      anti_risk_strategy: "conservative",
      delay_min_ms: 1000,
      delay_max_ms: 2000,
      last_probe_at: null,
      last_probe_success: null,
      last_probe_latency_ms: null,
      last_probe_error: null,
      last_call_at: null,
      last_call_success: null,
      last_call_error: null,
      total_calls: 0,
      total_failures: 0,
    },
    {
      key: "fund_etf_fund_info_em",
      name: "ETF 基金信息",
      category: "etf",
      module: "akshare",
      description: "ETF 基金指标",
      default_strategy: "fast",
      enabled: false,
      anti_risk_strategy: "fast",
      delay_min_ms: 100,
      delay_max_ms: 200,
      last_probe_at: "2026-07-04T09:00:00Z",
      last_probe_success: false,
      last_probe_latency_ms: null,
      last_probe_error: "HTTP 403 Forbidden",
      last_call_at: "2026-07-04T09:30:00Z",
      last_call_success: false,
      last_call_error: "timeout",
      total_calls: 5,
      total_failures: 3,
    },
  ];
  if (overrides.length === 0) return base;
  return base.map((item, idx) => ({ ...item, ...overrides[idx] }));
}

/** 默认 AkshareStrategyInfo 列表 */
export function makeMockStrategies(): AkshareStrategyInfo[] {
  return [
    { key: "fast", name: "快速", delay_min_ms: 100, delay_max_ms: 200, max_retries: 1, desc: "快速档位" },
    { key: "standard", name: "标准", delay_min_ms: 200, delay_max_ms: 500, max_retries: 2, desc: "标准档位" },
    { key: "conservative", name: "保守", delay_min_ms: 1000, delay_max_ms: 2000, max_retries: 3, desc: "保守档位" },
    { key: "extreme", name: "极保守", delay_min_ms: 3000, delay_max_ms: 5000, max_retries: 5, desc: "极保守档位" },
    { key: "custom", name: "自定义", delay_min_ms: null, delay_max_ms: null, max_retries: 3, desc: "自定义档位" },
  ];
}

/** 创建 api 对象的 mock。overrides 可覆盖指定方法。 */
export function makeMockApi(overrides: Record<string, ReturnType<typeof vi.fn>> = {}) {
  const listAkshareApis = vi.fn(async () => makeMockAkshareApis());
  const listAkshareStrategies = vi.fn(async () => makeMockStrategies());
  const probeAkshareApi = vi.fn(async () => ({ key: "stock_info_sz_name_code", success: true, latency_ms: 150, error: null }));
  const updateAkshareApiConfig = vi.fn(async (_key: string, payload: any) => payload);

  const syncFundamental = vi.fn(async () => ({ total: 5, success: 4, skipped: 1, failed: 0, errors: [] }));
  const syncCapitalFlow = vi.fn(async () => ({ total: 5, success: 4, skipped: 1, failed: 0, errors: [] }));
  const syncEtfIndicators = vi.fn(async () => ({ total: 3, success: 3, skipped: 0, failed: 0, errors: [] }));

  const listScoringConfigs = vi.fn(async () => []);
  const getActiveScoringConfig = vi.fn(async () => null);
  const activateScoringConfig = vi.fn(async () => ({ ok: true }));
  const duplicateScoringConfig = vi.fn(async () => ({ ok: true }));
  const deleteScoringConfig = vi.fn(async () => ({ ok: true, deleted: 1 }));
  const createScoringConfig = vi.fn(async () => ({ ok: true }));
  const updateScoringConfig = vi.fn(async () => ({ ok: true }));
  const listScoringConfigVersions = vi.fn(async () => []);

  const getPositions = vi.fn(async () => []);
  const getAllocation = vi.fn(async () => null);
  const getSymbols = vi.fn(async () => []);
  const createSymbol = vi.fn(async () => ({ id: 1 }));
  const upsertPosition = vi.fn(async () => ({ ok: true }));
  const deletePosition = vi.fn(async () => ({ ok: true }));
  const upsertPortfolioRule = vi.fn(async () => ({ ok: true }));
  const backupDatabase = vi.fn(async () => ({ backup_path: "/tmp/backup.db" }));
  const listBackups = vi.fn(async () => []);
  const restoreDatabase = vi.fn(async () => ({ ok: true }));

  const getLatestDiscoveryCandidates = vi.fn(async () => []);
  const getDataHealth = vi.fn(async () => ({ bars: { coverage_pct: 95 } }));
  const getCustomIndicators = vi.fn(async () => []);
  const getDiscoveryPlans = vi.fn(async () => []);
  const evaluateDiscoveryIndicators = vi.fn(async () => []);
  const createDiscoveryPlan = vi.fn(async () => ({ id: 1 }));
  const updateDiscoveryPlan = vi.fn(async () => ({ id: 1 }));
  const deleteDiscoveryPlan = vi.fn(async () => ({ ok: true }));
  const updateDiscoveryResult = vi.fn(async () => ({ ok: true }));
  const refreshDiscoveryResult = vi.fn(async () => ({ ok: true }));
  const createJournal = vi.fn(async () => ({ id: 1 }));

  return {
    listAkshareApis,
    listAkshareStrategies,
    probeAkshareApi,
    updateAkshareApiConfig,
    syncFundamental,
    syncCapitalFlow,
    syncEtfIndicators,
    listScoringConfigs,
    getActiveScoringConfig,
    activateScoringConfig,
    duplicateScoringConfig,
    deleteScoringConfig,
    createScoringConfig,
    updateScoringConfig,
    listScoringConfigVersions,
    getPositions,
    getAllocation,
    getSymbols,
    createSymbol,
    upsertPosition,
    deletePosition,
    upsertPortfolioRule,
    backupDatabase,
    listBackups,
    restoreDatabase,
    getLatestDiscoveryCandidates,
    getDataHealth,
    getCustomIndicators,
    getDiscoveryPlans,
    evaluateDiscoveryIndicators,
    createDiscoveryPlan,
    updateDiscoveryPlan,
    deleteDiscoveryPlan,
    updateDiscoveryResult,
    refreshDiscoveryResult,
    createJournal,
    ...overrides,
  };
}

/** 创建 AppContextValue 的 mock。overrides 可覆盖指定字段。 */
export function makeMockContext(overrides: Partial<AppContextValue> = {}): AppContextValue {
  const noop = async () => {};
  const noopSync = () => {};
  const base: AppContextValue = {
    portfolios: [],
    portfolioId: 1,
    locale: "zh-CN" as const,
    marketGroup: "all",
    activeTab: "decision",
    activeSubTab: "portfolio-workbench",
    activeSymbolId: null,
    workbench: null,
    detail: null,
    detailCache: {},
    detailOrder: [],
    activeWatchlistId: null,
    watchlistItems: {},
    primaryWatchlistSymbolIds: new Set<number>(),
    symbolDirectory: {},
    chartTimeframe: "daily" as const,
    chartWindowSize: 60,
    chartRange: null,
    chartExpanded: false,
    signalRulePresets: [],
    signalRule: null,
    signalSampleLimit: null,
    futurePlanScenario: "general",
    futurePlanCustom: { horizonDays: 20, pullbackPct: 3, positionPct: 5 },
    newsSnapshot: null,
    discoveryTask: null,
    discoveryScopeStats: null,
    syncTask: null,
    simQuantity: "",
    simPrice: "",
    candidateSearch: "",
    globalLoading: false,
    signalRulePreview: null,
    discoveryPolling: false,
    syncPolling: false,
    setLocaleValue: noopSync,
    setMarketGroup: noopSync,
    setActiveTab: noopSync,
    setActiveSubTab: noopSync,
    setChartTimeframe: noopSync,
    setChartWindowSize: noopSync,
    setChartRange: noopSync,
    setChartExpanded: noopSync,
    setActiveSymbolId: noopSync,
    setFuturePlanScenario: noopSync,
    setFuturePlanCustom: noopSync,
    setSimQuantity: noopSync,
    setSimPrice: noopSync,
    setCandidateSearch: noopSync,
    setSignalSampleLimit: noopSync,
    showToast: noopSync as any,
    loadPortfolios: noop as any,
    loadWorkbench: noop as any,
    loadSymbolDetail: noop as any,
    loadSignalRuleConfig: noop as any,
    updateSignalRule: noopSync as any,
    saveSignalRule: noop as any,
    loadSignalRulePreview: noop as any,
    fetchDiscoveryTasks: noop as any,
    runDiscoveryMining: noop as any,
    sendDiscoveryTaskCommand: noop as any,
    refreshDiscoveryTasks: noop as any,
    cleanupExpiredDiscoveryResults: noop as any,
    loadDiscoveryScopeStats: noop as any,
    runNewsUpdate: noop as any,
    runSync: noop as any,
    cancelSync: noop as any,
    runScan: noop as any,
    addSymbolFromInput: noop as any,
    generateTradeSetup: noop as any,
    submitSimOrder: noop as any,
    fetchWatchlistItems: noop as any,
    addSymbolToWatchlist: noop as any,
    addSymbolToPrimaryWatchlist: noop as any,
    refreshPrimaryWatchlistMembership: noop as any,
    fetchVisibleSymbols: noop as any,
    syncOrderForm: noopSync as any,
    removeDetailFromDock: noopSync as any,
    setSignalRulePreview: noopSync as any,
  };
  return { ...base, ...overrides };
}

/** Mock i18n 模块：t(key) 返回 key 字符串，template 返回拼接后的字符串。 */
export function mockI18n() {
  vi.mock("../i18n", () => ({
    t: (key: string) => key,
    template: (key: string, params: Record<string, string | number> = {}) => {
      return key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? ""));
    },
    DOT: " | ",
    stageLabel: (v: string | null | undefined) => v ?? "-",
    actionLabel: (v: string | null | undefined) => v ?? "-",
    assetTypeLabel: (v: string | null | undefined) => v ?? "unknown",
    regionShortLabel: (v: string | null | undefined) => v ?? "-",
    regionLongLabel: (v: string | null | undefined) => v ?? "unknown",
    sideLabel: (v: string | null | undefined) => v ?? "-",
    watchlistTypeLabel: (v: string | null | undefined) => v ?? "-",
    sentimentLabel: (v: string | null | undefined) => v ?? "-",
    riskLabel: (v: string | null | undefined) => v ?? "-",
    newsSourceLabel: (v: string | null | undefined) => v ?? "-",
    setLocale: () => {},
    getLocale: () => "zh-CN",
  }));
}
