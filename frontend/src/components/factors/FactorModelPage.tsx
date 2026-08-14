/**
 * WP7-06: 因子模型页。
 *
 * 功能：
 * - 查看当前 runtime 状态（weight_mode / active_model_run_id / fallback_reason）
 * - 查看 FactorSet 列表与成员详情
 * - 查看候选模型列表（含状态、门禁拒绝原因、增强门禁指标）
 * - 激活模型（shadow/ridge）与回退手工权重
 * - 查看审计日志
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  Modal,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  ReloadOutlined,
  ThunderboltOutlined,
  RollbackOutlined,
} from "@ant-design/icons";
import { t } from "../../i18n";
import {
  api,
  type FactorRuntime,
  type FactorModelRun,
  type FactorSet,
} from "../../api/client";

const { Text, Paragraph } = Typography;

/** 模型状态颜色映射。 */
function modelStatusColor(status: string): string {
  const map: Record<string, string> = {
    validated: "green",
    rejected: "red",
    training: "blue",
  };
  return map[status] || "default";
}

/** 权重模式颜色映射。 */
function weightModeColor(mode: string): string {
  const map: Record<string, string> = {
    manual: "default",
    shadow: "orange",
    ridge: "green",
  };
  return map[mode] || "default";
}

/** FactorSet 状态颜色映射。 */
function factorSetStatusColor(status: string): string {
  const map: Record<string, string> = {
    draft: "default",
    frozen: "blue",
    deprecated: "red",
  };
  return map[status] || "default";
}

function factorSetDisplayName(name: string | null | undefined, id: string): string {
  const normalized = name?.trim() || "";
  return normalized && !/^[?\uFFFD\s]+$/.test(normalized) ? normalized : id;
}

function formatNumber(value: unknown, digits = 4): string {
  if (value == null || value === "") return "-";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "-";
  return num.toFixed(digits);
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  return value.replace("T", " ").slice(0, 19);
}

interface AuditEntry {
  id: number;
  action: string;
  model_run_id: string | null;
  previous_mode: string | null;
  new_mode: string | null;
  previous_model_run_id: string | null;
  new_model_run_id: string | null;
  actor: string;
  note: string | null;
  created_at: string | null;
}

export default function FactorModelPage() {
  const { message, modal } = App.useApp();
  const [loading, setLoading] = useState(false);
  const [runtime, setRuntime] = useState<FactorRuntime | null>(null);
  const [models, setModels] = useState<FactorModelRun[]>([]);
  const [factorSets, setFactorSets] = useState<FactorSet[]>([]);
  const [selectedModel, setSelectedModel] = useState<FactorModelRun | null>(null);
  const [auditLogs, setAuditLogs] = useState<AuditEntry[]>([]);

  // 激活 Modal
  const [activateModalOpen, setActivateModalOpen] = useState(false);
  const [activateTarget, setActivateTarget] = useState<FactorModelRun | null>(null);
  const [activateMode, setActivateMode] = useState<"shadow" | "ridge">("shadow");
  const [activateNote, setActivateNote] = useState("");
  const [activating, setActivating] = useState(false);

  // 回退 Modal
  const [fallbackModalOpen, setFallbackModalOpen] = useState(false);
  const [fallbackReason, setFallbackReason] = useState("");
  const [fallingBack, setFallingBack] = useState(false);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [modelList, fsList] = await Promise.all([
        api.getFactorModels(undefined, 20),
        api.listFactorSets(undefined, 50),
      ]);
      setRuntime(modelList.runtime);
      setModels(modelList.items);
      setFactorSets(fsList);
    } catch (err) {
      message.error(t("factorModelLoadFailed") + ": " + String(err));
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // 加载模型详情（含审计日志）
  const loadModelDetail = useCallback(
    async (modelRunId: string) => {
      try {
        const detail = await api.getFactorModel(modelRunId);
        setSelectedModel(detail);
        setAuditLogs((detail.audit ?? []) as unknown as AuditEntry[]);
      } catch (err) {
        message.error(t("factorModelLoadFailed") + ": " + String(err));
      }
    },
    [message]
  );

  // 打开激活 Modal
  const openActivateModal = (model: FactorModelRun, mode: "shadow" | "ridge") => {
    setActivateTarget(model);
    setActivateMode(mode);
    setActivateNote("");
    setActivateModalOpen(true);
  };

  // 确认激活
  const handleActivate = async () => {
    if (!activateTarget) return;
    setActivating(true);
    try {
      const newRuntime = await api.activateFactorModel(
        activateTarget.id,
        activateMode,
        activateNote || undefined
      );
      setRuntime(newRuntime);
      message.success(t("factorModelActivated"));
      setActivateModalOpen(false);
      await loadData();
    } catch (err) {
      message.error(t("factorModelActionFailed") + ": " + String(err));
    } finally {
      setActivating(false);
    }
  };

  // 确认回退
  const handleFallback = async () => {
    if (!fallbackReason.trim()) {
      message.warning(t("factorModelReasonRequired"));
      return;
    }
    setFallingBack(true);
    try {
      const newRuntime = await api.fallbackFactorModel(fallbackReason.trim());
      setRuntime(newRuntime);
      message.success(t("factorModelFallbackDone"));
      setFallbackModalOpen(false);
      setFallbackReason("");
      await loadData();
    } catch (err) {
      message.error(t("factorModelActionFailed") + ": " + String(err));
    } finally {
      setFallingBack(false);
    }
  };

  // 模型列表列定义
  const modelColumns = [
    {
      title: t("factorModelModelId"),
      dataIndex: "id",
      key: "id",
      width: 180,
      render: (id: string, record: FactorModelRun) => (
        <Button
          type="link"
          size="small"
          style={{ padding: 0 }}
          onClick={() => loadModelDetail(id)}
        >
          {id.length > 20 ? id.slice(0, 20) + "..." : id}
        </Button>
      ),
    },
    {
      title: t("factorModelStatus"),
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (status: string) => (
        <Tag color={modelStatusColor(status)}>
          {status === "validated"
            ? t("factorModelValidated")
            : status === "rejected"
            ? t("factorModelRejected")
            : status}
        </Tag>
      ),
    },
    {
      title: t("factorModelValidationIc"),
      key: "validation_ic",
      width: 110,
      render: (_: unknown, record: FactorModelRun) =>
        formatNumber(record.metrics?.validation_ic),
    },
    {
      title: t("factorModelSamples"),
      key: "sample_count",
      width: 90,
      render: (_: unknown, record: FactorModelRun) => record.sample_count,
    },
    {
      title: t("factorModelCutoff"),
      dataIndex: "data_cutoff_at",
      key: "data_cutoff_at",
      width: 160,
      render: (v: string | null) => formatDateTime(v),
    },
    {
      title: "FactorSet",
      key: "factor_set_id",
      width: 140,
      render: (_: unknown, record: FactorModelRun) => {
        const fsId = record.hyperparameters?.factor_set_id as string | undefined;
        if (!fsId) return <Text type="secondary">-</Text>;
        const fs = factorSets.find((s) => s.id === fsId);
        return (
          <Tooltip title={fsId}>
            <Tag color="blue">{fs ? factorSetDisplayName(fs.name, fs.id) : fsId}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: t("factorModelActions"),
      key: "actions",
      width: 200,
      render: (_: unknown, record: FactorModelRun) => {
        if (record.status !== "validated") {
          return (
            <Tooltip title={record.rejection_reason || t("factorModelRejected")}>
              <Tag color="red">{t("factorModelRejected")}</Tag>
            </Tooltip>
          );
        }
        return (
          <Space size="small">
            <Button
              size="small"
              type="primary"
              ghost
              onClick={() => openActivateModal(record, "shadow")}
            >
              {t("factorModelShadow")}
            </Button>
            <Button
              size="small"
              type="primary"
              onClick={() => openActivateModal(record, "ridge")}
            >
              {t("factorModelRidge")}
            </Button>
          </Space>
        );
      },
    },
  ];

  // FactorSet 列表列定义
  const factorSetColumns = [
    {
      title: t("factorModelFactorSetName"),
      dataIndex: "name",
      key: "name",
      render: (name: string, record: FactorSet) => (
        <Space direction="vertical" size={0}>
          <Text strong>{factorSetDisplayName(name, record.id)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {record.id}
          </Text>
        </Space>
      ),
    },
    {
      title: t("factorModelFactorSetStatus"),
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (status: string) => (
        <Tag color={factorSetStatusColor(status)}>{status}</Tag>
      ),
    },
    {
      title: t("factorModelFactorSetMembers"),
      dataIndex: "n_members",
      key: "n_members",
      width: 90,
    },
    {
      title: t("factorModelCutoff"),
      dataIndex: "frozen_at",
      key: "frozen_at",
      width: 160,
      render: (v: string | null) => formatDateTime(v),
    },
  ];

  // FactorSet 成员展开行
  const factorSetExpandable = {
    expandedRowRender: (record: FactorSet) => {
      const memberColumns = [
        {
          title: t("factorModelFactorCode"),
          dataIndex: "factor_code",
          key: "factor_code",
        },
        {
          title: t("factorModelFactorVersion"),
          dataIndex: "factor_version",
          key: "factor_version",
          width: 80,
        },
        {
          title: t("factorModelFactorRole"),
          dataIndex: "role",
          key: "role",
          width: 90,
          render: (role: string) => <Tag>{role}</Tag>,
        },
        {
          title: t("factorModelFactorMissingPolicy"),
          dataIndex: "missing_policy",
          key: "missing_policy",
          width: 120,
        },
      ];
      return (
        <Table
          size="small"
          columns={memberColumns}
          dataSource={record.members}
          rowKey="id"
          pagination={false}
        />
      );
    },
    rowExpandable: (record: FactorSet) => record.members.length > 0,
  };

  // 审计日志列定义
  const auditColumns = [
    {
      title: t("factorModelAuditAction"),
      dataIndex: "action",
      key: "action",
      width: 100,
      render: (action: string) => (
        <Tag color={action === "activate" ? "green" : action === "fallback" ? "orange" : "default"}>
          {action}
        </Tag>
      ),
    },
    {
      title: t("factorModelAuditMode"),
      key: "mode",
      width: 140,
      render: (_: unknown, record: AuditEntry) => (
        <Space size={4}>
          {record.previous_mode && <Tag>{record.previous_mode}</Tag>}
          {record.previous_mode && <Text type="secondary">→</Text>}
          <Tag color={record.new_mode === "ridge" ? "green" : record.new_mode === "shadow" ? "orange" : "default"}>
            {record.new_mode ?? "-"}
          </Tag>
        </Space>
      ),
    },
    {
      title: t("factorModelAuditActor"),
      dataIndex: "actor",
      key: "actor",
      width: 120,
    },
    {
      title: t("factorModelAuditNote"),
      dataIndex: "note",
      key: "note",
      render: (note: string | null) =>
        note ? <Text type="secondary">{note}</Text> : <Text type="secondary">-</Text>,
    },
    {
      title: t("factorModelCutoff"),
      dataIndex: "created_at",
      key: "created_at",
      width: 160,
      render: (v: string | null) => formatDateTime(v),
    },
  ];

  return (
    <div className="factor-model-page">
      <Spin spinning={loading}>
        {/* 当前 runtime 状态 */}
        <Card
          size="small"
          title={t("factorModelRuntime")}
          extra={
            <Space>
              <Button
                size="small"
                icon={<RollbackOutlined />}
                onClick={() => {
                  setFallbackReason("");
                  setFallbackModalOpen(true);
                }}
                disabled={runtime?.weight_mode === "manual"}
              >
                {t("factorModelFallback")}
              </Button>
              <Button size="small" icon={<ReloadOutlined />} onClick={loadData}>
                {t("refresh")}
              </Button>
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          {runtime ? (
            <Descriptions size="small" column={3}>
              <Descriptions.Item label={t("factorModelRuntimeMode")}>
                <Tag color={weightModeColor(runtime.weight_mode)}>
                  {runtime.weight_mode}
                </Tag>
                {runtime.score_weight_mode === "ridge" && (
                  <Tag color="green">{t("factorModelScoreWeightMode")}</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelActiveModel")}>
                {runtime.active_model_run_id ? (
                  <Button
                    type="link"
                    size="small"
                    style={{ padding: 0 }}
                    onClick={() => loadModelDetail(runtime.active_model_run_id!)}
                  >
                    {runtime.active_model_run_id.slice(0, 24) + "..."}
                  </Button>
                ) : (
                  <Text type="secondary">-</Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelFallbackReason")}>
                {runtime.fallback_reason ? (
                  <Text type="warning">{runtime.fallback_reason}</Text>
                ) : (
                  <Text type="secondary">-</Text>
                )}
              </Descriptions.Item>
            </Descriptions>
          ) : (
            <Empty description={t("factorModelNoRuntime")} />
          )}
        </Card>

        {/* FactorSet 列表 */}
        <Card
          size="small"
          title={t("factorModelFactorSets")}
          style={{ marginBottom: 16 }}
        >
          {factorSets.length > 0 ? (
            <Table
              size="small"
              columns={factorSetColumns}
              dataSource={factorSets}
              rowKey="id"
              expandable={factorSetExpandable}
              pagination={{ pageSize: 5, size: "small" }}
            />
          ) : (
            <Empty description={t("factorModelNoFactorSets")} />
          )}
        </Card>

        {/* 候选模型列表 */}
        <Card
          size="small"
          title={t("factorModelModels")}
          style={{ marginBottom: 16 }}
        >
          {models.length > 0 ? (
            <Table
              size="small"
              columns={modelColumns}
              dataSource={models}
              rowKey="id"
              pagination={{ pageSize: 10, size: "small" }}
            />
          ) : (
            <Empty description={t("factorModelNoModels")} />
          )}
        </Card>

        {/* 选中模型详情 + 审计日志 */}
        {selectedModel && (
          <Card
            size="small"
            title={`${t("factorModelDetail")}: ${selectedModel.id}`}
            extra={
              <Button size="small" onClick={() => setSelectedModel(null)}>
                {t("close")}
              </Button>
            }
          >
            <Descriptions size="small" column={3} bordered>
              <Descriptions.Item label={t("factorModelStatus")}>
                <Tag color={modelStatusColor(selectedModel.status)}>
                  {selectedModel.status}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelValidationIc")}>
                {formatNumber(selectedModel.metrics?.validation_ic)}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelSamples")}>
                {selectedModel.sample_count}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelFactorSet")}>
                {(selectedModel.hyperparameters?.factor_set_id as string) ?? "-"}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelTrainRange")}>
                {selectedModel.train_start_date ?? "-"} ~ {selectedModel.train_end_date ?? "-"}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelValidationRange")}>
                {selectedModel.validation_start_date ?? "-"} ~ {selectedModel.validation_end_date ?? "-"}
              </Descriptions.Item>
            </Descriptions>

            {/* 权重快照 */}
            {selectedModel.weights.length > 0 && (
              <Paragraph style={{ marginTop: 12 }}>
                <Text strong>{t("factorModelWeights")}</Text>
              </Paragraph>
            )}
            <Table
              size="small"
              columns={[
                {
                  title: t("factorModelFactorCode"),
                  dataIndex: "factor_code",
                  key: "factor_code",
                },
                {
                  title: t("factorModelFactorVersion"),
                  dataIndex: "factor_version",
                  key: "factor_version",
                  width: 80,
                },
                {
                  title: t("factorModelCoefficient"),
                  dataIndex: "coefficient",
                  key: "coefficient",
                  render: (v: number) => formatNumber(v),
                },
                {
                  title: t("factorModelNormalizedWeight"),
                  dataIndex: "normalized_weight",
                  key: "normalized_weight",
                  render: (v: number) => formatNumber(v),
                },
                {
                  title: t("factorModelValidationIc"),
                  dataIndex: "validation_ic",
                  key: "validation_ic",
                  render: (v: number | null) => formatNumber(v),
                },
              ]}
              dataSource={selectedModel.weights}
              rowKey={(w) => w.factor_code}
              pagination={false}
              style={{ marginBottom: 16 }}
            />

            {/* 拒绝原因 */}
            {selectedModel.rejection_reason && (
              <Alert
                type="error"
                showIcon
                message={t("factorModelRejectionReason")}
                description={selectedModel.rejection_reason}
                style={{ marginBottom: 16 }}
              />
            )}

            {/* 审计日志 */}
            {auditLogs.length > 0 && (
              <>
                <Paragraph>
                  <Text strong>{t("factorModelAuditLogs")}</Text>
                </Paragraph>
                <Table
                  size="small"
                  columns={auditColumns}
                  dataSource={auditLogs}
                  rowKey="id"
                  pagination={{ pageSize: 5, size: "small" }}
                />
              </>
            )}
          </Card>
        )}
      </Spin>

      {/* 激活确认 Modal */}
      <Modal
        title={
          <Space>
            <ThunderboltOutlined />
            {t("factorModelActivateTitle")}
          </Space>
        }
        open={activateModalOpen}
        onOk={handleActivate}
        onCancel={() => setActivateModalOpen(false)}
        confirmLoading={activating}
        okText={t("factorTransitionOk")}
        cancelText={t("cancel")}
      >
        {activateTarget && (
          <div>
            <Paragraph>
              <Text>{t("factorModelActivateConfirm")}</Text>
            </Paragraph>
            <Descriptions size="small" column={1}>
              <Descriptions.Item label={t("factorModelModelId")}>
                {activateTarget.id}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelRuntimeMode")}>
                <Tag color={weightModeColor(activateMode)}>{activateMode}</Tag>
              </Descriptions.Item>
            </Descriptions>
            <Input.TextArea
              value={activateNote}
              onChange={(e) => setActivateNote(e.target.value)}
              placeholder={t("factorModelActivateNotePlaceholder")}
              rows={3}
              maxLength={500}
              style={{ marginTop: 8 }}
            />
          </div>
        )}
      </Modal>

      {/* 回退 Modal */}
      <Modal
        title={
          <Space>
            <RollbackOutlined />
            {t("factorModelFallbackTitle")}
          </Space>
        }
        open={fallbackModalOpen}
        onOk={handleFallback}
        onCancel={() => setFallbackModalOpen(false)}
        confirmLoading={fallingBack}
        okText={t("factorModelConfirmFallback")}
        cancelText={t("cancel")}
      >
        <Paragraph>
          <Text>{t("factorModelFallbackHint")}</Text>
        </Paragraph>
        <Input.TextArea
          value={fallbackReason}
          onChange={(e) => setFallbackReason(e.target.value)}
          placeholder={t("factorModelFallbackReason")}
          rows={3}
          maxLength={1000}
        />
      </Modal>
    </div>
  );
}
