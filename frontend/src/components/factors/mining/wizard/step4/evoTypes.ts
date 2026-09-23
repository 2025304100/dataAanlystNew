/**
 * Step4 进化参数与资源确认的类型（T31）—— 契约对齐后端
 * `GET /factor-mining/locks/status`（**已实现**，T21/T23）与 `POST /factor-mining/runs`。
 *
 * 口径（向导 §7）：双锁是**两个独立概念**——
 * - `mining_domain`：域内唯一，冲突 → **直接拒绝**（禁用提交，不排队）；
 * - `duckdb_write`：写库互斥，冲突 → **排队**并给出位次（ETA 必须标「估算」）。
 *
 * 2026-09-21：`MiningLockStatus` 契约对齐后端真实响应（camelCase：
 * `miningDomain` / `duckdbWrite` / `taskId` / `queue`），由 types/mining 统一导出。
 */

import type { MiningLockStatus } from "../../../../../types/mining";

export type { MiningLockStatus };

/** 进化强度（简单模式三档） */
export type EvoStrength = "conservative" | "balanced" | "aggressive";

/** 选优偏好（简单模式；默认 balanced） */
export type EvoPreference = "balanced" | "stable" | "aggressive" | "simple";

/** §6.2：进化强度 → 最大代数（弱 5 / 中 10 / 强 20），Slider marks 用 */
export const STRENGTH_ORDER: EvoStrength[] = ["conservative", "balanced", "aggressive"];
export const STRENGTH_TO_GENERATIONS: Record<EvoStrength, number> = {
  conservative: 5,
  balanced: 10,
  aggressive: 20,
};

/** §6.2：参数推荐值/安全上限/默认（高级模式 Tooltip 展示用途） */
export const PARAM_META: Record<
  string,
  { labelKey: string; default: number; min: number; max: number; noteKey: string }
> = {
  population_size: { labelKey: "miningEvoPopulation", default: 100, min: 50, max: 300, noteKey: "miningEvoPopNote" },
  max_generations: { labelKey: "miningEvoGenerations", default: 20, min: 5, max: 50, noteKey: "miningEvoGenNote" },
  mutation_rate: { labelKey: "miningEvoMutationRate", default: 60, min: 40, max: 80, noteKey: "miningEvoMutationNote" },
  crossover_rate: { labelKey: "miningEvoCrossoverRate", default: 30, min: 10, max: 50, noteKey: "miningEvoCrossoverNote" },
  random_rate: { labelKey: "miningEvoRandomRate", default: 10, min: 5, max: 20, noteKey: "miningEvoRandomNote" },
  tournament_k: { labelKey: "miningEvoTournamentK", default: 3, min: 2, max: 7, noteKey: "miningEvoTournamentNote" },
};

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
  // 高级模式（向导 §6.5.5 / §6.6.8）：折叠区 onChange 直接透传到 evolution_params
  ai_ratio?: number;
  random_ratio?: number;
  track_enabled?: boolean;
  track_floor_ratio?: number;
  weak_generations?: number;
  cross_category_ratio?: number;
  category_limits?: Record<string, number>;
  coverage_strategy?: "keep" | "drop" | "reselect";
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
