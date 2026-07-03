import { t } from "../i18n";
import type { CustomIndicatorPreviewRead, SignalRule, SignalRulePreviewResult } from "../types";

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

  // Portfolios
  getPortfolios: () => requestJson<any[]>(`${API}/portfolios`),
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
    requestJson<any>(`${API}/macro/update`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeoutMs: 60000 }),
  getMacroIndicatorHistory: (region: string, indicatorKey: string, limit = 60) =>
    requestJson<any[]>(`${API}/macro/indicators/${encodeURIComponent(indicatorKey)}/history?region=${encodeURIComponent(region)}&limit=${limit}`),

  // Discovery
  getDiscoveryTasks: (limit = 10) => requestJson<any[]>(`${API}/discovery/tasks?limit=${limit}`),
  createDiscoveryTask: (payload: unknown) =>
    requestJson<any>(`${API}/discovery/tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  sendDiscoveryCommand: (taskId: number, command: string) =>
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
};


