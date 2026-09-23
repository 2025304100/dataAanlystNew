import { t } from "../../../../../i18n";
import { InputNumber, Radio, Switch } from "antd";

/**
 * 探索配置（§6.3 初始种群·第二层，§6.5.5）：AI 与随机来源比例 + 预设。
 *
 * - AI 生成开关（Switch）；
 * - **探索预设 Radio**（§6.3.2）：保守 40:60 / 平衡 57:43（默认）/ 激进 70:30，
 *   选中即把 AI/随机比例写入（手动改比例后自动落到「自定义」未选中态）；
 * - AI/随机比例（InputNumber，0~1）；两者之和可不为 1（剩余由对方补位，后端口径）。
 */
export interface ExplorationConfigProps {
  aiRatio?: number;
  randomRatio?: number;
  aiEnabled?: boolean;
  onAiEnabledChange?: (next: boolean) => void;
  onChange?: (patch: Record<string, unknown>) => void;
}

/** §6.3.2 探索预设：预设 → (ai, random) */
export const EXPLORE_PRESETS: Array<{ key: string; labelKey: string; ai: number; random: number }> = [
  { key: "conservative", labelKey: "miningEvoExploreConservative", ai: 0.4, random: 0.6 },
  { key: "balanced", labelKey: "miningEvoExploreBalanced", ai: 0.57, random: 0.43 },
  { key: "aggressive", labelKey: "miningEvoExploreAggressive", ai: 0.7, random: 0.3 },
];

export default function ExplorationConfig({
  aiRatio = 0.5,
  randomRatio = 0.5,
  aiEnabled = true,
  onAiEnabledChange,
  onChange,
}: ExplorationConfigProps) {
  const presetKey = EXPLORE_PRESETS.find(
    (p) => Math.abs(p.ai - aiRatio) < 0.01 && Math.abs(p.random - randomRatio) < 0.01,
  )?.key;

  return (
    <details className="mining-evo-section" data-evo-section="exploration" open>
      <summary>{t("miningEvoAdvancedExploration")}</summary>

      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoAiEnabled")}</span>
        <div data-evo-ai-enabled-advanced>
          <Switch
            checked={aiEnabled}
            onChange={(v) => onAiEnabledChange?.(v)}
          />
        </div>
      </div>

      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoExplorePreset")}</span>
        <Radio.Group
          data-evo-explore-preset
          value={presetKey ?? "custom"}
          onChange={(e) => {
            const p = EXPLORE_PRESETS.find((x) => x.key === e.target.value);
            if (p) onChange?.({ ai_ratio: p.ai, random_ratio: p.random });
          }}
          options={EXPLORE_PRESETS.map((p) => ({ value: p.key, label: t(p.labelKey) }))}
        />
        {!presetKey && (
          <span className="mining-evo-muted" data-evo-explore-custom>
            {t("miningEvoExploreCustom")}
          </span>
        )}
      </div>

      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoAiRatio")}</span>
        <InputNumber
          data-evo-ai-ratio
          min={0}
          max={1}
          step={0.05}
          value={aiRatio}
          onChange={(v) => onChange?.({ ai_ratio: typeof v === "number" ? v : aiRatio })}
        />
      </div>
      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoRandomRatio")}</span>
        <InputNumber
          data-evo-random-ratio
          min={0}
          max={1}
          step={0.05}
          value={randomRatio}
          onChange={(v) => onChange?.({ random_ratio: typeof v === "number" ? v : randomRatio })}
        />
      </div>
    </details>
  );
}