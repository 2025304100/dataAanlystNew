import { t } from "../../../../../i18n";

/**
 * 繁殖与变异配置（高级模式折叠区，向导 §6.6.8）。
 *
 * - **自适应开启**（默认）：C2 类型比例 / C3 跨赛道率 / D2 注入率显示为灰色
 *   **「自动」**，不可手改（避免双调度）；
 * - 关闭自适应后才解锁手动配置，二选一不打架；
 * - 安全上下限与防抖参数**锁死不暴露**（不在此渲染）。
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
      <label>
        <span>{t("miningEvoAdaptive")}</span>
        <input
          type="checkbox"
          data-evo-adaptive
          checked={adaptive}
          onChange={(e) => onAdaptiveChange?.(e.target.checked)}
        />
      </label>
      {rows.map(([key, value]) => (
        <label key={key}>
          <span>{labelOf[key]}</span>
          {adaptive ? (
            <span data-evo-rate-auto={key} className="mining-evo-auto">
              {t("miningEvoAuto")}
            </span>
          ) : null}
          <input
            type="number"
            step="0.05"
            min="0"
            max="1"
            data-evo-rate-input={key}
            disabled={adaptive}
            defaultValue={value}
            onChange={(e) => onChange?.({ [`${key}_rate`]: Number(e.target.value) })}
          />
        </label>
      ))}
    </details>
  );
}
