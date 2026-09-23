import { t } from "../../../../../i18n";
import { Checkbox, InputNumber } from "antd";

/**
 * 繁殖与变异配置（§6.6.8；antd 控件版）。
 *
 * - **自适应开启**（默认）：三率显示灰色「自动」，不可手改（避免双调度）；
 * - 关闭自适应后解锁手动配置；
 * - 安全上下限与防抖参数**锁死不暴露**。
 */
export interface ReproductionConfigProps {
  adaptive: boolean;
  mutationRate?: number;
  crossoverRate?: number;
  randomRate?: number;
  onAdaptiveChange?: (next: boolean) => void;
  onChange?: (patch: Record<string, unknown>) => void;
}

export default function ReproductionConfig({
  adaptive,
  mutationRate = 0.55,
  crossoverRate = 0.25,
  randomRate = 0.2,
  onAdaptiveChange,
  onChange,
}: ReproductionConfigProps) {
  const rows: Array<[string, number]> = [
    ["mutation", mutationRate],
    ["crossover", crossoverRate],
    ["random", randomRate],
  ];
  const labelOf: Record<string, string> = {
    mutation: t("miningEvoMutationRate"),
    crossover: t("miningEvoCrossoverRate"),
    random: t("miningEvoRandomRate"),
  };
  return (
    <details className="mining-evo-section" data-evo-section="reproduction" open>
      <summary>{t("miningEvoAdaptiveSection")}</summary>

      <div className="mining-evo-row">
        <span className="mining-evo-field-label">{t("miningEvoAdaptive")}</span>
        <div data-evo-adaptive>
          <Checkbox
            checked={adaptive}
            onChange={(e) => onAdaptiveChange?.(e.target.checked)}
          />
        </div>
      </div>

      {rows.map(([key, value]) => (
        <div className="mining-evo-row" key={key}>
          <span className="mining-evo-field-label">{labelOf[key]}</span>
          {adaptive ? (
            <span data-evo-rate-auto={key} className="mining-evo-auto">
              {t("miningEvoAuto")}
            </span>
          ) : null}
          <div data-evo-rate-input={key}>
            <InputNumber
              min={0}
              max={1}
              step={0.05}
              disabled={adaptive}
              value={value}
              onChange={(v) => onChange?.({ [`${key}_rate`]: typeof v === "number" ? v : value })}
            />
          </div>
        </div>
      ))}
    </details>
  );
}