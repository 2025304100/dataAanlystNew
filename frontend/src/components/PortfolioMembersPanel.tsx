// WP4.5：组合成员正式页面
// 显示成员状态、是否持仓、执行模式、来源、最近信号等富信息
//
// 数据来源：
// - GET /api/v1/portfolios/{id}/members 列表（支持 status/include_archived 筛选）
// - POST /api/v1/portfolios/{id}/members 创建
// - POST /api/v1/portfolios/{id}/members/{member_id}/archive 归档
// - POST /api/v1/portfolios/{id}/members/{member_id}/pause 暂停（仅停止买入，卖出规则继续）
// - POST /api/v1/portfolios/{id}/members/{member_id}/restore 恢复
//
// 关键约束：
// - 无持仓成员可以存在且账户权益不变化（前端不阻止创建无持仓成员）
// - 接口失败降级不抛异常，显示错误信息 + 重试按钮
// - 错误消息不暴露敏感信息
// - 归档持仓冲突（409）时提示"仅停止买入"或先卖出后再归档
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
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  PlusOutlined,
  ReloadOutlined,
  PauseCircleOutlined,
  InboxOutlined,
  RollbackOutlined,
  QuestionCircleOutlined,
  ArrowRightOutlined,
} from "@ant-design/icons";
import { useApp } from "../context/AppContext";
import { requestJson } from "../api/client";
import { t } from "../i18n";
import { navigateToResearch } from "../utils/sourceContext";

// 组合成员富读模型（对齐 app.schemas.portfolio_member.PortfolioMemberRead）
interface PortfolioMember {
  id: number;
  portfolio_id: number;
  symbol_id: number;
  status: string; // active/paused/archived
  execution_mode: string; // manual/confirm/auto
  source_type: string; // legacy_position/candidate/observation/manual
  source_id: number | null;
  entry_rule_version_id: number | null;
  exit_rule_version_id: number | null;
  effective_from: string | null;
  effective_to: string | null;
  manual_lock: boolean;
  priority: number;
  note: string | null;
  created_at: string | null;
  updated_at: string | null;
  // WP4-FIX：最近信号摘要（联表 SimOrder 填充）
  latest_signal?: string | null;
  latest_signal_at?: string | null;
  latest_signal_action?: string | null; // buy/sell/hold
}

// 带持仓信息的扩展类型（后端在未来版本可附加，前端兼容处理）
interface MemberWithPosition extends PortfolioMember {
  has_position?: boolean;
  position_quantity?: number;
  symbol?: string;
}

export interface PortfolioMembersPanelProps {
  portfolioId?: number;
}

// 状态筛选可选项
const STATUS_OPTIONS = [
  { value: "active", label: "statusActive" },
  { value: "paused", label: "statusPaused" },
  { value: "archived", label: "statusArchived" },
];

// 判断是否为"成员存在持仓"冲突错误（HTTP 409）
function isMemberHasPositionError(err: unknown): boolean {
  if (err instanceof Error) {
    const anyErr = err as Error & { status?: number; detail?: unknown };
    if (anyErr.status === 409) return true;
    const detail = anyErr.detail;
    if (detail && typeof detail === "object") {
      const detailObj = detail as Record<string, unknown>;
      return (
        detailObj.error === "member_has_position" ||
        (detailObj.quantity != null && detailObj.symbol_id != null)
      );
    }
  }
  return false;
}

export const PortfolioMembersPanel: React.FC<PortfolioMembersPanelProps> = ({ portfolioId }) => {
  const ctx = useApp();
  const [members, setMembers] = useState<MemberWithPosition[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [createModalVisible, setCreateModalVisible] = useState(false);
  const [createForm] = Form.useForm();

  const activePortfolioId = portfolioId ?? ctx.portfolioId ?? undefined;

  const fetchMembers = useCallback(async () => {
    if (!activePortfolioId) {
      setMembers([]);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (statusFilter) params.set("status", statusFilter);
      params.set("include_archived", "true");
      params.set("limit", "200");
      const url = `/api/v1/portfolios/${activePortfolioId}/members?${params.toString()}`;
      const data = await requestJson<PortfolioMember[]>(url);
      setMembers((data ?? []) as MemberWithPosition[]);
    } catch (err) {
      // 错误消息不暴露敏感信息，仅展示通用错误
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("portfolioMembersLoadFailed"));
    } finally {
      setLoading(false);
    }
  }, [activePortfolioId, statusFilter]);

  useEffect(() => {
    fetchMembers();
  }, [fetchMembers]);

  // 归档成员：持仓冲突时提示"仅停止买入"或先卖出
  const handleArchive = useCallback(
    async (memberId: number) => {
      if (!activePortfolioId) return;
      try {
        await requestJson<PortfolioMember>(
          `/api/v1/portfolios/${activePortfolioId}/members/${memberId}/archive`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ force: false }),
          },
        );
        message.success(t("portfolioMemberArchived"));
        fetchMembers();
      } catch (err: unknown) {
        if (isMemberHasPositionError(err)) {
          // 持仓冲突：提示选择"仅停止买入"或先卖出
          Modal.confirm({
            title: t("memberHasPositionTitle"),
            content: t("memberHasPositionDesc"),
            okText: t("pauseBuyOnly"),
            cancelText: t("cancel"),
            onOk: async () => {
              try {
                await requestJson<PortfolioMember>(
                  `/api/v1/portfolios/${activePortfolioId}/members/${memberId}/pause`,
                  { method: "POST" },
                );
                message.success(t("memberPaused"));
                fetchMembers();
              } catch (pauseErr: unknown) {
                const pauseMsg = pauseErr instanceof Error ? pauseErr.message : "";
                message.error(pauseMsg || t("pauseFailed"));
              }
            },
          });
        } else {
          const msg = err instanceof Error ? err.message : "";
          message.error(msg || t("archiveFailed"));
        }
      }
    },
    [activePortfolioId, fetchMembers],
  );

  // 暂停成员（仅停止买入，卖出规则继续）
  const handlePause = useCallback(
    async (memberId: number) => {
      if (!activePortfolioId) return;
      try {
        await requestJson<PortfolioMember>(
          `/api/v1/portfolios/${activePortfolioId}/members/${memberId}/pause`,
          { method: "POST" },
        );
        message.success(t("memberPaused"));
        fetchMembers();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : "";
        message.error(msg || t("pauseFailed"));
      }
    },
    [activePortfolioId, fetchMembers],
  );

  // 恢复已归档的成员
  const handleRestore = useCallback(
    async (memberId: number) => {
      if (!activePortfolioId) return;
      try {
        await requestJson<PortfolioMember>(
          `/api/v1/portfolios/${activePortfolioId}/members/${memberId}/restore`,
          { method: "POST" },
        );
        message.success(t("memberRestored"));
        fetchMembers();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : "";
        message.error(msg || t("restoreFailed"));
      }
    },
    [activePortfolioId, fetchMembers],
  );

  // 创建成员（无持仓成员可以存在，前端不阻止）
  const handleCreate = useCallback(async () => {
    if (!activePortfolioId) return;
    try {
      const values = await createForm.validateFields();
      await requestJson<PortfolioMember>(
        `/api/v1/portfolios/${activePortfolioId}/members`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            symbol_id: Number(values.symbol_id),
            status: values.status || "active",
            execution_mode: values.execution_mode || "manual",
            source_type: values.source_type || "manual",
            priority: Number(values.priority ?? 0),
            note: values.note || null,
          }),
        },
      );
      message.success(t("memberCreated"));
      setCreateModalVisible(false);
      createForm.resetFields();
      fetchMembers();
    } catch (err: unknown) {
      // 表单验证错误：validateFields 抛出 errorFields 结构，直接忽略提示
      const anyErr = err as { errorFields?: unknown };
      if (anyErr.errorFields) return;
      const msg = err instanceof Error ? err.message : "";
      message.error(msg || t("createFailed"));
    }
  }, [activePortfolioId, createForm, fetchMembers]);

  const columns: ColumnsType<MemberWithPosition> = [
    {
      title: t("portfolioMemberColumnSymbol"),
      key: "symbol",
      width: 160,
      render: (_v: unknown, record: MemberWithPosition) => (
        <Space size="small">
          <span>{record.symbol ?? `#${record.symbol_id}`}</span>
          {record.has_position && (
            <Tag color="blue">{t("hasPosition")}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t("portfolioMemberColumnStatus"),
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (status: string) => {
        const color = status === "active" ? "green" : status === "paused" ? "orange" : "default";
        const text =
          status === "active"
            ? t("statusActive")
            : status === "paused"
              ? t("statusPaused")
              : t("statusArchived");
        return <Tag color={color}>{text}</Tag>;
      },
    },
    {
      title: t("portfolioMemberColumnExecutionMode"),
      dataIndex: "execution_mode",
      key: "execution_mode",
      width: 110,
      render: (mode: string) => {
        const text =
          mode === "manual"
            ? t("modeManual")
            : mode === "confirm"
              ? t("modeConfirm")
              : mode === "auto"
                ? t("modeAuto")
                : mode;
        return <span>{text}</span>;
      },
    },
    {
      title: t("portfolioMemberColumnSource"),
      dataIndex: "source_type",
      key: "source_type",
      width: 110,
      render: (source: string) => {
        const text =
          source === "legacy_position"
            ? t("sourceLegacy")
            : source === "candidate"
              ? t("sourceCandidate")
              : source === "observation"
                ? t("sourceObservation")
                : source === "manual"
                  ? t("portfolioMemberSourceManual")
                  : source;
        return <Tag>{text}</Tag>;
      },
    },
    {
      title: (
        <Tooltip title={t("portfolioMemberLatestSignalTooltip")}>
          <Space size={4}>
            <QuestionCircleOutlined />
            <span>{t("portfolioMemberColumnLatestSignal")}</span>
          </Space>
        </Tooltip>
      ),
      key: "latest_signal",
      width: 160,
      render: (_v: unknown, record: MemberWithPosition) => {
        const action = record.latest_signal_action;
        // 无信号：显示 "-"
        if (!action && !record.latest_signal) {
          return <span>-</span>;
        }
        // 由 action + latest_signal_at 构造 i18n 友好的显示文本，
        // 缺失 action 时回退到后端预格式化的 latest_signal
        const actionText =
          action === "buy"
            ? t("portfolioMemberSignalBuy")
            : action === "sell"
              ? t("portfolioMemberSignalSell")
              : action === "hold"
                ? t("portfolioMemberSignalHold")
                : record.latest_signal ?? "-";
        const dateStr = record.latest_signal_at
          ? new Date(record.latest_signal_at).toLocaleDateString()
          : "";
        const text = dateStr ? `${actionText} ${dateStr}` : actionText;
        const color =
          action === "buy" ? "green" : action === "sell" ? "red" : "default";
        return <Tag color={color}>{text}</Tag>;
      },
    },
    {
      title: t("portfolioMemberColumnPriority"),
      dataIndex: "priority",
      key: "priority",
      width: 80,
      render: (v: number) => v ?? 0,
    },
    {
      title: t("portfolioMemberColumnEffectiveFrom"),
      dataIndex: "effective_from",
      key: "effective_from",
      width: 130,
      render: (date: string | null) =>
        date ? new Date(date).toLocaleDateString() : "-",
    },
    {
      title: t("portfolioMemberColumnActions"),
      key: "actions",
      width: 280,
      render: (_v: unknown, record: MemberWithPosition) => (
        <Space size="small">
          {/* WP5.3：组合成员入口跳转，携带 portfolio_id 与 portfolio_member 来源 */}
          <Button
            size="small"
            type="link"
            icon={<ArrowRightOutlined />}
            onClick={() => {
              navigateToResearch(
                ctx,
                {
                  symbol_id: record.symbol_id,
                  source_type: "portfolio_member",
                  source_id: record.id,
                  portfolio_id: record.portfolio_id,
                  return_to: "portfolio",
                },
                {
                  returnState: {
                    statusFilter,
                    portfolioId: activePortfolioId,
                  },
                },
              );
            }}
          >
            {t("portfolioMemberEnterResearch")}
          </Button>
          {record.status === "active" && (
            <Button
              size="small"
              icon={<PauseCircleOutlined />}
              onClick={() => handlePause(record.id)}
            >
              {t("pause")}
            </Button>
          )}
          {record.status !== "archived" && (
            <Popconfirm
              title={t("archiveConfirm")}
              onConfirm={() => handleArchive(record.id)}
            >
              <Button size="small" icon={<InboxOutlined />}>
                {t("archive")}
              </Button>
            </Popconfirm>
          )}
          {record.status === "archived" && (
            <Button
              size="small"
              icon={<RollbackOutlined />}
              onClick={() => handleRestore(record.id)}
            >
              {t("restore")}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  if (!activePortfolioId) {
    return (
      <div className="portfolio-members-panel" data-empty="no-portfolio">
        <Empty description={t("portfolioMembersNoPortfolio")} />
      </div>
    );
  }

  return (
    <div className="portfolio-members-panel" data-opportunity-tab="members">
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
            placeholder={t("portfolioMemberFilterStatus")}
            style={{ minWidth: 140 }}
            value={statusFilter}
            onChange={(v) => setStatusFilter(v)}
            options={STATUS_OPTIONS.map((opt) => ({
              value: opt.value,
              label: t(opt.label),
            }))}
          />
          <Button
            icon={<ReloadOutlined />}
            onClick={fetchMembers}
            loading={loading}
          >
            {t("refresh")}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreateModalVisible(true)}
          >
            {t("addMember")}
          </Button>
        </Space>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>
          {t("opportunityObservationCount")}: {members.length}
        </span>
      </div>

      {error && (
        <Alert
          type="error"
          showIcon
          message={t("portfolioMembersLoadFailed")}
          description={error}
          action={
            <Button size="small" onClick={fetchMembers}>
              {t("refresh")}
            </Button>
          }
          style={{ marginBottom: 12 }}
        />
      )}

      {loading && members.length === 0 ? (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin tip={t("portfolioMembersLoading")} />
        </div>
      ) : members.length === 0 ? (
        <Empty description={t("portfolioMembersEmpty")} />
      ) : (
        <Table<MemberWithPosition>
          rowKey="id"
          dataSource={members}
          columns={columns}
          size="small"
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ x: "max-content" }}
          loading={loading}
          locale={{
            emptyText: <Empty description={t("portfolioMembersEmpty")} />,
          }}
        />
      )}

      <Modal
        open={createModalVisible}
        title={t("createMember")}
        onCancel={() => setCreateModalVisible(false)}
        onOk={handleCreate}
        okText={t("createMember")}
        cancelText={t("cancel")}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="symbol_id"
            label={t("symbolId")}
            rules={[{ required: true }]}
          >
            <Input type="number" />
          </Form.Item>
          <Form.Item name="status" label={t("status")} initialValue="active">
            <Select>
              <Select.Option value="active">{t("statusActive")}</Select.Option>
              <Select.Option value="paused">{t("statusPaused")}</Select.Option>
            </Select>
          </Form.Item>
          <Form.Item
            name="execution_mode"
            label={t("executionMode")}
            initialValue="manual"
          >
            <Select>
              <Select.Option value="manual">{t("modeManual")}</Select.Option>
              <Select.Option value="confirm">{t("modeConfirm")}</Select.Option>
              <Select.Option value="auto">{t("modeAuto")}</Select.Option>
            </Select>
          </Form.Item>
          <Form.Item
            name="source_type"
            label={t("sourceType")}
            initialValue="manual"
          >
            <Select>
              <Select.Option value="manual">{t("portfolioMemberSourceManual")}</Select.Option>
              <Select.Option value="candidate">{t("sourceCandidate")}</Select.Option>
              <Select.Option value="observation">{t("sourceObservation")}</Select.Option>
            </Select>
          </Form.Item>
          <Form.Item name="priority" label={t("priority")} initialValue={0}>
            <Input type="number" />
          </Form.Item>
          <Form.Item name="note" label={t("note")}>
            <Input.TextArea />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default PortfolioMembersPanel;
