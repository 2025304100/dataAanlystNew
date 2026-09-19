import { useState } from "react";
import { t } from "../../../../../i18n";
import { PREMATURE_DIVERSITY, STALL_SUGGEST_STOP } from "./runTypes";
import type { RunProgress } from "./runTypes";

/**
 * Step5 进化跟踪（向导 §8.3.1~§8.3.7）。
 *
 * - 进度总览：代数/最大代数、收敛状态、种群多样性（**<30% 提示早熟并建议提高注入率**）；
 * - 进化曲线：最优/平均 ICIR + **收敛参考线来自配置阈值**（§8.3.2 明令不得写死门禁）；
 * - 收敛且连续 3 代无显著进步 → **提示「建议停止」，不自动停**（是否停止由用户确认）；
 * - 三操作（§8.3.5）：中断（→暂停，按钮变「继续」）/ 提前停止（确认框说明保留前 X 代
 *   并对 Top N 做最终验证）/ 完全放弃（二次确认、提示不可恢复）；
 * - 最终验证进度**单独展示**（§8.3.7）。
 */
export interface FactorMiningRunTrackProps {
  progress: RunProgress;
  finalValidationTop?: number;
  onPause?: () => void;
  onResume?: () => void;
  onStop?: () => void;
  onDiscard?: () => void;
}

export default function FactorMiningRunTrack({
  progress,
  finalValidationTop = 50,
  onPause,
  onResume,
  onStop,
  onDiscard,
}: FactorMiningRunTrackProps) {
  const [confirmStop, setConfirmStop] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  const diversity = progress.diversity ?? null;
  const premature = diversity != null && diversity < PREMATURE_DIVERSITY;
  const suggestStop =
    Boolean(progress.converged) &&
    (progress.stall_generations ?? 0) >= STALL_SUGGEST_STOP;
  const paused = progress.status === "paused";
  const fv = progress.final_validation ?? null;

  return (
    <div className="mining-run-track" data-run-track>
      {/* 进度总览 */}
      <div className="mining-run-overview" data-run-overview>
        <span>
          {t("miningRunGeneration")}: {progress.generation} / {progress.max_generations}
        </span>
        <span data-run-converged>
          {progress.converged ? t("miningRunConverged") : t("miningRunNotConverged")}
        </span>
        {diversity != null && (
          <span data-run-diversity>
            {t("miningRunDiversity")}: {Math.round(diversity * 100)}%
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

      {/* 进化曲线（收敛参考线来自配置阈值，不写死） */}
      <div className="mining-run-curve" data-run-curve>
        <div className="mining-run-curve-title">{t("miningRunCurve")}</div>
        <div>
          {t("miningRunThreshold")}: {progress.convergence_threshold}
        </div>
        <svg viewBox="0 0 320 80" width="320" height="80" role="img"
          aria-label={t("miningRunCurve")}>
          {(progress.curve ?? []).map((pt, i, arr) => {
            const x = arr.length > 1 ? (i / (arr.length - 1)) * 300 + 10 : 10;
            const y = 70 - Math.min(1, Math.max(0, pt.best_icir)) * 60;
            const yAvg = 70 - Math.min(1, Math.max(0, pt.avg_icir)) * 60;
            return (
              <g key={pt.generation}>
                <circle cx={x} cy={y} r={2.5} fill="currentColor" />
                <circle cx={x} cy={yAvg} r={1.8} fill="none" stroke="currentColor" />
              </g>
            );
          })}
        </svg>
        <div className="mining-run-curve-legend">
          <span>{t("miningRunBestIc")}</span>
          <span>{t("miningRunAvgIc")}</span>
        </div>
      </div>

      {suggestStop && (
        <div className="mining-run-converge-tip" data-run-converge-tip role="status">
          {t("miningRunSuggestStop")}
        </div>
      )}

      {/* 最终验证（单独展示） */}
      {fv && (
        <div className="mining-run-final" data-run-final-validation>
          <div>{t("miningRunFinalValidation")}</div>
          <div>
            <span data-run-final-done>{fv.done}</span> /{" "}
            <span data-run-final-total>{fv.total}</span>
          </div>
        </div>
      )}

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
