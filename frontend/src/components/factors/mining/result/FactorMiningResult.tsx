import { useState } from "react";
import { t } from "../../../../i18n";
import { GRADE_CHIPS, contextComplete } from "./resultTypes";
import type { MiningResultContext, MiningResultRow, QualityGrade } from "./resultTypes";

/**
 * 结果总览（向导 §8.4，任务卡 pitfalls / not_do）。
 *
 * - **顶部必须标注研究声明**（pitfalls 第一条）—— 作为页面第一个区块；
 * - **不自动激活因子或模型**（not_do）：本组件不在挂载时调用任何激活/跳转回调；
 * - **跳转因子模型页必须携带 4 项**：factor_set_id / data_cutoff_at /
 *   candidate_pool_snapshot_id / rebalance_frequency（缺项则按钮禁用）；
 * - 等级 chip 筛选（全部/S/A/B/C/D 含数量）；**D 级默认不勾选**；
 *   选中集含 D 级时批量操作**二次确认**（§8.4 按等级批量处理）。
 */
export interface FactorMiningResultProps {
  rows?: MiningResultRow[];
  context?: MiningResultContext | null;
  onActivate?: (candidateId: string) => void;
  onGotoFactorModel?: (ctx: MiningResultContext) => void;
  onBatchAdd?: (candidateIds: string[]) => void;
  onExport?: () => void;
}

const SOURCE_LABEL: Record<string, string> = {
  elite: "精英保留",
  mutation: "变异",
  crossover: "交叉",
  random: "随机注入",
  ai: "AI 生成",
  enumerated: "枚举",
};

export default function FactorMiningResult({
  rows = [],
  context = null,
  onActivate,
  onGotoFactorModel,
  onBatchAdd,
  onExport,
}: FactorMiningResultProps) {
  const [filter, setFilter] = useState<QualityGrade | "all">("all");
  const [selected, setSelected] = useState<string[]>([]);
  const [confirmDGrade, setConfirmDGrade] = useState(false);

  const countOf = (g: QualityGrade | "all") =>
    g === "all" ? rows.length : rows.filter((r) => r.grade === g).length;
  const visible = filter === "all" ? rows : rows.filter((r) => r.grade === filter);
  const gradedRows = selected
    .map((id) => rows.find((r) => r.candidate_id === id))
    .filter(Boolean) as MiningResultRow[];
  const selectedDGrades = gradedRows.filter((r) => r.grade === "D").length;
  const canGoto = contextComplete(context);

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const doBatchAdd = () => {
    if (selectedDGrades > 0) {
      setConfirmDGrade(true);
      return;
    }
    onBatchAdd?.(selected);
  };

  return (
    <div className="mining-result-page" data-result-page>
      {/* ① 研究声明（必须在顶部，固定第一个区块） */}
      <div className="mining-result-disclaimer" data-result-disclaimer role="note">
        {t("miningResultDisclaimer")}
      </div>

      {/* ② 概览 + 操作 */}
      <div className="mining-result-head">
        <span>{t("miningResultValidCount")}: {rows.length}</span>
        <button type="button" data-result-export onClick={onExport}>
          {t("miningResultExport")}
        </button>
        <button
          type="button"
          data-result-goto-model
          disabled={!canGoto}
          onClick={() => {
            if (canGoto && context) onGotoFactorModel?.(context);
          }}
        >
          {t("miningResultGotoModel")}
        </button>
      </div>

      {/* ③ 等级 chip */}
      <div className="mining-result-chips">
        {GRADE_CHIPS.map((g) => (
          <button
            key={g}
            type="button"
            data-grade-chip={g}
            aria-pressed={filter === g}
            onClick={() => setFilter(g)}
          >
            {g === "all" ? t("miningResultGradeAll") : g}（{countOf(g)}）
          </button>
        ))}
      </div>

      {/* ④ 批量操作条 */}
      <div className="mining-result-batch">
        <span data-result-selected-count>
          {t("miningResultSelected")}: {selected.length}
        </span>
        <button type="button" data-result-batch-add onClick={doBatchAdd}>
          {t("miningResultBatchAdd")}
        </button>
      </div>

      {/* ⑤ 因子列表 */}
      <table className="mining-result-table">
        <thead>
          <tr>
            <th />
            <th>{t("miningResultColFormula")}</th>
            <th>{t("miningResultColGrade")}</th>
            <th>{t("miningResultColAdjusted")}</th>
            <th>{t("miningResultColDecay")}</th>
            <th>{t("miningResultColSource")}</th>
          </tr>
        </thead>
        <tbody data-result-rows>
          {visible.map((r) => (
            <tr key={r.candidate_id}>
              <td>
                <input
                  type="checkbox"
                  data-result-checkbox={r.candidate_id}
                  checked={selected.includes(r.candidate_id)}
                  onChange={() => toggle(r.candidate_id)}
                />
              </td>
              <td>{r.formula}</td>
              <td data-result-grade={r.candidate_id}>{r.grade}</td>
              <td>{r.icir_adjusted}</td>
              <td>{r.decay_ratio ?? "-"}</td>
              <td>{SOURCE_LABEL[String(r.source ?? "")] ?? r.source ?? "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* ⑥ D 级二次确认 */}
      {confirmDGrade && (
        <div className="mining-result-confirm" role="dialog">
          <div data-result-dgrade-confirm>
            {t("miningResultDGradeConfirm")
              .replace("{total}", String(selected.length))
              .replace("{dgrade}", String(selectedDGrades))}
            <button type="button" data-result-dgrade-cancel
              onClick={() => setConfirmDGrade(false)}>
              {t("miningResultCancel")}
            </button>
            <button
              type="button"
              data-result-dgrade-ok
              onClick={() => {
                setConfirmDGrade(false);
                onBatchAdd?.(selected);
              }}
            >
              {t("miningResultConfirm")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
