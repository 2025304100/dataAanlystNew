import React, { useCallback, useEffect, useRef, useState } from "react";
import { Button, Input, Select, Tag } from "antd";
import { CalendarClock, ChevronRight, ClipboardCheck, Download, FileClock, Filter, FolderSearch, History, RotateCcw, Search, ShieldCheck } from "lucide-react";
import { api } from "../../../api/client";
import type {
  HistoryBatch, HistoryDetail, HistoryExecutionResult, HistoryObject, HistoryPage,
  HistoryQuery, HistoryReference, HistoryReviewDecision, HistorySecurity, HistoryMaterial,
  InspectionHistoryApi, InspectionHistoryContext,
} from "./historyTypes";
import "./inspectionHistory.css";
import InspectionDrawer from "./InspectionDrawer";
import { InspectionConclusion, InspectionDisclosure } from "./InspectionSurface";
import {readInspectionBrowse,writeInspectionBrowse} from "./inspectionBrowseState";

const defaultApi = api as unknown as InspectionHistoryApi;
const REVIEW: Record<HistoryReviewDecision, string> = {
  pending: "待审核", approved: "同意", rejected: "拒绝", revoked: "已撤销", unknown: "未记录决定", not_applicable: "不适用",
};
const EXECUTION: Record<HistoryExecutionResult, string> = {
  not_executed: "未执行", running: "执行中", success: "成功", partial_failure: "部分失败", failed: "失败",
  dependency_blocked: "依赖阻断", unknown: "未知 / 待核验", not_applicable: "不适用",
};
const ACTIONS = [
  ["INSPECTION_MATERIAL_INVESTIGATED", "历史材料核查说明（未解除阻断）"],
  ["INSPECTION_AI_ANALYSIS", "AI 辅助分析检查证据"],
  ["REPAIR_TASK_REQUESTED", "提交数据修复任务"], ["REPAIR_OBJECT_RESULT", "逐对象修复结果"], ["REPAIR_TASK_RESULT", "数据修复任务结果"],
  ["ACCOUNT_BASELINE_ACCEPTANCE_REJECTED", "账户新覆盖起点未被接受"],
  ["DATA_BLOCK_RESOLUTION", "数据阻断处理"], ["RECONCILIATION_RESULT", "每日核对结果"],
  ["AUTO_SIMULATION_RESULT", "自动运行结果"], ["FACTOR_USAGE_APPLIED", "模型与策略变更"],
  ["MARKET_BAR_CORRECTION", "行情证据纠错"], ["DATA_SOURCE_FAILOVER", "数据来源切换"],
  ["DATA_QUALITY_QUARANTINE", "数据隔离处置"], ["ILLEGAL_STATE_TRANSITION", "状态转移被拒绝"],
  ["G6_ROLLOUT_STARTED", "运行范围扩大"], ["G6_ROLLOUT_ROLLED_BACK", "运行范围回退"],
  ["UNKNOWN_AUDIT_ACTION", "未登记操作"],
];

interface Draft {
  securityIds: string[];
  tradeFrom: string;
  tradeTo: string;
  occurredFrom: string;
  occurredTo: string;
  actions: string[];
  review: HistoryReviewDecision | undefined;
  execution: HistoryExecutionResult | undefined;
  operator: string;
  keyword: string;
  actorType: string;
  failureCode: string;
  aiAssisted: boolean | undefined;
}
type HistoryBrowse={draft:Draft;applied:HistoryQuery;related:boolean;more:boolean;endPinned:boolean;pages:number;detailBatch:string|null;materialReference:HistoryReference|null};

function localInput(at: number) { return new Date(at + 8 * 3600_000).toISOString().slice(0, 16); }
function initialDraft(context?: InspectionHistoryContext): Draft {
  return {
    securityIds: context?.security_ids || [], tradeFrom: context?.trade_date || "", tradeTo: context?.trade_date || "",
    occurredFrom: localInput(Date.now() - 30 * 86400_000), occurredTo: localInput(Date.now()),
    actions: [], review: undefined, execution: undefined, operator: "", keyword: "", actorType: "", failureCode: "", aiAssisted: undefined,
  };
}
function queryFrom(draft: Draft, context?: InspectionHistoryContext, related = false): HistoryQuery {
  return {
    security_id: draft.securityIds, trade_date_from: draft.tradeFrom || undefined, trade_date_to: draft.tradeTo || undefined,
    occurred_from: draft.occurredFrom ? `${draft.occurredFrom}:00+08:00` : undefined,
    occurred_to: draft.occurredTo ? `${draft.occurredTo}:59+08:00` : undefined,
    action: draft.actions, review_decision: draft.review, execution_result: draft.execution,
    operator: draft.operator.trim() || undefined, q: draft.keyword.trim() || undefined,
    actor_type: draft.actorType || undefined, failure_code: draft.failureCode.trim() || undefined, ai_assisted: draft.aiAssisted,
    ...(related ? { issue_id: context?.issue_id, correlation_id: context?.correlation_id,
      report_ref: context?.report_ref, report_version: context?.report_version } : {}),
  };
}
function errorMessage(error: unknown) {
  if (error instanceof Error) return error.message;
  return "暂时无法读取历史材料，请重试";
}
function occurred(value: string) {
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return "发生时间未知";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(at);
}
function record(value: unknown): Record<string, unknown> { return value && typeof value === "object" ? value as Record<string, unknown> : {}; }

export interface InspectionHistoryPanelProps {
  portfolioId: number;
  visible?: boolean;
  onOverlayChange?: (open: boolean) => void;
  context?: InspectionHistoryContext;
  onReturn?: () => void;
  onClearRelation?: () => void;
  historyApi?: InspectionHistoryApi;
  onOpenReference?: (reference: HistoryReference) => void;
}

export default function InspectionHistoryPanel({ portfolioId, visible = true, onOverlayChange, context, onReturn, onClearRelation, historyApi = defaultApi, onOpenReference }: InspectionHistoryPanelProps) {
  const contextKey = JSON.stringify(context || {});
  const scopeKey = `${portfolioId}:${contextKey}`;
  const hasRelation = !!(context?.issue_id || context?.correlation_id || context?.report_ref);
  const [related, setRelated] = useState(hasRelation);
  const [draft, setDraft] = useState<Draft>(() => initialDraft(context));
  const [applied, setApplied] = useState<HistoryQuery>({});
  const [data, setData] = useState<HistoryPage | null>(null);
  const [items, setItems] = useState<HistoryBatch[]>([]);
  const [loadedScope, setLoadedScope] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState(false);
  const [options, setOptions] = useState<HistorySecurity[]>([]);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [securitySearch, setSecuritySearch] = useState("");
  const [detailBatch, setDetailBatch] = useState<string|null>(null);
  const [details, setDetails] = useState<Record<string, HistoryDetail>>({});
  const [detailErrors, setDetailErrors] = useState<Record<string, string>>({});
  const [detailLoading, setDetailLoading] = useState<Set<string>>(new Set());
  const [material, setMaterial] = useState<HistoryMaterial | null>(null);
  const [materialError, setMaterialError] = useState<string | null>(null);
  const [materialLoading, setMaterialLoading] = useState(false);
  const [materialBatch, setMaterialBatch] = useState<string | null>(null);
  const [materialReference,setMaterialReference]=useState<HistoryReference|null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const materialRequest = useRef(0);
  const exportRequest = useRef(0);
  const failedRequest = useRef<{ query: HistoryQuery; append: boolean; pages:number } | null>(null);
  const request = useRef(0);
  const securityRequest = useRef(0);
  const activeScope = useRef(scopeKey);
  const mounted = useRef(true);
  const wasVisible = useRef(visible);
  const endTimePinned = useRef(false);
  const detailRequests = useRef<Record<string, number>>({});
  const detailSnapshot = useRef<number | null>(null);
  const selectedIds = useRef(draft.securityIds);
  const browseCache = useRef(new Map<string, {draft:Draft; related:boolean; more:boolean; applied:HistoryQuery; data:HistoryPage|null; items:HistoryBatch[]; options:HistorySecurity[]; endPinned:boolean}>());
  const restorePages=useRef(1);
  const restoreDetail=useRef<{batch:string;reference:HistoryReference|null}|null>(null);
  selectedIds.current = draft.securityIds;
  activeScope.current = scopeKey;

  const load = useCallback(async (query: HistoryQuery, append = false, preservePages = false, retryPages = 1) => {
    const token = ++request.current;
    const scope = scopeKey;
    setLoading(true); setError(null);
    failedRequest.current = null;
    const pages=Math.max(restorePages.current,retryPages);restorePages.current=1;
    try {
      let result = await historyApi.getPortfolioInspectionHistory(portfolioId, { ...query, page_size: 20 });
      if (!mounted.current || token !== request.current || activeScope.current !== scope) return;
      if (result.items.some(item => item.portfolio_id !== portfolioId)) throw new Error("历史材料不属于当前组合，请重新查询");
      if(!append&&pages>1){
        for(let page=2;page<=pages&&result.has_more;page++){
          const next=await historyApi.getPortfolioInspectionHistory(portfolioId,{...query,page,page_size:20,snapshot_event_id:result.snapshot_event_id});
          if(!mounted.current||token!==request.current||activeScope.current!==scope)return;
          if(next.snapshot_event_id!==result.snapshot_event_id||next.items.some(item=>item.portfolio_id!==portfolioId))throw new Error("历史分页范围已变化，请重新查询。");
          result={...next,items:[...result.items,...next.items]};
        }
      }
      const sameSnapshot = detailSnapshot.current === result.snapshot_event_id;
      if (!sameSnapshot) {
        detailSnapshot.current = result.snapshot_event_id;
        setDetails({}); setDetailErrors({}); setDetailLoading(new Set());
      }
      setData(previous => preservePages && sameSnapshot && previous && previous.page > result.page ? {...result,page:previous.page,has_more:previous.has_more} : result); setLoadedScope(scope);
      setItems(previous => append ? [...new Map([...previous, ...result.items].map(item => [item.batch_id, item])).values()] : preservePages && sameSnapshot ? previous : result.items);
      setApplied(query);
      if (query.since_last_reconciliation && typeof result.filters.occurred_from === "string") setApplied({ ...query, occurred_from: result.filters.occurred_from });
    } catch (cause) {
      if (mounted.current && token === request.current && activeScope.current === scope) {
        failedRequest.current = { query, append, pages }; setError(errorMessage(cause));
      }
    } finally {
      if (mounted.current && token === request.current && activeScope.current === scope) setLoading(false);
    }
  }, [historyApi, portfolioId, scopeKey, loadedScope]);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; ++request.current; ++securityRequest.current; };
  }, []);

  useEffect(() => {
    const cached = browseCache.current.get(scopeKey);
    const stored=readInspectionBrowse<HistoryBrowse>(portfolioId,`history:${contextKey}`);
    const validStored=stored&&stored.draft&&Array.isArray(stored.draft.securityIds)&&typeof stored.draft.tradeFrom==="string"&&stored.applied?stored:null;
    const next = cached?.draft ?? validStored?.draft ?? initialDraft(context);
    restorePages.current=cached?1:validStored&&Number.isSafeInteger(validStored.pages)&&validStored.pages>0?validStored.pages:1;
    restoreDetail.current=validStored?.detailBatch?{batch:validStored.detailBatch,reference:validStored.materialReference??null}:null;
    endTimePinned.current = cached?.endPinned ?? validStored?.endPinned ?? false;
    setDraft(next); setRelated(cached?.related ?? validStored?.related ?? hasRelation); setMore(cached?.more ?? validStored?.more ?? false); setSecuritySearch("");
    setOptions(cached?.options ?? []); setSearchError(null); setDetailBatch(null); setDetails({}); setDetailErrors({}); setDetailLoading(new Set());
    setData(cached?.data ?? null); setItems(cached?.items ?? []); setLoadedScope(cached?.data ? scopeKey : "");
    detailRequests.current = {};
    detailSnapshot.current = cached?.data?.snapshot_event_id ?? null;
    ++materialRequest.current; ++exportRequest.current;
    setMaterial(null); setMaterialReference(null); setMaterialError(null); setMaterialLoading(false); setMaterialBatch(null); setExporting(false); setExportError(null);
    const query = cached?.applied ?? validStored?.applied ?? queryFrom(next, context, hasRelation);
    setApplied(query); void load({...query,page:1,snapshot_event_id:undefined},false,Boolean(cached));
    // A context is serialized so callers can pass a new object each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey, historyApi]);

  useEffect(() => {
    if (loadedScope === scopeKey) {
      browseCache.current.set(scopeKey, {draft,related,more,applied,data,items,options,endPinned:endTimePinned.current});
      if(!restoreDetail.current)writeInspectionBrowse(portfolioId,`history:${contextKey}`,{draft,related,more,applied,endPinned:endTimePinned.current,pages:data?.page??1,detailBatch,materialReference} satisfies HistoryBrowse);
    }
  }, [scopeKey,loadedScope,draft,related,more,applied,data,items,options,detailBatch,materialReference]);

  useEffect(() => {
    const reopened = visible && !wasVisible.current;
    wasVisible.current = visible;
    if (!reopened || loadedScope !== scopeKey) return;
    // A newly registered material is outside the old event watermark. Refresh
    // the list on return, preserving filters and deliberately pinned time ends.
    const query = {...applied, page:1, snapshot_event_id:undefined};
    if (!endTimePinned.current) {
      const end = localInput(Date.now());
      setDraft(old => ({...old, occurredTo:end}));
      query.occurred_to = `${end}:59+08:00`;
    }
    void load(query,false,true);
  }, [visible, loadedScope, scopeKey, applied, load]);

  useEffect(() => {
    const token = ++securityRequest.current;
    const scope = scopeKey;
    const timer = window.setTimeout(() => {
      void historyApi.getPortfolioInspectionHistorySecurities(portfolioId, securitySearch.trim() || undefined).then(result => {
        if (!mounted.current || token !== securityRequest.current || activeScope.current !== scope) return;
        setOptions(previous => [...new Map([...previous.filter(item => selectedIds.current.includes(item.security_id)), ...result.items].map(item => [item.security_id, item])).values()]);
        setSearchError(null);
      }).catch(cause => {
        if (mounted.current && token === securityRequest.current && activeScope.current === scope) setSearchError(errorMessage(cause));
      });
    }, 200);
    return () => { window.clearTimeout(timer); ++securityRequest.current; };
  }, [historyApi, portfolioId, scopeKey, securitySearch]);

  const apply = (nextDraft = draft, nextRelated = related) => {
    if (nextDraft.tradeFrom && nextDraft.tradeTo && nextDraft.tradeFrom > nextDraft.tradeTo) { setError("所属交易日起点不能晚于终点"); return; }
    if (nextDraft.occurredFrom && nextDraft.occurredTo && nextDraft.occurredFrom > nextDraft.occurredTo) { setError("发生时间起点不能晚于终点"); return; }
    const next = queryFrom(nextDraft, context, nextRelated);
    setApplied(next); void load(next);
  };
  const clearRelation = () => { setRelated(false); apply(draft, false); onClearRelation?.(); };
  const visibleItems = loadedScope === scopeKey ? items : [];

  const readReference = async (reference: HistoryReference, batchId: string) => {
    if (onOpenReference) { onOpenReference(reference); return; }
    const token = ++materialRequest.current, scope = scopeKey;
    setMaterialBatch(batchId);
    setMaterialReference(reference);
    setMaterial(null); setMaterialError(null); setMaterialLoading(true);
    try {
      if (!historyApi.getPortfolioInspectionHistoryMaterial) throw new Error("只读材料接口尚未接入；原始引用继续保留，可导出后核查。");
      const result = await historyApi.getPortfolioInspectionHistoryMaterial(portfolioId, reference, data?.snapshot_event_id);
      if (!mounted.current || activeScope.current !== scope || token !== materialRequest.current) return;
      if (result.portfolio_id !== portfolioId || result.reference.source_event_id !== reference.source_event_id
          || result.reference.ref !== reference.ref || result.reference.kind !== reference.kind || (result.reference.version ?? null) !== (reference.version ?? null)) throw new Error("关联材料身份或版本不匹配，请重试");
      setMaterial(result);
    } catch (cause) {
      if (mounted.current && activeScope.current === scope && token === materialRequest.current) setMaterialError(errorMessage(cause));
    } finally {
      if (mounted.current && activeScope.current === scope && token === materialRequest.current) setMaterialLoading(false);
    }
  };
  const exportEvidence = async () => {
    const token = ++exportRequest.current, scope = scopeKey;
    setExporting(true); setExportError(null);
    try {
      if (!historyApi.exportPortfolioInspectionHistory) throw new Error("证据包导出接口尚未接入，未生成演示文件。");
      const result = await historyApi.exportPortfolioInspectionHistory(portfolioId, { ...applied, snapshot_event_id: data?.snapshot_event_id });
      if (!mounted.current || activeScope.current !== scope || token !== exportRequest.current) return;
      if (result.payload.portfolio_id !== portfolioId || (data && result.payload.snapshot_event_id !== data.snapshot_event_id)) throw new Error("证据包组合或查询水位不匹配，请重新读取后导出");
      const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json;charset=utf-8" }));
      const link = document.createElement("a"); link.href = url; link.download = `portfolio-${portfolioId}-inspection-evidence.json`;
      link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (cause) {
      if (mounted.current && activeScope.current === scope && token === exportRequest.current) setExportError(errorMessage(cause));
    } finally {
      if (mounted.current && activeScope.current === scope && token === exportRequest.current) setExporting(false);
    }
  };

  const closeMaterial=()=>{++materialRequest.current;setMaterialBatch(null);setMaterialReference(null);setMaterial(null);setMaterialError(null);setMaterialLoading(false);};
  const closeDetail=()=>{closeMaterial();setDetailBatch(null);};
  useEffect(()=>{onOverlayChange?.(visible&&detailBatch!==null);return()=>onOverlayChange?.(false);},[visible,detailBatch,onOverlayChange]);
  const openDetail = async (batchId:string) => {
    setDetailBatch(batchId);
    if(!details[batchId]) await readDetail(batchId);
  };
  const readDetail = async (batchId: string) => {
    const token = (detailRequests.current[batchId] || 0) + 1;
    detailRequests.current[batchId] = token;
    const scope = scopeKey;
    const listToken = request.current;
    setDetailLoading(old => new Set(old).add(batchId));
    setDetailErrors(old => { const next = { ...old }; delete next[batchId]; return next; });
    try {
      const result = await historyApi.getPortfolioInspectionHistoryDetail(portfolioId, batchId,
        { ...applied, snapshot_event_id: data?.snapshot_event_id });
      if (!mounted.current || activeScope.current !== scope || detailRequests.current[batchId] !== token || listToken !== request.current) return;
      if (result.portfolio_id !== portfolioId) throw new Error("详情材料不属于当前组合");
      setDetails(old => ({ ...old, [batchId]: result }));
    } catch (cause) {
      if (mounted.current && activeScope.current === scope && detailRequests.current[batchId] === token && listToken === request.current) setDetailErrors(old => ({ ...old, [batchId]: errorMessage(cause) }));
    } finally {
      if (mounted.current && activeScope.current === scope && detailRequests.current[batchId] === token) setDetailLoading(old => { const next = new Set(old); next.delete(batchId); return next; });
    }
  };

  const visibleBatchIds = visibleItems.map(item => item.batch_id).join("|");
  useEffect(()=>{
    const saved=restoreDetail.current;
    if(!saved||loading||loadedScope!==scopeKey)return;
    restoreDetail.current=null;setDetailBatch(saved.batch);
    void readDetail(saved.batch);
    if(saved.reference)void readReference(saved.reference,saved.batch);
  },[loading,loadedScope,scopeKey]);
  useEffect(() => {
    if (loadedScope !== scopeKey || !data) return;
    for (const batch of visibleItems) {
      if (detailBatch===batch.batch_id && !details[batch.batch_id] && !detailLoading.has(batch.batch_id)) void readDetail(batch.batch_id);
    }
    // Preserve expanded positions while refreshing their materials at a new
    // immutable event watermark; user-opened rows fetch in openDetail itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.snapshot_event_id, visibleBatchIds, loadedScope]);

  return <section className="pi-history-panel pi-surface-panel" aria-label="检查与对账历史记录">
    <InspectionConclusion label="历史查询结果" title={error?"历史记录暂时不可用":loading&&!data?"正在读取处理记录":data?`已载入 ${data.total} 个处置批次`:"查看历史处理记录"} icon={<History size={20}/>} tone={error?"error":"neutral"} badge={related?"本次处理":"历史记录"}
      description={context?`来自：${context.label || "当前阻断"}${context.trade_date?` · ${context.trade_date}`:""}。按标的和交易日追溯当时的核对、关联及审核结果。`:"按标的和交易日回看处置批次，分别查看当时的审核决定、执行结果及原始来源。"}
      meta={<>当前组合 · 真实审计 · 只读{visibleItems.length>0?` · 已显示 ${visibleItems.length} 个批次`:""}</>}
      actions={<>{related&&<Button size="small" onClick={clearRelation}>清除关联筛选</Button>}{onReturn&&<Button size="small" onClick={onReturn}>返回当前问题<ChevronRight size={13} aria-hidden="true"/></Button>}</>}/>
    <div className="pi-history-tabs" role="tablist" aria-label="审计范围">
      <button role="tab" aria-selected={related} disabled={!hasRelation} onClick={() => { setRelated(true); apply(draft, true); }}>本次处理</button>
      <button role="tab" aria-selected={!related} onClick={clearRelation}>历史记录</button>
      <span>审核与执行分别留痕</span>
    </div>
    {context&&related&&<InspectionDisclosure title="关联范围与原始引用" description="保留当前问题、交易日与报告版本" icon={<FolderSearch size={15}/>} className="pi-history-origin">
      <p>{context.security_ids?.length?`${context.security_ids.length} 个标的 · `:""}{context.trade_date||"按当前问题范围"}</p>
      <p>关联条件：{[context.issue_id && `问题 ${context.issue_id}`, context.correlation_id && `关联 ${context.correlation_id}`,
        context.report_ref && `报告 ${context.report_ref}${context.report_version ? ` / ${context.report_version}` : ""}`].filter(Boolean).join("；")}（任一真实引用命中）</p>
    </InspectionDisclosure>}
    <div className="pi-history-filters">
      <div className="pi-history-primary-filters">
      <label className="pi-history-security">标的 · 名称 / 代码，多选任一命中
        <Select mode="multiple" aria-label="标的名称或代码" showSearch allowClear placeholder="搜索名称或代码，可选择多个标的"
          value={draft.securityIds} filterOption={false} onSearch={setSecuritySearch}
          onChange={value => setDraft(old => ({ ...old, securityIds: value }))}
          options={options.map(item => ({ value: item.security_id, label: `${item.name || "名称未知"} · ${item.code || item.security_id} (${item.market || item.security_id.split(":")[0]})` }))}
          notFoundContent={searchError || "当前组合没有匹配的历史索引标的"} />
      </label>
      {searchError && <div className="pi-history-error" role="alert">标的索引读取失败：{searchError}<Button size="small" onClick={() => setSecuritySearch(old => `${old} `)}>重试标的搜索</Button></div>}
      <fieldset><legend>所属交易日 / 覆盖区间</legend><div className="pi-history-range">
        <input aria-label="所属交易日起点" type="date" value={draft.tradeFrom} onChange={event => setDraft(old => ({ ...old, tradeFrom: event.target.value }))} />
        <span>至</span><input aria-label="所属交易日终点" type="date" value={draft.tradeTo} onChange={event => setDraft(old => ({ ...old, tradeTo: event.target.value }))} />
      </div></fieldset>
      </div>
      <InspectionDisclosure title="操作发生时间" description="北京时间 · 与所属交易日分别筛选" icon={<CalendarClock size={15}/>} className="pi-history-time-filter">
      <fieldset><legend>操作发生时间 · 北京时间</legend><div className="pi-history-range">
        <input aria-label="发生时间起点" type="datetime-local" value={draft.occurredFrom} onChange={event => setDraft(old => ({ ...old, occurredFrom: event.target.value }))} />
        <span>至</span><input aria-label="发生时间终点" type="datetime-local" value={draft.occurredTo} onChange={event => {endTimePinned.current = true; setDraft(old => ({ ...old, occurredTo: event.target.value }));}} />
      </div></fieldset>
      </InspectionDisclosure>
      <div className="pi-history-inline pi-history-query-actions">
        <Button type="primary" aria-label="查询" icon={<Search size={14} aria-hidden="true"/>} onClick={() => apply()} loading={loading}>查询</Button>
        <Button type="text" icon={<RotateCcw size={14} aria-hidden="true"/>} onClick={() => { const next = initialDraft(); setDraft(next); apply(next); }}>重置筛选</Button>
        <Button type="text" icon={<Filter size={14} aria-hidden="true"/>} onClick={() => setMore(old => !old)} aria-expanded={more}>更多筛选{more ? " −" : " +"}</Button>
      </div>
      <div className="pi-history-inline pi-history-secondary-tools">
        <Button onClick={() => void load({ ...queryFrom(draft, context, related), since_last_reconciliation: true, occurred_from: undefined })}>自上次对账以来</Button>
        <Button type="text" icon={<Download size={14} aria-hidden="true"/>} loading={exporting} disabled={loading || !data} onClick={() => void exportEvidence()}>导出当前筛选证据包</Button>
      </div>
      {more && <div className="pi-history-more">
        <label>操作类型<Select mode="multiple" aria-label="操作类型" allowClear value={draft.actions} options={ACTIONS.map(([value, label]) => ({ value, label }))} onChange={actions => setDraft(old => ({ ...old, actions }))} /></label>
        <label>审核决定<Select aria-label="审核决定" allowClear value={draft.review} placeholder="全部审核决定" options={Object.entries(REVIEW).map(([value, label]) => ({ value, label }))} onChange={review => setDraft(old => ({ ...old, review }))} /></label>
        <label>执行结果<Select aria-label="执行结果" allowClear value={draft.execution} placeholder="全部执行结果" options={Object.entries(EXECUTION).map(([value, label]) => ({ value, label }))} onChange={execution => setDraft(old => ({ ...old, execution }))} /></label>
        <label>操作人<Input aria-label="操作人" value={draft.operator} onChange={event => setDraft(old => ({ ...old, operator: event.target.value }))} /></label>
        <label>执行主体<Select aria-label="执行主体" allowClear value={draft.actorType || undefined} placeholder="全部主体（未记录保持未知）" options={[{ value: "human", label: "本人 / 人工" }, { value: "scheduler", label: "调度" }, { value: "system", label: "系统" }, { value: "ai", label: "AI 工具" }]} onChange={actorType => setDraft(old => ({ ...old, actorType: actorType || "" }))} /></label>
        <label>AI 辅助<Select aria-label="AI 辅助" allowClear value={draft.aiAssisted === undefined ? undefined : String(draft.aiAssisted)} placeholder="全部" options={[{ value: "true", label: "有明确 AI 辅助记录" }, { value: "false", label: "明确记录未使用 AI" }]} onChange={value => setDraft(old => ({ ...old, aiAssisted: value === undefined ? undefined : value === "true" }))} /></label>
        <label>失败码<Input aria-label="失败码" value={draft.failureCode} onChange={event => setDraft(old => ({ ...old, failureCode: event.target.value }))} /></label>
        <label>关键词<Input aria-label="历史关键词" value={draft.keyword} onChange={event => setDraft(old => ({ ...old, keyword: event.target.value }))} onPressEnter={() => apply()} /></label>
      </div>}
    </div>
    <div className="pi-history-query-summary" aria-live="polite">
      已应用：{applied.security_id?.length || 0} 个标的 · 所属交易日 {applied.trade_date_from || "不限"} 至 {applied.trade_date_to || "不限"}
      <br />发生时间 {applied.occurred_from?.slice(0, 16).replace("T", " ") || "不限"} 至 {applied.occurred_to?.slice(0, 16).replace("T", " ") || "不限"}（北京时间）
      {applied.since_last_reconciliation && <span> · 起点依据当前组合最后一次真实通过的对账审计</span>}
    </div>
    <p className="pi-history-readonly pi-surface-note"><ShieldCheck size={14} aria-hidden="true"/>历史审核与报告仅供回看。当前处理需返回问题重新核验；旧同意决定不会授予当前执行能力。</p>
    {exportError && <div className="pi-history-error" role="alert">证据包导出失败：{exportError}</div>}
    {error && <div className="pi-history-error" role="alert"><strong>历史记录读取失败</strong><p>{error}</p><Button onClick={() => void load(failedRequest.current?.query || applied, failedRequest.current?.append || false,false,failedRequest.current?.pages??1)}>保留筛选重试</Button></div>}
    {loading && <p role="status">正在读取真实历史记录…</p>}
    {!loading && !error && visibleItems.length === 0 && <div className="pi-history-empty" role="status">{data?.empty_reason || "当前范围无匹配记录"}<p>可调整标的、两类日期或关联条件后重新查询。</p></div>}
    {visibleItems.length > 0 && <p className="pi-history-result-count">共 {data?.total || visibleItems.length} 个处置批次，已显示 {visibleItems.length} 个</p>}
    <div className="pi-history-batches">{visibleItems.map((batch, index) => <React.Fragment key={batch.batch_id}>
      {(index === 0 || occurred(visibleItems[index - 1].occurred_at).slice(0, 10) !== occurred(batch.occurred_at).slice(0, 10)) && <h3 className="pi-history-day">{occurred(batch.occurred_at).slice(0, 10)} · 北京时间</h3>}
      <article className="pi-history-batch">
      <button className="pi-history-batch-title" aria-haspopup="dialog" onClick={() => void openDetail(batch.batch_id)}>{batch.title}<span>查看详情<ChevronRight size={14} aria-hidden="true"/></span></button>
      <p className="pi-history-meta">{occurred(batch.occurred_at)} · 北京时间 · {batch.operators.join("、") || "操作人未知"}</p>
      <ResultLine batch={batch} />
      <p>本批共 {batch.total_object_count} 个已索引对象，命中 {batch.matched_object_count} 个对象 / {batch.matched_security_count} 个标的</p>
      {batch.trade_date_index_status === "not_applicable" && <p className="pi-history-meta">所属交易日不适用 · 组合级配置或运行范围审核</p>}
      {batch.index_warnings.length > 0 && <InspectionDisclosure title="来源保留情况与索引说明" description={`${batch.index_warnings.length} 项原审计提示`} icon={<FolderSearch size={14}/>}><p className="pi-history-warning">{batch.index_warnings.join("；")}</p></InspectionDisclosure>}
      <ObjectTable objects={batch.matched_objects} label="命中对象" />
    </article></React.Fragment>)}</div>
    {!error && data?.has_more && <Button className="pi-history-load-more" loading={loading} onClick={() => void load({ ...applied, page: data.page + 1, snapshot_event_id: data.snapshot_event_id }, true)}>加载更多历史批次</Button>}
    <InspectionDrawer open={Boolean(detailBatch)&&visible} active={!materialBatch&&visible} scrollKey={`${scopeKey}:${detailBatch}`} zIndex={1500}
      browseScope={{portfolioId,key:`history-detail:${contextKey}:${detailBatch}`}}
      focused icon={<FileClock size={20}/>}
      title={`${visibleItems.find(batch=>batch.batch_id===detailBatch)?.title??"历史处理记录"} · 详情`} context="检查与对账 / 操作记录 / 处置批次"
      onClose={closeDetail} onBack={closeDetail} backLabel="返回历史列表">
      {detailBatch&&<div className="pi-history-panel pi-history-detail">
        {detailLoading.has(detailBatch)&&<p role="status">正在读取本批原始材料…</p>}
        {detailErrors[detailBatch]&&<div className="pi-history-error" role="alert">详情读取失败：{detailErrors[detailBatch]} <Button onClick={()=>void readDetail(detailBatch)}>重试详情</Button></div>}
        {details[detailBatch]&&<BatchDetail detail={details[detailBatch]} onOpenReference={reference=>void readReference(reference,detailBatch)}/>}
        <Button onClick={closeDetail}>返回历史列表并保留筛选</Button>
      </div>}
    </InspectionDrawer>
    <InspectionDrawer open={Boolean(materialBatch)&&visible} active={visible} scrollKey={`${scopeKey}:${materialBatch}:${material?.reference.ref ?? "loading"}`} zIndex={1520} title="原始关联材料预览" context="操作记录 / 处置详情 / 原材料 · 只读"
      browseScope={{portfolioId,key:`history-material:${contextKey}:${materialBatch}:${materialReference?.ref??""}:${materialReference?.version??""}`}}
      focused icon={<FolderSearch size={20}/>}
      onClose={closeMaterial} onBack={closeMaterial} backLabel="返回处置详情">
      <div className="pi-history-panel">
        {materialLoading&&<p role="status">正在读取确切引用的只读材料…</p>}
        {materialError&&<div className="pi-history-error" role="alert">关联材料读取失败：{materialError}<Button onClick={closeMaterial}>返回详情重新选择材料</Button></div>}
        {material&&<MaterialView material={material} onClose={closeMaterial}/>}
      </div>
    </InspectionDrawer>
  </section>;
}

function ResultLine({ batch }: { batch: HistoryBatch }) {
  return <div className="pi-history-results"><div><span>审核决定</span><Tag bordered={false} color={batch.review_decision === "approved" ? "blue" : batch.review_decision === "rejected" ? "red" : undefined}>{REVIEW[batch.review_decision] || "未记录决定"}</Tag></div>
    <div><span>执行结果</span><Tag bordered={false} color={batch.execution_result === "success" ? "green" : ["failed", "partial_failure"].includes(batch.execution_result) ? "red" : undefined}>{EXECUTION[batch.execution_result] || "未知 / 待核验"}</Tag></div></div>;
}
function ObjectTable({ objects, label }: { objects: HistoryObject[]; label: string }) {
  if (!objects.length) return <p className="pi-history-meta">{label}：原审计无结构化对象索引，标的或交易日未知 / 不适用</p>;
  return <div className="pi-history-object-wrap"><table className="pi-history-object-table" aria-label={label}><thead><tr><th>标的</th><th>所属交易日 / 覆盖</th><th>当时结果</th></tr></thead><tbody>{objects.map((obj, index) => <tr key={`${obj.object_id}-${index}`}>
    <td>{obj.name || obj.code || obj.security_id || "标的未知"}<small>{obj.security_id || "市场身份未知"}{obj.name_source === "current_catalogue" ? " · 名称来自当前证券目录" : ""}</small></td>
    <td>{obj.trade_date || (obj.trade_date_index_status === "not_applicable" ? "交易日不适用" : "交易日未知")}{obj.trade_date_to ? ` 至 ${obj.trade_date_to}` : ""}</td><td>{EXECUTION[obj.result] || "未知 / 待核验"}</td>
  </tr>)}</tbody></table></div>;
}
function BatchDetail({ detail, onOpenReference }: { detail: HistoryDetail; onOpenReference?: (reference: HistoryReference) => void }) {
  return <>
    <InspectionConclusion label="当时审核与执行结论" title={`审核${REVIEW[detail.review_decision]||"未知"} · 执行${EXECUTION[detail.execution_result]||"未知"}`} icon={<ClipboardCheck size={19}/>} tone={["failed","partial_failure"].includes(detail.execution_result)?"warning":"neutral"} badge="历史只读"
      description={`${occurred(detail.occurred_at)} · 北京时间 · ${detail.operators.join("、")||"操作人未知"}`} meta={`${detail.event_count} 条原始事件 · ${detail.total_object_count} 个已索引对象`}><ResultLine batch={detail}/></InspectionConclusion>
    <h4>证据与关联来源</h4>
    {detail.references.length ? <ul className="pi-history-references">{detail.references.map((ref, index) => <li key={`${ref.kind}:${ref.ref}:${index}`}>
      {onOpenReference ? <button className="pi-history-reference-link" onClick={() => onOpenReference(ref)}>{ref.kind === "report" ? "原报告" : "关联来源"}：<code>{ref.ref}</code></button> : <span>{ref.kind === "report" ? "原报告" : "关联来源"}：<code>{ref.ref}</code></span>}
      {ref.version && <span> · 版本 {ref.version}</span>}<small>材料状态：{({ available: "可访问", archived: "已归档", missing: "缺失", inaccessible: "不可访问", unknown: "未核验" } as Record<string, string>)[ref.retention_status] || ref.retention_status} · 只读引用</small>
    </li>)}</ul> : <p>原审计未保留关联报告或任务引用。</p>}
    <details className="pi-history-all-objects"><summary>本批完整对象（{detail.total_object_count} 个，含未命中对象）</summary><ObjectTable objects={detail.objects} label="本批完整对象" /></details>
    <h4>处理经过</h4>
    <ol className="pi-history-timeline">{detail.events.map(event => {
      const attrs = record(event.attributes), before = record(event.before), after = record(event.after);
      const stateFrom = attrs.from_state || before.portfolio_state || attrs.portfolio_state_before;
      const stateTo = attrs.to_state || after.portfolio_state || attrs.portfolio_state_after;
      return <li key={event.id}><strong>{event.action_label}</strong><small>{occurred(event.occurred_at)} · 北京时间 · {event.operator_id}</small>
        <p>审核：{REVIEW[event.review_decision]} · 执行：{EXECUTION[event.execution_result]}</p>
        {Boolean(attrs.review_note || event.note) && <p>当时理由：{String(attrs.review_note || event.note)}</p>}
        {Boolean(stateFrom || stateTo) && <p>状态变化：{String(stateFrom || "未知")} → {String(stateTo || "未知")}</p>}
        {event.material_warnings.map(warning => <p key={warning} className="pi-history-warning">{warning}</p>)}
        <details><summary>原始快照与技术引用 · 只读</summary><pre>{JSON.stringify({ event_id: event.id, action: event.action, source_action: event.source_action, source_occurred_at: event.source_occurred_at, business_key: event.business_key,
          correlation_id: event.correlation_id, before: event.before, after: event.after, attributes: event.attributes }, null, 2)}</pre></details>
      </li>;
    })}</ol>
  </>;
}

function MaterialView({ material, onClose }: { material: HistoryMaterial; onClose: () => void }) {
  const data = record(material.data);
  const labels: Record<string, string> = { id: "原始编号", report_version: "原报告版本", evidence_version: "证据版本", baseline_id: "账户基线", verdict: "当时结论", closure_type: "关闭方式", status: "来源当前状态", policy_version: "政策版本", calendar_version: "日历版本", currency: "币种", amount: "金额", balance_after: "余额", trade_date: "所属交易日", coverage_start_trade_date: "覆盖起点", audit_event_id: "原审计编号", accepted_audit_id: "接受审计编号" };
  return <section className="pi-history-material" aria-label="历史引用只读材料">
    <div className="pi-history-inline"><strong>{material.title}</strong><Tag>{({ available: "材料可访问", partial: "原始材料不完整", archived: "已归档", missing: "确切来源缺失", inaccessible: "无权访问", unsupported: "来源尚未接入" })[material.status]}</Tag><Button size="small" onClick={onClose}>返回处置详情并保留位置</Button></div>
    <p>{material.explanation}</p><p className="pi-history-meta">引用：{material.reference.ref}{material.reference.version ? ` / ${material.reference.version}` : ""} · 只读</p>
    <dl>{Object.entries(labels).filter(([key]) => data[key] !== undefined && data[key] !== null).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{String(data[key])}</dd></div>)}</dl>
    {Boolean(data.checks) && <p>原检查项：{Array.isArray(data.checks) ? data.checks.length : Object.keys(record(data.checks)).length} 项；未重新运行当前检查。</p>}
    {Array.isArray(data.findings) && <p>原报告差异 / 缺口：{data.findings.length} 项。</p>}
    {material.missing_sources.length > 0 && <div className="pi-history-warning">缺少来源：<ul>{material.missing_sources.map(source => <li key={source}>{source}</li>)}</ul>原决定保留，未知不会填成通过。</div>}
    <details><summary>完整保留材料与原审计快照 · 只读</summary><pre>{JSON.stringify({ material: material.data, original_audit: material.original_audit, content_hash: material.content_hash }, null, 2)}</pre></details>
  </section>;
}
