import { t } from "../../../../../i18n";

/**
 * 探索配置（高级模式折叠区，向导 §6.5.5 / §6.6.8）：AI 与随机来源比例 + 预设。
 *
 * AI 生成受「10 次调用 × 每次 8~12 骨架」上限与 5 分钟总超时约束，
 * 失败/超限**回退受约束随机**、不阻塞进化（后端口径，本处仅配置）。
 */
export interface ExplorationConfigProps {
  aiRatio?: number;
  randomRatio?: number;
  aiEnabled?: boolean;
  onAiEnabledChange?: (next: boolean) => void;
  onChange?: (patch: Record<string, unknown>) => void;
}

export default function ExplorationConfig({
  aiRatio = 0.5,
  randomRatio = 0.5,
  aiEnabled = true,
  onAiEnabledChange,
  onChange,
}: ExplorationConfigProps) {
  return (
    <details className="mining-evo-section" data-evo-section="exploration" open>
      <summary>{t("miningEvoAdvancedExploration")}</summary>
      <label>
        <span>{t("miningEvoAiEnabled")}</span>
        <input
          type="checkbox"
          data-evo-ai-enabled-advanced
          checked={aiEnabled}
          onChange={(e) => onAiEnabledChange?.(e.target.checked)}
        />
      </label>
      <label>
        <span>{t("miningEvoAiRatio")}</span>
        <input
          type="number" step="0.1" min="0" max="1"
          data-evo-ai-ratio
          defaultValue={aiRatio}
          onChange={(e) => onChange?.({ ai_ratio: Number(e.target.value) })}
        />
      </label>
      <label>
        <span>{t("miningEvoRandomRatio")}</span>
        <input
          type="number" step="0.1" min="0" max="1"
          data-evo-random-ratio
          defaultValue={randomRatio}
          onChange={(e) => onChange?.({ random_ratio: Number(e.target.value) })}
        />
      </label>
    </details>
  );
}
