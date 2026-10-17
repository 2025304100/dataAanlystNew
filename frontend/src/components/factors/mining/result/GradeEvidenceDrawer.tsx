import { useMemo, useState } from "react";
import { Button, Input, Select } from "antd";
import { t } from "../../../../i18n";
import MiningChart, {
  MINING_CHART_COLORS,
  miningBaseOption,
} from "../charts/MiningChart";
import { ADJUST_REASON_MIN, STAT_ROWS, isDegraded } from "./gradeEvidenceTypes";
import type { Grade, GradeEvidence } from "./gradeEvidenceTypes";
import type { EChartsOption } from "echarts";
import { formatMiningDateTime, miningSourceLabel } from "../miningDisplay";

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
  actionBusy?: boolean;
  actionError?: string | null;
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
  p_adj: "miningEvidDimAdjustedP",
  p_raw: "miningEvidDimRawP",
  ci_lower: "miningEvidDimCiLower",
  perm_p: "miningEvidDimPermutationP",
  correlation: "miningEvidDimCorrelation",
  oos_icir_min: "miningEvidDimOosIcirMin",
  oos_icir_max: "miningEvidDimOosIcirMax",
};

function dimLabel(key: string): string {
  const k = DIM_LABEL[key];
  return k ? t(k) : key;
}

function gradeClass(grade: Grade | null): string {
  return grade ? `mining-evidence-grade-badge mining-grade mining-grade--${grade.toLowerCase()}` : "mining-chip mining-chip--muted";
}

export default function GradeEvidenceDrawer({
  evidence,
  onClose,
  onAdjustGrade,
  onRestoreAuto,
  actionBusy = false,
  actionError = null,
}: GradeEvidenceDrawerProps) {
  const [tab, setTab] = useState<TabKey>("grade");
  const [adjusting, setAdjusting] = useState(false);
  const [targetGrade, setTargetGrade] = useState<Grade>(evidence.grade ?? "C");
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

  const statOutcome = (key: string): "pass" | "fail" | "missing" | "reference" => {
    const s = evidence.stats;
    const values: Record<string, number | null | undefined> = {
      t_test: s.t_test_p, bonferroni: s.bonferroni_p, fdr: s.fdr_q,
      bootstrap: s.bootstrap_ci?.[0], permutation: s.permutation_p,
      dsr: s.dsr_icir, decay: s.decay_ratio,
      walk_forward: s.walk_forward?.windows && s.walk_forward?.same_direction != null
        ? s.walk_forward.same_direction / s.walk_forward.windows : null,
    };
    const value = values[key];
    if (value == null || !Number.isFinite(value)) return "missing";
    const threshold = evidence.stat_thresholds?.[key];
    if (!threshold || !Number.isFinite(threshold.value)) return "reference";
    const passed = threshold.operator === "lt" ? value < threshold.value
      : threshold.operator === "gt" ? value > threshold.value
        : threshold.operator === "gte" ? value >= threshold.value : null;
    return passed == null ? "reference" : passed ? "pass" : "fail";
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
      {actionError && <p data-evidence-action-error role="alert" className="mining-run-op-error">{actionError}</p>}

      {/* 月频提示（pitfalls 第二条的另一半） */}
      {evidence.frequency === "monthly" && (
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
          <button type="button" data-evidence-restore-auto disabled={actionBusy || !onRestoreAuto} onClick={onRestoreAuto}>
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
          <Select
            data-evidence-adjust-grade
            value={targetGrade}
            style={{ width: 110 }}
            onChange={(v) => setTargetGrade(v as Grade)}
            options={GRADES.map((g) => ({ value: g, label: g }))}
          />
          <Input.TextArea
            data-evidence-adjust-reason
            value={reason}
            rows={2}
            placeholder={t("miningEvidAdjustReasonHint")}
            onChange={(e) => setReason(e.target.value)}
          />
          <Button
            type="primary"
            size="small"
            data-evidence-adjust-submit
            loading={actionBusy}
            disabled={!canSubmit || !onAdjustGrade}
            onClick={() => onAdjustGrade?.(targetGrade, reason.trim())}
          >
            {t("miningEvidAdjustSubmit")}
          </Button>
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
              {evidence.grade ?? t("miningResultUngraded")}
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
                  <td className={d.passed == null ? undefined : d.passed ? "is-pass" : "is-fail"}>
                    {d.passed == null ? t("miningEvidNotComputed") : d.passed ? t("miningEvidPassed") : t("miningEvidFailed")}
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
                const outcome = statOutcome(row.key);
                return (
                  <tr
                    key={row.key}
                    data-evidence-stat={row.key}
                    data-disabled={disabled ? "true" : undefined}
                  >
                    <td>{t(row.labelKey)}</td>
                    <td className="mining-evidence-stat-value">{statValue(row.key)}</td>
                    <td className="mining-evidence-stat-value">
                      {evidence.stat_thresholds?.[row.key]?.label ?? t("miningEvidStatNoThreshold")}
                    </td>
                    <td>
                      {disabled ? (
                        <span className="mining-evidence-stat-note">
                          {t(evidence.frequency === "monthly" ? "miningEvidStatNotComputed" : "miningEvidNotComputed")}
                        </span>
                      ) : (
                        <span className={`mining-chip ${outcome === "pass" ? "is-pass" : outcome === "fail" ? "is-fail" : "mining-chip--muted"}`}>
                          {t(outcome === "pass" ? "miningEvidStatPass" : outcome === "fail" ? "miningEvidStatFail" : outcome === "missing" ? "miningEvidNotComputed" : "miningEvidStatReference")}
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
            <span>{miningSourceLabel(evidence.lineage.operation)}</span>
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
            <span title={h.changed_at}>{formatMiningDateTime(h.changed_at)}</span>
            <span>{TRIGGER_LABEL[h.trigger] ? t(TRIGGER_LABEL[h.trigger]) : h.trigger || "-"}</span>
            <span>{h.reason ?? "-"}</span>
          </div>
        ))}
      </section>
    </aside>
  );
}
