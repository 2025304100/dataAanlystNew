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

/** 单分类命中/排除统计（后端 `CategoryStat.to_dict`，向导 §3.2「预览区按分类展示」） */
export interface PoolCategoryStat {
  category: string;
  label_zh: string;
  /** 进入该分类时仍在池中的数量 */
  evaluated: number;
  /** 该分类排除的数量 */
  excluded: number;
  /** 通过数量（= evaluated - excluded） */
  passed?: number;
  detail_zh?: string | null;
}

/** 示例标的（后端 `_build_samples`，向导 §3.5「示例代码」；只含展示字段） */
export interface PoolSample {
  symbol: string;
  name?: string | null;
  market?: string | null;
  board?: string | null;
  board_label_zh?: string | null;
  [key: string]: unknown;
}

/**
 * 条件筛选预览（后端 `PreviewResult.to_dict` 契约：2026-09-21 对齐）。
 *
 * ⚠️ 后端**不返回** `by_rule`（历史误写）；按分类的统计在 `categories`，
 * 示例代码在 `samples`，字段覆盖率在 `field_coverage`。
 */
export interface PoolPreview {
  /** 基础股票池（数据截止日可用样本） */
  universe_size: number;
  /** 命中数（有效标的） */
  hits: number;
  /** 排除数 = universe_size - hits（按规则有重叠时仅作统计） */
  excluded_total: number;
  /** 系统硬下限（50，前端不得降低） */
  min_pool_size: number;
  /** 当前配置是否可生成（blocking_issues 为空） */
  can_generate: boolean;
  /** 按分类的命中/排除统计 */
  categories?: PoolCategoryStat[];
  /** 示例代码（最多若干只，只含展示字段） */
  samples?: PoolSample[];
  /** 字段覆盖率（key=字段，value=0~1） */
  field_coverage?: Record<string, number>;
  /** 实际采用的数据截止日 */
  as_of_date?: string | null;
  /** 规则哈希（快照复现用） */
  rule_hash?: string;
  warnings?: string[];
  /** 结构化阻断（命中 0 / <50 / 覆盖率 / PIT / 范围冲突等） */
  blocking_issues?: Array<{ error_code: string; detail_zh: string }>;
}

export interface CandidatePool {
  id: string;
  name: string;
  source_type: "filter" | "import" | string;
  status?: string | null;
  description?: string | null;
}

/** 概览（后端 `analysis.overview`） */
export interface PoolAnalysisOverview {
  stock_count?: number | null;
  below_min_pool_size?: boolean | null;
  avg_market_cap?: number | null;
  avg_market_cap_yi?: number | null;
  market_cap_band_zh?: string | null;
  time_range?: string | null;
  data_completeness?: number | null;
}

/** 市值分档（后端 `analysis.market_cap_distribution[]`） */
export interface PoolAnalysisCapTier {
  key: string;
  label_zh: string;
  count: number;
  ratio: number;
}

/** 行业分布（后端 `analysis.industry_distribution[]`） */
export interface PoolAnalysisIndustry {
  industry: string;
  count: number;
  ratio: number;
  note_zh?: string | null;
}

/** 风格维度（后端 `style_exposure.dimensions[dim]`） */
export interface PoolAnalysisStyleDim {
  available?: boolean;
  score?: number | null;
  label_zh?: string | null;
  coverage?: number | null;
  interpretation_zh?: string | null;
  reason_zh?: string | null;
}

/** 因子类型建议（后端 `factor_type_suggestions[]`） */
export interface PoolAnalysisSuggestion {
  factor_type: string;
  stars: number;
  stars_label_zh?: string | null;
}

/** 市场环境（后端 `analysis.market_environment`） */
export interface PoolAnalysisEnvironment {
  market_regime?: {
    stage?: string;
    label_zh?: string;
    cumulative_return?: number | null;
    max_drawdown?: number | null;
    reason_zh?: string | null;
    basis_zh?: string | null;
  };
  volatility?: {
    band?: string;
    label_zh?: string;
    annualized?: number | null;
    thresholds?: Record<string, number>;
  };
  trend_strength?: { band?: string; label_zh?: string; reason_zh?: string | null };
  factor_type_suggestions?: PoolAnalysisSuggestion[];
}

/** 字段覆盖率（后端 `data_quality.fields[]`） */
export interface PoolAnalysisQualityField {
  field: string;
  coverage: number;
  below_threshold?: boolean;
  source?: string | null;
}

/**
 * 候选池分析看板数据（后端 `analysis` 整体，向导 §3.7.4）。
 *
 * 形状来自 `app/services/factors/candidate_pool/analysis.py::build_analysis`，
 * 字段名逐一对应，**前端不臆造**；缺失项按空态展示。
 */
export interface PoolAnalysis {
  generated_at?: string | null;
  as_of_date?: string | null;
  lookback_days?: number | null;
  trade_days_actual?: number | null;
  avg_daily_symbols?: number | null;
  overview?: PoolAnalysisOverview;
  market_cap_distribution?: PoolAnalysisCapTier[];
  industry_distribution?: PoolAnalysisIndustry[];
  industry_concentration?: {
    top_ratio?: number | null;
    alert?: boolean | null;
    alert_zh?: string | null;
    threshold?: number | null;
  };
  style_exposure?: {
    dimensions?: Record<string, PoolAnalysisStyleDim>;
    dominant_style?: string | null;
    dominant_score?: number | null;
    complement_suggestion_zh?: string | null;
  };
  market_environment?: PoolAnalysisEnvironment;
  data_quality?: {
    fields?: PoolAnalysisQualityField[];
    overall_coverage?: number | null;
  };
  warnings?: string[];
}

/**
 * 快照（后端 `POST /candidate-pools/{id}/snapshot` 响应：
 * `{snapshot_id, pool_id, analysis_status, is_locked, data_cutoff_at, analysis, pool}`）。
 *
 * ⚠️ 路由/服务返回的是 **`snapshot_id`** 与 **`analysis`**（不是 `id`/`analysis_json`）。
 * 本模块在 `normalizeSnapshot()` 里做一次归一化，业务侧统一读 `id` / `analysis_json`。
 */
export interface PoolSnapshot {
  /** 快照 id（已归一化；后端字段名 `snapshot_id`） */
  id: string;
  pool_id?: string;
  is_locked?: boolean;
  /** 分析看板数据（已归一化；后端字段名 `analysis`） */
  analysis_json?: PoolAnalysis | null;
  analysis_status?: string | null;
  data_cutoff_at?: string | null;
  as_of_date?: string | null;
  analyzed_at?: string | null;
}

/** 后端快照响应 → 前端统一形状（兼容 `snapshot_id`/`id` 与 `analysis`/`analysis_json`） */
function normalizeSnapshot(raw: unknown): PoolSnapshot {
  const rec = (raw ?? {}) as Record<string, unknown>;
  const pool = (rec.pool ?? null) as Record<string, unknown> | null;
  const analysis = (rec.analysis ?? rec.analysis_json ?? null) as PoolAnalysis | null;
  const id = String(rec.snapshot_id ?? rec.id ?? "");
  return {
    id,
    pool_id: (rec.pool_id ?? pool?.id ?? undefined) as string | undefined,
    is_locked: Boolean(rec.is_locked),
    analysis_json: analysis,
    analysis_status: (rec.analysis_status ?? null) as string | null,
    data_cutoff_at:
      (rec.data_cutoff_at ?? pool?.data_cutoff_at ?? null) as string | null,
    as_of_date:
      (rec.as_of_date ?? analysis?.as_of_date ?? pool?.as_of_date ?? null) as string | null,
    analyzed_at:
      (rec.analyzed_at ?? analysis?.generated_at ?? pool?.analyzed_at ?? null) as string | null,
  };
}

/**
 * 候选池成员（后端 `service.list_members` 的真实返回）。
 *
 * ⚠️ 实测（2026-09-21）后端**只**返回：
 * `member_id / pool_id / symbol_id / symbol / name / asset_type / market / board /
 *  industry / is_st / is_active / included_at / is_deleted / deleted_at`。
 * 设计 §3.6 想要的行情/估值/财务指标（close/总市值/PE/PB/ROE/近 N 日均成交额…）
 * **一个都没有** —— 那些字段标为可选，拿到才渲染，不造假。
 */
export interface PoolMember {
  symbol_id?: number;
  symbol: string;
  name?: string | null;
  /** 资产类型（stock/etf）；后端返回 */
  asset_type?: string | null;
  /** 交易所（sh/sz/bj）；后端返回 */
  market?: string | null;
  /** 板块（main/gem/star/bj）；后端返回 */
  board?: string | null;
  /** 行业（当前实测全 NULL → 界面显示"未披露"） */
  industry?: string | null;
  /** 是否 ST（0/1）；后端返回。**不是** `risk_flag` */
  is_st?: number | null;
  /** 是否在市（0/1）；后端返回 */
  is_active?: number | null;
  /** 纳入时间 */
  included_at?: string | null;
  // ── 以下字段后端当前不返回（保留为可选，拿到才渲染）──
  listed_at?: string | null;
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
  risk_flag?: string | null;
}

export interface PoolPage<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** 导入逐行匹配结果（P1-6：六种状态，仅 success 入池） */
export interface ImportRowResult {
  row_no: number;
  status: string;
  status_label_zh?: string | null;
  symbol: string;
  symbol_raw?: string | null;
  name?: string | null;
}

/** 导入预览/入池结果（后端 `ImportResult.to_dict`） */
export interface ImportPreviewResult {
  pool_id?: string | null;
  import_batch_id?: string | null;
  total_rows: number;
  admitted_count: number;
  error_count: number;
  has_errors: boolean;
  counts: Record<string, number>;
  counts_label_zh: Record<string, string>;
  rows: ImportRowResult[];
}

/**
 * 条件筛选预览（**不落库**；300ms 防抖调用）。
 *
 * ⚠️ 必须给专用超时（2026-09-22 实测）：全市场 ≈5,500 只、20 日窗口的分位阈值与
 * 逐条排除都在服务端算，慢时显著超过 client 的 20s 默认值 —— 用户会看到
 * 「请求超时」而**底部统计其实随后就出结果了**（假失败）。
 */
export const previewPool = (
  filterConfig: Record<string, unknown>,
  asOfDate?: string,
): Promise<PoolPreview> =>
  requestJson<PoolPreview>(`${BASE}/preview`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ filter_config: filterConfig, as_of_date: asOfDate ?? null }),
    timeoutMs: 120_000,
  });

/** 按筛选结果生成候选池（含成员物化；4xx 时不写任何东西）。同样属重活，给长超时。 */
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
    timeoutMs: 120_000,
  });

/** 创建候选池（P1-6：导入方式先建池 source_type=import 需带 import_batch_id，再上传文件入池）。 */
export const createPool = (payload: {
  name: string;
  source_type: "filter" | "import" | string;
  description?: string | null;
  /** source_type=import 时必填（后端 service.create_pool 强制校验，取预览返回的 import_batch_id） */
  import_batch_id?: string | null;
}): Promise<CandidatePool> =>
  requestJson<CandidatePool>(BASE, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });

/** 「生成挖掘物料」：冻结并分析（analyze=false 只冻结，不锁定）。 */
export const createSnapshot = async (
  poolId: string,
  payload: { analyze?: boolean; as_of_date?: string | null } = {},
): Promise<PoolSnapshot> =>
  normalizeSnapshot(
    await requestJson<Record<string, unknown>>(
      `${BASE}/${encodeURIComponent(poolId)}/snapshot`,
      {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({ analyze: payload.analyze ?? true, as_of_date: payload.as_of_date ?? null }),
        // 冻结 + 全池分析（含市值分布/行业/覆盖率），重活：给 3 分钟
        timeoutMs: 180_000,
      },
    ),
  );

export const fetchLatestSnapshot = async (poolId: string): Promise<PoolSnapshot | null> => {
  const raw = await requestJson<Record<string, unknown> | null>(
    `${BASE}/${encodeURIComponent(poolId)}/snapshot/latest`,
  );
  return raw == null ? null : normalizeSnapshot(raw);
};

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
    { timeoutMs: 60_000 },
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

/** 条件字段目录项（后端 `FieldBinding.to_dict`） */
export interface FilterField {
  field: string;
  label_zh: string;
  category: string;
  category_label_zh?: string | null;
  physical_table?: string | null;
  physical_column?: string | null;
  availability?: string;
  /** 面向用户的阻断原因（界面正文用） */
  blocked_reason_zh?: string | null;
  /** 面向开发的实测口径（表名/常量/行数）→ 只放悬浮提示 */
  blocked_detail_zh?: string | null;
  data_mode?: string;
}

/** 左栏预设项（后端 `presets.Preset.to_dict`） */
export interface FilterPreset {
  preset_code: string;
  group: string;
  group_label_zh: string;
  label_zh: string;
  field: string | null;
  field_label_zh?: string | null;
  operator: string;
  min_value: number | null;
  max_value: number | null;
  basis_zh?: string;
  availability?: string;
  blocked_reason_zh?: string | null;
  note_zh?: string | null;
  applyable?: boolean;
}

export const fetchFilterFields = (): Promise<{
  fields: FilterField[];
  categories: Array<{ category: string; label_zh: string }>;
  min_pool_size: number;
}> => requestJson(`${BASE}/filter-fields`);

export const fetchFilterPresets = (): Promise<{
  as_of_date: string | null;
  window_days: number;
  groups: Array<{ group: string; label_zh: string }>;
  presets: FilterPreset[];
  warnings: string[];
}> => requestJson(`${BASE}/filter-presets`, { timeoutMs: 60_000 });

/** 下载文件（模板 / 错误明细）：原生 fetch → blob → 触发浏览器保存。 */
async function downloadFile(
  url: string,
  filename: string,
  init?: RequestInit,
): Promise<void> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`download failed: ${res.status}`);
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}

/** P1-6：下载导入模板（CSV，symbol 必填 + name/market/note 可选）。 */
export const downloadImportTemplate = (): Promise<void> =>
  downloadFile(`${BASE}/import-template?fmt=csv`, "import_template.csv");

/** P1-6：导入预览 —— 解析 + 逐行匹配，**不写任何东西**。 */
export const previewImport = (file: File): Promise<ImportPreviewResult> => {
  const form = new FormData();
  form.append("file", file);
  return requestJson<ImportPreviewResult>(`${BASE}/import-preview`, {
    method: "POST",
    body: form,
    timeoutMs: 120_000,
  });
};

/** P1-6：上传文件入池（仅成功行写入；multipart `file` 字段与后端一致）。 */
export const importPoolMembers = (
  poolId: string,
  file: File,
  importBatchId?: string | null,
): Promise<ImportPreviewResult> => {
  const form = new FormData();
  form.append("file", file);
  const qs = importBatchId
    ? `?import_batch_id=${encodeURIComponent(importBatchId)}`
    : "";
  return requestJson<ImportPreviewResult>(
    `${BASE}/${encodeURIComponent(poolId)}/import${qs}`,
    { method: "POST", body: form, timeoutMs: 120_000 },
  );
};

/** P1-6：下载错误明细 CSV（后端重新解析同一份文件，仅含非成功行）。 */
export const exportImportErrors = (file: File): Promise<void> => {
  const form = new FormData();
  form.append("file", file);
  return downloadFile(
    `${BASE}/import-errors`,
    `import_errors_${Date.now()}.csv`,
    { method: "POST", body: form },
  );
};
