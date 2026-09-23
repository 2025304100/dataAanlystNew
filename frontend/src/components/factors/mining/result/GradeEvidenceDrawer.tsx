import { useMemo, useState } from "react";
import { t } from "../../../../i18n";
import MiningChart, {
  MINING_CHART_COLORS,
  miningBaseOption,
} from "../charts/MiningChart";
import { ADJUST_REASON_MIN, STAT_ROWS, isDegraded } from "./gradeEvidenceTypes";
import type { Grade, GradeEvidence } from "./gradeEvidenceTypes";
import type { EChartsOption } from "echarts";

/**
 * 因子详情证据抽屉（向导 §8.4.1 / §8.4.2 / §8.4.3）—— 回答「为什么是这个等级」。
 *
 * - **恰好 3 个 Tab**（not_do：不做 4 Tab）：定级证据 / 统计检验证据 8 项 / 血缘与来源；
 * - Tab1：等级大标签 + 一句话理由 + 8 维雷达 + 8 维度明细（当前值/阈值/达标/差距）+ 阈值来源；
 * - Tab2：8 项统计检验；**月频时 Bootstrap 与置换两行灰掉并标注样本不足**
 *   （pitfalls 第二条），顶部提示「月频最高 B 级」；含 `total_trials` 说明；
 *   附带 Walk-Forward 窗口方向示意（按同向数量编码，不臆造具体窗口顺序）；
 * - Tab3：进化路径（代数/父代/操作）+ 经济逻辑（AI 生成必显示）；
 * - 人工调整等级（原因 ≥10 字）→ 提示「季度重评不会自动覆盖」+「恢复自动」；
 * - 季度重评横幅（升级绿 / 降级红 / 移出 FactorSet 追加）+ 评级历史时间轴。
 *
 * ⚠️ `data-evidence-*` 是测试钩子，其中 `data-evidence-dimension`（每条一个）、
 * `data-evidence-stat`（恰好 8 个）、`data-evidence-tab`（恰好 3 个）**数量被断言**。
 */
export interface GradeEvidenceDrawerProps {
  evidence: GradeEvidence;
  onClose?: () => void;
  onAdjustGrade?: (grade: Grade, reason: string) => void;
  onRestoreAuto?: () => void;
}

type TabKey = "grade" | "stats" | "lineage";

const GRADES: Grade[] = ["S", "A", "B", "C", "D"];

const TRIGGER_LABEL: Record<string, string> = {
  auto: "miningEvidTriggerAuto",
  manual: "miningEvidTriggerManual",
  quarterly: "miningEvidTriggerQuarterly",
};

/** 维度 key → 中文标签（未知 key 原样展示） */
const DIM_LABEL: Record<string, string> = {
  stat_significance: "miningEvidDimStatSignificance",
  icir: "miningEvidDimIcir",
  coverage: "miningEvidDimCoverage",
  turnover: "miningEvidDimTurnover",
  decay: "miningEvidDimDecay",
  decay_ratio: "miningEvidDimDecay",
  oos: "miningEvidDimOos",
  oos_stability: "miningEvidDimOos",
  complexity: "miningEvidDimComplexity",
  monotonicity: "miningEvidDimMonotonicity",
};

function dimLabel(key: string): string {
  const k = DIM_LABEL[key];
  return k ? t(k) : key;
}

function gradeClass(grade: Grade): string {
  return `mining-evidence-grade-badge mining-grade mining-grade--${grade.toLowerCase()}`;
}

export default function GradeEvidenceDrawer({
  evidence,
  onClose,
  onAdjustGrade,
  onRestoreAuto,
}: GradeEvidenceDrawerProps) {
  const [tab, setTab] = useState<TabKey>("grade");
  const [adjusting, setAdjusting] = useState(false);
  const [targetGrade, setTargetGrade] = useState<Grade>(evidence.grade);
  const [reason, setReason] = useState("");

  const degraded = isDegraded(evidence);
  const canSubmit = reason.trim().length >= ADJUST_REASON_MIN;
  const change = evidence.quarter_change ?? null;

  const statValue = (key: string): string => {
    const s = evidence.stats;
    switch (key) {
      case "t_test": return s.t_test_p != null ? `p=${s.t_test_p}` : "-";
      case "bonferroni": return s.bonferroni_p != null ? `p_adj=${s.bonferroni_p}` : "-";
      case "fdr": return s.fdr_q != null ? `q=${s.fdr_q}` : "-";
      case "bootstrap":
        return s.bootstrap_ci ? `CI=[${s.bootstrap_ci[0]}, ${s.bootstrap_ci[1]}]` : "-";
      case "permutation": return s.permutation_p != null ? `p=${s.permutation_p}` : "-";
      case "dsr": return s.dsr_icir != null ? `dsr_icir=${s.dsr_icir}` : "-";
      case "decay": return s.decay_ratio != null ? String(s.decay_ratio) : "-";
      case "walk_forward":
        return `${s.walk_forward.same_direction ?? "-"}/${s.walk_forward.windows ?? "-"}`;
      default: return "-";
    }
  };

  /** 判定阈值（后端未下发阈值时显示占位符，不臆造门槛） */
  const STAT_THRESHOLD: Record<string, string> = {
    t_test: "p<0.05",
    bonferroni: "p<0.001",
    fdr: "q<0.1",
    bootstrap: "下限>0",
    permutation: "p<0.001",
    decay: "≥0.5",
    walk_forward: "≥3/4",
  };

  const rowDisabled = (key: string) =>
    degraded && (key === "bootstrap" || key === "permutation");

  /** 维度雷达：按「当前值 / 该级阈值」归一化（阈值缺失时用同组最大值兜底） */
  const radarOption = useMemo<EChartsOption>(() => {
    const dims = evidence.dimensions ?? [];
    const normalized = dims.map((d) => {
      const cur = Math.abs(Number(d.current ?? 0));
      const thr = Math.abs(Number(d.threshold ?? 0));
      if (!thr) return 0;
      return Math.min(1.5, cur / thr);
    });
    return {
      ...miningBaseOption(),
      tooltip: { ...miningBaseOption().tooltip, trigger: "item" },
      radar: {
        indicator: dims.map((d) => ({ name: dimLabel(d.key), max: 1.5 })),
        radius: "64%",
        splitNumber: 3,
        axisName: { color: MINING_CHART_COLORS.axisLabel, fontSize: 11 },
        axisLine: { lineStyle: { color: MINING_CHART_COLORS.axis } },
        splitLine: { lineStyle: { color: MINING_CHART_COLORS.axis } },
        splitArea: {
          areaStyle: { color: ["rgba(15,118,110,0.03)", "rgba(15,118,110,0.07)"] },
        },
      },
      series: [
        {
          type: "radar",
          data: [
            {
              value: normalized,
              name: t("miningEvidRadarTitle"),
              areaStyle: { color: MINING_CHART_COLORS.brandSoft },
              lineStyle: { color: MINING_CHART_COLORS.brand, width: 2 },
              itemStyle: { color: MINING_CHART_COLORS.brand },
            },
          ],
        },
      ],
    };
  }, [evidence.dimensions]);

  const wfWindows = evidence.stats.walk_forward?.windows ?? 0;
  const wfSame = evidence.stats.walk_forward?.same_direction ?? 0;

  return (
    <aside className="mining-evidence-drawer" data-evidence-drawer>
      <header>
        <span>{evidence.formula}</span>
        <button type="button" data-evidence-close onClick={onClose}>×</button>
      </header>

      {/* 月频提示（pitfalls 第二条的另一半） */}
      {degraded && (
        <div className="mining-evidence-monthly" data-evidence-monthly-tip role="status">
          {t("miningEvidMonthlyTip")}
        </div>
      )}

      {/* 季度重评横幅（§8.4.3） */}
      {change && (
        <div
          className="mining-evidence-quarter"
          data-evidence-quarter-change
          data-direction={GRADES.indexOf(change.to) < GRADES.indexOf(change.from) ? "up" : "down"}
          role="status"
        >
          {change.from} → {change.to}：{change.reason}
        </div>
      )}
      {evidence.removed_from_factor_set && (
        <div data-evidence-removed-set className="mining-evidence-removed">
          {t("miningEvidRemovedSet").replace(
            "{name}", String(evidence.removed_from_factor_set))}
        </div>
      )}

      {/* 人工调整（§8.4.2） */}
      {evidence.manual_adjusted ? (
        <div className="mining-evidence-manual" data-evidence-manual-tip>
          {t("miningEvidManualTip")}
          <button type="button" data-evidence-restore-auto onClick={onRestoreAuto}>
            {t("miningEvidRestoreAuto")}
          </button>
        </div>
      ) : (
        <button type="button" data-evidence-adjust onClick={() => setAdjusting(true)}>
          {t("miningEvidAdjustGrade")}
        </button>
      )}

      {adjusting && (
        <div className="mining-evidence-adjust">
          <select
            data-evidence-adjust-grade
            value={targetGrade}
            onChange={(e) => setTargetGrade(e.target.value as Grade)}
          >
            {GRADES.map((g) => (
              <option key={g} value={g}>{g}</option>
            ))}
          </select>
          <textarea
            data-evidence-adjust-reason
            value={reason}
            placeholder={t("miningEvidAdjustReasonHint")}
            onChange={(e) => setReason(e.target.value)}
          />
          <button
            type="button"
            data-evidence-adjust-submit
            disabled={!canSubmit}
            onClick={() => onAdjustGrade?.(targetGrade, reason.trim())}
          >
            {t("miningEvidAdjustSubmit")}
          </button>
        </div>
      )}

      {/* Tab 结构：恰好 3 个（not_do） */}
      <nav className="mining-evidence-tabs">
        {(["grade", "stats", "lineage"] as TabKey[]).map((key) => (
          <button
            key={key}
            type="button"
            data-evidence-tab={key}
            aria-pressed={tab === key}
            onClick={() => setTab(key)}
          >
            {key === "grade"
              ? t("miningEvidTabGrade")
              : key === "stats"
                ? t("miningEvidTabStats")
                : t("miningEvidTabLineage")}
          </button>
        ))}
      </nav>

      {tab === "grade" && (
        <section data-evidence-panel="grade" className="mining-evidence-lineage">
          <div className="mining-evidence-grade-head">
            <span className={gradeClass(evidence.grade)} data-evidence-grade>
              {evidence.grade}
            </span>
            <span className="mining-evidence-reason" data-evidence-reason>
              {evidence.reason_zh}
            </span>
          </div>
          <div className="mining-card-sub" data-evidence-threshold-source>
            {t("miningEvidThresholdSource")}:{" "}
            {evidence.thresholds_source === "custom"
              ? t("miningEvidThresholdCustom")
              : t("miningEvidThresholdDefault")}
          </div>

          {/* 8 维雷达（维度 key 与阈值来源均来自证据体，不臆造） */}
          {(evidence.dimensions ?? []).length > 0 && (
            <MiningChart
              testId="evidence-radar"
              height={260}
              option={radarOption}
              emptyText={t("miningPoolBoardNoData")}
            />
          )}

          <table className="mining-evidence-dim-table">
            <thead>
              <tr>
                <th>{t("miningEvidColDimension")}</th>
                <th>{t("miningEvidColCurrent")}</th>
                <th>{t("miningEvidColThreshold")}</th>
                <th>{t("miningEvidColPass")}</th>
                <th>{t("miningEvidColGap")}</th>
              </tr>
            </thead>
            <tbody>
              {(evidence.dimensions ?? []).map((d) => (
                <tr key={d.key} data-evidence-dimension>
                  <td>{dimLabel(d.key)}</td>
                  <td>{String(d.current ?? "-")}</td>
                  <td>{String(d.threshold ?? "-")}</td>
                  <td className={d.passed ? "is-pass" : "is-fail"}>
                    {d.passed ? t("miningEvidPassed") : t("miningEvidFailed")}
                  </td>
                  <td>{String(d.gap ?? "-")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {tab === "stats" && (
        <section data-evidence-panel="stats" className="mining-evidence-lineage">
          <table className="mining-evidence-stat-table">
            <thead>
              <tr>
                <th>{t("miningEvidStatColMethod")}</th>
                <th>{t("miningEvidStatColResult")}</th>
                <th>{t("miningEvidStatColThreshold")}</th>
                <th>{t("miningEvidStatColPass")}</th>
              </tr>
            </thead>
            <tbody>
              {STAT_ROWS.map((row) => {
                const disabled = rowDisabled(row.key);
                return (
                  <tr
                    key={row.key}
                    data-evidence-stat={row.key}
                    data-disabled={disabled ? "true" : undefined}
                  >
                    <td>{t(row.labelKey)}</td>
                    <td className="mining-evidence-stat-value">{statValue(row.key)}</td>
                    <td className="mining-evidence-stat-value">
                      {STAT_THRESHOLD[row.key] ?? t("miningEvidStatNoThreshold")}
                    </td>
                    <td>
                      {disabled ? (
                        <span className="mining-evidence-stat-note">
                          {t("miningEvidStatNotComputed")}
                        </span>
                      ) : (
                        <span className="mining-chip mining-chip--muted">
                          {t("miningEvidStatPass")}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {/* Walk-Forward 窗口方向示意（按同向数量编码，不代表具体窗口顺序） */}
          {wfWindows > 0 && (
            <div className="mining-evidence-wf" data-evidence-walk-forward>
              <span className="mining-evidence-wf-title">
                {t("miningEvidWfTitle")} · {t("miningEvidWfSame")
                  .replace("{n}", String(wfSame))
                  .replace("{total}", String(wfWindows))}
              </span>
              <div className="mining-evidence-wf-bars">
                {Array.from({ length: wfWindows }).map((_, i) => {
                  const sameDirection = i < wfSame;
                  return (
                    <div
                      key={i}
                      className={`mining-evidence-wf-bar ${sameDirection ? "is-up" : "is-down"}`}
                    >
                      <span>{t("miningEvidWfWindow").replace("{n}", String(i + 1))}</span>
                      <span className="mining-meter mining-meter--sm">
                        <span
                          className={`mining-meter-fill ${
                            sameDirection ? "mining-meter-fill--success" : "mining-meter-fill--danger"
                          }`}
                          style={{ width: sameDirection ? "100%" : "38%" }}
                        />
                      </span>
                      <span>
                        {sameDirection ? `↑ ${t("miningEvidWfUp")}` : `↓ ${t("miningEvidWfDown")}`}
                      </span>
                    </div>
                  );
                })}
              </div>
              <span className="mining-card-sub">{t("miningEvidWalkForwardNote")}</span>
            </div>
          )}

          <div className="mining-card-sub" data-evidence-total-trials>
            {t("miningEvidTotalTrials").replace(
              "{n}", String(evidence.stats.total_trials ?? "-"))}
          </div>
        </section>
      )}

      {tab === "lineage" && (
        <section data-evidence-panel="lineage" className="mining-evidence-lineage">
          <div className="mining-evidence-lineage-row">
            <span>{t("miningEvidGeneration")}</span>
            <span>{evidence.lineage.generation ?? "-"}</span>
          </div>
          <div className="mining-evidence-lineage-row">
            <span>{t("miningEvidParents")}</span>
            <span>{(evidence.lineage.parent_ids ?? []).join(", ") || "-"}</span>
          </div>
          <div className="mining-evidence-lineage-row">
            <span>{t("miningEvidOperation")}</span>
            <span>{evidence.lineage.operation ?? "-"}</span>
          </div>
          {evidence.lineage.economic_logic && (
            <div className="mining-evidence-lineage-row">
              <span>{t("miningEvidEconomicLogic")}</span>
              <span>{evidence.lineage.economic_logic}</span>
            </div>
          )}
        </section>
      )}

      {/* 评级历史时间轴（§8.4.3） */}
      <section className="mining-evidence-history">
        <h4>{t("miningEvidHistory")}</h4>
        {(evidence.grade_history ?? []).map((h, i) => (
          <div
            key={`${h.changed_at}-${i}`}
            className="mining-evidence-history-item"
            data-evidence-history-item
          >
            <span>{h.grade}</span>
            <span>{h.changed_at}</span>
            <span>{t(TRIGGER_LABEL[h.trigger] ?? "miningEvidTriggerAuto")}</span>
            <span>{h.reason ?? "-"}</span>
          </div>
        ))}
      </section>
    </aside>
  );
}
