import { useState } from "react";
import { t } from "../../../../i18n";
import type { Grade } from "../result/gradeEvidenceTypes";

/**
 * F1 经验库列表页（向导 §6.9.7 / §8.4，T37）。
 *
 * - 等级筛选 chip（全部 / S / A / B / C / D，含各级数量）；
 * - 列表展示公式、等级标签、校正后 ICIR、衰减率；点击行打开**证据抽屉**；
 * - **D 级默认不进批量操作选择集**（pitfalls 第一条）—— 勾选与「全选」均跳过 D 级，
 *   需人工显式勾选单个 D 级因子才会入集；
 * - not_do：本期不做 4 Tab / 导入导出 / 清理建议 / 共享库。
 */
export interface MiningExperienceRow {
  candidate_id: string;
  formula: string;
  grade: Grade;
  icir_adjusted?: number | null;
  decay_ratio?: number | null;
  /** 等级变更 7 天内的「新」角标（§8.4.3） */
  grade_changed_recently?: boolean;
}

export interface MiningExperiencePageProps {
  rows?: MiningExperienceRow[];
  onOpenEvidence?: (candidateId: string) => void;
  onBatchReview?: (candidateIds: string[]) => void;
}

const GRADE_CHIPS: Array<Grade | "all"> = ["all", "S", "A", "B", "C", "D"];

export default function MiningExperiencePage({
  rows = [],
  onOpenEvidence,
  onBatchReview,
}: MiningExperiencePageProps) {
  const [filter, setFilter] = useState<Grade | "all">("all");
  const [selected, setSelected] = useState<string[]>([]);

  const countOf = (g: Grade | "all") =>
    g === "all" ? rows.length : rows.filter((r) => r.grade === g).length;
  const visible = filter === "all" ? rows : rows.filter((r) => r.grade === filter);

  const toggle = (row: MiningExperienceRow) => {
    setSelected((prev) =>
      prev.includes(row.candidate_id)
        ? prev.filter((x) => x !== row.candidate_id)
        : [...prev, row.candidate_id]);
  };

  /** 全选：**D 级默认不进选择集** —— 全选只覆盖非 D 级 */
  const selectAllNonD = () =>
    setSelected(visible.filter((r) => r.grade !== "D").map((r) => r.candidate_id));

  return (
    <div className="mining-exp-page" data-exp-page>
      <div className="mining-exp-chips">
        {GRADE_CHIPS.map((g) => (
          <button
            key={g}
            type="button"
            data-exp-grade-chip={g}
            aria-pressed={filter === g}
            onClick={() => setFilter(g)}
          >
            {g === "all" ? t("miningResultGradeAll") : g}（{countOf(g)}）
          </button>
        ))}
      </div>

      <div className="mining-exp-actions">
        <span data-exp-selected-count>{t("miningResultSelected")}: {selected.length}</span>
        <button type="button" data-exp-select-all-non-d onClick={selectAllNonD}>
          {t("miningExpSelectAllNonD")}
        </button>
        <button
          type="button"
          data-exp-batch-review
          disabled={selected.length === 0}
          onClick={() => onBatchReview?.(selected)}
        >
          {t("miningExpBatchReview")}
        </button>
      </div>

      <table className="mining-exp-table">
        <thead>
          <tr>
            <th />
            <th>{t("miningResultColFormula")}</th>
            <th>{t("miningResultColGrade")}</th>
            <th>{t("miningResultColAdjusted")}</th>
            <th>{t("miningResultColDecay")}</th>
          </tr>
        </thead>
        <tbody data-exp-rows>
          {visible.length === 0 && (
            <tr><td colSpan={5}>{t("miningExpNoData")}</td></tr>
          )}
          {visible.map((r) => (
            <tr key={r.candidate_id} data-exp-row={r.candidate_id}>
              <td>
                <input
                  type="checkbox"
                  data-exp-checkbox={r.candidate_id}
                  checked={selected.includes(r.candidate_id)}
                  onChange={() => toggle(r)}
                />
              </td>
              <td>
                <button
                  type="button"
                  data-exp-open-evidence={r.candidate_id}
                  onClick={() => onOpenEvidence?.(r.candidate_id)}
                >
                  {r.formula}
                </button>
              </td>
              <td data-exp-grade={r.candidate_id}>
                {r.grade}
                {r.grade_changed_recently ? (
                  <span data-exp-new-badge>{t("miningExpNewBadge")}</span>
                ) : null}
              </td>
              <td>{r.icir_adjusted ?? "-"}</td>
              <td>{r.decay_ratio ?? "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
