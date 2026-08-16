import { t } from "../i18n";
import type { AIDraftDetail, AIDraftExecuteResult, AIDraftPreviewResult, AsyncTaskRead, CapabilitiesResponse, CustomIndicatorPreviewRead, CustomIndicatorPromoteResponse, SignalRule, SignalRulePreviewResult, SnapshotStatusRead } from "../types";
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

// 指数同步异步任务返回结构（与后端 _task_to_dict 字段一致，percent/total/processed/ok_count/result.items）
export interface IndexSyncTaskRead {
  id: string;
  task_type: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled" | string;
  stage: string;
  percent: number;         // 0~100
  message: string;
  total: number;           // 指数总数
  processed: number;       // 已处理数
  ok_count: number;        // 成功数
  failed_count: number;    // 失败数
  current_item: string | null;  // 当前同步中的 symbol
  // 终态返回：结构与原同步接口 IndexPriceSyncResponse 完全一致
  result: null | {
    total: number; success: number; failed: number;
    items: Array<{
      symbol: string; name: string;
      received?: number; written?: number; skipped?: number;
      first_date?: string | null; last_date?: string | null;
      error?: string | null;
    }>;
  };
  errors: Array<Record<string, unknown>>;
  error_code: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  updated_at?: string | null;
  heartbeat_at?: string | null;
  last_progress_at?: string | null;
  current_step_description?: string | null;
  cancel_requested?: boolean;
}

// 通用 API 响应类型：默认 unknown，调用方可显式指定具体类型
type ApiResponse<T = unknown> = T;

// WP-S.6 统一错误协议字段（与 app/schemas/errors.py UserError 对齐）
export interface UnifiedErrorPayload {
  error_code: string;
  user_message: string;
  impact: string;
  retryable: boolean;
  completed: number;
  next_actions: Array<{
    label: string;
    action_type: "retry" | "redirect" | "configure" | "dismiss" | "view_details" | "sync";
    target?: string | null;
    reason?: string | null;
  }>;
  technical_details?: {
    exception_type?: string | null;
    status_code?: number | null;
    error_message?: string | null;
    stack_summary?: string | null;
    db_error_code?: string | null;
    upstream_response?: string | null;
    request_id?: string | null;
  } | null;
  correlation_id: string;
}

// 增强错误类型：携带统一错误协议字段，便于上层展示 user_message / next_actions
export class ApiError extends Error {
  status_code?: number;
  error_code?: string;
  user_message?: string;
  impact?: string;
  retryable?: boolean;
  next_actions?: UnifiedErrorPayload["next_actions"];
  correlation_id?: string;
  detail?: unknown;

  constructor(message: string, init: {
    status_code?: number;
    error_code?: string;
    user_message?: string;
    impact?: string;
    retryable?: boolean;
    next_actions?: UnifiedErrorPayload["next_actions"];
    correlation_id?: string;
    detail?: unknown;
  } = {}) {
    super(message);
    this.name = "ApiError";
    this.status_code = init.status_code;
    this.error_code = init.error_code;
    this.user_message = init.user_message;
    this.impact = init.impact;
    this.retryable = init.retryable;
    this.next_actions = init.next_actions;
    this.correlation_id = init.correlation_id;
    this.detail = init.detail;
  }
}

let activeRequests = 0;
const requestListeners: Array<(count: number) => void> = [];
const inFlightGetRequests = new Map<string, Promise<unknown>>();

type RequestJsonOptions = RequestInit & {
  timeoutMs?: number;
  /** Set to false when callers intentionally need parallel GET requests. */
  dedupe?: boolean;
};

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

function getRequestDedupeKey(url: string, options: RequestJsonOptions): string {
  const headers = Array.from(new Headers(options.headers).entries())
    .sort(([left], [right]) => left.localeCompare(right));
  return JSON.stringify([
    url,
    headers,
    options.credentials ?? null,
    options.cache ?? null,
    options.mode ?? null,
    options.timeoutMs ?? 20000,
  ]);
}

function shouldDedupeRequest(options: RequestJsonOptions): boolean {
  const method = (options.method ?? 'GET').toUpperCase();
  return method === 'GET' && options.dedupe !== false && options.signal == null;
}

// 判断 payload 是否为 WP-S.6 统一错误协议响应
function isUnifiedErrorPayload(payload: unknown): payload is UnifiedErrorPayload {
  return (
    !!payload &&
    typeof payload === "object" &&
    "error_code" in payload &&
    "user_message" in payload &&
    typeof (payload as UnifiedErrorPayload).error_code === "string" &&
    typeof (payload as UnifiedErrorPayload).user_message === "string"
  );
}

function defaultHttpErrorMessage(status: number): string {
  if (status === 400 || status === 422) return t("httpErrorInvalidRequest");
  if (status === 401) return t("httpErrorUnauthorized");
  if (status === 403) return t("httpErrorForbidden");
  if (status === 404) return t("httpErrorNotFound");
  if (status === 409) return t("httpErrorConflict");
  if (status === 429) return t("httpErrorRateLimited");
  if (status >= 500) return t("httpErrorServer");
  return t("httpErrorRequestFailed");
}

async function executeRequestJson<T>(url: string, options: RequestJsonOptions): Promise<T> {
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
  // 移除自定义字段，保留标准 RequestInit 字段
  const { timeoutMs: _timeoutMs, dedupe: _dedupe, ...requestOptions } = options;
  try {
    const response = await fetch(url, { ...requestOptions, signal: requestOptions.signal ?? controller.signal });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      // 优先识别 WP-S.6 统一错误协议
      if (isUnifiedErrorPayload(payload)) {
        const techMsg = payload.technical_details?.error_message?.trim();
        // 把 technical_details 里的字段级校验错误详情拼到最终消息里，用户能知道具体是哪个字段出问题
        const finalMessage = techMsg
          ? `${payload.user_message || payload.error_code}（${techMsg}）`
          : (payload.user_message || payload.error_code);
        throw new ApiError(finalMessage, {
          status_code: response.status,
          error_code: payload.error_code,
          user_message: payload.user_message,
          impact: payload.impact,
          retryable: payload.retryable,
          next_actions: payload.next_actions,
          correlation_id: payload.correlation_id,
          detail: payload,
        });
      }
      // 兼容旧式 { detail: string } 或 FastAPI HTTPException
      const detail = payload.detail || payload.message;
      const rawMessage = typeof detail === "string" ? detail : detail?.message || (detail ? JSON.stringify(detail) : "");
      const isGenericStatusText =
        !rawMessage ||
        rawMessage === response.statusText ||
        /^(?:Bad Request|Unauthorized|Forbidden|Not Found|Conflict|Too Many Requests|Internal Server Error|Service Unavailable)$/i.test(rawMessage);
      const msg = isGenericStatusText ? defaultHttpErrorMessage(response.status) : rawMessage;
      throw new ApiError(msg || `HTTP ${response.status}`, {
        status_code: response.status,
        detail: detail || response.statusText,
      });
    }
    return payload as T;
  } catch (error: unknown) {
    // 收窄 unknown 类型，仅对 Error 实例判断 name 属性
    if (error instanceof Error && error.name === "AbortError") {
      // 仅超时（timedOut=true）时抛出超时错误；调用方主动取消则静默返回 rejected
      if (timedOut) {
        throw new ApiError(t("requestTimeout"), {
          status_code: 408,
          error_code: "REQUEST_TIMEOUT",
          user_message: t("requestTimeout"),
          impact: t("unifiedErrorTimeoutImpact"),
          retryable: true,
          next_actions: [{ label: t("observationPoolRetry"), action_type: "retry" }],
        });
      }
      // 调用方主动取消：抛出 AbortError 让调用方自行判断
      throw error;
    }
    // 网络错误（fetch 直接抛 TypeError，无 response）：包装为统一错误协议
    if (!(error instanceof ApiError) && error instanceof Error && (error.name === "TypeError" || /network|fetch/i.test(error.message))) {
      throw new ApiError(t("unifiedErrorNetworkMessage"), {
        status_code: 0,
        error_code: "NETWORK_ERROR",
        user_message: t("unifiedErrorNetworkMessage"),
        impact: t("unifiedErrorNetworkImpact"),
        retryable: true,
        next_actions: [{ label: t("observationPoolRetry"), action_type: "retry" }],
      });
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    activeRequests = Math.max(0, activeRequests - 1);
    notifyRequestChange();
  }
}

export function requestJson<T = unknown>(url: string, options: RequestJsonOptions = {}): Promise<T> {
  if (!shouldDedupeRequest(options)) {
    return executeRequestJson<T>(url, options);
  }

  const key = getRequestDedupeKey(url, options);
  const existing = inFlightGetRequests.get(key) as Promise<T> | undefined;
  if (existing) return existing;

  const pending = executeRequestJson<T>(url, options);
  inFlightGetRequests.set(key, pending);
  const cleanup = () => {
    if (inFlightGetRequests.get(key) === pending) {
      inFlightGetRequests.delete(key);
    }
  };
  pending.then(cleanup, cleanup);
  return pending;
}

const API = "/api/v1";

export const SYSTEM_HEALTH_URL = API + "/system/data-health";

export type ExternalSyncDataset =
  | "fundamental"
  | "financial"
  | "lhb"
  | "hot_rank"
  | "tail_proxy"
  | "capital_flow"
  | "etf";

export interface AIStreamHandlers {
  onSession?: (sessionId: number) => void;
  onDelta?: (content: string) => void;
  onDone?: (sessionId: number, response: AIResponse) => void;
}

export interface AiChatStreamHandlers {
  onDelta?: (content: string) => void;
  onDone?: (response: AiChatResult) => void;
}

function parseSseBlock(block: string): { event: string; data: Record<string, unknown> } | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  return { event, data: JSON.parse(dataLines.join("\n")) };
}

export async function streamAISession(
  payload: { title: string; source_page?: string; message: string; references?: Record<string, unknown> },
  handlers: AIStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API}/ai/sessions/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(body.user_message || body.detail || defaultHttpErrorMessage(response.status), {
      status_code: response.status, detail: body,
    });
  }
  if (!response.body) throw new ApiError("浏览器不支持流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const dispatch = (block: string) => {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    const data = JSON.parse(dataLines.join("\n"));
    if (event === "session") handlers.onSession?.(Number(data.session_id));
    if (event === "delta") handlers.onDelta?.(String(data.content ?? ""));
    if (event === "done") handlers.onDone?.(Number(data.session_id), data.response as AIResponse);
    if (event === "error") throw new ApiError(String(data.message || "AI 流式响应失败"));
  };
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      dispatch(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  if (buffer.trim()) dispatch(buffer);
}

export async function streamAIChat(
  payload: AiChatPayload,
  handlers: AiChatStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API}/settings/ai-chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(body.user_message || body.detail || defaultHttpErrorMessage(response.status), {
      status_code: response.status,
      detail: body,
    });
  }
  if (!response.body) throw new ApiError("Browser does not support streaming responses");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const dispatch = (block: string) => {
    const parsed = parseSseBlock(block);
    if (!parsed) return;
    const { event, data } = parsed;
    if (event === "delta") handlers.onDelta?.(String(data.content ?? ""));
    if (event === "done") handlers.onDone?.(data as unknown as AiChatResult);
    if (event === "error") throw new ApiError(String(data.message || t("aiChatFailed")));
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      dispatch(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  if (buffer.trim()) dispatch(buffer);
}

export interface ExternalSyncResult {
  dataset: ExternalSyncDataset;
  total: number;
  success: number;
  skipped: number;
  failed: number;
  records: number;
  errors: string[];
  plan?: ExternalDataSyncPlan;
}

export interface ExternalDataSyncPlan {
  dataset: ExternalSyncDataset;
  mode: "incremental" | "backfill";
  requested_start_date: string;
  requested_end_date: string;
  requested_span_days: number;
  provider_history_limit_days: number | null;
  provider_reason: string;
  partition_strategy: string;
  symbol_batch_size: number;
}

export type ExternalDataSyncCapabilities = Record<ExternalSyncDataset, {
  modes: Array<"incremental" | "backfill">;
  history_limit_days: number | null;
  reason: string;
}>;

export interface ExternalSyncTask {
  id: string;
  task_type: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  stage: string;
  percent: number;
  message: string;
  total: number;
  processed: number;
  ok_count: number;
  failed_count: number;
  current_item: string | null;
  result: ExternalSyncResult | null;
  errors: Array<{ stage?: string; error?: string }>;
  batch_recovery: {
    plan?: ExternalDataSyncPlan;
    last_symbol_id?: number;
    processed?: number;
    total?: number;
  } | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string | null;
}

export interface ExternalDatasetOverview {
  dataset: ExternalSyncDataset;
  records: number;
  symbols: number;
  latest_date: string | null;
  last_updated_at: string | null;
  latest_task: ExternalSyncTask | null;
}

export interface ExternalDataOverview {
  datasets: ExternalDatasetOverview[];
  total_records: number;
  covered_symbols: number;
  available_datasets: number;
  running_tasks: number;
  refreshed_at: string;
}

export interface ExternalFieldCoverage {
  field: string;
  availability: "available" | "limited" | "event" | "snapshot" | "blocked" | "unknown";
  evaluation_enabled: boolean;
  first_date: string | null;
  latest_date: string | null;
  nonnull_rows: number;
  table_rows: number;
  distinct_symbols: number;
  distinct_dates: number;
  continuity_days: number;
  daily_coverage_p50: number | null;
  daily_coverage_p90: number | null;
  latest_daily_coverage: number | null;
  reason: string;
}

export interface ExternalDatasetCoverage {
  dataset: ExternalSyncDataset;
  readiness: "available" | "limited" | "event" | "snapshot" | "blocked" | "unknown" | "not_applicable";
  reason: string;
  fields: ExternalFieldCoverage[];
}

export interface ExternalDataCoverage {
  datasets: ExternalDatasetCoverage[];
  generated_at: string;
}

export interface DataQualitySnapshot {
  id: string;
  dataset: string;
  field: string;
  readiness: string;
  evaluation_mode: string;
  row_count: number;
  nonnull_rows: number;
  distinct_symbols: number;
  distinct_dates: number;
  first_date: string | null;
  latest_date: string | null;
  failure_reason: string | null;
  metrics: Record<string, unknown>;
  captured_at: string | null;
}

export interface ExternalDataGapItem {
  symbol_id: number;
  symbol: string;
  trade_date: string;
}

export interface ExternalDataGapReport {
  dataset: "fundamental" | "financial" | "capital_flow";
  start_date: string;
  end_date: string;
  total_missing: number;
  truncated: boolean;
  gaps: ExternalDataGapItem[];
}

export interface ExternalSyncPartition {
  id: string;
  partition_key: string;
  symbol_id: number | null;
  symbol: string | null;
  start_date: string;
  end_date: string;
  status: "queued" | "running" | "done" | "skipped" | "failed" | "cancelled";
  attempts: number;
  rows_written: number;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface ExternalSyncPlanDetails {
  task_id: string;
  plan: {
    id: string;
    parent_plan_id: string | null;
    dataset: ExternalSyncDataset;
    mode: "incremental" | "backfill";
    source: string;
    status: "queued" | "running" | "done" | "partial" | "failed" | "cancelled";
    requested_start_date: string;
    requested_end_date: string;
    partition_strategy: string;
    total_partitions: number;
    completed_partitions: number;
    skipped_partitions: number;
    failed_partitions: number;
  } | null;
  partitions: ExternalSyncPartition[];
}

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
  getFactorFormulaCatalog: () =>
    requestJson<FactorFormulaCatalog>(`${API}/factors/formula-catalog`),
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
  // WP7-06: FactorSet API
  listFactorSets: (status?: string, limit: number = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (status) params.set("status", status);
    return requestJson<FactorSet[]>(`${API}/factor-sets?${params.toString()}`);
  },
  getFactorSet: (factorSetId: string) =>
    requestJson<FactorSet>(`${API}/factor-sets/${encodeURIComponent(factorSetId)}`),
  freezeFactorSet: (factorSetId: string, reason: string) =>
    requestJson<FactorSet>(`${API}/factor-sets/${encodeURIComponent(factorSetId)}/freeze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actor: "local_user", reason }),
    }),
  deprecateFactorSet: (factorSetId: string, reason: string) =>
    requestJson<FactorSet>(`${API}/factor-sets/${encodeURIComponent(factorSetId)}/deprecate`, {
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

  // WP3: Factor library CRUD
  listFactorDefinitions: (params: {
    lifecycle_status?: string;
    origin?: string;
    factor_kind?: string;
    category?: string;
    search?: string;
    page?: number;
    page_size?: number;
  } = {}) => {
    const sp = new URLSearchParams();
    if (params.lifecycle_status) sp.set("lifecycle_status", params.lifecycle_status);
    if (params.origin) sp.set("origin", params.origin);
    if (params.factor_kind) sp.set("factor_kind", params.factor_kind);
    if (params.category) sp.set("category", params.category);
    if (params.search) sp.set("search", params.search);
    if (params.page) sp.set("page", String(params.page));
    if (params.page_size) sp.set("page_size", String(params.page_size));
    const suffix = sp.toString() ? `?${sp.toString()}` : "";
    return requestJson<FactorDefinitionListResponse>(`${API}/factors${suffix}`);
  },
  getFactorDefinition: (factorCode: string) =>
    requestJson<FactorDefinition>(`${API}/factors/${encodeURIComponent(factorCode)}`),
  listFactorVersions: (factorCode: string) =>
    requestJson<FactorVersionListItem[]>(`${API}/factors/${encodeURIComponent(factorCode)}/versions`),
  createFactorDraft: (payload: FactorDraftPayload) =>
    requestJson<FactorDefinition>(`${API}/factors`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  createFactorVersion: (factorCode: string, payload: FactorVersionPayload) =>
    requestJson<FactorVersionDefinition>(`${API}/factors/${encodeURIComponent(factorCode)}/versions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  getFactorReferences: (factorCode: string, versionId: number) =>
    requestJson<FactorReferenceInfo>(`${API}/factors/${encodeURIComponent(factorCode)}/references?version_id=${versionId}`),
  executeFactorTransition: (factorCode: string, payload: FactorTransitionPayload) =>
    requestJson<FactorTransitionResult>(`${API}/factors/${encodeURIComponent(factorCode)}/transitions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  getFactorTransitionHistory: (factorCode: string) =>
    requestJson<FactorTransitionAudit[]>(`${API}/factors/${encodeURIComponent(factorCode)}/transitions`),
  validateFactorFormula: (payload: FactorValidatePayload) =>
    requestJson<FactorValidateResult>(`${API}/factors/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  previewFactorFormula: (payload: FactorPreviewPayload) =>
    requestJson<FactorPreviewResult>(`${API}/factors/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),

  // WP5-07: 评估实验室 API
  preflightFactorEvaluation: (params: PreflightFactorEvaluationParams) => {
    const body: Record<string, unknown> = { factor_code: params.factorCode };
    if (params.factorVersionId != null) body.factor_version_id = params.factorVersionId;
    if (params.universe != null) body.universe = params.universe;
    if (params.startDate != null) body.start_date = params.startDate;
    if (params.endDate != null) body.end_date = params.endDate;
    if (params.targetHorizon != null) body.target_horizon = params.targetHorizon;
    return requestJson<PreflightResponse>(`${API}/factor-evaluation/preflight`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },
  createEvaluationTask: (payload: EvaluationTaskCreatePayload) =>
    requestJson<EvaluationTaskRead>(`${API}/factor-evaluation/tasks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  listEvaluationTasks: (limit: number = 20) =>
    requestJson<EvaluationTaskRead[]>(`${API}/factor-evaluation/tasks?limit=${limit}`),
  getEvaluationTask: (taskId: string) =>
    requestJson<EvaluationTaskRead>(`${API}/factor-evaluation/tasks/${encodeURIComponent(taskId)}`),
  cancelEvaluationTask: (taskId: string) =>
    requestJson<EvaluationTaskRead>(`${API}/factor-evaluation/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" }),
  listEvaluationRuns: (params: { factorVersionId?: number; gateResult?: string; limit?: number } = {}) => {
    const sp = new URLSearchParams();
    if (params.factorVersionId != null) sp.set("factor_version_id", String(params.factorVersionId));
    if (params.gateResult) sp.set("gate_result", params.gateResult);
    sp.set("limit", String(params.limit ?? 20));
    return requestJson<EvaluationRunRead[]>(`${API}/factor-evaluation/runs?${sp.toString()}`);
  },
  getEvaluationRun: (runId: string) =>
    requestJson<EvaluationRunRead>(`${API}/factor-evaluation/runs/${encodeURIComponent(runId)}`),

  // WP6-06: Shadow 观测与审批 API
  listShadowObservations: (factorVersionId: number, params: { startDate?: string; endDate?: string; validOnly?: boolean; limit?: number } = {}) => {
    const sp = new URLSearchParams();
    sp.set("factor_version_id", String(factorVersionId));
    if (params.startDate) sp.set("start_date", params.startDate);
    if (params.endDate) sp.set("end_date", params.endDate);
    if (params.validOnly) sp.set("valid_only", "true");
    sp.set("limit", String(params.limit ?? 200));
    return requestJson<ShadowObservationRead[]>(`${API}/factor-shadow/observations?${sp.toString()}`);
  },
  getShadowObservationSummary: (factorVersionId: number) =>
    requestJson<ShadowObservationSummary>(`${API}/factor-shadow/observations/summary?factor_version_id=${factorVersionId}`),
  getShadowHealth: (factorVersionId: number) =>
    requestJson<ShadowHealthReport>(`${API}/factor-shadow/health/${factorVersionId}`),
  requestActivation: (payload: { factor_id: number; factor_version_id: number; evidence_run_id?: string; actor?: string; reason?: string }) =>
    requestJson<ActivationResult>(`${API}/factor-shadow/activation/request`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  approveActivation: (payload: { factor_id: number; factor_version_id: number; approver?: string; reason: string; evidence_run_id?: string; request_id?: string }) =>
    requestJson<ActivationResult>(`${API}/factor-shadow/activation/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  rejectActivation: (payload: { factor_id: number; reviewer?: string; reason: string; request_id?: string }) =>
    requestJson<ActivationResult>(`${API}/factor-shadow/activation/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  quarantineFactor: (factorId: number, payload: { factor_version_id: number; reason: string; request_id?: string }) =>
    requestJson<ActivationResult>(`${API}/factor-shadow/quarantine/${factorId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),

  // Portfolios
  getPortfolios: () => requestJson<any[]>(`${API}/portfolios`),
  // 读取单个组合详情（含 active_rule / allocation），供总览页配置值回显
  getPortfolioDetail: (portfolioId: number) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}`),
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
  // P0-AutoTrade：自动交易就绪检查（for_schedule=true 时把调度状态升级为 blocker）
  getAutoTradeReadiness: (portfolioId: number, options?: { for_schedule?: boolean }) => {
    const params = new URLSearchParams();
    if (options?.for_schedule) params.set("for_schedule", "1");
    const qs = params.toString();
    return requestJson<any>(
      `${API}/portfolios/${portfolioId}/auto-trade/readiness${qs ? `?${qs}` : ""}`,
      { timeoutMs: 30000 },
    );
  },
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
  repairAllSymbolMarketData: (payload: {
    symbol_ids: number[];
    start_date?: string | null;
    end_date?: string | null;
    adjust?: string;
    auto_score?: boolean;
  }) =>
    requestJson(`${API}/market-data/repair/batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs: 300000,
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
  // P1-FIX: 组合成员 API
  getMembers: (portfolioId: number) =>
    requestJson<any[]>(`${API}/portfolios/${portfolioId}/members`),
  getPortfolioCandidates: (portfolioId: number) =>
    requestJson<any[]>(`${API}/portfolios/${portfolioId}/candidates`),
  addPortfolioCandidate: (portfolioId: number, payload: unknown) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}/candidates`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  addPortfolioMember: (portfolioId: number, payload: unknown) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}/members`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  updatePortfolioMember: (portfolioId: number, memberId: number, payload: unknown) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}/members/${memberId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  archivePortfolioMember: (portfolioId: number, memberId: number, force = false) =>
    requestJson<any>(`${API}/portfolios/${portfolioId}/members/${memberId}/archive`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ force }) }),

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
  getDiscoveryCandidatePools: (
    pool: "all" | "factor" | "technical" | "theme" = "all",
    minScore = 0,
    limit = 100,
    assetType?: "stock" | "etf",
    region?: "cn" | "hk" | "us" | "other",
    board?: "sh_main" | "sz_main" | "chinext" | "star" | "bse",
  ) =>
    requestJson<any>(`${API}/discovery/candidate-pools?pool=${pool}&min_score=${minScore}&limit=${limit}${assetType ? `&asset_type=${assetType}` : ""}${region ? `&region=${region}` : ""}${board ? `&board=${board}` : ""}`),
  getInvestmentThemes: (activeOnly = true) =>
    requestJson<any[]>(`${API}/investment-themes?active_only=${activeOnly ? "true" : "false"}`),
  createInvestmentTheme: (payload: unknown) =>
    requestJson<any>(`${API}/investment-themes`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  mapInvestmentThemeSymbol: (themeId: number, payload: unknown) =>
    requestJson<any>(`${API}/investment-themes/${themeId}/symbols`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  addInvestmentThemeCatalyst: (themeId: number, payload: unknown) =>
    requestJson<any>(`${API}/investment-themes/${themeId}/catalysts`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),

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

  // UAT-PAGES.2：已排除池 / 扫描记录 / 排除恢复 API
  listExcludedCandidates: (params: {
    symbol?: string;
    name?: string;
    excludeDateFrom?: string;
    excludeDateTo?: string;
    reasonType?: string;
    limit?: number;
    offset?: number;
  } = {}) => {
    const sp = new URLSearchParams();
    if (params.symbol) sp.set("symbol", params.symbol);
    if (params.name) sp.set("name", params.name);
    if (params.excludeDateFrom) sp.set("exclude_date_from", params.excludeDateFrom);
    if (params.excludeDateTo) sp.set("exclude_date_to", params.excludeDateTo);
    if (params.reasonType) sp.set("reason_type", params.reasonType);
    sp.set("limit", String(params.limit ?? 100));
    sp.set("offset", String(params.offset ?? 0));
    return requestJson<any[]>(`${API}/discovery/excluded?${sp.toString()}`);
  },
  excludeDiscoveryCandidate: (candidateId: number, payload: { reason?: string; actorType?: string } = {}) =>
    requestJson<{ ok: boolean; candidate_id: number; event_id?: number; excluded_at?: string; already_excluded: boolean }>(
      `${API}/discovery/candidates/${candidateId}/exclude`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: payload.reason, actor_type: payload.actorType }),
      }
    ),
  restoreDiscoveryCandidate: (candidateId: number, payload: {
    target?: "candidate" | "observation";
    watchlistId?: number;
    note?: string;
    priority?: number;
    tags?: string[];
    targetPortfolioId?: number;
    actorType?: string;
  } = {}) =>
    requestJson<{ ok: boolean; candidate_id: number; event_id?: number | null; restored_at?: string | null; target: string; observation_item_id?: number | null; already_restored: boolean }>(
      `${API}/discovery/candidates/${candidateId}/restore`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target: payload.target,
          watchlist_id: payload.watchlistId,
          note: payload.note,
          priority: payload.priority,
          tags: payload.tags,
          target_portfolio_id: payload.targetPortfolioId,
          actor_type: payload.actorType,
        }),
      }
    ),
  listScanRuns: (params: {
    scope?: string;
    tradeDate?: string;
    status?: string;
    limit?: number;
    offset?: number;
  } = {}) => {
    const sp = new URLSearchParams();
    if (params.scope) sp.set("scope", params.scope);
    if (params.tradeDate) sp.set("trade_date", params.tradeDate);
    if (params.status) sp.set("status", params.status);
    sp.set("limit", String(params.limit ?? 50));
    sp.set("offset", String(params.offset ?? 0));
    return requestJson<any[]>(`${API}/discovery/scan-runs?${sp.toString()}`);
  },
  getScanRunDetail: (scanRunId: number) =>
    requestJson<any>(`${API}/discovery/scan-runs/${scanRunId}`),

  // WP-P-FIX.1: 数据准备任务与快照状态
  startDataPrep: (payload: { scope: string; trigger_fast_scan_after_ready?: boolean; fast_scan_params?: Record<string, unknown> }) =>
    requestJson<AsyncTaskRead>(`${API}/discovery/data-prep`, {
      method: "POST",
      body: JSON.stringify(payload),
      headers: { "Content-Type": "application/json" },
    }),
  getSnapshotStatus: (scope: string) =>
    requestJson<SnapshotStatusRead>(`${API}/discovery/snapshot/status?scope=${encodeURIComponent(scope)}`),

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
  startUniverseIncrementalSync: (maxWorkers = 5, scopes: string[] | null = null) =>
    requestJson<any>(`${API}/universe/incremental-sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_workers: maxWorkers, scopes }),
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

  // ── 指数日线同步（回测基准曲线数据：index_prices 表）──
  listIndexPrices: (symbol: string, params?: { start_date?: string; end_date?: string; limit?: number }) => {
    const q = new URLSearchParams();
    q.set("symbol", symbol);
    if (params?.start_date) q.set("start_date", params.start_date);
    if (params?.end_date) q.set("end_date", params.end_date);
    if (params?.limit != null) q.set("limit", String(params.limit));
    return requestJson<any>(`${API}/index-prices?${q.toString()}`);
  },
  getIndexPricesStatus: (symbols?: string) => {
    const url = symbols ? `${API}/index-prices/status?symbols=${encodeURIComponent(symbols)}` : `${API}/index-prices/status`;
    return requestJson<{
      items: Array<{
        symbol: string; name: string; bar_count: number;
        first_date: string | null; last_date: string | null;
        freshness_days: number | null; linearity_dev_pct: number | null;
      }>;
    }>(url);
  },
  syncIndexPrices: (payload: { symbols?: string[]; history_days?: number; end_date?: string }) =>
    requestJson<IndexSyncTaskRead>(`${API}/index-prices/sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs: 90_000,  // 专用 90s 提交超时（防 Windows Defender/线程池排队 > 默认 20s）
      dedupe: false,
    }),
  getIndexPricesSyncTask: (taskId: string) =>
    requestJson<IndexSyncTaskRead>(`${API}/index-prices/sync-tasks/${encodeURIComponent(taskId)}`, {
      timeoutMs: 30_000,
      dedupe: false,
    }),
  syncAllBenchmarkIndices: () =>
    requestJson<IndexSyncTaskRead>(`${API}/index-prices/sync-all-benchmarks`, {
      method: "POST",
      timeoutMs: 90_000,
      dedupe: false,
    }),

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
  runPortfolioBacktest: (portfolioId: number, payload: { start_date: string; end_date: string; run_name?: string; only_auto?: boolean; current_universe?: boolean; benchmark?: string }) =>
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
  promoteIndicatorToFactor: (id: number, payload: {
    code?: string; name?: string; category?: string;
    direction?: "higher_better" | "lower_better" | "nonlinear";
    factor_kind?: "continuous" | "event" | "regime";
    risk_level?: "low" | "medium" | "high";
    description?: string; thesis?: string; change_note?: string;
  } = {}) =>
    requestJson<CustomIndicatorPromoteResponse>(`${API}/settings/custom-indicators/${id}/promote-to-factor`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),


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
  getTaskObservability: () =>
    requestJson<{ generated_at: string; domains: Record<string, { slot_limit: number; running: number; queued: number; waiting: number; available_slots: number }>; active_tasks: any[] }>(`${API}/system/task-observability`),
  getTaskBatch: (taskId: string) =>
    requestJson<any>(`${API}/system/tasks/${encodeURIComponent(taskId)}/batch`),

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
  getExternalDataOverview: () =>
    requestJson<ExternalDataOverview>(`${API}/external-data/overview`),
  getExternalDataCoverage: () =>
    requestJson<ExternalDataCoverage>(`${API}/external-data/coverage`),
  getExternalDataQualitySnapshots: () =>
    requestJson<{ captured_at: string | null; fields: DataQualitySnapshot[] }>(`${API}/external-data/quality-snapshots`),
  getExternalDataQualityHistory: (field: string, limit: number = 30) =>
    requestJson<{ field: string; snapshots: DataQualitySnapshot[] }>(`${API}/external-data/quality-snapshots/${encodeURIComponent(field)}?limit=${limit}`),
  refreshExternalDataQualitySnapshots: () =>
    requestJson<{ captured_at: string; fields: number; trigger: string }>(`${API}/external-data/quality-snapshots/refresh`, { method: "POST" }),
  getExternalDataGaps: (
    dataset: "fundamental" | "financial" | "capital_flow",
    startDate: string,
    endDate: string,
    limit: number = 200,
  ) => requestJson<ExternalDataGapReport>(
    `${API}/external-data/gaps?dataset=${dataset}&start_date=${startDate}&end_date=${endDate}&limit=${limit}`,
  ),
  repairExternalDataGaps: (payload: {
    dataset: "fundamental" | "financial" | "capital_flow";
    start_date: string;
    end_date: string;
    gaps: ExternalDataGapItem[];
  }) => requestJson<ExternalSyncTask>(`${API}/external-data/gaps/repair`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }),
  importTailProxyMinutes: (payload: { csv_text: string; filename?: string }) =>
    requestJson<{ source: string; written_sessions: number; rejected_sessions: number; rejected: Array<{ symbol: string; trade_date: string; reason: string }>; formula_evaluation: string }>(`${API}/external-data/tail-proxy/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  startExternalDataSync: (payload: {
    dataset: ExternalSyncDataset;
    source: "watchlist" | "positions" | "all";
    include_northbound?: boolean;
    mode?: "incremental" | "backfill";
    start_date?: string;
    end_date?: string;
    lookback_days?: number;
    limit?: number;
    max_workers?: number;
  }) =>
    requestJson<ExternalSyncTask>(`${API}/external-data/sync-tasks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  getExternalDataSyncCapabilities: () =>
    requestJson<ExternalDataSyncCapabilities>(`${API}/external-data/sync-capabilities`),
  previewExternalDataSyncPlan: (payload: {
    dataset: ExternalSyncDataset;
    source: "watchlist" | "positions" | "all";
    include_northbound?: boolean;
    mode?: "incremental" | "backfill";
    start_date?: string;
    end_date?: string;
    lookback_days?: number;
    limit?: number;
    max_workers?: number;
  }) => requestJson<ExternalDataSyncPlan>(`${API}/external-data/sync-plans/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }),
  getExternalDataSyncTask: (taskId: string) =>
    requestJson<ExternalSyncTask>(`${API}/external-data/sync-tasks/${encodeURIComponent(taskId)}`),
  getExternalDataSyncTaskPartitions: (taskId: string) =>
    requestJson<ExternalSyncPlanDetails>(`${API}/external-data/sync-tasks/${encodeURIComponent(taskId)}/partitions`),
  cancelExternalDataSyncTask: (taskId: string) =>
    requestJson<ExternalSyncTask>(`${API}/external-data/sync-tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: "POST",
    }),
  retryExternalDataSyncTask: (taskId: string) =>
    requestJson<ExternalSyncTask>(`${API}/external-data/sync-tasks/${encodeURIComponent(taskId)}/retry`, {
      method: "POST",
    }),

  // Legacy blocking endpoints kept for compatibility with existing callers.
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
  streamAIChat,
  getAISessions: (limit: number = 20, offset: number = 0) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return requestJson<{ items: AISession[]; limit: number; offset: number; include_archived: boolean }>(
      `${API}/ai/sessions?${params.toString()}`,
    );
  },
  getAISession: (sessionId: number) =>
    requestJson<AISession>(`${API}/ai/sessions/${sessionId}`),
  streamAISession,
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

  // WP4-05: AI 草案确认流程（通用 confirm/preview/reject/execute）
  getAIDraft: (auditId: number) =>
    requestJson<AIDraftDetail>(`${API}/ai/drafts/${auditId}`),
  previewAIDraft: (auditId: number, modifiedPayload?: Record<string, unknown>) =>
    requestJson<AIDraftPreviewResult>(`${API}/ai/drafts/${auditId}/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(modifiedPayload ? { modified_payload: modifiedPayload } : {}),
    }),
  confirmAIDraft: (auditId: number, modifiedPayload?: Record<string, unknown>) =>
    requestJson<AIDraftDetail>(`${API}/ai/drafts/${auditId}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(modifiedPayload ? { modified_payload: modifiedPayload } : {}),
    }),
  rejectAIDraft: (auditId: number, reason?: string) =>
    requestJson<AIDraftDetail>(`${API}/ai/drafts/${auditId}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reason ? { reason } : {}),
    }),
  executeAIDraft: (auditId: number) =>
    requestJson<AIDraftExecuteResult>(`${API}/ai/drafts/${auditId}/execute`, { method: "POST" }),

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

  // P2-FIX: 通知下拉使用的 inbox API（替代原纯本地 INITIAL_NOTIFICATIONS）
  // 失败时调用方兜底本地态即可，保证不影响整体使用
  getInboxNotifications: (params: { limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.limit != null) q.set("limit", String(params.limit));
    const suffix = q.toString() ? `?${q.toString()}` : "";
    return requestJson<
      Array<{
        id: number;
        type: "success" | "warning" | "error" | "info";
        title: string;
        description: string;
        time: string;
        created_at: string | null;
        read: boolean;
      }>
    >(`${API}/notifications/inbox${suffix}`);
  },
  markInboxItemRead: (itemId: number) =>
    requestJson<{ ok: boolean; id: number; read: boolean }>(`${API}/notifications/inbox/${itemId}/read`, { method: "PUT" }),
  markInboxAllRead: () =>
    requestJson<{ ok: boolean; count: number }>(`${API}/notifications/inbox/read-all`, { method: "PUT" }),
  viewAllInbox: () =>
    requestJson<{ ok: boolean; total: number; hint?: string }>(`${API}/notifications/inbox/view-all`, { method: "POST" }),
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

// WP7-06: FactorSet 类型
export interface FactorSetMember {
  id: number;
  factor_set_id: string;
  factor_id: number;
  factor_version_id: number;
  factor_code: string;
  factor_version: number;
  role: string;
  weight_constraint: string | null;
  display_order: number;
  missing_policy: string;
  excluded_reason: string | null;
}

export type FactorSetStatus = "draft" | "frozen" | "deprecated";

export interface FactorSet {
  id: string;
  name: string;
  description: string | null;
  content_hash: string | null;
  status: FactorSetStatus;
  frozen_at: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  n_members: number;
  members: FactorSetMember[];
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

// ══════════════════════════════════════════════════════════
// WP5-07: 评估实验室类型
// ══════════════════════════════════════════════════════════

export interface EvaluationTaskCreatePayload {
  factor_code: string;
  factor_kind?: "continuous" | "event" | "regime";
  factor_version_id?: string | number | null;
  universe?: string;
  start_date?: string | null;
  end_date?: string | null;
  target_horizon?: number;
  n_groups?: number;
  cost_rate?: number;
  direction?: "higher_better" | "lower_better" | "nonlinear" | string;
  created_by?: string;
}

export interface PreflightFixLink {
  tab?: string;
  subtab?: string;
  label_zh?: string;
}

export interface PreflightCheckItem {
  code: string;
  severity: "pass" | "warn" | "error" | "info";
  category: string;
  title_zh: string;
  detail_zh: string;
  evidence: Record<string, unknown>;
  fix_link?: PreflightFixLink | null;
  retryable: boolean;
}

export interface PreflightOverall {
  passed: boolean;
  blocking_count: number;
  recommended_date_range?: [string, string] | null;
}

export interface PreflightResponse {
  overall: PreflightOverall;
  items: PreflightCheckItem[];
}

export interface PreflightFactorEvaluationParams {
  factorCode: string;
  factorVersionId?: string;
  universe?: string;
  startDate?: string;
  endDate?: string;
  targetHorizon?: number;
}

/** 评估异步任务（结构与 AsyncTaskRead 对齐，复用通用任务协议字段） */
export interface EvaluationTaskRead {
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
  error_code?: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string | null;
  heartbeat_at?: string | null;
  suggested_action?: string | null;
  cancel_requested?: boolean;
}

/** 评估运行基础指标（IC/分组/换手/成本） */
export interface EvaluationMetrics {
  // 基础 IC 指标
  rank_ic_mean?: number;
  rank_ic_median?: number;
  rank_ic_std?: number;
  icir?: number;
  positive_ic_ratio?: number;
  coverage?: number;
  n_samples?: number;
  // 分组单调性
  quantile_returns?: number[];
  monotonicity_score?: number;
  long_short_return?: number;
  // 换手与成本
  turnover?: number;
  cost_adjusted_return?: number;
  // 时间切分
  train_start?: string;
  train_end?: string;
  validation_start?: string;
  validation_end?: string;
  // 压力测试（可选，由 worker 写入）
  stress_test?: StressTestSummary;
  [key: string]: unknown;
}

/** 参数扰动结果 */
export interface ParameterPerturbationResult {
  param_name: string;
  baseline_value: number;
  verdict: "stable" | "unstable" | "cliff_drop" | string;
  sign_consistency_ratio: number;
  median_ic_ratio: number;
  passing_neighbor_count: number;
  has_cliff_drop: boolean;
  points: Array<{
    label: string;
    ratio: number;
    param_value: number;
    ic_mean: number;
    icir: number;
    passed_min_gate: boolean;
  }>;
}

/** 时间段扰动结果 */
export interface TimePerturbationResult {
  ic_stability: number;
  verdict: "stable" | "unstable" | string;
  segments: Array<{
    segment_label: string;
    ic_mean: number;
    icir: number;
  }>;
}

/** 缺失敏感度结果 */
export interface MissingPerturbationResult {
  ic_decay_ratio: number;
  verdict: "stable" | "unstable" | string;
}

/** 压力测试汇总 */
export interface StressTestSummary {
  overall_verdict: "stable" | "unstable" | string;
  failure_reasons: string[];
  parameter_results: ParameterPerturbationResult[];
  time_result: TimePerturbationResult | null;
  missing_result: MissingPerturbationResult | null;
}

/** 评估运行记录（不可变） */
export interface EvaluationRunRead {
  id: string;
  factor_version_id: number;
  universe_snapshot_id: string | null;
  data_cutoff_at: string | null;
  target_code: string;
  train_start_date: string | null;
  train_end_date: string | null;
  validation_start_date: string | null;
  validation_end_date: string | null;
  config: Record<string, unknown>;
  metrics: EvaluationMetrics;
  gate_result: "passed" | "rejected" | "warn" | null;
  rejection_reasons: string[];
  artifact_path: string | null;
  task_id: string | null;
  created_by: string;
  // WPD-02 完整交易日证据
  selected_trade_date: string | null;
  observed_symbols: number | null;
  expected_symbols: number | null;
  completeness_ratio: number | null;
  fallback_reason: string | null;
  created_at: string | null;
}

// ══════════════════════════════════════════════════════════
// WP6-06: Shadow 观测与审批类型
// ══════════════════════════════════════════════════════════

/** Shadow 每日观测记录 */
export interface ShadowObservationRead {
  id: number;
  trade_date: string;
  is_valid_day: boolean;
  invalid_reason: string | null;
  ic_value: number | null;
  coverage: number | null;
  turnover: number | null;
  completeness_ratio: number | null;
  observed_symbols: number | null;
  expected_symbols: number | null;
  health_status: "healthy" | "degraded" | "blocked" | string;
  health_reason: string | null;
  metrics: Record<string, unknown>;
}

/** 观察期汇总 */
export interface ShadowObservationSummary {
  factor_version_id: number;
  valid_days: number;
  min_required_days: number;
  is_complete: boolean;
  reason: string | null;
}

/** 健康告警 */
export interface HealthAlert {
  alert_type: string;
  severity: "warn" | "critical" | string;
  message: string;
  current_value?: number | null;
  threshold?: number | null;
  window_days?: number | null;
}

/** Shadow 健康报告 */
export interface ShadowHealthReport {
  factor_version_id: number;
  health_status: "healthy" | "degraded" | "blocked" | string;
  alerts: HealthAlert[];
  recent_ic_mean: number | null;
  recent_ic_std: number | null;
  historical_ic_mean: number | null;
  recent_coverage_mean: number | null;
  historical_coverage_mean: number | null;
  n_valid_days: number;
  n_total_days: number;
  should_quarantine: boolean;
}

/** 激活审批结果 */
export interface ActivationResult {
  success: boolean;
  factor_id: number;
  from_status: string | null;
  to_status: string;
  actor?: string;
  audit_id: number | null;
  error: string | null;
  valid_days?: number;
  min_required_days?: number;
}

/** 因子簇 */
export interface FactorClusterRead {
  cluster_id: number;
  members: string[];
  representative: string;
  intra_max_correlation: number;
  mean_correlation: number;
}

/** 相关性治理报告 */
export interface CorrelationGovernanceReport {
  correlation_matrix: {
    matrix: Record<string, Record<string, number>>;
    method: string;
    n_dates: number;
    n_symbols_avg: number;
    factor_codes: string[];
    high_correlation_pairs: Array<[string, string, number]>;
  };
  cluster_report: {
    clusters: FactorClusterRead[];
    threshold: number;
    n_factors: number;
    n_clusters: number;
    orphans: string[];
  };
  residual_results: Array<{
    candidate_code: string;
    reference_codes: string[];
    residual_ic_mean: number;
    residual_icir: number;
    residual_ic_tstat: number;
    original_ic_mean: number;
    original_icir: number;
    incremental_ic_ratio: number;
    has_incremental_value: boolean;
    verdict: "valuable" | "marginal" | "redundant" | string;
    reasons: string[];
  }>;
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
  formula_mode?: "indicator" | "factor";
  /** 因子编辑器上下文：方向 */
  factor_direction?: string;
  /** 因子编辑器上下文：版本说明 */
  factor_change_note?: string;
  /** 因子编辑器上下文：参数JSON文本 */
  factor_params_text?: string;
}

export interface AiChatResult {
  ok: boolean;
  reply: string;
  error: string;
}

// ----------------------------------------------------------------------------
// WP3: Factor library definition types
// ----------------------------------------------------------------------------

export type FactorLifecycleStatus =
  | "draft" | "candidate" | "testing" | "shadow"
  | "active" | "quarantined" | "deprecated" | "rejected";

export type FactorOrigin = "system" | "user" | "ai_assisted" | "imported";
export type FactorKind = "continuous" | "event" | "regime";

export interface FactorDefinition {
  id: number;
  code: string;
  name: string;
  category: string;
  direction: string;
  status: string | null;
  is_active: number | null;
  source_type: string | null;
  frequency: string | null;
  default_missing_policy: string;
  description: string | null;
  formula_expr: string | null;
  origin: string | null;
  lifecycle_status: string | null;
  owner: string | null;
  thesis: string | null;
  factor_kind: string | null;
  asset_scope: string[] | null;
  active_version_id: number | null;
  shadow_version_id: number | null;
  risk_level: string | null;
  archived_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface FactorDefinitionListResponse {
  items: FactorDefinition[];
  total: number;
  page: number;
  page_size: number;
}

export interface FactorVersionDefinition {
  id: number;
  factor_id: number;
  version: number;
  formula_expr: string;
  params: Record<string, unknown>;
  direction: string;
  source_mapping: Record<string, unknown>;
  effective_from: string | null;
  change_note: string;
  is_latest: number;
  created_at: string | null;
  formula_ast: Record<string, unknown> | null;
  postprocess: Record<string, unknown> | null;
  parameter_schema: Record<string, unknown> | null;
  data_dependencies: Record<string, unknown> | null;
  compiler_version: string | null;
  execution_plan_hash: string | null;
  complexity_score: number | null;
  created_by: string | null;
  created_via: string | null;
  validation_status: string | null;
  validation_errors: Array<Record<string, unknown>> | null;
}

export interface FactorVersionListItem {
  factor_code: string;
  version: number;
  formula_expr: string;
  params: Record<string, unknown>;
  direction: string;
  source_mapping: Record<string, unknown>;
  effective_from: string | null;
  change_note: string;
  is_latest: boolean;
  created_at: string | null;
}

export interface FactorVersionListItem {
  factor_code: string;
  version: number;
  formula_expr: string;
  params: Record<string, unknown>;
  direction: string;
  source_mapping: Record<string, unknown>;
  effective_from: string | null;
  change_note: string;
  is_latest: boolean;
  created_at: string | null;
}

export interface FactorReferenceInfo {
  factor_code: string;
  factor_version: number;
  referenced_by_evaluations: number;
  referenced_by_factor_sets: number;
  referenced_by_model_runs: number;
  is_immutable: boolean;
}

export interface FactorTransitionAudit {
  id: number;
  factor_id: number;
  factor_version_id: number | null;
  from_status: string | null;
  to_status: string;
  actor: string;
  reason: string | null;
  evidence_run_id: string | null;
  request_id: string | null;
  migration_note: string | null;
  created_at: string;
}

export interface FactorTransitionResult {
  factor_id: number;
  from_status: string | null;
  to_status: string;
  action: string;
  actor: string;
  reason: string | null;
  audit_id: number;
  success: boolean;
  error: string | null;
}

export interface FactorDraftPayload {
  code: string;
  name: string;
  category: string;
  direction: "higher_better" | "lower_better" | "nonlinear";
  factor_kind: FactorKind;
  description?: string;
  thesis?: string;
  owner?: string;
  risk_level: "low" | "medium" | "high";
  asset_scope: string[];
  default_missing_policy: "exclude" | "impute_zero" | "ignore";
  frequency?: string;
}

export interface FactorVersionPayload {
  formula_expr: string;
  params?: Record<string, unknown>;
  direction?: "higher_better" | "lower_better" | "nonlinear";
  source_mapping?: Record<string, unknown>;
  postprocess?: Record<string, unknown> | null;
  parameter_schema?: Record<string, unknown> | null;
  change_note?: string;
  created_by?: string;
  created_via?: "manual" | "template" | "ai" | "import";
}

export interface FactorTransitionPayload {
  action: "submit_candidate" | "start_testing" | "reject" | "revoke_to_draft" | "deprecate";
  actor?: string;
  reason?: string;
  evidence_run_id?: string;
  request_id?: string;
}

export interface FactorValidatePayload {
  formula_expr: string;
  params?: Record<string, unknown>;
  direction?: "higher_better" | "lower_better" | "nonlinear";
  postprocess?: Record<string, unknown> | null;
  source_mapping?: Record<string, unknown>;
}

export interface FactorFormulaDiagnostic {
  error_code: string;
  message: string;
  detail: Record<string, unknown>;
  start: number | null;
  end: number | null;
  line: number | null;
  column: number | null;
  token: string | null;
}

export interface FactorValidateResult {
  is_valid: boolean;
  execution_plan: Record<string, unknown> | null;
  errors: FactorFormulaDiagnostic[];
  data_dependencies: Record<string, unknown> | null;
}

export interface FactorPreviewPayload {
  formula_expr: string;
  params?: Record<string, unknown>;
  direction?: "higher_better" | "lower_better" | "nonlinear";
  postprocess?: Record<string, unknown> | null;
  source_mapping?: Record<string, unknown>;
  trade_date?: string | null;
  symbols?: string[] | null;
  max_symbols?: number;
}

export interface FactorPreviewValueItem {
  symbol: string;
  trade_date: string;
  raw_value: number | null;
  processed_value?: number | null;
  winsorized_value: number | null;
  normalized_value: number | null;
  eligible: boolean;
  data_source?: string | null;
  missing_reason?: string | null;
}

export interface FactorPreviewResult {
  is_valid: boolean;
  execution_plan: Record<string, unknown> | null;
  errors: FactorFormulaDiagnostic[];
  data_cutoff_at: string | null;
  selected_trade_date: string | null;
  complete_trade_day_evidence: Record<string, unknown> | null;
  data_readiness: Record<string, unknown> | null;
  data_dependencies: Record<string, unknown> | null;
  values: FactorPreviewValueItem[];
  missing_reasons: Record<string, string>;
  attempted_count: number;
  valid_count: number;
  missing_count: number;
  coverage_rate: number;
  missing_rate: number;
  distribution: Record<string, number | null>;
  outlier_count: number;
  elapsed_ms: number;
  data_fix_links: Array<{
    section: "universe" | "external";
    source_table: string;
    label: string;
  }>;
  evaluation_supported: boolean;
  evaluation_mode: "continuous" | "event" | "snapshot" | "mixed" | string;
  blocking_fields: FactorFormulaFieldReadiness[];
  readiness_warnings: FactorFormulaFieldReadiness[];
}

export interface FactorFormulaFieldReadiness {
  field?: string;
  availability: "available" | "limited" | "event" | "snapshot" | "blocked" | "unknown";
  data_mode: "continuous" | "point_in_time" | "event" | "snapshot" | "derived" | "blocked" | string;
  reason?: string;
  status_reason?: string;
  first_date?: string | null;
  latest_date?: string | null;
  nonnull_rows?: number;
  distinct_symbols?: number;
  distinct_dates?: number;
}

export interface FactorFormulaCatalogField {
  key: string;
  label_zh: string;
  label_en: string;
  description: string;
  dtype: string;
  source_table: string;
  layer: string;
  point_in_time: boolean;
  snippet: string;
  enabled: boolean;
  availability: FactorFormulaFieldReadiness["availability"];
  data_mode: FactorFormulaFieldReadiness["data_mode"];
  evaluation_enabled: boolean;
  preview_enabled: boolean;
  draft_enabled: boolean;
  status_reason: string;
  table_rows: number;
  nonnull_rows: number;
  distinct_symbols: number;
  distinct_dates: number;
  first_date: string | null;
  latest_date: string | null;
  derived: boolean;
  derived_from: string[];
}

export interface FactorFormulaCatalogFunction {
  key: string;
  label_zh: string;
  label_en: string;
  description?: string;
  category: string;
  signature: string;
  snippet: string;
  params?: string[];
  min_args?: number;
  max_args?: number;
  window_arg_index?: number | null;
  enabled: boolean;
  disabled_reason?: string;
}

export interface FactorFormulaCatalogOperator {
  key: string;
  label: string;
  snippet: string;
  description: string;
}

export interface FactorFormulaCatalogTemplate {
  key: string;
  name_zh: string;
  name_en: string;
  formula: string;
}

export interface FactorFormulaCatalog {
  dsl_version: string;
  compiler_version: string;
  fields: FactorFormulaCatalogField[];
  functions: FactorFormulaCatalogFunction[];
  disabled_functions: FactorFormulaCatalogFunction[];
  operators: FactorFormulaCatalogOperator[];
  templates: FactorFormulaCatalogTemplate[];
}
