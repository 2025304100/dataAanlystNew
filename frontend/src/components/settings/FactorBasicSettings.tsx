import { useEffect, useState } from "react";
import { InputNumber, Select, Space, Typography } from "antd";
import { t } from "../../i18n";
import { api } from "../../api/client";

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
  /** 后端真实返回但此前未展示的字段 */
  featureEnabled?: boolean | null;
  updatedBy?: string | null;
  updatedAt?: string | null;
}

/** GET /factors/config 的原始结构（字段名与后端一致，非 camelCase）。 */
interface FactorConfigResponse {
  feature_enabled?: boolean;
  warehouse_path?: string | null;
  updated_by?: string | null;
  updated_at?: string | null;
}

function toConfig(raw: FactorConfigResponse): FactorBasicConfig {
  return {
    warehousePath: raw.warehouse_path ?? null,
    featureEnabled: raw.feature_enabled ?? null,
    updatedBy: raw.updated_by ?? null,
    updatedAt: raw.updated_at ?? null,
    // 后端目前不返回这三项：留 null，由 UI 明确标"后端未提供"，不再假装"未设置"
    defaultDirection: null,
    maxComplexity: null,
    language: null,
  };
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
  config: configProp = null,
}: FactorBasicSettingsProps) {
  // VIZ-0930-20：此前 Settings.tsx 以 <FactorBasicSettings /> 无 props 调用，config 恒为 null，
  // 于是这个分区永远停在"后端配置未下发/未设置"，而 GET /factors/config 其实有真实值。
  // 补上自身读取：父层没注入就自己去拿。
  const [fetched, setFetched] = useState<FactorBasicConfig | null>(configProp);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (configProp) {
      setFetched(configProp);
      return;
    }
    let cancelled = false;
    api.getFactorBasicConfig()
      .then((raw) => { if (!cancelled) { setFetched(toConfig(raw as FactorConfigResponse)); setLoadError(null); } })
      .catch((err: unknown) => {
        if (cancelled) return;
        setFetched(null);
        setLoadError(err instanceof Error ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, [configProp]);

  const config = fetched;
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

      {empty && (
        <p data-factor-basic-empty>
          {t("factorBasicEmpty")}
          {loadError ? `（${loadError}）` : ""}
        </p>
      )}

      <section className="mining-pool-blocked" style={{ borderColor: "var(--line)", background: "rgba(255,255,255,0.5)", color: "var(--ink)" }}>
        <dl className="factor-basic-dl">
          <div>
            <dt>{t("factorBasicDefaultDirection")}</dt>
            <dd>{direction ? t(DIRECTIONS.find((d) => d.value === direction)?.labelKey ?? direction) : t("factorBasicNotProvided")}</dd>
          </div>
          <div>
            <dt>{t("factorBasicWarehouse")}</dt>
            <dd>{config?.warehousePath ?? t("factorBasicPlaceholder")}</dd>
          </div>
          <div>
            <dt>{t("factorBasicLanguage")}</dt>
            <dd>{config?.language ?? t("factorBasicNotProvided")}</dd>
          </div>
          <div>
            <dt>{t("factorBasicEnabled")}</dt>
            <dd>
              {config?.featureEnabled == null
                ? t("factorBasicPlaceholder")
                : config.featureEnabled
                  ? t("yes")
                  : t("no")}
            </dd>
          </div>
          <div>
            <dt>{t("factorBasicUpdatedAt")}</dt>
            <dd>{config?.updatedAt ?? t("factorBasicPlaceholder")}{config?.updatedBy ? ` · ${config.updatedBy}` : ""}</dd>
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