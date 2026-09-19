import { t } from "../../../../../i18n";

/**
 * 经典底座配置（高级模式折叠区，向导 §6.5.5）。
 *
 * 类别与 Step3 字段**联动置灰**：未选估值/财报字段时对应用量类别置灰
 * （由父级传入 `availableCategories` 决定）。
 */
export interface ClassicalBaseConfigProps {
  availableCategories?: string[];
  categoryLimits?: Record<string, number>;
  coverageStrategy?: "keep" | "drop" | "reselect";
  onChange?: (patch: Record<string, unknown>) => void;
}

const ALL_CATEGORIES = ["trend", "reversal", "volatility", "valuation", "quality", "volume_price"];

export default function ClassicalBaseConfig({
  availableCategories = ALL_CATEGORIES,
  categoryLimits = {},
  coverageStrategy = "keep",
  onChange,
}: ClassicalBaseConfigProps) {
  return (
    <details className="mining-evo-section" data-evo-section="classical" open>
      <summary>{t("miningEvoAdvancedClassical")}</summary>
      {ALL_CATEGORIES.map((cat) => {
        const disabled = !availableCategories.includes(cat);
        return (
          <label key={cat} className={disabled ? "is-disabled" : ""}>
            <span>{cat}</span>
            <input
              type="number"
              min="0"
              data-evo-category-limit={cat}
              disabled={disabled}
              defaultValue={categoryLimits[cat] ?? 0}
              onChange={(e) => onChange?.({ category_limits: { [cat]: Number(e.target.value) } })}
            />
          </label>
        );
      })}
      <label>
        <span>{t("miningEvoCoverageStrategy")}</span>
        <select
          data-evo-coverage-strategy
          defaultValue={coverageStrategy}
          onChange={(e) => onChange?.({ coverage_strategy: e.target.value })}
        >
          <option value="keep">{t("miningEvoCoverageKeep")}</option>
          <option value="drop">{t("miningEvoCoverageDrop")}</option>
          <option value="reselect">{t("miningEvoCoverageReselect")}</option>
        </select>
      </label>
    </details>
  );
}
