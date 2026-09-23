import { t } from "../../../../../i18n";
import { Checkbox, InputNumber, Tooltip } from "antd";

/**
 * 选择机制配置（§6.5 赛道竞争 + 多目标 + 锦标赛；antd 控件版）。
 * 术语只在本折叠区出现（简单模式不渲染本组件）。
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

      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoTrackEnabled")}</span>
        <div data-evo-track-enabled>
          <Checkbox
            checked={trackEnabled}
            onChange={(e) => emit({ track_enabled: e.target.checked })}
          />
        </div>
      </div>

      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoTrackFloor")}</span>
        <InputNumber
          data-evo-track-floor
          min={0}
          max={1}
          step={0.05}
          value={trackFloorRatio}
          onChange={(v) => emit({ track_floor_ratio: typeof v === "number" ? v : trackFloorRatio })}
        />
      </div>

      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoWeakGenerations")}</span>
        <InputNumber
          data-evo-weak-generations
          min={1}
          value={weakGenerations}
          onChange={(v) => emit({ weak_generations: typeof v === "number" ? v : weakGenerations })}
        />
      </div>

      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoCrossCategory")}</span>
        <InputNumber
          data-evo-cross-category
          min={0}
          max={1}
          step={0.05}
          value={crossCategoryRatio}
          onChange={(v) => emit({ cross_category_ratio: typeof v === "number" ? v : crossCategoryRatio })}
        />
      </div>

      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoTournamentK")}</span>
        <Tooltip title={t("miningEvoTournamentNote")}>
          <InputNumber
            data-evo-tournament-k
            min={2}
            max={7}
            value={tournamentK}
            onChange={(v) => emit({ tournament_k: typeof v === "number" ? v : tournamentK })}
          />
        </Tooltip>
      </div>
    </details>
  );
}