// WP-MSG.6：发送记录页签
//
// 数据来源：
// - GET    /api/v1/notifications/outbox                  发件箱列表（支持筛选）
// - GET    /api/v1/notifications/outbox/{id}/deliveries  发送记录详情
// - POST   /api/v1/notifications/outbox/{id}/retry       手动重发
// - GET    /api/v1/notifications/channels               渠道列表（用于筛选）
//
// 关键约束：
// - 接口失败降级不抛异常，显示错误信息 + 重试按钮
// - 错误消息不暴露敏感信息（后端已脱敏）
// - 不伪造数据：列表为空显示 Empty，接口失败显示 Alert
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
  Drawer,
  Empty,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  ReloadOutlined,
  SyncOutlined,
  EyeOutlined,
} from "@ant-design/icons";
import type { Dayjs } from "dayjs";
import { requestJson } from "../../api/client";
import { t } from "../../i18n";
import {
  DELIVERY_STATUS_TONE,
  NOTIF,
  NOTIF_LAYOUT,
  OUTBOX_STATUS_TONE,
  semanticTagStyle,
  type OutboxTone,
} from "./notificationTheme";

const { RangePicker } = DatePicker;

// 发件箱记录（对齐 app.models.notification.NotificationOutbox）
interface OutboxItem {
  id: number;
  event_key: string;
  source_type: string;
  source_id: number | null;
  event_type: string;
  severity: string;
  payload_json: string | null;
  channel_id: number;
  policy_id: number | null;
  status: string; // pending/sending/sent/failed/dead_letter
  attempt_count: number;
  max_attempts: number;
  next_retry_at: string | null;
  last_error_code: string | null;
  last_error_message: string | null;
  sent_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// 发送记录（对齐 app.models.notification.NotificationDelivery）
interface DeliveryItem {
  id: number;
  outbox_id: number;
  channel_id: number;
  attempt_number: number;
  status: string; // success/failed/timeout/auth_failed/rate_limited
  status_code: number | null;
  response_summary: string | null;
  error_code: string | null;
  error_message: string | null;
  duration_ms: number | null;
  sent_at: string | null;
  created_at: string | null;
}

// 渠道（简化版，仅用于筛选）
interface ChannelFilterOption {
  id: number;
  name: string;
}

// Outbox 状态展示配置（色板统一走 notificationTheme，禁 antd 预设杂色）
const OUTBOX_STATUS_CONFIG: Record<string, { tone: OutboxTone; labelKey: string }> = {
  pending: { tone: "neutral", labelKey: "outboxStatusPending" },
  sending: { tone: "info", labelKey: "outboxStatusSending" },
  sent: { tone: "success", labelKey: "outboxStatusSent" },
  failed: { tone: "error", labelKey: "outboxStatusFailed" },
  dead_letter: { tone: "critical", labelKey: "outboxStatusDeadLetter" },
};

// Delivery 状态展示配置（同上）
const DELIVERY_STATUS_CONFIG: Record<string, { tone: OutboxTone; labelKey: string }> = {
  success: { tone: "success", labelKey: "deliveryStatusSuccess" },
  failed: { tone: "error", labelKey: "deliveryStatusFailed" },
  timeout: { tone: "warning", labelKey: "deliveryStatusTimeout" },
  auth_failed: { tone: "critical", labelKey: "deliveryStatusAuthFailed" },
  rate_limited: { tone: "warning", labelKey: "deliveryStatusRateLimited" },
};

// 来源类型选项（对齐 app.services.notifications.sources.MESSAGE_SOURCES）
const SOURCE_TYPE_OPTIONS = [
  { value: "system", labelKey: "sourceGroupSystem" },
  { value: "data", labelKey: "sourceGroupData" },
  { value: "factor", labelKey: "sourceGroupFactor" },
  { value: "opportunity", labelKey: "sourceGroupOpportunity" },
  { value: "portfolio", labelKey: "sourceGroupPortfolio" },
  { value: "review", labelKey: "sourceGroupReview" },
];

// 状态选项
const STATUS_OPTIONS = [
  { value: "pending", labelKey: "outboxStatusPending" },
  { value: "sending", labelKey: "outboxStatusSending" },
  { value: "sent", labelKey: "outboxStatusSent" },
  { value: "failed", labelKey: "outboxStatusFailed" },
  { value: "dead_letter", labelKey: "outboxStatusDeadLetter" },
];

export const DeliveryLog: React.FC = () => {
  const [outboxItems, setOutboxItems] = useState<OutboxItem[]>([]);
  const [channels, setChannels] = useState<ChannelFilterOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [channelFilter, setChannelFilter] = useState<number | undefined>(undefined);
  const [sourceFilter, setSourceFilter] = useState<string | undefined>(undefined);
  const [dateRange, setDateRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  // 详情抽屉
  const [detailVisible, setDetailVisible] = useState(false);
  const [detailOutbox, setDetailOutbox] = useState<OutboxItem | null>(null);
  const [deliveries, setDeliveries] = useState<DeliveryItem[]>([]);
  const [deliveriesLoading, setDeliveriesLoading] = useState(false);

  const fetchOutbox = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (statusFilter) params.set("status", statusFilter);
      if (channelFilter != null) params.set("channel_id", String(channelFilter));
      if (sourceFilter) params.set("source_type", sourceFilter);
      if (dateRange && dateRange[0]) params.set("date_from", dateRange[0].format("YYYY-MM-DD"));
      if (dateRange && dateRange[1]) params.set("date_to", dateRange[1].format("YYYY-MM-DD"));
      params.set("limit", "100");
      const url = `/api/v1/notifications/outbox?${params.toString()}`;
      const data = await requestJson<OutboxItem[]>(url);
      setOutboxItems(Array.isArray(data) ? data : []);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("deliveryLoadFailed"));
      setOutboxItems([]);
    } finally {
      setLoading(false);
    }
  }, [statusFilter, channelFilter, sourceFilter, dateRange]);

  // 加载渠道列表（用于筛选）
  const fetchChannels = useCallback(async () => {
    try {
      const data = await requestJson<ChannelFilterOption[]>("/api/v1/notifications/channels");
      setChannels(Array.isArray(data) ? data : []);
    } catch {
      // 渠道加载失败不抛异常
      setChannels([]);
    }
  }, []);

  useEffect(() => {
    fetchOutbox();
    fetchChannels();
  }, [fetchOutbox, fetchChannels]);

  // 手动重发
  const handleRetry = useCallback(
    async (outboxId: number) => {
      try {
        await requestJson<{ ok: boolean }>(`/api/v1/notifications/outbox/${outboxId}/retry`, {
          method: "POST",
        });
        message.success(t("deliveryRetrySubmitted"));
        fetchOutbox();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : "";
        message.error(msg || t("deliveryRetryFailed"));
      }
    },
    [fetchOutbox],
  );

  // 查看详情：加载该 outbox 的所有 delivery 记录
  const handleViewDetail = useCallback(async (record: OutboxItem) => {
    setDetailOutbox(record);
    setDetailVisible(true);
    setDeliveriesLoading(true);
    setDeliveries([]);
    try {
      const data = await requestJson<DeliveryItem[]>(
        `/api/v1/notifications/outbox/${record.id}/deliveries`,
      );
      setDeliveries(Array.isArray(data) ? data : []);
    } catch (err: unknown) {
      // 加载失败不抛异常，仅显示空列表
      setDeliveries([]);
    } finally {
      setDeliveriesLoading(false);
    }
  }, []);

  const columns: ColumnsType<OutboxItem> = [
    {
      title: t("deliverySourceType"),
      dataIndex: "source_type",
      key: "source_type",
      width: 110,
      render: (source: string) => {
        const labelKeyMap: Record<string, string> = {
          system: "sourceGroupSystem",
          data: "sourceGroupData",
          factor: "sourceGroupFactor",
          opportunity: "sourceGroupOpportunity",
          portfolio: "sourceGroupPortfolio",
          review: "sourceGroupReview",
        };
        return <Tag>{t(labelKeyMap[source] || source)}</Tag>;
      },
    },
    {
      title: t("deliveryEventType"),
      dataIndex: "event_type",
      key: "event_type",
      width: 180,
      ellipsis: true,
    },
    {
      title: t("deliveryChannel"),
      dataIndex: "channel_id",
      key: "channel_id",
      width: 120,
      render: (channelId: number) => {
        const ch = channels.find((c) => c.id === channelId);
        return ch ? ch.name : `#${channelId}`;
      },
    },
    {
      title: t("deliveryStatus"),
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (status: string) => {
        const cfg = OUTBOX_STATUS_CONFIG[status] || { tone: "neutral" as OutboxTone, labelKey: "outboxStatusUnknown" };
        return <Tag style={semanticTagStyle(cfg.tone)}>{t(cfg.labelKey)}</Tag>;
      },
    },
    {
      title: t("deliveryAttemptCount"),
      dataIndex: "attempt_count",
      key: "attempt_count",
      width: 100,
      render: (count: number, record: OutboxItem) => `${count}/${record.max_attempts}`,
    },
    {
      title: t("deliverySentAt"),
      dataIndex: "sent_at",
      key: "sent_at",
      width: 160,
      render: (date: string | null) =>
        date ? (
          <span style={{ fontSize: 12 }}>{new Date(date).toLocaleString()}</span>
        ) : (
          <span style={{ color: "var(--muted)" }}>-</span>
        ),
    },
    {
      title: t("channelColumnActions"),
      key: "actions",
      width: 220,
      render: (_v: unknown, record: OutboxItem) => (
        <Space size="small">
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => handleViewDetail(record)}
          >
            {t("deliveryViewDetail")}
          </Button>
          {(record.status === "failed" || record.status === "dead_letter") && (
            <Popconfirm
              title={t("deliveryRetryConfirm")}
              onConfirm={() => handleRetry(record.id)}
            >
              <Button size="small" icon={<SyncOutlined />}>
                {t("deliveryManualRetry")}
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  // Delivery 详情表格列（tableLayout=fixed：固定列收窄，摘要/错误两列吃剩余宽度，禁横向溢出）
  const deliveryColumns: ColumnsType<DeliveryItem> = [
    {
      title: t("deliveryAttemptNumber"),
      dataIndex: "attempt_number",
      key: "attempt_number",
      width: 72,
    },
    {
      title: t("deliveryStatus"),
      dataIndex: "status",
      key: "status",
      width: 92,
      render: (status: string) => {
        const cfg = DELIVERY_STATUS_CONFIG[status] || { tone: "neutral" as OutboxTone, labelKey: "deliveryStatusUnknown" };
        return <Tag style={semanticTagStyle(cfg.tone)}>{t(cfg.labelKey)}</Tag>;
      },
    },
    {
      title: t("deliveryStatusCode"),
      dataIndex: "status_code",
      key: "status_code",
      width: 84,
      render: (code: number | null) => (code != null ? code : "-"),
    },
    {
      title: t("deliveryDuration"),
      dataIndex: "duration_ms",
      key: "duration_ms",
      width: 76,
      render: (ms: number | null) => (ms != null ? `${ms}ms` : "-"),
    },
    {
      title: t("deliveryResponseSummary"),
      dataIndex: "response_summary",
      key: "response_summary",
      ellipsis: true,
      render: (s: string | null) => s || <span style={{ color: "var(--muted)" }}>-</span>,
    },
    {
      title: t("deliveryErrorMessage"),
      key: "error",
      ellipsis: true,
      render: (_v: unknown, record: DeliveryItem) =>
        record.error_message ? (
          <span style={{ color: "#dc2626", fontSize: 12 }}>
            {record.error_message}
          </span>
        ) : (
          <span style={{ color: "var(--muted)" }}>-</span>
        ),
    },
    {
      title: t("deliverySentAt"),
      dataIndex: "sent_at",
      key: "sent_at",
      width: 152,
      render: (date: string | null) =>
        date ? (
          <span style={{ fontSize: 12 }}>{new Date(date).toLocaleString()}</span>
        ) : (
          <span style={{ color: "var(--muted)" }}>-</span>
        ),
    },
  ];

  return (
    <div className="notification-delivery-log" data-tab-content="deliveries">
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
          <Select
            allowClear
            placeholder={t("deliveryFilterStatus")}
            style={{ minWidth: 140 }}
            value={statusFilter}
            onChange={(v) => setStatusFilter(v)}
            options={STATUS_OPTIONS.map((opt) => ({
              value: opt.value,
              label: t(opt.labelKey),
            }))}
          />
          <Select
            allowClear
            placeholder={t("deliveryFilterChannel")}
            style={{ minWidth: 140 }}
            value={channelFilter}
            onChange={(v) => setChannelFilter(v)}
            options={channels.map((ch) => ({
              value: ch.id,
              label: ch.name,
            }))}
          />
          <Select
            allowClear
            placeholder={t("deliveryFilterSource")}
            style={{ minWidth: 140 }}
            value={sourceFilter}
            onChange={(v) => setSourceFilter(v)}
            options={SOURCE_TYPE_OPTIONS.map((opt) => ({
              value: opt.value,
              label: t(opt.labelKey),
            }))}
          />
          <RangePicker
            value={dateRange as [Dayjs, Dayjs] | null}
            onChange={(dates) => {
              if (dates && dates[0] && dates[1]) {
                setDateRange([dates[0], dates[1]]);
              } else {
                setDateRange(null);
              }
            }}
          />
          <Button icon={<ReloadOutlined />} onClick={fetchOutbox} loading={loading}>
            {t("refresh")}
          </Button>
        </Space>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>
          {t("deliveryCount")}: {outboxItems.length}
        </span>
      </div>

      {error && (
        <Alert
          type="error"
          showIcon
          message={t("deliveryLoadFailed")}
          description={error}
          action={
            <Button size="small" onClick={fetchOutbox}>
              {t("refresh")}
            </Button>
          }
          style={{ marginBottom: 12 }}
        />
      )}

      {loading && outboxItems.length === 0 ? (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin tip={t("deliveryLoading")} />
        </div>
      ) : outboxItems.length === 0 ? (
        <Empty description={t("deliveryEmpty")} />
      ) : (
        <Table<OutboxItem>
          rowKey="id"
          dataSource={outboxItems}
          columns={columns}
          size="small"
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ x: "max-content" }}
          loading={loading}
          locale={{ emptyText: <Empty description={t("deliveryEmpty")} /> }}
        />
      )}

      {/* 详情抽屉 */}
      <Drawer
        open={detailVisible}
        title={t("deliveryDetailTitle")}
        onClose={() => {
          setDetailVisible(false);
          setDetailOutbox(null);
          setDeliveries([]);
        }}
        width={1080}
      >
        {detailOutbox && (
          <div>
            <div style={{ marginBottom: 20 }}>
              <div style={{ ...NOTIF_LAYOUT.sectionTitle, marginBottom: 10 }}>
                {t("deliveryOutboxInfo")}
              </div>
              {/* 信息区两列网格：原 5 行竖排占满垂直空间，改 grid 后一屏可看全 */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: "10px 24px",
                  padding: "14px 16px",
                  background: "#f8fafc",
                  border: "1px solid #e2e8f0",
                  borderRadius: 8,
                }}
              >
                <div>
                  <span style={NOTIF_LAYOUT.labelText}>{t("deliveryEventType")}:</span>{" "}
                  <span style={{ fontSize: 13 }}>{detailOutbox.event_type}</span>
                </div>
                <div>
                  <span style={NOTIF_LAYOUT.labelText}>{t("deliverySourceType")}:</span>{" "}
                  <span style={{ fontSize: 13 }}>{detailOutbox.source_type}</span>
                </div>
                <div>
                  <span style={NOTIF_LAYOUT.labelText}>{t("deliveryStatus")}:</span>{" "}
                  <Tag style={semanticTagStyle(OUTBOX_STATUS_TONE[detailOutbox.status] || "neutral")}>
                    {t(OUTBOX_STATUS_CONFIG[detailOutbox.status]?.labelKey || "outboxStatusUnknown")}
                  </Tag>
                </div>
                <div>
                  <span style={NOTIF_LAYOUT.labelText}>{t("deliveryAttemptCount")}:</span>{" "}
                  <span style={{ fontSize: 13 }}>{detailOutbox.attempt_count}/{detailOutbox.max_attempts}</span>
                </div>
                {detailOutbox.sent_at && (
                  <div>
                    <span style={NOTIF_LAYOUT.labelText}>{t("deliverySentAt")}:</span>{" "}
                    <span style={{ fontSize: 13 }}>{new Date(detailOutbox.sent_at).toLocaleString()}</span>
                  </div>
                )}
                {detailOutbox.last_error_message && (
                  <div style={{ gridColumn: "1 / -1" }}>
                    <span style={NOTIF_LAYOUT.labelText}>{t("deliveryLastError")}:</span>{" "}
                    <span style={{ color: NOTIF.error, fontSize: 13 }}>{detailOutbox.last_error_message}</span>
                  </div>
                )}
              </div>
            </div>
            <div>
              <div style={{ ...NOTIF_LAYOUT.sectionTitle, marginBottom: 10 }}>
                {t("deliveryAttemptsTitle")}
              </div>
              <Spin spinning={deliveriesLoading}>
                <Table<DeliveryItem>
                  rowKey="id"
                  dataSource={deliveries}
                  columns={deliveryColumns}
                  size="small"
                  pagination={false}
                  tableLayout="fixed"
                  locale={{
                    emptyText: <Empty description={t("deliveryAttemptsEmpty")} />,
                  }}
                />
              </Spin>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
};

export default DeliveryLog;
