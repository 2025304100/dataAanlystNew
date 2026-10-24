import {AcceptanceButton as Button,AcceptanceModal as Modal} from './AcceptanceControls';
import { useCallback, useEffect, useRef, useState } from "react";
import { Checkbox, Input } from "antd";
import { ArrowRight, ClipboardList, FileClock, FolderSearch, Link2, ListChecks, RefreshCw, ShieldCheck } from "lucide-react";
import { api } from "../../../api/client";
import type { EvidenceCandidate, EvidenceSupplement, EvidenceSupplementPage } from "../../../types/portfolioInspection";
import { InspectionConclusion, InspectionDisclosure } from "./InspectionSurface";
import useInspectionFreshness, { InspectionStaleNotice } from "./useInspectionFreshness";

const labels: Record<string,string> = {PENDING_REVIEW:"待本人审核",APPROVED:"关联已审核，待核对结果",REJECTED:"已拒绝",NEEDS_RECHECK:"来源已变化，需重新申请"};
const key = () => `evidence-${crypto.randomUUID()}`;
const errorText = (e: unknown) => e instanceof Error ? e.message : "材料读取失败，请重试";
interface Props {portfolioId:number;tradeDate:string|null;visible:boolean;onChanged:()=>Promise<void>;onRecheck:()=>Promise<void>;
  onHistory:()=>void;onBack:()=>void;backLabel?:string;onMissingMaterial?:()=>void;onConfirmationChange:(open:boolean)=>void;confirmationReady:boolean;}

export default function InspectionEvidencePanel({portfolioId,tradeDate,visible,onChanged,onRecheck,onHistory,onBack,backLabel="返回执行与对账",onMissingMaterial,onConfirmationChange,confirmationReady}:Props) {
  const [data,setData]=useState<EvidenceSupplementPage|null>(null);
  const [selected,setSelected]=useState<string[]>([]);
  const [note,setNote]=useState(""); const [reviewNote,setReviewNote]=useState("");
  const [loading,setLoading]=useState(false); const [busy,setBusy]=useState(false); const [error,setError]=useState<string|null>(null);
  const [confirmation,setConfirmation]=useState<{kind:"submit";candidates:EvidenceCandidate[];note:string;key:string}|{kind:"review";request:EvidenceSupplement;decision:"approve"|"reject";key:string}|null>(null);
  const [ack,setAck]=useState(false); const epoch=useRef(0); const sequence=useRef(0);
  const candidatesRef=useRef<HTMLElement>(null), requestsRef=useRef<HTMLDetailsElement>(null);
  const {stale,checking,capture,complete,isStale}=useInspectionFreshness(portfolioId,()=>{sequence.current++;setLoading(false);setData(null);setConfirmation(null);setAck(false);});
  useEffect(()=>{epoch.current++;setData(null);setSelected([]);setNote("");setReviewNote("");setError(null);setConfirmation(null);setBusy(false);return()=>{epoch.current++;};},[portfolioId,tradeDate]);
  useEffect(()=>{onConfirmationChange(Boolean(confirmation));},[confirmation,onConfirmationChange]);
  const load=useCallback(async()=>{if(!tradeDate)return;const current=epoch.current;const request=++sequence.current;const freshness=capture();setLoading(true);setError(null);
    try {const result=await api.getPortfolioEvidenceSupplements(portfolioId,tradeDate);
      if(current!==epoch.current||request!==sequence.current)return;
      if(result.portfolio_id!==portfolioId||result.trade_date!==tradeDate||!Array.isArray(result.candidates)||!Array.isArray(result.requests))throw new Error("补证材料不属于当前组合与交易日");
      setData(result);setError(null);setSelected(old=>old.filter(id=>result.candidates.some(row=>row.candidate_id===id)));complete(freshness);
    }catch(e){if(current===epoch.current&&request===sequence.current){setData(null);setError(errorText(e));}}finally{if(current===epoch.current&&request===sequence.current)setLoading(false);}
  },[portfolioId,tradeDate,capture,complete]);
  useEffect(()=>{if(visible)void load();},[visible,load]);
  const confirm=async()=>{if(isStale()||!confirmation||!ack||busy||!tradeDate)return;const frozen=confirmation;const current=epoch.current;setBusy(true);setError(null);
    try {if(frozen.kind==="submit")await api.submitPortfolioEvidenceBatch(portfolioId,{trade_date:tradeDate,
        items:frozen.candidates.map(row=>({candidate_id:row.candidate_id,expected_source_hash:row.source_hash})),request_key:frozen.key,note:frozen.note});
      else await api.reviewPortfolioEvidenceSupplement(portfolioId,frozen.request.id,{decision:frozen.decision,
        expected_source_hash:frozen.request.source_hash,decision_key:frozen.key,review_note:reviewNote.trim(),acknowledge_scope:true});
      if(current!==epoch.current)return;setConfirmation(null);setAck(false);await load();await onChanged();
    }catch(e){if(current===epoch.current){setError(errorText(e));setAck(false);}}
    finally{if(current===epoch.current)setBusy(false);}
  };
  const pending=Boolean(data?.requests.some(row=>row.status==="PENDING_REVIEW"));
  const showRequests=()=>{if(requestsRef.current){requestsRef.current.open=true;requestsRef.current.scrollIntoView?.({block:"nearest"});}};
  const sourceTitle=!data?(error?"原订单来源暂时无法读取":"正在核查原订单来源")
    :data.candidates.length?`找到 ${data.candidate_count} 笔可核验原订单`
    :data.matched_source_count?"本日匹配来源均已登记，请继续审核或重跑核对":"尚未找到满足条件的自动执行来源";
  return <section className="pi-surface-panel pi-evidence-panel" data-testid="evidence-supplement-panel">
    {stale&&<InspectionStaleNotice loading={loading} onReload={()=>void load()}/>}
    <InspectionConclusion label="原订单关联结论" title={sourceTitle} icon={<Link2 size={19}/>} tone={error?"error":data&&!data.candidates.length&&!data.matched_source_count?"warning":"neutral"}
      badge={tradeDate??"未选择交易日"}
      description={data?.candidates.length?"先核对原订单与确切计划来源，选定范围后提交关联申请。原始订单、成交和旧报告继续保留。":data?.matched_source_count?"原来源已保留，继续查看登记结果与待审核事项。关联完成后仍需核对账实差异。":"需要同组合、同交易日的真实自动订单及可核验的确切计划键；没有候选时先核查原任务与备份。"}
      meta={data?<>{data.candidate_count} 笔候选 · {data.requests.length} 条登记{pending?` · ${data.requests.filter(row=>row.status==="PENDING_REVIEW").length} 条待本人审核`:""}</>:"读取真实来源，不生成历史订单"}
      actions={data&&<Button onClick={pending||data.matched_source_count&&!data.candidates.length?showRequests:data.candidates.length?()=>candidatesRef.current?.scrollIntoView?.({block:"nearest"}):onMissingMaterial??onHistory}>{pending?"核查待审核关联":data.candidates.length?"核查可关联原订单":data.matched_source_count?"查看已登记关联":onMissingMaterial?"返回历史缺口，查看后续处理":"查找原任务与历史来源"}<ArrowRight size={14} aria-hidden="true"/></Button>}/>
    {error&&!confirmation&&<div role="alert" className="pi-callout pi-error">{error}</div>}
    <div className="pi-surface-tools"><Button type="text" icon={<RefreshCw size={14} aria-hidden="true"/>} loading={loading} onClick={()=>void load()}>刷新实际补证来源</Button><Button type="text" icon={<FileClock size={14} aria-hidden="true"/>} onClick={onHistory}>核查相关历史材料</Button><Button type="text" onClick={onBack}>{backLabel}</Button></div>
    {data&&<>
      {data.candidates.length>0&&<section ref={candidatesRef} aria-label="可关联原订单">
        <div className="pi-surface-section-head"><h3><ClipboardList size={16} aria-hidden="true"/>可核验的原订单关联 <span className="pi-surface-count">{data.candidate_count}</span></h3><span>仅当前组合与所选交易日</span></div>
        <div className="pi-evidence-selection"><Checkbox checked={selected.length===data.candidates.length} indeterminate={selected.length>0&&selected.length<data.candidates.length} onChange={e=>setSelected(e.target.checked?data.candidates.map(row=>row.candidate_id):[])}>选择当前交易日全部候选</Checkbox><span>已选 {selected.length} 笔原订单</span></div>
        <div className="pi-evidence-candidates" aria-label="本交易日可关联的原订单">{data.candidates.map(row=><div className="pi-evidence-order" key={row.candidate_id}><Checkbox checked={selected.includes(row.candidate_id)} onChange={e=>setSelected(old=>e.target.checked?[...old,row.candidate_id]:old.filter(id=>id!==row.candidate_id))}><strong>{row.name} · {row.symbol}</strong> / 原订单 #{row.order_id}</Checkbox>
          <p>决策日 {row.trade_date} → 执行日 {row.execution_date} · 已成交 {row.filled_quantity} / 委托 {row.quantity} · {row.fill_count} 笔成交 / {row.ledger_count} 笔资金流水</p>
          <InspectionDisclosure title="查看匹配依据和确切来源" icon={<FolderSearch size={15}/>}>{row.match_reasons.map(reason=><p key={reason}>{reason}</p>)}<p>运行 {row.run_id} · 决策 {row.evidence_id}</p><small>来源指纹 {row.source_hash}</small></InspectionDisclosure></div>)}</div>
        {data.has_more&&<p className="pi-warning-text">此交易日还有更多候选；本批最多处理显示的100笔，未显示来源继续保留。</p>}
        <label className="pi-input-label">来源核查说明（至少10个字）<Input.TextArea aria-label="来源核查说明" value={note} onChange={e=>setNote(e.target.value)} rows={3}/></label>
        <Button mutation disabled={checking||stale||!selected.length||note.trim().length<10||loading||Boolean(error)} type="primary" onClick={()=>{setAck(false);setConfirmation({kind:"submit",candidates:data.candidates.filter(row=>selected.includes(row.candidate_id)),note:note.trim(),key:key()});}}>预览登记所选补证（{selected.length} 笔）</Button>
      </section>}
      <InspectionDisclosure ref={requestsRef} title="已登记的补证与审核记录" description={`${data.requests.length} 条${pending?" · 有待本人审核事项":""}`} icon={<FileClock size={16}/>} open={pending||undefined}>
      {data.requests.length===0?<p className="pi-muted">此交易日尚无补证申请。</p>:data.requests.map(row=><div className="pi-evidence-request" key={row.id}><strong>订单 #{row.order_id} · {labels[row.status]??"状态待核实"}</strong><p>{row.note}</p><small>申请人 {row.created_by} · 申请 {row.id}</small>{row.review_note&&<p>审核人 {row.reviewed_by} · {row.review_note}</p>}
        {row.status==="PENDING_REVIEW"&&<div className="pi-panel-actions">{(["approve","reject"] as const).map(decision=><Button mutation key={decision} type={decision==="approve"?"primary":"default"} onClick={()=>{setAck(false);setReviewNote("");setConfirmation({kind:"review",request:row,decision,key:key()});}}>{decision==="approve"?"审核关联这笔原订单":"拒绝本次关联"}</Button>)}</div>}
        {row.status==="NEEDS_RECHECK"&&<p className="pi-warning-text">原申请和决定保留，当前已不作为有效关联。刷新来源后重新申请。</p>}</div>)}</InspectionDisclosure>
      <InspectionDisclosure title="关联规则与处理流程" description="先核查来源，再限定关联，最后重新核对" icon={<ListChecks size={16}/>}>
        <ol className="pi-evidence-flow"><li>核查实际来源，保留原订单与确切计划键。</li><li>{pending?"申请已登记，待本人审核。":"登记限定范围的补证，核查后由本人审核。"}</li><li>审核后重检账实差异，继续核实资金与持仓。</li></ol>
        {data.limitations.map(item=><p className="pi-muted" key={item}>{item}</p>)}
        <p>关联只保存确切来源，不产生交易，也不直接恢复运行权限。</p>
      </InspectionDisclosure>
      <div className="pi-evidence-footer"><span><ShieldCheck size={14} aria-hidden="true"/>关联完成后仍需核对</span><div><Button mutation disabled={busy} icon={<RefreshCw size={14} aria-hidden="true"/>} onClick={()=>void onRecheck()}>重跑关联日核对</Button><Button onClick={onBack}>{backLabel}并查看结果</Button></div></div>
    </>}
    <Modal mutation open={Boolean(confirmation)&&confirmationReady} title={confirmation?.kind==="submit"?"确认登记限定补证范围":"本人审核原订单关联"}
      onCancel={()=>{if(!busy)setConfirmation(null);}} onOk={()=>void confirm()} confirmLoading={busy} cancelText="取消" okText={confirmation?.kind==="submit"?"登记补证申请":"保存本次审核"}
      okButtonProps={{disabled:checking||stale||!ack||busy||(confirmation?.kind==="review"&&reviewNote.trim().length<10)}} focusTriggerAfterClose={false}>
      <div className="pi-workbench"><p>组合 #{portfolioId} / 决策日 {tradeDate} / {confirmation?.kind==="submit"?`${confirmation.candidates.length} 笔原订单`:`订单 #${confirmation?.request.order_id}`}</p>
        <div className="pi-callout pi-warning"><strong>本次不产生交易，也不直接恢复权限</strong><p>只保存确切来源的申请或关联审核；完成后仍需重检，资金与持仓差异必须独立核验。</p></div>
        {confirmation?.kind==="submit"?<><p>{confirmation.note}</p>{confirmation.candidates.map(row=><p key={row.candidate_id}>{row.name} · 原订单 #{row.order_id} · 决策 {row.evidence_id}<small>{row.source_hash}</small></p>)}</>:<><p>{confirmation?.decision==="approve"?"确认原订单与原决策关联":"拒绝本次关联，保留原始材料与缺口"}</p><label className="pi-input-label">审核理由（至少10个字）<Input.TextArea aria-label="补证审核理由" value={reviewNote} onChange={e=>setReviewNote(e.target.value)} rows={3}/></label></>}
        <Checkbox checked={ack} disabled={busy} onChange={e=>setAck(e.target.checked)}>我已核查本组合、交易日与原始来源，确认当前限定范围</Checkbox>{error&&<p role="alert" className="pi-error-text">{error}</p>}
      </div>
    </Modal>
  </section>;
}
