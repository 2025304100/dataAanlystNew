import { useCallback, useEffect, useState } from "react";
import { t } from "../../../i18n";
import { factorMiningApi } from "../../../api/factorMining";
import { MINING_STEPS } from "../../../types/mining";
import type { MiningRun, MiningStepKey } from "../../../types/mining";
import MiningEvoParamStep from "./wizard/step4/MiningEvoParamStep";
import MiningFieldStep from "./wizard/step3/MiningFieldStep";
import MiningPoolStep from "./wizard/step1/MiningPoolStep";
import MiningTimeTargetStep from "./wizard/step2/MiningTimeTargetStep";
import FactorMiningRunTrack from "./wizard/step5/FactorMiningRunTrack";
import type { RunProgress } from "./wizard/step5/runTypes";

/**
 * 因子挖掘壳（设计 §9.2）：子页签「向导 / 批次列表」。
 *
 * **G5 接线（2026-09-19，经用户授权）**：T27 只交付了壳与步骤条，本处把
 * step1~step5 组件接入向导，使「5 步向导可点通到结果页」可验收：
 *   - 步骤条可点击跳转（`data-mining-step`）；底部「上一步/下一步」逐步推进；
 *   - step5 需要运行数据：无 `runProgress` 时显示空态占位（不臆造进度）。
 *
 * 批次列表：按契约调 `GET /factor-mining/runs`，**后端当前未实现（M5）**，
 * 因此必须三态容错（加载中 / 空 / 错误），不可因接口 501 让整页崩溃。
 *
 * 文案全部走 t()，禁止硬编码中文（需求 §3.4 / 开发 §6）。
 */

const STEP_LABEL_KEYS: Record<MiningStepKey, string> = {
  pool: "miningStepPool",
  "time-target": "miningStepTimeTarget",
  field: "miningStepField",
  evolution: "miningStepEvolution",
  run: "miningStepRun",
};

type ShellTab = "wizard" | "runs";

export interface MiningShellProps {
  /** 运行数据（step5 用；无则不臆造，显示空态） */
  runProgress?: RunProgress | null;
  /** step4 提交后的回调（由外层接后端创建批次） */
  onSubmitted?: () => void;
}

export default function MiningShell({
  runProgress = null,
  onSubmitted,
}: MiningShellProps) {
  const [tab, setTab] = useState<ShellTab>("wizard");
  const [currentStep, setCurrentStep] = useState(0);
  const [runs, setRuns] = useState<MiningRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  const loadRuns = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const page = await factorMiningApi.listRuns({ page: 1, page_size: 20 });
      setRuns(Array.isArray(page?.items) ? page.items : []);
    } catch {
      // 后端未实现（M5）或网络异常：不崩，落错误态
      setRuns([]);
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (tab === "runs") void loadRuns();
  }, [tab, loadRuns]);

  return (
    <div data-mining-shell className="settings-indicator-stack">
      <div className="sub-tabs" aria-label={t("factorMiningTabTitle")}>
        <button
          type="button"
          className={`sub-tab ${tab === "wizard" ? "active" : ""}`}
          aria-current={tab === "wizard" ? "page" : undefined}
          onClick={() => setTab("wizard")}
        >
          {t("miningTabWizard")}
        </button>
        <button
          type="button"
          className={`sub-tab ${tab === "runs" ? "active" : ""}`}
          aria-current={tab === "runs" ? "page" : undefined}
          onClick={() => setTab("runs")}
        >
          {t("miningTabRuns")}
        </button>
      </div>

      <div className="sub-tab-container" hidden={tab !== "wizard"}>
        <ol className="mining-step-bar" data-mining-step-bar>
          {MINING_STEPS.map((key, idx) => (
            <li
              key={key}
              className={`mining-step ${idx === currentStep ? "current" : "todo"}`}
              data-mining-step={key}
              aria-current={idx === currentStep ? "step" : undefined}
            >
              <button
                type="button"
                className="mining-step-btn"
                data-mining-step-jump={key}
                onClick={() => setCurrentStep(idx)}
              >
                <span className="mining-step-index">{idx + 1}</span>
                <span className="mining-step-label">{t(STEP_LABEL_KEYS[key])}</span>
              </button>
            </li>
          ))}
        </ol>

        {/* G5：5 步内容接线（step1~step5） */}
        <div className="mining-wizard-body" data-mining-wizard-body>
          {currentStep === 0 && (
            <div data-mining-step-panel="pool">
              <MiningPoolStep onNext={() => setCurrentStep(1)} />
            </div>
          )}
          {currentStep === 1 && (
            <div data-mining-step-panel="time-target">
              <MiningTimeTargetStep />
            </div>
          )}
          {currentStep === 2 && (
            <div data-mining-step-panel="field">
              <MiningFieldStep />
            </div>
          )}
          {currentStep === 3 && (
            <div data-mining-step-panel="evolution">
              <MiningEvoParamStep onSubmit={onSubmitted} />
            </div>
          )}
          {currentStep === 4 && (
            <div data-mining-step-panel="run">
              {runProgress ? (
                <FactorMiningRunTrack progress={runProgress} />
              ) : (
                <p className="mining-placeholder" data-mining-run-empty>
                  {t("miningWizardNoRun")}
                </p>
              )}
            </div>
          )}
        </div>

        <div className="mining-wizard-nav">
          <button
            type="button"
            data-mining-prev
            disabled={currentStep === 0}
            onClick={() => setCurrentStep((s) => Math.max(0, s - 1))}
          >
            {t("miningWizardPrev")}
          </button>
          <button
            type="button"
            data-mining-next
            disabled={currentStep >= MINING_STEPS.length - 1}
            onClick={() => setCurrentStep((s) => Math.min(MINING_STEPS.length - 1, s + 1))}
          >
            {t("miningWizardNext")}
          </button>
        </div>
      </div>

      <div className="sub-tab-container" hidden={tab !== "runs"}>
        {loading && <p data-mining-runs-loading>{t("miningRunsLoading")}</p>}
        {!loading && failed && <p data-mining-runs-error>{t("miningRunsError")}</p>}
        {!loading && !failed && runs.length === 0 && (
          <p data-mining-runs-empty>{t("miningRunsEmpty")}</p>
        )}
        {!loading && !failed && runs.length > 0 && (
          <table className="mining-runs-table" data-mining-runs-table>
            <thead>
              <tr>
                <th>{t("miningRunsColRun")}</th>
                <th>{t("miningRunsColStatus")}</th>
                <th>{t("miningRunsColProgress")}</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id}>
                  <td>{run.id}</td>
                  <td>{run.status ?? "-"}</td>
                  <td>
                    {run.current_generation ?? 0}
                    {run.total_generations ? ` / ${run.total_generations}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
