/**
 * Step5 运行跟踪类型（T32）—— 契约对齐后端
 * `GET /factor-mining/runs/{id}`、`GET /runs/{id}/generations`（M7，待实现）。
 *
 * 口径（向导 §8.3）：运行可离开页面，进度由任务中心持续更新；
 * **收敛参考线来自挖掘配置的收敛阈值**，不得写成固定合格门禁（§8.3.2）。
 */

export type RunStatus =
  | "draft" | "queued" | "running" | "paused" | "validating"
  | "succeeded" | "failed" | "cancel_requested" | "cancelled"
  | "converged" | "invalidated" | string;

export interface CurvePoint {
  generation: number;
  best_icir: number;
  avg_icir: number;
}

export interface FinalValidationProgress {
  done: number;
  total: number;
  started_at?: string | null;
}

export interface RunProgress {
  run_id: string;
  status: RunStatus;
  generation: number;
  max_generations: number;
  best_icir?: number | null;
  avg_icir?: number | null;
  /** 种群多样性 ∈[0,1]；<0.3 视为早熟 */
  diversity?: number | null;
  /** 收敛阈值（来自配置，**不是门禁**） */
  convergence_threshold: number;
  converged: boolean;
  /** 连续无显著进步的代数（用于「建议停止」提示） */
  stall_generations?: number;
  eta_seconds?: number | null;
  population_size?: number | null;
  final_validation?: FinalValidationProgress | null;
  curve?: CurvePoint[];
}

/** 多样性早熟阈值（向导 §8.3.1：低于 30% 告警） */
export const PREMATURE_DIVERSITY = 0.3;

/** 「建议停止」所需的连续停滞代数 */
export const STALL_SUGGEST_STOP = 3;
