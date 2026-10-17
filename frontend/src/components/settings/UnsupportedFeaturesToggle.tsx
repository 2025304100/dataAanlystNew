import { useEffect, useState } from "react";
import { Space, Switch, Typography } from "antd";
import { t } from "../../i18n";

const KEY = "settings.show_unsupported";
const EVENT = "settings:unsupported-visibility";

function readPreference() {
  try { return window.localStorage.getItem(KEY) === "true"; } catch { return false; }
}

/** This preference changes visibility, never the execution capability. */
export function useUnsupportedFeatures() {
  const [visible, setVisible] = useState(readPreference);
  useEffect(() => {
    const reload = () => setVisible(readPreference());
    window.addEventListener(EVENT, reload);
    window.addEventListener("storage", reload);
    return () => {
      window.removeEventListener(EVENT, reload);
      window.removeEventListener("storage", reload);
    };
  }, []);
  return visible;
}

export default function UnsupportedFeaturesToggle() {
  const visible = useUnsupportedFeatures();
  return <Space wrap style={{ justifyContent: "flex-end", width: "100%", marginBottom: 12 }}>
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>{t("settingsUnsupportedVisibilityHelp")}</Typography.Text>
    <Switch size="small" checked={visible} aria-label={t("settingsShowUnsupported")} onChange={(checked) => {
      try { window.localStorage.setItem(KEY, String(checked)); } catch { /* Display remains conservative. */ }
      window.dispatchEvent(new Event(EVENT));
    }} />
    <span>{t("settingsShowUnsupported")}</span>
  </Space>;
}
