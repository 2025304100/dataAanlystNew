/**
 * Step3 字段与校验的类型（T30）—— 对齐后端 T14
 * `app/api/routes/factor_mining_wizard.py` 与 `validation_service.py`。
 *
 * 字段目录口径（向导 §5）：展示**已注册且通过 readiness** 的字段，分组为
 * 行情 / 估值 / 财报 / 资金流 / 事件；「字段存在 ≠ 可用于挖掘」——
 * `data_mode=blocked` 或带 `blocked_reason` 的字段**不可勾选**。
 *
 * P0-2（2026-09-21）：目录改由候选池 `filter-fields` 供给（后端唯一现成的
 * 可用字段目录），分组随之携带候选池分类（universe/risk/valuation/...）。
 */

export type FieldGroup = "quote" | "valuation" | "financial" | "flow" | "event" | string;

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
  /** 面向开发的实测口径（表名/行数）→ 只放悬浮提示，不进界面正文 */
  blocked_detail?: string | null;
  /** P0-2：候选池分类的中文组标题（filter-fields 提供；缺失时回退 GROUP_LABEL） */
  category_label_zh?: string | null;
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

/** 单字段的校验实测元数据（来自校验报告 `shards[].result`，**校验完成后才有**） */
export interface FieldValidationMeta {
  /** 非空覆盖率 0~1 */
  coverage?: number | null;
  /** 物理表实际最早/最晚日期（→ Step3 的「可用区间」） */
  min_date?: string | null;
  max_date?: string | null;
  total_rows?: number | null;
  non_null_rows?: number | null;
  verdict?: string | null;
}

/** 校验任务状态（GET /factor-mining/validations/{task_id}，**已归一化**） */
export interface ValidationStatus {
  task_id: string;
  status: "queued" | "running" | "paused" | "passed" | "blocked" | "failed" | string;
  progress: ValidationProgress;
  blocked: BlockedItem[];
  warnings: string[];
  passed: boolean;
  message?: string | null;
  /** 后端报告结论：pass / warn / block（原始值，便于如实展示"警告"态） */
  verdict?: string | null;
  /** 后端给出的结论中文标签（通过 / 警告 / 阻断） */
  verdict_label_zh?: string | null;
  /** 后端报告摘要（`report.summary_zh`） */
  summary_zh?: string | null;
  /** 报告 24h 有效期截止时间（§5 硬规则） */
  valid_until?: string | null;
  /**
   * 逐字段实测元数据（key = 字段代码）。
   *
   * ⚠️ 字段目录接口（`filter-fields` / `FieldBinding.to_dict`）**不含**覆盖率、
   * 最新日期与可用区间 —— 这些只有校验任务跑完才由后端算出来。
   * 因此 Step3 的字段卡默认显示「校验后显示」，校验完成后用本字段回填。
   */
  field_meta?: Record<string, FieldValidationMeta>;
}

/** 终态：轮询到此即停 */
export const TERMINAL_STATUSES = ["passed", "blocked", "failed"] as const;

/** 轮询间隔（not_do：不引入 WebSocket，固定 5s 轮询） */
export const POLL_INTERVAL_MS = 5000;
