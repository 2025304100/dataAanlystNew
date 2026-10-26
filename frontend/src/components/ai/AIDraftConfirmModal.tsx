import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Collapse, Descriptions, Form, Modal, Space, Tag, Tooltip, message } from "antd";
import { CheckCircleOutlined, CloseCircleOutlined, EditOutlined, FileTextOutlined } from "@ant-design/icons";
import { api } from "../../api/client";
import { t, template } from "../../i18n";
import type { AIDraftDetail, AIDraftPreviewResult } from "../../types";
import { getDraftRenderer, type DraftValues } from "./draftFieldRenderers";

interface AIDraftConfirmModalProps {
  open: boolean;
  /** 审计记录 ID：有值才可预览/确认/执行（三步流程的后端锚点） */
  auditId?: number | null;
  /** 草稿类型（draft_note / draft_alert / draft_filter） */
  draftType?: string | null;
  /** 无 auditId 时的兜底 payload（只读展示） */
  suggestedPayload?: DraftValues | null;
  onClose: () => void;
  /** 执行成功回调（父组件可用于刷新列表或标记已执行） */
  onExecuted?: (result: { success: boolean; message?: string }) => void;
}

function displayValue(value: unknown): string {
  if (value == null || value === "") return "—";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/**
 * 通用 AI 草稿确认 Modal（Batch 1：note / alert / filter）。
 *
 * 复用 FactorDraftConfirmModal 的流程骨架：加载审计详情 → 差异对比 → 规则校验预览
 * → 用户确认 → 执行；所有请求带序号防串响（切换审计目标时旧响应必须丢弃）。
 * 与因子版的差异：字段由 draftFieldRenderers 按类型渲染，且不提供「应用到编辑器」。
 */
export default function AIDraftConfirmModal({
  open,
  auditId,
  draftType,
  suggestedPayload,
  onClose,
  onExecuted,
}: AIDraftConfirmModalProps) {
  const renderer = getDraftRenderer(draftType);
  const [loading, setLoading] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [rejectLoading, setRejectLoading] = useState(false);
  const [detail, setDetail] = useState<AIDraftDetail | null>(null);
  const [originalPayload, setOriginalPayload] = useState<DraftValues | null>(null);
  const [formValues, setFormValues] = useState<DraftValues | null>(null);
  const [previewResult, setPreviewResult] = useState<AIDraftPreviewResult | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [readRevision, setReadRevision] = useState(0);
  const draftSession = useRef(0);
  const previewRequest = useRef(0);
  const pendingOperation = useRef<object | null>(null);

  // 初始化/重置：open 或 auditId 变化时加载
  useEffect(() => {
    const session = ++draftSession.current;
    previewRequest.current += 1;
    const isCurrent = () => session === draftSession.current;
    setPreviewLoading(false);
    setConfirmLoading(false);
    setRejectLoading(false);
    pendingOperation.current = null;
    setDetail(null);
    setOriginalPayload(null);
    setFormValues(null);
    setPreviewResult(null);
    setLoadError(false);
    if (!open) {
      return () => { draftSession.current += 1; };
    }

    if (auditId) {
      setLoading(true);
      api
        .getAIDraft(auditId)
        .then((loaded) => {
          if (!isCurrent()) return;
          // 审计目标必须与预期草稿类型一致：不允许把别的审计记录当成本草稿确认
          if (draftType && loaded.action_type !== draftType) {
            throw new Error("draft_target_mismatch");
          }
          setDetail(loaded);
          setOriginalPayload((loaded.original_suggested_payload ?? {}) as DraftValues);
          setFormValues((loaded.current_payload ?? {}) as DraftValues);
        })
        .catch(() => {
          if (!isCurrent()) return;
          setLoadError(true);
        })
        .finally(() => { if (isCurrent()) setLoading(false); });
    } else {
      setLoading(false);
      setOriginalPayload(suggestedPayload ?? null);
      setFormValues(suggestedPayload ?? null);
    }
    return () => { draftSession.current += 1; };
  }, [open, auditId, draftType, suggestedPayload, readRevision]);

  // 差异对比（字段集由渲染器提供）
  const diffList = useMemo(() => {
    if (!renderer || !originalPayload || !formValues) return [];
    return renderer.diffFields
      .map(({ key, labelKey }) => {
        const oldVal = displayValue(originalPayload[key]);
        const newVal = displayValue(formValues[key]);
        return { labelKey, oldVal, newVal, changed: oldVal !== newVal };
      })
      .filter((item) => item.changed);
  }, [renderer, originalPayload, formValues]);

  const handlePreview = async () => {
    if (!auditId || !formValues || loading || loadError || pendingOperation.current) return;
    const session = draftSession.current;
    const request = ++previewRequest.current;
    const isCurrent = () => session === draftSession.current && request === previewRequest.current;
    setPreviewLoading(true);
    try {
      const result = await api.previewAIDraft(auditId, formValues as Record<string, unknown>);
      if (!isCurrent()) return;
      setPreviewResult(result);
      if (result.is_valid) message.success(t("aiDraftPreviewValid"));
      else message.warning(t("aiDraftPreviewInvalid"));
    } catch {
      if (!isCurrent()) return;
      message.error(t("aiDraftPreviewFailed"));
    } finally {
      if (isCurrent()) setPreviewLoading(false);
    }
  };

  const handleConfirmAndExecute = async () => {
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
      // 执行前必须再校验一次：不允许绕过 preview 直接写业务
      const preview = await api.previewAIDraft(targetAuditId, payloadSnapshot);
      if (!isCurrent()) return;
      if (!preview.is_valid) {
        setPreviewResult(preview);
        message.error(t("aiDraftConfirmBlocked"));
        return;
      }
      await api.confirmAIDraft(targetAuditId, payloadSnapshot);
      if (!isCurrent()) return;
      const execResult = await api.executeAIDraft(targetAuditId);
      if (!isCurrent()) return;
      if (execResult.success) {
        message.success(execResult.message || t("aiDraftExecuted"));
        onExecuted?.({ success: true, message: execResult.message });
        onClose();
      } else {
        message.error(execResult.message || execResult.error || t("aiDraftExecuteFailed"));
      }
    } catch (error: unknown) {
      if (!isCurrent()) return;
      const err = error as { detail?: { error_code?: string }; message?: string };
      if (err?.detail && typeof err.detail === "object" && err.detail.error_code === "not_confirmed") {
        message.error(t("aiDraftNotConfirmed"));
      } else {
        message.error(err?.message || t("aiDraftExecuteFailed"));
      }
    } finally {
      if (isCurrent()) {
        pendingOperation.current = null;
        setConfirmLoading(false);
      }
    }
  };

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

  const updateField = (key: string, value: unknown) => {
    if (pendingOperation.current) return;
    previewRequest.current += 1;
    setPreviewResult(null);
    setPreviewLoading(false);
    setFormValues((prev) => (prev ? { ...prev, [key]: value } : prev));
  };

  const busy = confirmLoading || rejectLoading;

  return (
    <Modal
      title={
        <Space>
          <FileTextOutlined />
          <span>{t("aiDraftConfirmTitle")}</span>
          <Tag>{renderer ? t(renderer.titleKey) : draftType || t("aiDraftUnsupported")}</Tag>
        </Space>
      }
      open={open}
      onCancel={onClose}
      closable={!busy}
      maskClosable={!busy}
      keyboard={!busy}
      width={720}
      destroyOnHidden
      data-testid="ai-draft-confirm-modal"
      footer={
        <Space wrap>
          <Button onClick={onClose} disabled={busy}>{t("aiDraftCancel")}</Button>
          {auditId && !loadError && (
            <Button danger loading={rejectLoading} disabled={loading || confirmLoading} onClick={handleReject} data-testid="ai-draft-reject">
              {t("aiDraftReject")}
            </Button>
          )}
          <Tooltip title={auditId ? undefined : t("aiDraftAuditMissing")}>
            <Button
              icon={<EditOutlined />}
              loading={previewLoading}
              disabled={!auditId || !formValues || loading || loadError || busy}
              onClick={handlePreview}
              data-testid="ai-draft-preview"
            >
              {t("aiDraftPreview")}
            </Button>
          </Tooltip>
          {auditId && !loadError && (
            <Tooltip title={t("aiDraftExecuteTip")}>
              <Button
                type="primary"
                icon={<CheckCircleOutlined />}
                loading={confirmLoading}
                onClick={handleConfirmAndExecute}
                disabled={!formValues || loading || rejectLoading}
                data-testid="ai-draft-execute"
              >
                {t("aiDraftExecute")}
              </Button>
            </Tooltip>
          )}
        </Space>
      }
    >
      {loading ? (
        <div style={{ textAlign: "center", padding: 24 }}>{t("aiDraftLoading")}</div>
      ) : loadError ? (
        <Alert
          type="error"
          showIcon
          message={t("aiDraftLoadFailed")}
          description={t("aiDraftLoadFailedHelp")}
          action={<Button size="small" onClick={() => setReadRevision((r) => r + 1)}>{t("retry")}</Button>}
        />
      ) : !renderer ? (
        <Alert type="warning" showIcon message={t("aiDraftUnsupported")} />
      ) : formValues ? (
        <>
          <Alert
            type="info"
            showIcon
            message={t("aiDraftGenericHint")}
            description={t("aiDraftGenericHintDesc")}
            style={{ marginBottom: 12 }}
          />

          {!auditId && (
            <Alert
              type="warning"
              showIcon
              message={t("aiDraftAuditMissing")}
              style={{ marginBottom: 12 }}
            />
          )}

          {diffList.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message={template("aiDraftDiffCount", { count: diffList.length })}
              style={{ marginBottom: 12 }}
              description={
                <Descriptions size="small" column={1} bordered>
                  {diffList.map((d) => (
                    <Descriptions.Item key={d.labelKey} label={t(d.labelKey)}>
                      <Space>
                        <Tag color="default"><CloseCircleOutlined /> {d.oldVal}</Tag>
                        <span>→</span>
                        <Tag color="green"><CheckCircleOutlined /> {d.newVal}</Tag>
                      </Space>
                    </Descriptions.Item>
                  ))}
                </Descriptions>
              }
            />
          )}

          {previewResult && (
            <Alert
              type={previewResult.is_valid ? "success" : "error"}
              showIcon
              message={previewResult.is_valid ? t("aiDraftPreviewValid") : t("aiDraftPreviewInvalid")}
              style={{ marginBottom: 12 }}
              description={
                previewResult.errors.length > 0 ? (
                  <ul style={{ margin: 0, paddingLeft: 20 }}>
                    {previewResult.errors.map((error, index) => (
                      <li key={index} style={{ fontSize: 12 }}>{error}</li>
                    ))}
                  </ul>
                ) : null
              }
            />
          )}

          <Form layout="vertical" size="small" disabled={busy}>
            {renderer.render(formValues, updateField)}
          </Form>

          <Collapse
            size="small"
            items={[{
              key: "raw",
              label: t("aiDraftRawPayload"),
              children: (
                <pre style={{ margin: 0, maxHeight: 180, overflow: "auto", fontSize: 11, fontFamily: "var(--font-mono, monospace)" }}>
                  {JSON.stringify(detail?.current_payload ?? formValues, null, 2)}
                </pre>
              ),
            }]}
          />
        </>
      ) : (
        <Alert type="info" showIcon message={t("aiDraftNoPayload")} />
      )}
    </Modal>
  );
}
