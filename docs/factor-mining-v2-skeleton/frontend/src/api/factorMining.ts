/**
 * 因子挖掘 API 封装（设计文档 §9.3）。
 *
 * ⚠️ 为什么独立成文件而不追加到 `api/client.ts`：
 *    `client.ts` 已 4585 行；本项目已有 `api/dbConfig.ts` 作为"独立小 service"范式。
 *    v1.0 §2.4 同时写了"新建 api/mining.ts"与"追加到 client.ts 的 api 对象"，
 *    自相矛盾——本文件按 dbConfig 范式统一为**独立文件**。
 *
 * 复用 `client.ts` 的 `requestJson` / `ApiError`，不重复实现请求层。
 */
import { requestJson, ApiError } from "./client";
import type {
  FieldValidationReport,
  LockStatus,
  MiningRun,
  MiningRunCreated,
  MiningRunDetail,
  PoolAnalysis,
  PoolPreviewResult,
  PoolSnapshotRef,
  QualityGrade,
  GenerationStat,
  CandidateRead,
  GradeEvidence,
} from "../types/mining";

const BASE = "/api/v1";

/** 挖掘类接口多为长任务提交/查询，放宽默认 20s 超时 */
const DEFAULT_OPTS = { timeoutMs: 60000 } as const;

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

// ── 批次 ──────────────────────────────────────────────

export interface MiningRunCreatePayload {
  draft_id?: string;
  template_id?: string;
  candidate_pool_snapshot_id: string;
  data_cutoff_at: string;
  start_date: string;
  end_date: string;
  rebalance_frequency: "daily" | "weekly" | "monthly";
  target_horizon: number;
  train_ratio?: number;
  validation_ratio?: number;
  random_seed?: number;
  evolution_params?: Record<string, unknown>;
  filter_config?: Record<string, unknown>;
}

export const factorMiningApi = {
  // ── 批次 ──
  createRun: (payload: MiningRunCreatePayload) =>
    requestJson<MiningRunCreated>(`${BASE}/factor-mining/runs`, {
      method: "POST",
      jsonBody: payload,
      ...DEFAULT_OPTS,
    }),

  listRuns: (params: { page?: number; page_size?: number; status?: string } = {}) =>
    requestJson<Page<MiningRun>>(`${BASE}/factor-mining/runs`, {
      query: params,
      ...DEFAULT_OPTS,
    }),

  getRun: (runId: string) =>
    requestJson<MiningRunDetail>(`${BASE}/factor-mining/runs/${runId}`, DEFAULT_OPTS),

  cancelRun: (runId: string) =>
    requestJson<{ status: string }>(`${BASE}/factor-mining/runs/${runId}/cancel`, {
      method: "POST",
      ...DEFAULT_OPTS,
    }),

  pauseRun: (runId: string) =>
    requestJson<{ status: string }>(`${BASE}/factor-mining/runs/${runId}/pause`, {
      method: "POST",
      ...DEFAULT_OPTS,
    }),

  resumeRun: (runId: string) =>
    requestJson<{ status: string }>(`${BASE}/factor-mining/runs/${runId}/resume`, {
      method: "POST",
      ...DEFAULT_OPTS,
    }),

  stopRun: (runId: string) =>
    requestJson<{ status: string }>(`${BASE}/factor-mining/runs/${runId}/stop`, {
      method: "POST",
      ...DEFAULT_OPTS,
    }),

  discardRun: (runId: string) =>
    requestJson<{ status: string }>(`${BASE}/factor-mining/runs/${runId}/discard`, {
      method: "POST",
      ...DEFAULT_OPTS,
    }),

  // ── 代际与候选 ──
  listGenerations: (runId: string) =>
    requestJson<GenerationStat[]>(`${BASE}/factor-mining/runs/${runId}/generations`, DEFAULT_OPTS),

  listCandidates: (
    runId: string,
    params: { page?: number; page_size?: number; grade?: QualityGrade; category?: string } = {},
  ) =>
    requestJson<Page<CandidateRead>>(`${BASE}/factor-mining/runs/${runId}/candidates`, {
      query: params,
      ...DEFAULT_OPTS,
    }),

  getCandidateLineage: (runId: string, candidateId: string) =>
    requestJson<unknown>(
      `${BASE}/factor-mining/runs/${runId}/candidates/${candidateId}/lineage`,
      DEFAULT_OPTS,
    ),

  // ── 锁（Step4 提交前必查） ──
  getLockStatus: () => requestJson<LockStatus>(`${BASE}/factor-mining/locks/status`),

  // ── 候选池 ──
  previewPool: (payload: Record<string, unknown>) =>
    requestJson<PoolPreviewResult>(`${BASE}/factor-mining/candidate-pools/preview`, {
      method: "POST",
      jsonBody: payload,
      ...DEFAULT_OPTS,
    }),

  /** 冻结快照 + 生成看板 + 锁定（唯一能产生 snapshot 的入口） */
  analyzePool: (poolId: string) =>
    requestJson<PoolSnapshotRef>(`${BASE}/factor-mining/candidate-pools/${poolId}/analyze`, {
      method: "POST",
      ...DEFAULT_OPTS,
    }),

  getPoolAnalysis: (poolId: string) =>
    requestJson<PoolAnalysis>(`${BASE}/factor-mining/candidate-pools/${poolId}/analysis`, DEFAULT_OPTS),

  resetPoolAnalysis: (poolId: string) =>
    requestJson<{ status: string }>(
      `${BASE}/factor-mining/candidate-pools/${poolId}/reset-analysis`,
      { method: "POST", ...DEFAULT_OPTS },
    ),

  // ── 字段校验 ──
  createValidation: (payload: { draft_id: string; fields: string[]; config: Record<string, unknown> }) =>
    requestJson<{ validation_id: string; reused: boolean }>(`${BASE}/factor-mining/validations`, {
      method: "POST",
      jsonBody: payload,
      ...DEFAULT_OPTS,
    }),

  getValidation: (validationId: string) =>
    requestJson<FieldValidationReport>(`${BASE}/factor-mining/validations/${validationId}`, DEFAULT_OPTS),

  recheckValidation: (validationId: string) =>
    requestJson<{ validation_id: string }>(
      `${BASE}/factor-mining/validations/${validationId}/recheck`,
      { method: "POST", ...DEFAULT_OPTS },
    ),

  // ── 最终准备校验（含锁状态） ──
  prepareDraft: (draftId: string) =>
    requestJson<{
      ok: boolean;
      errors: { error_code: string; title_zh: string; detail_zh: string }[];
      lock_status: LockStatus;
      resource_estimate: Record<string, unknown>;
    }>(`${BASE}/factor-mining/drafts/${draftId}/prepare`, { method: "POST", ...DEFAULT_OPTS }),

  // ── 候选 → 正式因子 ──
  submitCandidate: (candidateId: string) =>
    requestJson<{ factor_code: string; factor_version_id: number }>(
      `${BASE}/factor-mining/candidates/${candidateId}/submit`,
      { method: "POST", ...DEFAULT_OPTS },
    ),

  batchReview: (payload: {
    candidate_ids: string[];
    action: "approve" | "reject" | "add_to_factorset";
    factor_set_id?: string;
  }) =>
    requestJson<{ ok: number; skipped: number; messages: string[] }>(
      `${BASE}/factor-mining/candidates/batch-review`,
      { method: "POST", jsonBody: payload, ...DEFAULT_OPTS },
    ),

  // ── 分级（M2） ──
  getGradeEvidence: (candidateId: string) =>
    requestJson<GradeEvidence>(
      `${BASE}/factor-mining/candidates/${candidateId}/grade/evidence`,
      DEFAULT_OPTS,
    ),

  manualGrade: (candidateId: string, payload: { grade: QualityGrade; reason: string }) =>
    requestJson<{ grade: QualityGrade; manual_adjusted: 1 }>(
      `${BASE}/factor-mining/candidates/${candidateId}/grade/manual`,
      { method: "POST", jsonBody: payload, ...DEFAULT_OPTS },
    ),

  restoreAutoGrade: (candidateId: string) =>
    requestJson<{ manual_adjusted: 0 }>(
      `${BASE}/factor-mining/candidates/${candidateId}/grade/restore-auto`,
      { method: "POST", ...DEFAULT_OPTS },
    ),
};

/**
 * 从 `ApiError` 的 `extras` 中取出挖掘域结构化修复动作。
 *
 * 后端错误契约（设计文档 §8.1）：
 *   FactorSevenError.to_dict() → 7 要素 + extras
 *   extras.fix_action = { action, target_step, message }
 */
export function extractMiningFixAction(error: unknown) {
  if (!(error instanceof ApiError)) return null;
  const extras = (error as unknown as { extras?: Record<string, unknown> }).extras;
  const fix = extras?.fix_action as
    | { action: string; target_step?: number; message?: string }
    | undefined;
  if (!fix) return null;
  return {
    action: fix.action,
    targetStep: fix.target_step ?? 1,
    message: fix.message ?? "",
  };
}

export default factorMiningApi;
