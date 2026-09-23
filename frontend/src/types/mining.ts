/**
 * 因子挖掘域前端类型（T27）。
 *
 * 契约来源：后端 `app/api/routes/factor_mining.py`
 *   - `MiningRunCreate` / `MiningRunCreated` / `Page`
 *   - `app/models/factor_mining.py` → `FactorMiningRun`
 * 向导步骤来源：docs/因子挖掘实验向导-详细设计.md §2（5 步）
 *
 * 注意：后端多数接口仍为 `NotImplementedError`（M5/M7/M10/M12/M13 待实现），
 * 类型按**契约先行**定义，UI 侧须容错（见 MiningShell 的空态/错误态）。
 */

export type MiningFrequency = "daily" | "weekly" | "monthly";

/** 质量等级（M2 分级；S/A/B/C/D） */
export type MiningGrade = "S" | "A" | "B" | "C" | "D" | string;

/** 批次状态 */
export type MiningRunStatus =
  | "queued"
  | "running"
  | "paused"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "discarded"
  | string;

/** 后端 `MiningRunCreate`（POST /factor-mining/runs） */
export interface MiningRunCreate {
  draft_id?: string | null;
  template_id?: string | null;
  candidate_pool_snapshot_id: string;
  data_cutoff_at: string;
  start_date: string;
  end_date: string;
  rebalance_frequency: MiningFrequency;
  target_horizon?: number;
  train_ratio?: number;
  validation_ratio?: number;
  random_seed?: number;
  evolution_params?: Record<string, unknown>;
  filter_config?: Record<string, unknown>;
}

/** 后端 `MiningRunCreated`（POST /factor-mining/runs 返回） */
export interface MiningRunCreated {
  run_id: string;
  task_id: string;
  queue_position: number;
  eta_seconds: number | null;
  split_budget: Record<string, unknown> | null;
}

/** 批次（GET /factor-mining/runs/{run_id}、列表 items） */
export interface MiningRun {
  id: string;
  status: MiningRunStatus;
  /** 挖掘快照 id（GET /runs/{id} 返回；结果页「跳转因子模型页」上下文用） */
  candidate_pool_snapshot_id?: string | null;
  /** 数据截止日（GET /runs/{id} 返回；同上） */
  data_cutoff_at?: string | null;
  current_generation?: number | null;
  total_generations?: number | null;
  progress?: number | null;
  queue_position?: number | null;
  best_icir?: number | null;
  rebalance_frequency?: MiningFrequency | null;
  target_horizon?: number | null;
  start_date?: string | null;
  end_date?: string | null;
  error_code?: string | null;
  message?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** 后端 `Page`（GET /factor-mining/runs） */
export interface MiningPage<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** 候选因子（GET /factor-mining/runs/{run_id}/candidates，A3 已实现） */
export interface MiningCandidate {
  id: string;
  run_id: string;
  factor_code?: string | null;
  factor_version_id?: string | null;
  generation?: number | null;
  category?: string | null;
  grade?: MiningGrade | null;
  icir?: number | null;
  icir_adjusted?: number | null;
  decay_ratio?: number | null;
  formula?: string | null;
  economic_logic?: string | null;
  // ── A3 真实契约（service.list_candidates 序列化）──
  formula_expr?: string | null;
  canonical_formula?: string | null;
  formula_hash?: string | null;
  operation?: string | null;
  generation_icir?: number | null;
  generation_coverage?: number | null;
  generation_complexity?: number | null;
  generation_turnover?: number | null;
  generation_rank?: number | null;
  crowding_distance?: number | null;
  expected_direction?: string | null;
}

/** 双锁状态（GET /factor-mining/locks/status，后端**已实现**；2026-09-21 对齐真实契约） */
export interface MiningLockStatus {
  /** mining_domain 锁：域内唯一，冲突直接拒绝（不排队） */
  miningDomain: {
    busy: boolean;
    taskId?: string | null;
    runId?: string | null;
    acquiredAt?: string | null;
    heartbeatAt?: string | null;
  };
  /** duckdb_write 锁：写库互斥，冲突排队 */
  duckdbWrite: {
    busy: boolean;
    taskId?: string | null;
    /** 排队任务 id 列表（前面的排队数 = queue.length） */
    queue: string[];
  };
}

/** 切分预算（POST /factor-mining/split-budget；对齐后端 `contracts.SplitBudget` snake_case） */
export interface SplitBudget {
  frequency: MiningFrequency;
  total_points: number;
  train_points: number;
  val_points: number;
  test_points: number;
  purge_points: number;
  embargo_points: number;
  purge_trading_days: number;
  embargo_trading_days: number;
  tail_loss: number;
  frequency_floor: number;
  meets_floor: boolean;
  /** 月频 → True（不做 Bootstrap/置换，最高 B 级） */
  statistically_degraded: boolean;
  train_start?: string | null;
  train_end?: string | null;
  val_end?: string | null;
}

/** 向导 5 步（详细设计 §2：候选股票池/时间与目标/字段与校验/进化参数/进化执行与结果） */
export const MINING_STEPS = [
  "pool",
  "time-target",
  "field",
  "evolution",
  "run",
] as const;

export type MiningStepKey = (typeof MINING_STEPS)[number];

/** 步骤条状态：已完成(蓝)/当前(蓝+进度)/阻断(红)/未到(灰) */
export type MiningStepState = "done" | "current" | "blocked" | "todo";
