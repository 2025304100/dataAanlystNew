/**
 * Step3 字段与校验的类型（T30）—— 对齐后端 T14
 * `app/api/routes/factor_mining_wizard.py` 与 `validation_service.py`。
 *
 * 字段目录口径（向导 §5）：展示**已注册且通过 readiness** 的字段，分组为
 * 行情 / 估值 / 财报 / 资金流 / 事件；「字段存在 ≠ 可用于挖掘」——
 * `data_mode=blocked` 或带 `blocked_reason` 的字段**不可勾选**。
 */

export type FieldGroup = "quote" | "valuation" | "financial" | "flow" | "event";

/** 数据模式（后端字段目录） */
export type FieldDataMode =
  | "continuous"
  | "PIT"
  | "event"
  | "snapshot"
  | "blocked"
  | string;

export interface MiningField {
  code: string;
  name_zh: string;
  group: FieldGroup;
  source_table?: string | null;
  data_mode?: FieldDataMode | null;
  coverage?: number | null;
  latest_date?: string | null;
  available_from?: string | null;
  available_to?: string | null;
  /** 限制原因（如覆盖率不足 / PIT 违规 / blocked）—— 有值即不可勾选 */
  blocked_reason?: string | null;
}

/** 校验分片进度 */
export interface ValidationProgress {
  done: number;
  total: number;
}

/** 单个阻断字段（向导 §5.1：字段代码/中文名/问题类型/当前值/要求值/原因） */
export interface BlockedItem {
  field: string;
  name_zh?: string | null;
  issue?: string | null;
  current?: number | string | null;
  required?: number | string | null;
  reason?: string | null;
}

/** 校验任务状态（GET /factor-mining/validations/{task_id}） */
export interface ValidationStatus {
  task_id: string;
  status: "queued" | "running" | "paused" | "passed" | "blocked" | "failed" | string;
  progress: ValidationProgress;
  blocked: BlockedItem[];
  warnings: string[];
  passed: boolean;
  message?: string | null;
}

/** 终态：轮询到此即停 */
export const TERMINAL_STATUSES = ["passed", "blocked", "failed"] as const;

/** 轮询间隔（not_do：不引入 WebSocket，固定 5s 轮询） */
export const POLL_INTERVAL_MS = 5000;
