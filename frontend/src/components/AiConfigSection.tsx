import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Collapse, Form, Input, InputNumber, Select, Switch, Tag, Tooltip, message } from "antd";
import { CheckCircleOutlined, CloseCircleOutlined, ReloadOutlined, RobotOutlined, SyncOutlined } from "@ant-design/icons";
import { t } from "../i18n";
import { api } from "../api/client";
import type { AiConfigUpdate, AiModelInfo } from "../api/client";

const PROVIDER_PRESETS: Record<string, Partial<AiConfigUpdate>> = {
  openai_compatible: {
    service_url: "https://api.openai.com/v1",
    auth_type: "bearer",
    auth_header: "Authorization",
    chat_path: "/chat/completions",
    models_path: "/models",
  },
  anthropic: {
    service_url: "https://api.anthropic.com/v1",
    auth_type: "x-api-key",
    auth_header: "x-api-key",
    chat_path: "/messages",
    models_path: "/models",
  },
  ollama: {
    service_url: "http://127.0.0.1:11434",
    auth_type: "none",
    auth_header: "Authorization",
    chat_path: "/api/chat",
    models_path: "/api/tags",
  },
  custom: {
    service_url: "",
    auth_type: "bearer",
    auth_header: "Authorization",
    chat_path: "/chat/completions",
    models_path: "/models",
  },
};

export default function AiConfigSection() {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [models, setModels] = useState<AiModelInfo[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelSearch, setModelSearch] = useState("");
  const [persisted, setPersisted] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const serviceUrl = Form.useWatch("service_url", form) || "";
  const watchedApiKey = Form.useWatch("api_key", form) || "";
  const authType = Form.useWatch("auth_type", form) || "bearer";
  const canSubmit = Boolean(serviceUrl) && (authType === "none" || Boolean(watchedApiKey));

  const loadConfig = useCallback(async () => {
    setLoading(true);
    try {
      const cfg = await api.getAiConfig();
      setEnabled(cfg.enabled);
      setPersisted(cfg.persisted);
      setUpdatedAt(cfg.updated_at || null);
      form.setFieldsValue({
        provider: cfg.provider,
        service_url: cfg.service_url,
        api_key: cfg.api_key,
        model: cfg.model,
        auth_type: cfg.auth_type,
        auth_header: cfg.auth_header,
        chat_path: cfg.chat_path,
        models_path: cfg.models_path,
        timeout_seconds: cfg.timeout_seconds,
        temperature: cfg.temperature,
        max_tokens: cfg.max_tokens,
        extra_headers_json: Object.keys(cfg.extra_headers || {}).length
          ? JSON.stringify(cfg.extra_headers, null, 2)
          : "",
      });
      // 如果有已保存的模型且不在列表中，保留为自定义选项
      if (cfg.model) {
        setModels((prev) => {
          const exists = prev.some((m) => m.id === cfg.model);
          if (!exists && cfg.model) {
            return [...prev, { id: cfg.model, owned_by: "custom", created: 0 }];
          }
          return prev;
        });
      }
    } catch (error: any) {
      message.error(error?.message || t("aiConfigLoadFailed"));
    } finally {
      setLoading(false);
    }
  }, [form]);

  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  const buildPayload = useCallback((): AiConfigUpdate => {
    const values = form.getFieldsValue(true);
    let extraHeaders: Record<string, string> = {};
    const rawHeaders = String(values.extra_headers_json || "").trim();
    if (rawHeaders) {
      const parsed = JSON.parse(rawHeaders);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error(t("aiExtraHeadersInvalid"));
      }
      extraHeaders = Object.fromEntries(
        Object.entries(parsed).map(([key, value]) => [key, String(value)]),
      );
    }
    const { extra_headers_json: _ignored, ...configValues } = values;
    return { ...configValues, extra_headers: extraHeaders, enabled };
  }, [enabled, form]);

  const handleProviderChange = useCallback((provider: string) => {
    form.setFieldsValue({ provider, ...PROVIDER_PRESETS[provider] });
    setModels([]);
    setTestResult(null);
  }, [form]);

  const fetchModels = useCallback(async () => {
    if (!canSubmit) {
      message.warning(t("aiModelFetchHint"));
      return;
    }
    setModelsLoading(true);
    try {
      const result = await api.listAiModels(buildPayload());
      if (result.error) {
        message.warning(result.error);
      } else {
        setModels(result.models);
        message.success(t("aiModelFetched").replace("{count}", String(result.models.length)));
      }
    } catch (error: any) {
      message.error(error?.message || t("aiModelFetchFailed"));
    } finally {
      setModelsLoading(false);
    }
  }, [buildPayload, canSubmit]);

  const handleSave = async () => {
    try {
      await form.validateFields();
      const values = buildPayload();
      setSaving(true);
      setTestResult(null);
      await api.updateAiConfig(values);
      await loadConfig();
      message.success(t("aiConfigSaved"));
    } catch (error: any) {
      message.error(error?.message || t("aiConfigSaveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    try {
      setTesting(true);
      setTestResult(null);
      const result = await api.testAiConnection(buildPayload());
      setTestResult(result);
      if (result.success) {
        message.success(t("aiTestSuccess"));
      } else {
        message.warning(result.message);
      }
    } catch (error: any) {
      setTestResult({ success: false, message: error?.message || t("aiTestFailed") });
      message.error(error?.message || t("aiTestFailed"));
    } finally {
      setTesting(false);
    }
  };

  const modelOptions = models.map((m) => ({
    label: m.owned_by ? `${m.id} (${m.owned_by})` : m.id,
    value: m.id,
  }));

  return (
    <div className="settings-db-section">
      <div className="panel">
        <div className="panel-head">
          <div>
            <p className="panel-kicker"><RobotOutlined style={{ marginRight: 6 }} />{t("aiConfigKicker")}</p>
            <h2>{t("aiConfigTabTitle")}</h2>
          </div>
          <Tag color={enabled ? "green" : "default"} style={{ fontSize: 13, padding: "2px 10px" }}>
            {enabled ? t("aiEnabled") : t("aiDisabled")}
          </Tag>
        </div>

        <Alert
          type="info"
          showIcon
          message={t("aiConfigDesc")}
          style={{ marginBottom: 20 }}
        />
        {persisted && (
          <Alert
            type="success"
            showIcon
            message={t("aiConfigPersisted")}
            description={updatedAt ? `${t("aiConfigUpdatedAt")}: ${new Date(updatedAt).toLocaleString()}` : undefined}
            style={{ marginBottom: 20 }}
          />
        )}

        <Form layout="vertical" form={form}>
          <Form.Item label={t("aiProvider")} name="provider" initialValue="openai_compatible">
            <Select
              onChange={handleProviderChange}
              options={[
                { value: "openai_compatible", label: "OpenAI Compatible" },
                { value: "anthropic", label: "Anthropic Messages" },
                { value: "ollama", label: "Ollama" },
                { value: "custom", label: t("aiProviderCustom") },
              ]}
            />
          </Form.Item>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Form.Item label={t("aiServiceUrl")} name="service_url" rules={[{ required: true, message: t("aiServiceUrlRequired") }]}>
              <Input placeholder="https://api.openai.com/v1" />
            </Form.Item>
            <Form.Item
              label={
                <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  {t("aiModel")}
                  <Tooltip title={t("aiModelRefreshTip")}>
                    <Button
                      size="small"
                      type="link"
                      icon={<ReloadOutlined spin={modelsLoading} />}
                      onClick={fetchModels}
                      loading={modelsLoading}
                      style={{ padding: 0, height: "auto" }}
                    />
                  </Tooltip>
                  {models.length > 0 && <Tag style={{ margin: 0 }}>{models.length}</Tag>}
                </span>
              }
              name="model"
            >
              <Select
                showSearch
                allowClear
                filterOption={(input, option) => (option?.label ?? "").toLowerCase().includes(input.toLowerCase())}
                placeholder={models.length > 0 ? t("aiModelSelectPlaceholder") : t("aiModelInputPlaceholder")}
                options={modelOptions}
                onSearch={setModelSearch}
                notFoundContent={modelSearch ? t("aiModelNotFound") : t("aiModelFetchFirst")}
                mode={modelSearch && !models.some((m) => m.id.toLowerCase() === modelSearch.toLowerCase()) ? "tags" : undefined}
              />
            </Form.Item>
          </div>

          <Form.Item
            label={`${t("aiApiKey")}${authType === "none" ? ` (${t("aiOptional")})` : ""}`}
            name="api_key"
            rules={[{ required: authType !== "none", message: t("aiApiKeyRequired") }]}
          >
            <Input.Password placeholder={authType === "none" ? t("aiApiKeyOptional") : "sk-..."} />
          </Form.Item>

          <Collapse
            ghost
            style={{ marginBottom: 16 }}
            items={[{
              key: "advanced",
              label: t("aiAdvancedSettings"),
              children: (
                <>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                    <Form.Item label={t("aiAuthType")} name="auth_type" initialValue="bearer">
                      <Select options={[
                        { value: "bearer", label: "Bearer Token" },
                        { value: "x-api-key", label: "x-api-key" },
                        { value: "api-key", label: "api-key" },
                        { value: "custom", label: t("aiAuthCustom") },
                        { value: "none", label: t("aiAuthNone") },
                      ]} />
                    </Form.Item>
                    <Form.Item label={t("aiAuthHeader")} name="auth_header" initialValue="Authorization">
                      <Input disabled={authType !== "custom"} placeholder="Authorization" />
                    </Form.Item>
                    <Form.Item label={t("aiChatPath")} name="chat_path" initialValue="/chat/completions">
                      <Input placeholder="/chat/completions" />
                    </Form.Item>
                    <Form.Item label={t("aiModelsPath")} name="models_path" initialValue="/models">
                      <Input placeholder="/models" />
                    </Form.Item>
                    <Form.Item label={t("aiTimeoutSeconds")} name="timeout_seconds" initialValue={30}>
                      <InputNumber min={3} max={300} style={{ width: "100%" }} />
                    </Form.Item>
                    <Form.Item label={t("aiMaxTokens")} name="max_tokens" initialValue={1024}>
                      <InputNumber min={1} max={32768} style={{ width: "100%" }} />
                    </Form.Item>
                    <Form.Item label={t("aiTemperature")} name="temperature" initialValue={0.3}>
                      <InputNumber min={0} max={2} step={0.1} style={{ width: "100%" }} />
                    </Form.Item>
                  </div>
                  <Form.Item label={t("aiExtraHeaders")} name="extra_headers_json">
                    <Input.TextArea rows={3} placeholder='{"X-Custom-Header": "value"}' />
                  </Form.Item>
                </>
              ),
            }]}
          />

          <div style={{ marginBottom: 16, display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 14 }}>{t("aiEnableSwitch")}:</span>
            <Switch checked={enabled} onChange={setEnabled} />
            <span style={{ fontSize: 13, color: enabled ? "#0f766e" : "#9ca3af" }}>
              {enabled ? t("aiEnabled") : t("aiDisabled")}
            </span>
          </div>

          <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 20 }}>
            <Button type="default" loading={testing} onClick={handleTest} icon={<SyncOutlined />}>
              {t("aiTestConn")}
            </Button>
            {testResult && (
              <span style={{ color: testResult.success ? "#0f766e" : "#b42318", fontSize: 13, display: "inline-flex", alignItems: "center", gap: 4 }}>
                {testResult.success
                  ? <><CheckCircleOutlined /> {testResult.message}</>
                  : <><CloseCircleOutlined /> {testResult.message}</>
                }
              </span>
            )}
          </div>

          <div style={{ marginBottom: 20 }}>
            <Button
              type="primary"
              size="large"
              loading={saving}
              onClick={handleSave}
              disabled={!canSubmit}
            >
              {t("aiSave")}
            </Button>
          </div>

          <Alert
            type="warning"
            showIcon
            message={t("aiPrivacyNote")}
            style={{ marginTop: 8 }}
          />
        </Form>
      </div>
    </div>
  );
}
