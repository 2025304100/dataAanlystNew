/**
 * 因子挖掘域 API（T27）。
 *
 * 契约来源：后端 `app/api/routes/factor_mining.py`（前缀 `/api/v1/factor-mining`）。
 *
 * ⚠️ 后端实现进度（2026-09-18 核对）：
 *   - **已实现**：`locks/status`、`candidates/{id}/submit`、`candidates/batch-review`
 *   - **未实现**（M5/M7/M9/M10/M12/M13，当前返回 NotImplementedError）：
 *     runs 列表/详情/取消/暂停/恢复/停止/放弃、generations、candidates 列表、
 *     血缘、AI 预览、评估相关、模板另存、等级证据
 *
 * 因此调用方（MiningShell 等）**必须**对未实现接口做空态/错误态容错，
 * 不能假设数据一定返回——接口本身按契约先行定义，后端补齐后前端无需改动。
 */
import { requestJson } from "./client";
import type {
  MiningCandidate,
  MiningLockStatus,
  MiningPage,
  MiningRun,
  MiningRunCreate,
  MiningRunCreated,
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

  // ── 代际与候选 ────────────────────────────────────────────────────
  /** 每代汇总（含性能探针）。**后端未实现（M7）**。 */
  listGenerations: (runId: string): Promise<MiningPage<Record<string, unknown>>> =>
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
