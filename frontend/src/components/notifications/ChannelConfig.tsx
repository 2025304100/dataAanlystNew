// WP-MSG.6：渠道配置页签
//
// 数据来源：
// - GET    /api/v1/notifications/channels               列表
// - POST   /api/v1/notifications/channels               创建
// - PATCH  /api/v1/notifications/channels/{id}          更新（启用/禁用/配置）
// - DELETE /api/v1/notifications/channels/{id}          删除
// - POST   /api/v1/notifications/channels/{id}/test     测试发送
//
// 关键约束：
// - 接口失败降级不抛异常，显示错误信息 + 重试按钮
// - 错误消息不暴露敏感信息（后端已脱敏，前端不再二次解析）
// - 未配置或测试失败的渠道在策略页签中不可选（在 PolicyEditor 中实现）
// - 不伪造数据：列表为空显示 Empty，接口失败显示 Alert
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  PlusOutlined,
  ReloadOutlined,
  EditOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { requestJson } from "../../api/client";
import { t } from "../../i18n";

// 通知渠道（对齐 app.models.notification.NotificationChannel）
interface Channel {
  id: number;
  name: string;
  channel_type: string; // in_app/wxpusher/dingtalk/onebot/email/webhook
  enabled: boolean;
  status: string; // unconfigured/pending_test/test_success/test_failed/enabled/disabled
  config_mask_json: string | null;
  verified_at: string | null;
  last_test_at: string | null;
  last_test_success: boolean | null;
  last_error_code: string | null;
  last_error_message: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

// 渠道类型选项
const CHANNEL_TYPE_OPTIONS = [
  { value: "in_app", labelKey: "channelTypeInApp" },
  { value: "wxpusher", labelKey: "channelTypeWxPusher" },
  { value: "dingtalk", labelKey: "channelTypeDingTalk" },
  { value: "onebot", labelKey: "channelTypeOneBot" },
  { value: "email", labelKey: "channelTypeEmail" },
  { value: "webhook", labelKey: "channelTypeWebhook" },
];

// 渠道类型展示标签
const CHANNEL_TYPE_LABELS: Record<string, string> = {
  in_app: "channelTypeInApp",
  wxpusher: "channelTypeWxPusher",
  dingtalk: "channelTypeDingTalk",
  onebot: "channelTypeOneBot",
  email: "channelTypeEmail",
  webhook: "channelTypeWebhook",
};

// 渠道状态展示配置
const CHANNEL_STATUS_CONFIG: Record<string, { color: string; labelKey: string }> = {
  unconfigured: { color: "default", labelKey: "channelStatusUnconfigured" },
  pending_test: { color: "orange", labelKey: "channelStatusPendingTest" },
  test_success: { color: "blue", labelKey: "channelStatusTestSuccess" },
  test_failed: { color: "red", labelKey: "channelStatusTestFailed" },
  enabled: { color: "green", labelKey: "channelStatusEnabled" },
  disabled: { color: "default", labelKey: "channelStatusDisabled" },
};

// 不同渠道类型的配置字段定义（脱敏字段名）
const CHANNEL_CONFIG_FIELDS: Record<
  string,
  Array<{ key: string; labelKey: string; required?: boolean; placeholderKey?: string }>
> = {
  in_app: [],
  wxpusher: [
    { key: "app_token", labelKey: "channelConfigAppToken", required: true, placeholderKey: "channelConfigAppTokenPlaceholder" },
    { key: "uid", labelKey: "channelConfigUid", required: true, placeholderKey: "channelConfigUidPlaceholder" },
  ],
  dingtalk: [
    { key: "webhook", labelKey: "channelConfigWebhookUrl", required: true, placeholderKey: "channelConfigWebhookUrlPlaceholder" },
    { key: "secret", labelKey: "channelConfigSecret", placeholderKey: "channelConfigSecretPlaceholder" },
  ],
  onebot: [
    { key: "base_url", labelKey: "channelConfigBaseUrl", required: true, placeholderKey: "channelConfigBaseUrlPlaceholder" },
    { key: "token", labelKey: "channelConfigToken", required: true, placeholderKey: "channelConfigTokenPlaceholder" },
    { key: "user_id", labelKey: "channelConfigUserId", required: true, placeholderKey: "channelConfigUserIdPlaceholder" },
  ],
  email: [
    { key: "smtp_host", labelKey: "channelConfigSmtpHost", required: true, placeholderKey: "channelConfigSmtpHostPlaceholder" },
    { key: "smtp_port", labelKey: "channelConfigSmtpPort", required: true, placeholderKey: "channelConfigSmtpPortPlaceholder" },
    { key: "username", labelKey: "channelConfigUsername", required: true, placeholderKey: "channelConfigUsernamePlaceholder" },
    { key: "password", labelKey: "channelConfigPassword", required: true, placeholderKey: "channelConfigPasswordPlaceholder" },
    { key: "from_addr", labelKey: "channelConfigFromAddr", required: true, placeholderKey: "channelConfigFromAddrPlaceholder" },
    { key: "to_addrs", labelKey: "channelConfigToAddrs", required: true, placeholderKey: "channelConfigToAddrsPlaceholder" },
  ],
  webhook: [
    { key: "url", labelKey: "channelConfigWebhookUrl", required: true, placeholderKey: "channelConfigWebhookUrlPlaceholder" },
    { key: "secret", labelKey: "channelConfigSecret", placeholderKey: "channelConfigSecretPlaceholder" },
  ],
};

// 解析脱敏配置 JSON
function parseMaskConfig(json: string | null): Record<string, string> {
  if (!json) return {};
  try {
    const parsed = JSON.parse(json);
    if (parsed && typeof parsed === "object") {
      const result: Record<string, string> = {};
      Object.entries(parsed).forEach(([k, v]) => {
        result[k] = v == null ? "" : String(v);
      });
      return result;
    }
  } catch {
    // 解析失败返回空对象，不抛异常
  }
  return {};
}

export const ChannelConfig: React.FC = () => {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [createModalVisible, setCreateModalVisible] = useState(false);
  const [editModalVisible, setEditModalVisible] = useState(false);
  const [editingChannel, setEditingChannel] = useState<Channel | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const [createChannelType, setCreateChannelType] = useState<string>("in_app");

  const fetchChannels = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await requestJson<Channel[]>("/api/v1/notifications/channels");
      setChannels(Array.isArray(data) ? data : []);
    } catch (err: unknown) {
      // 接口失败降级：不抛异常，记录错误信息展示给用户
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("notificationChannelsLoadFailed"));
      setChannels([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchChannels();
  }, [fetchChannels]);

  // 测试渠道：调用 /test 接口，后端会发送一条测试消息
  const handleTest = useCallback(
    async (channelId: number) => {
      try {
        const result = await requestJson<{ success: boolean; error_message?: string; error_code?: string }>(
          `/api/v1/notifications/channels/${channelId}/test`,
          { method: "POST" },
        );
        if (result?.success) {
          message.success(t("channelTestSuccess"));
        } else {
          // 测试失败：展示脱敏错误信息（后端已脱敏）
          const errMsg = result?.error_message || t("channelTestFailed");
          message.error(`${t("channelTestFailed")}: ${errMsg}`);
        }
        fetchChannels();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : "";
        message.error(msg || t("channelTestFailed"));
      }
    },
    [fetchChannels],
  );

  // 启用/禁用渠道
  const handleToggleEnabled = useCallback(
    async (channelId: number, enabled: boolean) => {
      try {
        await requestJson<Channel>(`/api/v1/notifications/channels/${channelId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        });
        message.success(enabled ? t("channelEnabled") : t("channelDisabled"));
        fetchChannels();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : "";
        message.error(msg || t("updateFailed"));
      }
    },
    [fetchChannels],
  );

  // 删除渠道
  const handleDelete = useCallback(
    async (channelId: number) => {
      try {
        await requestJson<{ ok: boolean }>(`/api/v1/notifications/channels/${channelId}`, {
          method: "DELETE",
        });
        message.success(t("channelDeleted"));
        fetchChannels();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : "";
        message.error(msg || t("deleteFailed"));
      }
    },
    [fetchChannels],
  );

  // 打开编辑弹窗
  const handleEdit = useCallback(
    (channel: Channel) => {
      setEditingChannel(channel);
      const config = parseMaskConfig(channel.config_mask_json);
      editForm.setFieldsValue({
        name: channel.name,
        channel_type: channel.channel_type,
        ...config,
      });
      setEditModalVisible(true);
    },
    [editForm],
  );

  // 创建渠道提交
  const handleCreateSubmit = useCallback(async () => {
    try {
      const values = await createForm.validateFields();
      setSubmitting(true);
      // 收集配置字段（除 name/channel_type 之外的字段都作为 config）
      const { name, channel_type, ...rest } = values as Record<string, unknown>;
      const config: Record<string, unknown> = {};
      Object.entries(rest).forEach(([k, v]) => {
        if (v != null && v !== "") config[k] = v;
      });
      await requestJson<Channel>("/api/v1/notifications/channels", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          channel_type,
          config,
        }),
      });
      message.success(t("channelCreated"));
      setCreateModalVisible(false);
      createForm.resetFields();
      setCreateChannelType("in_app");
      fetchChannels();
    } catch (err: unknown) {
      // 表单验证错误：errorFields 结构，忽略提示
      const anyErr = err as { errorFields?: unknown };
      if (anyErr.errorFields) return;
      const msg = err instanceof Error ? err.message : "";
      message.error(msg || t("createFailed"));
    } finally {
      setSubmitting(false);
    }
  }, [createForm, fetchChannels]);

  // 编辑渠道提交
  const handleEditSubmit = useCallback(async () => {
    if (!editingChannel) return;
    try {
      const values = await editForm.validateFields();
      setSubmitting(true);
      const { name, channel_type, ...rest } = values as Record<string, unknown>;
      const config: Record<string, unknown> = {};
      Object.entries(rest).forEach(([k, v]) => {
        if (v != null && v !== "") config[k] = v;
      });
      await requestJson<Channel>(`/api/v1/notifications/channels/${editingChannel.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          channel_type,
          config,
        }),
      });
      message.success(t("channelUpdated"));
      setEditModalVisible(false);
      setEditingChannel(null);
      editForm.resetFields();
      fetchChannels();
    } catch (err: unknown) {
      const anyErr = err as { errorFields?: unknown };
      if (anyErr.errorFields) return;
      const msg = err instanceof Error ? err.message : "";
      message.error(msg || t("updateFailed"));
    } finally {
      setSubmitting(false);
    }
  }, [editingChannel, editForm, fetchChannels]);

  // 渲染渠道配置表单字段
  const renderConfigFields = (channelType: string, form: "create" | "edit") => {
    const fields = CHANNEL_CONFIG_FIELDS[channelType] || [];
    if (fields.length === 0) {
      return (
        <Alert
          type="info"
          showIcon
          message={t("channelConfigNoFields")}
          style={{ marginBottom: 12 }}
        />
      );
    }
    return fields.map((field) => (
      <Form.Item
        key={field.key}
        name={field.key}
        label={t(field.labelKey)}
        rules={field.required ? [{ required: true }] : undefined}
      >
        <Input
          placeholder={field.placeholderKey ? t(field.placeholderKey) : ""}
          autoComplete="off"
        />
      </Form.Item>
    ));
  };

  const columns: ColumnsType<Channel> = [
    {
      title: t("channelColumnName"),
      dataIndex: "name",
      key: "name",
      width: 160,
    },
    {
      title: t("channelColumnType"),
      dataIndex: "channel_type",
      key: "channel_type",
      width: 120,
      render: (type: string) => {
        const labelKey = CHANNEL_TYPE_LABELS[type] || "channelTypeUnknown";
        return <Tag>{t(labelKey)}</Tag>;
      },
    },
    {
      title: t("channelColumnStatus"),
      key: "status",
      width: 160,
      render: (_v: unknown, record: Channel) => {
        const cfg = CHANNEL_STATUS_CONFIG[record.status] || CHANNEL_STATUS_CONFIG.unconfigured;
        return (
          <Space size="small">
            <Tag color={cfg.color}>{t(cfg.labelKey)}</Tag>
            {record.enabled && <Tag color="green">{t("enabled")}</Tag>}
          </Space>
        );
      },
    },
    {
      title: t("channelColumnLastTest"),
      key: "last_test",
      width: 220,
      render: (_v: unknown, record: Channel) => (
        <Space direction="vertical" size={2}>
          {record.last_test_at ? (
            <span style={{ fontSize: 12 }}>
              {new Date(record.last_test_at).toLocaleString()}
            </span>
          ) : (
            <span style={{ fontSize: 12, color: "var(--muted)" }}>-</span>
          )}
          {record.last_test_success === false && record.last_error_message && (
            <span style={{ color: "var(--danger, #ff4d4f)", fontSize: 12 }}>
              {record.last_error_message}
            </span>
          )}
        </Space>
      ),
    },
    {
      title: t("channelColumnActions"),
      key: "actions",
      width: 280,
      render: (_v: unknown, record: Channel) => (
        <Space size="small" wrap>
          {record.channel_type !== "in_app" && (
            <Button
              size="small"
              icon={<ThunderboltOutlined />}
              onClick={() => handleTest(record.id)}
            >
              {t("test")}
            </Button>
          )}
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => handleEdit(record)}
          >
            {t("edit")}
          </Button>
          <Switch
            size="small"
            checked={record.enabled}
            onChange={(checked) => handleToggleEnabled(record.id, checked)}
          />
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
    <div className="notification-channel-config" data-tab-content="channels">
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
          <Button
            icon={<ReloadOutlined />}
            onClick={fetchChannels}
            loading={loading}
          >
            {t("refresh")}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              createForm.resetFields();
              setCreateChannelType("in_app");
              setCreateModalVisible(true);
            }}
          >
            {t("addChannel")}
          </Button>
        </Space>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>
          {t("channelCount")}: {channels.length}
        </span>
      </div>

      {error && (
        <Alert
          type="error"
          showIcon
          message={t("notificationChannelsLoadFailed")}
          description={error}
          action={
            <Button size="small" onClick={fetchChannels}>
              {t("refresh")}
            </Button>
          }
          style={{ marginBottom: 12 }}
        />
      )}

      {loading && channels.length === 0 ? (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin tip={t("channelLoading")} />
        </div>
      ) : channels.length === 0 ? (
        <Empty description={t("channelsEmpty")} />
      ) : (
        <Table<Channel>
          rowKey="id"
          dataSource={channels}
          columns={columns}
          size="small"
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ x: "max-content" }}
          loading={loading}
          locale={{
            emptyText: <Empty description={t("channelsEmpty")} />,
          }}
        />
      )}

      {/* 创建渠道弹窗 */}
      <Modal
        open={createModalVisible}
        title={t("createChannel")}
        onCancel={() => {
          setCreateModalVisible(false);
          createForm.resetFields();
          setCreateChannelType("in_app");
        }}
        onOk={handleCreateSubmit}
        confirmLoading={submitting}
        okText={t("create")}
        cancelText={t("cancel")}
        width={520}
      >
        <Form form={createForm} layout="vertical" preserve={false}>
          <Form.Item
            name="name"
            label={t("channelColumnName")}
            rules={[{ required: true }]}
          >
            <Input placeholder={t("channelColumnNamePlaceholder")} autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="channel_type"
            label={t("channelColumnType")}
            initialValue="in_app"
            rules={[{ required: true }]}
          >
            <Select
              options={CHANNEL_TYPE_OPTIONS.map((opt) => ({
                value: opt.value,
                label: t(opt.labelKey),
              }))}
              onChange={(v) => setCreateChannelType(v)}
            />
          </Form.Item>
          {renderConfigFields(createChannelType, "create")}
        </Form>
      </Modal>

      {/* 编辑渠道弹窗 */}
      <Modal
        open={editModalVisible}
        title={t("editChannel")}
        onCancel={() => {
          setEditModalVisible(false);
          setEditingChannel(null);
          editForm.resetFields();
        }}
        onOk={handleEditSubmit}
        confirmLoading={submitting}
        okText={t("save")}
        cancelText={t("cancel")}
        width={520}
      >
        <Form form={editForm} layout="vertical" preserve={false}>
          <Form.Item
            name="name"
            label={t("channelColumnName")}
            rules={[{ required: true }]}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item name="channel_type" label={t("channelColumnType")}>
            <Select
              disabled
              options={CHANNEL_TYPE_OPTIONS.map((opt) => ({
                value: opt.value,
                label: t(opt.labelKey),
              }))}
            />
          </Form.Item>
          {editingChannel &&
            renderConfigFields(editingChannel.channel_type, "edit")}
          <Alert
            type="info"
            showIcon
            message={t("channelConfigEditHint")}
            style={{ marginTop: 8 }}
          />
        </Form>
      </Modal>
    </div>
  );
};

export default ChannelConfig;
