import { t } from "../../../../../i18n";

/**
 * 条件筛选面板（向导 §3.2）。
 *
 * 布局口径：左「常用区间预设」+ 右「分类条件 Accordion」；本期实现为
 * 预设区 + 常用区间输入 + 分类勾选区的简化骨架（完整 7 类 Accordion 随
 * `filter-fields` 元数据逐步展开），**所有区间留空表示无该侧边界**。
 *
 * 锁定态下全部控件置灰（§3.7.3），hover 提示走 `title`。
 */
export interface PoolFilterPanelProps {
  locked: boolean;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}

export default function PoolFilterPanel({ locked, value, onChange }: PoolFilterPanelProps) {
  const minCap = typeof value.min_market_cap === "string" ? value.min_market_cap : "";
  return (
    <div className="mining-pool-filter" data-pool-filter-panel>
      <div className="mining-pool-filter-presets">
        <span>{t("miningPoolFilterTitle")}</span>
      </div>
      <div className="mining-pool-filter-range">
        <label htmlFor="pool-min-cap">{t("miningPoolFilterInputLabel")}</label>
        <input
          id="pool-min-cap"
          data-pool-filter-input
          type="number"
          disabled={locked}
          title={locked ? t("miningPoolLockedHint") : undefined}
          value={minCap}
          onChange={(e) => onChange({ ...value, min_market_cap: e.target.value })}
        />
      </div>
    </div>
  );
}
