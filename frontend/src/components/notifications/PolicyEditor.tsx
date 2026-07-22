// WP-MSG.6：推送策略页签
//
// 数据来源：
// - GET    /api/v1/notifications/policies               列表
// - POST   /api/v1/notifications/policies               创建
// - PATCH  /api/v1/notifications/policies/{id}          更新
// - DELETE /api/v1/notifications/policies/{id}          删除
// - GET    /api/v1/notifications/channels               渠道列表（用于多选）
// - GET    /api/v1/notifications/templates              模板列表（用于选择）
//
// 关键约束（project_memory 硬约束）：
// - 用户可同时选择多个消息来源和多个渠道
// - 未配置或测试失败的第三方渠道不能被策略选中（禁用 + 提示"去配置"或"立即测试"）
// - 接口失败降级不抛异常
// - 不伪造数据：列表为空显示 Empty，接口失败显示 Alert
//
// 消息来源分组（对齐 app.services.notifications.sources.MESSAGE_SOURCES）：
// - system     系统运行
// - data       数据与接口
// - factor     因子与模型
// - opportunity 机会与观察
// - portfolio  组合与交易
// - review     绩效与复盘
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  PlusOutlined,
  ReloadOutlined,
  EditOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { requestJson } from "../../api/client";
import { t } from "../../i18n";

// 策略（对齐 app.models.notification.NotificationPolicy）
interface Policy {
  id: number;
  name: string;
  enabled: boolean;
  source_types_json: string | null;
  min_severity: string; // info/warn/error/critical
  scope_type: string; // all/portfolio/watchlist/symbol
  scope_ids_json: string | null;
  delivery_mode: string; // instant/digest
  digest_schedule: string | null;
  quiet_hours_json: string | null;
  cooldown_minutes: number;
  dedup_window_minutes: number;
  template_id: number | null;
  channel_ids?: number[];
  created_at?: string | null;
  updated_at?: string | null;
}

// 渠道（简化版，仅用于多选）
interface ChannelOption {
  id: number;
  name: string;
  channel_type: string;
  enabled: boolean;
  status: string;
}

// 模板（简化版，仅用于选择）
interface TemplateOption {
  id: number;
  name: string;
  is_active: boolean;
}

// 严重级别选项
const SEVERITY_OPTIONS = [
  { value: "info", labelKey: "severityInfo" },
  { value: "warn", labelKey: "severityWarn" },
  { value: "error", labelKey: "severityError" },
  { value: "critical", labelKey: "severityCritical" },
];

// 范围类型选项
const SCOPE_OPTIONS = [
  { value: "all", labelKey: "policyScopeAll" },
  { value: "portfolio", labelKey: "policyScopePortfolio" },
  { value: "watchlist", labelKey: "policyScopeWatchlist" },
  { value: "symbol", labelKey: "policyScopeSymbol" },
];

// 推送模式选项
const DELIVERY_MODE_OPTIONS = [
  { value: "instant", labelKey: "policyDeliveryInstant" },
  { value: "digest", labelKey: "policyDeliveryDigest" },
];

// 消息来源分组（对齐 app.services.notifications.sources.MESSAGE_SOURCES）
// 静态定义，不依赖 API
const MESSAGE_SOURCE_GROUPS: Array<{
  group: string;
  labelKey: string;
  events: Array<{ type: string; labelKey: string; default_severity: string }>;
}> = [
  {
    group: "system",
    labelKey: "sourceGroupSystem",
    events: [
      { type: "system_error", labelKey: "sourceEventSystemError", default_severity: "error" },
      { type: "system_warning", labelKey: "sourceEventSystemWarning", default_severity: "warn" },
      { type: "task_complete", labelKey: "sourceEventTaskComplete", default_severity: "info" },
      { type: "data_expired", labelKey: "sourceEventDataExpired", default_severity: "warn" },
      { type: "api_failure", labelKey: "sourceEventApiFailure", default_severity: "error" },
    ],
  },
  {
    group: "data",
    labelKey: "sourceGroupData",
    events: [
      { type: "data_update", labelKey: "sourceEventDataUpdate", default_severity: "info" },
      { type: "data_quality", labelKey: "sourceEventDataQuality", default_severity: "warn" },
      { type: "api_rate_limit", labelKey: "sourceEventApiRateLimit", default_severity: "warn" },
    ],
  },
  {
    group: "factor",
    labelKey: "sourceGroupFactor",
    events: [
      { type: "factor_update", labelKey: "sourceEventFactorUpdate", default_severity: "info" },
      { type: "model_run", labelKey: "sourceEventModelRun", default_severity: "info" },
      { type: "model_failure", labelKey: "sourceEventModelFailure", default_severity: "error" },
    ],
  },
  {
    group: "opportunity",
    labelKey: "sourceGroupOpportunity",
    events: [
      { type: "discovery_new", labelKey: "sourceEventDiscoveryNew", default_severity: "info" },
      { type: "signal_matched", labelKey: "sourceEventSignalMatched", default_severity: "info" },
      { type: "opportunity_expired", labelKey: "sourceEventOpportunityExpired", default_severity: "warn" },
    ],
  },
  {
    group: "portfolio",
    labelKey: "sourceGroupPortfolio",
    events: [
      { type: "trade_executed", labelKey: "sourceEventTradeExecuted", default_severity: "info" },
      { type: "auto_trade_blocked", labelKey: "sourceEventAutoTradeBlocked", default_severity: "warn" },
      { type: "drawdown_warning", labelKey: "sourceEventDrawdownWarning", default_severity: "error" },
      { type: "position_changed", labelKey: "sourceEventPositionChanged", default_severity: "info" },
    ],
  },
  {
    group: "review",
    labelKey: "sourceGroupReview",
    events: [
      { type: "review_complete", labelKey: "sourceEventReviewComplete", default_severity: "info" },
      { type: "performance_alert", labelKey: "sourceEventPerformanceAlert", default_severity: "warn" },
    ],
  },
];

// 渠道类型标签
const CHANNEL_TYPE_LABELS: Record<string, string> = {
  in_app: "channelTypeInApp",
  wxpusher: "channelTypeWxPusher",
  dingtalk: "channelTypeDingTalk",
  onebot: "channelTypeOneBot",
  email: "channelTypeEmail",
  webhook: "channelTypeWebhook",
};

// 判断渠道是否可被策略选中：
// - in_app 渠道：enabled 即可选
// - 第三方渠道：必须 status=test_success 或 status=enabled 且 enabled=true
function isChannelSelectable(channel: ChannelOption): boolean {
  if (channel.channel_type === "in_app") {
    return channel.enabled;
  }
  // 第三方渠道：必须测试成功且启用
  return (
    channel.enabled &&
    (channel.status === "test_success" || channel.status === "enabled")
  );
}

// 解析 JSON 字符串为数组
function parseJsonArray(json: string | null): string[] {
  if (!json) return [];
  try {
    const parsed = JSON.parse(json);
    if (Array.isArray(parsed)) return parsed.map(String);
  } catch {
    // 解析失败返回空数组
  }
  return [];
}

// 解析 quiet_hours_json
interface QuietHours {
  start?: string;
  end?: string;
  timezone?: string;
  bypass_for_critical?: boolean;
}
function parseQuietHours(json: string | null): QuietHours {
  if (!json) return {};
  try {
    const parsed = JSON.parse(json);
    if (parsed && typeof parsed === "object") return parsed as QuietHours;
  } catch {
    // 解析失败返回空对象
  }
  return {};
}

export const PolicyEditor: React.FC = () => {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [channels, setChannels] = useState<ChannelOption[]>([]);
  const [templates, setTemplates] = useState<TemplateOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [createModalVisible, setCreateModalVisible] = useState(false);
  const [editModalVisible, setEditModalVisible] = useState(false);
  const [editingPolicy, setEditingPolicy] = useState<Policy | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();

  const fetchPolicies = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await requestJson<Policy[]>("/api/v1/notifications/policies");
      setPolicies(Array.isArray(data) ? data : []);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("policyLoadFailed"));
      setPolicies([]);
    } finally {
      setLoading(false);
    }
  }, []);

  // 加载渠道列表（用于多选）
  const fetchChannels = useCallback(async () => {
    try {
      const data = await requestJson<ChannelOption[]>("/api/v1/notifications/channels");
      setChannels(Array.isArray(data) ? data : []);
    } catch {
      // 渠道加载失败不抛异常，策略页签仍可用（渠道多选为空）
      setChannels([]);
    }
  }, []);

  // 加载模板列表（用于选择）
  const fetchTemplates = useCallback(async () => {
    try {
      const data = await requestJson<TemplateOption[]>("/api/v1/notifications/templates");
      setTemplates(Array.isArray(data) ? data : []);
    } catch {
      // 模板加载失败不抛异常，策略页签仍可用（模板选择为空）
      setTemplates([]);
    }
  }, []);

  useEffect(() => {
    fetchPolicies();
    fetchChannels();
    fetchTemplates();
  }, [fetchPolicies, fetchChannels, fetchTemplates]);

  // 渠道多选项：可选 + 不可选（禁用 + 提示）
  const channelSelectOptions = useMemo(() => {
    return channels.map((ch) => {
      const selectable = isChannelSelectable(ch);
      const typeLabel = t(CHANNEL_TYPE_LABELS[ch.channel_type] || "channelTypeUnknown");
      let hint = "";
      if (!selectable) {
        if (!ch.enabled) {
          hint = t("policyChannelNotEnabled");
        } else if (ch.status === "unconfigured") {
          hint = t("policyChannelGoConfig");
        } else if (ch.status === "pending_test") {
          hint = t("policyChannelTestNow");
        } else if (ch.status === "test_failed") {
          hint = t("policyChannelTestFailed");
        } else if (ch.status === "disabled") {
          hint = t("policyChannelNotEnabled");
        }
      }
      return {
        value: ch.id,
        label: `${ch.name} (${typeLabel})${selectable ? "" : " - " + hint}`,
        disabled: !selectable,
        selectable,
        hint,
        channel: ch,
      };
    });
  }, [channels]);

  // 消息来源多选项（按分组展开为事件类型）
  const sourceSelectOptions = useMemo(() => {
    return MESSAGE_SOURCE_GROUPS.map((group) => ({
      label: t(group.labelKey),
      title: t(group.labelKey),
      options: group.events.map((ev) => ({
        value: ev.type,
        label: `${t(ev.labelKey)} (${ev.type})`,
      })),
    }));
  }, []);

  // 创建策略
  const handleCreateSubmit = useCallback(async () => {
    try {
      const values = await createForm.validateFields();
      setSubmitting(true);
      const payload = buildPolicyPayload(values);
      await requestJson<Policy>("/api/v1/notifications/policies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      message.success(t("policyCreated"));
      setCreateModalVisible(false);
      createForm.resetFields();
      fetchPolicies();
    } catch (err: unknown) {
      const anyErr = err as { errorFields?: unknown };
      if (anyErr.errorFields) return;
      const msg = err instanceof Error ? err.message : "";
      message.error(msg || t("createFailed"));
    } finally {
      setSubmitting(false);
    }
  }, [createForm, fetchPolicies]);

  // 更新策略
  const handleEditSubmit = useCallback(async () => {
    if (!editingPolicy) return;
    try {
      const values = await editForm.validateFields();
      setSubmitting(true);
      const payload = buildPolicyPayload(values);
      await requestJson<Policy>(`/api/v1/notifications/policies/${editingPolicy.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      message.success(t("policyUpdated"));
      setEditModalVisible(false);
      setEditingPolicy(null);
      editForm.resetFields();
      fetchPolicies();
    } catch (err: unknown) {
      const anyErr = err as { errorFields?: unknown };
      if (anyErr.errorFields) return;
      const msg = err instanceof Error ? err.message : "";
      message.error(msg || t("updateFailed"));
    } finally {
      setSubmitting(false);
    }
  }, [editForm, editingPolicy, fetchPolicies]);

  // 切换启用状态
  const handleToggleEnabled = useCallback(
    async (policyId: number, enabled: boolean) => {
      try {
        await requestJson<Policy>(`/api/v1/notifications/policies/${policyId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        });
        message.success(enabled ? t("policyEnabled") : t("policyDisabled"));
        fetchPolicies();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : "";
        message.error(msg || t("updateFailed"));
      }
    },
    [fetchPolicies],
  );

  // 删除策略
  const handleDelete = useCallback(
    async (policyId: number) => {
      try {
        await requestJson<{ ok: boolean }>(`/api/v1/notifications/policies/${policyId}`, {
          method: "DELETE",
        });
        message.success(t("policyDeleted"));
        fetchPolicies();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : "";
        message.error(msg || t("deleteFailed"));
      }
    },
    [fetchPolicies],
  );

  // 打开编辑弹窗
  const handleEdit = useCallback(
    (policy: Policy) => {
      setEditingPolicy(policy);
      const sourceTypes = parseJsonArray(policy.source_types_json);
      const quietHours = parseQuietHours(policy.quiet_hours_json);
      editForm.setFieldsValue({
        name: policy.name,
        enabled: policy.enabled,
        source_types: sourceTypes,
        min_severity: policy.min_severity,
        scope_type: policy.scope_type,
        delivery_mode: policy.delivery_mode,
        cooldown_minutes: policy.cooldown_minutes,
        dedup_window_minutes: policy.dedup_window_minutes,
        template_id: policy.template_id,
        channel_ids: policy.channel_ids || [],
        quiet_start: quietHours.start,
        quiet_end: quietHours.end,
        quiet_bypass_critical: quietHours.bypass_for_critical ?? false,
      });
      setEditModalVisible(true);
    },
    [editForm],
  );

  const columns: ColumnsType<Policy> = [
    {
      title: t("policyColumnName"),
      dataIndex: "name",
      key: "name",
      width: 160,
    },
    {
      title: t("policyColumnEnabled"),
      key: "enabled",
      width: 90,
      render: (_v: unknown, record: Policy) => (
        <Switch
          size="small"
          checked={record.enabled}
          onChange={(checked) => handleToggleEnabled(record.id, checked)}
        />
      ),
    },
    {
      title: t("policyEditorSourceTypes"),
      key: "source_types",
      width: 200,
      render: (_v: unknown, record: Policy) => {
        const types = parseJsonArray(record.source_types_json);
        if (types.length === 0) {
          return <span style={{ color: "var(--muted)" }}>-</span>;
        }
        return (
          <Space size={[2, 2]} wrap>
            {types.slice(0, 3).map((tp) => (
              <Tag key={tp}>{tp}</Tag>
            ))}
            {types.length > 3 && <Tag>+{types.length - 3}</Tag>}
          </Space>
        );
      },
    },
    {
      title: t("policyEditorMinSeverity"),
      dataIndex: "min_severity",
      key: "min_severity",
      width: 100,
      render: (sev: string) => {
        const colorMap: Record<string, string> = {
          info: "blue",
          warn: "orange",
          error: "red",
          critical: "magenta",
        };
        const labelKeyMap: Record<string, string> = {
          info: "severityInfo",
          warn: "severityWarn",
          error: "severityError",
          critical: "severityCritical",
        };
        return <Tag color={colorMap[sev] || "default"}>{t(labelKeyMap[sev] || sev)}</Tag>;
      },
    },
    {
      title: t("policyEditorChannels"),
      key: "channels",
      width: 180,
      render: (_v: unknown, record: Policy) => {
        const ids = record.channel_ids || [];
        if (ids.length === 0) {
          return <span style={{ color: "var(--muted)" }}>-</span>;
        }
        return (
          <Space size={[2, 2]} wrap>
            {ids.slice(0, 3).map((id) => {
              const ch = channels.find((c) => c.id === id);
              return <Tag key={id}>{ch?.name ?? `#${id}`}</Tag>;
            })}
            {ids.length > 3 && <Tag>+{ids.length - 3}</Tag>}
          </Space>
        );
      },
    },
    {
      title: t("policyColumnActions"),
      key: "actions",
      width: 200,
      render: (_v: unknown, record: Policy) => (
        <Space size="small">
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => handleEdit(record)}
          >
            {t("edit")}
          </Button>
          <Popconfirm
            title={t("deleteConfirm")}
            onConfirm={() => handleDelete(record.id)}
          >
            <Button size="small" danger>
              {t("delete")}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="notification-policy-editor" data-tab-content="policies">
      <div
        style={{
          marginBottom: 12,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: 8,
        }}
      >
        <Space size="small" wrap>
          <Button icon={<ReloadOutlined />} onClick={fetchPolicies} loading={loading}>
            {t("refresh")}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              createForm.resetFields();
              setCreateModalVisible(true);
            }}
          >
            {t("addPolicy")}
          </Button>
        </Space>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>
          {t("policyCount")}: {policies.length}
        </span>
      </div>

      {/* 渠道不可选提示 */}
      {channels.some((ch) => !isChannelSelectable(ch)) && channels.length > 0 && (
        <Alert
          type="info"
          showIcon
          message={t("policyChannelNotSelectableHint")}
          style={{ marginBottom: 12 }}
        />
      )}

      {error && (
        <Alert
          type="error"
          showIcon
          message={t("policyLoadFailed")}
          description={error}
          action={
            <Button size="small" onClick={fetchPolicies}>
              {t("refresh")}
            </Button>
          }
          style={{ marginBottom: 12 }}
        />
      )}

      {loading && policies.length === 0 ? (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin tip={t("policyLoading")} />
        </div>
      ) : policies.length === 0 ? (
        <Empty description={t("policiesEmpty")} />
      ) : (
        <Table<Policy>
          rowKey="id"
          dataSource={policies}
          columns={columns}
          size="small"
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ x: "max-content" }}
          loading={loading}
          locale={{ emptyText: <Empty description={t("policiesEmpty")} /> }}
        />
      )}

      {/* 创建策略弹窗 */}
      <Modal
        open={createModalVisible}
        title={t("createPolicy")}
        onCancel={() => {
          setCreateModalVisible(false);
          createForm.resetFields();
        }}
        onOk={handleCreateSubmit}
        confirmLoading={submitting}
        okText={t("create")}
        cancelText={t("cancel")}
        width={640}
      >
        <PolicyFormFields
          form={createForm}
          channelSelectOptions={channelSelectOptions}
          sourceSelectOptions={sourceSelectOptions}
          templates={templates}
        />
      </Modal>

      {/* 编辑策略弹窗 */}
      <Modal
        open={editModalVisible}
        title={t("editPolicy")}
        onCancel={() => {
          setEditModalVisible(false);
          setEditingPolicy(null);
          editForm.resetFields();
        }}
        onOk={handleEditSubmit}
        confirmLoading={submitting}
        okText={t("save")}
        cancelText={t("cancel")}
        width={640}
      >
        <PolicyFormFields
          form={editForm}
          channelSelectOptions={channelSelectOptions}
          sourceSelectOptions={sourceSelectOptions}
          templates={templates}
        />
      </Modal>
    </div>
  );
};

// 构建策略提交 payload
function buildPolicyPayload(values: Record<string, unknown>): Record<string, unknown> {
  const sourceTypes = Array.isArray(values.source_types) ? values.source_types : [];
  const channelIds = Array.isArray(values.channel_ids) ? values.channel_ids : [];
  const quietHours: QuietHours = {};
  if (values.quiet_start) quietHours.start = String(values.quiet_start);
  if (values.quiet_end) quietHours.end = String(values.quiet_end);
  quietHours.bypass_for_critical = Boolean(values.quiet_bypass_critical);
  return {
    name: values.name,
    enabled: Boolean(values.enabled ?? false),
    source_types: sourceTypes,
    min_severity: values.min_severity || "info",
    scope_type: values.scope_type || "all",
    delivery_mode: values.delivery_mode || "instant",
    cooldown_minutes: Number(values.cooldown_minutes ?? 0),
    dedup_window_minutes: Number(values.dedup_window_minutes ?? 0),
    template_id: values.template_id ?? null,
    channel_ids: channelIds,
    quiet_hours: Object.keys(quietHours).length > 0 ? quietHours : null,
  };
}

// 策略表单字段（创建/编辑共用）
interface PolicyFormFieldsProps {
  form: ReturnType<typeof Form.useForm>[0];
  channelSelectOptions: Array<{
    value: number;
    label: string;
    disabled: boolean;
    selectable: boolean;
    hint: string;
    channel: ChannelOption;
  }>;
  sourceSelectOptions: Array<{
    label: string;
    title: string;
    options: Array<{ value: string; label: string }>;
  }>;
  templates: TemplateOption[];
}

const PolicyFormFields: React.FC<PolicyFormFieldsProps> = ({
  form,
  channelSelectOptions,
  sourceSelectOptions,
  templates,
}) => {
  return (
    <Form form={form} layout="vertical" preserve={false} initialValues={{ enabled: false, min_severity: "info", scope_type: "all", delivery_mode: "instant", cooldown_minutes: 0, dedup_window_minutes: 0 }}>
      <Form.Item name="name" label={t("policyColumnName")} rules={[{ required: true }]}>
        <Input placeholder={t("policyColumnNamePlaceholder")} autoComplete="off" />
      </Form.Item>
      <Form.Item name="enabled" label={t("policyColumnEnabled")} valuePropName="checked">
        <Switch />
      </Form.Item>
      <Form.Item
        name="source_types"
        label={t("policyEditorSourceTypes")}
        tooltip={t("policySourceTypesTooltip")}
        rules={[{ required: true, message: t("policySourceTypesRequired") }]}
      >
        <Select
          mode="multiple"
          placeholder={t("policySourceTypesPlaceholder")}
          options={sourceSelectOptions}
          showSearch
          optionFilterProp="label"
        />
      </Form.Item>
      <Form.Item name="min_severity" label={t("policyEditorMinSeverity")}>
        <Select options={SEVERITY_OPTIONS.map((s) => ({ value: s.value, label: t(s.labelKey) }))} />
      </Form.Item>
      <Form.Item
        name="channel_ids"
        label={t("policyEditorChannels")}
        tooltip={t("policyChannelsTooltip")}
        rules={[{ required: true, message: t("policyChannelsRequired") }]}
      >
        <Select
          mode="multiple"
          placeholder={t("policyChannelsPlaceholder")}
          showSearch
          optionFilterProp="label"
        >
          {channelSelectOptions.map((opt) => (
            <Select.Option
              key={opt.value}
              value={opt.value}
              disabled={opt.disabled}
            >
              <Space size="small">
                <span>{opt.label}</span>
                {!opt.selectable && (
                  <Tooltip title={opt.hint}>
                    <span style={{ fontSize: 11, color: "var(--warning, #faad14)" }}>
                      <SettingOutlined /> {opt.hint}
                    </span>
                  </Tooltip>
                )}
              </Space>
            </Select.Option>
          ))}
        </Select>
      </Form.Item>
      <Form.Item name="scope_type" label={t("policyScopeType")}>
        <Select options={SCOPE_OPTIONS.map((s) => ({ value: s.value, label: t(s.labelKey) }))} />
      </Form.Item>
      <Form.Item name="delivery_mode" label={t("policyDeliveryMode")}>
        <Select options={DELIVERY_MODE_OPTIONS.map((s) => ({ value: s.value, label: t(s.labelKey) }))} />
      </Form.Item>
      <Form.Item name="template_id" label={t("policyEditorTemplate")}>
        <Select
          allowClear
          placeholder={t("policyTemplatePlaceholder")}
          options={templates.map((tp) => ({
            value: tp.id,
            label: `${tp.name}${tp.is_active ? " ✓" : ""}`,
          }))}
        />
      </Form.Item>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Form.Item name="quiet_start" label={t("policyEditorQuietHoursStart")}>
          <Input placeholder="22:00" autoComplete="off" />
        </Form.Item>
        <Form.Item name="quiet_end" label={t("policyEditorQuietHoursEnd")}>
          <Input placeholder="08:00" autoComplete="off" />
        </Form.Item>
      </div>
      <Form.Item name="quiet_bypass_critical" label={t("policyQuietBypassCritical")} valuePropName="checked">
        <Switch />
      </Form.Item>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Form.Item name="cooldown_minutes" label={t("policyEditorCooldown")}>
          <InputNumber min={0} max={1440} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="dedup_window_minutes" label={t("policyEditorDedupWindow")}>
          <InputNumber min={0} max={1440} style={{ width: "100%" }} />
        </Form.Item>
      </div>
    </Form>
  );
};

export default PolicyEditor;
