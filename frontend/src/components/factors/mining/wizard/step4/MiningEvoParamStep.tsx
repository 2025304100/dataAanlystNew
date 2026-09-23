import { useState } from "react";
import { t } from "../../../../../i18n";
import { InputNumber, Select, Slider, Switch } from "antd";
import ClassicalBaseConfig from "./ClassicalBaseConfig";
import ExplorationConfig from "./ExplorationConfig";
import ReproductionConfig from "./ReproductionConfig";
import ResourceConfirmModal from "./ResourceConfirmModal";
import SelectionConfig from "./SelectionConfig";
import {
  DEFAULT_EVO_CONFIG,
  QUICK_TRIAL_PRESET,
  STRENGTH_ORDER,
  STRENGTH_TO_GENERATIONS,
} from "./evoTypes";
import type { EvoConfig, EvoPreference, EvoStrength, MiningLockStatus } from "./evoTypes";

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
  /** 进化参数变化时上报（A4：MiningShell 组装 evolution_params 用） */
  onConfig?: (config: EvoConfig) => void;
}

export default function MiningEvoParamStep({
  lockStatus = null,
  etaSeconds = null,
  resourcesOk = true,
  onSubmit,
  onSaveDraft,
  onConfig,
}: MiningEvoParamStepProps) {
  const [simpleMode, setSimpleMode] = useState(DEFAULT_EVO_CONFIG.simple_mode);
  const [strength, setStrength] = useState<EvoStrength>(DEFAULT_EVO_CONFIG.strength);
  const [preference, setPreference] = useState<EvoPreference>(DEFAULT_EVO_CONFIG.preference);
  const [aiEnabled, setAiEnabled] = useState(DEFAULT_EVO_CONFIG.ai_enabled);
  const [adaptive, setAdaptive] = useState(DEFAULT_EVO_CONFIG.adaptive);
  const [population, setPopulation] = useState(DEFAULT_EVO_CONFIG.population_size);
  const [generations, setGenerations] = useState(DEFAULT_EVO_CONFIG.max_generations);
  // P1-7：高级折叠区（经典底座/探索/选择/繁殖）的 patch 汇总，随 emitConfig 上报
  const [advanced, setAdvanced] = useState<Record<string, unknown>>({});
  const [showConfirm, setShowConfirm] = useState(false);

  const emitConfig = (patch: Partial<EvoConfig> = {}, advancedPatch?: Record<string, unknown>) => {
    const nextAdvanced = advancedPatch ? { ...advanced, ...advancedPatch } : advanced;
    if (advancedPatch) setAdvanced(nextAdvanced);
    onConfig?.({
      simple_mode: patch.simple_mode ?? simpleMode,
      strength: patch.strength ?? strength,
      preference: patch.preference ?? preference,
      ai_enabled: patch.ai_enabled ?? aiEnabled,
      adaptive: patch.adaptive ?? adaptive,
      population_size: patch.population_size ?? population,
      max_generations: patch.max_generations ?? generations,
      ...nextAdvanced,
    });
  };

  const applyQuickTrial = () => {
    setPopulation(QUICK_TRIAL_PRESET.population_size);
    setGenerations(QUICK_TRIAL_PRESET.max_generations);
    emitConfig({
      population_size: QUICK_TRIAL_PRESET.population_size,
      max_generations: QUICK_TRIAL_PRESET.max_generations,
    });
  };

  return (
    <div className="mining-evo-step" data-evo-step>
      <div className="mining-evo-mode">
        <button
          type="button"
          className={`sub-tab ${simpleMode ? "active" : ""}`}
          data-evo-mode-simple
          onClick={() => { setSimpleMode(true); emitConfig({ simple_mode: true }); }}
        >
          {t("miningEvoModeSimple")}
        </button>
        <button
          type="button"
          className={`sub-tab ${!simpleMode ? "active" : ""}`}
          data-evo-mode-advanced
          onClick={() => { setSimpleMode(false); emitConfig({ simple_mode: false }); }}
        >
          {t("miningEvoModeAdvanced")}
        </button>
      </div>

      {/* 简单模式三件套（无专业术语） */}
      <div className="mining-evo-simple">
        <p className="mining-hint">{t("miningEvoSimpleHint")}</p>
        {/* C1：进化强度用 Slider（弱/中/强 → 最大代数 5/10/20），Tooltip 说明映射 */}
        <div className="mining-evo-row" data-evo-strength>
          <span className="mining-evo-field-label">{t("miningEvoStrength")}</span>
          <Slider
            style={{ width: 260 }}
            min={0}
            max={STRENGTH_ORDER.length - 1}
            step={1}
            value={STRENGTH_ORDER.indexOf(strength)}
            tooltip={{ formatter: () => t("miningEvoStrengthHint") }}
            marks={{
              0: t("miningEvoStrengthConservative"),
              1: t("miningEvoStrengthBalanced"),
              2: t("miningEvoStrengthAggressive"),
            }}
            onChange={(idx) => {
              const v = STRENGTH_ORDER[idx];
              if (!v) return;
              setStrength(v);
              emitConfig({ strength: v, max_generations: STRENGTH_TO_GENERATIONS[v] });
            }}
          />
          <span className="mining-evo-muted">{t("miningEvoStrengthHint")}</span>
        </div>
        <div className="mining-evo-row">
          <span className="mining-evo-field-label">{t("miningEvoPreference")}</span>
          <Select
            data-evo-preference
            value={preference}
            style={{ width: 220 }}
            onChange={(v) => {
              setPreference(v);
              emitConfig({ preference: v });
            }}
            options={[
              { value: "balanced", label: t("miningEvoPrefBalanced") },
              { value: "stable", label: t("miningEvoPrefStable") },
              { value: "aggressive", label: t("miningEvoPrefAggressive") },
              { value: "simple", label: t("miningEvoPrefSimple") },
            ]}
          />
        </div>
        <div className="mining-evo-row">
          <span className="mining-evo-field-label">{t("miningEvoAiEnabled")}</span>
          <div data-evo-ai-enabled>
            <Switch
              checked={aiEnabled}
              onChange={(v) => {
                setAiEnabled(v);
                emitConfig({ ai_enabled: v });
              }}
            />
          </div>
        </div>
      </div>

      {!simpleMode && (
        <div className="mining-evo-advanced" data-evo-advanced-sections>
          <p className="mining-hint">{t("miningEvoAdvancedHint")}</p>
          <div className="mining-evo-section">
            <div className="mining-evo-row">
              <span className="mining-evo-field-label">{t("miningEvoPopulation")}</span>
              <InputNumber
                data-evo-population
                min={50}
                max={300}
                value={population}
                onChange={(v) => {
                  const next = typeof v === "number" ? v : DEFAULT_EVO_CONFIG.population_size;
                  setPopulation(next);
                  emitConfig({ population_size: next });
                }}
              />
            </div>
            <div className="mining-evo-row">
              <span className="mining-evo-field-label">{t("miningEvoGenerations")}</span>
              <InputNumber
                data-evo-generations
                min={5}
                max={50}
                value={generations}
                onChange={(v) => {
                  const next = typeof v === "number" ? v : DEFAULT_EVO_CONFIG.max_generations;
                  setGenerations(next);
                  emitConfig({ max_generations: next });
                }}
              />
            </div>
            <div className="mining-evo-row">
              <span className="mining-evo-field-label">{t("miningEvoQuickTrial")}</span>
              <Switch data-evo-quick-trial onChange={applyQuickTrial} />
            </div>
          </div>

          <ClassicalBaseConfig onChange={(p) => emitConfig({}, p)} />
          <ExplorationConfig
            aiEnabled={aiEnabled}
            onAiEnabledChange={(v) => { setAiEnabled(v); emitConfig({ ai_enabled: v }); }}
            onChange={(p) => emitConfig({}, p)}
          />
          <SelectionConfig onChange={(p) => emitConfig({}, p)} />
          <ReproductionConfig
            adaptive={adaptive}
            onAdaptiveChange={(v) => { setAdaptive(v); emitConfig({ adaptive: v }); }}
            onChange={(p) => emitConfig({}, p)}
          />
        </div>
      )}

      <div className="mining-evo-actions">
        <span className="mining-hint" style={{ marginRight: "auto" }}>
          {t("miningEvoSubmitHint")}
        </span>
        {/* 次要操作弱化：主操作「开始挖掘实验」是这一步的唯一视觉重心 */}
        <button type="button" className="mining-pool-btn ghost" data-evo-save-draft
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
          // P0-3：先关弹窗再提交（提交前置校验失败时错误可见、不再被遮罩卡死）
          onConfirm={() => {
            setShowConfirm(false);
            onSubmit?.();
          }}
        />
      )}
    </div>
  );
}
