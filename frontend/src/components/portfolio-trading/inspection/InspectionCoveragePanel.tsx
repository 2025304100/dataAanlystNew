import {AcceptanceButton as Button} from './AcceptanceControls';
import React, { useEffect, useRef, useState } from "react";
import { Checkbox, Input } from "antd";
import { api } from "../../../api/client";
import type { CoverageAccepted, CoverageGap, CoveragePreview, CoverageProposal, CoverageState, InspectionCoverageApi } from "./coverageTypes";
import "./inspectionCoverage.css";
import InspectionDrawer from "./InspectionDrawer";
import useInspectionFreshness, { InspectionStaleNotice } from "./useInspectionFreshness";

const defaultApi = api as unknown as InspectionCoverageApi;
const SOURCE_LABELS: Record<string, string> = { cash_ledger: "现金流水", positions: "当前逐标的持仓", sim_orders: "模拟订单",
  sim_trades: "模拟成交", decision_runs: "正式运行与任务", decision_evidence: "逐对象决策证据", decision_order_plans: "订单意图" };
const splitLines = (value: string) => value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
function errorText(error: unknown) { return error instanceof Error ? error.message : "当前账户材料读取失败，请重试"; }
function displayTime(value: string) {
  const at = new Date(value);
  return Number.isNaN(at.getTime()) ? "快照时间未知" : new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", dateStyle: "medium", timeStyle: "medium" }).format(at);
}

export interface InspectionCoveragePanelProps {
  portfolioId: number;
  coverageApi?: InspectionCoverageApi;
  onAccepted?: (accepted: CoverageAccepted) => void;
  onRepairAccount?: () => void;
  onViewAudit?: () => void;
  onRecoverBackup?: () => void;
  onCreateIndependentAccount?: () => void;
  hostVisible?:boolean;
  onOverlayChange?:(open:boolean)=>void;
}

export default function InspectionCoveragePanel({ portfolioId, coverageApi = defaultApi, onAccepted, onRepairAccount, onViewAudit, onRecoverBackup, onCreateIndependentAccount,hostVisible=true,onOverlayChange }: InspectionCoveragePanelProps) {
  const [state, setState] = useState<CoverageState | null>(null);
  const [preview, setPreview] = useState<CoveragePreview | null>(null);
  const [accepted, setAccepted] = useState<CoverageAccepted | null>(null);
  const [applicationOpen, setApplicationOpen] = useState(false);
  useEffect(()=>{onOverlayChange?.(hostVisible&&applicationOpen);return()=>onOverlayChange?.(false);},[hostVisible,applicationOpen,onOverlayChange]);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");
  const [attempted, setAttempted] = useState("");
  const [references, setReferences] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [ack, setAck] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [stateError, setStateError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [loadedPortfolio, setLoadedPortfolio] = useState<number | null>(null);
  const sequence = useRef(0);
  const stateSequence = useRef(0);
  const activePortfolio = useRef(portfolioId);
  const mounted = useRef(true);
  const proposalRef = useRef<CoverageProposal | null>(null);
  const acceptanceKey = useRef("");
  const exportSequence = useRef(0);
  activePortfolio.current = portfolioId;
  const {stale:crossTabStale,checking,capture,complete,isStale}=useInspectionFreshness(portfolioId,()=>{++sequence.current;++stateSequence.current;setPreview(null);setAck(false);setLoading(false);setStale(true);});

  const readState = async () => {
    const freshness=capture();
    const pid = portfolioId;
    const token = ++stateSequence.current;
    setStateError(null);
    try {
      const result = await coverageApi.getPortfolioInspectionCoverage(pid);
      if (!mounted.current || activePortfolio.current !== pid || token !== stateSequence.current) return;
      if (result.portfolio_id !== pid) throw new Error("核验范围不属于当前组合");
      setState(result); setLoadedPortfolio(pid);complete(freshness);
    } catch (cause) {
      if (mounted.current && activePortfolio.current === pid && token === stateSequence.current) setStateError(errorText(cause));
    }
  };
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; ++sequence.current; ++stateSequence.current; };
  }, []);
  useEffect(() => {
    ++sequence.current;
    ++exportSequence.current; setExporting(false); setExportError(null);
    setState(null); setPreview(null); setAccepted(null); setFrom(""); setTo(""); setTarget(""); setReason("");
    setApplicationOpen(false);
    setAttempted(""); setReferences(""); setReviewNote(""); setAck(false); setStale(false); setError(null);
    setLoading(false); setSaving(false); proposalRef.current = null; acceptanceKey.current = "";
    void readState();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [portfolioId, coverageApi]);

  const invalidate = () => { ++sequence.current; setPreview(null); setAccepted(null); setAck(false); setStale(false); setError(null); setLoading(false); };
  const query = async () => {
    if (!from || !to || from > to || reason.trim().length < 10 || !splitLines(attempted).length || !splitLines(references).length) {
      setError("请填写有效历史区间、至少10字的不可恢复说明、已尝试来源和保留的证据引用。"); return;
    }
    const gap: CoverageGap = { trade_date_from: from, trade_date_to: to, reason: reason.trim(),
      attempted_sources: splitLines(attempted), evidence_refs: splitLines(references),
      verdict: "HISTORICAL_EVIDENCE_UNKNOWN", closure_type: "ARCHIVED_UNVERIFIABLE" };
    const proposal: CoverageProposal = { coverage_start_trade_date: target || undefined, history_gaps: [gap] };
    const token = ++sequence.current, pid = portfolioId;
    setLoading(true); setError(null); setPreview(null); setAccepted(null); setAck(false);
    try {
      const result = await coverageApi.previewPortfolioInspectionCoverage(pid, proposal);
      if (!mounted.current || activePortfolio.current !== pid || token !== sequence.current) return;
      if (result.portfolio_id !== pid) throw new Error("快照不属于当前组合");
      proposalRef.current = { coverage_start_trade_date: result.coverage_start_trade_date || undefined, history_gaps: result.history_gaps };
      acceptanceKey.current = `coverage-${crypto.randomUUID()}`;
      setPreview(result); setLoadedPortfolio(pid); setStale(false);
    } catch (cause) {
      if (mounted.current && activePortfolio.current === pid && token === sequence.current) setError(errorText(cause));
    } finally {
      if (mounted.current && activePortfolio.current === pid && token === sequence.current) setLoading(false);
    }
  };
  const accept = async () => {
    if (isStale() || !preview?.can_accept || !preview.coverage_start_trade_date || !proposalRef.current || !ack || stale || reviewNote.trim().length < 10 || saving) return;
    const token = ++sequence.current, pid = portfolioId;
    setSaving(true); setError(null);
    try {
      const result = await coverageApi.acceptPortfolioInspectionCoverage(pid, { ...proposalRef.current,
        coverage_start_trade_date: preview.coverage_start_trade_date,
        expected_evidence_fingerprint: preview.evidence_fingerprint,
        expected_cursor_row_version: preview.cursor_row_version, acceptance_key: acceptanceKey.current,
        review_note: reviewNote.trim(), acknowledge_history_unknown: true });
      if (!mounted.current || activePortfolio.current !== pid || token !== sequence.current) return;
      if (result.portfolio_id !== pid) throw new Error("接受结果不属于当前组合");
      setAccepted(result); setAck(false); onAccepted?.(result); void readState();
    } catch (cause) {
      if (mounted.current && activePortfolio.current === pid && token === sequence.current) {
        setError(errorText(cause));
        const status = (cause as { status_code?: number })?.status_code;
        if (status === 409 || status === 428) setStale(true);
      }
    } finally {
      if (mounted.current && activePortfolio.current === pid && token === sequence.current) setSaving(false);
    }
  };
  const visible = loadedPortfolio === portfolioId;
  const exportEvidence = async () => {
    const pid = portfolioId, token = ++exportSequence.current;
    setExporting(true); setExportError(null);
    try {
      if (!coverageApi.exportPortfolioInspectionHistory) throw new Error("证据包接口尚未接入，未生成替代证据。");
      const packageData = await coverageApi.exportPortfolioInspectionHistory(pid, {});
      if (!mounted.current || activePortfolio.current !== pid || token !== exportSequence.current) return;
      if (packageData.payload.portfolio_id !== pid) throw new Error("证据包不属于当前组合");
      const content = { persisted_evidence_package: packageData, current_account_preview: preview,
        unsubmitted_proposal: proposalRef.current, preview_is_accepted_baseline: false,
        explanation: "预览与未提交申请单独保留，仅已保存基线/原报告属于正式接受历史；缺少来源不会补成通过。" };
      const url = URL.createObjectURL(new Blob([JSON.stringify(content, null, 2)], { type: "application/json;charset=utf-8" }));
      const link = document.createElement("a"); link.href = url; link.download = `portfolio-${pid}-account-coverage-evidence.json`;
      link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (cause) {
      if (mounted.current && activePortfolio.current === pid && token === exportSequence.current) setExportError(errorText(cause));
    } finally {
      if (mounted.current && activePortfolio.current === pid && token === exportSequence.current) setExporting(false);
    }
  };

  return <section className="pi-coverage-panel" aria-label="历史未知与账户新覆盖起点">
    {crossTabStale&&<InspectionStaleNotice onReload={()=>void readState()}/>}
    <h3>核查历史证据并建立当前账户覆盖起点</h3>
    <p>旧期间仍保留“历史证据未知”。只有当前现金、逐标的持仓及未完成义务可独立核验，才能申请新覆盖范围。</p>
    {stateError && <div className="pi-coverage-error" role="alert">覆盖信息读取失败：{stateError}<Button size="small" onClick={() => void readState()}>重试覆盖信息</Button></div>}
    {visible && state?.cursor && <div className="pi-coverage-current"><strong>当前第 {state.cursor.generation} 代</strong><span>覆盖起点 {state.cursor.coverage_start_trade_date}</span><span>已连续核验至 {state.cursor.verified_through_trade_date || "尚未核验任何日期"}</span>
      {state.historical_coverage_unknown && <p>旧期历史缺口继续保留，当前代的核验结论仅适用于新范围。</p>}</div>}
    {visible && !!state?.baselines.length && <details className="pi-coverage-preserved-gaps"><summary>已保留的历史缺口与原账户基线（{state.baselines.length} 份）</summary>
      {state.baselines.map(baseline => <section key={baseline.id}><strong>原账户基线 {baseline.id}</strong><p>采集于 {displayTime(baseline.as_of_at)} · 北京时间；覆盖从 {baseline.coverage_start_trade_date} 开始。</p>
        <ul>{baseline.history_gaps.map((gap, index) => <li key={index}><strong>{gap.trade_date_from} 至 {gap.trade_date_to} · 历史证据未知</strong><p>{gap.reason}</p><p>尝试来源：{gap.attempted_sources.join("、")}</p><p>保留引用：{gap.evidence_refs.join("、")}</p></li>)}</ul>
        <small>接受审计 {baseline.accepted_audit_id} · 原证据指纹 {baseline.content_hash} · 只读</small></section>)}
    </details>}
    <div className="pi-coverage-recovery"><strong>先核查已有历史证据</strong><p>瞬时读取失败请先重试；已有运行、订单、流水或备份应先核查与补齐。下面的申请用于已确认不可恢复的历史缺口，填写理由本身不会证明当前账户可信。</p>{onViewAudit && <Button onClick={onViewAudit}>核查历史证据与处理记录</Button>}<Button loading={exporting} disabled={saving} onClick={() => void exportEvidence()}>导出证据与历史缺口</Button></div>
    {exportError && <div className="pi-coverage-error" role="alert">证据导出失败：{exportError}</div>}
    <Button mutation onClick={() => setApplicationOpen(true)}>已核查仍无法恢复，登记历史缺口并申请新基线</Button>
    <InspectionDrawer open={applicationOpen&&hostVisible} zIndex={1500} title="登记历史缺口与核验新覆盖申请" context="检查与对账 / 历史未知 / 独立申请"
      onClose={()=>{if(!saving)setApplicationOpen(false);}} onBack={()=>{if(!saving)setApplicationOpen(false);}} backLabel="返回历史证据核查">
    {applicationOpen && <div className="pi-coverage-panel pi-coverage-form">
      <fieldset><legend>已确认不可恢复的历史区间</legend><div className="pi-coverage-range">
        <input aria-label="历史缺口起点" type="date" value={from} disabled={saving} onChange={event => { setFrom(event.target.value); invalidate(); }} /><span>至</span>
        <input aria-label="历史缺口终点" type="date" value={to} disabled={saving} onChange={event => { setTo(event.target.value); invalidate(); }} />
      </div></fieldset>
      <label>不可恢复原因<Input.TextArea aria-label="不可恢复原因" rows={2} value={reason} disabled={saving} placeholder="说明缺失的配置、执行器或历史材料及恢复尝试" onChange={event => { setReason(event.target.value); invalidate(); }} /></label>
      <div className="pi-coverage-grid"><label>已尝试的证据来源<Input.TextArea aria-label="已尝试的证据来源" rows={2} value={attempted} disabled={saving} placeholder="每行一项，如备份记录、配置审计" onChange={event => { setAttempted(event.target.value); invalidate(); }} /></label>
        <label>保留的材料或任务引用<Input.TextArea aria-label="保留的材料或任务引用" rows={2} value={references} disabled={saving} placeholder="每行一项，记录原材料或恢复任务的真实引用" onChange={event => { setReferences(event.target.value); invalidate(); }} /></label></div>
      <label>拟开始覆盖的交易日（可留空，按真实日历选下一交易日）<input aria-label="新覆盖起点" type="date" value={target} disabled={saving} onChange={event => { setTarget(event.target.value); invalidate(); }} /></label>
      <Button mutation type="primary" loading={loading} disabled={saving} onClick={() => void query()}>核查账户来源并预览</Button>
    </div>}
    {error && <div className="pi-coverage-error" role="alert">{error}{stale && <p>材料或代次已变化，请保留上述申请内容，重新预览后再审核。</p>}</div>}
    {visible && preview && <div className="pi-coverage-preview">
      <h4>当前账户核验快照</h4><p>采集于 {displayTime(preview.as_of_at)} · 北京时间</p>
      <div className="pi-coverage-facts"><div><span>当前现金</span><strong>{preview.snapshot.cash ?? "来源缺失"} {preview.snapshot.currency}</strong></div>
        <div><span>独立核算现金</span><strong>{preview.snapshot.independent_expected_cash ?? "无法核验"}</strong></div>
        <div><span>未核清义务</span><strong>{preview.snapshot.unresolved_obligation_count ?? "未知"}</strong></div></div>
      <table aria-label="新基线逐标的持仓"><thead><tr><th>标的</th><th>数量</th><th>成本</th></tr></thead><tbody>{preview.snapshot.positions?.map(position => <tr key={position.symbol_id}><td>{position.name || position.security_id || `证券 ${position.symbol_id}`}<small>{position.security_id}</small></td><td>{position.quantity}</td><td>{position.avg_cost}</td></tr>)}</tbody></table>
      <details><summary>账户来源与证据指纹</summary><ul>{preview.source_refs.map(source => <li key={source.source}><strong>{SOURCE_LABELS[source.source] || "未登记来源"}</strong> · {source.available ? `${source.row_count ?? 0} 条真实来源记录` : "来源缺失"}<small>{source.source} · {source.content_hash || source.missing_columns?.join("、")}</small></li>)}</ul><code>{preview.evidence_fingerprint}</code></details>
      {!preview.can_accept ? <div className="pi-coverage-blocked" role="status"><strong>暂不能重建基线</strong><ul>{preview.blockers.map((blocker, index) => <li key={`${blocker.code}-${index}`}>{blocker.message}</li>)}</ul>
        <div className="pi-coverage-actions">{onRepairAccount && <Button onClick={onRepairAccount}>返回账户证据修复</Button>}{onViewAudit && <Button onClick={onViewAudit}>查看来源与处理记录</Button>}{onRecoverBackup && <Button mutation onClick={onRecoverBackup}>恢复已有备份证据</Button>}{onCreateIndependentAccount && <Button mutation onClick={onCreateIndependentAccount}>申请独立新模拟组合</Button>}</div></div>
        : <div className="pi-coverage-review"><strong>可申请从 {preview.coverage_start_trade_date} 开始新的核验覆盖</strong><p>接受后为第 {preview.current_generation + 1} 代，新范围尚未核验任何交易日。实际运行与模型审核仍按当前门禁判断。</p>
          <label>本人审核理由<Input.TextArea aria-label="新覆盖本人审核理由" value={reviewNote} disabled={saving || !!accepted} rows={2} onChange={event => setReviewNote(event.target.value)} placeholder="说明已核验的当前资产与承担的后续核对责任，至少10字" /></label>
          <Checkbox checked={ack} disabled={saving || !!accepted} onChange={event => setAck(event.target.checked)}>我确认当前材料，接受旧期继续保持未知；新起点不代表旧期通过，也不代表交易已恢复。</Checkbox>
          <Button mutation type="primary" loading={saving} disabled={checking || crossTabStale || !ack || reviewNote.trim().length < 10 || stale || !!accepted} onClick={() => void accept()}>本人接受新覆盖起点</Button>
        </div>}
    </div>}
    {visible && accepted && <div className="pi-coverage-accepted" role="status"><strong>已接受第 {accepted.generation} 代覆盖起点 {accepted.coverage_start_trade_date}</strong><p>新范围尚未核验任何交易日，旧期间仍为历史证据未知。可回到当前事项按新代证据继续核对。</p>{onViewAudit && <Button onClick={onViewAudit}>查看本人接受记录</Button>}</div>}
    </InspectionDrawer>
  </section>;
}
