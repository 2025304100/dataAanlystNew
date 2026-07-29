import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from "react";
import { message } from "antd";
import { api, onRequestChange } from "../api/client";
import { setLocale, t, template } from "../i18n";
import {
  Portfolio,
  DashboardWorkbench,
  SymbolDetail,
  SignalRulePreset,
  SignalRule,
  SignalRulePreviewResult,
  DiscoveryTask,
  DiscoveryScopeStats,
  NewsSnapshot,
  WatchlistItem,
  Symbol,
  TradeSetupOverrides,
  CapabilitiesResponse,
  CapabilityItem,
} from "../types";
import { computeSuggestedPrice, computeSuggestedBuyQuantity, computeDefaultSellQuantity, score as fmtScore, setCurrency, inferSymbolPayload } from "../utils/format";

// WP0.4：旧路由与 activeTab 兼容映射（re-export，便于从 context 模块统一访问）
export {
  resolveLegacyTab,
  resolveTabFromUrl,
  LEGACY_TAB_MAPPING,
  VALID_TABS,
  DEFAULT_TAB,
} from "../utils/tabCompatibility";

const DEFAULT_CHART_WINDOW = 60;

interface AppState {
  portfolios: Portfolio[];
  portfolioId: number | null;
  locale: "zh-CN" | "en-US";
  marketGroup: string;
  activeTab: string;
  activeSubTab: string;
  activeSymbolId: number | null;
  workbench: DashboardWorkbench | null;
  detail: SymbolDetail | null;
  detailFocusRequest: number;
  detailCache: Record<number, SymbolDetail>;
  detailOrder: number[];
  activeWatchlistId: number | null;
  watchlistItems: Record<number, WatchlistItem[]>;
  primaryWatchlistSymbolIds: Set<number>;
  symbolDirectory: Record<number, Symbol>;
  chartTimeframe: "daily" | "weekly";
  chartWindowSize: number;
  chartRange: { start: number; end: number } | null;
  chartExpanded: boolean;
  signalRulePresets: SignalRulePreset[];
  signalRule: SignalRule | null;
  signalSampleLimit: number | null;
  futurePlanScenario: string;
  futurePlanCustom: { horizonDays: number; pullbackPct: number; positionPct: number };
  newsSnapshot: NewsSnapshot | null;
  discoveryTask: DiscoveryTask | null;
  discoveryScopeStats: DiscoveryScopeStats | null;
  syncTask: any | null;
  simQuantity: string;
  simPrice: string;
  candidateSearch: string;
  globalLoading: boolean;
  capabilities: CapabilitiesResponse | null;
  capabilitiesLoading: boolean;
}

export interface AppContextValue extends AppState {
  setLocaleValue: (locale: "zh-CN" | "en-US") => void;
  setMarketGroup: (market: string) => void;
  setActiveTab: (tab: string) => void;
  setActiveSubTab: (subTab: string) => void;
  setChartTimeframe: (tf: "daily" | "weekly") => void;
  setChartWindowSize: (size: number) => void;
  setChartRange: (range: { start: number; end: number } | null) => void;
  setChartExpanded: (expanded: boolean) => void;
  setActiveSymbolId: (id: number | null) => void;
  setFuturePlanScenario: (scenario: string) => void;
  setFuturePlanCustom: (custom: { horizonDays: number; pullbackPct: number; positionPct: number }) => void;
  setSimQuantity: (qty: string) => void;
  setSimPrice: (price: string) => void;
  setCandidateSearch: (search: string) => void;
  setSignalSampleLimit: (limit: number | null) => void;
  showToast: (type: "success" | "error" | "info", msg: string) => void;
  loadPortfolios: () => Promise<void>;
  // P0-7：组合管理方法（切换/创建/更新/删除）
  switchPortfolio: (portfolioId: number) => Promise<void>;
  createPortfolio: (payload: {
    name: string;
    account_type: string;
    total_capital: number;
    investable_ratio: number;
    cash_reserve_ratio: number;
    currency?: string;
    is_default?: boolean;
  }) => Promise<Portfolio | null>;
  updatePortfolio: (portfolioId: number, payload: {
    name?: string;
    total_capital?: number;
    investable_ratio?: number;
    cash_reserve_ratio?: number;
    currency?: string;
    is_default?: boolean;
  }) => Promise<Portfolio | null>;
  deletePortfolio: (portfolioId: number) => Promise<boolean>;
  loadWorkbench: () => Promise<void>;
  loadSymbolDetail: (symbolId: number, options?: { force?: boolean; focus?: boolean; barLimit?: number }) => Promise<SymbolDetail | undefined>;
  loadSignalRuleConfig: () => Promise<void>;
  updateSignalRule: (partial: Partial<SignalRule>) => void;
  saveSignalRule: () => Promise<void>;
  loadSignalRulePreview: () => Promise<void>;
  fetchDiscoveryTasks: (scope?: string) => Promise<DiscoveryTask | null>;
  setDiscoveryScope: (scope: string) => Promise<void>;
  runDiscoveryMining: (config?: {
    scope?: string;
    minScore?: number;
    dataMode?: string;
    batchSize?: number;
    delaySeconds?: number;
    maxWorkers?: number;
    warningDays?: number;
    validDays?: number;
    includeNews?: boolean;
  }) => Promise<void>;
  sendDiscoveryTaskCommand: (command: string) => Promise<void>;
  refreshDiscoveryTasks: () => Promise<void>;
  cleanupExpiredDiscoveryResults: () => Promise<void>;
  loadDiscoveryScopeStats: () => Promise<void>;
  runNewsUpdate: () => Promise<void>;
  runSync: () => Promise<void>;
  cancelSync: () => Promise<void>;
  runScan: () => Promise<void>;
  addSymbolFromInput: (code: string) => Promise<void>;
  generateTradeSetup: (overrides?: TradeSetupOverrides) => Promise<void>;
  submitSimOrder: (side: "buy" | "sell") => Promise<void>;
  fetchWatchlistItems: (watchlistId: number) => Promise<WatchlistItem[]>;
  addSymbolToWatchlist: (watchlistId: number, symbolId: number) => Promise<void>;
  addSymbolToPrimaryWatchlist: (symbolId: number) => Promise<void>;
  refreshPrimaryWatchlistMembership: () => Promise<void>;
  fetchVisibleSymbols: () => Promise<Symbol[]>;
  syncOrderForm: (detail: SymbolDetail | null) => void;
  removeDetailFromDock: (symbolId: number) => void;
  signalRulePreview: SignalRulePreviewResult | null;
  setSignalRulePreview: (result: SignalRulePreviewResult | null) => void;
  discoveryPolling: boolean;
  syncPolling: boolean;
  loadCapabilities: () => Promise<void>;
  getCapability: (key: string) => CapabilityItem | undefined;
  isCapabilityBlocked: (key: string) => boolean;
}

export const AppContext = createContext<AppContextValue | null>(null);

export function useApp() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp must be used within AppProvider");
  return ctx;
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AppState>({
    portfolios: [],
    portfolioId: null,
    locale: "zh-CN",
    marketGroup: "all",
    activeTab: "decision",
    activeSubTab: "portfolio-workbench",
    activeSymbolId: null,
    workbench: null,
    detail: null,
    detailFocusRequest: 0,
    detailCache: {},
    detailOrder: [],
    activeWatchlistId: null,
    watchlistItems: {},
    primaryWatchlistSymbolIds: new Set(),
    symbolDirectory: {},
    chartTimeframe: "daily",
    chartWindowSize: DEFAULT_CHART_WINDOW,
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
    capabilities: null,
    capabilitiesLoading: false,
  });

  const [signalRulePreview, setSignalRulePreview] = useState<SignalRulePreviewResult | null>(null);
  const [discoveryPolling, setDiscoveryPolling] = useState(false);
  const discoveryPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // 当前挖掘 scope 的 ref 镜像：供 fetchDiscoveryTasks/startDiscoveryPolling 读取最新值，
  // 切换 scope 时通过 setDiscoveryScope 更新此 ref，避免轮询拉取错误 scope 的任务状态。
  const discoveryScopeRef = useRef<string>("cn-stock");
  const [syncPolling, setSyncPolling] = useState(false);
  const syncPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const syncTaskIdRef = useRef<string | null>(null);
  const signalRulePreviewTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const signalSampleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const portfolioIdRef = useRef<number | null>(null);
  // detailCache 的 ref 镜像：供 useCallback 读取最新值而不必进入依赖数组
  const detailCacheRef = useRef<Record<number, SymbolDetail>>({});
  const detailFocusRequestRef = useRef(0);

  const update = useCallback((partial: Partial<AppState>) => {
    setState((prev) => ({ ...prev, ...partial }));
    if (partial.portfolioId !== undefined) portfolioIdRef.current = partial.portfolioId;
    if (partial.detailCache !== undefined) detailCacheRef.current = partial.detailCache;
  }, []);

  const showToast = useCallback((type: "success" | "error" | "info", msg: string) => {
    if (!msg) return;
    if (type === "error") message.error(msg);
    else if (type === "success") message.success(msg);
    else message.info(msg);
  }, []);

  const setLocaleValue = useCallback((locale: "zh-CN" | "en-US") => {
    setLocale(locale);
    update({ locale });
  }, [update]);

  const setMarketGroup = useCallback((market: string) => update({ marketGroup: market }), [update]);
  const setActiveTab = useCallback((tab: string) => update({ activeTab: tab }), [update]);
  const setActiveSubTab = useCallback((subTab: string) => update({ activeSubTab: subTab }), [update]);
  const setChartTimeframe = useCallback((tf: "daily" | "weekly") => update({ chartTimeframe: tf, chartRange: null, chartWindowSize: DEFAULT_CHART_WINDOW }), [update]);
  const setChartWindowSize = useCallback((size: number) => update({ chartWindowSize: size }), [update]);
  const setChartRange = useCallback((range: { start: number; end: number } | null) => update({ chartRange: range }), [update]);
  const setChartExpanded = useCallback((expanded: boolean) => update({ chartExpanded: expanded }), [update]);
  const setActiveSymbolId = useCallback((id: number | null) => update({ activeSymbolId: id }), [update]);
  const setFuturePlanScenario = useCallback((scenario: string) => update({ futurePlanScenario: scenario }), [update]);
  const setFuturePlanCustom = useCallback((custom: { horizonDays: number; pullbackPct: number; positionPct: number }) => update({ futurePlanCustom: custom }), [update]);
  const setSimQuantity = useCallback((qty: string) => update({ simQuantity: qty }), [update]);
  const setSimPrice = useCallback((price: string) => update({ simPrice: price }), [update]);
  const setCandidateSearch = useCallback((search: string) => update({ candidateSearch: search }), [update]);
  const setSignalSampleLimit = useCallback((limit: number | null) => update({ signalSampleLimit: limit }), [update]);

  const syncOrderForm = useCallback((detail: SymbolDetail | null) => {
    if (!detail) {
      update({ simQuantity: "", simPrice: "" });
      return;
    }
    const suggestedQty = computeSuggestedBuyQuantity(detail);
    const suggestedPrice = computeSuggestedPrice(detail);
    update({
      simQuantity: suggestedQty > 0 ? String(suggestedQty) : "",
      simPrice: suggestedPrice > 0 ? fmtScore(suggestedPrice) : "",
    });
  }, [update]);

  const fetchVisibleSymbols = useCallback(async (): Promise<Symbol[]> => {
    const response = await api.getAllSymbols();
    update({ symbolDirectory: Object.fromEntries(response.map((item) => [item.id, item])) });
    return state.marketGroup === "all" ? response : response.filter((item) => item.region === state.marketGroup);
  }, [state.marketGroup, update]);

  const fetchWatchlistItems = useCallback(async (watchlistId: number): Promise<WatchlistItem[]> => {
    if (state.watchlistItems[watchlistId]) return state.watchlistItems[watchlistId];
    const [items, directory] = await Promise.all([
      api.getWatchlistItems(watchlistId),
      (async () => {
        if (Object.keys(state.symbolDirectory).length) return state.symbolDirectory;
        const symbols = await api.getAllSymbols();
        const dir = Object.fromEntries(symbols.map((item) => [item.id, item]));
        update({ symbolDirectory: dir });
        return dir;
      })(),
    ]);
    const mapped = items.map((item: any) => ({ ...item, symbol: directory[item.symbol_id] ?? null }));
    update({ watchlistItems: { ...state.watchlistItems, [watchlistId]: mapped } });
    return mapped;
  }, [state.watchlistItems, state.symbolDirectory, update]);

  const refreshPrimaryWatchlistMembership = useCallback(async () => {
    const lists = state.workbench?.watchlists ?? [];
    const watchlist = lists.find((item) => item.list_type === "watch") ?? lists[0] ?? null;
    if (!watchlist) {
      update({ primaryWatchlistSymbolIds: new Set() });
      return;
    }
    const items = await fetchWatchlistItems(watchlist.id);
    update({ primaryWatchlistSymbolIds: new Set(items.map((item) => Number(item.symbol_id))) });
  }, [state.workbench, fetchWatchlistItems, update]);

  const loadLatestNewsSnapshot = useCallback(async (data: DashboardWorkbench) => {
    const symbolIds = [
      ...new Set([
        ...data.candidates.map((item) => item.symbol_id),
        ...data.latest_scores.map((item) => item.symbol_id),
      ]),
    ].slice(0, 20);
    if (!symbolIds.length) {
      update({ newsSnapshot: null });
      return;
    }
    try {
      const pid = portfolioIdRef.current;
      if (!pid) return;
      const response = await api.getLatestNews(pid, symbolIds);
      update({ newsSnapshot: response.symbols_total || response.macro ? response : null });
    } catch (err) {
      console.warn("loadLatestNewsSnapshot failed", err);
      update({ newsSnapshot: null });
    }
  }, [update]);

  const loadWorkbench = useCallback(async () => {
    const pid = portfolioIdRef.current;
    if (!pid) return;
    try {
      await loadDiscoveryScopeStatsInternal();
      const data = await api.getWorkbench(pid, state.marketGroup);
      setCurrency(data.portfolio?.currency || "CNY");
      update({ workbench: data });
      await refreshPrimaryWatchlistMembership();
      await loadLatestNewsSnapshot(data);

      const visibleSymbolIds = new Set([
        ...data.candidates.map((item: { symbol_id: number }) => item.symbol_id),
        ...data.latest_scores.map((item: { symbol_id: number }) => item.symbol_id),
      ]);
      if (state.activeSymbolId && visibleSymbolIds.has(state.activeSymbolId) && detailCacheRef.current[state.activeSymbolId]) {
        await loadSymbolDetail(state.activeSymbolId, { force: true });
      }
    } catch (error: any) {
      showToast("error", error?.message || t("loadFailed"));
    }
  }, [state.marketGroup, state.activeSymbolId, update, showToast]);

  const loadSymbolDetail = useCallback(async (symbolId: number, options?: { force?: boolean; focus?: boolean; barLimit?: number }): Promise<SymbolDetail | undefined> => {
    const initialUpdates: Partial<AppState> = { chartRange: null, chartWindowSize: DEFAULT_CHART_WINDOW };
    if (state.activeSymbolId !== symbolId) {
      initialUpdates.activeSymbolId = symbolId;
      initialUpdates.detail = null;
    }
    update(initialUpdates);
    const cached = detailCacheRef.current[symbolId];
    if (cached && !options?.force) {
      const cachedUpdates: Partial<AppState> = { detail: cached };
      if (options?.focus) {
        detailFocusRequestRef.current += 1;
        cachedUpdates.detailFocusRequest = detailFocusRequestRef.current;
      }
      update(cachedUpdates);
      scheduleSignalRulePreviewInternal();
      return cached;
    }
    const pid = portfolioIdRef.current;
    if (!pid) return;
    try {
      const detail = await api.getSymbolDetail(pid, symbolId, state.signalSampleLimit ?? undefined, options?.barLimit);
      // Remember detail
      const newCache = { ...detailCacheRef.current, [symbolId]: detail };
      const newOrder = [symbolId, ...state.detailOrder.filter((item) => item !== symbolId)].slice(0, 4);
      const detailUpdates: Partial<AppState> = { detail, detailCache: newCache, detailOrder: newOrder };
      if (options?.focus) {
        detailFocusRequestRef.current += 1;
        detailUpdates.detailFocusRequest = detailFocusRequestRef.current;
      }
      update(detailUpdates);
      syncOrderForm(detail);
      scheduleSignalRulePreviewInternal();
      return detail;
    } catch (error: any) {
      showToast("error", error?.message || t("loadFailed"));
    }
  }, [state.activeSymbolId, state.detailOrder, state.signalSampleLimit, update, syncOrderForm, showToast]);

  const loadPortfolios = useCallback(async () => {
    try {
      const portfolios = await api.getPortfolios();
      if (!portfolios.length) {
        update({ portfolios, portfolioId: null });
        return;
      }
      const defaultPortfolio = portfolios.find((item) => Number(item.is_default) === 1) ?? portfolios[0];
      update({ portfolios, portfolioId: defaultPortfolio.id });
    } catch (error: any) {
      showToast("error", error?.message || t("loadFailed"));
    }
  }, [update, showToast]);

  // P0-7：切换当前活跃组合（替代原 window.location.reload() 的暴力刷新）
  const switchPortfolio = useCallback(async (portfolioId: number) => {
    update({ portfolioId });
    // 切换后重新加载 workbench 以反映新组合的持仓/规则/账户摘要
    try {
      await loadWorkbench();
    } catch (error: any) {
      showToast("error", error?.message || t("loadFailed"));
    }
  }, [update, loadWorkbench, showToast]);

  // P0-7：创建新组合
  const createPortfolio = useCallback(async (payload: {
    name: string;
    account_type: string;
    total_capital: number;
    investable_ratio: number;
    cash_reserve_ratio: number;
    currency?: string;
    is_default?: boolean;
  }): Promise<Portfolio | null> => {
    try {
      const newPortfolio = await api.createPortfolio(payload);
      // 刷新组合列表
      await loadPortfolios();
      showToast("success", t("portfolioCreated"));
      return newPortfolio as Portfolio;
    } catch (error: any) {
      showToast("error", error?.message || t("portfolioCreateFailed"));
      return null;
    }
  }, [loadPortfolios, showToast]);

  // P0-7：更新组合属性
  const updatePortfolio = useCallback(async (portfolioId: number, payload: {
    name?: string;
    total_capital?: number;
    investable_ratio?: number;
    cash_reserve_ratio?: number;
    currency?: string;
    is_default?: boolean;
  }): Promise<Portfolio | null> => {
    try {
      const updated = await api.updatePortfolio(portfolioId, payload);
      await loadPortfolios();
      // 若更新的是当前组合，重新加载 workbench
      if (portfolioId === portfolioIdRef.current) {
        await loadWorkbench();
      }
      showToast("success", t("portfolioUpdated"));
      return updated as Portfolio;
    } catch (error: any) {
      showToast("error", error?.message || t("portfolioUpdateFailed"));
      return null;
    }
  }, [loadPortfolios, loadWorkbench, showToast]);

  // P0-7：删除组合（默认组合和最后一个组合会被后端拒绝）
  const deletePortfolio = useCallback(async (portfolioId: number): Promise<boolean> => {
    try {
      await api.deletePortfolio(portfolioId);
      // loadPortfolios 会自动把 portfolioId 设为默认组合（或第一个）
      // 若删除的是当前组合，loadPortfolios 会切换到新默认；需补一次 workbench 刷新
      const wasCurrent = portfolioId === portfolioIdRef.current;
      await loadPortfolios();
      if (wasCurrent) {
        await loadWorkbench();
      }
      showToast("success", t("portfolioDeleted"));
      return true;
    } catch (error: any) {
      showToast("error", error?.message || t("portfolioDeleteFailed"));
      return false;
    }
  }, [loadPortfolios, loadWorkbench, showToast]);

  const loadSignalRuleConfig = useCallback(async () => {
    const pid = portfolioIdRef.current;
    if (!pid) return;
    try {
      const [presets, rule] = await Promise.all([
        api.getSignalRulePresets(),
        api.getSignalRule(pid),
      ]);
      update({ signalRulePresets: presets, signalRule: rule });
    } catch (error: any) {
      showToast("error", error?.message || t("ruleSaveFailed"));
    }
  }, [update, showToast]);

  const loadSignalRulePreview = useCallback(async () => {
    const pid = portfolioIdRef.current;
    if (!pid || !state.signalRule) return;
    if (!state.activeSymbolId) {
      setSignalRulePreview(null);
      return;
    }
    setSignalRulePreview(null);
    const payload = {
      rule_name: state.signalRule.rule_name,
      mode: state.signalRule.mode,
      quality_tolerance: state.signalRule.quality_tolerance,
      timing_tolerance: state.signalRule.timing_tolerance,
      min_sample_count: state.signalRule.min_sample_count,
      max_samples: state.signalRule.max_samples,
      same_region: state.signalRule.same_region,
      same_asset_type: state.signalRule.same_asset_type,
      same_stage: state.signalRule.same_stage,
      same_action: state.signalRule.same_action,
      symbol_id: state.activeSymbolId,
    };
    try {
      const result = await api.previewSignalRule(pid, payload);
      setSignalRulePreview(result);
    } catch (error: any) {
      showToast("error", error?.message || t("ruleSaveFailed"));
    }
  }, [state.signalRule, state.activeSymbolId, showToast]);

  const updateSignalRule = useCallback((partial: Partial<SignalRule>) => {
    if (!state.signalRule) return;
    const updated = { ...state.signalRule, ...partial };
    update({ signalRule: updated });
    // Schedule preview with debounce
    if (signalRulePreviewTimer.current) clearTimeout(signalRulePreviewTimer.current);
    signalRulePreviewTimer.current = setTimeout(() => {
      loadSignalRulePreview().catch((err: unknown) => console.warn("signalRulePreview failed", err));
    }, 350);
  }, [state.signalRule, update, loadSignalRulePreview]);

  const saveSignalRule = useCallback(async () => {
    const pid = portfolioIdRef.current;
    if (!pid || !state.signalRule) return;
    const payload = {
      rule_name: state.signalRule.rule_name,
      mode: state.signalRule.mode,
      quality_tolerance: state.signalRule.quality_tolerance,
      timing_tolerance: state.signalRule.timing_tolerance,
      min_sample_count: state.signalRule.min_sample_count,
      max_samples: state.signalRule.max_samples,
      same_region: state.signalRule.same_region,
      same_asset_type: state.signalRule.same_asset_type,
      same_stage: state.signalRule.same_stage,
      same_action: state.signalRule.same_action,
    };
    const saved = await api.saveSignalRule(pid, payload);
    update({ signalRule: saved });
    if (state.activeSymbolId) {
      await loadSymbolDetail(state.activeSymbolId, { force: true });
    }
    showToast("success", t("ruleSaved"));
  }, [state.signalRule, state.activeSymbolId, update, showToast, loadSymbolDetail]);

  const scheduleSignalRulePreviewInternal = useCallback((delay = 350) => {
    if (signalRulePreviewTimer.current) clearTimeout(signalRulePreviewTimer.current);
    signalRulePreviewTimer.current = setTimeout(() => {
      loadSignalRulePreview().catch((err: unknown) => console.warn("signalRulePreview failed", err));
    }, delay);
  }, [loadSignalRulePreview]);

  const loadDiscoveryScopeStatsInternal = useCallback(async () => {
    const scope = "cn-stock";
    try {
      const stats = await api.getDiscoveryScopeStats(scope);
      update({ discoveryScopeStats: stats });
    } catch (err) {
      console.warn("loadDiscoveryScopeStats failed", err);
    }
  }, [update]);

  const loadDiscoveryScopeStats = useCallback(async () => {
    await loadDiscoveryScopeStatsInternal();
  }, [loadDiscoveryScopeStatsInternal]);

  const fetchDiscoveryTasks = useCallback(async (scope?: string): Promise<DiscoveryTask | null> => {
    const queryScope = scope ?? discoveryScopeRef.current;
    try {
      const tasks = await api.getDiscoveryTasks(10, queryScope);
      const active = tasks.find((item) => ["queued", "running"].includes(item.status))
        ?? tasks.find((item) => item.status === "paused" && item.can_resume)
        ?? tasks[0] ?? null;
      update({ discoveryTask: active });
      return active;
    } catch (error: any) {
      showToast("error", error?.message || t("discoveryCommandFailed"));
      return null;
    }
  }, [update, showToast]);

  const startDiscoveryPolling = useCallback(() => {
    if (discoveryPollRef.current) return;
    setDiscoveryPolling(true);
    discoveryPollRef.current = setInterval(async () => {
      try {
        const tasks = await api.getDiscoveryTasks(10, discoveryScopeRef.current);
        const current = tasks.find((item) => ["queued", "running"].includes(item.status)) ?? tasks[0] ?? null;
        update({ discoveryTask: current });
        if (!current || ["done", "failed", "cancelled", "expired", "paused"].includes(current.status)) {
          if (discoveryPollRef.current) {
            clearInterval(discoveryPollRef.current);
            discoveryPollRef.current = null;
          }
          setDiscoveryPolling(false);
          if (current?.status === "done") {
            showToast("success", t("discoveryCompleted"));
            await loadWorkbench();
          } else if (current?.status === "expired") {
            showToast("error", t("taskExpiredRestart"));
          }
        }
      } catch (err) {
        console.warn("discovery task polling failed", err);
      }
    }, 2000);
  }, [update, showToast, loadWorkbench]);

  // 切换挖掘 scope：更新 ref，停止旧 scope 的轮询，拉取新 scope 的任务，
  // 若新 scope 有运行中的任务则重启轮询。
  const setDiscoveryScope = useCallback(async (scope: string) => {
    if (discoveryScopeRef.current === scope) return;
    discoveryScopeRef.current = scope;
    // 停止旧 scope 的轮询（避免继续拉取错误 scope 的任务状态）
    if (discoveryPollRef.current) {
      clearInterval(discoveryPollRef.current);
      discoveryPollRef.current = null;
    }
    setDiscoveryPolling(false);
    // 拉取新 scope 的最新任务
    const active = await fetchDiscoveryTasks(scope);
    // 若新 scope 有运行中的任务，重启轮询
    if (active && ["queued", "running"].includes(active.status)) {
      startDiscoveryPolling();
    }
  }, [fetchDiscoveryTasks, startDiscoveryPolling]);

  // WP-S-FIX.1: 能力门禁加载与查询
  const loadCapabilities = useCallback(async () => {
    update({ capabilitiesLoading: true });
    try {
      const resp = await api.getCapabilities();
      update({ capabilities: resp, capabilitiesLoading: false });
    } catch (error: any) {
      update({ capabilitiesLoading: false });
      // 静默失败，不弹 toast，避免启动时噪音；capabilities 保持 null 或旧值
      console.warn("loadCapabilities failed", error);
    }
  }, [update]);

  const getCapability = useCallback((key: string): CapabilityItem | undefined => {
    return state.capabilities?.capabilities.find((c) => c.key === key);
  }, [state.capabilities]);

  const isCapabilityBlocked = useCallback((key: string): boolean => {
    return getCapability(key)?.status === "blocked";
  }, [getCapability]);

  const runDiscoveryMining = useCallback(async (config?: {
    scope?: string;
    minScore?: number;
    dataMode?: string;
    batchSize?: number;
    delaySeconds?: number;
    maxWorkers?: number;
    warningDays?: number;
    validDays?: number;
    includeNews?: boolean;
  }) => {
    const isSync = config?.dataMode === "sync";
    const effectiveScope = config?.scope ?? "cn-stock";
    discoveryScopeRef.current = effectiveScope;

    const pausedTask = state.discoveryTask;
    const shouldResumePaused = pausedTask?.status === "paused"
      && !!pausedTask?.can_resume
      && pausedTask.scope === effectiveScope;

    try {
      if (shouldResumePaused && pausedTask?.id) {
        const resumed = await api.sendDiscoveryCommand(pausedTask.id, "resume");
        update({ discoveryTask: resumed });
        startDiscoveryPolling();
        showToast("success", t("discoveryResumed"));
        return;
      }

      const payload = {
        scope: effectiveScope,
        min_score: config?.minScore ?? 55,
        include_news: config?.includeNews ?? true,
        portfolio_id: state.portfolioId,
        portfolio_rule_id: state.workbench?.active_rule?.id ?? null,
        batch_size: config?.batchSize ?? 20,
        delay_seconds: config?.delaySeconds ?? 0.25,
        max_workers: config?.maxWorkers ?? 1,
        warning_days: config?.warningDays ?? 3,
        valid_days: config?.validDays ?? 5,
        news_limit: 30,
        refresh_universe: isSync,
        use_cached_bars_first: !isSync,
        use_cached_symbols_only: !isSync,
        global_mode: "library",
      };
      const task = await api.createDiscoveryTask(payload);
      update({ discoveryTask: task });
      await loadWorkbench();
      startDiscoveryPolling();
      showToast("success", isSync ? t("discoveryStartedSync") : t("discoveryStartedCached"));
      void loadCapabilities();
    } catch (error: any) {
      showToast("error", error?.message || t("discoveryCommandFailed"));
    }
  }, [state.discoveryTask, state.portfolioId, state.workbench, update, showToast, loadWorkbench, startDiscoveryPolling, t, loadCapabilities]);

  const sendDiscoveryTaskCommand = useCallback(async (command: string) => {
    const task = state.discoveryTask;
    if (!task?.id) return;
    try {
      const result = await api.sendDiscoveryCommand(task.id, command);
      update({ discoveryTask: result });
      if (command === "pause") showToast("success", t("discoveryPaused"));
      else if (command === "resume") {
        showToast("success", t("discoveryResumed"));
        startDiscoveryPolling();
      } else if (command === "cancel") showToast("success", t("discoveryCancelled"));
      else if (command === "retry") {
        showToast("success", t("discoveryRetried"));
        startDiscoveryPolling();
      }
    } catch (error: any) {
      showToast("error", error?.message || t("discoveryCommandFailed"));
    }
  }, [state.discoveryTask, update, showToast, startDiscoveryPolling]);

  const refreshDiscoveryTasks = useCallback(async () => {
    try {
      await fetchDiscoveryTasks();
      await loadWorkbench();
      showToast("success", t("discoveryRefreshDone"));
    } catch (error: any) {
      showToast("error", error?.message || t("discoveryCommandFailed"));
    }
  }, [fetchDiscoveryTasks, loadWorkbench, showToast]);

  const cleanupExpiredDiscoveryResults = useCallback(async () => {
    const result = await api.cleanupDiscoveryResults();
    await loadWorkbench();
    if (result.deleted > 0) {
      showToast("success", template("cleanupSuccess", { count: result.deleted }));
    } else {
      showToast("info", t("cleanupNone"));
    }
  }, [loadWorkbench, showToast]);

  const runNewsUpdate = useCallback(async () => {
    try {
      const candidateIds = state.workbench?.candidates?.map((item) => item.symbol_id) ?? [];
      let symbolIds = candidateIds;
      if (!symbolIds.length) {
        const symbols = await fetchVisibleSymbols();
        symbolIds = symbols.map((item) => item.id);
      }
      if (!symbolIds.length) return;
      const response = await api.updateNews({
        portfolio_id: state.portfolioId,
        scope: "symbols",
        symbol_ids: symbolIds,
        days: 7,
        include_macro: true,
        include_sector: true,
        include_symbol: true,
      });
      update({ newsSnapshot: response });
      showToast("success", template("newsSummary", { count: response.symbols_total }));
    } catch (error: any) {
      showToast("error", error?.message || t("newsFailed"));
    }
  }, [state.portfolioId, state.workbench, fetchVisibleSymbols, update, showToast]);

  const stopSyncPolling = useCallback(() => {
    if (syncPollRef.current) {
      clearInterval(syncPollRef.current);
      syncPollRef.current = null;
    }
    syncTaskIdRef.current = null;
    setSyncPolling(false);
  }, []);

  const startSyncPolling = useCallback((taskId: string) => {
    if (syncPollRef.current && syncTaskIdRef.current === taskId) return;
    stopSyncPolling();
    syncTaskIdRef.current = taskId;
    setSyncPolling(true);
    syncPollRef.current = setInterval(async () => {
      try {
        const currentTaskId = syncTaskIdRef.current;
        if (!currentTaskId) return;
        const task = await api.getMarketDataSyncTask(currentTaskId);
        update({ syncTask: task });
        if (["done", "failed", "cancelled"].includes(task.status)) {
          stopSyncPolling();
          if (task.status === "done" && task.result) {
            showToast("success", template("syncSummary", {
              ok: task.result.ok_count ?? task.ok_count,
              total: task.result.symbols_total ?? task.total,
              failed: task.result.failed_count ?? task.failed_count,
            }));
            await loadWorkbench();
          } else if (task.status === "failed") {
            showToast("error", task.message || t("syncFailed"));
          } else if (task.status === "cancelled") {
            showToast("info", t("syncCancelled"));
          }
          update({ syncTask: null });
        }
      } catch (err) {
        console.warn("sync task polling failed", err);
      }
    }, 2000);
  }, [update, showToast, loadWorkbench, stopSyncPolling]);

  const runSync = useCallback(async () => {
    try {
      const symbols = await fetchVisibleSymbols();
      const symbolIds = symbols.map((s) => s.id);
      if (!symbolIds.length) return;
      const task = await api.createMarketDataSyncTask({
        scope: "symbols",
        symbol_ids: symbolIds,
        asset_types: [...new Set(symbols.map((item) => item.asset_type))],
        adjust: "qfq",
        auto_scan: true,
        portfolio_id: state.portfolioId,
        portfolio_rule_id: state.workbench?.active_rule?.id ?? null,
      });
      update({ syncTask: task });
      startSyncPolling(task.id);
      showToast("info", t("syncStarted"));
      void loadCapabilities();
    } catch (error: any) {
      showToast("error", error?.message || t("syncFailed"));
    }
  }, [state.portfolioId, state.workbench, fetchVisibleSymbols, showToast, startSyncPolling, update, loadCapabilities]);

  const cancelSync = useCallback(async () => {
    const taskId = syncTaskIdRef.current || state.syncTask?.id;
    if (!taskId) return;
    try {
      await api.cancelMarketDataSyncTask(taskId);
      stopSyncPolling();
      update({ syncTask: null });
      showToast("info", t("syncCancelled"));
    } catch (error: any) {
      showToast("error", error?.message || t("syncFailed"));
    }
  }, [state.syncTask, stopSyncPolling, showToast, update]);

  const runScan = useCallback(async () => {
    try {
      const symbols = await fetchVisibleSymbols();
      const symbolIds = symbols.map((s) => s.id);
      if (!symbolIds.length) return;
      await api.createScanRun({
        portfolio_id: state.portfolioId,
        portfolio_rule_id: state.workbench?.active_rule?.id ?? null,
        run_name: "manual-workbench-scan",
        scope_snapshot: {
          symbol_ids: symbolIds,
          asset_types: [...new Set(symbols.map((item) => item.asset_type))],
          markets: [...new Set(symbols.map((item) => item.market))],
        },
      });
      await loadWorkbench();
      showToast("success", template("scanSummary", { count: state.workbench?.latest_scan?.executable_count ?? 0 }));
      void loadCapabilities();
    } catch (error: any) {
      showToast("error", error?.message || t("scanFailed"));
    }
  }, [state.portfolioId, state.workbench, fetchVisibleSymbols, loadWorkbench, showToast, loadCapabilities]);

  const addSymbolToWatchlist = useCallback(async (watchlistId: number, symbolId: number) => {
    try {
      await api.addWatchlistItem(watchlistId, symbolId);
    } catch (error: any) {
      if (!String(error.message).includes("already")) throw error;
    }
    update({ watchlistItems: { ...state.watchlistItems, [watchlistId]: undefined as any }, activeWatchlistId: watchlistId });
  }, [state.watchlistItems, update]);

  const addSymbolToPrimaryWatchlist = useCallback(async (symbolId: number) => {
    const watchlist = state.workbench?.watchlists?.[0];
    if (!watchlist) return;
    await addSymbolToWatchlist(watchlist.id, symbolId);
  }, [state.workbench, addSymbolToWatchlist]);

  const addSymbolFromInput = useCallback(async (code: string) => {
    const payload = inferSymbolPayload(code);
    let symbol = await (async () => {
      const symbols = await api.getSymbols(payload.symbol);
      return symbols.find((item) => item.symbol.toUpperCase() === payload.symbol.toUpperCase()) ?? null;
    })();
    if (!symbol) {
      symbol = await api.createSymbol(payload);
    }
    await addSymbolToPrimaryWatchlist(symbol.id);
    if (state.marketGroup !== "all" && symbol.region && state.marketGroup !== symbol.region) {
      update({ marketGroup: symbol.region });
    }
    update({ activeSymbolId: symbol.id, symbolDirectory: {} });
    await loadWorkbench();
    await loadSymbolDetail(symbol.id, { focus: true });
    showToast("success", template("symbolAdded", { symbol: symbol.symbol }));
  }, [state.marketGroup, addSymbolToPrimaryWatchlist, loadWorkbench, loadSymbolDetail, showToast, update]);

  const generateTradeSetup = useCallback(async (overrides?: TradeSetupOverrides) => {
    if (!state.activeSymbolId || !state.detail?.latest_score) return;
    try {
      await api.generateTradeSetup({
        portfolio_id: state.portfolioId,
        symbol_id: state.activeSymbolId,
        score_id: state.detail.latest_score.id,
        ...(overrides ? { overrides } : {}),
      });
      await loadSymbolDetail(state.activeSymbolId, { force: true });
      showToast("success", template("planSummary"));
    } catch (error: any) {
      showToast("error", error?.message || t("planFailed"));
      throw error;
    }
  }, [state.portfolioId, state.activeSymbolId, state.detail, loadSymbolDetail, showToast]);

  const submitSimOrder = useCallback(async (side: "buy" | "sell") => {
    if (!state.portfolioId || !state.activeSymbolId || !state.detail) return;
    let quantity = Number(state.simQuantity);
    let price = Number(state.simPrice);
    if (!(quantity > 0)) {
      quantity = side === "sell" ? computeDefaultSellQuantity(state.detail) : computeSuggestedBuyQuantity(state.detail);
    }
    if (!(price > 0)) {
      price = computeSuggestedPrice(state.detail);
    }
    if (!(quantity > 0) || !(price > 0)) {
      throw new Error(t("invalidOrderInput"));
    }
    const totalAmount = quantity * price;
    const confirmKey = side === "buy" ? "confirmBuy" : "confirmSell";
    const confirmMsg = template(confirmKey, {
      symbol: state.detail.symbol.symbol,
      quantity,
      price: fmtScore(price),
      cost: totalAmount,
      proceeds: totalAmount,
    });
    if (!window.confirm(confirmMsg)) return;
    const result = await api.submitSimOrder(state.portfolioId, {
      symbol_id: state.activeSymbolId,
      side,
      quantity,
      price,
      order_type: "market",
    });
    await loadWorkbench();
    showToast("success", template("orderSummary", {
      side: side === "buy" ? t("buySide") : t("sellSide"),
      symbol: state.detail.symbol.symbol,
      quantity: result.trade.quantity,
      price: fmtScore(result.trade.price),
    }));
  }, [state.portfolioId, state.activeSymbolId, state.detail, state.simQuantity, state.simPrice, loadWorkbench, showToast]);

  const removeDetailFromDock = useCallback((symbolId: number) => {
    const newCache = { ...detailCacheRef.current };
    delete newCache[symbolId];
    const newOrder = state.detailOrder.filter((item) => item !== symbolId);
    if (state.activeSymbolId === symbolId) {
      const nextId = newOrder[0] ?? null;
      update({
        detailCache: newCache,
        detailOrder: newOrder,
        activeSymbolId: nextId,
        detail: nextId ? newCache[nextId] : null,
      });
    } else {
      update({ detailCache: newCache, detailOrder: newOrder });
    }
  }, [state.detailOrder, state.activeSymbolId, update]);

  // Track active requests for a global loading indicator
  useEffect(() => {
    return onRequestChange((count) => {
      update({ globalLoading: count > 0 });
    });
  }, [update]);

  // Resume sync polling on mount (only once)
  const syncResumeDone = useRef(false);
  useEffect(() => {
    if (syncResumeDone.current) return;
    syncResumeDone.current = true;
    (async () => {
      try {
        const tasks = await api.listMarketDataSyncTasks(1);
        const latestTask = tasks[0];
        if (latestTask && ["queued", "running"].includes(latestTask.status)) {
          update({ syncTask: latestTask });
          startSyncPolling(latestTask.id);
        }
      } catch (err) {
        console.warn("resume sync task failed", err);
      }
    })();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Bootstrap
  useEffect(() => {
    (async () => {
      setLocale("zh-CN");
      await loadPortfolios();
      await loadSignalRuleConfig();
      const activeTask = await fetchDiscoveryTasks();
      await loadWorkbench();
      await loadCapabilities(); // WP-S-FIX.1: 启动时加载能力状态
      if (activeTask && ["queued", "running"].includes(activeTask.status)) {
        startDiscoveryPolling();
      }
    })().catch((error) => showToast("error", error.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (discoveryPollRef.current) clearInterval(discoveryPollRef.current);
      stopSyncPolling();
      if (signalRulePreviewTimer.current) clearTimeout(signalRulePreviewTimer.current);
      if (signalSampleTimer.current) clearTimeout(signalSampleTimer.current);
    };
  }, [stopSyncPolling]);

  const value: AppContextValue = {
    ...state,
    signalRulePreview,
    setSignalRulePreview,
    discoveryPolling,
    syncPolling,
    setLocaleValue,
    setMarketGroup,
    setActiveTab,
    setActiveSubTab,
    setChartTimeframe,
    setChartWindowSize,
    setChartRange,
    setChartExpanded,
    setActiveSymbolId,
    setFuturePlanScenario,
    setFuturePlanCustom,
    setSimQuantity,
    setSimPrice,
    setCandidateSearch,
    setSignalSampleLimit,
    showToast,
    loadPortfolios,
    // P0-7：组合管理方法
    switchPortfolio,
    createPortfolio,
    updatePortfolio,
    deletePortfolio,
    loadWorkbench,
    loadSymbolDetail,
    loadSignalRuleConfig,
    updateSignalRule,
    saveSignalRule,
    loadSignalRulePreview,
    fetchDiscoveryTasks,
    setDiscoveryScope,
    runDiscoveryMining,
    sendDiscoveryTaskCommand,
    refreshDiscoveryTasks,
    cleanupExpiredDiscoveryResults,
    loadDiscoveryScopeStats,
    runNewsUpdate,
    runSync,
    cancelSync,
    runScan,
    addSymbolFromInput,
    generateTradeSetup,
    submitSimOrder,
    fetchWatchlistItems,
    addSymbolToWatchlist,
    addSymbolToPrimaryWatchlist,
    refreshPrimaryWatchlistMembership,
    fetchVisibleSymbols,
    syncOrderForm,
    removeDetailFromDock,
    loadCapabilities,
    getCapability,
    isCapabilityBlocked,
  };

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}



