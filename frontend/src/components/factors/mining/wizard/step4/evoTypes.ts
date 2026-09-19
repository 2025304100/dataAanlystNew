/**
 * Step4 进化参数与资源确认的类型（T31）—— 契约对齐后端
 * `GET /factor-mining/locks/status`（**已实现**，T21/T23）与 `POST /factor-mining/runs`。
 *
 * 口径（向导 §7）：双锁是**两个独立概念**——
 * - `mining_domain`：域内唯一，冲突 → **直接拒绝**（禁用提交，不排队）；
 * - `duckdb_write`：写库互斥，冲突 → **排队**并给出位次与 ETA（ETA 必须标「估算」）。
 */

export interface MiningDomainLock {
  busy: boolean;
  owner_task_id?: string | null;
  owner_status?: string | null;
  owner_generation?: number | null;
}

export interface DuckdbWriteLock {
  busy: boolean;
  owner_task_id?: string | null;
  owner_task_type?: string | null;
  /** 排队位次（1 表示队首；0/undefined 表示未排队） */
  queue_position?: number;
  eta_seconds?: number | null;
}

/** 双锁状态（GET /factor-mining/locks/status） */
export interface MiningLockStatus {
  mining_domain: MiningDomainLock;
  duckdb_write: DuckdbWriteLock;
}

/** 进化强度（简单模式三档） */
export type EvoStrength = "conservative" | "balanced" | "aggressive";

/** 选优偏好（简单模式；默认 balanced） */
export type EvoPreference = "balanced" | "stable" | "aggressive" | "simple";

/** Step4 配置（提交给后端 `MiningRunCreate.evolution_params`） */
export interface EvoConfig {
  simple_mode: boolean;
  strength: EvoStrength;
  preference: EvoPreference;
  ai_enabled: boolean;
  population_size: number;
  max_generations: number;
  adaptive: boolean;
  mutation_rate?: number;
  crossover_rate?: number;
  random_rate?: number;
  tournament_k?: number;
}

/** 默认配置（正式运行质量；快速试验模式会覆盖种群/代数） */
export const DEFAULT_EVO_CONFIG: EvoConfig = {
  simple_mode: true,
  strength: "balanced",
  preference: "balanced",
  ai_enabled: true,
  population_size: 100,
  max_generations: 20,
  adaptive: true,
};

/** 快速试验模式预设（向导 §7：种群 50 / 代数 10） */
export const QUICK_TRIAL_PRESET = { population_size: 50, max_generations: 10 };

/** ETA 展示：秒 → 「X 分钟」并**必须**带「估算」标注口径（由 i18n 提供文案） */
export function formatEtaMinutes(seconds: number | null | undefined): number | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return null;
  return Math.max(1, Math.round(seconds / 60));
}
