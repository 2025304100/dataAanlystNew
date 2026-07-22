import { useState, useCallback, useEffect } from "react";
import {
  Card,
  Table,
  Button,
  Modal,
  Form,
  Input,
  InputNumber,
  Select,
  Switch,
  Space,
  Tag,
  Tooltip,
  Popconfirm,
  Spin,
  Empty,
  Alert,
  Progress,
  Descriptions,
  List,
  Typography,
  message,
} from "antd";
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  ApiOutlined,
  ExperimentOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  HeartOutlined,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { api } from "../../api/client";
import { t } from "../../i18n";
import type {
  AIProfile,
  AIProfileTestResult,
  AIProfileUsage,
  AIHealth,
} from "../../types";

const { Text } = Typography;

const PROVIDER_OPTIONS = [
  { label: "OpenAI", value: "openai" },
  { label: "Anthropic", value: "anthropic" },
  { label: "Ollama", value: "ollama" },
  { label: "Azure", value: "azure" },
  { label: "OpenAI Compatible", value: "openai_compatible" },
];

const AUTH_TYPE_OPTIONS = [
  { label: "Bearer", value: "bearer" },
  { label: "API Key", value: "api_key" },
  { label: "OAuth", value: "oauth" },
  { label: "None", value: "none" },
];

const PURPOSE_OPTIONS = [
  { label: "All", value: "all" },
  { label: "Explanation", value: "explanation" },
  { label: "Draft", value: "draft" },
  { label: "Chat", value: "chat" },
];

function healthTag(status: string): { color: string; label: string } {
  switch (status) {
    case "healthy":
      return { color: "green", label: t("aiSettings.healthHealthy") };
    case "degraded":
      return { color: "gold", label: t("aiSettings.healthDegraded") };
    case "down":
      return { color: "red", label: t("aiSettings.healthDown") };
    default:
      return { color: "default", label: t("aiSettings.healthUnknown") };
  }
}

interface ProfileFormValues {
  name: string;
  provider: string;
  base_url?: string;
  model: string;
  auth_type?: string;
  secret_value?: string;
  timeout_seconds: number;
  max_tokens: number;
  max_context_tokens: number;
  daily_request_limit: number;
  max_concurrent: number;
  purpose: string;
  priority: number;
  is_enabled: boolean;
  is_fallback: boolean;
}

export default function AISettings() {
  const [profiles, setProfiles] = useState<AIProfile[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<"create" | "edit">("create");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<ProfileFormValues>();

  // 测试连接状态
  const [testingId, setTestingId] = useState<number | null>(null);
  const [testResults, setTestResults] = useState<Record<number, AIProfileTestResult>>({});

  // 模型发现
  const [modelsModalOpen, setModelsModalOpen] = useState(false);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [discoveredModels, setDiscoveredModels] = useState<Array<{ id: string; owned_by?: string }>>([]);
  const [modelsProfileName, setModelsProfileName] = useState("");

  // 用量
  const [usageData, setUsageData] = useState<Record<number, AIProfileUsage>>({});

  // 健康状态
  const [healthData, setHealthData] = useState<AIHealth[]>([]);
  const [healthLoading, setHealthLoading] = useState(false);

  const loadProfiles = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getAIProfiles();
      setProfiles(data);
    } catch (err: any) {
      message.error(err?.message || t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadHealth = useCallback(async () => {
    setHealthLoading(true);
    try {
      const data = await api.getAIHealth();
      setHealthData(data);
    } catch {
      setHealthData([]);
    } finally {
      setHealthLoading(false);
    }
  }, []);

  useEffect(() => {
    loadProfiles();
    loadHealth();
  }, [loadProfiles, loadHealth]);

  /** 打开新增 Modal */
  const openCreateModal = useCallback(() => {
    setModalMode("create");
    setEditingId(null);
    form.resetFields();
    form.setFieldsValue({
      provider: "openai_compatible",
      auth_type: "bearer",
      timeout_seconds: 30,
      max_tokens: 4096,
      max_context_tokens: 8192,
      daily_request_limit: 100,
      max_concurrent: 3,
      purpose: "all",
      priority: 0,
      is_enabled: true,
      is_fallback: false,
    });
    setModalOpen(true);
  }, [form]);

  /** 打开编辑 Modal */
  const openEditModal = useCallback((profile: AIProfile) => {
    setModalMode("edit");
    setEditingId(profile.id);
    form.setFieldsValue({
      name: profile.name,
      provider: profile.provider,
      base_url: profile.base_url ?? undefined,
      model: profile.model,
      auth_type: profile.auth_type ?? undefined,
      timeout_seconds: profile.timeout_seconds,
      max_tokens: profile.max_tokens,
      max_context_tokens: profile.max_context_tokens,
      daily_request_limit: profile.daily_request_limit,
      max_concurrent: profile.max_concurrent,
      purpose: profile.purpose,
      priority: profile.priority,
      is_enabled: profile.is_enabled,
      is_fallback: profile.is_fallback,
    });
    setModalOpen(true);
  }, [form]);

  /** 提交表单 */
  const handleSubmit = useCallback(async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      const payload: Record<string, unknown> = { ...values };
      // 编辑模式下 secret_value 为空表示不修改
      if (modalMode === "edit" && !payload.secret_value) {
        delete payload.secret_value;
      }
      if (modalMode === "create") {
        await api.createAIProfile(payload);
        message.success(t("save"));
      } else if (editingId !== null) {
        await api.updateAIProfile(editingId, payload);
        message.success(t("save"));
      }
      setModalOpen(false);
      await loadProfiles();
      await loadHealth();
    } catch (err: any) {
      if (err?.errorFields) return;
      message.error(err?.message || t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }, [form, modalMode, editingId, loadProfiles, loadHealth]);

  /** 删除 Profile */
  const handleDelete = useCallback(async (id: number) => {
    try {
      await api.deleteAIProfile(id);
      message.success(t("save"));
      await loadProfiles();
      await loadHealth();
    } catch (err: any) {
      message.error(err?.message || t("saveFailed"));
    }
  }, [loadProfiles, loadHealth]);

  /** 测试连接 */
  const handleTest = useCallback(async (profile: AIProfile) => {
    setTestingId(profile.id);
    try {
      const result = await api.testAIProfile(profile.id);
      setTestResults((prev) => ({ ...prev, [profile.id]: result }));
      if (result.success) {
        message.success(`${t("aiSettings.testSuccess")} (${result.latency_ms}ms)`);
      } else {
        message.error(`${t("aiSettings.testFailed")}: ${result.error || ""}`);
      }
      await loadHealth();
    } catch (err: any) {
      const failResult: AIProfileTestResult = {
        success: false,
        latency_ms: 0,
        model_info: null,
        error: err?.message || t("aiSettings.testFailed"),
      };
      setTestResults((prev) => ({ ...prev, [profile.id]: failResult }));
      message.error(err?.message || t("aiSettings.testFailed"));
    } finally {
      setTestingId(null);
    }
  }, [loadHealth]);

  /** 发现模型 */
  const handleDiscoverModels = useCallback(async (profile: AIProfile) => {
    setModelsModalOpen(true);
    setModelsProfileName(profile.name);
    setModelsLoading(true);
    setDiscoveredModels([]);
    try {
      const resp = await api.discoverAIModels(profile.id);
      setDiscoveredModels(resp.models || []);
    } catch (err: any) {
      message.error(err?.message || t("aiSettings.noModels"));
    } finally {
      setModelsLoading(false);
    }
  }, []);

  /** 查看用量 */
  const handleViewUsage = useCallback(async (profile: AIProfile) => {
    try {
      const usage = await api.getAIProfileUsage(profile.id);
      setUsageData((prev) => ({ ...prev, [profile.id]: usage }));
    } catch {
      // 静默失败
    }
  }, []);

  /** 优先级调整 */
  const handlePriorityChange = useCallback(async (id: number, priority: number) => {
    try {
      await api.updateAIProfile(id, { priority });
      await loadProfiles();
    } catch (err: any) {
      message.error(err?.message || t("saveFailed"));
    }
  }, [loadProfiles]);

  const columns: ColumnsType<AIProfile> = [
    {
      title: t("aiSettings.profileName"),
      dataIndex: "name",
      key: "name",
      width: 140,
      render: (name: string, record) => (
        <Space direction="vertical" size={0}>
          <strong>{name}</strong>
          <Space size={4}>
            {record.is_enabled ? <Tag color="green">ON</Tag> : <Tag>OFF</Tag>}
            {record.is_fallback && <Tag color="purple">{t("aiSettings.isFallback")}</Tag>}
          </Space>
        </Space>
      ),
    },
    {
      title: t("aiSettings.provider"),
      dataIndex: "provider",
      key: "provider",
      width: 120,
      render: (provider: string, record) => (
        <Space direction="vertical" size={0}>
          <span>{provider}</span>
          <Text type="secondary" style={{ fontSize: 11 }}>{record.model}</Text>
        </Space>
      ),
    },
    {
      title: t("aiSettings.priority"),
      dataIndex: "priority",
      key: "priority",
      width: 100,
      render: (priority: number, record) => (
        <InputNumber
          size="small"
          min={0}
          max={1000}
          value={priority}
          onChange={(val) => handlePriorityChange(record.id, val ?? 0)}
          style={{ width: 70 }}
        />
      ),
    },
    {
      title: t("aiSettings.health"),
      key: "health",
      width: 100,
      render: (_: unknown, record) => {
        const tag = healthTag(record.health_status);
        return <Tag color={tag.color}>{tag.label}</Tag>;
      },
    },
    {
      title: t("aiSettings.usage"),
      key: "usage",
      width: 140,
      render: (_: unknown, record) => {
        const usage = usageData[record.id];
        if (!usage) {
          return (
            <Button size="small" type="link" onClick={() => handleViewUsage(record)}>
              {t("aiSettings.usage")}
            </Button>
          );
        }
        const pct = usage.daily_request_limit > 0
          ? Math.round((usage.daily_request_count / usage.daily_request_limit) * 100)
          : 0;
        return (
          <Tooltip title={`${usage.daily_request_count}/${usage.daily_request_limit}`}>
            <Progress percent={pct} size="small" style={{ width: 80 }} />
          </Tooltip>
        );
      },
    },
    {
      title: t("aiSettings.testConnection"),
      key: "actions",
      width: 280,
      render: (_: unknown, record) => (
        <Space size="small" wrap>
          <Button
            size="small"
            icon={<ApiOutlined />}
            loading={testingId === record.id}
            onClick={() => handleTest(record)}
          >
            {t("aiSettings.testConnection")}
          </Button>
          <Button
            size="small"
            icon={<ExperimentOutlined />}
            onClick={() => handleDiscoverModels(record)}
          >
            {t("aiSettings.discoverModels")}
          </Button>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEditModal(record)} />
          <Popconfirm
            title={t("aiSettings.deleteProfileConfirm")}
            onConfirm={() => handleDelete(record.id)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="tab-container" data-tab-content="ai-settings" data-testid="ai-settings-page">
      <div className="settings-layout">
        <div className="settings-content" style={{ padding: 16 }}>
          {/* 健康状态总览 */}
          <Card
            size="small"
            title={
              <Space>
                <HeartOutlined />
                <span>{t("aiSettings.health")}</span>
                <Tooltip title={t("aiSettings.health")}>
                  <QuestionCircleOutlined style={{ color: "#94a3b8" }} />
                </Tooltip>
                <Button size="small" type="link" icon={<ReloadOutlined />} onClick={loadHealth} loading={healthLoading} />
              </Space>
            }
            style={{ marginBottom: 16 }}
            data-testid="ai-health-card"
          >
            {healthLoading ? (
              <div style={{ textAlign: "center", padding: 16 }}><Spin /></div>
            ) : healthData.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("aiSettings.noProfiles")} />
            ) : (
              <List
                size="small"
                dataSource={healthData}
                renderItem={(item) => {
                  const tag = healthTag(item.health_status);
                  const pct = item.daily_request_limit > 0
                    ? Math.round((item.daily_request_count / item.daily_request_limit) * 100)
                    : 0;
                  return (
                    <List.Item>
                      <Space size="large" wrap>
                        <Space>
                          <strong>{item.name}</strong>
                          <Tag color={tag.color}>{tag.label}</Tag>
                        </Space>
                        <Space size={4}>
                          <Text type="secondary" style={{ fontSize: 12 }}>{item.provider}</Text>
                          <Text style={{ fontSize: 12 }}>{item.model}</Text>
                        </Space>
                        <Space size={4}>
                          <Text type="secondary" style={{ fontSize: 12 }}>{t("aiSettings.priority")}:</Text>
                          <Text style={{ fontSize: 12 }}>{item.priority}</Text>
                        </Space>
                        <Tooltip title={`${item.daily_request_count}/${item.daily_request_limit}`}>
                          <Progress percent={pct} size="small" style={{ width: 60 }} />
                        </Tooltip>
                      </Space>
                    </List.Item>
                  );
                }}
              />
            )}
          </Card>

          {/* Profile 列表 */}
          <Card
            size="small"
            title={
              <Space>
                <span>{t("aiSettings.profiles")}</span>
                <Button
                  size="small"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={openCreateModal}
                >
                  {t("aiSettings.addProfile")}
                </Button>
              </Space>
            }
          >
            {loading ? (
              <div style={{ textAlign: "center", padding: 24 }}><Spin /></div>
            ) : (
              <Table
                columns={columns}
                dataSource={profiles}
                rowKey="id"
                size="small"
                pagination={false}
                locale={{ emptyText: <Empty description={t("aiSettings.noProfiles")} /> }}
              />
            )}
          </Card>

          {/* 测试结果展示 */}
          {Object.keys(testResults).length > 0 && (
            <Card size="small" title={t("aiSettings.testConnection")} style={{ marginTop: 16 }}>
              {Object.entries(testResults).map(([id, result]) => {
                const profile = profiles.find((p) => p.id === Number(id));
                return (
                  <Alert
                    key={id}
                    type={result.success ? "success" : "error"}
                    message={`${profile?.name ?? id}: ${result.success ? t("aiSettings.testSuccess") : t("aiSettings.testFailed")}`}
                    description={result.success ? `${t("aiAssistant.latency")}: ${result.latency_ms}ms` : result.error}
                    style={{ marginBottom: 8 }}
                  />
                );
              })}
            </Card>
          )}
        </div>
      </div>

      {/* 新增/编辑 Modal */}
      <Modal
        open={modalOpen}
        title={modalMode === "create" ? t("aiSettings.addProfile") : t("aiSettings.editProfile")}
        onCancel={() => setModalOpen(false)}
        onOk={handleSubmit}
        confirmLoading={saving}
        okText={t("save")}
        cancelText={t("cancel")}
        width={600}
        destroyOnClose
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item
            name="name"
            label={t("aiSettings.profileName")}
            rules={[{ required: true }]}
          >
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item
            name="provider"
            label={t("aiSettings.provider")}
            rules={[{ required: true }]}
          >
            <Select options={PROVIDER_OPTIONS} />
          </Form.Item>
          <Form.Item name="base_url" label={t("aiSettings.baseUrl")}>
            <Input placeholder="https://api.openai.com/v1" />
          </Form.Item>
          <Form.Item
            name="model"
            label={t("aiSettings.model")}
            rules={[{ required: true }]}
          >
            <Input placeholder="gpt-4o-mini" />
          </Form.Item>
          <Form.Item name="auth_type" label={t("aiSettings.authType")}>
            <Select options={AUTH_TYPE_OPTIONS} />
          </Form.Item>
          <Form.Item
            name="secret_value"
            label={t("aiSettings.secretValue")}
            tooltip={t("aiSettings.secretValueHint")}
          >
            <Input.Password placeholder={t("aiSettings.secretValueHint")} />
          </Form.Item>
          <Space wrap>
            <Form.Item name="timeout_seconds" label={t("aiSettings.timeoutSeconds")}>
              <InputNumber min={1} max={600} />
            </Form.Item>
            <Form.Item name="max_tokens" label={t("aiSettings.maxTokens")}>
              <InputNumber min={1} max={32768} />
            </Form.Item>
            <Form.Item name="max_context_tokens" label={t("aiSettings.maxContextTokens")}>
              <InputNumber min={1} max={200000} />
            </Form.Item>
          </Space>
          <Space wrap>
            <Form.Item name="daily_request_limit" label={t("aiSettings.dailyRequestLimit")}>
              <InputNumber min={0} max={100000} />
            </Form.Item>
            <Form.Item name="max_concurrent" label={t("aiSettings.maxConcurrent")}>
              <InputNumber min={1} max={100} />
            </Form.Item>
            <Form.Item name="priority" label={t("aiSettings.priority")}>
              <InputNumber min={0} max={1000} />
            </Form.Item>
            <Form.Item name="purpose" label={t("aiSettings.purpose")}>
              <Select options={PURPOSE_OPTIONS} style={{ width: 120 }} />
            </Form.Item>
          </Space>
          <Space>
            <Form.Item name="is_enabled" label={t("aiSettings.isEnabled")} valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="is_fallback" label={t("aiSettings.isFallback")} valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      {/* 模型发现 Modal */}
      <Modal
        open={modelsModalOpen}
        title={`${t("aiSettings.availableModels")} - ${modelsProfileName}`}
        onCancel={() => setModelsModalOpen(false)}
        footer={null}
        width={500}
      >
        {modelsLoading ? (
          <div style={{ textAlign: "center", padding: 24 }}><Spin /></div>
        ) : discoveredModels.length === 0 ? (
          <Empty description={t("aiSettings.noModels")} />
        ) : (
          <List
            size="small"
            dataSource={discoveredModels}
            renderItem={(model) => (
              <List.Item>
                <Space>
                  <Tag color="blue">{model.id}</Tag>
                  {model.owned_by && <Text type="secondary">{model.owned_by}</Text>}
                </Space>
              </List.Item>
            )}
          />
        )}
      </Modal>
    </div>
  );
}
