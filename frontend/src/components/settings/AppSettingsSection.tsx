import { useCallback, useEffect, useState } from "react";
import { Alert, Switch } from "antd";
import { api } from "../../api/client";
import type { AppSettingRead } from "../../types";
import { t } from "../../i18n";

/**
 * 「系统设置」分区：渲染后端 `app_settings` 登记表里的可持久化开关。
 *
 * 前端**不维护开关清单**：键名、文案、类型、默认值、以及"当前这个值是从哪儿来的"
 * 全部由 `GET /settings/app` 下发。以后新增一个开关只需要在后端登记表加一条，
 * 本组件零改动 —— 否则就是"同一份事实两处记录"（体检报告 PT-DEF-13 那一类脱节）。
 *
 * 保存失败时必须回读后端真相：乐观更新留在界面上会显示"已打开"而实际没存，
 * 是比报错更糟的假反馈。
 */
export default function AppSettingsSection() {
  const [items, setItems] = useState<AppSettingRead[] | null>(null);
  const [loading, setLoading] = useState(false);
  // 区分加载 / 保存失败：两者提示文案不能同一句（“保存没成功”报成“读取失败”会误导运维）
  const [error, setError] = useState<{ kind: "load" | "save"; message: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError((prev) => (prev?.kind === "save" ? prev : null));
    try {
      setItems(await api.getAppSettings());
    } catch (e: any) {
      setError({ kind: "load", message: String(e?.message || e || "load failed") });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (item: AppSettingRead, next: boolean) => {
    setItems((prev) =>
      (prev ?? []).map((it) =>
        it.key === item.key ? { ...it, value: next, source: "table" } : it,
      ),
    );
    setError(null);
    try {
      await api.putAppSetting(item.key, next);
    } catch (e: any) {
      setError({ kind: "save", message: String(e?.message || e || "save failed") });
      // 回到后端的真相，别把没存成功的乐观更新留在界面上
      await load();
    }
  };

  const sourceLabel = (item: AppSettingRead) => {
    if (item.source === "table") return t("appSettingsSourceTable");
    if (item.source === "env") return t("appSettingsSourceEnv");
    return t("appSettingsSourceDefault");
  };

  const empty = !loading && (items === null || items.length === 0);

  return (
    <div data-app-settings-section>
      <p className="mining-exp-desc">{t("appSettingsDesc")}</p>
      {error && (
        <Alert
          type="error"
          showIcon
          message={
            error.kind === "save"
              ? t("appSettingsSaveFailed")
              : t("appSettingsLoadFailed")
          }
          description={error.message}
          style={{ marginBottom: 12 }}
        />
      )}
      {empty && <p data-app-settings-empty>{t("appSettingsEmpty")}</p>}
      {(items ?? []).map((item) => (
        <div
          key={item.key}
          data-app-setting-item={item.key}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 16,
            padding: "10px 0",
            borderTop: "1px solid rgba(0,0,0,0.06)",
          }}
        >
          <div style={{ minWidth: 0 }}>
            <strong>{item.label_zh || item.key}</strong>
            <div className="item-subline">{item.description_zh || item.key}</div>
            <div className="item-subline" data-app-setting-source={item.key}>
              {t("appSettingsValueSource")}
              {"："}
              {sourceLabel(item)}
              {item.env_var ? `（${item.env_var}）` : ""}
            </div>
          </div>
          {item.type === "bool" ? (
            <Switch
              checked={item.value === true}
              loading={loading}
              onChange={(next) => void toggle(item, next)}
              aria-label={item.label_zh || item.key}
              data-app-setting-switch={item.key}
            />
          ) : (
            <span className="item-subline">{t("appSettingsUnsupportedType")}</span>
          )}
        </div>
      ))}
    </div>
  );
}
