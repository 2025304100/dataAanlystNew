/**
 * 结果总览类型（T32）—— 契约对齐后端
 * `GET /factor-mining/runs/{id}/candidates`、`/candidates/{cid}/lineage`（M7，待实现）。
 *
 * 口径（向导 §8.4）：有效因子按**校正后 ICIR** 排序；展示质量分级标签（M2）；
 * **D 级默认不勾选**；跳转因子模型页必须携带 4 项上下文。
 */

export type QualityGrade = "S" | "A" | "B" | "C" | "D";

export type CandidateSource =
  | "elite" | "mutation" | "crossover" | "random" | "ai" | "enumerated" | string;

export interface MiningResultRow {
  candidate_id: string;
  formula: string;
  grade: QualityGrade;
  icir: number;
  /** 校正后 ICIR（Bonferroni/FDR）—— 列表默认排序依据 */
  icir_adjusted: number;
  coverage?: number | null;
  turnover?: number | null;
  decay_ratio?: number | null;
  source?: CandidateSource | null;
  generation?: number | null;
  lineage_parents?: string[];
}

/** 跳转因子模型页所需上下文（**四项缺一不可**，pitfalls 第二条） */
export interface MiningResultContext {
  factor_set_id?: string | null;
  data_cutoff_at?: string | null;
  candidate_pool_snapshot_id?: string | null;
  rebalance_frequency?: string | null;
}

/** 四项上下文是否齐备（缺项时禁止跳转，避免带残缺参数） */
export function contextComplete(ctx: MiningResultContext | null | undefined): boolean {
  if (!ctx) return false;
  return Boolean(
    ctx.factor_set_id &&
    ctx.data_cutoff_at &&
    ctx.candidate_pool_snapshot_id &&
    ctx.rebalance_frequency,
  );
}

export const GRADE_CHIPS: Array<QualityGrade | "all"> = ["all", "S", "A", "B", "C", "D"];
