/**
 * 数据中心 · 历史行情镜像 API（C2）。
 *
 * 契约来源：`app/api/routes/data_mirror.py`（前缀 /api/v1/data-mirror）：
 *   - GET  /status              当前镜像状态（区间/行数/标的/低覆盖年份）
 *   - POST /tasks               创建镜像任务（preset + 磁盘检查 + 估算）
 *   - GET  /tasks               最近任务列表（含分块进度）
 *   - GET  /tasks/{task_id}     进度 / 分块 / 报告
 *   - POST /tasks/{task_id}/cancel  取消（分块边界自止；已完成块保留）
 */
import { requestJson } from "./client";

const BASE = "/api/v1/data-mirror";
const JSON_HEADERS = { "Content-Type": "application/json" };

/** GET /status 返回体（见 `mirror_task.get_mirror_status`）。 */
export interface DataMirrorStatus {
  available: boolean;
  warehouse_path?: string | null;
  total_rows?: number;
  total_symbols?: number;
  mirrored_from?: string | null;
  mirrored_to?: string | null;
  note_zh?: string | null;
  error?: string | null;
  low_coverage_years?: Array<{
    year: number;
    symbols: number;
    boundary_hint?: boolean;
    note_zh?: string | null;
  }>;
}

/** GET /tasks 项（见 `mirror_task.list_mirror_tasks`）。 */
export interface DataMirrorTask {
  task_id: string;
  status?: string | null;
  stage?: string | null;
  preset?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  completed_chunks?: number;
  total_chunks?: number;
  message?: string | null;
  created_at?: string | null;
  finished_at?: string | null;
}

export interface DataMirrorTaskPage {
  items: DataMirrorTask[];
}

export interface DataMirrorCreatePayload {
  preset?: string;
  start_date?: string | null;
  end_date?: string | null;
  batch_size?: number;
  skip_disk_check?: boolean;
}

export const dataMirrorApi = {
  getStatus: (): Promise<DataMirrorStatus> =>
    requestJson<DataMirrorStatus>(`${BASE}/status`),

  getTasks: (limit = 20): Promise<DataMirrorTaskPage> =>
    requestJson<DataMirrorTaskPage>(`${BASE}/tasks?limit=${limit}`),

  createTask: (payload: DataMirrorCreatePayload): Promise<DataMirrorTask> =>
    requestJson<DataMirrorTask>(`${BASE}/tasks`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({
        preset: payload.preset ?? "5y",
        start_date: payload.start_date ?? null,
        end_date: payload.end_date ?? null,
        batch_size: payload.batch_size ?? 1000,
        skip_disk_check: payload.skip_disk_check ?? false,
      }),
    }),

  cancelTask: (taskId: string): Promise<{ task_id: string; status: string }> =>
    requestJson(`${BASE}/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: "POST",
    }),
};