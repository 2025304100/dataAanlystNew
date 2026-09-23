import { t } from "../../../../../i18n";
import { Checkbox, InputNumber, Radio, Tooltip } from "antd";

/**
 * 经典底座配置（§6.3 初始种群·第一层；antd 控件版）。
 *
 * - 经典数量上限（InputNumber）+ Tooltip：硬上限 = 种群大小 × 60%；
 * - 类别勾选（Checkbox）+ 数量上限：依赖估值/财报字段的类别在 Step3
 *   未选对应字段时**置灰**并提示「需选择 XX 字段」（§6.3.2 字段联动）；
 * - 覆盖策略（Radio：均衡覆盖 / 优先级覆盖）。
 */
export interface ClassicalBaseConfigProps {
  availableCategories?: string[];
  categoryLimits?: Record<string, number>;
  coverageStrategy?: "keep" | "drop" | "reselect";
  onChange?: (patch: Record<string, unknown>) => void;
}

const ALL_CATEGORIES = ["trend", "reversal", "volatility", "valuation", "quality", "volume_price"];

const CATEGORY_LABEL_KEY: Record<string, string> = {
  trend: "miningEvoCatTrend",
  reversal: "miningEvoCatReversal",
  volatility: "miningEvoCatVolatility",
  valuation: "miningEvoCatValuation",
  quality: "miningEvoCatQuality",
  volume_price: "miningEvoCatVolumePrice",
};

/** 类别 → 所需字段提示（§6.3.2 字段联动：估值/质量类依赖估值/财报字段） */
const CATEGORY_NEED_FIELD: Record<string, string> = {
  valuation: "miningEvoClassicNeedFieldValuation",
  quality: "miningEvoClassicNeedFieldFinancial",
};

export default function ClassicalBaseConfig({
  availableCategories = ALL_CATEGORIES,
  categoryLimits = {},
  coverageStrategy = "keep",
  onChange,
}: ClassicalBaseConfigProps) {
  return (
    <details className="mining-evo-section" data-evo-section="classical" open>
      <summary>{t("miningEvoAdvancedClassical")}</summary>

      {/* 覆盖策略（§6.3.2：均衡覆盖 / 优先级覆盖） */}
      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoCoverageStrategy")}</span>
        <Radio.Group
          data-evo-coverage-strategy
          value={coverageStrategy}
          onChange={(e) => onChange?.({ coverage_strategy: e.target.value })}
          options={[
            { value: "keep", label: t("miningEvoCoverModeBalanced") },
            { value: "drop", label: t("miningEvoCoverModePriority") },
          ]}
        />
      </div>

      {ALL_CATEGORIES.map((cat) => {
        const disabled = !availableCategories.includes(cat);
        const needKey = CATEGORY_NEED_FIELD[cat];
        const row = (
          <div className={`mining-evo-row ${disabled ? "is-disabled" : ""}`} key={cat}>
            <Checkbox checked={!disabled} disabled={disabled}>
              {t(CATEGORY_LABEL_KEY[cat] ?? cat)}
            </Checkbox>
            <InputNumber
              data-evo-category-limit={cat}
              min={0}
              max={60}
              disabled={disabled}
              value={categoryLimits[cat] ?? 0}
              onChange={(v) =>
                onChange?.({ category_limits: { [cat]: typeof v === "number" ? v : 0 } })
              }
            />
            {disabled && needKey ? (
              <span className="mining-filter-blocked-hint">{t(needKey)}</span>
            ) : null}
          </div>
        );
        return disabled && needKey ? (
          <Tooltip key={cat} title={t(needKey)}>
            {row}
          </Tooltip>
        ) : (
          row
        );
      })}

      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoCoverageKeep")}</span>
        <Tooltip title={t("miningEvoClassicLimitNote")}>
          <span className="mining-evo-muted">{t("miningEvoClassicLimitNote")}</span>
        </Tooltip>
      </div>
    </details>
  );
}