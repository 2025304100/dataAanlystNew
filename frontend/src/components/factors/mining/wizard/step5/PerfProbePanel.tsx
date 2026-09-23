import { t } from "../../../../../i18n";
import type { PerfProbeGeneration } from "./runTypes";

/**
 * 性能探针面板（§8.3.6，调试态）。
 *
 * 入口：本组件仅由父级在 `?debug=perf` 或开发者模式开启时渲染（默认折叠）。
 * 内容：
 *   1. **本代各阶段耗时条形图**：data_load / ast_eval / subexpr_compute /
 *      factor_assemble / metric_calc / db_write（§8.3.6 ①）；
 *   2. **G2 命中率曲线**（随代数，SVG 折线，§8.3.6 ②）；
 *   3. **子表达式总数 / 去重后唯一数**（§8.3.6 ③）；
 *   4. **每代缓存校验状态图标**：`cache_validation_passed` ✅/❌，
 *      ❌ 代次标红并提示「该代缓存校验未通过，已重算」（§8.3.6 ④）。
 *
 * 纯展示组件：数据由父级（MiningShell）轮询 `GET /runs/{id}/generations` 注入。
 */
export interface PerfProbePanelProps {
  generations: PerfProbeGeneration[];
}

const STAGES: Array<{ key: keyof PerfProbeGeneration; labelKey: string }> = [
  { key: "probe_data_load_ms", labelKey: "miningProbeDataLoad" },
  { key: "probe_ast_eval_ms", labelKey: "miningProbeAstEval" },
  { key: "probe_subexpr_compute_ms", labelKey: "miningProbeSubexprCompute" },
  { key: "probe_factor_assemble_ms", labelKey: "miningProbeFactorAssemble" },
  { key: "probe_metric_calc_ms", labelKey: "miningProbeMetricCalc" },
  { key: "probe_db_write_ms", labelKey: "miningProbeDbWrite" },
];

export default function PerfProbePanel({ generations }: PerfProbePanelProps) {
  if (!generations || generations.length === 0) {
    return null;
  }
  const sorted = [...generations].sort((a, b) => a.generation - b.generation);
  const latest = sorted[sorted.length - 1];
  const maxStageMs = Math.max(
    1,
    ...STAGES.map((s) => Number(latest[s.key] ?? 0)),
  );
  const maxHit = Math.max(1, ...sorted.map((g) => Number(g.probe_g2_hit_rate ?? 0)));

  // G2 命中率折线（SVG 极简，320×80 与进化曲线一致）
  const W = 320;
  const H = 80;
  const pad = 4;
  const points = sorted
    .map((g, i) => {
      const x = sorted.length > 1 ? pad + (i / (sorted.length - 1)) * (W - 2 * pad) : W / 2;
      const y = H - pad - (Number(g.probe_g2_hit_rate ?? 0) / maxHit) * (H - 2 * pad);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const lastPt = points.split(" ").pop() ?? "";

  return (
    <details className="mining-perf-panel" data-perf-panel open>
      <summary>{t("miningProbeTitle")}</summary>

      {/* ③ 子表达式总数/唯一数 + 最近一代 G2 */}
      <div className="mining-perf-summary" data-perf-summary>
        <span>
          {t("miningProbeSubexpr")}:{" "}
          <span data-perf-subexpr-total>{latest.probe_subexpr_total ?? 0}</span>
          {" / "}
          <span data-perf-subexpr-unique>{latest.probe_subexpr_unique ?? 0}</span>
          {" "}
          <span className="mining-perf-muted">
            ({t("miningProbeUnique")})
          </span>
        </span>
        <span>
          {t("miningProbeG2Rate")}:{" "}
          <span data-perf-g2>{((latest.probe_g2_hit_rate ?? 0) * 100).toFixed(1)}%</span>
        </span>
      </div>

      {/* ① 最近一代六阶段耗时条形图 */}
      <div className="mining-perf-stages" data-perf-stages>
        {STAGES.map((s) => {
          const ms = Number(latest[s.key] ?? 0);
          const width = Math.round((ms / maxStageMs) * 100);
          return (
            <div key={String(s.key)} className="mining-perf-stage" data-perf-stage={String(s.key)}>
              <span className="mining-perf-stage-label">{t(s.labelKey)}</span>
              <div className="mining-perf-bar-track">
                <div className="mining-perf-bar" style={{ width: `${width}%` }} />
              </div>
              <span className="mining-perf-stage-ms">{ms}ms</span>
            </div>
          );
        })}
      </div>

      {/* ② G2 命中率曲线（随代数） */}
      {generations.length > 1 && (
        <div className="mining-perf-curve" data-perf-curve>
          <div className="mining-run-curve-title">{t("miningProbeG2Curve")}</div>
          <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img"
            aria-label={t("miningProbeG2Curve")}>
            <polyline points={points} fill="none" stroke="#1677ff" strokeWidth="1.5" />
            <circle
              cx={lastPt.split(",")[0]} cy={lastPt.split(",")[1]}
              r="2.5" fill="#1677ff"
            />
          </svg>
        </div>
      )}

      {/* ④ 每代缓存校验状态表 */}
      <table className="mining-perf-table" data-perf-table>
        <thead>
          <tr>
            <th>{t("miningProbeColGen")}</th>
            <th>{t("miningProbeColG2")}</th>
            <th>{t("miningProbeColSubexpr")}</th>
            <th>{t("miningProbeColCache")}</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((g) => {
            const passed = g.cache_validation_passed;
            const ok = passed == null ? null : passed === 1;
            return (
              <tr
                key={g.generation}
                className={ok === false ? "is-cache-fail" : ""}
                data-perf-row={g.generation}
                data-cache-ok={ok == null ? "na" : String(ok)}
              >
                <td>{g.generation}</td>
                <td>{((g.probe_g2_hit_rate ?? 0) * 100).toFixed(1)}%</td>
                <td>
                  {g.probe_subexpr_total ?? 0} / {g.probe_subexpr_unique ?? 0}
                </td>
                <td>
                  {ok == null ? (
                    "-"
                  ) : ok ? (
                    <span className="mining-perf-cache-ok" title={t("miningProbeCachePassed")}>
                      ✅
                    </span>
                  ) : (
                    <span
                      className="mining-perf-cache-fail"
                      title={`${t("miningProbeCacheFailed")} (max_diff=${g.cache_validation_max_diff ?? "-"})`}
                    >
                      ❌
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {sorted.some((g) => g.cache_validation_passed === 0) && (
        <p className="mining-perf-cache-warn" data-perf-cache-warn role="alert">
          {t("miningProbeCacheWarn")}
        </p>
      )}
    </details>
  );
}