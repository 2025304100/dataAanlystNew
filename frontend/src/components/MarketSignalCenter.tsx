import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Drawer, Modal, Space, Spin, Tag, Tabs, Descriptions, Progress } from "antd";
import { useApp } from "../context/AppContext";
import { signalApi } from "../api/marketSignals";
import { signalLabels } from "../i18n/marketSignals";
import { t } from "../i18n";
import type { SignalAssessment, SignalDashboard, SignalEvidence, SignalJob, SignalMetadata, SignalPage, SignalPrediction, SignalRule, SignalRuleDraft, SignalSettings } from "../types/marketSignal";
import "../styles/marketSignals.css";

const defaults: SignalRuleDraft = { name: "", target_type: "symbol", target_key: "", quote_key: null, quote_type: "symbol",
  min_score: 85, window_days: 3, min_move_pct: 2, adaptive_threshold: true, volatility_percentile: 80,
  max_age_hours: 24, cooldown_hours: 6, max_daily: 1, delivery_mode: "instant", digest_time: "16:10",
  mode: "shadow", enabled: true, channel_ids: [] };
const percentage = (value: number | null | undefined) => value == null ? "—" : `${(value*100).toFixed(1)}%`;
const stamp = (value: string | null | undefined, locale: string) => {
  if (!value) return "—";
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value+"Z";
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale, { timeZone: "Asia/Shanghai", hour12: false });
};
const safeLink = (value: string | null | undefined) => value && /^https?:\/\//i.test(value) ? value : undefined;

export default function MarketSignalCenter({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { locale } = useApp();
  const L = signalLabels(locale);
  const [metadata, setMetadata] = useState<SignalMetadata | null>(null);
  const [rules, setRules] = useState<SignalRule[]>([]);
  const [settings, setSettings] = useState<SignalSettings | null>(null);
  const [dashboard, setDashboard] = useState<SignalDashboard | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState<number | null | undefined>(undefined);
  const [draft, setDraft] = useState<SignalRuleDraft>({ ...defaults });
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [evidence, setEvidence] = useState<SignalPage<SignalEvidence>>({ total: 0, items: [] });
  const [assessments, setAssessments] = useState<SignalPage<SignalAssessment>>({ total: 0, items: [] });
  const [predictions, setPredictions] = useState<SignalPage<SignalPrediction>>({ total: 0, items: [] });
  const [evidenceOffset, setEvidenceOffset] = useState(0);
  const [assessmentOffset, setAssessmentOffset] = useState(0);
  const [predictionOffset, setPredictionOffset] = useState(0);
  const [job, setJob] = useState<SignalJob | null>(null);
  const [notice, setNotice] = useState("");
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [article, setArticle] = useState({ title: "", text: "", source_url: "", published_at: "" });
  const [detail, setDetail] = useState<{ title: string; values: Record<string, unknown> } | null>(null);
  const busy = !!job && !["done", "partial", "failed", "cancelled"].includes(job.status);
  const request = useRef(0);
  const alive = useRef(false);
  const initialized = useRef(false);
  const [activeTab, setActiveTab] = useState("rules");
  const load = useCallback(async () => {
    const sequence = ++request.current;
    setLoading(true);
    try {
      const [meta, nextRules, nextSettings, stats, nextEvidence, nextAssessments, nextPredictions] = await Promise.all([
        signalApi.metadata(), signalApi.rules(), signalApi.settings(), signalApi.dashboard(),
        signalApi.evidence(evidenceOffset), signalApi.assessments(assessmentOffset), signalApi.predictions(predictionOffset),
      ]);
      if (!alive.current || sequence !== request.current) return;
      setMetadata(meta); setRules(nextRules); setSettings(nextSettings); setDashboard(stats); setError("");
      if (!initialized.current) { setActiveTab(nextRules.length ? "predictions" : "rules"); initialized.current = true; }
      setEvidence(nextEvidence); setAssessments(nextAssessments); setPredictions(nextPredictions);
    } catch (cause) { if (alive.current && sequence === request.current) setError(String(cause)); }
    finally { if (alive.current && sequence === request.current) setLoading(false); }
  }, [evidenceOffset, assessmentOffset, predictionOffset]);
  useEffect(() => {
    alive.current = open;
    if (open) void load();
    return () => { alive.current = false; request.current++; };
  }, [open, load]);
  useEffect(() => {
    if (!open || !search) return;
    let current = true;
    const timer = window.setTimeout(() => { void signalApi.metadata(search).then(value => {
      if (current && alive.current) setMetadata(previous => previous ? { ...previous, symbols: value.symbols } : value);
    }).catch(cause => { if (current) setError(String(cause)); }); }, 250);
    return () => { current = false; window.clearTimeout(timer); };
  }, [open, search]);
  useEffect(() => {
    if (!open || !job || ["done", "partial", "failed", "cancelled"].includes(job.status)) return;
    let current = true;
    let timer: number;
    const poll = async () => {
      try {
        const next = await signalApi.job(job.id);
        if (!current) return;
        setJob(next);
        if (["done", "partial", "failed", "cancelled"].includes(next.status)) {
          void load().then(() => {
            if (!alive.current) return;
            if (next.status === "failed") setError(next.message || L.error);
            else setNotice(next.message || L.complete);
          });
        } else timer = window.setTimeout(() => void poll(), 1000);
      } catch (cause) { if (current) { setError(String(cause)); timer = window.setTimeout(() => void poll(), 2500); } }
    };
    timer = window.setTimeout(() => void poll(), 800);
    return () => { current = false; window.clearTimeout(timer); };
  }, [open, job?.id, job?.status, load, L.error, L.complete]);
  const startJob = async (payload: Record<string, unknown>) => {
    try { setNotice(""); setError(""); setJob(await signalApi.startJob(payload)); }
    catch (cause) { setError(String(cause)); }
  };
  const saveEvidence = async (event: React.FormEvent) => {
    event.preventDefault(); setSaving(true);
    try {
      await signalApi.addEvidence({ ...article, source_url: article.source_url || null,
        published_at: article.published_at ? new Date(article.published_at+"+08:00").toISOString() : null });
      setEvidenceOpen(false); setEvidenceOffset(0); await load();
    } catch (cause) { setError(String(cause)); }
    finally { setSaving(false); }
  };
  const showVersions = async (rule: SignalRule) => {
    try { setDetail({ title: `${rule.name} · ${L.revision}`, values: { [L.history]: await signalApi.ruleVersions(rule.id) } }); }
    catch (cause) { setError(String(cause)); }
  };
  const recheck = async (row: SignalPrediction) => {
    try { await signalApi.recheck(row.id); const versions = await signalApi.outcomeVersions(row.id);
      setDetail({ title: L.review, values: { [L.history]: versions } }); await load(); }
    catch (cause) { setError(String(cause)); }
  };
  const pager = (offset: number, total: number, update: (value: number) => void) => <Space>
    <Button disabled={offset === 0 || loading} onClick={() => update(Math.max(0, offset-50))}>{L.previous}</Button>
    <span>{total === 0 ? "0 / 0" : `${offset+1}–${Math.min(offset+50,total)} / ${total}`}</span>
    <Button disabled={offset+50 >= total || loading} onClick={() => update(offset+50)}>{L.next}</Button></Space>;
  const stateLabel = (value: string) => ({
    shadow: L.shadowMode, scored: L.complete, pending: L.pending, hit: L.hit, miss: L.miss,
    unverifiable: L.missing, queued: L.queued, waiting_digest: L.digest, sent: L.live,
    not_applicable: "—", suppressed: L.paused, failed: L.error,
    historical_target_met: L.historical_target_met, below_target: L.below_target,
    not_verified: L.not_verified, data_incomplete: L.data_incomplete,
    supplier: L.supplierTime, manual: L.manualTime, date_only: L.dateOnly, unknown: L.unknownTime, legacy_unknown: L.unknownTime,
    unrelated: L.unrelated, insufficient_evidence: L.insufficient, invalid_response: L.invalid, unavailable: L.unavailable, calendar: L.calendar,
  }[value] || value);
  const analysisDetails = (analysis: Record<string, unknown>) => ({
    [L.mechanism]: analysis.mechanism || analysis.reason,
    [L.facts]: Array.isArray(analysis.new_facts) ? analysis.new_facts.join("；") : "—",
    [L.material]: `${analysis.material_points ?? "—"} / 40`, [L.novelty]: `${analysis.novelty_points ?? "—"} / 30`,
    [L.breadth]: `${analysis.breadth_points ?? "—"} / 10`, [L.duration]: `${analysis.duration_points ?? "—"} / 10`,
    [L.timeliness]: `${analysis.time_points ?? "—"} / 10`,
    [L.evidenceText]: Array.isArray(analysis.evidence_quotes) ? analysis.evidence_quotes.join("\n") : "—",
  });
  const change = <K extends keyof SignalRuleDraft>(key: K, value: SignalRuleDraft[K]) => setDraft(previous => ({ ...previous, [key]: value }));
  const saveRule = async (event: React.FormEvent) => {
    event.preventDefault(); setSaving(true);
    try {
      if (editing == null) await signalApi.createRule(draft); else await signalApi.updateRule(editing, draft);
      setEditing(undefined); setActiveTab("rules"); await load();
    } catch (cause) { setError(String(cause)); }
    finally { setSaving(false); }
  };
  const toggle = async (rule: SignalRule) => {
    try { await signalApi.updateRule(rule.id, { enabled: !rule.enabled }); await load(); }
    catch (cause) { setError(String(cause)); }
  };
  const editRule = (rule?: SignalRule) => {
    setDraft(rule ? Object.fromEntries(Object.keys(defaults).map(key => [key, rule[key as keyof SignalRuleDraft]])) as unknown as SignalRuleDraft : { ...defaults });
    setEditing(rule?.id ?? null);
  };
  const saveSettings = async () => {
    if (!settings) return;
    setSaving(true);
    try { await signalApi.saveSettings(settings); setSettingsOpen(false); await load(); }
    catch (cause) { setError(String(cause)); }
    finally { setSaving(false); }
  };
  const sectorName = (key: string) => {
    const label = t(`market_news_sector_${key}`);
    return label.startsWith("market_news_sector_") ? key : label;
  };
  return <Drawer open={open} onClose={onClose} title={L.title} width="96vw" destroyOnHidden>
    <div className="msc-page">
      <div className="msc-heading"><div><h2>{L.intro}</h2><p>{L.target}</p></div><Space wrap>
        <Button onClick={() => void load()} loading={loading}>{L.refresh}</Button>
        <Button onClick={() => setSettingsOpen(true)}>{L.settings}</Button>
        <Button loading={busy} onClick={() => settings?.enabled ? void startJob({ kind: "monitor", analyze: true }) : setSettingsOpen(true)}>{L.run}</Button>
        <Button type="primary" onClick={() => editRule()}>{L.newRule}</Button>
      </Space></div>
      <Alert type="info" showIcon message={L.trialNotice} description={L.startHelp} />
      {error && <Alert type="error" showIcon closable onClose={() => setError("")} message={error} />}
      {notice && <Alert type="success" showIcon closable onClose={() => setNotice("")} message={notice} />}
      {job && <div><span>{L.job}: {job.percent}% · {job.message}</span><Progress percent={job.percent} status={job.status === "failed" ? "exception" : busy ? "active" : "success"} />
        {job.result != null && <details><summary>{L.result}</summary><pre className="msc-detail">{JSON.stringify(job.result, null, 2)}</pre></details>}</div>}
      <Spin spinning={loading}>
        <div className="msc-stats">
          <Card size="small" title={L.live}><strong>{dashboard?.live.precision == null ? L.unverified : percentage(dashboard.live.precision)}</strong><p>{L.matured}: {dashboard?.live.matured ?? 0} · {L.missing}: {dashboard?.live.unverifiable ?? 0}</p></Card>
          <Card size="small" title={L.shadow}><strong>{percentage(dashboard?.shadow.precision)}</strong><p>{L.matured}: {dashboard?.shadow.matured ?? 0} · {L.pending}: {dashboard?.shadow.pending ?? 0}</p></Card>
          <Card size="small" title={L.queued}><strong>{dashboard?.queued ?? 0}</strong><p>{L.history}</p></Card>
        </div>
        <Tabs activeKey={activeTab} onChange={setActiveTab} items={[{ key: "rules", label: L.rules, children: <Card title={L.rules} extra={<Tag color={settings?.enabled ? "green" : "default"}>{settings?.enabled ? L.enabled : L.paused}</Tag>}>
          <div className="msc-table-wrap"><table className="msc-table"><thead><tr>
            <th>{L.name}</th><th>{L.type}</th><th>{L.score}</th><th>{L.window}</th><th>{L.mode}</th><th>{L.readiness}</th><th>{L.actions}</th>
          </tr></thead><tbody>{rules.map(rule => {
            const ready = metadata?.readiness.find(value => value.rule_id === rule.id);
            return <tr key={rule.id}><td>{rule.name}<small>{rule.target_type === "sector" ? sectorName(rule.target_key) : rule.target_name} · v{rule.revision}</small></td>
              <td>{rule.target_type === "sector" ? L.sector : L.stock}</td><td>{rule.min_score}</td><td>{rule.window_days} {L.days}<small>≥{rule.min_move_pct}%</small></td>
              <td>{rule.mode === "shadow" ? L.shadowMode : L.trialMode}</td><td title={ready?.reason ?? undefined}>{ready?.ready ? L.ready : ready?.reason ?? L.notReady}</td>
              <td><Space wrap><Button size="small" aria-label={`${L.edit} ${rule.name}`} onClick={() => editRule(rule)}>{L.edit}</Button><Button size="small" aria-label={`${rule.enabled ? L.paused : L.enabled} ${rule.name}`} onClick={() => void toggle(rule)}>{rule.enabled ? L.paused : L.enabled}</Button><Button size="small" onClick={() => void showVersions(rule)}>{L.inspectHistory}</Button></Space></td></tr>;
          })}</tbody></table>{rules.length === 0 && <p className="msc-empty">{L.noData}</p>}</div>
          <div className="msc-toolbar"><Button loading={busy} onClick={() => void startJob({ kind: "sync_data" })}>{L.sync}</Button><small>{metadata?.schedules.some(row => row.enabled) ? L.globalEnabled : L.noSchedule}</small></div>
          {dashboard?.rules.map(row => <p key={row.rule_id}>{row.name} v{row.revision} · {L.live}: {percentage(row.live.precision)} ({row.live.hits}/{row.live.matured}) · {stateLabel(row.live.validation_status)} · {L.shadow}: {percentage(row.shadow.precision)} · {L.controls}: {percentage(row.live.historical_baseline_rate)}</p>)}
        </Card> }, { key: "evidence", label: L.evidence, children: <>
          <div className="msc-toolbar"><Space wrap><Button onClick={() => { setArticle({ title: "", text: "", source_url: "", published_at: "" }); setEvidenceOpen(true); }}>{L.addEvidence}</Button>
            <Button loading={busy} onClick={() => void startJob({ kind: "import_legacy" })}>{L.importLegacy}</Button></Space>{pager(evidenceOffset, evidence.total, setEvidenceOffset)}</div>
          <div className="msc-card-list">{evidence.items.map(row => <Card key={row.id} size="small"><h3>{row.title}</h3><p>{stamp(row.published_at, locale)} · {row.source} · {stateLabel(row.time_quality)}</p><p>{row.text.slice(0, 450)}</p><Space wrap>
            <Button aria-label={`${L.analyze} ${row.title}`} loading={busy} onClick={() => void startJob({ kind: "analyze", evidence_ids: [row.id], retry_failed: true })}>{L.analyze}</Button>
            {safeLink(row.source_url) && <><a href={safeLink(row.source_url)} target="_blank" rel="noreferrer">{L.source}</a><Button loading={busy} onClick={() => void startJob({ kind: "enrich", evidence_id: row.id })}>{L.enrich}</Button></>}
            <Button onClick={() => { setArticle({ title: row.title, text: row.text, source_url: row.source_url || "", published_at: "" }); setEvidenceOpen(true); }}>{L.addEvidence}</Button>
          </Space></Card>)}</div>
          <div className="msc-toolbar"><h3>{L.evidence}</h3>{pager(assessmentOffset, assessments.total, setAssessmentOffset)}</div>
          <div className="msc-card-list">{assessments.items.map(row => <Card key={row.id} size="small"><Space><Tag>{stateLabel(row.status)}</Tag><strong>{L.scoreLabel}: {row.score ?? "—"}</strong><span>{L.revision}: {row.rule_revision}</span></Space>
            <p>{String(row.analysis.mechanism || row.analysis.reason || "")}</p><p>{row.decision_reason}</p><p>{Array.isArray(row.analysis.evidence_quotes) ? row.analysis.evidence_quotes.join("；") : ""}</p>
            <small>{L.model}: {row.model_name || "—"} · {L.tokens}: {row.total_tokens}</small><Button type="link" onClick={() => setDetail({ title: L.detail, values: { ...analysisDetails(row.analysis), [L.reason]: row.decision_reason, [L.model]: row.model_name } })}>{L.detail}</Button>
          </Card>)}</div>
        </> }, { key: "predictions", label: L.predictions, children: <>
          <div className="msc-toolbar"><p>{L.history}</p>{pager(predictionOffset, predictions.total, setPredictionOffset)}</div>
          <div className="msc-card-list">{predictions.items.map(row => <Card key={row.id} size="small"><h3>{row.snapshot.evidence.title}</h3><Space wrap><Tag>{stateLabel(row.delivery_status)}</Tag><Tag color={row.outcome_status === "hit" ? "green" : row.outcome_status === "miss" ? "orange" : "default"}>{stateLabel(row.outcome_status)}</Tag><Tag>{L.scoreLabel}: {row.snapshot.score}</Tag><span>{row.snapshot.rule.target_name || row.target_key} · v{row.rule_revision}</span></Space>
            <p>{String(row.snapshot.analysis.mechanism || "")}</p>
            <p>{L.window}: {row.snapshot.rule.window_days} {L.days} · {L.move}: {row.snapshot.threshold_pct.toFixed(2)} · {L.result}: {row.outcome.max_move_pct == null ? "—" : `${row.outcome.max_move_pct.toFixed(2)}%`}</p>
            <p>{stamp(row.sent_at, locale)} → {stamp(row.due_at, locale)}</p>{row.outcome.reason && <p>{row.outcome.reason}</p>}
            <Space><Button onClick={() => setDetail({ title: L.snapshot, values: { [L.evidenceTitle]: row.snapshot.evidence.title,
              ...analysisDetails(row.snapshot.analysis), [L.window]: row.snapshot.observation_dates.join(" / "),
              [L.move]: row.snapshot.threshold_pct, [L.source]: row.snapshot.evidence.source_url,
              [L.result]: row.outcome, [L.revision]: row.rule_revision } })}>{L.detail}</Button>
              {row.outcome_status !== "pending" && row.outcome_status !== "not_applicable" && <Button onClick={() => void recheck(row)}>{L.review}</Button>}</Space>
          </Card>)}</div>
        </> }]} />
      </Spin>
    </div>
    <Modal open={editing !== undefined} onCancel={() => setEditing(undefined)} title={L.newRule} footer={null} width={820} destroyOnHidden>
      <form onSubmit={event => void saveRule(event)}><p>{L.ruleHelp}</p><div className="msc-form-grid">
        <label>{L.name}<input aria-label={L.name} required maxLength={80} value={draft.name} onChange={event => change("name", event.target.value)} /></label>
        <label>{L.type}<select aria-label={L.type} value={draft.target_type} onChange={event => setDraft(previous => ({ ...previous, target_type: event.target.value as "symbol" | "sector", target_key: "", quote_type: event.target.value === "sector" ? "index" : "symbol", quote_key: null }))}><option value="symbol">{L.stock}</option><option value="sector">{L.sector}</option></select></label>
        {draft.target_type === "symbol" ? <><label>{L.search}<input aria-label={L.search} value={search} onChange={event => setSearch(event.target.value)} /></label>
          <label>{L.stockTarget}<select aria-label={L.stockTarget} required value={draft.target_key} onChange={event => change("target_key", event.target.value)}><option value="">{L.choose}</option>
            {draft.target_key && !metadata?.symbols.some(item => item.key === draft.target_key) && <option value={draft.target_key}>{draft.target_key} {rules.find(item => item.target_key === draft.target_key)?.target_name}</option>}
            {metadata?.symbols.map(item => <option key={item.key} value={item.key}>{item.key} {item.name}</option>)}</select></label></> : <>
          <label>{L.sectorTarget}<select aria-label={L.sectorTarget} required value={draft.target_key} onChange={event => change("target_key", event.target.value)}><option value="">{L.choose}</option>{metadata?.sectors.map(item => <option key={item.key} value={item.key}>{sectorName(item.key)}</option>)}</select></label>
          <label>{L.quoteType}<select aria-label={L.quoteType} value={draft.quote_type} onChange={event => change("quote_type", event.target.value as "index" | "symbol")}><option value="index">{L.index}</option><option value="symbol">ETF / {L.stock}</option></select></label>
          <label>{L.quoteKey}<input aria-label={L.quoteKey} required maxLength={32} value={draft.quote_key ?? ""} onChange={event => change("quote_key", event.target.value)} /><small>{L.quoteHint}</small></label></>}
        <label>{L.score}<input aria-label={L.score} type="number" min={0} max={100} required value={draft.min_score} onChange={event => change("min_score", Number(event.target.value))} /></label>
        <label>{L.window}<select aria-label={L.window} value={draft.window_days} onChange={event => change("window_days", Number(event.target.value) as 1 | 3 | 5)}>{[1,3,5].map(value => <option value={value} key={value}>{value} {L.days}</option>)}</select></label>
        <label>{L.move}<input aria-label={L.move} type="number" min={0.01} max={50} step={0.01} required value={draft.min_move_pct} onChange={event => change("min_move_pct", Number(event.target.value))} /></label>
        <label className="msc-check"><input type="checkbox" checked={draft.adaptive_threshold} onChange={event => change("adaptive_threshold", event.target.checked)} />{L.adaptive}</label>
        {draft.adaptive_threshold && <label>{L.percentile}<input aria-label={L.percentile} type="number" min={50} max={99} value={draft.volatility_percentile} onChange={event => change("volatility_percentile", Number(event.target.value))} /></label>}
        <label>{L.age}<input aria-label={L.age} type="number" min={1} max={168} value={draft.max_age_hours} onChange={event => change("max_age_hours", Number(event.target.value))} /></label>
        <label>{L.cooldown}<input aria-label={L.cooldown} type="number" min={0} max={168} value={draft.cooldown_hours} onChange={event => change("cooldown_hours", Number(event.target.value))} /></label>
        <label>{L.daily}<input aria-label={L.daily} type="number" min={1} max={10} value={draft.max_daily} onChange={event => change("max_daily", Number(event.target.value))} /></label>
        <label>{L.mode}<select aria-label={L.mode} value={draft.mode} onChange={event => change("mode", event.target.value as "shadow" | "trial")}><option value="shadow">{L.shadowMode}</option><option value="trial">{L.trialMode}</option></select></label>
        <label>{L.delivery}<select aria-label={L.delivery} value={draft.delivery_mode} onChange={event => change("delivery_mode", event.target.value as "instant" | "digest")}><option value="instant">{L.instant}</option><option value="digest">{L.digest}</option></select></label>
        {draft.delivery_mode === "digest" && <label>{L.digestTime}<input aria-label={L.digestTime} type="time" required value={draft.digest_time} onChange={event => change("digest_time", event.target.value)} /></label>}
        <label>{L.channels}<select multiple aria-label={L.channels} value={draft.channel_ids.map(String)} onChange={event => change("channel_ids", Array.from(event.target.selectedOptions, item => Number(item.value)))}>{metadata?.channels.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
      </div><Space><Button htmlType="submit" type="primary" loading={saving}>{L.saveRule}</Button><Button onClick={() => setEditing(undefined)}>{L.cancel}</Button></Space></form>
    </Modal>
    <Modal open={settingsOpen} onCancel={() => setSettingsOpen(false)} title={L.settings} footer={null} destroyOnHidden>
      {settings && <div className="msc-form-grid"><label>{L.profile}<select aria-label={L.profile} value={settings.ai_profile_id ?? ""} onChange={event => setSettings({ ...settings, ai_profile_id: event.target.value ? Number(event.target.value) : null })}><option value="">{L.choose}</option>{metadata?.ai_profiles.map(item => <option key={item.id} value={item.id} disabled={!item.enabled}>{item.name} ({item.model})</option>)}</select></label>
        <label className="msc-check"><input type="checkbox" checked={settings.enabled} onChange={event => setSettings({ ...settings, enabled: event.target.checked })} />{L.globalEnabled}</label>
        <label className="msc-check"><input type="checkbox" checked={settings.auto_analyze} onChange={event => setSettings({ ...settings, auto_analyze: event.target.checked })} />{L.autoAnalyze}</label>
        <label className="msc-check"><input type="checkbox" checked={settings.collect_on_monitor ?? true} onChange={event => setSettings({ ...settings, collect_on_monitor: event.target.checked })} />{L.autoCollect}</label>
        <label>{L.globalLimit}<input type="number" min={1} max={20} value={settings.global_daily_limit} onChange={event => setSettings({ ...settings, global_daily_limit: Number(event.target.value) })} /></label>
        <label>{L.batch}<input type="number" min={1} max={30} value={settings.max_analysis_per_run} onChange={event => setSettings({ ...settings, max_analysis_per_run: Number(event.target.value) })} /></label>
        <Button type="primary" loading={saving} onClick={() => void saveSettings()}>{L.saveSettings}</Button></div>}
    </Modal>
    <Modal open={evidenceOpen} title={L.addEvidence} onCancel={() => setEvidenceOpen(false)} footer={null} width={800} destroyOnHidden>
      <form onSubmit={event => void saveEvidence(event)}><div className="msc-form-grid">
        <label className="msc-full">{L.evidenceTitle}<input aria-label={L.evidenceTitle} required minLength={6} maxLength={300} value={article.title} onChange={event => setArticle({ ...article, title: event.target.value })} /></label>
        <label className="msc-full">{L.evidenceText}<textarea aria-label={L.evidenceText} required minLength={6} maxLength={40000} rows={8} value={article.text} onChange={event => setArticle({ ...article, text: event.target.value })} /></label>
        <label>{L.sourceUrl}<input aria-label={L.sourceUrl} type="url" maxLength={2000} value={article.source_url} onChange={event => setArticle({ ...article, source_url: event.target.value })} /></label>
        <label>{L.pub}<input aria-label={L.pub} type="datetime-local" value={article.published_at} onChange={event => setArticle({ ...article, published_at: event.target.value })} /></label>
      </div><Button type="primary" htmlType="submit" loading={saving}>{L.submitEvidence}</Button></form>
    </Modal>
    <Modal open={!!detail} title={detail?.title} onCancel={() => setDetail(null)} footer={null} width={860} destroyOnHidden>
      <Descriptions column={1} items={Object.entries(detail?.values || {}).map(([key, value]) => ({ key, label: key,
        children: typeof value === "object" ? <pre className="msc-detail">{JSON.stringify(value, null, 2)}</pre> : String(value ?? "—") }))} />
    </Modal>
  </Drawer>;
}
