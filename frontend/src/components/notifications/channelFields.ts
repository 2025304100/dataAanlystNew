export type ChannelField = {
  key: string; labelKey: string; required?: boolean;
  kind?: "secret" | "list" | "number" | "boolean" | "target" | "transport" | "text";
};

export const channelFields: Record<string, ChannelField[]> = {
  in_app: [],
  wxpusher: [
    { key: "app_token", labelKey: "channelConfigAppToken", required: true, kind: "secret" },
    { key: "uids", labelKey: "channelConfigUid", kind: "list" },
    { key: "topic_ids", labelKey: "channelConfigTopicIds", kind: "list" },
  ],
  dingtalk: [
    { key: "webhook", labelKey: "channelConfigWebhookUrl", required: true, kind: "secret" },
    { key: "secret", labelKey: "channelConfigSecret", kind: "secret" },
  ],
  onebot: [
    { key: "webhook", labelKey: "channelConfigWebhookUrl", required: true, kind: "secret" },
    { key: "access_token", labelKey: "channelConfigToken", kind: "secret" },
    { key: "target_type", labelKey: "settingsNotificationTargetType", required: true, kind: "target" },
    { key: "target_id", labelKey: "settingsNotificationTargetId", required: true },
  ],
  email: [
    { key: "smtp_host", labelKey: "channelConfigSmtpHost", required: true },
    { key: "smtp_port", labelKey: "channelConfigSmtpPort", required: true, kind: "number" },
    { key: "transport", labelKey: "settingsNotificationTransport", required: true, kind: "transport" },
    { key: "username", labelKey: "channelConfigUsername", required: true },
    { key: "password", labelKey: "channelConfigPassword", required: true, kind: "secret" },
    { key: "from_addr", labelKey: "channelConfigFromAddr", required: true },
    { key: "to_addrs", labelKey: "channelConfigToAddrs", required: true, kind: "list" },
  ],
  webhook: [
    { key: "url", labelKey: "channelConfigWebhookUrl", required: true, kind: "secret" },
    { key: "auth_header", labelKey: "channelConfigToken", kind: "secret" },
  ],
};

export function channelFormConfig(type: string, values: Record<string, unknown>, editing: boolean) {
  const config: Record<string, unknown> = {};
  const clearKeys: string[] = [];
  for (const field of channelFields[type] ?? []) {
    const value = values[field.key];
    if (values[`clear_${field.key}`]) { clearKeys.push(field.key); continue; }
    if (value == null || value === "") continue;
    if (field.kind === "transport") {
      config.use_ssl = value === "ssl";
      config.use_tls = value === "starttls";
    } else if (field.kind === "list") {
      const parts = (Array.isArray(value) ? value : String(value).split(/[\n,，;]/)).map(String).map((v) => v.trim()).filter(Boolean);
      config[field.key] = field.key === "topic_ids" ? parts.map(Number) : parts;
    } else if (field.kind === "number") config[field.key] = Number(value);
    else if (field.kind === "boolean") config[field.key] = Boolean(value);
    else config[field.key] = value;
  }
  if (!editing && type === "email") {
    config.use_ssl ??= true;
    config.use_tls ??= false;
  }
  return { config, clear_config_keys: clearKeys };
}

export function channelEditValues(type: string, masked: string | null) {
  let config: Record<string, unknown> = {};
  try { config = JSON.parse(masked ?? "{}"); } catch { /* Empty form with retryable credentials. */ }
  const values: Record<string, unknown> = {};
  for (const field of channelFields[type] ?? []) {
    values[field.key] = field.kind === "transport" ? (config.use_ssl !== false ? "ssl" : config.use_tls ? "starttls" : "plain")
      : field.kind === "secret" ? undefined : config[field.key];
    values[`clear_${field.key}`] = false;
  }
  return values;
}
