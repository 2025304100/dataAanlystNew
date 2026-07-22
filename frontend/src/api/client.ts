import { t } from "../i18n";
import type { CapabilitiesResponse, CustomIndicatorPreviewRead, SignalRule, SignalRulePreviewResult } from "../types";
import type { AttributionReport, Review } from "../types";
import type { SymbolRelationships } from "../types/symbolRelationships";
import type {
  AISession,
  AIMessage,
  AIProfile,
  AIProfileTestResult,
  AIProfileUsage,
  AIHealth,
  AIResponse,
} from "../types";

// 通用 API 响应类型：默认 unknown，调用方可显式指定具体类型
type ApiResponse<T = unknown> = T;

let activeRequests = 0;
const requestListeners: Array<(count: number) => void> = [];

export function onRequestChange(listener: (count: number) => void) {
  requestListeners.push(listener);
  return () => {
    const idx = requestListeners.indexOf(listener);
    if (idx >= 0) requestListeners.splice(idx, 1);
  };
}

function notifyRequestChange() {
  requestListeners.forEach((fn) => fn(activeRequests));
}

export async function requestJson<T = unknown>(url: string, options: RequestInit & { timeoutMs?: number } = {}): Promise<T> {
  activeRequests++;
  notifyRequestChange();
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? 20000;
  // 标记位：区分"超时触发 abort"与"调用方主动取消"
  let timedOut = false;
  const timeoutId = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  // 移除自定义 timeoutMs 字段，保留标准 RequestInit 字段
  const { timeoutMs: _omit, ...requestOptions } = options;
  try {
    const response = await fetch(url, { ...requestOptions, signal: requestOptions.signal ?? controller.signal });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload.detail || payload.message || response.statusText;
      const msg = typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail);
      const error = new Error(msg) as Error & { detail?: unknown };
      error.detail = detail;
      throw error;
    }
    return payload as T;
  } catch (error: unknown) {
    // 收窄 unknown 类型，仅对 Error 实例判断 name 属性
    if (error instanceof Error && error.name === "AbortError") {
      // 仅超时（timedOut=true）时抛出超时错误；调用方主动取消则静默返回 rejected
      if (timedOut) {
        throw new Error(t("requestTimeout"));
      }
      // 调用方主动取消：抛出 AbortError 让调用方自行判断
      throw error;
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    activeRequests = Math.max(0, activeRequests - 1);
    notifyRequestChange();
  }
}

const API = "/api/v1";

export const SYSTEM_HEALTH_URL = API + "/system/data-health";

// TODO: 待后续类型强化——下方 requestJson<any>/requestJson<any[]> 调用保留 any 是为了
// 兼容各调用方对返回值字段的直接访问（如 .id / .symbol 等），避免大面积级联报错。
export const api = {
  // System
  getDataHealth: () => requestJson<any>(SYSTEM_HEALTH_URL),
  getSymbolDataHealth: (symbolId: number) => requestJson<any>(`${API}/system/data-health/symbols/${symbolId}`),
  getCapabilities: () => requestJson<CapabilitiesResponse>(`${API}/system/capabilities`),

  // Dynamic factor engine
  getFactorOverview: () =>
    requestJson<FactorOverview>(`${API}/factors/overview`),
  updateFactorSystemConfig: (featureEnabled: boolean) =>
    requestJson<FactorSystemConfig>(`${API}/factors/config`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ feature_enabled: featureEnabled, actor: "local_user" }),
    }),
  initializeFactorWarehouse: () =>
    requestJson<FactorOverview>(`${API}/factors/warehouse/initialize`, { method: "POST" }),
  getFactorModels: (status?: string, limit: number = 20) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (status) params.set("status", status);
    return requestJson<FactorModelList>(`${API}/factor-models?${params.toString()}`);
  },
  getFactorModel: (modelRunId: string) =>
    requestJson<FactorModelRun>(`${API}/factor-models/${encodeURIComponent(modelRunId)}`),
  activateFactorModel: (modelRunId: string, mode: "shadow" | "ridge", note?: string) =>
    requestJson<FactorRuntime>(`${API}/factor-models/${encodeURIComponent(modelRunId)}/activate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, actor: "local_user", note }),
    }),
  fallbackFactorModel: (reason: string) =>
    requestJson<FactorRuntime>(`${API}/factor-models/fallback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actor: "local_user", reason }),
    }),
  createFactorPipelineTask: (payload: FactorPipelineCreate) =>
    requestJson<FactorPipelineTask>(`${API}/factor-pipeline/tasks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  listFactorPipelineTasks: (limit: number = 20) =>
    requestJson<FactorPipelineTask[]>(`${API}/factor-pipeline/tasks?limit=${limit}`),
  getFactorPipelineTask: (taskId: string) =>
    requestJson<FactorPipelineTask>(`${API}/factor-pipeline/tasks/${encodeURIComponent(taskId)}`),
  cancelFactorPipelineTask: (taskId: string) =>
    requestJson<FactorPipelineTask>(`${API}/factor-pipeline/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" }),
  getFactorPipelineEta: (trainModel: boolean = true, fullRefresh: boolean = false) => {
    const params = new URLSearchParams({
      train_model: String(trainModel),
      full_refresh: String(fullRefresh),
    });
    return requestJson<FactorPipelineEta>(`${API}/factor-pipeline/eta?${params.toString()}`);
  },
  getSymbolFactorExplanation: (symbolId: number, options: { tradeDate?: string; modelRunId?: string } = {}) => {
    const params = new URLSearchParams();
    if (options.tradeDate) params.set("trade_date", options.tradeDate);
    if (options.modelRunId) params.set("model_run_id", options.modelRunId);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return requestJson<SymbolFactorExplanation>(`${API}/factors/symbols/${symbolId}/explanation${suffix}`);
  },

  // Portfolios
  getPortfolios: () => requestJson<any[]>(`${API}/portfolios`),
  // P0-6：组合 CRUD 补全
  createPortfolio: (payload: unknown) =>
    requestJson<any>(`${API}/portfolios`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  updatePortfolio: (portfolioId: number, payload: unknown) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  deletePortfolio: (portfolioId: number) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}`, { method: "DELETE" }),
  // P0-10/P1-1：组合净值快照 + 绩效指标
  getPortfolioEquitySnapshots: (portfolioId: number, params: { startDate?: string; endDate?: string; limit?: number } = {}) => {
    const sp = new URLSearchParams();
    if (params.startDate) sp.set("start_date", params.startDate);
    if (params.endDate) sp.set("end_date", params.endDate);
    sp.set("limit", String(params.limit ?? 400));
    return requestJson<any[]>(`${API}/portfolios/${portfolioId}/equity-snapshots?${sp.toString()}`);
  },
  getPortfolioPerformance: (portfolioId: number, params: { startDate?: string; endDate?: string; snapshotLimit?: number } = {}) => {
    const sp = new URLSearchParams();
    if (params.startDate) sp.set("start_date", params.startDate);
    if (params.endDate) sp.set("end_date", params.endDate);
    sp.set("snapshot_limit", String(params.snapshotLimit ?? 1000));
    return requestJson<any>(`${API}/portfolios/${portfolioId}/performance?${sp.toString()}`);
  },
  // WP8.3：绩效归因报告
  getAttributionReport: (portfolioId: number, startDate: string, endDate: string, dimensions?: string) => {
    const sp = new URLSearchParams({ start_date: startDate, end_date: endDate });
    if (dimensions) sp.set("dimensions", dimensions);
    return requestJson<AttributionReport>(`${API}/portfolios/${portfolioId}/attribution?${sp.toString()}`);
  },
  // WP8.3：复盘记录列表
  getReviews: (portfolioId: number) =>
    requestJson<Review[]>(`${API}/portfolios/${portfolioId}/reviews`),
  // WP8.3：创建复盘记录（备注 + 自动附归因快照）
  createReview: (portfolioId: number, payload: { note: string; attribution_snapshot?: string }) =>
    requestJson<Review>(`${API}/portfolios/${portfolioId}/reviews`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  // P2-3：自动交易执行（dry_run 只返回计划，dry_run=False 实际下单）
  executeAutoTrade: (portfolioId: number, payload: { dry_run: boolean; buy_candidate_limit?: number }) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}/auto-trade/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs: 60000,
    }),
  // WP6.6：自动交易双跑与成员级状态
  getAutoTradeDryRunDiff: (portfolioId: number) =>
    requestJson<{
      portfolio_id: number;
      old_set: { buys: any[]; sells: any[]; rejected: any[] };
      new_set: { buys: any[]; sells: any[]; rejected: any[] };
      diffs: Array<{
        symbol_id: number;
        side: string;
        old_action: string | null;
        new_action: string | null;
        reason: string;
        detail: string;
      }>;
    }>(`${API}/portfolios/${portfolioId}/auto-trade/dry-run-diff`, { timeoutMs: 60000 }),
  getAutoTradeMemberSourceStatus: (portfolioId: number) =>
    requestJson<{
      portfolio_id: number;
      enabled: boolean;
      env_var_name: string;
      env_flag: string;
      whitelist_match: boolean;
      blacklist_match: boolean;
      whitelist: number[];
      blacklist: number[];
    }>(`${API}/portfolios/${portfolioId}/auto-trade/member-source-status`),
  rollbackAutoTradeToOldSource: (portfolioId: number) =>
    requestJson<{ ok: boolean; portfolio_id: number; message: string }>(
      `${API}/portfolios/${portfolioId}/auto-trade/rollback-to-old-source`,
      { method: "POST" },
    ),
  getAutoTradeMemberStatus: (portfolioId: number) =>
    requestJson<{
      portfolio_id: number;
      total: number;
      members: Array<{
        member_id: number;
        symbol_id: number;
        symbol: string | null;
        status: string;
        execution_mode: string;
        source_type: string;
        manual_lock: boolean;
        has_position: boolean;
        position_quantity: number;
        latest_order: {
          order_id: number;
          side: string;
          status: string;
          created_at: string | null;
          source_type: string | null;
          signal_id: number | null;
          execution_mode: string | null;
          client_order_key: string | null;
          rejection_code: string | null;
          rejection_detail: string | null;
        } | null;
        data_health: {
          healthy: boolean;
          reason: string;
          kline_latest_at: string | null;
          score_latest_at: string | null;
          rule_version_id: number | null;
        };
        risk_blocked: boolean;
        data_expired: boolean;
      }>;
    }>(`${API}/portfolios/${portfolioId}/auto-trade/member-status`),
  getWorkbench: (portfolioId: number, marketGroup: string) =>
    requestJson<any>(`${API}/dashboard/workbench?portfolio_id=${portfolioId}&market_group=${marketGroup}`),
  getSymbolDetail: (portfolioId: number, symbolId: number, sampleLimit?: number, barLimit?: number) => {
    const params = new URLSearchParams({ portfolio_id: String(portfolioId), symbol_id: String(symbolId) });
    if (sampleLimit) params.set("sample_limit", String(sampleLimit));
    if (barLimit) params.set("bar_limit", String(barLimit));
    return requestJson<any>(`${API}/dashboard/symbol-detail?${params.toString()}`);
  },

  // Lazy load more bars for a symbol
  getBars: (symbolId: number, limit: number = 250) => {
    return requestJson<any[]>(`${API}/market-data/bars/${symbolId}?limit=${limit}`);
  },

  // Symbols
  getSymbols: (keyword?: string, options: { page?: number; pageSize?: number; assetType?: string; market?: string } = {}) => {
    const params = new URLSearchParams({
      page: String(options.page ?? 1),
      page_size: String(options.pageSize ?? 200),
    });
    if (keyword) params.set("keyword", keyword);
    if (options.assetType) params.set("asset_type", options.assetType);
    if (options.market) params.set("market", options.market);
    return requestJson<any[]>(`${API}/symbols?${params.toString()}`);
  },
  getAllSymbols: async (keyword?: string, options: { assetType?: string; market?: string } = {}) => {
    const pageSize = 200;
    const rows: any[] = [];
    for (let page = 1; page <= 100; page += 1) {
      const batch = await api.getSymbols(keyword, { ...options, page, pageSize });
      rows.push(...batch);
      if (batch.length < pageSize) break;
    }
    return rows;
  },
  createSymbol: (payload: unknown) =>
    requestJson<any>(`${API}/symbols`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

  // WP1.5：标的统一关联状态（候选/观察/组合成员/持仓/告警）
  getSymbolRelationships: (symbolId: number) =>
    requestJson<SymbolRelationships>(`${API}/symbols/${symbolId}/relationships`),

  // Watchlists
  getWatchlistItems: (watchlistId: number) => requestJson<any[]>(`${API}/watchlists/${watchlistId}/items`),
  addWatchlistItem: (watchlistId: number, symbolId: number) =>
    requestJson(`${API}/watchlists/${watchlistId}/items`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol_id: symbolId }) }),

  // Market data
  syncMarketData: (payload: unknown) =>
    requestJson(`${API}/market-data/update`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  repairSymbolMarketData: (symbolId: number, payload: unknown = {}) =>
    requestJson(`${API}/market-data/symbols/${symbolId}/repair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs: 60000,
    }),

  // Async sync tasks (heartbeat polling)
  createMarketDataSyncTask: (payload: unknown) =>
    requestJson<any>(`${API}/market-data/sync-tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  listMarketDataSyncTasks: (limit: number = 10) =>
    requestJson<any[]>(`${API}/market-data/sync-tasks?limit=${limit}`),
  getMarketDataSyncTask: (taskId: string) =>
    requestJson<any>(`${API}/market-data/sync-tasks/${taskId}`),
  cancelMarketDataSyncTask: (taskId: string) =>
    requestJson<any>(`${API}/market-data/sync-tasks/${taskId}/cancel`, { method: "POST" }),

  startHistoryInitialization: (payload: {
    preset: string;
    adjust?: string;
    asset_types?: string[];
    symbol_ids?: number[];
    repair_mode?: "both" | "bars" | "scores";
    symbol_source?: "all" | "watchlist" | "positions" | "scored" | "candidates" | "cn-stock" | "cn-etf";
    auto_scan?: boolean;
    portfolio_id?: number | null;
    watchlist_id?: number | null;
  }) =>
    requestJson<any>(`${API}/market-data/initialize-history`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs: 30000,
    }),
  getHistoryInitializationStatus: () =>
    requestJson<any>(`${API}/market-data/initialize-history/status`),
  cancelHistoryInitialization: () =>
    requestJson<any>(`${API}/market-data/initialize-history/cancel`, {
      method: "POST",
    }),
  retryHistoryInitializationFailed: (taskId: string) =>
    requestJson<any>(`${API}/market-data/initialize-history/retry-failed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: taskId }),
    }),

  cleanupHistoryRecords: (keep: number) =>
    requestJson<any>(`${API}/market-data/initialize-history/cleanup?keep=${keep}`, {
      method: "POST",
    }),

  // Scans
  createScanRun: (payload: unknown) =>
    requestJson(`${API}/scans/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeoutMs: 60000 }),

  // Scores
  calculateScores: (payload: unknown) =>
    requestJson(`${API}/scores/calculate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeoutMs: 120000 }),

  // Scoring configs (P0: 轻量自定义评分配置)
  listScoringConfigs: (assetType: "stock" | "etf") =>
    requestJson<any[]>(`${API}/settings/scoring-configs?asset_type=${assetType}`),
  getActiveScoringConfig: (assetType: "stock" | "etf") =>
    requestJson<any>(`${API}/settings/scoring-configs/active?asset_type=${assetType}`),
  createScoringConfig: (payload: unknown) =>
    requestJson<any>(`${API}/settings/scoring-configs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  updateScoringConfig: (id: number, payload: unknown) =>
    requestJson<any>(`${API}/settings/scoring-configs/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  activateScoringConfig: (id: number) =>
    requestJson<any>(`${API}/settings/scoring-configs/${id}/activate`, { method: "POST" }),
  duplicateScoringConfig: (id: number, payload: unknown) =>
    requestJson<any>(`${API}/settings/scoring-configs/${id}/duplicate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  listScoringConfigVersions: (id: number) =>
    requestJson<any[]>(`${API}/settings/scoring-configs/${id}/versions`),
  deleteScoringConfig: (id: number) =>
    requestJson<{ ok: boolean; deleted: number }>(`${API}/settings/scoring-configs/${id}`, { method: "DELETE" }),

  // Trade setups
  generateTradeSetup: (payload: unknown) =>
    requestJson(`${API}/trade-setups/generate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeoutMs: 60000 }),
  saveTradeSetupTranches: (setupId: number, payload: unknown) =>
    requestJson(`${API}/trade-setups/${setupId}/tranches`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

  // Signal rules
  getSignalRulePresets: () => requestJson<any[]>(`${API}/signal-rules/presets`),
  getSignalRule: (portfolioId: number) => requestJson<SignalRule>(`${API}/portfolios/${portfolioId}/signal-rule`),
  saveSignalRule: (portfolioId: number, payload: unknown) =>
    requestJson<SignalRule>(`${API}/portfolios/${portfolioId}/signal-rule`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  previewSignalRule: (portfolioId: number, payload: unknown) =>
    requestJson<SignalRulePreviewResult>(`${API}/portfolios/${portfolioId}/signal-rule/preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

  // Sim accounts
  submitSimOrder: (portfolioId: number, payload: unknown) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}/sim-orders`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

  // Positions, allocation & portfolio rules (P2 holdings management)
  getPositions: (portfolioId: number) =>
    requestJson<any[]>(`${API}/portfolios/${portfolioId}/positions`),
  upsertPosition: (portfolioId: number, payload: unknown) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}/positions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  deletePosition: (portfolioId: number, symbolId: number) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}/positions/${symbolId}`, { method: "DELETE" }),
  upsertPortfolioRule: (portfolioId: number, payload: unknown) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}/rules`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  getAllocation: (portfolioId: number) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}/allocation`),

  // News
  updateNews: (payload: unknown) =>
    requestJson<any>(`${API}/news/update`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  getLatestNews: (portfolioId: number, symbolIds: number[], days = 7, limit = 20) => {
    const params = new URLSearchParams({ portfolio_id: String(portfolioId), days: String(days), limit: String(limit) });
    symbolIds.forEach((id) => params.append("symbol_ids", String(id)));
    return requestJson<any>(`${API}/news/latest?${params.toString()}`);
  },

  // Macro
  getMacroOverview: (region = "all") =>
    requestJson<any>(`${API}/macro/overview?region=${encodeURIComponent(region)}`),
  updateMacroData: (payload: unknown) =>
    requestJson<any>(`${API}/macro/update`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeoutMs: 180000 }),
  startMacroUpdateTask: (payload: unknown) =>
    requestJson<any>(`${API}/macro/update-tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  getLatestMacroUpdateTask: () =>
    requestJson<any | null>(`${API}/macro/update-tasks/latest`),
  getMacroUpdateTask: (taskId: string) =>
    requestJson<any>(`${API}/macro/update-tasks/${taskId}`),
  cancelMacroUpdateTask: (taskId: string) =>
    requestJson<any>(`${API}/macro/update-tasks/${taskId}/cancel`, { method: "POST" }),
  getMacroIndicatorHistory: (region: string, indicatorKey: string, limit = 60) =>
    requestJson<any[]>(`${API}/macro/indicators/${encodeURIComponent(indicatorKey)}/history?region=${encodeURIComponent(region)}&limit=${limit}`),

  // Discovery
  getDiscoveryTasks: (limit = 10, scope?: string) => {
    const params = new URLSearchParams();
    params.set("limit", String(limit));
    if (scope) params.set("scope", scope);
    return requestJson<any[]>(`${API}/discovery/tasks?${params.toString()}`);
  },
  createDiscoveryTask: (payload: unknown) =>
    requestJson<any>(`${API}/discovery/tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  sendDiscoveryCommand: (taskId: string | number, command: string) =>
    requestJson<any>(`${API}/discovery/tasks/${taskId}/${command}`, { method: "POST" }),
  getDiscoveryScopeStats: (scope: string) => requestJson<any>(`${API}/discovery/scopes/${encodeURIComponent(scope)}/stats`),
  updateDiscoveryResult: (resultId: number, payload: unknown) =>
    requestJson(`${API}/discovery/results/${resultId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  refreshDiscoveryResult: (resultId: number) =>
    requestJson(`${API}/discovery/results/${resultId}/refresh`, { method: "POST" }),
  evaluateDiscoveryIndicators: (payload: { scan_result_ids: number[]; indicator_keys: string[] }) =>
    requestJson<any[]>(`${API}/discovery/indicators/evaluate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  cleanupDiscoveryResults: () =>
    requestJson<{ deleted: number; skipped_frozen: number }>(`${API}/discovery/results/cleanup`, { method: "POST" }),
  getLatestDiscoveryCandidates: (minScore = 0, limit = 50, scope?: string) =>
    requestJson<any[]>(`${API}/discovery/latest-candidates?min_score=${minScore}&limit=${limit}${scope ? `&scope=${scope}` : ""}`),

  // P1：挖掘候选手动晋升 API
  listDiscoveryCandidates: (params: { scanRunId?: number; isPromoted?: 0 | 1; limit?: number; offset?: number } = {}) => {
    const sp = new URLSearchParams();
    if (params.scanRunId != null) sp.set("scan_run_id", String(params.scanRunId));
    if (params.isPromoted != null) sp.set("is_promoted", String(params.isPromoted));
    sp.set("limit", String(params.limit ?? 100));
    sp.set("offset", String(params.offset ?? 0));
    return requestJson<any[]>(`${API}/discovery/candidates?${sp.toString()}`);
  },
  promoteDiscoveryCandidate: (candidateId: number) =>
    requestJson<{ ok: boolean; candidate_id: number; symbol: string; already_promoted: boolean }>(
      `${API}/discovery/candidates/${candidateId}/promote`,
      { method: "POST" }
    ),
  unpromoteDiscoveryCandidate: (candidateId: number) =>
    requestJson<{ ok: boolean; candidate_id: number; symbol: string }>(
      `${API}/discovery/candidates/${candidateId}/unpromote`,
      { method: "POST" }
    ),
  promoteDiscoveryCandidatesBatch: (candidateIds: number[]) =>
    requestJson<{ ok: boolean; promoted_count: number; already_promoted_count: number; not_found_ids: number[] }>(
      `${API}/discovery/candidates/promote-batch`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_ids: candidateIds }),
      }
    ),

  // Universe 基础数据层（全市场标的 + K线初始化同步）
  // P0.6：scopes 支持分 scope 独立初始化，None=全部
  startUniverseInit: (maxWorkers = 5, historyDays = 365, scopes: string[] | null = null, syncLimit = 0) =>
    requestJson<any>(`${API}/universe/initialize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_workers: maxWorkers, history_days: historyDays, scopes, sync_limit: syncLimit }),
    }),
  getUniverseInitStatus: () => requestJson<any | null>(`${API}/universe/initialize/status`),
  cancelUniverseInit: () =>
    requestJson<any>(`${API}/universe/initialize/cancel`, { method: "POST" }),
  retryUniverseInit: (maxWorkers = 5, historyDays = 365, scopes: string[] | null = null, syncLimit = 0) =>
    requestJson<any>(`${API}/universe/initialize/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_workers: maxWorkers, history_days: historyDays, scopes, sync_limit: syncLimit }),
    }),
  // 智能同步：自动处理初始化 + 历史补缺 + 近期增量
  startUniverseSmartSync: (maxWorkers = 5, historyDays = 365, scopes: string[] | null = null, syncLimit = 0) =>
    requestJson<any>(`${API}/universe/smart-sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_workers: maxWorkers, history_days: historyDays, scopes, sync_limit: syncLimit }),
    }),
  getUniverseSmartSyncStatus: () => requestJson<any | null>(`${API}/universe/smart-sync/status`),
  cancelUniverseSmartSync: () =>
    requestJson<any>(`${API}/universe/smart-sync/cancel`, { method: "POST" }),
  // P2：增量同步（每日定时 + 手动触发）
  // 区间修复：按 chunk 分片重拉近期范围，适合修复中间缺口
  startUniverseRangeRepair: (maxWorkers = 5, historyDays = 365, chunkDays = 90, scopes: string[] | null = null, syncLimit = 0) =>
    requestJson<any>(`${API}/universe/range-repair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_workers: maxWorkers, history_days: historyDays, chunk_days: chunkDays, scopes, sync_limit: syncLimit }),
    }),
  getUniverseRangeRepairStatus: () => requestJson<any | null>(`${API}/universe/range-repair/status`),
  cancelUniverseRangeRepair: () =>
    requestJson<any>(`${API}/universe/range-repair/cancel`, { method: "POST" }),
  startUniverseIncrementalSync: (maxWorkers = 5) =>
    requestJson<any>(`${API}/universe/incremental-sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_workers: maxWorkers }),
    }),
  getUniverseIncrementalSyncStatus: () => requestJson<any | null>(`${API}/universe/incremental-sync/status`),
  cancelUniverseIncrementalSync: () =>
    requestJson<any>(`${API}/universe/incremental-sync/cancel`, { method: "POST" }),
  // 历史回补（对已同步标的强制按新 history_days 重新拉取K线）
  startUniverseBackfill: (maxWorkers = 5, historyDays = 365, scopes: string[] | null = null, syncLimit = 0) =>
    requestJson<any>(`${API}/universe/backfill`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_workers: maxWorkers, history_days: historyDays, scopes, sync_limit: syncLimit }),
    }),
  getUniverseBackfillStatus: () => requestJson<any | null>(`${API}/universe/backfill/status`),
  cancelUniverseBackfill: () =>
    requestJson<any>(`${API}/universe/backfill/cancel`, { method: "POST" }),
  getUniverseStats: () => requestJson<any>(`${API}/universe/stats`),

  // Market Events
  getMarketEvents: (params: {
    impact_scope?: string;
    importance_level_min?: number;
    importance_level_max?: number;
    affected_market?: string;
    sentiment?: string;
    date_from?: string;
    date_to?: string;
    is_manual?: number;
    limit?: number;
    offset?: number;
    sort_by?: string;
  } = {}) => {
    const q = new URLSearchParams();
    if (params.impact_scope) q.set("impact_scope", params.impact_scope);
    if (params.importance_level_min != null) q.set("importance_level_min", String(params.importance_level_min));
    if (params.importance_level_max != null) q.set("importance_level_max", String(params.importance_level_max));
    if (params.affected_market) q.set("affected_market", params.affected_market);
    if (params.sentiment) q.set("sentiment", params.sentiment);
    if (params.date_from) q.set("date_from", params.date_from);
    if (params.date_to) q.set("date_to", params.date_to);
    if (params.is_manual != null) q.set("is_manual", String(params.is_manual));
    q.set("limit", String(params.limit ?? 50));
    q.set("offset", String(params.offset ?? 0));
    if (params.sort_by) q.set("sort_by", params.sort_by);
    return requestJson<any>(`${API}/market-events?${q.toString()}`);
  },
  collectMarketEvents: (payload: { days?: number; sources?: string[] } = {}) =>
    requestJson<any>(`${API}/market-events/collect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs: 60000,
    }),
  getMarketEventScopes: () =>
    requestJson<string[]>(`${API}/market-events/scopes`),

  // Journals
  getJournals: (portfolioId: number, symbolId?: number) => {
    const params = new URLSearchParams({ portfolio_id: String(portfolioId) });
    if (symbolId) params.set("symbol_id", String(symbolId));
    return requestJson<any[]>(`${API}/journals?${params.toString()}`);
  },
  createJournal: (payload: unknown) =>
    requestJson<any>(`${API}/journals`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  updateJournal: (journalId: number, payload: unknown) =>
    requestJson<any>(`${API}/journals/${journalId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  deleteJournal: (journalId: number) =>
    requestJson<any>(`${API}/journals/${journalId}`, { method: "DELETE" }),

  // Signal stats
  getSignalStats: (symbolId: number, portfolioId: number = 1) =>
    requestJson<any>(`${API}/signal-rules/stats/${symbolId}?portfolio_id=${portfolioId}`),

  // Backtest
  runBacktest: (payload: unknown) =>
    requestJson<any>(`${API}/backtest/run`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeoutMs: 120000 }),
  getBacktestRuns: (portfolioId: number, limit = 20) =>
    requestJson<any[]>(`${API}/backtest/runs?portfolio_id=${portfolioId}&limit=${limit}`),
  getBacktestRun: (runId: number) =>
    requestJson<any>(`${API}/backtest/runs/${runId}`),
  deleteBacktestRun: (runId: number) =>
    requestJson<any>(`${API}/backtest/runs/${runId}`, { method: "DELETE" }),
  applyBacktestToPortfolio: (runId: number, portfolioId: number, clearExisting = false) =>
    requestJson<any>(
      `${API}/backtest/runs/${runId}/apply-to-portfolio`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ portfolio_id: portfolioId, clear_existing: clearExisting }),
        timeoutMs: 60000,
      },
    ),
  // P2-2: 组合整体回测（symbol_ids 与 rule_config 由后端自动推导）
  // WP7.3: 新增 only_auto 参数（仅回测 auto 成员，跳过 manual/confirm）
  runPortfolioBacktest: (portfolioId: number, payload: { start_date: string; end_date: string; run_name?: string; only_auto?: boolean }) =>
    requestJson<any>(`${API}/backtest/portfolio/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ portfolio_id: portfolioId, ...payload }),
      timeoutMs: 120000,
    }),
  // WP7.4: 组合回测标的来源开关状态
  getPortfolioBacktestSourceStatus: (portfolioId: number) =>
    requestJson<{ enabled: boolean; env_flag: string; source_label: string }>(
      `${API}/portfolios/${portfolioId}/backtest/source-status`,
    ),
  // WP7.4: 新旧引擎对比（同区间跑两次回测并对比标的集/指标差异）
  comparePortfolioBacktestEngines: (
    portfolioId: number,
    payload: { start_date: string; end_date: string; initial_capital?: number; run_name_prefix?: string },
  ) =>
    requestJson<{
      old: {
        run_id: number;
        symbol_ids: number[];
        source_type: string;
        trades: Array<{ symbol_id: number; entry_date: string | null; exit_date: string | null; pnl: number | null; pnl_pct: number | null }>;
        metrics: {
          total_return: number | null;
          total_return_pct: number | null;
          max_drawdown: number | null;
          max_drawdown_pct: number | null;
          sharpe_ratio: number | null;
          trade_count: number;
        };
      };
      new: {
        run_id: number;
        symbol_ids: number[];
        source_type: string;
        trades: Array<{ symbol_id: number; entry_date: string | null; exit_date: string | null; pnl: number | null; pnl_pct: number | null }>;
        metrics: {
          total_return: number | null;
          total_return_pct: number | null;
          max_drawdown: number | null;
          max_drawdown_pct: number | null;
          sharpe_ratio: number | null;
          trade_count: number;
        };
      };
      diff: {
        symbol_ids_added: number[];
        symbol_ids_removed: number[];
        metrics_diff: Record<string, { old: number | null; new: number | null; delta: number | null }>;
        explanation: string;
      };
    }>(`${API}/portfolios/${portfolioId}/backtest/compare`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ portfolio_id: portfolioId, ...payload }),
      timeoutMs: 240000,
    }),

  // Backtest rule templates
  getBacktestTemplates: () =>
    requestJson<any[]>(`${API}/backtest/templates`),
  createBacktestTemplate: (data: { name: string; description: string; rule_config: Record<string, unknown> }) =>
    requestJson<any>(`${API}/backtest/templates`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) }),
  updateBacktestTemplate: (id: number, data: Partial<{ name: string; description: string; rule_config: Record<string, unknown> }>) =>
    requestJson<any>(`${API}/backtest/templates/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) }),
  deleteBacktestTemplate: (id: number) =>
    requestJson<any>(`${API}/backtest/templates/${id}`, { method: "DELETE" }),


  // Custom indicators
  getCustomIndicators: (params: { scope?: string; enabled?: boolean } = {}) => {
    const q = new URLSearchParams();
    if (params.scope) q.set("scope", params.scope);
    if (params.enabled != null) q.set("enabled", String(params.enabled));
    const suffix = q.toString() ? `?${q.toString()}` : "";
    return requestJson<any[]>(`${API}/settings/custom-indicators${suffix}`);
  },
  createCustomIndicator: (payload: unknown) =>
    requestJson<any>(`${API}/settings/custom-indicators`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  updateCustomIndicator: (id: number, payload: unknown) =>
    requestJson<any>(`${API}/settings/custom-indicators/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  previewCustomIndicator: (payload: { symbol_id: number; formula: string; value_type: "boolean" | "number"; trade_date?: string; recent_count?: number }) =>
    requestJson<CustomIndicatorPreviewRead>(`${API}/settings/custom-indicators/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  deleteCustomIndicator: (id: number) =>
    requestJson<any>(`${API}/settings/custom-indicators/${id}`, { method: "DELETE" }),
  getIndicatorVersions: (id: number) =>
    requestJson<any[]>(`${API}/settings/custom-indicators/${id}/versions`),
  rollbackIndicator: (id: number, version: number) =>
    requestJson<any>(`${API}/settings/custom-indicators/${id}/rollback/${version}`, { method: "POST", headers: { "Content-Type": "application/json" } }),


  // Discovery plans
  getDiscoveryPlans: () =>
    requestJson<any[]>(`${API}/settings/discovery-plans`),
  createDiscoveryPlan: (payload: unknown) =>
    requestJson<any>(`${API}/settings/discovery-plans`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  updateDiscoveryPlan: (id: number, payload: unknown) =>
    requestJson<any>(`${API}/settings/discovery-plans/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  deleteDiscoveryPlan: (id: number) =>
    requestJson<any>(`${API}/settings/discovery-plans/${id}`, { method: "DELETE" }),
  // Backup & Export
  backupDatabase: () =>
    requestJson<any>(`${API}/system/backup`, { method: "POST", timeoutMs: 60000 }),
  listBackups: () =>
    requestJson<any>(`${API}/system/backups`),
  restoreDatabase: (backupPath: string) =>
    requestJson<any>(`${API}/system/restore?backup_path=${encodeURIComponent(backupPath)}`, { method: "POST", timeoutMs: 120000 }),
  exportData: (dataType: string, portfolioId: number = 1) =>
    requestJson<any>(`${API}/system/export/${dataType}?portfolio_id=${portfolioId}`),

  // Unified task history
  getTaskHistory: (taskType?: string, limit: number = 30) =>
    requestJson<{ tasks: any[] }>(`${API}/system/tasks?limit=${limit}${taskType ? `&task_type=${encodeURIComponent(taskType)}` : ""}`),

  // Cross-platform scheduled tasks
  getScheduledTaskDefinitions: () =>
    requestJson<ScheduledTaskDefinition[]>(`${API}/scheduled-tasks/definitions`),
  getScheduledTasks: () =>
    requestJson<ScheduledTask[]>(`${API}/scheduled-tasks`),
  getScheduledTaskRuns: (limit: number = 100) =>
    requestJson<ScheduledTaskRun[]>(`${API}/scheduled-tasks/runs?limit=${limit}`),
  createScheduledTask: (payload: ScheduledTaskPayload) =>
    requestJson<ScheduledTask>(`${API}/scheduled-tasks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  updateScheduledTask: (scheduleId: number, payload: Partial<ScheduledTaskPayload>) =>
    requestJson<ScheduledTask>(`${API}/scheduled-tasks/${scheduleId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  deleteScheduledTask: (scheduleId: number) =>
    requestJson<{ status: string; id: number }>(`${API}/scheduled-tasks/${scheduleId}`, { method: "DELETE" }),
  runScheduledTask: (scheduleId: number) =>
    requestJson<ScheduledTaskRun>(`${API}/scheduled-tasks/${scheduleId}/run`, { method: "POST" }),

  // Alerts
  getAlertRules: () =>
    requestJson<any[]>(`${API}/alerts/rules`),
  createAlertRule: (payload: unknown) =>
    requestJson<any>(`${API}/alerts/rules`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  updateAlertRule: (id: number, payload: unknown) =>
    requestJson<any>(`${API}/alerts/rules/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  deleteAlertRule: (id: number) =>
    requestJson<any>(`${API}/alerts/rules/${id}`, { method: "DELETE" }),
  getActiveAlerts: (limit: number = 50) =>
    requestJson<{ events: any[]; count: number }>(`${API}/alerts/active?limit=${limit}`),
  getAlertEvents: (limit: number = 50, includeAcknowledged: boolean = false) =>
    requestJson<{ events: any[]; unacknowledged_count: number }>(`${API}/alerts/events?limit=${limit}&include_acknowledged=${includeAcknowledged}`),
  acknowledgeAlert: (eventId: number) =>
    requestJson<any>(`${API}/alerts/acknowledge/${eventId}`, { method: "POST" }),
  acknowledgeAllAlerts: () =>
    requestJson<any>(`${API}/alerts/acknowledge-all`, { method: "POST" }),
  evaluateAlerts: () =>
    requestJson<any>(`${API}/alerts/evaluate`, { method: "POST" }),

  // P2: External data sync (valuation / financial reports / flow / ETF)
  syncFundamental: (source: "watchlist" | "positions" | "all") =>
    requestJson<{ total: number; success: number; skipped: number; failed: number; errors: string[] }>(
      `${API}/external-data/fundamental/sync?source=${source}`,
      { method: "POST", timeoutMs: 120000 },
    ),
  syncFinancialReports: (source: "watchlist" | "positions" | "all") =>
    requestJson<{ total: number; success: number; skipped: number; failed: number; records: number; errors: string[] }>(
      API + "/external-data/financial-reports/sync?source=" + source,
      { method: "POST", timeoutMs: 300000 },
    ),
  syncLhbInstitution: (lookbackDays: number = 30) =>
    requestJson<{ total: number; success: number; skipped: number; failed: number; records: number; errors: string[] }>(
      API + "/external-data/lhb-institution/sync?lookback_days=" + lookbackDays,
      { method: "POST", timeoutMs: 120000 },
    ),
  syncHotRank: () =>
    requestJson<{ total: number; success: number; skipped: number; failed: number; records: number; errors: string[] }>(
      API + "/external-data/hot-rank/sync",
      { method: "POST", timeoutMs: 60000 },
    ),
  syncTailProxy: (limit: number = 20) =>
    requestJson<{ total: number; success: number; skipped: number; failed: number; records: number; errors: string[] }>(
      API + "/external-data/tail-proxy/sync?source=candidates&limit=" + limit,
      { method: "POST", timeoutMs: 300000 },
    ),
  syncCapitalFlow: (source: "watchlist" | "positions" | "all", includeNorthbound: boolean = true) =>
    requestJson<{ total: number; success: number; skipped: number; failed: number; errors: string[] }>(
      `${API}/external-data/capital-flow/sync?source=${source}&include_northbound=${includeNorthbound}`,
      { method: "POST", timeoutMs: 120000 },
    ),
  syncEtfIndicators: (source: "watchlist" | "positions" | "all") =>
    requestJson<{ total: number; success: number; skipped: number; failed: number; errors: string[] }>(
      `${API}/external-data/etf-indicators/sync?source=${source}`,
      { method: "POST", timeoutMs: 120000 },
    ),

  // P2-E: Akshare API management (status / probe / config)
  listAkshareApis: (locale: string = "zh-CN") =>
    requestJson<AkshareApiStatus[]>(`${API}/external-data/apis?locale=${locale}`),
  listAkshareStrategies: (locale: string = "zh-CN") =>
    requestJson<AkshareStrategyInfo[]>(`${API}/external-data/apis/strategies?locale=${locale}`),
  probeAkshareApi: (apiKey: string) =>
    requestJson<{ key: string; success: boolean; latency_ms: number | null; error: string | null }>(
      `${API}/external-data/apis/${encodeURIComponent(apiKey)}/probe`,
      { method: "POST", timeoutMs: 30000 },
    ),
  updateAkshareApiConfig: (apiKey: string, payload: Partial<AkshareApiConfigUpdate>, locale: string = "zh-CN") =>
    requestJson<AkshareApiStatus>(
      `${API}/external-data/apis/${encodeURIComponent(apiKey)}?locale=${locale}`,
      { method: "PUT", body: JSON.stringify(payload) },
    ),

  // AI 接口配置
  getAiConfig: () =>
    requestJson<AiConfig>(`${API}/settings/ai-config`),
  updateAiConfig: (payload: AiConfigUpdate) =>
    requestJson<{ status: string; message: string }>(
      `${API}/settings/ai-config`,
      { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
    ),
  testAiConnection: (params: AiConfigUpdate) =>
    requestJson<{ success: boolean; message: string }>(
      `${API}/settings/ai-config/test`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(params), timeoutMs: 20000 },
    ),
  listAiModels: (params: AiConfigUpdate) =>
    requestJson<{ models: AiModelInfo[]; error?: string }>(
      `${API}/settings/ai-config/models`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(params), timeoutMs: 20000 },
    ),
  aiChat: (payload: AiChatPayload) =>
    requestJson<AiChatResult>(
      `${API}/settings/ai-chat`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeoutMs: 35000 },
    ),

  // WP-AI.7：AI 会话管理
  getAISessions: (limit: number = 20, offset: number = 0) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return requestJson<{ items: AISession[]; limit: number; offset: number; include_archived: boolean }>(
      `${API}/ai/sessions?${params.toString()}`,
    );
  },
  getAISession: (sessionId: number) =>
    requestJson<AISession>(`${API}/ai/sessions/${sessionId}`),
  createAISession: (payload: { title: string; source_page?: string; message?: string; references?: Record<string, unknown> }) =>
    requestJson<{ session_id: number; response: AIResponse }>(
      `${API}/ai/sessions`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeoutMs: 60000 },
    ),
  getAIMessages: (sessionId: number, limit: number = 100) =>
    requestJson<{ items: AIMessage[]; session_id: number; limit: number; offset: number }>(
      `${API}/ai/sessions/${sessionId}/messages?limit=${limit}`,
    ),
  deleteAISession: (sessionId: number) =>
    requestJson<{ status: string; message: string; session_id: number }>(
      `${API}/ai/sessions/${sessionId}`,
      { method: "DELETE" },
    ),
  cleanupAISessions: () =>
    requestJson<{ status: string; cleaned: number; retention_days: number; message: string }>(
      `${API}/ai/sessions/cleanup`,
      { method: "POST" },
    ),

  // WP-AI.7：AI Profile 管理
  getAIProfiles: () =>
    requestJson<AIProfile[]>(`${API}/ai/profiles`),
  createAIProfile: (payload: Record<string, unknown>) =>
    requestJson<AIProfile>(`${API}/ai/profiles`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }),
  updateAIProfile: (id: number, payload: Record<string, unknown>) =>
    requestJson<AIProfile>(`${API}/ai/profiles/${id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }),
  deleteAIProfile: (id: number) =>
    requestJson<{ status: string; message: string }>(`${API}/ai/profiles/${id}`, { method: "DELETE" }),
  testAIProfile: (id: number) =>
    requestJson<AIProfileTestResult>(`${API}/ai/profiles/${id}/test`, { method: "POST", timeoutMs: 30000 }),
  discoverAIModels: (id: number) =>
    requestJson<{ models: Array<{ id: string; owned_by?: string }> }>(`${API}/ai/profiles/${id}/models`),
  getAIProfileUsage: (id: number) =>
    requestJson<AIProfileUsage>(`${API}/ai/profiles/${id}/usage`),
  getAIHealth: () =>
    requestJson<AIHealth[]>(`${API}/ai/health`),
};

// ----------------------------------------------------------------------------
// P2-E: Akshare API management types
// ----------------------------------------------------------------------------
export interface AkshareApiStatus {
  // 元数据（只读）
  key: string;
  name: string;
  category: string;
  module: string;
  description: string;
  default_strategy: string;
  // 用户配置（可修改）
  enabled: boolean;
  anti_risk_strategy: string;
  delay_min_ms: number;
  delay_max_ms: number;
  // 运行时状态（只读）
  last_probe_at: string | null;
  last_probe_success: boolean | null;
  last_probe_latency_ms: number | null;
  last_probe_error: string | null;
  last_call_at: string | null;
  last_call_success: boolean | null;
  last_call_error: string | null;
  total_calls: number;
  total_failures: number;
}

export interface AkshareStrategyInfo {
  key: string;
  name: string;
  delay_min_ms: number | null;
  delay_max_ms: number | null;
  max_retries: number;
  desc: string;
}

export interface AkshareApiConfigUpdate {
  enabled?: boolean;
  anti_risk_strategy?: string;
  delay_min_ms?: number;
  delay_max_ms?: number;
}

// ----------------------------------------------------------------------------
// Dynamic factor engine types
// ----------------------------------------------------------------------------
export type FactorWeightMode = "manual" | "shadow" | "ridge";

export interface FactorRuntime {
  weight_mode: FactorWeightMode;
  score_weight_mode: "manual" | "ridge";
  active_model_run_id: string | null;
  updated_by: string;
  fallback_reason: string | null;
  version: number;
  updated_at: string | null;
}

export interface FactorSystemConfig {
  feature_enabled: boolean;
  warehouse_path: string;
  updated_by: string;
  updated_at: string | null;
}

export interface FactorCoverage {
  factor_code: string;
  latest_trade_date: string | null;
  universe_symbols: number;
  eligible_symbols: number;
  imputed_symbols: number;
  coverage: number;
}

export interface FactorOverview {
  runtime: FactorRuntime;
  config: FactorSystemConfig;
  feature_enabled: boolean;
  warehouse_error: string | null;
  health: {
    status: "healthy" | "warning" | "degraded" | "failed" | string;
    warehouse_available: boolean;
    warehouse_path: string;
    schema_version: string | null;
    calc_batch_id: string | null;
    latest_bar_date: string | null;
    raw_tables: Array<{ table: string; row_count: number; latest_date: string | null }>;
    factors: FactorCoverage[];
    reasons: string[];
  };
  latest_trade_date: string | null;
  factor_coverage: FactorCoverage[];
}

export interface FactorModelWeight {
  factor_code: string;
  factor_version: number;
  coefficient: number;
  normalized_weight: number;
  train_ic: number | null;
  validation_ic: number | null;
}

export interface FactorModelRun {
  id: string;
  model_type: string;
  asset_type: string;
  target_code: string;
  train_start_date: string | null;
  train_end_date: string | null;
  validation_start_date: string | null;
  validation_end_date: string | null;
  data_cutoff_at: string | null;
  feature_versions: Record<string, number>;
  hyperparameters: Record<string, unknown>;
  metrics: Record<string, number | string | boolean | null>;
  sample_count: number;
  symbol_count: number;
  trade_date_count: number;
  status: "validated" | "rejected" | string;
  rejection_reason: string | null;
  artifact_path: string | null;
  created_at: string | null;
  activated_at: string | null;
  weights: FactorModelWeight[];
  audit?: Array<Record<string, unknown>>;
}

export interface FactorModelList {
  runtime: FactorRuntime;
  items: FactorModelRun[];
}

export interface FactorPipelineCreate {
  start_date?: string | null;
  end_date?: string | null;
  data_cutoff_date?: string | null;
  full_refresh?: boolean;
  train_model?: boolean;
  materialize_scores?: boolean;
  window_days?: number;
  validation_days?: number;
}

export interface FactorPipelineTask {
  id: string;
  task_type: string;
  status: "queued" | "running" | "done" | "completed" | "failed" | "cancelled" | string;
  stage: string;
  percent: number;
  message: string;
  total: number;
  processed: number;
  ok_count: number;
  failed_count: number;
  current_item: string | null;
  result: Record<string, unknown> | null;
  errors: Array<Record<string, unknown>>;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string | null;
}

export interface FactorPipelineEta {
  avg_seconds: number;
  median_seconds: number;
  sample_count: number;
  fallback_seconds: number;
  recommended_seconds: number;
  train_model: boolean;
  full_refresh: boolean;
}

export interface FactorContribution {
  factor_version?: number;
  category?: string;
  raw_value?: number | null;
  winsorized_value?: number | null;
  normalized_value?: number | null;
  coefficient?: number | null;
  normalized_weight?: number | null;
  contribution?: number | null;
  is_imputed?: boolean;
  imputation_method?: string | null;
}

export interface SymbolFactorExplanation {
  symbol_id: number;
  symbol: string;
  name: string;
  trade_date: string;
  weight_mode: "shadow" | "ridge";
  model_run_id: string;
  factor_data_cutoff_at: string | null;
  factor_quality_score: number | null;
  factor_timing_score: number | null;
  model_alpha_score: number | null;
  macro_regime: string | null;
  macro_position_multiplier: number | null;
  explanation: {
    mode?: string;
    model_run_id?: string;
    factor_calc_batch_id?: string;
    factor_data_cutoff_at?: string | null;
    factors?: Record<string, FactorContribution>;
    event_factors?: Record<string, FactorContribution>;
    factor_quality_raw?: number;
    factor_timing_raw?: number;
    model_alpha_raw?: number;
    factor_quality_score?: number;
    factor_timing_score?: number;
    model_alpha_score?: number;
    blend?: Record<string, number>;
    macro?: {
      as_of?: string | null;
      regime?: string;
      position_multiplier?: number;
      available?: boolean;
      cn_10y_change?: number | null;
      us_10y_change?: number | null;
      margin_change_ratio?: number | null;
      market_amount_change_ratio?: number | null;
      market_amount_z20?: number | null;
      advancing_ratio?: number | null;
      margin_amount_divergence?: number | null;
      liquidity_score?: number | null;
      liquidity_available?: boolean;
      missing_indicators?: string[];
      missing_liquidity_indicators?: string[];
    };
  };
}

// ----------------------------------------------------------------------------
// Cross-platform scheduled task types
// ----------------------------------------------------------------------------
export type ScheduledTaskFrequency = "daily" | "weekly" | "interval";

export interface ScheduledTaskDefinition {
  task_type: string;
  name: string;
  description: string;
  default_payload: Record<string, unknown>;
}

export interface ScheduledTaskPayload {
  name: string;
  task_type: string;
  frequency: ScheduledTaskFrequency;
  time_of_day: string | null;
  weekdays: number[];
  interval_minutes: number | null;
  timezone: string;
  payload: Record<string, unknown>;
  enabled: boolean;
}

export interface ScheduledTask extends ScheduledTaskPayload {
  id: number;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string | null;
  last_task_id: string | null;
  last_task_status: string | null;
  last_message: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ScheduledTaskRun {
  id: number;
  schedule_id: number;
  schedule_name: string | null;
  task_type: string | null;
  trigger_source: "manual" | "scheduled" | string;
  task_source: "async" | "discovery" | null;
  task_id: string | null;
  status: string;
  message: string | null;
  created_at: string;
  finished_at: string | null;
}

// ----------------------------------------------------------------------------
// AI 接口配置类型
// ----------------------------------------------------------------------------
export interface AiConfig {
  provider: "openai_compatible" | "anthropic" | "ollama" | "custom";
  service_url: string;
  api_key: string;
  model: string;
  enabled: boolean;
  auth_type: "bearer" | "x-api-key" | "api-key" | "custom" | "none";
  auth_header: string;
  chat_path: string;
  models_path: string;
  timeout_seconds: number;
  temperature: number;
  max_tokens: number;
  extra_headers: Record<string, string>;
  api_key_set: boolean;
  persisted: boolean;
  updated_at?: string | null;
}

export interface AiModelInfo {
  id: string;
  owned_by: string;
  created: number;
}

export interface AiConfigUpdate {
  provider?: AiConfig["provider"];
  service_url?: string;
  api_key?: string | null;
  model?: string;
  enabled?: boolean;
  auth_type?: AiConfig["auth_type"];
  auth_header?: string;
  chat_path?: string;
  models_path?: string;
  timeout_seconds?: number;
  temperature?: number;
  max_tokens?: number;
  extra_headers?: Record<string, string>;
}

export interface AiChatPayload {
  message: string;
  formula?: string;
  history?: Array<{ role: string; content: string }>;
}

export interface AiChatResult {
  ok: boolean;
  reply: string;
  error: string;
}
