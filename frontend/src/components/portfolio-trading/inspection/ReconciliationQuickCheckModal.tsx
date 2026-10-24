import {AcceptanceButton as Button,AcceptanceModal as Modal} from './AcceptanceControls';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Spin } from "antd";
import { CalendarDays, CheckCircle2, ChevronDown, ChevronRight, CircleAlert, ClipboardCheck, Clock3, Database, FileClock, FolderSearch, Link2, RefreshCw, ShieldCheck, X } from "lucide-react";
import { api } from "../../../api/client";
import type { ReconciliationResponse } from "../../../types";
import { reconDimLabel } from "../../../utils/reconDimLabel";
import InspectionActivityEvidence from "./InspectionActivityEvidence";
import InspectionDrawer from "./InspectionDrawer";
import InspectionEvidencePanel from "./InspectionEvidencePanel";
import InspectionHistoryPanel from "./InspectionHistoryPanel";
import { MATERIAL_GUIDES } from "./materialGuides";
import { historyGaps, reconciliationWaiting } from "./reconciliationGaps";
import { reconciliationReason, reconciliationText, reconciliationValue } from "./reconciliationPresentation";
import useInspectionPalette from "./useInspectionPalette";
import useInspectionBrowsePosition from "./useInspectionBrowsePosition";
import useInspectionFreshness, { InspectionStaleNotice } from "./useInspectionFreshness";
import "./inspection.css";
import "./reconciliationQuickCheck.css";

interface Props {
  portfolioId: number;
  tradeDate?: string;
  onClose: () => void;
  onChanged: () => void;
}

type Detail = "history" | "evidence";

export function validReconciliationDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

/** A date-scoped quick check. Opening reads evidence; only an explicit recheck writes a report. */
export default function ReconciliationQuickCheckModal({ portfolioId, tradeDate, onClose, onChanged }: Props) {
  const palette = useInspectionPalette();
  const [report, setReport] = useState<ReconciliationResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [layers, setLayers] = useState<Detail[]>([]);
  const [detailOverlay, setDetailOverlay] = useState(false);
  const [evidenceConfirmation, setEvidenceConfirmation] = useState(false);
  const [seen, setSeen] = useState<Detail[]>([]);
  const inFlight = useRef(false);
  const sequence = useRef(0);
  const lifetime = useRef(0);
  const gapDetails = useRef<HTMLDetailsElement>(null);
  const browseBody = useRef<HTMLDivElement>(null);
  const quickReturnFocus = useRef<HTMLElement | null>(null);
  const {stale,checking,capture,complete,isStale}=useInspectionFreshness(portfolioId,()=>{sequence.current++;inFlight.current=false;setLoading(false);setReport(null);});
  const detail = layers[layers.length - 1];
  const topLevel = !detail && !detailOverlay && !evidenceConfirmation;
  useInspectionBrowsePosition(browseBody,{portfolioId,key:`quick:${tradeDate??"pending"}`},topLevel,0,undefined,".ant-modal-body");

  const checkResponse = useCallback((value: ReconciliationResponse, expected?: string) => {
    if (!value || value.portfolio_id !== portfolioId || !validReconciliationDate(value.trade_date)
      || (expected && value.trade_date !== expected) || !Array.isArray(value.items)
      || typeof value.zero_sum_check_passed !== "boolean" || typeof value.differences_found !== "boolean") {
      throw new Error("返回结果不属于所选组合与交易日，已停止显示，请重新读取。");
    }
    if (value.zero_sum_check_passed && (value.differences_found || historyGaps(value).length || reconciliationWaiting(value))) {
      throw new Error("返回的核对结论与材料状态不一致，请重新核对。");
    }
    return value;
  }, [portfolioId]);

  const read = useCallback(async () => {
    if (inFlight.current) return;
    const scope = lifetime.current;
    const request = ++sequence.current;
    const freshness=capture();
    inFlight.current = true;
    setLoading(true);
    setError(null);
    try {
      if (tradeDate !== undefined && !validReconciliationDate(tradeDate)) throw new Error("交易日不明确，请关闭后重新选择待核对事项。");
      const value = checkResponse(await api.previewPortfolioReconciliation(portfolioId, tradeDate), tradeDate);
      if (scope === lifetime.current && request === sequence.current) {setReport(value);complete(freshness);}
    } catch (cause) {
      if (scope === lifetime.current && request === sequence.current) {
        setReport(null);
        setError(reconciliationText(cause instanceof Error ? cause.message : null, "本日核对材料读取失败，请重试。"));
      }
    } finally {
      if (scope === lifetime.current && request === sequence.current) { inFlight.current = false; setLoading(false); }
    }
  }, [portfolioId, tradeDate, checkResponse,capture,complete]);

  useEffect(() => {
    lifetime.current++;
    inFlight.current = false;
    setReport(null);
    setLayers([]);
    setSeen([]);
    setError(null);
    quickReturnFocus.current = null;
    void read();
    return () => { lifetime.current++; sequence.current++; inFlight.current = false; };
  }, [read]);

  const recheck = async () => {
    if (isStale() || !report || inFlight.current || error) return;
    const scope = lifetime.current;
    const request = ++sequence.current;
    const date = report.trade_date;
    inFlight.current = true;
    setLoading(true);
    setError(null);
    try {
      const value = checkResponse(await api.triggerPortfolioReconcile(portfolioId, { trade_date: date }), date);
      if (scope !== lifetime.current || request !== sequence.current) return;
      setReport(value);
      onChanged();
    } catch (cause) {
      if (scope === lifetime.current && request === sequence.current) {
        setReport(null);
        setError(reconciliationText(cause instanceof Error ? cause.message : null, "本日重检未完成，请重新读取后再试。"));
      }
    } finally {
      if (scope === lifetime.current && request === sequence.current) { inFlight.current = false; setLoading(false); }
    }
  };
  const restoreQuickFocus = useCallback(() => {
    const trigger = quickReturnFocus.current;
    if (trigger?.isConnected && trigger.closest(".pi-quick-check")) trigger.focus({ preventScroll: true });
  }, []);
  useLayoutEffect(() => {
    if (!topLevel) return;
    restoreQuickFocus();
    const frame = requestAnimationFrame(restoreQuickFocus);
    return () => cancelAnimationFrame(frame);
  }, [topLevel, restoreQuickFocus]);
  const openDetail = (target: Detail) => {
    if (!layers.length) quickReturnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setSeen(previous => previous.includes(target) ? previous : [...previous, target]);
    setLayers(previous => previous.includes(target) ? previous.slice(0, previous.indexOf(target) + 1) : [...previous, target]);
  };
  const back = () => { setDetailOverlay(false); setLayers(previous => previous.slice(0, -1)); };
  const close = () => { if (topLevel) onClose(); };
  const gaps = report ? historyGaps(report) : [];
  const waiting = report ? reconciliationWaiting(report) : false;
  const passed = Boolean(report?.zero_sum_check_passed);
  const comparisons = report?.items.filter(item => item.expected_value != null || item.actual_value != null || item.diff_value != null) ?? [];
  const VerdictIcon = passed ? CheckCircle2 : waiting ? Clock3 : CircleAlert;
  const selectedDate = tradeDate ?? report?.trade_date;
  const showGap = () => {
    if (gapDetails.current) { gapDetails.current.open = true; gapDetails.current.scrollIntoView?.({ block: "nearest" }); }
  };

  return <>
    <Modal centered open width={760} zIndex={1400} className="pi-quick-check" rootClassName="pi-quick-check-root"
      styles={{ wrapper: { ...palette, ...(!topLevel ? { visibility: "hidden", pointerEvents: "none" } : {}) } }}
      closeIcon={<X size={18} aria-hidden="true"/>}
      title={<div className="pi-quick-title">
        <div className="pi-quick-title-context"><span className="pi-quick-title-icon"><ClipboardCheck size={20} aria-hidden="true"/></span><div><strong>快捷核对</strong><span>执行与对账 · 按日核查</span></div></div>
        <span className="pi-quick-date"><CalendarDays size={14} aria-hidden="true"/>{selectedDate ? <time dateTime={selectedDate}>{selectedDate}</time> : "当前待核对交易日"}</span>
      </div>}
      onCancel={close} keyboard={topLevel} maskClosable={topLevel} mask={topLevel}
      footer={<div className="pi-quick-footer"><span><ShieldCheck size={14} aria-hidden="true"/>核对不会自动恢复交易</span><div><Button onClick={close}>关闭</Button><Button mutation type="primary" loading={loading} disabled={checking||stale||!report || Boolean(error)} icon={<RefreshCw size={14} aria-hidden="true"/>} onClick={() => void recheck()}>重新核对本日</Button></div></div>}>
      <div ref={browseBody} className="pi-workbench pi-quick-body" style={palette} aria-busy={loading}>
        {stale&&<InspectionStaleNotice loading={loading} onReload={()=>void read()}/>}
        {loading && !report && <div className="pi-quick-loading"><Spin/><p>正在读取本日核对材料…</p></div>}
        {error && <div role="alert" className="pi-callout pi-error"><strong>本日核对结果暂不可用</strong><p>{error}</p><Button onClick={() => void read()} loading={loading}>重新读取本日材料</Button></div>}
        {report && <>
          <section className={`pi-quick-verdict ${passed ? "pi-quick-passed" : waiting ? "pi-quick-waiting" : ""}`} aria-label="本日核对结论">
            <div className="pi-quick-verdict-heading">
              <span className="pi-quick-verdict-icon"><VerdictIcon size={20} aria-hidden="true"/></span>
              <div><span className="pi-quick-eyebrow">本日核对结论</span><h3>{passed ? "本日核对已通过" : waiting ? "等待本日执行结果" : gaps.length ? "历史执行资料不完整" : "本日仍有待核实差异"}</h3></div>
              <span className="pi-quick-mode">{report.read_only ? "只读预览" : "本次重检结果"}</span>
            </div>
            <p>{passed ? "此日期的核对已完成；其他日期、模型与策略条件仍需独立核验。" : waiting ? "结果到达后再核对，目前无需人工登记材料。" : gaps.length ? `发现 ${gaps.length} 类材料缺口，先核查来源，补齐可信材料后再重检。` : "查看下方账实差异和原始来源，处理后重新核对本日。"}</p>
            <div className="pi-quick-verdict-bottom"><span className="pi-quick-continuity">连续已核验至 <strong>{report.last_reconciled_trade_date ?? "暂无连续核验依据"}</strong></span>{gaps.length > 0 && !waiting && <Button className="pi-quick-gap-trigger" onClick={showGap}>查看缺口与查找方法<ChevronRight size={14} aria-hidden="true"/></Button>}</div>
          </section>
          <div className="pi-quick-actions"><Button type="text" disabled={loading} icon={<FileClock size={15} aria-hidden="true"/>} onClick={() => openDetail("history")}>查看本日处理记录</Button>{gaps.some(row => row.kind === "execution") && !waiting && <Button type="text" disabled={loading} icon={<Link2 size={15} aria-hidden="true"/>} onClick={() => openDetail("evidence")}>查看可关联原订单</Button>}</div>
          {comparisons.length > 0 && <section aria-label="本日账实差异"><h3>账实差异</h3><div className="pi-table-wrap"><table><thead><tr><th>核对项目</th><th>应有</th><th>实际</th><th>原因与差异</th></tr></thead><tbody>{comparisons.map((item, index) => <tr key={`${item.dimension}-${index}`}><td>{reconciliationText(reconDimLabel(item.dimension), "其他核对项目")}</td><td>{reconciliationValue(item.expected_value)}</td><td>{reconciliationValue(item.actual_value)}</td><td>{item.diff_value == null ? "材料不足，差异待核实" : `差异 ${reconciliationValue(item.diff_value)}`}<small>{reconciliationReason(item.dimension, item.explain_note)}</small></td></tr>)}</tbody></table></div></section>}
          <div className="pi-quick-detail-group">
            {gaps.length > 0 && !waiting && <details ref={gapDetails} className="pi-quick-disclosure pi-quick-gaps">
              <summary><FolderSearch size={17} aria-hidden="true"/><span className="pi-quick-disclosure-label">历史缺口 <span className="pi-quick-count">{gaps.length} 类</span><small>材料要求与查找方法</small></span><ChevronDown className="pi-quick-chevron" size={16} aria-hidden="true"/></summary>
              <div className="pi-quick-disclosure-content"><ol className="pi-quick-gap-list">{gaps.map((row, index) => <li key={row.key}><span className="pi-quick-gap-number" aria-hidden="true">{index + 1}</span><div><strong>{row.title}</strong><p>{row.reason}</p>{row.kind && <details className="pi-quick-material"><summary>去哪里找、什么材料可用<ChevronDown size={13} aria-hidden="true"/></summary><div><ul>{MATERIAL_GUIDES[row.kind].sources.map(source => <li key={source}>{source}</li>)}</ul><p>可信材料需要包含：</p><ul>{MATERIAL_GUIDES[row.kind].required.map(required => <li key={required}>{required}</li>)}</ul></div></details>}</div></li>)}</ol>
                <p className="pi-quick-gap-note">原任务或备份材料需核验并恢复原始来源，再重新核对；无需逐项登记查找结果。</p>
                <div className="pi-quick-gap-exit"><strong>资料确实找不到？</strong><p>确实找不到时，保留该日历史未知，可先处理其他日期和当前数据。重复重检不会清除缺口，后续处置需单独审核。</p></div>
              </div>
            </details>}
            <details className="pi-quick-disclosure pi-quick-sources"><summary><Database size={17} aria-hidden="true"/><span className="pi-quick-disclosure-label">系统已读取的来源<small>查看原始记录与读取情况</small></span><ChevronDown className="pi-quick-chevron" size={16} aria-hidden="true"/></summary><div className="pi-quick-disclosure-content"><InspectionActivityEvidence facts={report.activity_evidence}/></div></details>
          </div>
          {report.cash_audit?.status === "CONSISTENT" && <div className="pi-quick-cash-note"><CheckCircle2 size={15} aria-hidden="true"/><p><strong>当前账本资金一致</strong><span>历史资金与持仓仍需所选交易日的原始凭据。</span></p></div>}
        </>}
      </div>
    </Modal>
    {report && seen.map(surface => <InspectionDrawer key={surface} open={layers.includes(surface)} active={detail === surface && !detailOverlay && !evidenceConfirmation}
      browseScope={{portfolioId,key:`quick:${report.trade_date}:${surface}`}}
      focused icon={surface === "history" ? <FileClock size={20}/> : <Link2 size={20}/>}
      afterOpenChange={shown => { if (!shown && topLevel) requestAnimationFrame(restoreQuickFocus); }}
      zIndex={1500} title={surface === "history" ? "本日处理记录" : "本日原订单关联"} context={`快捷核对 / ${report.trade_date}`}
      backLabel={layers.length > 1 ? "返回原订单关联" : "返回快捷核对"} onBack={back} onClose={back}>
      {surface === "history" ? <InspectionHistoryPanel portfolioId={portfolioId} visible={detail === surface}
        context={{ trade_date: report.trade_date, report_ref: report.report_id ?? undefined, report_version: report.report_version ?? undefined, label: `${report.trade_date} 本日核对` }}
        onOverlayChange={setDetailOverlay} onReturn={back}/>
        : <InspectionEvidencePanel portfolioId={portfolioId} tradeDate={report.trade_date} visible={detail === surface}
          onChanged={async () => { onChanged(); await read(); }} onRecheck={recheck} onHistory={() => openDetail("history")}
          onBack={back} backLabel="返回快捷核对" onMissingMaterial={() => { setLayers([]); showGap(); }}
          onConfirmationChange={setEvidenceConfirmation} confirmationReady/>}
    </InspectionDrawer>)}
  </>;
}
