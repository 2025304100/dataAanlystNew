import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  InputNumber,
  Modal,
  Progress,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExclamationCircleOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import ReactECharts from "echarts-for-react";
import { t } from "../../i18n";
import { api, type ShadowObservationRead, type ShadowObservationSummary, type ShadowHealthReport, type ActivationResult } from "../../api/client";

const { Text } = Typography;

/** 健康状态颜色映射。 */
function healthColor(status: string | null | undefined): string {
  if (!status) return "default";
  const map: Record<string, string> = {
    healthy: "green",
    degraded: "orange",
    blocked: "red",
  };
  return map[status] || "default";
}

function healthLabel(status: string | null | undefined): string {
  if (!status) return "-";
  const key = `shadowLabHealth_${status}`;
  const translated = t(key);
  return translated === key ? status : translated;
}

/** 告警严重度颜色。 */
function alertSeverityColor(severity: string): string {
  const map: Record<string, string> = {
    warn: "warning",
    critical: "error",
  };
  return map[severity] || "info";
}

function alertSeverityIcon(severity: string) {
  if (severity === "critical") return <CloseCircleOutlined style={{ color: "#ff4d4f" }} />;
  if (severity === "warn") return <ExclamationCircleOutlined style={{ color: "#faad14" }} />;
  return <CheckCircleOutlined style={{ color: "#52c41a" }} />;
}

function formatNumber(value: unknown, digits = 4): string {
  if (value == null || value === "") return "-";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "-";
  if (Math.abs(num) >= 1000) return num.toFixed(2);
  return num.toFixed(digits);
}

function formatPercent(value: unknown, digits = 2): string {
  if (value == null || value === "") return "-";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "-";
  return `${(num * 100).toFixed(digits)}%`;
}

/** 审批操作类型。 */
type ApprovalAction = "request" | "approve" | "reject" | "quarantine";

interface ApprovalModalState {
  open: boolean;
  action: ApprovalAction | null;
  reason: string;
  factorId: number | null;
  factorVersionId: number | null;
}

const DEFAULT_MODAL_STATE: ApprovalModalState = {
  open: false,
  action: null,
  reason: "",
  factorId: null,
  factorVersionId: null,
};

export default function FactorShadowLab() {
  const { message } = App.useApp();
  const [factorVersionId, setFactorVersionId] = useState<number | null>(null);
  const [factorId, setFactorId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [observations, setObservations] = useState<ShadowObservationRead[]>([]);
  const [summary, setSummary] = useState<ShadowObservationSummary | null>(null);
  const [health, setHealth] = useState<ShadowHealthReport | null>(null);
  const [modal, setModal] = useState<ApprovalModalState>(DEFAULT_MODAL_STATE);

  const loadAll = useCallback(
    async (fvId: number) => {
      setLoading(true);
      try {
        const [obs, summ, hlh] = await Promise.all([
          api.listShadowObservations(fvId, { limit: 200 }),
          api.getShadowObservationSummary(fvId),
          api.getShadowHealth(fvId),
        ]);
        setObservations(obs);
        setSummary(summ);
        setHealth(hlh);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        message.error(`${t("shadowLabLoadFailed")}: ${msg}`);
        setObservations([]);
        setSummary(null);
        setHealth(null);
      } finally {
        setLoading(false);
      }
    },
    [message],
  );

  // 首次加载不自动拉取，等用户输入 factorVersionId
  useEffect(() => {
    if (factorVersionId != null && factorVersionId > 0) {
      void loadAll(factorVersionId);
    }
  }, [factorVersionId, loadAll]);

  // 审批 Modal 操作
  const openModal = useCallback((action: ApprovalAction) => {
    setModal({
      open: true,
      action,
      reason: "",
      factorId,
      factorVersionId,
    });
  }, [factorId, factorVersionId]);

  const closeModal = useCallback(() => {
    setModal(DEFAULT_MODAL_STATE);
  }, []);

  const handleSubmitApproval = useCallback(async () => {
    if (!modal.action || !modal.factorVersionId) return;
    if (!modal.reason.trim()) {
      message.warning(t("shadowLabReasonRequired"));
      return;
    }
    setSubmitting(true);
    try {
      let result: ActivationResult;
      switch (modal.action) {
        case "request":
          result = await api.requestActivation({
            factor_id: modal.factorId ?? 0,
            factor_version_id: modal.factorVersionId,
            reason: modal.reason,
          });
          break;
        case "approve":
          result = await api.approveActivation({
            factor_id: modal.factorId ?? 0,
            factor_version_id: modal.factorVersionId,
            reason: modal.reason,
          });
          break;
        case "reject":
          result = await api.rejectActivation({
            factor_id: modal.factorId ?? 0,
            reason: modal.reason,
          });
          break;
        case "quarantine":
          result = await api.quarantineFactor(modal.factorId ?? 0, {
            factor_version_id: modal.factorVersionId,
            reason: modal.reason,
          });
          break;
        default:
          return;
      }
      if (result.success) {
        const successKey = `shadowLab_${modal.action}Success` as const;
        message.success(t(successKey));
        closeModal();
        // 重新加载数据
        if (factorVersionId != null) {
          await loadAll(factorVersionId);
        }
      } else {
        message.error(`${t("shadowLabOperationFailed")}: ${result.error ?? "unknown"}`);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(`${t("shadowLabOperationFailed")}: ${msg}`);
    } finally {
      setSubmitting(false);
    }
  }, [modal, factorVersionId, message, closeModal, loadAll]);

  // IC 曲线 option
  const icChartOption = useMemo(() => {
    const dates = observations.map((o) => o.trade_date);
    const icValues = observations.map((o) => o.ic_value);
    const covValues = observations.map((o) => o.coverage);
    return {
      tooltip: { trigger: "axis" },
      legend: { data: ["IC", t("shadowLabCoverage")] },
      grid: { left: 50, right: 20, top: 40, bottom: 30 },
      xAxis: { type: "category", data: dates, axisLabel: { rotate: 30 } },
      yAxis: [
        { type: "value", name: "IC", position: "left" },
        { type: "value", name: t("shadowLabCoverage"), position: "right", min: 0, max: 1 },
      ],
      series: [
        {
          name: "IC",
          type: "line",
          data: icValues,
          smooth: true,
          itemStyle: { color: "#1677ff" },
          connectNulls: true,
        },
        {
          name: t("shadowLabCoverage"),
          type: "line",
          yAxisIndex: 1,
          data: covValues,
          smooth: true,
          itemStyle: { color: "#52c41a" },
          connectNulls: true,
        },
      ],
    };
  }, [observations]);

  // 观察记录表格列
  const observationColumns = useMemo(
    () => [
      {
        title: t("shadowLabTradeDate"),
        dataIndex: "trade_date",
        key: "trade_date",
        width: 110,
      },
      {
        title: t("shadowLabValidDay"),
        dataIndex: "is_valid_day",
        key: "is_valid_day",
        width: 80,
        render: (valid: boolean, record: ShadowObservationRead) =>
          valid ? (
            <Tag color="green">{t("shadowLabYes")}</Tag>
          ) : (
            <Tooltip title={record.invalid_reason || ""}>
              <Tag color="red">{t("shadowLabNo")}</Tag>
            </Tooltip>
          ),
      },
      {
        title: "IC",
        dataIndex: "ic_value",
        key: "ic_value",
        width: 90,
        render: (v: number | null) => formatNumber(v),
      },
      {
        title: t("shadowLabCoverage"),
        dataIndex: "coverage",
        key: "coverage",
        width: 90,
        render: (v: number | null) => formatPercent(v),
      },
      {
        title: t("shadowLabTurnover"),
        dataIndex: "turnover",
        key: "turnover",
        width: 90,
        render: (v: number | null) => formatPercent(v),
      },
      {
        title: t("shadowLabCompleteness"),
        dataIndex: "completeness_ratio",
        key: "completeness_ratio",
        width: 100,
        render: (v: number | null) => formatPercent(v),
      },
      {
        title: t("shadowLabHealthStatus"),
        dataIndex: "health_status",
        key: "health_status",
        width: 90,
        render: (v: string) => <Tag color={healthColor(v)}>{healthLabel(v)}</Tag>,
      },
    ],
    [],
  );

  const validDays = summary?.valid_days ?? 0;
  const minRequired = summary?.min_required_days ?? 20;
  const progressPct = minRequired > 0 ? Math.min(100, (validDays / minRequired) * 100) : 0;

  return (
    <div className="factor-shadow-lab">
      <div style={{ marginBottom: 12 }}>
        <h3>{t("shadowLabTitle")}</h3>
        <Text type="secondary">{t("shadowLabSubtitle")}</Text>
      </div>

      {/* 顶部：因子版本输入 + 刷新 */}
      <Card size="small" style={{ marginBottom: 12 }}>
        <Space wrap>
          <span>{t("shadowLabFactorId")}:</span>
          <InputNumber
            placeholder="如 1"
            value={factorId ?? undefined}
            onChange={(v) => setFactorId(v ?? null)}
            style={{ width: 100 }}
            min={1}
          />
          <span>{t("shadowLabFactorVersionId")}:</span>
          <InputNumber
            placeholder="如 1"
            value={factorVersionId ?? undefined}
            onChange={(v) => setFactorVersionId(v ?? null)}
            style={{ width: 100 }}
            min={1}
          />
          <Button
            icon={<ReloadOutlined />}
            loading={loading}
            disabled={!factorVersionId}
            onClick={() => factorVersionId && loadAll(factorVersionId)}
          >
            {t("shadowLabRefresh")}
          </Button>
        </Space>
      </Card>

      <Spin spinning={loading}>
        {!factorVersionId ? (
          <Empty description={t("shadowLabInputHint")} />
        ) : (
          <>
            {/* 观察期汇总 + 健康状态 */}
            <Space direction="vertical" style={{ width: "100%", marginBottom: 12 }}>
              <Card size="small" title={t("shadowLabObservationSummary")} extra={
                summary?.is_complete ? (
                  <Tag color="green" icon={<CheckCircleOutlined />}>{t("shadowLabGateComplete")}</Tag>
                ) : (
                  <Tag color="orange" icon={<ExclamationCircleOutlined />}>{t("shadowLabGateIncomplete")}</Tag>
                )
              }>
                <Descriptions size="small" column={3}>
                  <Descriptions.Item label={t("shadowLabValidDays")}>
                    <Text strong>{validDays}</Text> / {minRequired}
                  </Descriptions.Item>
                  <Descriptions.Item label={t("shadowLabHealthStatus")}>
                    <Tag color={healthColor(health?.health_status)}>
                      {healthLabel(health?.health_status)}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label={t("shadowLabTotalDays")}>
                    {health?.n_total_days ?? 0}
                  </Descriptions.Item>
                </Descriptions>
                <Progress percent={progressPct} status={summary?.is_complete ? "success" : "active"} style={{ marginTop: 8 }} />
                {summary?.reason ? (
                  <Text type="secondary" style={{ fontSize: 12 }}>{summary.reason}</Text>
                ) : null}
              </Card>

              {/* 健康告警 */}
              {health && health.alerts.length > 0 ? (
                <Card size="small" title={t("shadowLabAlerts")}>
                  <Space direction="vertical" style={{ width: "100%" }}>
                    {health.alerts.map((alert, idx) => (
                      <Alert
                        key={idx}
                        type={alertSeverityColor(alert.severity) as "warning" | "error" | "info"}
                        icon={alertSeverityIcon(alert.severity)}
                        message={alert.message}
                        showIcon
                      />
                    ))}
                  </Space>
                  {health.should_quarantine ? (
                    <Alert
                      type="error"
                      message={t("shadowLabQuarantineRecommended")}
                      style={{ marginTop: 8 }}
                      showIcon
                    />
                  ) : null}
                </Card>
              ) : (
                health ? (
                  <Card size="small" title={t("shadowLabAlerts")}>
                    <Empty description={t("shadowLabNoAlerts")} image={Empty.PRESENTED_IMAGE_SIMPLE} />
                  </Card>
                ) : null
              )}
            </Space>

            {/* IC / 覆盖率曲线 */}
            {observations.length > 0 ? (
              <Card size="small" title={t("shadowLabIcCurve")} style={{ marginBottom: 12 }}>
                <ReactECharts option={icChartOption} style={{ height: 260 }} />
              </Card>
            ) : null}

            {/* 观察记录表 */}
            <Card size="small" title={t("shadowLabObservations")} style={{ marginBottom: 12 }}>
              <Table<ShadowObservationRead>
                dataSource={observations}
                columns={observationColumns}
                rowKey="id"
                size="small"
                pagination={{ pageSize: 10, showSizeChanger: false }}
                scroll={{ x: 660 }}
                locale={{ emptyText: t("shadowLabEmptyObservations") }}
              />
            </Card>

            {/* 审批操作区 */}
            <Card size="small" title={t("shadowLabApproval")} extra={<SafetyCertificateOutlined />}>
              <Space wrap>
                <Button
                  type="primary"
                  onClick={() => openModal("request")}
                  disabled={!factorVersionId}
                >
                  {t("shadowLabRequestActivation")}
                </Button>
                <Button
                  type="primary"
                  danger
                  onClick={() => openModal("approve")}
                  disabled={!factorVersionId}
                >
                  {t("shadowLabApprove")}
                </Button>
                <Button
                  onClick={() => openModal("reject")}
                  disabled={!factorId}
                >
                  {t("shadowLabReject")}
                </Button>
                <Button
                  danger
                  onClick={() => openModal("quarantine")}
                  disabled={!factorId}
                >
                  {t("shadowLabQuarantine")}
                </Button>
              </Space>
            </Card>
          </>
        )}
      </Spin>

      {/* 审批 Modal */}
      <Modal
        open={modal.open}
        title={t(`shadowLab_${modal.action ?? "request"}` as const)}
        onCancel={closeModal}
        confirmLoading={submitting}
        onOk={handleSubmitApproval}
        okText={t("shadowLabConfirm")}
        cancelText={t("shadowLabCancel")}
      >
        <div style={{ marginBottom: 8 }}>
          <Text type="secondary">{t("shadowLabReason")}</Text>
        </div>
        <Input.TextArea
          value={modal.reason}
          onChange={(e) => setModal((prev) => ({ ...prev, reason: e.target.value }))}
          placeholder={t("shadowLabReasonPlaceholder")}
          rows={3}
          maxLength={500}
          showCount
          autoFocus
        />
      </Modal>
    </div>
  );
}
