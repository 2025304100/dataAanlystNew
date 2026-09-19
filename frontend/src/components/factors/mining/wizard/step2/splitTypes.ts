/**
 * Step2 时间与目标的类型（T29）—— 契约对齐后端 `contracts.SplitBudget`
 * （`app/services/factors/mining/contracts.py`）与 `MiningRunCreate`。
 *
 * ⚠️ **not_do：不在前端计算分位数或切分** —— 所有点数/边界一律由后端
 * `compute_split_budget()` / `build_time_split()` 产出后下发，前端只做
 * 「收集配置 + 原样展示」，不本地推算段边界。
 */

export type MiningFrequency = "daily" | "weekly" | "monthly";

/** 预测目标类型（向导 §4） */
export type MiningTargetType = "simple" | "log" | "excess" | "rank";

/** 切分方式：按比例 / 自定义日期边界 */
export type SplitMode = "ratio" | "custom";

/** 三段比例（百分比整数，和必须为 100） */
export interface SplitRatios {
  train: number;
  val: number;
  test: number;
}

/** 后端 `SplitBudget`（展示用；字段与后端一一对应，不新增前端自算字段） */
export interface SplitBudget {
  frequency: MiningFrequency;
  total_points: number;
  train_points: number;
  val_points: number;
  test_points: number;
  /** purge 调仓点数（**必须**与 `purge_trading_days` 同时展示） */
  purge_points: number;
  embargo_points: number;
  /** purge 折算交易日数（用户视角；与点数口径不同） */
  purge_trading_days: number;
  embargo_trading_days: number;
  tail_loss: number;
  frequency_floor: number;
  meets_floor: boolean;
  /** 月频 → True（不做 Bootstrap/置换，最高 B 级） */
  statistically_degraded: boolean;
  /** 段边界（后端下发） */
  train_start?: string | null;
  train_end?: string | null;
  val_end?: string | null;
}

/** 最小样本量硬门槛（向导 §4：日 252 / 周 104 / 月 36） */
export const FREQUENCY_FLOOR: Record<MiningFrequency, number> = {
  daily: 252,
  weekly: 104,
  monthly: 36,
};

/** 生产建议样本量（周 156 / 月 60；日频沿用硬门槛） */
export const FREQUENCY_RECOMMENDED: Record<MiningFrequency, number> = {
  daily: 252,
  weekly: 156,
  monthly: 60,
};
