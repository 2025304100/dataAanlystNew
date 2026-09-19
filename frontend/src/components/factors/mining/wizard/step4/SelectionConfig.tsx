import { t } from "../../../../../i18n";

/**
 * 选择机制配置（高级模式折叠区，向导 §6.5.5）—— 术语只在本折叠区出现。
 *
 * 简单模式**不得渲染本组件**（简单模式禁出现「赛道/锦标赛/帕累托」等术语）。
 */
export interface SelectionConfigProps {
  trackEnabled?: boolean;
  trackFloorRatio?: number;
  weakGenerations?: number;
  crossCategoryRatio?: number;
  tournamentK?: number;
  onChange?: (patch: Record<string, unknown>) => void;
}

export default function SelectionConfig({
  trackEnabled = true,
  trackFloorRatio = 0.6,
  weakGenerations = 3,
  crossCategoryRatio = 0.2,
  tournamentK = 3,
  onChange,
}: SelectionConfigProps) {
  const emit = (patch: Record<string, unknown>) => onChange?.(patch);
  return (
    <details className="mining-evo-section" data-evo-section="selection" open>
      <summary>{t("miningEvoAdvancedSelection")}</summary>
      <label>
        <span>{t("miningEvoTrackEnabled")}</span>
        <input
          type="checkbox"
          data-evo-track-enabled
          checked={trackEnabled}
          onChange={(e) => emit({ track_enabled: e.target.checked })}
        />
      </label>
      <label>
        <span>{t("miningEvoTrackFloor")}</span>
        <input
          type="number" step="0.05" min="0" max="1"
          data-evo-track-floor
          defaultValue={trackFloorRatio}
          onChange={(e) => emit({ track_floor_ratio: Number(e.target.value) })}
        />
      </label>
      <label>
        <span>{t("miningEvoWeakGenerations")}</span>
        <input
          type="number" min="1"
          data-evo-weak-generations
          defaultValue={weakGenerations}
          onChange={(e) => emit({ weak_generations: Number(e.target.value) })}
        />
      </label>
      <label>
        <span>{t("miningEvoCrossCategory")}</span>
        <input
          type="number" step="0.05" min="0" max="1"
          data-evo-cross-category
          defaultValue={crossCategoryRatio}
          onChange={(e) => emit({ cross_category_ratio: Number(e.target.value) })}
        />
      </label>
      <label>
        <span>{t("miningEvoTournamentK")}</span>
        <input
          type="number" min="2" max="7"
          data-evo-tournament-k
          defaultValue={tournamentK}
          onChange={(e) => emit({ tournament_k: Number(e.target.value) })}
        />
      </label>
    </details>
  );
}
