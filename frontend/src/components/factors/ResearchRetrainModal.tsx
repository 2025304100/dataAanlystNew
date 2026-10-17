import { useEffect, useRef, useState } from "react";
import { Alert, Button, Descriptions, Input, Modal, Spin } from "antd";
import { api, type FactorPipelineTask, type ResearchRetrainPlan } from "../../api/client";
import { enumLabel, t } from "../../i18n";
import { clearPendingResearch, readPendingResearch, savePendingResearch, type PendingResearchRequest } from "../../utils/researchRequest";

type Props = { modelId: string | null; onClose: () => void; onSubmitted: (task: FactorPipelineTask) => void };

export default function ResearchRetrainModal({ modelId, onClose, onSubmitted }: Props) {
  const [plan, setPlan] = useState<ResearchRetrainPlan | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [pending, setPending] = useState<PendingResearchRequest | null>(null);
  const [recoveredTask, setRecoveredTask] = useState<FactorPipelineTask | null>(null);
  const [recovering, setRecovering] = useState(false);
  const [storageBlocked, setStorageBlocked] = useState(false);
  const generation = useRef(0);
  const inFlight = useRef(false);

  const recover = async (request: PendingResearchRequest, current: number) => {
    setRecovering(true);
    try {
      const result = await api.scoringRecoverResearchRetrain(request.modelId, request.requestId);
      if (generation.current !== current) return;
      if (result.model_id !== request.modelId || result.request_id !== request.requestId
          || !["found", "not_found"].includes(result.state)) throw new Error(t("factorResearchPlanMismatch"));
      if (result.state === "found") {
        if (!result.task?.id || result.manifest_sha256 !== request.manifestSha256 || result.note !== request.note) throw new Error(t("factorResearchPlanMismatch"));
        setRecoveredTask(result.task); setError(null);
      } else { setRecoveredTask(null); setError(t("factorResearchRequestNotFound")); }
    } catch (err) {
      if (generation.current === current) { setRecoveredTask(null); setError(t("factorResearchRequestUnknown") + " " + String(err)); }
    } finally { if (generation.current === current) setRecovering(false); }
  };

  useEffect(() => {
    const current = ++generation.current;
    setPlan(null); setError(null); setNote(""); setPending(null); setRecoveredTask(null); setStorageBlocked(false); setRecovering(false);
    if (!modelId) { setLoading(false); return; }
    try {
      const previous = readPendingResearch(modelId);
      if (previous) {
        setPending(previous); setNote(previous.note); setLoading(false);
        void recover(previous, current);
        return () => { generation.current += 1; };
      }
    } catch {
      setStorageBlocked(true); setError(t("factorResearchStorageBlocked")); setLoading(false);
      return () => { generation.current += 1; };
    }
    setLoading(true);
    void Promise.resolve().then(() => api.scoringGetResearchRetrainPlan(modelId)).then(value => {
      if (generation.current !== current) return;
      if (value.model_id !== modelId || value.research_only !== true || value.independent_validation !== false
          || value.auto_activate !== false || value.materialize_scores !== false
          || !/^[a-f0-9]{64}$/.test(value.manifest_sha256)
          || !/^[a-z0-9][a-z0-9_-]{0,79}$/.test(value.dataset_id)
          || !value.factor_set_id || value.window_days !== 250 || value.validation_days !== 50
          || ![value.symbol_count, value.trade_date_count, value.bar_count].every(n => Number.isInteger(n) && n > 0)) throw new Error(t("factorResearchPlanMismatch"));
      setPlan(value);
    }).catch(err => {
      if (generation.current === current) setError(String(err));
    }).finally(() => { if (generation.current === current) setLoading(false); });
    return () => { generation.current += 1; };
  }, [modelId]);

  const submit = async () => {
    if (inFlight.current || recovering || storageBlocked || !modelId) return;
    if (pending && pending.modelId !== modelId) return;
    if (recoveredTask && pending?.modelId === modelId) {
      try { clearPendingResearch(pending); } catch { setError(t("factorResearchStorageBlocked")); return; }
      onSubmitted(recoveredTask); return;
    }
    if (!pending && (!plan || plan.model_id !== modelId)) return;
    let request: PendingResearchRequest;
    try {
      // Read again to preserve another entry point/tab's pending submission.
      const previous = readPendingResearch(modelId);
      if (previous && (!pending || previous.requestId !== pending.requestId)) {
        setPending(previous); setRecoveredTask(null); setPlan(null); setNote(previous.note);
        void recover(previous, generation.current); return;
      }
      request = previous ?? pending ?? { version: 1, modelId,
        requestId: crypto.randomUUID().replace(/-/g, ""), manifestSha256: plan!.manifest_sha256, note: note.trim() };
      if (!pending && request.manifestSha256 !== plan!.manifest_sha256) throw new Error("research_request_already_pending");
      savePendingResearch(request);
      setPending(request);
    } catch {
      setError(t("factorResearchStorageBlocked")); setStorageBlocked(true); return;
    }
    inFlight.current = true; setSubmitting(true); setError(null);
    const current = generation.current;
    try {
      const task = await api.scoringRetrainResearchModel(request.modelId, request.manifestSha256, request.note, request.requestId);
      if (!task?.id) throw new Error(t("factorResearchPlanMismatch"));
      if (generation.current === current) { clearPendingResearch(request); onSubmitted(task); }
    } catch {
      if (generation.current === current) { setError(t("factorResearchRequestUnknown")); await recover(request, current); }
    } finally { inFlight.current = false; setSubmitting(false); }
  };
  return <Modal open={Boolean(modelId)} title={t("factorResearchRetrainTitle")} width={650}
    onCancel={() => { if (!inFlight.current) onClose(); }} onOk={() => void submit()}
    okText={t(recoveredTask ? "factorResearchViewRecovered" : pending ? "factorResearchRetrySameRequest" : "factorResearchRetrainSubmit")} cancelText={t("cancel")} confirmLoading={submitting}
    okButtonProps={{ disabled: loading || recovering || storageBlocked || (!pending && (!plan || plan.model_id !== modelId)) }}
    cancelButtonProps={{ disabled: submitting }} maskClosable={!submitting} closable={!submitting}>
    <Alert type="warning" showIcon message={t("factorResearchRetrainWarning")} description={t("factorResearchRetrainHelp")} style={{ marginBottom: 16 }} />
    {loading && <div role="status" style={{ display: "flex", gap: 12, alignItems: "center", padding: "16px 0" }}><Spin /><span>{t("factorResearchChecking")}</span></div>}
    {pending && <Alert type={recoveredTask ? "success" : "warning"} showIcon message={t(recoveredTask ? "factorResearchRequestFound" : "factorResearchRequestPending")}
      description={<><p>{t("factorResearchRequestHelp")}</p><p>{pending.requestId}</p>{recoveredTask && <p>{recoveredTask.id} · {enumLabel("taskStatus", recoveredTask.status)}</p>}</>}
      action={!submitting && !recoveredTask ? <Button size="small" loading={recovering} onClick={() => void recover(pending, generation.current)}>{t("factorResearchCheckRequest")}</Button> : undefined} />}
    {recovering && <div role="status"><Spin /> {t("factorResearchCheckingRequest")}</div>}
    {error && <Alert type={pending ? "warning" : "error"} showIcon message={t(pending ? "factorResearchRecoveryStatus" : "factorResearchRetrainBlocked")} description={error} />}
    {plan && <>
      <Descriptions bordered column={1} size="small" style={{ overflowWrap: "anywhere", marginBottom: 16 }}>
        <Descriptions.Item label={t("factorResearchDataset")}>{plan.dataset_id}</Descriptions.Item>
        <Descriptions.Item label={t("factorResearchRange")}>{plan.start_date} → {plan.end_date}</Descriptions.Item>
        <Descriptions.Item label={t("factorResearchSamples")}>{plan.symbol_count} / {plan.trade_date_count} / {plan.bar_count.toLocaleString()}</Descriptions.Item>
        <Descriptions.Item label={t("factorResearchSet")}>{plan.factor_set_id}</Descriptions.Item>
        <Descriptions.Item label={t("factorResearchFingerprint")}><span style={{ wordBreak: "break-all" }}>{plan.manifest_sha256}</span></Descriptions.Item>
      </Descriptions>
      <Input.TextArea aria-label={t("factorResearchNote")} placeholder={t("factorResearchNote")} value={note} disabled={Boolean(pending) || submitting} onChange={event => setNote(event.target.value)} maxLength={300} rows={2} />
    </>}
  </Modal>;
}
