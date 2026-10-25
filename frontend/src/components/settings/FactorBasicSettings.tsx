import { useEffect, useState } from "react";
import { InputNumber, Select } from "antd";
import { t } from "../../i18n";
import { api } from "../../api/client";
import { useUnsupportedFeatures } from "./UnsupportedFeaturesToggle";

/**
 * 「因子基础配置」设置栏（C2）。
 *
 * 读取真实功能状态和仓库信息；尚无执行消费者的演示配置由共享偏好
 * 控制展示，始终只读，不把展示开关当作业务启用。
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

/** 后端给的是 ISO 串（2026-08-30T03:36:50），显示时只去掉 T，不改时区与精度。 */
function displayTimestamp(value: string | null | undefined): string | null {
  return value ? value.replace("T", " ") : null;
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
  const [loading, setLoading] = useState(!configProp);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (configProp) {
      setFetched(configProp);
      setLoading(false);
      return;
    }
    // 请求在飞时不能先渲染"配置未下发"，否则每次进页先闪一条错误态文案。
    setLoading(true);
    let cancelled = false;
    api.getFactorBasicConfig()
      .then((raw) => {
        if (cancelled) return;
        setFetched(toConfig(raw as FactorConfigResponse));
        setLoadError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setFetched(null);
        setLoadError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [configProp]);

  const config = fetched;
  const showUnsupported = useUnsupportedFeatures();
  const enabled = config?.featureEnabled ?? null;
  const direction = config?.defaultDirection ?? null;
  const maxComplexity = config?.maxComplexity ?? 5;
  const updatedAt = displayTimestamp(config?.updatedAt);

  const empty = !loading && config == null;

  return (
    <div data-factor-basic-settings className="mining-exp-page factor-basic">
      <p className="mining-exp-desc">{t("factorBasicDesc")}</p>

      {loading && <p className="factor-basic-note">{t("factorBasicLoading")}</p>}

      {empty && (
        <p data-factor-basic-empty className="factor-basic-note">
          {t("factorBasicEmpty")}
          {loadError ? `（${loadError}）` : ""}
        </p>
      )}

      {config && (
        <section className="factor-basic-card" aria-label={t("factorBasicTabTitle")}>
          <div className="factor-basic-grid">
            <div className="factor-basic-item factor-basic-item--wide">
              <span className="factor-basic-label">{t("factorBasicWarehouse")}</span>
              <code className="factor-basic-path" title={config.warehousePath ?? undefined}>
                {config.warehousePath ?? t("factorBasicPlaceholder")}
              </code>
            </div>

            <div className="factor-basic-item">
              <span className="factor-basic-label">{t("factorBasicEnabled")}</span>
              <span
                className="factor-basic-status"
                data-state={enabled == null ? "unknown" : enabled ? "on" : "off"}
              >
                <i className="factor-basic-dot" aria-hidden="true" />
                {enabled == null
                  ? t("factorBasicPlaceholder")
                  : enabled
                    ? t("factorBasicStatusOn")
                    : t("factorBasicStatusOff")}
              </span>
            </div>

            <div className="factor-basic-item">
              <span className="factor-basic-label">{t("factorBasicUpdatedAt")}</span>
              <span className="factor-basic-value">{updatedAt ?? t("factorBasicPlaceholder")}</span>
              {config.updatedBy && <span className="factor-basic-meta">{config.updatedBy}</span>}
            </div>
          </div>

          {showUnsupported && (
            <div className="factor-basic-unsupported">
              <p className="factor-basic-unsupported-head">{t("factorBasicUnsupportedTitle")}</p>
              <div className="factor-basic-unsupported-row">
                <span className="factor-basic-unsupported-label">{t("factorBasicDefaultDirection")}</span>
                <Select
                  data-factor-basic-direction
                  style={{ width: 180 }}
                  value={direction ?? undefined}
                  placeholder={t("factorBasicNotProvided")}
                  disabled
                  options={DIRECTIONS.map((d) => ({
                    value: d.value,
                    label: t(d.labelKey),
                  }))}
                />
              </div>
              <div className="factor-basic-unsupported-row">
                <span className="factor-basic-unsupported-label">{t("factorBasicLanguage")}</span>
                <span className="factor-basic-unsupported-value">{config.language ?? t("factorBasicNotProvided")}</span>
              </div>
              <div className="factor-basic-unsupported-row">
                <span className="factor-basic-unsupported-label">{t("factorBasicMaxComplexity")}</span>
                <InputNumber
                  data-factor-basic-max-complexity
                  min={1}
                  max={20}
                  value={maxComplexity ?? undefined}
                  disabled
                />
                <span className="factor-basic-unsupported-tip">{t("factorBasicMaxComplexityTip")}</span>
              </div>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

export { FactorBasicSettings };
export type { FactorBasicSettingsProps };
