import { useState } from "react";
import { InputNumber, Select, Space, Typography } from "antd";
import { t } from "../../i18n";

/**
 * 「因子基础配置」设置栏（C2）。
 *
 * 展示/编辑因子基础配置的一组只读信息区 + 1 个表单控件（默认复杂度上限，
 * 仅本地展示，不落库；重点在于设置栏挂载可用）。
 *
 * - 三态/空态容错：配置读取异常或字段缺失时显示占位，不崩页；
 * - 选择器前缀 `data-factor-basic-*` 供测试定位。
 */
export interface FactorBasicConfig {
  defaultDirection?: string | null;
  maxComplexity?: number | null;
  language?: string | null;
  warehousePath?: string | null;
}

interface FactorBasicSettingsProps {
  /** 注入的配置；缺省用内置默认（模拟后端未实现时的空态回退） */
  config?: FactorBasicConfig | null;
}

const DIRECTIONS = [
  { value: "long", labelKey: "factorBasicDirectionLong" },
  { value: "short", labelKey: "factorBasicDirectionShort" },
  { value: "both", labelKey: "factorBasicDirectionBoth" },
];

export default function FactorBasicSettings({
  config = null,
}: FactorBasicSettingsProps) {
  // 内部展示用状态：config 为空 → 空态，但控件仍可编辑（不落库）
  const [direction, setDirection] = useState<string | null>(
    config?.defaultDirection ?? null,
  );
  const [maxComplexity, setMaxComplexity] = useState<number | null>(
    config?.maxComplexity ?? 5,
  );

  const empty = config == null;

  return (
    <div data-factor-basic-settings className="mining-exp-page">
      <p className="mining-exp-desc">{t("factorBasicDesc")}</p>

      {empty && <p data-factor-basic-empty>{t("factorBasicEmpty")}</p>}

      <section className="mining-pool-blocked" style={{ borderColor: "var(--line)", background: "rgba(255,255,255,0.5)", color: "var(--ink)" }}>
        <dl className="factor-basic-dl">
          <div>
            <dt>{t("factorBasicDefaultDirection")}</dt>
            <dd>{direction ? t(DIRECTIONS.find((d) => d.value === direction)?.labelKey ?? direction) : t("factorBasicPlaceholder")}</dd>
          </div>
          <div>
            <dt>{t("factorBasicWarehouse")}</dt>
            <dd>{config?.warehousePath ?? t("factorBasicPlaceholder")}</dd>
          </div>
          <div>
            <dt>{t("factorBasicLanguage")}</dt>
            <dd>{config?.language ?? "zh"}</dd>
          </div>
        </dl>
      </section>

      <div className="factor-basic-form" style={{ display: "grid", gap: 10, marginTop: 12 }}>
        <div className="mining-dl-row" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Typography.Text>{t("factorBasicMaxComplexity")}</Typography.Text>
          <InputNumber
            data-factor-basic-max-complexity
            min={1}
            max={20}
            value={maxComplexity ?? undefined}
            onChange={(v) => setMaxComplexity(v ?? null)}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("factorBasicMaxComplexityTip")}
          </Typography.Text>
        </div>
        <div className="mining-dl-row" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Typography.Text>{t("factorBasicDefaultDirection")}</Typography.Text>
          <Select
            data-factor-basic-direction
            style={{ width: 180 }}
            value={direction ?? undefined}
            placeholder={t("factorBasicPlaceholder")}
            onChange={(v) => setDirection(v)}
            options={DIRECTIONS.map((d) => ({
              value: d.value,
              label: t(d.labelKey),
            }))}
          />
        </div>
      </div>
    </div>
  );
}

export { FactorBasicSettings };
export type { FactorBasicSettingsProps };