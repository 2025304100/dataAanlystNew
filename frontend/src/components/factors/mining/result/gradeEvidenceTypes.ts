/**
 * 等级证据抽屉类型（T37）—— 契约对齐后端 T36 `factor_grading` + T35 `statistical_tests`。
 *
 * 口径（向导 §8.4.1）：抽屉回答「为什么是这个等级」——
 * Tab1 定级证据（8 维度 + 阈值来源）/ Tab2 统计检验 8 项 / Tab3 血缘与来源。
 * **月频**：Bootstrap 与置换两行灰掉并标注「样本不足，未计算」，顶部提示最高 B 级。
 */

export type Grade = "S" | "A" | "B" | "C" | "D";

export interface DimensionEvidence {
  key: string;
  current: number | null;
  threshold: number | null;
  passed: boolean;
  gap?: number | null;
}

export interface WalkForwardEvidence {
  windows?: number | null;
  same_direction?: number | null;
}

export interface StatsEvidence {
  t_test_p: number | null;
  bonferroni_p: number | null;
  fdr_q: number | null;
  /** 月频降级时为 null（不做 Bootstrap） */
  bootstrap_ci: [number, number] | null;
  /** 月频降级时为 null（不做置换检验） */
  permutation_p: number | null;
  dsr_icir: number | null;
  decay_ratio: number | null;
  walk_forward: WalkForwardEvidence;
  total_trials: number | null;
  degraded: boolean;
}

export interface LineageEvidence {
  generation?: number | null;
  parent_ids?: string[];
  operation?: string | null;
  economic_logic?: string | null;
  logic_source?: string | null;
}

export interface GradeHistoryItem {
  grade: Grade;
  changed_at: string;
  trigger: "auto" | "manual" | "quarterly" | string;
  reason?: string | null;
}

export interface QuarterChange {
  from: Grade;
  to: Grade;
  reason: string;
}

export interface GradeEvidence {
  candidate_id: string;
  formula: string;
  grade: Grade;
  reason_zh: string;
  thresholds_source: "default" | "custom";
  frequency: "daily" | "weekly" | "monthly" | string;
  dimensions: DimensionEvidence[];
  stats: StatsEvidence;
  lineage: LineageEvidence;
  manual_adjusted: boolean;
  grade_history?: GradeHistoryItem[];
  quarter_change?: QuarterChange | null;
  /** 非空表示已被移出该 FactorSet */
  removed_from_factor_set?: string | null;
}

/** 统计检验 8 项的展示定义（顺序固定） */
export const STAT_ROWS: Array<{ key: string; labelKey: string; thresholdKey?: string }> = [
  { key: "t_test", labelKey: "miningEvidStatTTest" },
  { key: "bonferroni", labelKey: "miningEvidStatBonferroni" },
  { key: "fdr", labelKey: "miningEvidStatFdr" },
  { key: "bootstrap", labelKey: "miningEvidStatBootstrap" },
  { key: "permutation", labelKey: "miningEvidStatPermutation" },
  { key: "dsr", labelKey: "miningEvidStatDsr" },
  { key: "decay", labelKey: "miningEvidStatDecay" },
  { key: "walk_forward", labelKey: "miningEvidStatWalkForward" },
];

/** 月频降级：Bootstrap / 置换不做（样本不足） */
export function isDegraded(ev: GradeEvidence): boolean {
  return ev.stats.degraded || ev.frequency === "monthly";
}

/** 人工调整原因最小字数（§8.4.2：必填且 ≥10 字） */
export const ADJUST_REASON_MIN = 10;
