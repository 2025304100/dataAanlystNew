import { useEffect, useRef, useState } from "react";
import { Alert, Button, Descriptions, Modal, Progress, Select, Space, Steps, Table, Tag, Tooltip, Typography } from "antd";
import { api, type ScoringModelBrief } from "../api/client";
import { t } from "../i18n";
import type { VerifiedInputBundle, InputReplayRequest, InputReplayTask, InputReplayReport, ReplayRow } from "../types/inputReplay";
import VerifiedInputBusinessStatus from "./VerifiedInputBusinessStatus";

const STORAGE = "verified_input_replay_request_v1";
const terminal = (status: string) => ["done", "failed", "cancelled", "partial"].includes(status);
const number = (value: number | null | undefined, digits = 6) => value == null ? "—" : value.toLocaleString(undefined, { maximumFractionDigits: digits });
const factorName = (code: string) => t(`inputReplay.factor.${code}`);

export default function VerifiedInputReplay() {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);
  const [bundles, setBundles] = useState<VerifiedInputBundle[]>([]);
  const [models, setModels] = useState<ScoringModelBrief[]>([]);
  const [bundleId, setBundleId] = useState<string>();
  const [modelId, setModelId] = useState<string>();
  const [pending, setPending] = useState<InputReplayRequest | null>(null);
  const [job, setJob] = useState<InputReplayTask | null>(null);
  const [report, setReport] = useState<InputReplayReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [query, setQuery] = useState("");
  const generation = useRef(0);

  useEffect(() => {
    if (!open) return;
    const current = ++generation.current;
    setBusy(true); setError(undefined); setReport(null); setJob(null); setPending(null); setStep(0);
    void Promise.all([api.listVerifiedInputBundles(), api.scoringListModels("any", 200)]).then(async ([items, allModels]) => {
      if (generation.current !== current) return;
      setBundles(items);
      const research = allModels.filter((m) => m.readiness_code === "MODEL_RESEARCH_DATASET_ONLY");
      setModels(research);
      let saved: InputReplayRequest | null = null;
      try { saved = JSON.parse(localStorage.getItem(STORAGE) || "null"); } catch { /* Ignore malformed local preference. */ }
      if (saved && /^[a-f0-9]{32}$/.test(saved.request_id)) {
        setPending(saved); setBundleId(saved.bundle_id); setModelId(saved.model_id);
        const next = await api.getInputReplayTask(saved.request_id);
        if (generation.current === current) { setJob(next); setStep(next.status === "done" ? 2 : 1); }
      } else {
        setBundleId(items[0]?.bundle_id);
        setModelId(research.find((m) => m.id === items[0]?.suggested_model_id)?.id || research[0]?.id);
      }
    }).catch((reason: Error) => { if (generation.current === current) setError(reason.message); })
      .finally(() => { if (generation.current === current) setBusy(false); });
    return () => { ++generation.current; };
  }, [open]);

  useEffect(() => {
    if (!open || !job || terminal(job.status)) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await api.getInputReplayTask(job.id);
        if (!stopped) { setJob(next); setError(undefined); }
        if (!stopped && !terminal(next.status)) timer = setTimeout(poll, 3000);
      } catch (reason) {
        if (!stopped) { setError(String(reason)); timer = setTimeout(poll, 5000); }
      }
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [open, job?.id, job?.status]);

  useEffect(() => {
    if (!open || job?.status !== "done") return;
    let stopped = false;
    void api.getInputReplayReport(job.id).then((next) => {
      if (!stopped) { setReport(next); setStep(2); setError(undefined); }
    }).catch((reason: Error) => { if (!stopped) setError(reason.message); });
    return () => { stopped = true; };
  }, [open, job?.id, job?.status]);

  const run = async () => {
    const bundle = bundles.find((item) => item.bundle_id === bundleId);
    if (!bundle || !modelId) return;
    const payload = pending || {bundle_id: bundle.bundle_id, manifest_sha256: bundle.manifest_sha256,
      model_id: modelId, request_id: crypto.randomUUID().replace(/-/g, "")};
    setBusy(true); setError(undefined); setPending(payload);
    localStorage.setItem(STORAGE, JSON.stringify(payload));
    try { const next = await api.createInputReplay(payload); setJob(next); setStep(1); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const reset = () => {
    localStorage.removeItem(STORAGE); setPending(null); setJob(null); setReport(null); setError(undefined); setStep(0);
  };
  const bundle = bundles.find((item) => item.bundle_id === bundleId);
  const inFlight = job && !terminal(job.status);
  const visibleRows = report?.rows.filter((row) => !query || row.symbol.includes(query)) || [];

  return <>
    <div style={{display: "flex", justifyContent: "space-between", gap: 16, alignItems: "center", padding: "12px 16px", marginBottom: 16, border: "1px solid var(--border-color, #e5e7eb)", borderRadius: 8}}>
      <div><b>{t("inputReplay.title")}</b><div style={{fontSize: 12, color: "var(--text-secondary)"}}>{t("inputReplay.subtitle")}</div></div>
      <Space wrap><VerifiedInputBusinessStatus/><Button onClick={() => setOpen(true)}>{t("inputReplay.open")}</Button></Space>
    </div>
    <Modal title={t("inputReplay.title")} open={open} onCancel={() => setOpen(false)} width={1080} footer={null}>
      <Steps size="small" current={step} items={[{title:t("inputReplay.select")}, {title:t("inputReplay.verify")}, {title:t("inputReplay.result")}]} style={{marginBottom: 20}} />
      <Alert type="info" showIcon message={t("inputReplay.boundary")} style={{marginBottom: 16}} />
      {error && <Alert type="error" showIcon message={error} style={{marginBottom: 12}} />}
      {step === 0 && <Space direction="vertical" style={{width: "100%"}} size={12}>
        <label>{t("inputReplay.bundle")}<Select aria-label={t("inputReplay.bundle")} style={{width:"100%"}} loading={busy} disabled={!!pending}
          value={bundleId} onChange={(id) => {setBundleId(id); const next=bundles.find((item) => item.bundle_id === id); if (next?.suggested_model_id) setModelId(next.suggested_model_id);}}
          options={bundles.map((item) => ({value:item.bundle_id,label:item.label}))} /></label>
        <label>{t("inputReplay.model")}<Select aria-label={t("inputReplay.model")} style={{width:"100%"}} loading={busy} disabled={!!pending} value={modelId} onChange={setModelId}
          options={models.map((item) => ({value:item.id,label:item.name}))} /></label>
        {bundle && <Descriptions size="small" bordered column={2} items={[
          {key:"fin",label:t("inputReplay.financial"),children:number(bundle.financial_rows,0)},
          {key:"flow",label:t("inputReplay.flow"),children:`${bundle.flow_symbols} · ${number(bundle.flow_rows,0)}`},
          {key:"sha",label:t("inputReplay.fingerprint"),span:2,children:<Typography.Text code copyable>{bundle.manifest_sha256}</Typography.Text>},
        ]} />}
        {(!bundles.length || !models.length) && !busy && <Alert type="warning" message={t("inputReplay.unavailable")} />}
        <Button type="primary" loading={busy} disabled={!bundle || !modelId} onClick={() => void run()}>{t(pending ? "inputReplay.retrySubmit" : "inputReplay.start")}</Button>
      </Space>}
      {step === 1 && job && <Space direction="vertical" style={{width: "100%"}}>
        <Typography.Text>{t(`inputReplay.stage.${job.stage}`)}</Typography.Text>
        <Progress percent={Math.round(job.percent)} status={inFlight ? "active" : job.status === "failed" ? "exception" : "normal"} />
        <Typography.Text type="secondary">{t("inputReplay.task")}: {job.id}</Typography.Text>
        {terminal(job.status) && job.status !== "done" && <><Alert type="error" message={t("inputReplay.failed")} description={job.message} /><Button onClick={reset}>{t("inputReplay.restart")}</Button></>}
      </Space>}
      {step === 2 && !report && <Alert type="info" message={t("inputReplay.loadingReport")} />}
      {step === 2 && report && <Space direction="vertical" style={{width:"100%"}} size={14}>
        <Alert type="warning" showIcon message={t("inputReplay.research")} description={t("inputReplay.scoreMeaning")} />
        <Descriptions size="small" bordered column={3} items={[
          {key:"model",label:t("inputReplay.model"),span:3,children:report.model.label},
          {key:"date",label:t("inputReplay.date"),children:report.last_date},
          {key:"cohort",label:t("inputReplay.cohort"),children:report.frozen_symbol_count},
          {key:"scored",label:t("inputReplay.scored"),children:`${report.scored_symbols}/${report.frozen_symbol_count}`},
          {key:"bars",label:t("inputReplay.bars"),children:number(report.input_counts.raw_daily_bars,0)},
          {key:"financial",label:t("inputReplay.financial"),children:number(report.input_counts.raw_financial_reports,0)},
          {key:"flow",label:t("inputReplay.flow"),children:number(report.input_counts.raw_fund_flows,0)},
        ]} />
        <Table rowKey={(r) => `${r.code}:${r.version}`} size="small" pagination={false} dataSource={report.coverage} columns={[
          {title:t("inputReplay.factor"),render:(_,r) => <Tooltip title={t(`inputReplay.hint.${r.code}`)}>{factorName(r.code)} · v{r.version} {r.model_feature && <Tag>{t("inputReplay.feature")}</Tag>}</Tooltip>},
          {title:t("inputReplay.coverage"),render:(_,r) => `${r.eligible}/${r.denominator}`},
        ]} />
        <Typography.Text type="secondary">{t("inputReplay.gaps")}: {report.gaps.no_flow_symbols.length} · {t("inputReplay.valuationAbsent")}</Typography.Text>
        <Select showSearch aria-label={t("inputReplay.search")} placeholder={t("inputReplay.search")} allowClear style={{width:230}} value={query || undefined}
          onChange={(value) => setQuery(value || "")} options={report.rows.map((r) => ({value:r.symbol,label:r.symbol}))} />
        <Table<ReplayRow> rowKey="symbol" size="small" dataSource={visibleRows} pagination={{pageSize:10,showSizeChanger:false}} columns={[
          {title:t("inputReplay.symbol"),dataIndex:"symbol"},
          {title:t("inputReplay.percentile"),render:(_,r) => number(r.score,2)},
          {title:t("inputReplay.signal"),render:(_,r) => number(r.linear_signal)},
          {title:t("inputReplay.state"),render:(_,r) => <Tag color={r.score == null ? "warning" : "success"}>{t(r.score == null ? "inputReplay.missing" : "inputReplay.calculated")}</Tag>},
        ]} expandable={{expandedRowRender:(row) => <>
          <Typography.Paragraph>{t("inputReplay.intercept")}: {number(report.model.intercept,10)}</Typography.Paragraph>
          {Object.entries(row.features).map(([code,f]) => <p key={code}>{factorName(code)} · v{f.version}: {number(f.normalized_value)} × {number(f.coefficient)} = {number(f.contribution)} · {t("inputReplay.raw")}: {number(f.raw_value)}</p>)}
          <Table rowKey={(r) => `${r.code}:${r.version}`} size="small" pagination={false} dataSource={row.factor_observations} columns={[
            {title:t("inputReplay.factor"),render:(_,r) => `${factorName(r.code)} · v${r.version}`},
            {title:t("inputReplay.raw"),render:(_,r) => number(r.raw_value)},
            {title:t("inputReplay.normalized"),render:(_,r) => number(r.normalized_value)},
          ]} />
        </>}} />
        <Typography.Text type="secondary">{t("inputReplay.fingerprint")}: {report.bundle_manifest_sha256}<br/>{t("inputReplay.datasetFingerprint")}: {report.dataset_manifest_sha256}</Typography.Text>
        <Button onClick={reset}>{t("inputReplay.restart")}</Button>
      </Space>}
    </Modal>
  </>;
}
