import { useState, useEffect, useCallback } from "react";
import { useApp } from "../../context/AppContext";
import { t, factorLabel, factorCategoryLabel, factorDirectionLabel } from "../../i18n";
import {
  api,
  requestJson,
  type FactorDefinition,
  type FactorReferenceInfo,
  type FactorTransitionAudit,
} from "../../api/client";
import {
  Card,
  Table,
  Tag,
  Button,
  Timeline,
  Modal,
  Input,
  Alert,
  Spin,
  Descriptions,
  App,
  Space,
  Empty,
  Typography,
} from "antd";

type FactorDetailProps = {
  factorCode: string;
  onOpenEditor: (code: string) => void;
  onBack: () => void;
};

// 版本列表端点返回结构（与 FactorVersionDefinition 略有差异：无 id、无 validation_status，
// is_latest 为布尔值）。该端点目前未在 client.ts 中封装，故直接使用 requestJson 调用。
type FactorVersionListItem = {
  factor_code: string;
  version: number;
  formula_expr: string;
  params: Record<string, unknown>;
  direction: string;
  source_mapping: Record<string, unknown>;
  effective_from: string | null;
  change_note: string;
  is_latest: boolean;
  created_at: string | null;
};

type TransitionModalState = {
  open: boolean;
  action: string;
  label: string;
  danger?: boolean;
  reason: string;
};

const INITIAL_MODAL: TransitionModalState = {
  open: false,
  action: "",
  label: "",
  reason: "",
};

export default function FactorDetail({ factorCode, onOpenEditor, onBack }: FactorDetailProps) {
  const ctx = useApp();
  const { message } = App.useApp();
  const isZh = ctx.locale.startsWith("zh");

  const [loading, setLoading] = useState(true);
  const [transitioning, setTransitioning] = useState(false);
  const [factor, setFactor] = useState<FactorDefinition | null>(null);
  const [versions, setVersions] = useState<FactorVersionListItem[]>([]);
  const [references, setReferences] = useState<FactorReferenceInfo | null>(null);
  const [transitions, setTransitions] = useState<FactorTransitionAudit[]>([]);
  const [modalState, setModalState] = useState<TransitionModalState>(INITIAL_MODAL);

  // —— 标签 / 颜色映射辅助函数（与 FactorLibrary 保持一致）——

  const statusColor = (status: string | null): string => {
    if (!status) return "default";
    const map: Record<string, string> = {
      draft: "default",
      candidate: "blue",
      testing: "orange",
      shadow: "purple",
      active: "green",
      quarantined: "red",
      deprecated: "gray",
      rejected: "red",
    };
    return map[status] || "default";
  };

  const statusLabel = (status: string | null): string => {
    if (!status) return "-";
    return t(`factorStatus_${status}`);
  };

  const directionLabel = (dir: string | null | undefined): string => {
    if (!dir) return "-";
    return t(`factorDirection_${dir}`) || dir;
  };

  const originLabel = (origin: string | null | undefined): string => {
    if (!origin) return "-";
    return t(`factorOrigin_${origin}`) || origin;
  };

  const kindLabel = (kind: string | null | undefined): string => {
    if (!kind) return "-";
    return t(`factorKind_${kind}`) || kind;
  };

  const riskLevelLabel = (level: string | null | undefined): string => {
    if (!level) return "-";
    return t(`factorRiskLevel_${level}`) || level;
  };

  const formatTime = (value: string | null | undefined): string => {
    if (!value) return "-";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    return d.toLocaleString(isZh ? "zh-CN" : "en-US");
  };

  // —— 数据加载 ——
  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [f, v, tr] = await Promise.all([
        api.getFactorDefinition(factorCode),
        requestJson<FactorVersionListItem[]>(
          `/api/v1/factors/${encodeURIComponent(factorCode)}/versions`
        ),
        api.getFactorTransitionHistory(factorCode),
      ]);
      setFactor(f);
      setVersions(Array.isArray(v) ? v : []);
      setTransitions(Array.isArray(tr) ? tr : []);

      // 引用信息：优先使用 active_version_id，回退 shadow_version_id
      const versionId = f.active_version_id ?? f.shadow_version_id;
      if (versionId) {
        try {
          const ref = await api.getFactorReferences(factorCode, versionId);
          setReferences(ref);
        } catch {
          setReferences(null);
        }
      } else {
        setReferences(null);
      }
    } catch (err: any) {
      message.error(err?.message || t("factorDetailLoadFailed"));
    } finally {
      setLoading(false);
    }
  }, [factorCode, message]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // —— 状态迁移 ——
  // 对齐后端 ACTION_PREREQUISITES（factor_lifecycle.py）：
  // - draft: submit_candidate（→candidate）、reject（→rejected）
  // - candidate: start_testing（→testing）、reject（→rejected）
  // - testing: reject（→rejected）
  // - rejected: 无 transition 按钮，通过"创建新版本"回到 draft（onOpenEditor）
  // - active: deprecate（R1 不应达 active，防御性回退）
  // - shadow/quarantined/deprecated: 无按钮（R1 不开放）
  // 注意：revoke_to_draft 的 prereq={rejected}，从 testing 调用会被后端拒绝（transition_forbidden）
  const TRANSITION_ACTIONS: Record<string, { action: string; label: string; danger?: boolean }[]> = {
    draft: [
      { action: "submit_candidate", label: t("factorActionSubmitCandidate") },
      { action: "reject", label: t("factorActionReject"), danger: true },
    ],
    candidate: [
      { action: "start_testing", label: t("factorActionStartTesting") },
      { action: "reject", label: t("factorActionReject"), danger: true },
    ],
    testing: [{ action: "reject", label: t("factorActionReject"), danger: true }],
    active: [{ action: "deprecate", label: t("factorActionDeprecate"), danger: true }],
  };

  const openTransitionModal = (action: string, label: string, danger?: boolean) => {
    setModalState({ open: true, action, label, danger, reason: "" });
  };

  const handleTransitionSubmit = async () => {
    const { action, reason } = modalState;
    setTransitioning(true);
    try {
      const result = await api.executeFactorTransition(factorCode, {
        action: action as "submit_candidate" | "start_testing" | "reject" | "revoke_to_draft" | "deprecate",
        reason: reason || undefined,
      });
      if (result && result.success === false) {
        message.error(result.error || t("factorTransitionFailed"));
      } else {
        message.success(t("factorTransitionSuccess"));
      }
      setModalState(INITIAL_MODAL);
      await loadAll();
    } catch (err: any) {
      message.error(err?.message || t("factorTransitionFailed"));
    } finally {
      setTransitioning(false);
    }
  };

  // —— 渲染 ——
  if (loading && !factor) {
    return (
      <Card>
        <Spin tip={t("factorDetailLoading")} style={{ display: "block", padding: 48 }} />
      </Card>
    );
  }

  if (!factor) {
    return (
      <Card>
        <Empty description={t("factorDetailNotFound")}>
          <Button onClick={onBack}>{t("factorDetailBack")}</Button>
        </Empty>
      </Card>
    );
  }

  const lifecycleStatus = factor.lifecycle_status;
  const availableActions = lifecycleStatus ? TRANSITION_ACTIONS[lifecycleStatus] || [] : [];

  // 版本列表列定义
  const versionColumns = [
    { title: t("factorDetailVersionCol"), dataIndex: "version", key: "version", width: 80 },
    {
      title: t("factorDetailFormulaCol"),
      dataIndex: "formula_expr",
      key: "formula_expr",
      ellipsis: true,
      render: (v: string) => (
        <Typography.Text code style={{ wordBreak: "break-all" }}>
          {v || "-"}
        </Typography.Text>
      ),
    },
    {
      title: t("factorDetailDirectionCol"),
      dataIndex: "direction",
      key: "direction",
      width: 120,
      render: (dir: string) => directionLabel(dir),
    },
    {
      title: t("factorDetailValidationStatusCol"),
      key: "validation_status",
      width: 120,
      render: () => "-",
    },
    {
      title: t("factorDetailChangeNoteCol"),
      dataIndex: "change_note",
      key: "change_note",
      ellipsis: true,
      render: (v: string) => v || "-",
    },
    {
      title: t("factorDetailIsLatestCol"),
      dataIndex: "is_latest",
      key: "is_latest",
      width: 90,
      render: (latest: boolean) =>
        latest ? <Tag color="green">{t("factorDetailYes")}</Tag> : <Tag>{t("factorDetailNo")}</Tag>,
    },
    {
      title: t("factorDetailCreatedAtCol"),
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (v: string) => formatTime(v),
    },
  ];

  // 迁移历史时间线
  const timelineItems = [...transitions]
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
    .map((item) => ({
      color: statusColor(item.to_status),
      children: (
        <div>
          <Space size={4} wrap>
            <Tag color={statusColor(item.from_status)}>{statusLabel(item.from_status)}</Tag>
            <span style={{ color: "#999" }}>→</span>
            <Tag color={statusColor(item.to_status)}>{statusLabel(item.to_status)}</Tag>
          </Space>
          <div style={{ marginTop: 4, fontSize: 13 }}>
            <span style={{ color: "#888" }}>{t("factorDetailTransitionActor")}:</span>{" "}
            <span>{item.actor || "-"}</span>
          </div>
          {item.reason ? (
            <div style={{ fontSize: 13 }}>
              <span style={{ color: "#888" }}>{t("factorDetailTransitionReason")}:</span>{" "}
              <span>{item.reason}</span>
            </div>
          ) : null}
          <div style={{ fontSize: 12, color: "#aaa" }}>{formatTime(item.created_at)}</div>
        </div>
      ),
    }));

  return (
    <Spin spinning={loading}>
      <div className="factor-editor-actions" style={{ marginBottom: 16 }}>
        <Space wrap>
          <Button onClick={onBack}>{t("factorDetailBack")}</Button>
          <Typography.Text strong style={{ fontSize: 16 }}>
            {factor.code}
          </Typography.Text>
          <Tag color={statusColor(lifecycleStatus)}>{statusLabel(lifecycleStatus)}</Tag>
        </Space>
        <Button type="primary" onClick={() => onOpenEditor(factor.code)}>
          {t("factorDetailNewVersion")}
        </Button>
      </div>

      {/* 风险提示：版本被引用时不可变 */}
      {references && references.is_immutable ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("factorDetailImmutableWarningTitle")}
          description={t("factorDetailImmutableWarningDesc")}
        />
      ) : null}

      {/* 1. 因子基本信息 */}
      <Card title={t("factorDetailBasicInfo")} style={{ marginBottom: 16 }} size="small">
        <Descriptions column={{ xs: 1, sm: 2, md: 3 }} size="small" bordered>
          <Descriptions.Item label={t("factorColCode")}>{factor.code}</Descriptions.Item>
          <Descriptions.Item label={t("factorColName")}>{factorLabel(factor.code, factor.name)}</Descriptions.Item>
          <Descriptions.Item label={t("factorColCategory")}>{factorCategoryLabel(factor.category)}</Descriptions.Item>
          <Descriptions.Item label={t("factorColDirection")}>
            {factorDirectionLabel(factor.direction)}
          </Descriptions.Item>
          <Descriptions.Item label={t("factorColStatus")}>
            <Tag color={statusColor(lifecycleStatus)}>{statusLabel(lifecycleStatus)}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label={t("factorColOrigin")}>{originLabel(factor.origin)}</Descriptions.Item>
          <Descriptions.Item label={t("factorColKind")}>{kindLabel(factor.factor_kind)}</Descriptions.Item>
          <Descriptions.Item label={t("factorColRiskLevel")}>
            {riskLevelLabel(factor.risk_level)}
          </Descriptions.Item>
          <Descriptions.Item label={t("factorColOwner")}>{factor.owner || "-"}</Descriptions.Item>
          <Descriptions.Item label={t("factorColDescription")} span={3}>
            {factor.description || "-"}
          </Descriptions.Item>
          <Descriptions.Item label={t("factorColThesis")} span={3}>
            {factor.thesis || "-"}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {/* 5. 状态迁移操作按钮 */}
      {availableActions.length > 0 ? (
        <Card title={t("factorDetailTransitionActions")} style={{ marginBottom: 16 }} size="small">
          <Space wrap>
            {availableActions.map((act) => (
              <Button
                key={act.action}
                danger={act.danger}
                loading={transitioning}
                onClick={() => openTransitionModal(act.action, act.label, act.danger)}
              >
                {act.label}
              </Button>
            ))}
          </Space>
        </Card>
      ) : null}

      {/* 2. 版本列表 */}
      <Card title={t("factorDetailVersions")} style={{ marginBottom: 16 }} size="small">
        <Table
          columns={versionColumns}
          dataSource={versions}
          rowKey={(record) => `${record.factor_code}-${record.version}`}
          size="small"
          pagination={versions.length > 10 ? { pageSize: 10 } : false}
          locale={{ emptyText: <Empty description={t("factorDetailNoVersions")} /> }}
        />
      </Card>

      {/* 3. 引用信息 */}
      <Card title={t("factorDetailReferences")} style={{ marginBottom: 16 }} size="small">
        {references ? (
          <Descriptions column={{ xs: 1, sm: 2, md: 3 }} size="small" bordered>
            <Descriptions.Item label={t("factorDetailReferenceVersion")}>
              {references.factor_version}
            </Descriptions.Item>
            <Descriptions.Item label={t("factorDetailReferenceImmutable")}>
              {references.is_immutable ? (
                <Tag color="orange">{t("factorDetailYes")}</Tag>
              ) : (
                <Tag>{t("factorDetailNo")}</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label={t("factorDetailReferenceTotal")}>
              {references.referenced_by_evaluations +
                references.referenced_by_factor_sets +
                references.referenced_by_model_runs}
            </Descriptions.Item>
            <Descriptions.Item label={t("factorDetailReferenceByEvaluations")}>
              {references.referenced_by_evaluations}
            </Descriptions.Item>
            <Descriptions.Item label={t("factorDetailReferenceByFactorSets")}>
              {references.referenced_by_factor_sets}
            </Descriptions.Item>
            <Descriptions.Item label={t("factorDetailReferenceByModelRuns")}>
              {references.referenced_by_model_runs}
            </Descriptions.Item>
          </Descriptions>
        ) : (
          <Empty description={t("factorDetailNoReferences")} />
        )}
      </Card>

      {/* 4. 状态迁移历史 */}
      <Card title={t("factorDetailTransitions")} size="small">
        {timelineItems.length > 0 ? (
          <Timeline items={timelineItems} />
        ) : (
          <Empty description={t("factorDetailNoTransitions")} />
        )}
      </Card>

      {/* 状态迁移理由输入弹窗 */}
      <Modal
        title={`${t("factorTransitionConfirm")} - ${modalState.label}`}
        open={modalState.open}
        onOk={handleTransitionSubmit}
        onCancel={() => setModalState(INITIAL_MODAL)}
        okText={t("factorTransitionOk")}
        cancelText={t("factorTransitionCancel")}
        okButtonProps={{ danger: modalState.danger, loading: transitioning }}
        destroyOnHidden
      >
        <Input.TextArea
          placeholder={t("factorTransitionReasonPlaceholder")}
          value={modalState.reason}
          onChange={(e) => setModalState((prev) => ({ ...prev, reason: e.target.value }))}
          rows={4}
          maxLength={500}
          showCount
        />
      </Modal>
    </Spin>
  );
}
