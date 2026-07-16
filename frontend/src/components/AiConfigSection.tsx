import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Form, Input, Select, Switch, Tag, Tooltip, message } from "antd";
import { CheckCircleOutlined, CloseCircleOutlined, ReloadOutlined, RobotOutlined, SyncOutlined } from "@ant-design/icons";
import { t } from "../i18n";
import { api } from "../api/client";
import type { AiModelInfo } from "../api/client";

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

  const loadConfig = useCallback(async () => {
    setLoading(true);
    try {
      const cfg = await api.getAiConfig();
      setEnabled(cfg.enabled);
      form.setFieldsValue({
        service_url: cfg.service_url,
        api_key: cfg.api_key,
        model: cfg.model,
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

  const fetchModels = useCallback(async () => {
    const serviceUrl = form.getFieldValue("service_url");
    const apiKey = form.getFieldValue("api_key");
    if (!serviceUrl || !apiKey) {
      message.warning(t("aiModelFetchHint"));
      return;
    }
    setModelsLoading(true);
    try {
      const result = await api.listAiModels({ service_url: serviceUrl, api_key: apiKey });
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
  }, [form]);

  const handleSave = async () => {
    try {
      const values = form.getFieldsValue();
      setSaving(true);
      setTestResult(null);
      await api.updateAiConfig({ ...values, enabled });
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
      const result = await api.testAiConnection({
        service_url: form.getFieldValue("service_url") || "",
        api_key: form.getFieldValue("api_key") || "",
        model: form.getFieldValue("model") || "",
      });
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

        <Form layout="vertical" form={form}>
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

          <Form.Item label={t("aiApiKey")} name="api_key" rules={[{ required: true, message: t("aiApiKeyRequired") }]}>
            <Input.Password placeholder="sk-..." />
          </Form.Item>

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
              disabled={!form.getFieldValue("service_url") || !form.getFieldValue("api_key")}
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
