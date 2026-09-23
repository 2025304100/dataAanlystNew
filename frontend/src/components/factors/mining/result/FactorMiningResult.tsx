import { useMemo, useState } from "react";
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
 *
 * 列表按**校正后 ICIR 降序**展示，并补齐原始 ICIR / 覆盖率 / 换手率列。
 * ⚠️ `data-result-*` / `data-grade-chip` / `data-pareto-*` 是测试钩子
 * （含逐点数量断言），不得改动。
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

/** 质量分级数值序（用于「等级变更标记」等展示） */
const GRADE_ORDER: QualityGrade[] = ["S", "A", "B", "C", "D"];

function gradeClass(grade: QualityGrade): string {
  const key = GRADE_ORDER.includes(grade) ? grade.toLowerCase() : "d";
  return `mining-grade mining-grade--${key}`;
}

function fmt(value: number | null | undefined, digits = 3): string {
  return value == null ? "-" : Number(value).toFixed(digits);
}

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
  const sorted = useMemo(
    () => rows.slice().sort((a, b) => (b.icir_adjusted ?? 0) - (a.icir_adjusted ?? 0)),
    [rows],
  );
  const visible = filter === "all" ? sorted : sorted.filter((r) => r.grade === filter);
  const gradedRows = selected
    .map((id) => rows.find((r) => r.candidate_id === id))
    .filter(Boolean) as MiningResultRow[];
  const selectedDGrades = gradedRows.filter((r) => r.grade === "D").length;
  const canGoto = contextComplete(context);

  // 帕累托散点（§6.5.6：X=换手率、Y=ICIR、颜色=覆盖率、大小=复杂度、rank=1 高亮）
  const paretoPoints = rows
    .filter((r) => r.icir != null && r.turnover != null)
    .map((r) => ({
      id: r.candidate_id,
      x: Math.min(1, Math.max(0, r.turnover ?? 0)),
      y: Math.min(1, Math.max(0, r.icir)),
      coverage: Math.min(1, Math.max(0, r.coverage ?? 0)),
      complexity: r.complexity ?? 3,
      rank: r.generation_rank,
      formula: r.formula,
    }));

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const doBatchAdd = () => {
    if (selectedDGrades > 0) {
      setConfirmDGrade(true);
      return;
    }
    onBatchAdd?.(selected);
  };

  // 帕累托散点坐标系（560×220）
  const pw = 560;
  const ph = 220;
  const padL = 48;
  const padR = 16;
  const padT = 14;
  const padB = 34;
  const px = (v: number) => padL + v * (pw - padL - padR);
  const py = (v: number) => padT + (1 - v) * (ph - padT - padB);

  return (
    <div className="mining-result-page" data-result-page>
      {/* ① 研究声明（必须在顶部，固定第一个区块） */}
      <div className="mining-result-disclaimer" data-result-disclaimer role="note">
        {t("miningResultDisclaimer")}
      </div>

      {/* ② 概览磁贴 */}
      <div className="mining-tiles">
        <div className="mining-tile mining-tile--brand">
          <span className="mining-tile-label">{t("miningResultValidCount")}</span>
          <span className="mining-tile-value">{rows.length}</span>
          <span className="mining-tile-note">{t("miningResultGradeLegend")}</span>
        </div>
        {GRADE_ORDER.map((g) => (
          <div className="mining-tile" key={g}>
            <span className="mining-tile-label">{t("miningResultColGrade")} {g}</span>
            <span className="mining-tile-value">{countOf(g)}</span>
          </div>
        ))}
      </div>

      {/* ③ 概览 + 操作 */}
      <div className="mining-result-head">
        <span>
          {t("miningResultValidCount")}: {rows.length}
          <span className="mining-card-sub" style={{ fontWeight: 400 }}>
            {t("miningResultSortHint")}
          </span>
        </span>
        <button type="button" data-result-export onClick={onExport}>
          {t("miningResultExport")}
        </button>
        <button
          type="button"
          data-result-goto-model
          disabled={!canGoto}
          title={canGoto ? undefined : t("miningResultGotoDisabledHint")}
          onClick={() => {
            if (canGoto && context) onGotoFactorModel?.(context);
          }}
        >
          {t("miningResultGotoModel")}
        </button>
      </div>

      {/* ④ 等级 chip */}
      <div className="mining-result-chips">
        {GRADE_CHIPS.map((g) => (
          <button
            key={g}
            type="button"
            data-grade-chip={g}
            aria-pressed={filter === g}
            className={filter === g ? "on" : undefined}
            onClick={() => setFilter(g)}
          >
            {g === "all" ? t("miningResultGradeAll") : g}（{countOf(g)}）
          </button>
        ))}
      </div>

      {/* ⑤ 批量操作条 */}
      <div className="mining-result-batch">
        <span data-result-selected-count>
          {t("miningResultSelected")}: {selected.length}
          {selected.length === 0 ? `（${t("miningResultSelectedNone")}）` : ""}
        </span>
        <span className="mining-chip mining-chip--muted">
          {t("miningResultDGradeHint")}
        </span>
        <button type="button" data-result-batch-add onClick={doBatchAdd}>
          {t("miningResultBatchAdd")}
        </button>
      </div>

      {/* ⑥ 帕累托前沿散点（§6.5.6） */}
      {paretoPoints.length > 0 && (
        <div className="mining-result-pareto" data-result-pareto>
          <div className="mining-result-pareto-title">{t("miningResultParetoTitle")}</div>
          <svg
            viewBox={`0 0 ${pw} ${ph}`}
            width={pw}
            height={ph}
            role="img"
            aria-label={t("miningResultParetoTitle")}
          >
            {/* 网格 + 轴 */}
            {[0.25, 0.5, 0.75, 1].map((v) => (
              <g key={`g-${v}`}>
                <line
                  x1={px(v)}
                  y1={py(0)}
                  x2={px(v)}
                  y2={py(1)}
                  stroke="rgba(31,41,51,.06)"
                />
                <line
                  x1={px(0)}
                  y1={py(v)}
                  x2={px(1)}
                  y2={py(v)}
                  stroke="rgba(31,41,51,.06)"
                />
              </g>
            ))}
            <line x1={px(0)} y1={py(0)} x2={px(1)} y2={py(0)} stroke="rgba(31,41,51,.28)" />
            <line x1={px(0)} y1={py(0)} x2={px(0)} y2={py(1)} stroke="rgba(31,41,51,.28)" />
            {[0, 0.5, 1].map((v) => (
              <g key={`t-${v}`}>
                <text
                  x={px(v)}
                  y={ph - 12}
                  textAnchor="middle"
                  fontSize={10}
                  fill="#7b8794"
                >
                  {v.toFixed(2)}
                </text>
                <text
                  x={padL - 8}
                  y={py(v) + 3.5}
                  textAnchor="end"
                  fontSize={10}
                  fill="#7b8794"
                >
                  {v.toFixed(2)}
                </text>
              </g>
            ))}
            {paretoPoints.map((p) => {
              const frontier = p.rank === 1;
              return (
                <circle
                  key={p.id}
                  cx={px(p.x)}
                  cy={py(p.y)}
                  r={Math.max(3, Math.min(10, 3 + p.complexity))}
                  data-pareto-point={p.id}
                  data-pareto-frontier={frontier ? p.id : undefined}
                  fill={`hsl(174 70% ${Math.round(46 - p.coverage * 20)}%)`}
                  fillOpacity={0.72}
                  stroke={frontier ? "#0f766e" : "rgba(31,41,51,.18)"}
                  strokeWidth={frontier ? 2 : 1}
                >
                  <title>
                    {`${p.formula} · ICIR ${p.y.toFixed(3)} · ${t("miningResultParetoAxisX")} ${p.x.toFixed(2)}`}
                  </title>
                </circle>
              );
            })}
          </svg>
          <div className="mining-result-pareto-legend">
            <span>{t("miningResultParetoAxisX")}</span>
            <span>{t("miningResultParetoAxisY")}</span>
            <span>{t("miningResultParetoColor")}</span>
            <span>{t("miningResultParetoSize")}</span>
            <span>{t("miningResultParetoFrontier")}</span>
          </div>
        </div>
      )}

      {/* ⑦ 因子列表 */}
      <div className="mining-table-wrap">
        <table className="mining-result-table">
          <thead>
            <tr>
              <th style={{ width: 40 }} />
              <th>{t("miningResultColFormula")}</th>
              <th>{t("miningResultColGrade")}</th>
              <th className="mining-cell-num">{t("miningResultColRaw")}</th>
              <th className="mining-cell-num">{t("miningResultColAdjusted")}</th>
              <th className="mining-cell-num">{t("miningResultColCoverageShort")}</th>
              <th className="mining-cell-num">{t("miningResultColDecay")}</th>
              <th>{t("miningResultColSource")}</th>
            </tr>
          </thead>
          <tbody data-result-rows>
            {visible.length === 0 && (
              <tr className="mining-table-empty">
                <td colSpan={8}>{t("miningResultEmpty")}</td>
              </tr>
            )}
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
                <td className="mining-result-formula">{r.formula}</td>
                <td data-result-grade={r.candidate_id}>
                  <span className={gradeClass(r.grade)}>{r.grade}</span>
                </td>
                <td className="mining-cell-num">{fmt(r.icir)}</td>
                <td className="mining-cell-num">{fmt(r.icir_adjusted)}</td>
                <td className="mining-cell-num">
                  {r.coverage != null ? `${(r.coverage * 100).toFixed(0)}%` : "-"}
                </td>
                <td className="mining-cell-num">{fmt(r.decay_ratio, 2)}</td>
                <td>{SOURCE_LABEL[String(r.source ?? "")] ?? r.source ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* ⑧ D 级二次确认 */}
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
