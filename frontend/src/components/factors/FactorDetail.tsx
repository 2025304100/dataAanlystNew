import { useState, useEffect, useCallback, useRef } from "react";
import { useApp } from "../../context/AppContext";
import { t, factorLabel, factorCategoryLabel, factorDirectionLabel } from "../../i18n";
import {
  api,
  type FactorDefinition,
  type FactorReferenceInfo,
  type FactorTransitionAudit,
  type FactorVersionListItem,
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
  onChanged?: () => void;
};

type TransitionModalState = {
  open: boolean;
  action: string;
  label: string;
  danger?: boolean;
  reason: string;
  requestId?: string;
};

const INITIAL_MODAL: TransitionModalState = {
  open: false,
  action: "",
  label: "",
  reason: "",
};

export default function FactorDetail({ factorCode, onOpenEditor, onBack, onChanged }: FactorDetailProps) {
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
  const requestSequence = useRef(0);
  const currentCode = useRef(factorCode);
  currentCode.current = factorCode;
  const [loadError, setLoadError] = useState<string | null>(null);
  const [referenceError, setReferenceError] = useState(false);

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
    const request = ++requestSequence.current;
    setLoading(true);
    setLoadError(null);
    setFactor(null);
    setVersions([]);
    setReferences(null);
    setTransitions([]);
    setReferenceError(false);
    try {
      const [f, v, tr] = await Promise.all([
        // Factor-domain internal - DO NOT USE outside factor center
        api.scoringGetFactorDefinition(factorCode),
        api.listFactorVersions(factorCode),
        api.getFactorTransitionHistory(factorCode),
      ]);
      if (request !== requestSequence.current || currentCode.current !== factorCode) return;
      setFactor(f);
      const versionList = Array.isArray(v) ? v : [];
      setVersions(versionList);
      setTransitions(Array.isArray(tr) ? tr : []);

      // References belong to the version shown as latest, including drafts.
      const versionId = f.latest_version_id ?? versionList.find((version) => version.is_latest)?.id ?? f.active_version_id ?? f.shadow_version_id;
      if (versionId) {
        try {
          const ref = await api.getFactorReferences(factorCode, versionId);
          if (request === requestSequence.current) setReferences(ref);
        } catch {
          if (request === requestSequence.current) setReferenceError(true);
        }
      } else {
        setReferences(null);
      }
    } catch (err: any) {
      if (request === requestSequence.current) setLoadError(err?.message || t("factorDetailLoadFailed"));
    } finally {
      if (request === requestSequence.current) setLoading(false);
    }
  }, [factorCode]);

  useEffect(() => {
    void loadAll();
    setModalState(INITIAL_MODAL);
    setTransitioning(false);
    return () => { requestSequence.current += 1; };
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
    setModalState({ open: true, action, label, danger, reason: "", requestId:crypto.randomUUID() });
  };

  const handleTransitionSubmit = async () => {
    if (transitioning) return;
    const { action, reason } = modalState;
    const code = factorCode;
    const request = requestSequence.current;
    setTransitioning(true);
    try {
      const result = await api.executeFactorTransition(code, {
        action: action as "submit_candidate" | "start_testing" | "reject" | "revoke_to_draft" | "deprecate",
        reason: reason || undefined,
        request_id: modalState.requestId,
      });
      if (result && result.success !== false) onChanged?.();
      if (request !== requestSequence.current || currentCode.current !== code) return;
      if (result && result.success === false) {
        message.error(result.error || t("factorTransitionFailed"));
        return;
      } else {
        message.success(t("factorTransitionSuccess"));
      }
      setModalState(INITIAL_MODAL);
      await loadAll();
    } catch (err: any) {
      if (request === requestSequence.current) message.error(err?.message || t("factorTransitionFailed"));
    } finally {
      if (currentCode.current === code) setTransitioning(false);
    }
  };

  // —— 渲染 ——
  if (loading && !factor) {
    return (
      <Card>
        {/* P2-3：Spin tip 独立使用会告警，改用嵌套模式使 tip 生效且不触发废弃警告 */}
        <Spin spinning={true} tip={t("factorDetailLoading")}>
          <div style={{ padding: 48 }} />
        </Spin>
      </Card>
    );
  }

  if (!factor) {
    return (
      <Card>
        {loadError && <Alert type="error" showIcon message={loadError} action={<Button onClick={loadAll}>{t("factorCenterLibraryRetry")}</Button>} />}
        <Empty description={t("factorDetailNotFound")}>
          <Button onClick={onBack}>{t("factorDetailBack")}</Button>
        </Empty>
      </Card>
    );
  }

  const lifecycleStatus = factor.lifecycle_status;
  const servicePending = !Object.prototype.hasOwnProperty.call(factor, "latest_version_id");
  const availableActions = lifecycleStatus ? TRANSITION_ACTIONS[lifecycleStatus] || [] : [];

  // 版本列表列定义
  const versionColumns = [
    { title: t("factorDetailVersionCol"), dataIndex: "version", key: "version", width: 80, render:(version:number)=>`v${version}` },
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
      render: (_:unknown, version:FactorVersionListItem) => version.validation_status ? <Tag color={version.validation_status === "valid" ? "green" : "orange"}>{t(`factorValidation_${version.validation_status}`)}</Tag> : t("factorCenterDetailNoValidation"),
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

      {servicePending && <Alert style={{marginBottom:16}} type="warning" showIcon message={t("factorCenterLibraryServicePending")} description={t("factorCenterLibraryServicePendingDesc")}/>}
      <Alert className="factor-center-detail-guide" type="info" showIcon
        message={t("factorCenterDetailNextStep")}
        description={t(`factorCenterDetailGuide_${lifecycleStatus === "draft" ? "draft" : lifecycleStatus === "candidate" ? "candidate" : lifecycleStatus === "rejected" || lifecycleStatus === "deprecated" ? "revise" : "evaluate"}`)}
        action={(lifecycleStatus === "testing" || lifecycleStatus === "active" || lifecycleStatus === "shadow") && factor.latest_version_id ? <Button type="primary" onClick={() => window.dispatchEvent(new CustomEvent("factor-center:navigate",{detail:{target:"evaluation",factor_code:factor.code,version_id:factor.latest_version_id}}))}>{t("factorCenterLibraryEvaluate")}</Button> : undefined} />

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
                type={act.danger ? "default" : "primary"}
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
        {referenceError || (servicePending && versions.length > 0 && !references) ? <Alert type="warning" showIcon message={t("factorCenterDetailReferencesFailed")} action={<Button onClick={loadAll}>{t("factorCenterLibraryRetry")}</Button>}/> : references ? (
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
        onCancel={() => { if (!transitioning) setModalState(INITIAL_MODAL); }}
        closable={!transitioning}
        maskClosable={!transitioning}
        keyboard={!transitioning}
        okText={t("factorTransitionOk")}
        cancelText={t("factorTransitionCancel")}
        cancelButtonProps={{disabled:transitioning}}
        okButtonProps={{ danger: modalState.danger, loading: transitioning }}
        destroyOnHidden
      >
        <Input.TextArea
          placeholder={t("factorTransitionReasonPlaceholder")}
          value={modalState.reason}
          disabled={transitioning}
          onChange={(e) => setModalState((prev) => ({ ...prev, reason: e.target.value }))}
          rows={4}
          maxLength={500}
          showCount
        />
      </Modal>
    </Spin>
  );
}
