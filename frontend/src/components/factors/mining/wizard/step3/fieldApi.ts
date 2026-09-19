/**
 * Step3 字段校验 API（T30）—— 契约来源 T14
 * `app/api/routes/factor_mining_wizard.py`（**该组接口已实现**）。
 *
 * 进度获取走**轮询**（not_do：不引入 WebSocket），间隔 `POLL_INTERVAL_MS=5000`。
 */
import { requestJson } from "../../../../../api/client";

const BASE = "/api/v1/factor-mining/validations";
const JSON_HEADERS = { "Content-Type": "application/json" };

/** 创建异步校验任务（同一配置已有运行中的任务时后端复用，不重复创建）。 */
export const createValidation = (payload: {
  draft_id?: string | null;
  selected_fields: string[];
  config_hash?: string | null;
}): Promise<{ task_id: string; status: string }> =>
  requestJson<{ task_id: string; status: string }>(BASE, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });

/** 查询校验状态与分片进度。 */
export const getValidation = (taskId: string): Promise<import("./fieldTypes").ValidationStatus> =>
  requestJson<import("./fieldTypes").ValidationStatus>(
    `${BASE}/${encodeURIComponent(taskId)}`,
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
