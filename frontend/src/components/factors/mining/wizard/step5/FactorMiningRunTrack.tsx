import { useMemo, useRef, useState } from "react";
import { t } from "../../../../../i18n";
import { Progress } from "antd";
import MiningChart, {
  MINING_CHART_COLORS,
  miningBaseOption,
} from "../../charts/MiningChart";
import { PREMATURE_DIVERSITY, STALL_SUGGEST_STOP } from "./runTypes";
import type { RunLogEntry, RunProgress, TopFactor } from "./runTypes";
import type { EChartsOption } from "echarts";

/**
 * Step5 进化跟踪（向导 §8.3.1~§8.3.7）。
 *
 * - 进度总览：代数/最大代数、收敛状态、种群多样性（**<30% 提示早熟并建议提高注入率**）；
 * - 进化曲线：最优/平均 ICIR + **收敛参考线来自配置阈值**（§8.3.2 明令不得写死门禁）；
 * - 收敛且连续 3 代无显著进步 → **提示「建议停止」，不自动停**（是否停止由用户确认）；
 * - 三操作（§8.3.5）：中断（→暂停，按钮变「继续」）/ 提前停止（确认框说明保留前 X 代
 *   并对 Top N 做最终验证）/ 完全放弃（二次确认、提示不可恢复）；
 * - 最终验证进度**单独展示**（§8.3.7）；
 * - §8.3.6 资源占用与按代日志：**后端未上报时显示等待态**，不由前端估算。
 *
 * ⚠️ `data-run-*` / `data-diversity-*` 是测试钩子（含逐代节点数量断言），不得改动。
 */
export interface FactorMiningRunTrackProps {
  progress: RunProgress;
  finalValidationTop?: number;
  onPause?: () => void;
  onResume?: () => void;
  onStop?: () => void;
  onDiscard?: () => void;
  /** §8.2：排队态「取消排队」 */
  onCancelQueue?: () => void;
  /** F2 §8.3.3：当前种群 Top 因子列表（新进 Top10 高亮闪烁） */
  topCandidates?: TopFactor[];
}

/** §8.3.2 最优/平均 ICIR 进化曲线（收敛参考线来自配置阈值，不写死门禁） */
function icirCurveOption(
  curve: RunProgress["curve"],
  threshold: number | undefined,
): EChartsOption {
  const points = curve ?? [];
  return {
    ...miningBaseOption(),
    legend: {
      show: true,
      right: 0,
      top: 0,
      itemWidth: 18,
      itemHeight: 2,
      textStyle: { color: MINING_CHART_COLORS.axisLabel, fontSize: 12 },
      data: [t("miningRunBestIc"), t("miningRunAvgIc")],
    },
    grid: { left: 8, right: 12, top: 34, bottom: 4, containLabel: true },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: points.map((p) => String(p.generation)),
      axisLine: { lineStyle: { color: MINING_CHART_COLORS.axis } },
      axisTick: { show: false },
      axisLabel: { color: MINING_CHART_COLORS.axisLabel, fontSize: 11 },
    },
    yAxis: {
      type: "value",
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: MINING_CHART_COLORS.axisLabel, fontSize: 11 },
      splitLine: { lineStyle: { color: MINING_CHART_COLORS.grid } },
    },
    series: [
      {
        name: t("miningRunBestIc"),
        type: "line",
        smooth: true,
        symbol: "circle",
        symbolSize: 6,
        data: points.map((p) => p.best_icir),
        lineStyle: { width: 2.5, color: MINING_CHART_COLORS.brand },
        itemStyle: { color: MINING_CHART_COLORS.brand },
        areaStyle: { color: MINING_CHART_COLORS.brandSoft },
        markLine:
          threshold != null
            ? {
                silent: true,
                symbol: "none",
                label: {
                  formatter: String(threshold),
                  color: MINING_CHART_COLORS.warn,
                  fontSize: 11,
                  position: "insideEndTop",
                },
                lineStyle: { type: "dashed", color: MINING_CHART_COLORS.warn },
                data: [{ yAxis: threshold }],
              }
            : undefined,
      },
      {
        name: t("miningRunAvgIc"),
        type: "line",
        smooth: true,
        symbol: "emptyCircle",
        symbolSize: 5,
        data: points.map((p) => p.avg_icir),
        lineStyle: { width: 2, type: "dashed", color: MINING_CHART_COLORS.accent2 },
        itemStyle: { color: MINING_CHART_COLORS.accent2 },
      },
    ],
  };
}

/** §8.3.6 资源占用一条（无数据不画条） */
function ResItem({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | null | undefined;
  tone?: "info" | "warn" | "danger";
}) {
  const pct = value == null ? null : Math.max(0, Math.min(100, value));
  const fillClass = `mining-meter-fill${tone ? ` mining-meter-fill--${tone}` : ""}`;
  return (
    <div className="mining-run-res-item">
      <span>
        {label}
        <b>{pct == null ? "-" : `${Math.round(pct)}%`}</b>
      </span>
      <span className="mining-meter mining-meter--sm">
        <span className={fillClass} style={{ width: `${pct ?? 0}%` }} />
      </span>
    </div>
  );
}

export default function FactorMiningRunTrack({
  progress,
  finalValidationTop = 50,
  onPause,
  onResume,
  onStop,
  onDiscard,
  onCancelQueue,
  topCandidates = [],
}: FactorMiningRunTrackProps) {
  const [confirmStop, setConfirmStop] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  // F2：记录上一轮 Top 序列（按公式），**新进 Top10** 的条目高亮闪烁；
  // 跨代时自然生效（上一代已在 Top10 的公式再出现不算新进）。
  const prevTopRef = useRef<Set<string> | null>(null);

  const topNew = useMemo(() => {
    if (!Array.isArray(topCandidates) || topCandidates.length === 0) {
      prevTopRef.current = null;
      return new Set<string>();
    }
    const currentFormulas = new Set(topCandidates.map((c) => c.formula));
    // 首次展示：仅建立基线，不闪烁（无「上一轮」可对比）
    if (prevTopRef.current == null) {
      prevTopRef.current = currentFormulas;
      return new Set<string>();
    }
    const prevFormulas = prevTopRef.current;
    const fresh = new Set(
      topCandidates
        .filter((c) => !prevFormulas.has(c.formula))
        .map((c) => c.formula),
    );
    prevTopRef.current = currentFormulas;
    return fresh;
  }, [topCandidates]);

  const diversity = progress.diversity ?? null;
  const premature = diversity != null && diversity < PREMATURE_DIVERSITY;
  const suggestStop =
    Boolean(progress.converged) &&
    (progress.stall_generations ?? 0) >= STALL_SUGGEST_STOP;
  const paused = progress.status === "paused";
  const fv = progress.final_validation ?? null;
  const genPercent = Math.round(
    (progress.generation / Math.max(1, progress.max_generations)) * 100,
  );
  const diversityDots = Math.min(10, Math.round((diversity ?? 0) * 10));

  // 多样性双轴曲线（SVG）：左轴最优 ICIR、右轴 health，均归一化到 0~1
  const dv = progress.diversity_curve ?? [];
  const curvePts = progress.curve ?? [];
  const dvW = 560;
  const dvH = 170;
  const padL = 42;
  const padR = 42;
  const padT = 14;
  const padB = 24;
  const innerW = dvW - padL - padR;
  const innerH = dvH - padT - padB;
  const xAt = (i: number, n: number) =>
    n > 1 ? padL + (i / (n - 1)) * innerW : padL + innerW / 2;
  const yAt = (v: number) => padT + innerH - Math.max(0, Math.min(1, v)) * innerH;
  const gridValues = [0, 0.25, 0.5, 0.75, 1];

  return (
    <div className="mining-run-track" data-run-track>
      {/* 进度总览 */}
      <div className="mining-run-overview" data-run-overview>
        <span data-run-generation>
          {t("miningRunGeneration")}: {progress.generation} / {progress.max_generations}
          {/* §8.3.1：每代进度条 */}
          <Progress
            data-run-progressbar
            percent={genPercent}
            showInfo={false}
            size="small"
          />
        </span>
        <span data-run-converged>
          {progress.converged ? t("miningRunConverged") : t("miningRunNotConverged")}
        </span>
        {diversity != null && (
          <span data-run-diversity>
            {t("miningRunDiversity")}:{" "}
            {/* §8.3.1：多样性点阵（●●●●●○○○○○ 52%） */}
            <span data-run-diversity-dots className="mining-run-diversity-dots">
              {"●".repeat(diversityDots)}
              {"○".repeat(Math.max(0, 10 - diversityDots))}
            </span>{" "}
            {Math.round(diversity * 100)}%
          </span>
        )}
        {progress.best_icir != null && (
          <span data-run-best>
            {t("miningRunBestIcLabel")}: {Number(progress.best_icir).toFixed(3)}
          </span>
        )}
        {progress.population_size != null && (
          <span>
            {t("miningRunPopulation")}: {progress.population_size}
          </span>
        )}
        {progress.eta_seconds != null && (
          <span>
            {t("miningRunEta")}: {Math.max(1, Math.round(progress.eta_seconds / 60))}
            {" "}{t("miningRunMinutes")}
          </span>
        )}
      </div>

      {premature && (
        <div className="mining-run-premature" data-run-premature role="alert">
          {t("miningRunPremature")}
        </div>
      )}

      {/* §8.2：排队态（duckdb_write 排队时展示位次 + 取消排队） */}
      {progress.status === "queued" && (progress.queue_position ?? 0) > 0 && (
        <div className="mining-run-queued" data-run-queued role="status">
          <span>
            {t("miningRunQueued").replace("{position}", String(progress.queue_position))}
          </span>
          {onCancelQueue && (
            <button
              type="button"
              className="mining-pool-btn"
              data-run-cancel-queue
              onClick={onCancelQueue}
            >
              {t("miningRunCancelQueue")}
            </button>
          )}
        </div>
      )}

      {/* 进化曲线（收敛参考线来自配置阈值，不写死） */}
      <div className="mining-run-curve" data-run-curve>
        <div className="mining-run-curve-title">
          <span>{t("miningRunCurve")}</span>
          <span className="mining-card-sub">{t("miningRunCurveHint")}</span>
        </div>
        <div>
          {t("miningRunThreshold")}: {progress.convergence_threshold}
        </div>
        <MiningChart
          testId="icir"
          height={240}
          emptyText={t("miningRunResNoData")}
          option={icirCurveOption(progress.curve, progress.convergence_threshold)}
        />
        <div className="mining-run-curve-legend">
          <span>{t("miningRunBestIc")}</span>
          <span>{t("miningRunAvgIc")}</span>
        </div>
      </div>

      {/* 多样性 health 曲线（双轴：左 ICIR / 右 diversity health） */}
      {dv.length > 0 && (
        <div className="mining-run-diversity-curve" data-run-diversity-curve>
          <div className="mining-run-curve-title">
            <span>{t("miningRunDiversityCurve")}</span>
          </div>
          <svg
            viewBox={`0 0 ${dvW} ${dvH}`}
            width={dvW}
            height={dvH}
            role="img"
            aria-label={t("miningRunDiversityCurve")}
          >
            {/* 水平网格 + 左轴刻度 */}
            {gridValues.map((v) => (
              <g key={`grid-${v}`}>
                <line
                  x1={padL}
                  y1={yAt(v)}
                  x2={dvW - padR}
                  y2={yAt(v)}
                  stroke={MINING_CHART_COLORS.grid}
                  strokeWidth={1}
                />
                <text
                  x={padL - 6}
                  y={yAt(v) + 3.5}
                  textAnchor="end"
                  fontSize={10}
                  fill={MINING_CHART_COLORS.axisLabel}
                >
                  {v.toFixed(2)}
                </text>
              </g>
            ))}
            {/* 右轴 0.3 早熟参考线 */}
            <line
              x1={padL}
              y1={yAt(PREMATURE_DIVERSITY)}
              x2={dvW - padR}
              y2={yAt(PREMATURE_DIVERSITY)}
              stroke={MINING_CHART_COLORS.warn}
              strokeDasharray="4 3"
              strokeWidth={1}
              data-diversity-premature-line
            />
            <text
              x={dvW - padR}
              y={yAt(PREMATURE_DIVERSITY) - 4}
              textAnchor="end"
              fontSize={10}
              fill={MINING_CHART_COLORS.warn}
            >
              {PREMATURE_DIVERSITY}
            </text>
            {/* 左轴：最优 ICIR */}
            {curvePts.map((pt, i) => (
              <circle
                key={`ic-${pt.generation}`}
                cx={xAt(i, curvePts.length)}
                cy={yAt(pt.best_icir)}
                r={3.5}
                fill={MINING_CHART_COLORS.brand}
                data-diversity-icir={pt.generation}
              />
            ))}
            {/* 右轴：diversity health */}
            {dv.map((pt, i) => (
              <circle
                key={`dv-${pt.generation}`}
                cx={xAt(i, dv.length)}
                cy={yAt(pt.diversity_health ?? 0)}
                r={3.5}
                fill="none"
                stroke={MINING_CHART_COLORS.accent2}
                strokeWidth={2}
                data-diversity-health={pt.generation}
              />
            ))}
            {/* X 轴代数刻度 */}
            {dv.map((pt, i) =>
              i % Math.max(1, Math.ceil(dv.length / 8)) === 0 ? (
                <text
                  key={`x-${pt.generation}`}
                  x={xAt(i, dv.length)}
                  y={dvH - 8}
                  textAnchor="middle"
                  fontSize={10}
                  fill={MINING_CHART_COLORS.axisLabel}
                >
                  {pt.generation}
                </text>
              ) : null,
            )}
          </svg>
          <div className="mining-run-curve-legend">
            <span>{t("miningRunDiversityLeftAxis")}</span>
            <span>{t("miningRunDiversityRightAxis")}</span>
          </div>
        </div>
      )}

      {suggestStop && (
        <div className="mining-run-converge-tip" data-run-converge-tip role="status">
          {t("miningRunSuggestStop")}
        </div>
      )}

      {/* §8.3.3 当前种群 Top 因子（F2：新进 Top10 高亮闪烁） */}
      {!Array.isArray(topCandidates) || topCandidates.length === 0 ? null : (
        <div className="mining-run-top" data-run-top-list>
          <div className="mining-run-top-title" data-run-top-title>
            {t("miningRunTopTitle")
              .replace("{generation}", String(progress.generation))
              .replace("{size}", String(topCandidates.length))}
            {topNew.size > 0 && (
              <span className="mining-run-top-new-hint" data-run-top-new-hint>
                {t("miningRunTopNewHint").replace("{count}", String(topNew.size))}
              </span>
            )}
            <span className="mining-card-sub">{t("miningRunTopHint")}</span>
          </div>
          <table className="mining-run-top-table">
            <thead>
              <tr>
                <th>{t("miningRunTopColRank")}</th>
                <th>{t("miningRunTopColFormula")}</th>
                <th>{t("miningRunTopColIc")}</th>
                <th>{t("miningRunTopColCoverage")}</th>
                <th>{t("miningRunTopColSource")}</th>
              </tr>
            </thead>
            <tbody>
              {topCandidates.map((c) => (
                <tr
                  key={c.rank}
                  className={topNew.has(c.formula) ? "mining-run-top-new" : undefined}
                  data-run-top-row={c.rank}
                  data-run-top-islot={topNew.has(c.formula) ? "new" : "stable"}
                >
                  <td>{c.rank}.</td>
                  <td className="mining-run-top-formula">{c.formula}</td>
                  <td>{c.icir != null ? c.icir.toFixed(2) : "-"}</td>
                  <td>{c.coverage != null ? `${(c.coverage * 100).toFixed(0)}%` : "-"}</td>
                  <td>{c.source ?? "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* 最终验证（单独展示） */}
      {fv && (
        <div className="mining-run-final" data-run-final-validation>
          <div className="mining-run-final-title">
            <span>{t("miningRunFinalValidation")}</span>
            <span className="mining-run-final-status" data-run-final-status>
              {fv.done >= fv.total
                ? t("miningRunFinalDone")
                : t("miningRunFinalRunning")}
            </span>
          </div>
          <Progress
            data-run-final-progress
            percent={Math.min(100, Math.round((fv.done / Math.max(1, fv.total)) * 100))}
            showInfo={false}
            size="small"
          />
          <div>
            <span data-run-final-done>{fv.done}</span> /{" "}
            <span data-run-final-total>{fv.total}</span>
            {" "}{t("miningRunFinalUnits")}
          </div>
          {fv.done >= fv.total && (
            <div className="mining-run-final-overview-hint" data-run-final-overview-hint>
              {t("miningRunFinalToOverview")}
            </div>
          )}
        </div>
      )}

      {/* §8.3.6 资源占用 + 按代日志（后端未上报时显示等待态，不估算） */}
      <div className="mining-run-res" data-run-resources>
        <div className="mining-run-curve-title">
          <span>{t("miningRunResTitle")}</span>
        </div>
        {progress.resources ? (
          <div className="mining-run-res-grid">
            <ResItem label={t("miningRunResCpu")} value={progress.resources.cpu_percent} />
            <ResItem
              label={t("miningRunResMem")}
              value={progress.resources.memory_percent}
              tone="warn"
            />
            <ResItem
              label={t("miningRunResDisk")}
              value={progress.resources.disk_percent}
              tone="info"
            />
            <div className="mining-run-res-item">
              <span>
                {t("miningRunSpeed")}
                <b>
                  {progress.resources.generations_per_minute != null
                    ? `${progress.resources.generations_per_minute} ${t("miningRunSpeedUnit")}`
                    : "-"}
                </b>
              </span>
            </div>
          </div>
        ) : (
          <p className="mining-hint">{t("miningRunResNoData")}</p>
        )}
      </div>

      <div className="mining-run-res" data-run-log>
        <div className="mining-run-curve-title">
          <span>{t("miningRunLogTitle")}</span>
        </div>
        {progress.logs && progress.logs.length > 0 ? (
          <div className="mining-run-log" data-run-log-list>
            {Object.entries(
              progress.logs.reduce<Record<string, RunLogEntry[]>>((acc, entry) => {
                const key = String(entry?.generation ?? 0);
                const bucket = acc[key] ?? [];
                bucket.push(entry);
                acc[key] = bucket;
                return acc;
              }, {}),
            ).map(([generation, entries]) => (
              <details className="mining-run-log-group" key={generation} open>
                <summary>
                  {t("miningRunLogGroup").replace("{generation}", generation)}
                </summary>
                {entries.map((entry, i) => (
                  <div
                    key={i}
                    className={`mining-run-log-row ${
                      entry.level === "error" ? "is-error" : ""
                    }`}
                  >
                    {entry.message}
                  </div>
                ))}
              </details>
            ))}
          </div>
        ) : (
          <p className="mining-run-log-empty">{t("miningRunLogEmpty")}</p>
        )}
      </div>

      {/* 三操作（§8.3.5） */}
      <div className="mining-run-actions">
        {paused ? (
          <button type="button" data-run-resume onClick={onResume}>
            {t("miningRunResume")}
          </button>
        ) : (
          <button type="button" data-run-pause onClick={onPause}>
            {t("miningRunPause")}
          </button>
        )}
        <button type="button" data-run-stop onClick={() => setConfirmStop(true)}>
          {t("miningRunStopEarly")}
        </button>
        <button type="button" data-run-discard onClick={() => setConfirmDiscard(true)}>
          {t("miningRunDiscard")}
        </button>
      </div>

      {confirmStop && (
        <div className="mining-run-confirm" role="dialog">
          <div data-run-stop-confirm>
            {t("miningRunStopConfirm")
              .replace("{generation}", String(progress.generation))
              .replace("{top}", String(finalValidationTop))}
            <button type="button" data-run-stop-cancel onClick={() => setConfirmStop(false)}>
              {t("miningRunCancel")}
            </button>
            <button
              type="button"
              data-run-stop-ok
              onClick={() => {
                setConfirmStop(false);
                onStop?.();
              }}
            >
              {t("miningRunConfirm")}
            </button>
          </div>
        </div>
      )}

      {confirmDiscard && (
        <div className="mining-run-confirm" role="dialog">
          <div data-run-discard-confirm>
            {t("miningRunDiscardConfirm")}
            <button type="button" data-run-discard-cancel
              onClick={() => setConfirmDiscard(false)}>
              {t("miningRunCancel")}
            </button>
            <button
              type="button"
              data-run-discard-ok
              onClick={() => {
                setConfirmDiscard(false);
                onDiscard?.();
              }}
            >
              {t("miningRunConfirm")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
