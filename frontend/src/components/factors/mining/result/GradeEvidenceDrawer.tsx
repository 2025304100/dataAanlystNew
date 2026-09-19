import { useState } from "react";
import { t } from "../../../../i18n";
import { ADJUST_REASON_MIN, STAT_ROWS, isDegraded } from "./gradeEvidenceTypes";
import type { Grade, GradeEvidence } from "./gradeEvidenceTypes";

/**
 * 因子详情证据抽屉（向导 §8.4.1 / §8.4.2 / §8.4.3）—— 回答「为什么是这个等级」。
 *
 * - **恰好 3 个 Tab**（not_do：不做 4 Tab）：定级证据 / 统计检验证据 8 项 / 血缘与来源；
 * - Tab1：等级大标签 + 一句话理由 + 8 维度明细（当前值/阈值/达标/差距）+ 阈值来源；
 * - Tab2：8 项统计检验；**月频时 Bootstrap 与置换两行灰掉并标注样本不足**
 *   （pitfalls 第二条），顶部提示「月频最高 B 级」；含 `total_trials` 说明；
 * - Tab3：进化路径（代数/父代/操作）+ 经济逻辑（AI 生成必显示）；
 * - 人工调整等级（原因 ≥10 字）→ 提示「季度重评不会自动覆盖」+「恢复自动」；
 * - 季度重评横幅（升级绿 / 降级红 / 移出 FactorSet 追加）+ 评级历史时间轴。
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

  const rowDisabled = (key: string) =>
    degraded && (key === "bootstrap" || key === "permutation");

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
        <section data-evidence-panel="grade">
          <div data-evidence-grade>{evidence.grade}</div>
          <div data-evidence-reason>{evidence.reason_zh}</div>
          <div data-evidence-threshold-source>
            {t("miningEvidThresholdSource")}:{" "}
            {evidence.thresholds_source === "custom"
              ? t("miningEvidThresholdCustom")
              : t("miningEvidThresholdDefault")}
          </div>
          {evidence.dimensions.map((d) => (
            <div key={d.key} data-evidence-dimension>
              <span>{d.key}</span>
              <span>{String(d.current ?? "-")}</span>
              <span>{String(d.threshold ?? "-")}</span>
              <span>{d.passed ? "✅" : "❌"}</span>
              <span>{String(d.gap ?? "-")}</span>
            </div>
          ))}
        </section>
      )}

      {tab === "stats" && (
        <section data-evidence-panel="stats">
          {STAT_ROWS.map((row) => (
            <div
              key={row.key}
              data-evidence-stat={row.key}
              data-disabled={rowDisabled(row.key) ? "true" : undefined}
            >
              <span>{t(row.labelKey)}</span>
              <span>{statValue(row.key)}</span>
              {rowDisabled(row.key) && (
                <span>{t("miningEvidStatNotComputed")}</span>
              )}
            </div>
          ))}
          <div data-evidence-total-trials>
            {t("miningEvidTotalTrials").replace(
              "{n}", String(evidence.stats.total_trials ?? "-"))}
          </div>
          <div>{t("miningEvidWalkForwardNote")}</div>
        </section>
      )}

      {tab === "lineage" && (
        <section data-evidence-panel="lineage">
          <div>{t("miningEvidGeneration")}: {evidence.lineage.generation ?? "-"}</div>
          <div>{t("miningEvidParents")}: {(evidence.lineage.parent_ids ?? []).join(", ") || "-"}</div>
          <div>{t("miningEvidOperation")}: {evidence.lineage.operation ?? "-"}</div>
          {evidence.lineage.economic_logic && (
            <div>{t("miningEvidEconomicLogic")}: {evidence.lineage.economic_logic}</div>
          )}
        </section>
      )}

      {/* 评级历史时间轴（§8.4.3） */}
      <section className="mining-evidence-history">
        <h4>{t("miningEvidHistory")}</h4>
        {(evidence.grade_history ?? []).map((h, i) => (
          <div key={`${h.changed_at}-${i}`} data-evidence-history-item>
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
