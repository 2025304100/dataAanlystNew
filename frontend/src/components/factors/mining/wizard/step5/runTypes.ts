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

/** M2 多样性 health 逐代观测（来自 `factor_mining_generations` 的 diversity_* 字段） */
export interface DiversityCurvePoint {
  generation: number;
  /** 三层多样性合成 health（∈[0,1]；<0.3 视为早熟） */
  diversity_health?: number | null;
  category_evenness?: number | null;
  diversity_genotype?: number | null;
  diversity_phenotype?: number | null;
}

export interface FinalValidationProgress {
  done: number;
  total: number;
  started_at?: string | null;
}

/** 每代性能探针（§8.3.6，后端 factor_mining_generations 的 probe 与 cache_validation 字段全列） */
export interface PerfProbeGeneration {
  generation: number;
  probe_data_load_ms?: number | null;
  probe_ast_eval_ms?: number | null;
  probe_subexpr_compute_ms?: number | null;
  probe_factor_assemble_ms?: number | null;
  probe_metric_calc_ms?: number | null;
  probe_db_write_ms?: number | null;
  probe_subexpr_total?: number | null;
  probe_subexpr_unique?: number | null;
  probe_g2_hit_rate?: number | null;
  cache_validation_passed?: number | null;
  cache_validation_max_diff?: number | null;
  [key: string]: unknown;
}

export interface RunProgress {
  run_id: string;
  status: RunStatus;
  generation: number;
  max_generations: number;
  /** §8.2：排队位次（>0 表示在 duckdb_write 队列中等待；1 = 队首） */
  queue_position?: number | null;
  best_icir?: number | null;
  avg_icir?: number | null;
  /** 种群多样性 ∈[0,1]；<0.3 视为早熟 */
  diversity?: number | null;
  /** 收敛阈值（来自配置，**不是门禁**） */
  convergence_threshold?: number;
  converged: boolean;
  /** 连续无显著进步的代数（用于「建议停止」提示） */
  stall_generations?: number;
  eta_seconds?: number | null;
  population_size?: number | null;
  final_validation?: FinalValidationProgress | null;
  curve?: CurvePoint[];
  /** M2 多样性 health 曲线（双轴右轴，来自 generations 表） */
  diversity_curve?: DiversityCurvePoint[];
  /**
   * §8.3.6 资源占用（可选）—— 后端探针上报后才展示，缺省时界面显示等待态，
   * **不由前端估算**（避免显示假数据）。
   */
  resources?: RunResources | null;
  /** §8.3.6 运行日志（可选，按代分组；缺省时显示空态） */
  logs?: RunLogEntry[] | null;
}

/** §8.3.6 资源占用（百分比 0~100；缺项不展示该条） */
export interface RunResources {
  cpu_percent?: number | null;
  memory_percent?: number | null;
  disk_percent?: number | null;
  /** 进化速度（代/分钟），后端测量值，前端不推算 */
  generations_per_minute?: number | null;
}

/** §8.3.6 运行日志条目 */
export interface RunLogEntry {
  generation: number;
  level?: "info" | "warn" | "error" | string | null;
  message: string;
}

/** §8.3.3 当前种群 Top 因子（F2：新进 Top10 高亮闪烁） */
export interface TopFactor {
  /** 排名（1 起，<= finalValidationTop） */
  rank: number;
  formula: string;
  /** 血缘来源描述（精英保留/变异自第 N 代#m/交叉/随机/AI 生成） */
  source?: string | null;
  icir?: number | null;
  coverage?: number | null;
  canvas?: string | null;
}

/** 多样性早熟阈值（向导 §8.3.1：低于 30% 告警） */
export const PREMATURE_DIVERSITY = 0.3;

/** 「建议停止」所需的连续停滞代数 */
export const STALL_SUGGEST_STOP = 3;
