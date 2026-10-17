import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Collapse, Descriptions, Form, Input, Modal, Select, Space, Tag, Tooltip, message } from "antd";
import { ExperimentOutlined, CheckCircleOutlined, CloseCircleOutlined, EditOutlined } from "@ant-design/icons";
import { api } from "../../api/client";
import { t, template } from "../../i18n";
import type { AIDraftDetail, AIDraftPreviewResult } from "../../types";

/** AI 因子草案字段（与后端 FactorDraftSchema 对齐）。 */
type FactorDraftPayload = {
  code: string;
  name: string;
  category: string;
  formula_expr: string;
  params: Record<string, unknown>;
  direction: "higher_better" | "lower_better" | "nonlinear";
  factor_kind: "continuous" | "event" | "regime";
  risk_level: "low" | "medium" | "high";
  description?: string;
  thesis?: string;
  postprocess?: Record<string, unknown> | null;
  change_note?: string;
};

interface FactorDraftConfirmModalProps {
  open: boolean;
  /** 审计记录 ID（若提供则支持完整确认/执行流程） */
  auditId?: number | null;
  /** AI 建议的原始 payload（无 auditId 时直接使用） */
  suggestedPayload?: FactorDraftPayload | null;
  onClose: () => void;
  /** 应用到编辑器：将（可能修改后的）payload 传入 FactorEditor，用户主动保存 */
  onApplyToEditor: (payload: FactorDraftPayload) => void;
  /** 直接创建成功后回调（走 audit confirm+execute 流程时） */
  onCreated?: (factorCode: string, factorId: number) => void;
}

/** 对比字段定义：用于差异展示。 */
const DIFF_FIELDS: Array<{ key: keyof FactorDraftPayload; label: string }> = [
  { key: "code", label: "aiDraftFieldCode" },
  { key: "name", label: "aiDraftFieldName" },
  { key: "category", label: "aiDraftFieldCategory" },
  { key: "formula_expr", label: "aiDraftFieldFormula" },
  { key: "direction", label: "aiDraftFieldDirection" },
  { key: "factor_kind", label: "aiDraftFieldKind" },
  { key: "risk_level", label: "aiDraftFieldRisk" },
  { key: "params", label: "aiDraftFieldParams" },
  { key: "postprocess", label: "aiDraftFieldPostprocess" },
];

export default function FactorDraftConfirmModal({
  open,
  auditId,
  suggestedPayload,
  onClose,
  onApplyToEditor,
  onCreated,
}: FactorDraftConfirmModalProps) {
  const [loading, setLoading] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [rejectLoading, setRejectLoading] = useState(false);
  const [draftDetail, setDraftDetail] = useState<AIDraftDetail | null>(null);
  const [originalPayload, setOriginalPayload] = useState<FactorDraftPayload | null>(null);
  const [formValues, setFormValues] = useState<FactorDraftPayload | null>(null);
  const [previewResult, setPreviewResult] = useState<AIDraftPreviewResult | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [readRevision, setReadRevision] = useState(0);
  const draftSession = useRef(0);
  const previewRequest = useRef(0);
  const pendingOperation = useRef<object | null>(null);

  // 初始化/重置：open 或 auditId/suggestedPayload 变化时加载
  useEffect(() => {
    const session = ++draftSession.current;
    previewRequest.current += 1;
    const isCurrent = () => session === draftSession.current;
    setLoading(false);
    setPreviewLoading(false);
    setConfirmLoading(false);
    setRejectLoading(false);
    pendingOperation.current = null;
    setDraftDetail(null);
    setOriginalPayload(null);
    setFormValues(null);
    setPreviewResult(null);
    setLoadError(false);
    if (!open) {
      return () => { draftSession.current += 1; };
    }

    const initial = (suggestedPayload ?? null) as FactorDraftPayload | null;
    if (auditId) {
      // 有 auditId：从后端加载详情
      setLoading(true);
      api
        .getAIDraft(auditId)
        .then((detail) => {
          if (!isCurrent()) return;
          if (detail.audit_id !== auditId || detail.action_type !== "draft_factor") throw new Error("draft_target_mismatch");
          setDraftDetail(detail);
          const orig = (detail.original_suggested_payload ?? {}) as FactorDraftPayload;
          const curr = (detail.current_payload ?? {}) as FactorDraftPayload;
          setOriginalPayload(orig);
          setFormValues(curr);
        })
        .catch(() => {
          if (!isCurrent()) return;
          // An unverified fallback must never be attached to a different
          // persisted audit target and then confirmed as that draft.
          setLoadError(true);
        })
        .finally(() => { if (isCurrent()) setLoading(false); });
    } else {
      // 无 auditId：直接用传入的 suggestedPayload
      setOriginalPayload(initial);
      setFormValues(initial);
    }
    return () => { draftSession.current += 1; };
  }, [open, auditId, suggestedPayload, readRevision]);

  // 计算差异
  const diffList = useMemo(() => {
    if (!originalPayload || !formValues) return [];
    return DIFF_FIELDS.map(({ key, label }) => {
      const display = (value: unknown) => value && typeof value === "object" ? JSON.stringify(value) : String(value ?? "—");
      const oldVal = display(originalPayload[key]);
      const newVal = display(formValues[key]);
      return { label, oldVal, newVal, changed: oldVal !== newVal };
    }).filter((d) => d.changed);
  }, [originalPayload, formValues]);

  // 预览校验
  const handlePreview = async () => {
    if (!formValues || loading || loadError || pendingOperation.current) return;
    const session = draftSession.current;
    const request = ++previewRequest.current;
    const isCurrent = () => session === draftSession.current && request === previewRequest.current;
    setPreviewLoading(true);
    try {
      let result: AIDraftPreviewResult;
      if (auditId) {
        result = await api.previewAIDraft(auditId, formValues as Record<string, unknown>);
      } else {
        // 无 auditId 时直接用 factor 校验 API
        const resp = await api.validateFactorFormula({
          formula_expr: formValues.formula_expr,
          params: formValues.params,
          direction: formValues.direction,
          ...(formValues.postprocess !== undefined ? { postprocess: formValues.postprocess } : {}),
        });
        const errorMessages = (resp.errors ?? []).map((e) => {
          if (typeof e === "string") return e;
          const obj = e as unknown as Record<string, unknown>;
          return String(obj.message ?? obj.error_code ?? JSON.stringify(e));
        });
        result = {
          is_valid: resp.is_valid,
          errors: errorMessages,
          changes: [],
          extra: resp.execution_plan ? { execution_plan: resp.execution_plan } : undefined,
        };
      }
      if (!isCurrent()) return;
      setPreviewResult(result);
      if (result.is_valid) {
        message.success(t("aiDraftPreviewValid"));
      } else {
        message.warning(t("aiDraftPreviewInvalid"));
      }
    } catch {
      if (!isCurrent()) return;
      message.error(t("aiDraftPreviewFailed"));
    } finally {
      if (isCurrent()) setPreviewLoading(false);
    }
  };

  // 应用到编辑器（不写 DB，用户主动保存）
  const handleApplyToEditor = () => {
    if (!formValues || loading || loadError || pendingOperation.current) return;
    onApplyToEditor(JSON.parse(JSON.stringify(formValues)));
    onClose();
  };

  // 确认并直接创建（走 audit confirm + execute 流程）
  const handleConfirmAndCreate = async () => {
    if (!auditId || !formValues || loading || loadError || pendingOperation.current) return;
    const session = draftSession.current;
    const operation = {};
    pendingOperation.current = operation;
    const isCurrent = () => session === draftSession.current && pendingOperation.current === operation;
    const targetAuditId = auditId;
    const payloadSnapshot = JSON.parse(JSON.stringify(formValues)) as Record<string, unknown>;
    previewRequest.current += 1;
    setPreviewLoading(false);
    setConfirmLoading(true);
    try {
      // 先校验
      const preview = await api.previewAIDraft(targetAuditId, payloadSnapshot);
      if (!isCurrent()) return;
      if (!preview.is_valid) {
        setPreviewResult(preview);
        message.error(t("aiDraftConfirmBlocked"));
        return;
      }
      // 确认（携带修改后的 payload）
      await api.confirmAIDraft(targetAuditId, payloadSnapshot);
      if (!isCurrent()) return;
      // 执行
      const execResult = await api.executeAIDraft(targetAuditId);
      if (!isCurrent()) return;
      if (execResult.success && execResult.factor_code && execResult.factor_id) {
        message.success(
          template("aiDraftCreateSuccess", { code: execResult.factor_code }),
        );
        onCreated?.(execResult.factor_code, execResult.factor_id);
        onClose();
      } else {
        message.error(execResult.message || t("aiDraftCreateFailed"));
      }
    } catch (error: unknown) {
      if (!isCurrent()) return;
      const err = error as { detail?: { error_code?: string; user_message?: string }; message?: string };
      const detail = err?.detail;
      if (detail && typeof detail === "object" && detail.error_code === "not_confirmed") {
        message.error(t("aiDraftNotConfirmed"));
      } else {
        message.error(err?.message || t("aiDraftCreateFailed"));
      }
    } finally {
      if (isCurrent()) {
        pendingOperation.current = null;
        setConfirmLoading(false);
      }
    }
  };

  // 拒绝
  const handleReject = async () => {
    if (loading || loadError || pendingOperation.current) return;
    if (!auditId) {
      onClose();
      return;
    }
    const session = draftSession.current;
    const operation = {};
    pendingOperation.current = operation;
    const isCurrent = () => session === draftSession.current && pendingOperation.current === operation;
    previewRequest.current += 1;
    setPreviewLoading(false);
    setRejectLoading(true);
    try {
      await api.rejectAIDraft(auditId, t("aiDraftRejectedByUser"));
      if (!isCurrent()) return;
      message.info(t("aiDraftRejected"));
      onClose();
    } catch {
      if (!isCurrent()) return;
      message.error(t("aiDraftRejectFailed"));
    } finally {
      if (isCurrent()) {
        pendingOperation.current = null;
        setRejectLoading(false);
      }
    }
  };

  const updateField = <K extends keyof FactorDraftPayload>(key: K, value: FactorDraftPayload[K]) => {
    if (pendingOperation.current) return;
    previewRequest.current += 1;
    setPreviewResult(null);
    setPreviewLoading(false);
    setFormValues((prev) => (prev ? { ...prev, [key]: value } : prev));
  };

  return (
    <Modal
      title={
        <Space>
          <ExperimentOutlined style={{ color: "#722ed1" }} />
          <span>{t("aiDraftFactorTitle")}</span>
        </Space>
      }
      open={open}
      onCancel={onClose}
      closable={!confirmLoading && !rejectLoading}
      maskClosable={!confirmLoading && !rejectLoading}
      keyboard={!confirmLoading && !rejectLoading}
      width={720}
      destroyOnHidden
      footer={
        <Space wrap>
          <Button onClick={onClose} disabled={confirmLoading || rejectLoading}>{t("aiDraftCancel")}</Button>
          {auditId && (
            <Button danger loading={rejectLoading} disabled={loading || loadError || confirmLoading} onClick={handleReject}>
              {t("aiDraftReject")}
            </Button>
          )}
          <Button icon={<EditOutlined />} loading={previewLoading} disabled={!formValues || loading || loadError || confirmLoading || rejectLoading} onClick={handlePreview}>
            {t("aiDraftPreview")}
          </Button>
          <Button type="primary" ghost onClick={handleApplyToEditor} disabled={!formValues || loading || confirmLoading || rejectLoading}>
            {t("aiDraftApplyToEditor")}
          </Button>
          {auditId && (
            <Tooltip title={t("aiDraftConfirmCreateTip")}>
              <Button
                type="primary"
                icon={<CheckCircleOutlined />}
                loading={confirmLoading}
                onClick={handleConfirmAndCreate}
                disabled={!formValues || loading || rejectLoading}
              >
                {t("aiDraftConfirmCreate")}
              </Button>
            </Tooltip>
          )}
        </Space>
      }
    >
      {loading ? (
        <div style={{ textAlign: "center", padding: 24 }}>{t("aiDraftLoading")}</div>
      ) : loadError ? (
        <Alert type="error" showIcon message={t("aiDraftLoadFailed")} description={t("aiDraftLoadFailedHelp")}
          action={<Button size="small" onClick={() => setReadRevision((revision) => revision + 1)}>{t("retry")}</Button>} />
      ) : formValues ? (
        <>
          <Alert
            type="info"
            showIcon
            message={t("aiDraftHint")}
            description={t("aiDraftHintDesc")}
            style={{ marginBottom: 12 }}
          />

          {/* 差异展示 */}
          {diffList.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message={template("aiDraftDiffCount", { count: diffList.length })}
              style={{ marginBottom: 12 }}
              description={
                <Descriptions size="small" column={1} bordered>
                  {diffList.map((d) => (
                    <Descriptions.Item key={d.label} label={t(d.label)}>
                      <Space>
                        <Tag color="default">
                          <CloseCircleOutlined /> {d.oldVal}
                        </Tag>
                        <span>→</span>
                        <Tag color="green">
                          <CheckCircleOutlined /> {d.newVal}
                        </Tag>
                      </Space>
                    </Descriptions.Item>
                  ))}
                </Descriptions>
              }
            />
          )}

          {/* 校验结果 */}
          {previewResult && (
            <Alert
              type={previewResult.is_valid ? "success" : "error"}
              showIcon
              message={previewResult.is_valid ? t("aiDraftPreviewValid") : t("aiDraftPreviewInvalid")}
              style={{ marginBottom: 12 }}
              description={
                previewResult.errors.length > 0 ? (
                  <ul style={{ margin: 0, paddingLeft: 20 }}>
                    {previewResult.errors.map((e, i) => (
                      <li key={i} style={{ fontSize: 12 }}>{e}</li>
                    ))}
                  </ul>
                ) : null
              }
            />
          )}

          {/* 可编辑表单 */}
          <Form layout="vertical" size="small" disabled={confirmLoading || rejectLoading}>
            <div className="indicator-form-grid compact">
              <Form.Item label={t("aiDraftFieldCode")}>
                <Input
                  value={formValues.code}
                  onChange={(e) =>
                    updateField(
                      "code",
                      e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"),
                    )
                  }
                  placeholder="factor_code"
                />
              </Form.Item>
              <Form.Item label={t("aiDraftFieldName")}>
                <Input
                  value={formValues.name}
                  onChange={(e) => updateField("name", e.target.value)}
                />
              </Form.Item>
              <Form.Item label={t("aiDraftFieldCategory")}>
                <Input
                  value={formValues.category}
                  onChange={(e) => updateField("category", e.target.value)}
                />
              </Form.Item>
              <Form.Item label={t("aiDraftFieldDirection")}>
                <Select
                  value={formValues.direction}
                  onChange={(v) => updateField("direction", v)}
                  options={[
                    { label: t("factorDirection_higher_better"), value: "higher_better" },
                    { label: t("factorDirection_lower_better"), value: "lower_better" },
                    { label: t("factorDirection_nonlinear"), value: "nonlinear" },
                  ]}
                />
              </Form.Item>
              <Form.Item label={t("aiDraftFieldKind")}>
                <Select
                  value={formValues.factor_kind}
                  onChange={(v) => updateField("factor_kind", v)}
                  options={[
                    { label: t("factorEditorKindContinuous"), value: "continuous" },
                    { label: t("factorEditorKindEvent"), value: "event" },
                    { label: t("factorEditorKindRegime"), value: "regime" },
                  ]}
                />
              </Form.Item>
              <Form.Item label={t("aiDraftFieldRisk")}>
                <Select
                  value={formValues.risk_level}
                  onChange={(v) => updateField("risk_level", v)}
                  options={[
                    { label: t("factorRiskLevel_low"), value: "low" },
                    { label: t("factorRiskLevel_medium"), value: "medium" },
                    { label: t("factorRiskLevel_high"), value: "high" },
                  ]}
                />
              </Form.Item>
            </div>
            <Form.Item label={t("aiDraftFieldFormula")}>
              <Input.TextArea
                value={formValues.formula_expr}
                onChange={(e) => updateField("formula_expr", e.target.value)}
                rows={3}
                style={{ fontFamily: "monospace", fontSize: 12 }}
              />
            </Form.Item>
            <Form.Item label={t("aiDraftFieldThesis")}>
              <Input.TextArea
                value={formValues.thesis ?? ""}
                onChange={(e) => updateField("thesis", e.target.value)}
                rows={2}
              />
            </Form.Item>
            <Collapse size="small" items={[{
              key: "advanced", label: t("aiDraftAdvancedPayload"),
              children: <>
                <p style={{ color: "var(--pt-muted-foreground)", fontSize: 12 }}>{t("aiDraftAdvancedPayloadHelp")}</p>
                <Descriptions size="small" column={1} bordered>
                  <Descriptions.Item label={t("aiDraftFieldParams")}><pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{JSON.stringify(formValues.params || {}, null, 2)}</pre></Descriptions.Item>
                  <Descriptions.Item label={t("aiDraftFieldPostprocess")}><pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{JSON.stringify(formValues.postprocess || {}, null, 2)}</pre></Descriptions.Item>
                </Descriptions>
              </>,
            }]} />
          </Form>
        </>
      ) : (
        <div style={{ textAlign: "center", padding: 24 }}>{t("aiDraftNoPayload")}</div>
      )}
    </Modal>
  );
}
