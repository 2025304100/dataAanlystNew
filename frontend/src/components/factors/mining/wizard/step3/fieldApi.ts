/**
 * Step3 字段校验 API（T30）—— 契约来源
 * `app/api/routes/factor_mining_wizard.py`（**该组接口已实现**）。
 *
 * 进度获取走**轮询**（not_do：不引入 WebSocket），间隔 `POLL_INTERVAL_MS=5000`。
 *
 * ⚠️ 真实契约（2026-09-21 对齐，勿再凭字段名猜）：
 * - `POST /factor-mining/validations`
 *   body = `{draft_id(必填,≥1), config(对象,参与 config_hash 去重), fields(必填,≥1), operator_id?}`
 *   —— **不是** `selected_fields`，也**不允许** `draft_id=null`（否则 422）。
 *   resp = `{task_id, reused, config_hash, total_shards, status}`
 * - `GET /factor-mining/validations/{task_id}`
 *   resp = `{found, task_id, draft_id, config_hash, status, completed_shards,
 *            total_shards, valid_until, report}`
 *   其中 `status ∈ queued|running|done|failed|cancelled`；
 *   `report = {verdict: pass|warn|block, verdict_label_zh, summary_zh,
 *              total_shards, failed_shards[], shards[], valid_until}`
 *
 * 本模块负责把上面的**后端原始形状**归一化成前端 `ValidationStatus`，
 * 业务组件只认归一化后的形状（改后端形状只需改这里）。
 */
import { requestJson } from "../../../../../api/client";
import type { BlockedItem, FieldValidationMeta, ValidationStatus } from "./fieldTypes";

const BASE = "/api/v1/factor-mining/validations";
const JSON_HEADERS = { "Content-Type": "application/json" };

/** 与后端 `validation_service.WARN_COVERAGE` 一致（覆盖率告警线） */
export const WARN_COVERAGE = 0.8;

/** 草稿 id 的 localStorage 键（后端按 draft_id + config_hash 幂等复用任务） */
const DRAFT_KEY = "mining_wizard_draft_id";

/**
 * 取（或首次生成）浏览器级草稿 id。
 *
 * 为什么需要：后端 `ValidationCreate.draft_id` 是**必填**且参与"同配置复用"判定；
 * 前端目前没有独立的草稿管理界面，因此用**稳定的浏览器级 id** 充当草稿，
 * 保证同一台机器上反复点「开始校验」时后端复用同一任务而不是每次新建。
 */
export function ensureDraftId(): string {
  try {
    const existing = window.localStorage.getItem(DRAFT_KEY);
    if (existing) return existing;
    const gen =
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `draft-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
    window.localStorage.setItem(DRAFT_KEY, gen);
    return gen;
  } catch {
    // localStorage 不可用（隐私模式/受限环境）时退化为进程内 id
    return "draft-anonymous";
  }
}

/** `POST /validations` 的返回（后端原始形状） */
export interface ValidationCreateResult {
  task_id: string;
  reused?: boolean;
  config_hash?: string | null;
  total_shards?: number;
  status?: string;
}

/** 后端 `ValidationShard.to_dict()` */
export interface RawValidationShard {
  field: string;
  bucket?: string | null;
  status?: string | null;
  result?: {
    verdict?: string | null;
    reason_zh?: string | null;
    coverage?: number | null;
    total_rows?: number | null;
    non_null_rows?: number | null;
    physical_table?: string | null;
    physical_column?: string | null;
    min_date?: string | null;
    max_date?: string | null;
    staleness_days?: number | null;
    [key: string]: unknown;
  } | null;
  error?: string | null;
}

/** 后端校验报告 */
export interface RawValidationReport {
  draft_id?: string | null;
  config_hash?: string | null;
  verdict?: string | null;
  verdict_label_zh?: string | null;
  summary_zh?: string | null;
  total_shards?: number | null;
  failed_shards?: string[] | null;
  shards?: RawValidationShard[] | null;
  valid_until?: string | null;
  [key: string]: unknown;
}

/** 后端 `GET /validations/{task_id}` 原始响应 */
export interface RawValidationProgress {
  found?: boolean;
  task_id?: string;
  draft_id?: string | null;
  config_hash?: string | null;
  status?: string | null;
  completed_shards?: number | null;
  total_shards?: number | null;
  valid_until?: string | null;
  report?: RawValidationReport | null;
}

/**
 * 后端原始响应 → 前端 `ValidationStatus`。
 *
 * 三态映射口径（向导 §5.1）：通过 / 警告 / 阻断。
 * - `status=done` + `verdict=block` → `blocked`（不能进入下一步）
 * - `status=done` + `verdict=warn` → `passed`（终态可继续）+ `warnings[]`（需确认）
 * - `status=done` + `verdict=pass` → `passed`（`passed=true`）
 * - `status=queued|running` → 同名字段；`failed|cancelled` → `failed`
 *
 * 同时把 `shards[].result` 里的**逐字段实测元数据**（覆盖率 / 最新日期 / 可用区间）
 * 提取成 `field_meta`：字段目录接口本身不返回这些值，Step3 靠它回填。
 */
export function normalizeValidation(raw: RawValidationProgress): ValidationStatus {
  const report = raw.report ?? null;
  const shards = report?.shards ?? [];
  const backendStatus = String(raw.status ?? "running");
  const verdict = String(report?.verdict ?? "");

  const blocked: BlockedItem[] = shards
    .filter((s) => s.result?.verdict === "block" || s.status === "failed")
    .map((s) => ({
      field: s.field,
      name_zh: null,
      issue: s.result?.reason_zh ?? s.error ?? "校验未通过",
      current: s.result?.coverage ?? null,
      required: WARN_COVERAGE,
      reason: s.result?.reason_zh ?? null,
    }));

  const field_meta: Record<string, FieldValidationMeta> = {};
  for (const s of shards) {
    if (!s?.field) continue;
    const r = s.result ?? null;
    field_meta[s.field] = {
      coverage: r?.coverage ?? null,
      min_date: r?.min_date ?? null,
      max_date: r?.max_date ?? null,
      total_rows: r?.total_rows ?? null,
      non_null_rows: r?.non_null_rows ?? null,
      verdict: r?.verdict ?? null,
    };
  }

  const warnings: string[] = shards
    .filter((s) => s.result?.verdict === "warn")
    .map((s) => `${s.field}：${s.result?.reason_zh ?? "覆盖率低于告警线"}`);
  if (report?.summary_zh && verdict === "warn") warnings.unshift(report.summary_zh);

  let status: string;
  let passed = false;
  if (backendStatus === "done") {
    if (verdict === "block" || blocked.length > 0) {
      status = "blocked";
    } else {
      status = "passed";
      passed = verdict === "pass" || verdict === "";
    }
  } else if (backendStatus === "failed" || backendStatus === "cancelled") {
    status = "failed";
  } else if (backendStatus === "queued" || backendStatus === "running") {
    status = backendStatus;
  } else {
    status = backendStatus;
  }

  return {
    task_id: String(raw.task_id ?? ""),
    status,
    progress: {
      done: Number(raw.completed_shards ?? 0),
      total: Number(raw.total_shards ?? report?.total_shards ?? 0),
    },
    blocked,
    warnings,
    passed,
    message: report?.summary_zh ?? null,
    verdict: verdict || null,
    verdict_label_zh: report?.verdict_label_zh ?? null,
    summary_zh: report?.summary_zh ?? null,
    valid_until: raw.valid_until ?? report?.valid_until ?? null,
    field_meta,
  };
}

/**
 * 创建（或幂等复用）异步校验任务。
 *
 * 入参保留 `selected_fields` 语义，内部按后端契约组装
 * `{draft_id, config, fields}`；`draft_id` 缺省时用浏览器级稳定 id。
 */
export const createValidation = (payload: {
  selected_fields: string[];
  /** 缺省自动取浏览器级草稿 id */
  draft_id?: string | null;
  /** 缺省用 `{selected_fields}` 作为配置哈希键 */
  config?: Record<string, unknown> | null;
}): Promise<ValidationCreateResult> =>
  requestJson<ValidationCreateResult>(BASE, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      draft_id: payload.draft_id || ensureDraftId(),
      config: payload.config ?? { selected_fields: payload.selected_fields },
      fields: payload.selected_fields,
    }),
  });

/** 查询校验状态与分片进度（已归一化为前端 `ValidationStatus`）。 */
export const getValidation = async (taskId: string): Promise<ValidationStatus> =>
  normalizeValidation(
    await requestJson<RawValidationProgress>(`${BASE}/${encodeURIComponent(taskId)}`),
  );

/** 查询已通过校验的字段。 */
export const getValidFields = (
  taskId: string,
): Promise<{ fields: string[] }> =>
  requestJson<{ fields: string[] }>(
    `${BASE}/${encodeURIComponent(taskId)}/valid`,
  );

/** 断点续跑。 */
export const resumeValidation = (taskId: string): Promise<unknown> =>
  requestJson(`${BASE}/${encodeURIComponent(taskId)}/resume`, { method: "POST" });

/** 暂停（保留已完成分片）。 */
export const pauseValidation = (taskId: string): Promise<unknown> =>
  requestJson(`${BASE}/${encodeURIComponent(taskId)}/pause`, { method: "POST" });
