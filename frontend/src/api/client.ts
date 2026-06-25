import { t } from "../i18n";

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

export async function requestJson<T = any>(url: string, options: RequestInit & { timeoutMs?: number } = {}): Promise<T> {
  activeRequests++;
  notifyRequestChange();
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? 20000;
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  const { timeoutMs: _omit, ...requestOptions } = options as any;
  try {
    const response = await fetch(url, { ...requestOptions, signal: requestOptions.signal ?? controller.signal });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.message || response.statusText);
    return payload as T;
  } catch (error: any) {
    if (error.name === "AbortError") {
      throw new Error(t("requestTimeout"));
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    activeRequests = Math.max(0, activeRequests - 1);
    notifyRequestChange();
  }
}

const API = "/api/v1";

export const api = {
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
  createSymbol: (payload: any) =>
    requestJson<any>(`${API}/symbols`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

  // Watchlists
  getWatchlistItems: (watchlistId: number) => requestJson<any[]>(`${API}/watchlists/${watchlistId}/items`),
  addWatchlistItem: (watchlistId: number, symbolId: number) =>
    requestJson(`${API}/watchlists/${watchlistId}/items`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol_id: symbolId }) }),

  // Market data
  syncMarketData: (payload: any) =>
    requestJson(`${API}/market-data/update`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

  // Scans
  createScanRun: (payload: any) =>
    requestJson(`${API}/scans/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

  // Scores
  calculateScores: (payload: any) =>
    requestJson(`${API}/scores/calculate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

  // Trade setups
  generateTradeSetup: (payload: any) =>
    requestJson(`${API}/trade-setups/generate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

  // Signal rules
  getSignalRulePresets: () => requestJson<any[]>(`${API}/signal-rules/presets`),
  getSignalRule: (portfolioId: number) => requestJson<any>(`${API}/portfolios/${portfolioId}/signal-rule`),
  saveSignalRule: (portfolioId: number, payload: any) =>
    requestJson(`${API}/portfolios/${portfolioId}/signal-rule`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  previewSignalRule: (portfolioId: number, payload: any) =>
    requestJson(`${API}/portfolios/${portfolioId}/signal-rule/preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

  // Sim accounts
  submitSimOrder: (portfolioId: number, payload: any) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}/sim-orders`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

  // News
  updateNews: (payload: any) =>
    requestJson<any>(`${API}/news/update`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  getLatestNews: (portfolioId: number, symbolIds: number[], days = 7, limit = 20) => {
    const params = new URLSearchParams({ portfolio_id: String(portfolioId), days: String(days), limit: String(limit) });
    symbolIds.forEach((id) => params.append("symbol_ids", String(id)));
    return requestJson<any>(`${API}/news/latest?${params.toString()}`);
  },

  // Macro
  getMacroOverview: (region = "all") =>
    requestJson<any>(`${API}/macro/overview?region=${encodeURIComponent(region)}`),
  updateMacroData: (payload: any) =>
    requestJson<any>(`${API}/macro/update`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeoutMs: 60000 }),
  getMacroIndicatorHistory: (region: string, indicatorKey: string, limit = 60) =>
    requestJson<any[]>(`${API}/macro/indicators/${encodeURIComponent(indicatorKey)}/history?region=${encodeURIComponent(region)}&limit=${limit}`),

  // Discovery
  getDiscoveryTasks: (limit = 10) => requestJson<any[]>(`${API}/discovery/tasks?limit=${limit}`),
  createDiscoveryTask: (payload: any) =>
    requestJson<any>(`${API}/discovery/tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  sendDiscoveryCommand: (taskId: number, command: string) =>
    requestJson<any>(`${API}/discovery/tasks/${taskId}/${command}`, { method: "POST" }),
  getDiscoveryScopeStats: (scope: string) => requestJson<any>(`${API}/discovery/scopes/${encodeURIComponent(scope)}/stats`),
  updateDiscoveryResult: (resultId: number, payload: any) =>
    requestJson(`${API}/discovery/results/${resultId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  refreshDiscoveryResult: (resultId: number) =>
    requestJson(`${API}/discovery/results/${resultId}/refresh`, { method: "POST" }),
  cleanupDiscoveryResults: () =>
    requestJson<{ deleted: number; skipped_frozen: number }>(`${API}/discovery/results/cleanup`, { method: "POST" }),

  // Market Events (行情消息)
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
};
