import { useState } from "react";
import { t } from "../../../../../i18n";
import ClassicalBaseConfig from "./ClassicalBaseConfig";
import ExplorationConfig from "./ExplorationConfig";
import ReproductionConfig from "./ReproductionConfig";
import ResourceConfirmModal from "./ResourceConfirmModal";
import SelectionConfig from "./SelectionConfig";
import { DEFAULT_EVO_CONFIG, QUICK_TRIAL_PRESET } from "./evoTypes";
import type { EvoPreference, EvoStrength, MiningLockStatus } from "./evoTypes";

/**
 * Step4 进化参数（向导 §6.5.5 / §6.6.8 / §7，设计 §9.2）。
 *
 * **简单模式**（默认）：只出现「进化强度」「选优偏好」「启用 AI 生成」
 *   —— **不得出现任何专业术语**（帕累托/非支配/拥挤度/赛道/锦标赛），
 *   选择与繁殖机制默认开启、默认参数自动生效（有测试断言守黑名单）。
 * **高级模式**：展开经典底座 / 探索 / 选择机制 / 繁殖与变异四块折叠区。
 *
 * 点击「开始挖掘实验」→ `ResourceConfirmModal`（锁三态 + ETA 必标「估算」）。
 * **not_do：不暴露并行度** —— 界面与本组件文案均不得出现并行/进程/worker 字样。
 */
export interface MiningEvoParamStepProps {
  lockStatus?: MiningLockStatus | null;
  etaSeconds?: number | null;
  resourcesOk?: boolean;
  onSubmit?: () => void;
  onSaveDraft?: () => void;
}

export default function MiningEvoParamStep({
  lockStatus = null,
  etaSeconds = null,
  resourcesOk = true,
  onSubmit,
  onSaveDraft,
}: MiningEvoParamStepProps) {
  const [simpleMode, setSimpleMode] = useState(DEFAULT_EVO_CONFIG.simple_mode);
  const [strength, setStrength] = useState<EvoStrength>(DEFAULT_EVO_CONFIG.strength);
  const [preference, setPreference] = useState<EvoPreference>(DEFAULT_EVO_CONFIG.preference);
  const [aiEnabled, setAiEnabled] = useState(DEFAULT_EVO_CONFIG.ai_enabled);
  const [adaptive, setAdaptive] = useState(DEFAULT_EVO_CONFIG.adaptive);
  const [population, setPopulation] = useState(DEFAULT_EVO_CONFIG.population_size);
  const [generations, setGenerations] = useState(DEFAULT_EVO_CONFIG.max_generations);
  const [showConfirm, setShowConfirm] = useState(false);

  const applyQuickTrial = () => {
    setPopulation(QUICK_TRIAL_PRESET.population_size);
    setGenerations(QUICK_TRIAL_PRESET.max_generations);
  };

  return (
    <div className="mining-evo-step" data-evo-step>
      <div className="mining-evo-mode">
        <button
          type="button"
          className={`sub-tab ${simpleMode ? "active" : ""}`}
          data-evo-mode-simple
          onClick={() => setSimpleMode(true)}
        >
          {t("miningEvoModeSimple")}
        </button>
        <button
          type="button"
          className={`sub-tab ${!simpleMode ? "active" : ""}`}
          data-evo-mode-advanced
          onClick={() => setSimpleMode(false)}
        >
          {t("miningEvoModeAdvanced")}
        </button>
      </div>

      {/* 简单模式三件套（无专业术语） */}
      <div className="mining-evo-simple">
        <label>
          <span>{t("miningEvoStrength")}</span>
          <select
            data-evo-strength
            value={strength}
            onChange={(e) => setStrength(e.target.value as EvoStrength)}
          >
            <option value="conservative">{t("miningEvoStrengthConservative")}</option>
            <option value="balanced">{t("miningEvoStrengthBalanced")}</option>
            <option value="aggressive">{t("miningEvoStrengthAggressive")}</option>
          </select>
        </label>
        <label>
          <span>{t("miningEvoPreference")}</span>
          <select
            data-evo-preference
            value={preference}
            onChange={(e) => setPreference(e.target.value as EvoPreference)}
          >
            <option value="balanced">{t("miningEvoPrefBalanced")}</option>
            <option value="stable">{t("miningEvoPrefStable")}</option>
            <option value="aggressive">{t("miningEvoPrefAggressive")}</option>
            <option value="simple">{t("miningEvoPrefSimple")}</option>
          </select>
        </label>
        <label>
          <span>{t("miningEvoAiEnabled")}</span>
          <input
            type="checkbox"
            data-evo-ai-enabled
            checked={aiEnabled}
            onChange={(e) => setAiEnabled(e.target.checked)}
          />
        </label>
      </div>

      {!simpleMode && (
        <div className="mining-evo-advanced" data-evo-advanced-sections>
          <label>
            <span>{t("miningEvoPopulation")}</span>
            <input
              type="number" min="10" max="1000"
              data-evo-population
              value={population}
              onChange={(e) => setPopulation(Number(e.target.value))}
            />
          </label>
          <label>
            <span>{t("miningEvoGenerations")}</span>
            <input
              type="number" min="1" max="200"
              data-evo-generations
              value={generations}
              onChange={(e) => setGenerations(Number(e.target.value))}
            />
          </label>
          <label>
            <span>{t("miningEvoQuickTrial")}</span>
            <input type="checkbox" data-evo-quick-trial onChange={applyQuickTrial} />
          </label>

          <ClassicalBaseConfig />
          <ExplorationConfig aiEnabled={aiEnabled} onAiEnabledChange={setAiEnabled} />
          <SelectionConfig />
          <ReproductionConfig adaptive={adaptive} onAdaptiveChange={setAdaptive} />
        </div>
      )}

      <div className="mining-evo-actions">
        <button type="button" className="mining-pool-btn" data-evo-save-draft
          onClick={onSaveDraft}>
          {t("miningEvoSaveDraft")}
        </button>
        <button
          type="button"
          className="mining-pool-btn primary"
          data-evo-submit
          onClick={() => setShowConfirm(true)}
        >
          {t("miningEvoSubmit")}
        </button>
      </div>

      {showConfirm && (
        <ResourceConfirmModal
          lockStatus={lockStatus}
          etaSeconds={etaSeconds}
          resourcesOk={resourcesOk}
          onClose={() => setShowConfirm(false)}
          onConfirm={onSubmit}
        />
      )}
    </div>
  );
}
