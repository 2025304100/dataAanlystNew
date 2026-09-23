/**
 * 因子挖掘域 API（T27）。
 *
 * 契约来源：后端 `app/api/routes/factor_mining.py`（前缀 `/api/v1/factor-mining`）。
 *
 * ⚠️ 后端实现进度（**2026-09-21 实测更新**，早期注释已过时）：
 *   - **已实现且实测可用**：`locks/status`、`runs`（列表/详情/创建/取消/暂停/恢复/停止/放弃）、
 *     `runs/{id}/generations`、`runs/{id}/candidates`、`split-budget`、
 *     `candidates/{id}/submit`、`candidates/batch-review`。
 *     （`POST /runs` 实测 201 建批次并在 Step5 正常跟踪进化。）
 *   - **仍需容错**：血缘、AI 预览、模板另存、等级证据等，见各方法注释。
 *
 * 调用方（MiningShell 等）仍**必须**对空态/错误态容错：接口异常不得崩页。
 */
import { requestJson } from "./client";
import type {
  MiningCandidate,
  MiningLockStatus,
  MiningPage,
  MiningRun,
  MiningRunCreate,
  MiningRunCreated,
  SplitBudget,
} from "../types/mining";

const BASE = "/api/v1/factor-mining";
const JSON_HEADERS = { "Content-Type": "application/json" };

export const factorMiningApi = {
  // ── 批次 ──────────────────────────────────────────────────────────
  /** 分页查询批次。**后端未实现（M5）**，调用方需容错。 */
  listRuns: (
    params: { page?: number; page_size?: number; status?: string } = {},
  ): Promise<MiningPage<MiningRun>> => {
    const qs = new URLSearchParams();
    if (params.page) qs.set("page", String(params.page));
    if (params.page_size) qs.set("page_size", String(params.page_size));
    if (params.status) qs.set("status", params.status);
    const suffix = qs.toString();
    return requestJson<MiningPage<MiningRun>>(
      `${BASE}/runs${suffix ? `?${suffix}` : ""}`,
    );
  },

  /** 批次详情（状态/当前代数/进度/排队位次）。**后端未实现（M5）**。 */
  getRun: (runId: string): Promise<MiningRun> =>
    requestJson<MiningRun>(`${BASE}/runs/${encodeURIComponent(runId)}`),

  /** 创建批次（含双锁检查）。**后端未实现（M5）**。 */
  createRun: (payload: MiningRunCreate): Promise<MiningRunCreated> =>
    requestJson<MiningRunCreated>(BASE + "/runs", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(payload),
    }),

  cancelRun: (runId: string): Promise<unknown> =>
    requestJson(`${BASE}/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }),

  pauseRun: (runId: string): Promise<unknown> =>
    requestJson(`${BASE}/runs/${encodeURIComponent(runId)}/pause`, { method: "POST" }),

  resumeRun: (runId: string): Promise<unknown> =>
    requestJson(`${BASE}/runs/${encodeURIComponent(runId)}/resume`, { method: "POST" }),

  stopRun: (runId: string): Promise<unknown> =>
    requestJson(`${BASE}/runs/${encodeURIComponent(runId)}/stop`, { method: "POST" }),

  discardRun: (runId: string): Promise<unknown> =>
    requestJson(`${BASE}/runs/${encodeURIComponent(runId)}/discard`, { method: "POST" }),

  // ── 草稿（后端**已实现**：draft_service.save_draft）─────────────────
  /**
   * 暂存/更新草稿：无 `draft_id` 新建，有则**部分更新**（未出现的步骤原样保留）。
   *
   * ⚠️ 2026-09-23：此前前端从未调用本接口，「保存草稿」按钮的 `onClick` 实为
   * `undefined`（零反应）。步骤键固定 `step1`~`step4`（后端 `DRAFT_STEP_KEYS`）。
   */
  saveDraft: (payload: {
    draft_id?: string | null;
    name?: string | null;
    current_step?: number;
    candidate_pool_snapshot_id?: string | null;
    steps?: Record<string, unknown>;
  }): Promise<{ draft_id: string; current_step?: number; status?: string }> =>
    requestJson(`${BASE}/drafts`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(payload),
      timeoutMs: 30_000,
    }),

  listDrafts: (params: { owner?: string; limit?: number } = {}): Promise<unknown> => {
    const qs = new URLSearchParams();
    if (params.owner) qs.set("owner", params.owner);
    if (params.limit) qs.set("limit", String(params.limit));
    const suffix = qs.toString();
    return requestJson(`${BASE}/drafts${suffix ? `?${suffix}` : ""}`);
  },

  getDraft: (draftId: string): Promise<Record<string, unknown>> =>
    requestJson(`${BASE}/drafts/${encodeURIComponent(draftId)}`),

  /** 草稿 → 可提交状态（冻结因子集等前置）；失败时后端给明确错误码。 */
  prepareDraft: (draftId: string): Promise<unknown> =>
    requestJson(`${BASE}/drafts/${encodeURIComponent(draftId)}/prepare`, { method: "POST" }),

  // ── 代际与候选 ────────────────────────────────────────────────────
  /** 每代汇总（含性能探针与 cache_validation_*）。后端返回**数组**（§8.3.6）。 */
  listGenerations: (
    runId: string,
  ): Promise<Array<Record<string, unknown>>> =>
    requestJson(`${BASE}/runs/${encodeURIComponent(runId)}/generations`),

  /** 分页候选（可按等级/类型筛选）。**后端未实现（M7/M13）**。 */
  listCandidates: (
    runId: string,
    params: { page?: number; page_size?: number; grade?: string; category?: string } = {},
  ): Promise<MiningPage<MiningCandidate>> => {
    const qs = new URLSearchParams();
    if (params.page) qs.set("page", String(params.page));
    if (params.page_size) qs.set("page_size", String(params.page_size));
    if (params.grade) qs.set("grade", params.grade);
    if (params.category) qs.set("category", params.category);
    const suffix = qs.toString();
    return requestJson<MiningPage<MiningCandidate>>(
      `${BASE}/runs/${encodeURIComponent(runId)}/candidates${suffix ? `?${suffix}` : ""}`,
    );
  },

  getCandidateLineage: (runId: string, candidateId: string): Promise<unknown> =>
    requestJson(
      `${BASE}/runs/${encodeURIComponent(runId)}/candidates/${encodeURIComponent(candidateId)}/lineage`,
    ),

  // ── 锁状态（后端**已实现**，Step4 提交前可直接调用）────────────────
  getLockStatus: (): Promise<MiningLockStatus> =>
    requestJson<MiningLockStatus>(`${BASE}/locks/status`),

  // ── Step3 挖掘字段目录（后端 GET /factor-mining/fields，设计 §5）────
  // 与候选池筛选字段（filter-fields）不同：这里是公式/模板实际引用的
  // DSL 注册字段（close/pe_ttm/roe_ttm…），分组为行情/估值/财报/资金流/事件。
  listMiningFields: (): Promise<{
    dsl_version?: string | null;
    fields: Array<{
      field: string;
      label_zh: string;
      group: string;
      group_label_zh?: string | null;
      source_table?: string | null;
      data_mode?: string | null;
      availability?: string | null;
      /** 面向用户的阻断原因（界面正文） */
      blocked_reason_zh?: string | null;
      /** 面向开发的实测口径（表名/常量）→ 只放悬浮提示 */
      blocked_detail_zh?: string | null;
    }>;
    groups?: Array<{ group: string; label_zh: string }>;
  }> => requestJson(`${BASE}/fields`),

  // ── 切分预算（P1-9：后端 POST /factor-mining/split-budget，Step2 展示用）────
  computeSplitBudget: (payload: {
    start_date: string;
    end_date: string;
    frequency: string;
    target_horizon: number;
    train_ratio: number;
    validation_ratio: number;
  }): Promise<SplitBudget> =>
    requestJson<SplitBudget>(`${BASE}/split-budget`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(payload),
    }),

  // ── 候选 → 正式因子 / 批量操作（后端**已实现**，T25）────────────────
  submitCandidate: (candidateId: string): Promise<unknown> =>
    requestJson(
      `${BASE}/candidates/${encodeURIComponent(candidateId)}/submit`,
      { method: "POST" },
    ),

  batchReview: (actions: Array<Record<string, unknown>>, createdBy = "local_user"): Promise<unknown> =>
    requestJson(`${BASE}/candidates/batch-review`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ actions, created_by: createdBy }),
    }),
};
