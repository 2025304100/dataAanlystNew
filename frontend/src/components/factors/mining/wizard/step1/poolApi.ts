/**
 * Step1 候选池 API（T28）—— 契约来源 `app/api/routes/mining_candidate_pool.py`。
 *
 * 后端候选池域（T08/T09/T11）**已完整实现**：19 条路由覆盖池 CRUD、筛选预览、
 * 导入、快照冻结/分析、成员增删。本模块按 `api/dbConfig.ts` 范式走
 * `requestJson` 单一入口，不做第二套 fetch。
 *
 * 关键口径（向导 §3）：
 * - `previewPool` **不落库**，仅用于底部实时统计；**防抖预览结果不得作为挖掘快照**；
 * - `createPoolFromFilter` 是唯一会物化成员的筛选入口，命中 0 / <50 /
 *   覆盖率不足 / 范围冲突一律 4xx 且不写任何东西；
 * - `createSnapshot(analyze=true)` 即「生成挖掘物料」，**分析完成才锁定**
 *   （冻结只写成员，不锁定）；
 * - 成员删除只解除池与标的的关联，**不删主数据**。
 */
import { requestJson } from "../../../../../api/client";

const BASE = "/api/v1/factor-mining/candidate-pools";
const JSON_HEADERS = { "Content-Type": "application/json" };

/** 有效标的硬下限（向导 §3.5：50 为系统硬下限，前端不得降低） */
export const MIN_POOL_SIZE = 50;

export interface PoolPreview {
  matched: number;
  total: number;
  excluded: number;
  by_rule: Array<{ rule: string; excluded: number }>;
  warnings: string[];
  blocked: null | { error_code: string; detail_zh: string };
}

export interface CandidatePool {
  id: string;
  name: string;
  source_type: "filter" | "import" | string;
  status?: string | null;
  description?: string | null;
}

export interface PoolSnapshot {
  id: string;
  pool_id?: string;
  is_locked?: boolean;
  analysis_json?: Record<string, unknown> | null;
  as_of_date?: string | null;
  analyzed_at?: string | null;
}

export interface PoolMember {
  symbol_id?: number;
  symbol: string;
  name?: string | null;
  market?: string | null;
  listed_at?: string | null;
  risk_flag?: string | null;
  latest_trade_date?: string | null;
  close?: number | null;
  total_market_cap?: number | null;
  pe_ttm?: number | null;
  pb?: number | null;
  roe_ttm?: number | null;
  loss_years?: number | null;
  avg_amount?: number | null;
  source?: string | null;
  reason?: string | null;
  integrity?: string | null;
}

export interface PoolPage<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** 条件筛选预览（**不落库**；300ms 防抖调用）。 */
export const previewPool = (
  filterConfig: Record<string, unknown>,
  asOfDate?: string,
): Promise<PoolPreview> =>
  requestJson<PoolPreview>(`${BASE}/preview`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ filter_config: filterConfig, as_of_date: asOfDate ?? null }),
  });

/** 按筛选结果生成候选池（含成员物化；4xx 时不写任何东西）。 */
export const createPoolFromFilter = (payload: {
  name: string;
  filter_config: Record<string, unknown>;
  description?: string | null;
  as_of_date?: string | null;
}): Promise<CandidatePool> =>
  requestJson<CandidatePool>(`${BASE}/from-filter`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });

/** 「生成挖掘物料」：冻结并分析（analyze=false 只冻结，不锁定）。 */
export const createSnapshot = (
  poolId: string,
  payload: { analyze?: boolean; as_of_date?: string | null } = {},
): Promise<PoolSnapshot> =>
  requestJson<PoolSnapshot>(`${BASE}/${encodeURIComponent(poolId)}/snapshot`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ analyze: payload.analyze ?? true, as_of_date: payload.as_of_date ?? null }),
  });

export const fetchLatestSnapshot = (poolId: string): Promise<PoolSnapshot | null> =>
  requestJson<PoolSnapshot | null>(
    `${BASE}/${encodeURIComponent(poolId)}/snapshot/latest`,
  );

/** 删除最新快照（「重新选择」= 解锁 + 清分析数据）。 */
export const deleteLatestSnapshot = (poolId: string): Promise<unknown> =>
  requestJson(`${BASE}/${encodeURIComponent(poolId)}/snapshot/latest`, {
    method: "DELETE",
  });

export const fetchMembers = (
  poolId: string,
  params: { page?: number; page_size?: number; keyword?: string } = {},
): Promise<PoolPage<PoolMember>> => {
  const qs = new URLSearchParams();
  if (params.page) qs.set("page", String(params.page));
  if (params.page_size) qs.set("page_size", String(params.page_size));
  if (params.keyword) qs.set("keyword", params.keyword);
  const suffix = qs.toString();
  return requestJson<PoolPage<PoolMember>>(
    `${BASE}/${encodeURIComponent(poolId)}/members${suffix ? `?${suffix}` : ""}`,
  );
};

/** 批量删除成员：只解除池-标的关联，不删主数据（需二次确认）。 */
export const removeMembers = (
  poolId: string,
  symbolIds: number[],
): Promise<unknown> =>
  requestJson(`${BASE}/${encodeURIComponent(poolId)}/members`, {
    method: "DELETE",
    headers: JSON_HEADERS,
    body: JSON.stringify({ symbol_ids: symbolIds }),
  });

export const fetchFilterFields = (): Promise<unknown> =>
  requestJson(`${BASE}/filter-fields`);

export const fetchFilterPresets = (): Promise<unknown> =>
  requestJson(`${BASE}/filter-presets`);

export const downloadImportTemplate = (): Promise<unknown> =>
  requestJson(`${BASE}/import-template`);

export const previewImport = (file: File): Promise<{ rows: unknown[] }> => {
  const form = new FormData();
  form.append("file", file);
  return requestJson<{ rows: unknown[] }>(`${BASE}/import-preview`, {
    method: "POST",
    body: form,
  });
};

export const exportImportErrors = (rows: unknown[]): Promise<unknown> =>
  requestJson(`${BASE}/import-errors`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ rows }),
  });

export const importMembers = (
  poolId: string,
  rows: unknown[],
): Promise<unknown> =>
  requestJson(`${BASE}/${encodeURIComponent(poolId)}/import`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ rows }),
  });
