import {AcceptanceButton as Button,AcceptanceModal as Modal} from './AcceptanceControls';
import { useEffect, useMemo, useRef, useState } from "react";
import { Checkbox, Input, Progress, Select } from "antd";
import { ChevronDown, Database, RefreshCw } from "lucide-react";
import { api } from "../../../api/client";
import InspectionDrawer from "./InspectionDrawer";
import { repairNextAction, repairObjectMessage } from "./inspectionRepairPresentation";
import type { InspectionEvidence, InspectionRepairOperation, InspectionRepairSubmit, InspectionRepairTask, InspectionSymbol, PortfolioInspectionDiagnostics } from "../../../types/portfolioInspection";
import {preparedFromSubmission,readRepairSubmission,recoverRepairSubmission,sendRepairSubmission,submissionBlocksNew,subscribeRepairSubmission,type RepairSubmission} from './repairSubmissionJournal';

const LABELS: Record<string, string> = { PASSED: "有效", MISSING: "缺失", STALE: "过期", READ_ERROR: "读取失败", VERSION_MISMATCH: "版本不匹配", FUTURE_DATA: "数据时点不符", NOT_DUE: "尚未到期", CALENDAR_UNKNOWN: "日历待核实", UNKNOWN: "未知", NOT_APPLICABLE: "不适用" };
const displayValue = (value: unknown) => value == null ? "未知" : typeof value === "object" ? JSON.stringify(value) : String(value);
const healthy = (e: InspectionEvidence) => ["PASSED", "NOT_APPLICABLE", "NOT_DUE"].includes(e.status);
const TASK_LABELS: Record<string,string> = { pending: "待执行", queued: "排队中", running: "执行中", succeeded: "产物已生成", done: "执行完成", failed: "执行失败", dependency_failed: "依赖步骤失败", cancelled: "已取消", partial: "部分失败", expired: "已过期", stalled: "执行中断", PASSED: "重检通过", READ_ERROR: "重检读取失败", UNKNOWN: "待核实", STILL_BLOCKED: "仍有未通过项", RECHECK_FAILED: "重检读取失败", REMAINING: "仍有未通过项" };
const terminal = (status: string) => ["done", "completed", "succeeded", "failed", "cancelled", "expired", "stalled", "partial"].includes(status.toLowerCase());
const errorMessage = (error: unknown) => error instanceof Error ? error.message : "读取失败，请重试";
const newKey = () => `inspection-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
function readRepairDraft(portfolioId:number):Record<string,any>|null {
  try {const value=JSON.parse(sessionStorage.getItem("portfolio-inspection-data-return-v1")??"null");
    if(value?.portfolioId===portfolioId&&Date.now()-value.savedAt<30*60*1000)return value;
  }catch{/* Browse drafts cannot authorize repair execution. */}
  return null;
}

export interface InspectionDataPanelProps {
  portfolioId: number;
  data: PortfolioInspectionDiagnostics | null;
  loading: boolean;
  error: string | null;
  onReload: () => Promise<void>;
  onHistory: (symbol?: InspectionSymbol) => void;
  onNavigate: (target: string) => void;
  onAnalyse?: (symbol: InspectionSymbol) => void;
  onConfirmationChange?: (open: boolean) => void;
  confirmationReady?: boolean;
  covered?: boolean;
}

export default function InspectionDataPanel({ portfolioId, data, loading, error, onReload, onHistory, onNavigate, onAnalyse, onConfirmationChange, confirmationReady = true, covered=false }: InspectionDataPanelProps) {
  const savedDraft=useRef(readRepairDraft(portfolioId));
  const [view, setView] = useState<"cause" | "asset" | "results">("cause");
  const [query, setQuery] = useState<string>(savedDraft.current?.query??"");
  const [filters, setFilters] = useState({ cause: "", date: "", config: "", repair: "", process: "" });
  const [recentTasks, setRecentTasks] = useState<InspectionRepairTask[]>([]);
  const [recentError, setRecentError] = useState<string | null>(null);
  const [taskCursor, setTaskCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [detailSymbolId,setDetailSymbolId]=useState<number|null>(null);
  const [confirm, setConfirm] = useState(false);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [task, setTask] = useState<InspectionRepairTask | null>(null);
  const [taskPreview,setTaskPreview]=useState<InspectionRepairTask|null>(null);
  const [failureDetail,setFailureDetail]=useState<{task:InspectionRepairTask;object:Record<string,unknown>}|null>(null);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [retryMode, setRetryMode] = useState(false);
  const generation = useRef(0);
  const submitting=useRef(false);
  const uncertainRequest=useRef(false);
  const submittedKey = useRef(newKey());
  const pending = useRef<{ request: InspectionRepairSubmit; operations: InspectionRepairOperation[]; assetCount: number; retryTaskId: string | null; retryCount: number } | null>(null);
  const plan = data?.repair_plan;
  const initialSelection = useRef(false);
  const returnTaskId = useRef<string|null>(null);
  const [submission,setSubmission]=useState<RepairSubmission|null>(null);
  const submissionRef=useRef(submission);submissionRef.current=submission;
  const [submissionError,setSubmissionError]=useState<string|null>(null);
  const [recovery,setRecovery]=useState<'idle'|'checking'|'found'|'missing'|'error'>('idle');
  const recoveryRequest=useRef(0);
  useEffect(()=>{
    const read=()=>{try{const record=readRepairSubmission(portfolioId);if(!record&&submissionRef.current?.portfolioId===portfolioId&&submissionRef.current.state==='uncertain')throw new Error('原请求的持久记录已缺失，已停止新提交。请恢复原记录或先核查已有任务，不能将记录缺失视为未提交。');setSubmission(previous=>JSON.stringify(previous)===JSON.stringify(record)?previous:record);setSubmissionError(null);
      if(record&&pending.current&&pending.current.request.idempotency_key!==record.request.idempotency_key&&record.state==='uncertain'){setConfirm(false);setAck(false);pending.current=null;}
    }catch(error){setSubmissionError(errorMessage(error));setConfirm(false);setAck(false);}};
    setSubmission(null);setRecovery('idle');read();const stop=subscribeRepairSubmission(portfolioId,read);
    return()=>{stop();recoveryRequest.current++;};
  },[portfolioId]);
  useEffect(() => { onConfirmationChange?.(confirm); }, [confirm, onConfirmationChange]);
  useEffect(() => {
    generation.current++;submitting.current=false;uncertainRequest.current=false;pending.current=null; initialSelection.current = false; setSelected([]); setDetailSymbolId(null); setTaskPreview(null); setFailureDetail(null); setTask(null); setTaskError(null); setConfirm(false); setBusy(false); setQuery(""); setView("cause");
    setFilters({cause:"",date:"",config:"",repair:"",process:""});returnTaskId.current=null;
    try {
      if(savedDraft.current?.portfolioId!==portfolioId)savedDraft.current=readRepairDraft(portfolioId);
      const saved=savedDraft.current;
      if (saved?.portfolioId === portfolioId && Date.now() - saved.savedAt < 30 * 60 * 1000) {
        initialSelection.current = true; setSelected(Array.isArray(saved.selected) ? saved.selected : []); setDetailSymbolId(typeof saved.detailSymbolId==="number"?saved.detailSymbolId:null); setQuery(saved.query ?? "");
        if(saved.filters&&typeof saved.filters==="object")setFilters(old=>({...old,...saved.filters}));
        if(["cause","asset","results"].includes(saved.view))setView(saved.view);
        if(typeof saved.taskId==="string")returnTaskId.current=saved.taskId;
      }
    } catch { /* Optional navigation draft; it never grants execution rights. */ }
    return () => { generation.current++; };
  }, [portfolioId]);
  const draftSnapshot=useRef<Record<string,unknown>>({});
  draftSnapshot.current={portfolioId,selected,detailSymbolId,query,filters,view,taskId:task?.task_id??returnTaskId.current??savedDraft.current?.taskId,savedAt:Date.now()};
  useEffect(()=>{try{sessionStorage.setItem("portfolio-inspection-data-return-v1",JSON.stringify(draftSnapshot.current));}catch{/* Preserve normal operation without local storage. */}},[portfolioId,selected,detailSymbolId,query,filters,view,task?.task_id]);
  useEffect(()=>()=>{try{sessionStorage.setItem("portfolio-inspection-data-return-v1",JSON.stringify({...draftSnapshot.current,savedAt:Date.now()}));}catch{/* Local drafts grant no execution rights. */}},[]);
  const navigateWithDraft = (target: string) => {
    try { sessionStorage.setItem("portfolio-inspection-data-return-v1", JSON.stringify({portfolioId,selected,detailSymbolId,query,filters,view,taskId:task?.task_id,savedAt:Date.now()})); } catch { /* The official destination remains usable. */ }
    onNavigate(target);
  };
  const retainTask = (value: InspectionRepairTask) => {
    if(value.portfolio_id!==portfolioId)throw new Error("任务结果不属于当前组合，请重新读取原任务");
    setTask(value);
    setRecentTasks(old=>[value,...old.filter(row=>row.task_id!==value.task_id)]);
  };
  const recoverOriginal=async(record:RepairSubmission|null=submission)=>{
    if(!record||record.state==='rejected')return;
    const token=++recoveryRequest.current,current=generation.current;setRecovery('checking');setSubmissionError(null);
    try{const original=await recoverRepairSubmission(record,api.getPortfolioInspectionRepairTask);
      if(token!==recoveryRequest.current||current!==generation.current)return;
      retainTask(original);setView('results');setRecovery('found');
    }catch(error){if(token!==recoveryRequest.current||current!==generation.current)return;const status=(error as {status_code?:number})?.status_code;
      setRecovery(status===404?'missing':'error');setSubmissionError(status===404?'暂未查到原任务，不能据此认定未提交。原请求标识继续保留，可重新读取，或核对原范围后由本人按同一请求再试。':`原任务暂时无法核实：${errorMessage(error)}。原请求继续保留，不会自动重提。`);}
  };
  useEffect(()=>{if(submission&&submission.state!=='rejected')void recoverOriginal();},[submission?.request.idempotency_key,submission?.state,submission?.taskStatus,portfolioId]);
  const reviewOriginal=()=>{if(!submission||submission.state!=='uncertain'||recovery!=='missing')return;pending.current=preparedFromSubmission(submission);submittedKey.current=submission.request.idempotency_key;setRetryMode(Boolean(submission.parentTaskId));setAck(false);setTaskError('这是保留的原请求范围。再次确认仅使用原标识，不生成另一批请求；仍由后台校验原范围与当前权限。');setConfirm(true);};
  const loadRecent = async (cursor?: string) => {
    const current = generation.current;
    try {
      const page = await api.getPortfolioInspectionRepairTasks(portfolioId, cursor);
      if (current !== generation.current) return;
      if (!page || page.portfolio_id !== portfolioId || !Array.isArray(page.items)) throw new Error("任务列表材料不完整或不属于当前组合");
      setRecentTasks(old => cursor ? [...old.filter(t => !page.items.some(x => x.task_id === t.task_id)), ...page.items] : page.items);
      setTaskCursor(page.next_cursor); setRecentError(null);
      if (!cursor) {
        const original=readRepairSubmission(portfolioId);if(original&&original.state!=='rejected'){returnTaskId.current=null;return;}
        if(returnTaskId.current){const id=returnTaskId.current;returnTaskId.current=null;
          const retained=page.items.find(row=>row.task_id===id)??await api.getPortfolioInspectionRepairTask(portfolioId,id);
          if(current!==generation.current)return;
          if(retained.portfolio_id===portfolioId)setTask(retained);else throw new Error("返回任务不属于当前组合");
        }else setTask(old => old ?? page.items.find(t => !terminal(t.status)) ?? null);
      }
    } catch (e) { if (current === generation.current) setRecentError(`任务记录读取失败：${errorMessage(e)}`); }
  };
  useEffect(() => { setRecentTasks([]); setTaskCursor(null); void loadRecent(); }, [portfolioId]);
  useEffect(() => {
    if (!plan) return;
    if (!initialSelection.current) {
      initialSelection.current = true;
      setSelected(plan.operations.filter(x => x.enabled).map(x => x.operation_id));
    } else setSelected(old => old.filter(id => plan.operations.some(x => x.operation_id === id && x.enabled)));
  }, [plan?.plan_id]);
  useEffect(() => {
    if (!task || terminal(task.status)) return;
    const current = generation.current;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await api.getPortfolioInspectionRepairTask(portfolioId, task.task_id);
        if (stopped || current !== generation.current) return;
        retainTask(next); setTaskError(null);
        if (terminal(next.status)) { await onReload(); return; }
      } catch (e) { if (!stopped && current === generation.current) { setTaskError(`任务进度读取失败：${errorMessage(e)}。原任务不会重复提交。`); return; } }
      if (!stopped) timer = setTimeout(poll, 2000);
    };
    timer = setTimeout(poll, 1000);
    return () => { stopped = true; clearTimeout(timer); };
  }, [portfolioId, task?.task_id, task?.status, onReload]);

  const operations = plan?.operations ?? [];
  const actualSelection = useMemo(() => {
    const ids = new Set(selected);
    const visit = (id: string) => { const op = operations.find(x => x.operation_id === id); for (const dep of op?.depends_on ?? []) if (!ids.has(dep)) { ids.add(dep); visit(dep); } };
    selected.forEach(visit);
    return operations.filter(op => ids.has(op.operation_id));
  }, [selected, operations]);
  const active = busy || Boolean(task && !terminal(task.status));
  const globalFailures = data?.evidence.filter(e => !healthy(e)) ?? [];
  const reinspection = task?.reinspection as (Partial<PortfolioInspectionDiagnostics> & { status?: string; reason?: string }) | null | undefined;
  const reinspectionFailures = reinspection?.symbols?.flatMap(x => x.evidence.filter(e => !healthy(e))) ?? [];
  const reinspectionGlobals = reinspection?.evidence?.filter(e => !healthy(e)) ?? [];
  const reinspectionPassed = Boolean(reinspection && reinspection.status !== "READ_ERROR" && Array.isArray(reinspection.symbols) && !reinspectionFailures.length && !reinspectionGlobals.length);
  const taskFailed = Boolean(task?.failed_count || task && ["failed","cancelled","partial","expired","stalled"].includes(task.status.toLowerCase()));
  const symbols = (data?.symbols ?? []).filter(x => x.evidence.some(e => !healthy(e)));
  const roots = (symbol: InspectionSymbol) => {
    const failed = symbol.evidence.filter(e => !healthy(e));
    return failed.filter(e => !e.depends_on.some(id => failed.some(upstream => upstream.evidence_id === id)));
  };
  const configKey = (s: InspectionSymbol) => String(s.scoring?.actual_config_name ?? s.scoring?.current_config_name ?? s.scoring?.expected_model_run_id ?? "未提供配置名称");
  const visible = symbols.filter(x => `${x.name} ${x.symbol} ${x.market}`.toLowerCase().includes(query.toLowerCase())
    && (!filters.cause || roots(x).some(e => e.check_code === filters.cause)) && (!filters.date || x.expected_trade_date === filters.date)
    && (!filters.config || configKey(x) === filters.config)
    && (!filters.repair || (operations.some(op => op.symbol_id === x.symbol_id && op.enabled) ? "auto" : "manual") === filters.repair)
    && (!filters.process || (task?.objects.some(o => o.symbol_id === x.symbol_id && String(o.status) === filters.process) ?? false)));
  const selectedAssets = new Set(actualSelection.map(x => x.symbol_id).filter(x => x != null));
  const selectedHidden = actualSelection.filter(x => x.symbol_id != null && !visible.some(y => y.symbol_id === x.symbol_id)).length;
  const setSymbol = (symbol: InspectionSymbol, checked: boolean) => {
    const ids = operations.filter(x => x.symbol_id === symbol.symbol_id && x.enabled).map(x => x.operation_id);
    setSelected(old => checked ? [...new Set([...old, ...ids])] : old.filter(id => !ids.includes(id)));
  };
  const visibleIds = operations.filter(op => op.enabled && visible.some(s => s.symbol_id === op.symbol_id)).map(op => op.operation_id);
  const toggleIds = (ids: string[], checked: boolean) => setSelected(old => checked ? [...new Set([...old, ...ids])] : old.filter(id => !ids.includes(id)));
  const cancelTask = async () => {
    if (!task || busy) return;
    const current = generation.current; setBusy(true);
    try { const next = await api.cancelPortfolioInspectionRepair(portfolioId, task.task_id); if (current === generation.current) { retainTask(next); setTaskError(null); } }
    catch (e) { if (current === generation.current) setTaskError(errorMessage(e)); }
    finally { if (current === generation.current) setBusy(false); }
  };
  const prepare = (retry = false) => {
    if (!plan||submitting.current||busy) return;
    if(submissionError||submissionBlocksNew(submission)){setTaskError('请先核查保留的原请求或原任务，不另起一笔提交。');return;}
    submittedKey.current = newKey();
    pending.current = { request: { plan_id: plan.plan_id, snapshot_id: plan.snapshot_id, generated_at: plan.generated_at,
      operation_ids: actualSelection.map(x => x.operation_id), confirmed: true, idempotency_key: submittedKey.current },
      operations: actualSelection, assetCount: selectedAssets.size, retryTaskId: retry ? task?.task_id ?? null : null,
      retryCount: retry ? task?.retryable_operation_ids.length ?? 0 : 0 };
    setRetryMode(retry); setAck(false); setTaskError(null); setConfirm(true);
  };
  const submit = async () => {
    if (!ack || busy || !plan||submitting.current) return;
    const current = generation.current;submitting.current=true;let received=false; setBusy(true); setTaskError(null);
    try {
      const frozen = pending.current;
      if (!frozen) throw new Error("请先预览当前修复计划");
      const result = await sendRepairSubmission(portfolioId,frozen,()=>frozen.retryTaskId
        ? api.retryPortfolioInspectionRepair(portfolioId, frozen.retryTaskId, frozen.request.idempotency_key)
        : api.submitPortfolioInspectionRepair(portfolioId, frozen.request),api.getPortfolioInspectionRepairTask);
      if (current !== generation.current) return;
      retainTask(result);received=true;uncertainRequest.current=false; setConfirm(false); setView("results");
      if (terminal(result.status)) await onReload();
    } catch (e) { if (current === generation.current) {setAck(false);const status=(e as {status_code?:number})?.status_code,notSent=(e as {error_code?:string})?.error_code==="OFFLINE_NOT_SENT",unknown=!received&&!notSent&&(status===0||status===408||(status!=null&&status>=500));const code=(e as {error_code?:string;detail?:{code?:string}})?.error_code??(e as {detail?:{code?:string}})?.detail?.code;uncertainRequest.current=["REPAIR_PLAN_EXPIRED","INSPECTION_SNAPSHOT_CHANGED"].includes(code??"")?false:uncertainRequest.current||unknown;try{const original=readRepairSubmission(portfolioId);if(original&&original.state!=="rejected")void recoverOriginal(original);}catch{/* The original storage error remains visible. */}setTaskError(received?"原任务已接收，最新数据读取失败，请刷新原任务进度。":notSent?errorMessage(e):unknown?"提交结果暂时无法确认，请先读取原任务记录，确认是否已接收。系统不会自动重提任务。":errorMessage(e));} }
    finally { if (current === generation.current) {submitting.current=false;setBusy(false);} }
  };
  const readTask = async () => {
    if (!task) return;
    const current = generation.current;
    try { const result = await api.getPortfolioInspectionRepairTask(portfolioId, task.task_id); if (current === generation.current) { retainTask(result); setTaskError(null); if (terminal(result.status)) await onReload(); } }
    catch (e) { if (current === generation.current) setTaskError(errorMessage(e)); }
  };
  const evidenceTable = (symbol: InspectionSymbol) => <div className="pi-asset-evidence">
    <p>所属交易日：{symbol.expected_trade_date ?? "未知"} · 行情实际：{symbol.actual_bar_trade_date ?? "未提供"} · 评分实际：{symbol.actual_score_trade_date ?? "未提供"}</p>
    <dl className="pi-facts"><div><dt>实际评分配置</dt><dd>{displayValue(symbol.scoring?.actual_config_name ?? symbol.scoring?.current_config_name)} · 版本 {displayValue(symbol.scoring?.actual_config_version ?? symbol.scoring?.current_config_version)}</dd></div><div><dt>实际模型引用</dt><dd>{displayValue(symbol.scoring?.actual_model_run_id ?? symbol.scoring?.expected_model_run_id ?? "当前评分未绑定模型")}</dd></div></dl>
    <div className="pi-table-wrap"><table><thead><tr><th>检查项</th><th>实际 / 要求</th><th>结论</th><th>具体原因与来源</th></tr></thead><tbody>{symbol.evidence.map(e => <tr key={e.evidence_id}><td>{e.label}</td><td>{displayValue(e.value)}<small>要求 {displayValue(e.expected)}{e.trading_day_lag != null ? ` · 滞后 ${e.trading_day_lag} 个交易日` : ""}</small></td><td><span className={`pi-tag ${healthy(e) ? "pi-ok" : e.status === "READ_ERROR" ? "pi-danger" : "pi-warn"}`}>{LABELS[e.status] ?? "未知"}</span></td><td>{e.reason}<small>{e.source_label}{e.expected_trade_date && ` · 应到 ${e.expected_trade_date}`}{e.actual_trade_date && ` · 实际 ${e.actual_trade_date}`}</small></td></tr>)}</tbody></table></div>
    {operations.filter(x => x.symbol_id === symbol.symbol_id && !x.enabled).map(op => <div className="pi-callout pi-warning" key={op.operation_id}><strong>{op.label}</strong><p>{op.disabled_reason ?? "此动作尚不具备可靠执行条件"}</p><Button onClick={() => navigateWithDraft(op.target)}>前往对应配置核实</Button></div>)}
    <details className="pi-disclosure"><summary>来源时点、依赖与影响能力</summary>{symbol.evidence.map(e => <p key={e.evidence_id}><strong>{e.label}</strong> · 可用时间 {e.available_at ?? "未提供"} · 检查时间 {e.observed_at ?? "未提供"} · 允许滞后 {displayValue(e.metadata?.allowed_lag)} · 影响 {e.affected_capabilities?.join("、") || "仅解释性证据"}<small>来源引用 {e.source_record_ref ?? "未保留"} · 依赖 {e.depends_on.join("、") || "无"}</small></p>)}</details>
    <div className="pi-operation-choices"><strong>本对象的修复步骤</strong>{operations.filter(op => op.symbol_id === symbol.symbol_id).map(op => <Checkbox key={op.operation_id} disabled={active || !op.enabled} checked={actualSelection.some(x => x.operation_id === op.operation_id)} onChange={e => toggleIds([op.operation_id], e.target.checked)}>{op.label} · {op.trade_date ?? "待核实日期"}<small>{op.enabled ? op.expected_effect : op.disabled_reason}</small></Checkbox>)}</div>
    <Button type="link" onClick={() => onHistory(symbol)}>该标的相关历史处理</Button>
    {onAnalyse && <Button type="link" onClick={() => onAnalyse(symbol)}>AI 分析此标的缺口</Button>}
  </div>;
  const asset = (symbol: InspectionSymbol) => {
    const ops = operations.filter(x => x.symbol_id === symbol.symbol_id && x.enabled);
    const n = ops.filter(x => selected.includes(x.operation_id)).length;
    return <article className="pi-asset" key={symbol.symbol_id}><div className="pi-asset-head"><Checkbox checked={ops.length > 0 && n === ops.length} indeterminate={n > 0 && n < ops.length} disabled={active || !ops.length} onChange={e => setSymbol(symbol, e.target.checked)}><span><strong>{symbol.name} <small>{symbol.symbol}</small></strong><small>应到 {symbol.expected_trade_date ?? "待核实"} · {symbol.market}</small></span></Checkbox><Button type="link" onClick={() => setDetailSymbolId(symbol.symbol_id)}>查看证据</Button></div></article>;
  };
  const grouped = new Map<string, InspectionSymbol[]>();
  visible.forEach(symbol => { for (const root of roots(symbol)) grouped.set(root.check_code, [...(grouped.get(root.check_code) ?? []), symbol]); });

  return <div className="pi-data-panel">
    {(submission||submissionError)&&<section className="pi-callout pi-warning" aria-label="原请求恢复"><strong>{submission?.state==='known'?'已保留原请求与任务关联':submission?.state==='rejected'?'原请求已明确拒绝，可重新核实当前计划':'发现一笔待核实的原提交'}</strong>
      <p>{submission?.state==='known'?'重新进入只读取原任务，不重新提交。任务完成不代表数据校验或交易权限已通过。':'原请求标识已保留在本浏览器，刷新、关闭重进和其他标签页可继续核查；不会恢复勾选或自动发送任务。'}</p>
      {submissionError&&<p role="alert">{submissionError}</p>}
      {submission&&submission.state!=='rejected'&&<Button loading={recovery==='checking'} onClick={()=>void recoverOriginal()}>重新读取原请求对应任务</Button>}
      {submission?.state==='uncertain'&&recovery==='missing'&&<Button mutation onClick={reviewOriginal}>核对原请求并重试</Button>}
    </section>}
    {loading && <p role="status">正在读取实际输入、评分与依赖证据…</p>}
    {error && <div className="pi-callout pi-error" role="alert"><strong>数据诊断读取失败</strong><p>{error}</p><Button onClick={() => void onReload()}>重新读取</Button></div>}
    {Boolean(data || task || recentTasks.length || recentError) && <>
      <div className={`pi-callout ${symbols.length || globalFailures.length ? "pi-warning" : "pi-success"}`}><strong>{globalFailures.length ? `组合级还有 ${globalFailures.length} 项证据待核实；` : ""}{symbols.length} / {data?.symbols.length ?? "诊断未读取"} 个标的有待核实证据</strong><p>按真实行情、实际评分配置和冻结依赖逐项检查。诊断结果不代替运行门禁，修复完成后仍须重检和本人审核。</p></div>
      {data?.evidence.filter(e => !healthy(e)).map(e => <div className="pi-callout pi-warning" key={e.evidence_id}><strong>{e.label} · {LABELS[e.status]}</strong><p>{e.reason}</p></div>)}
      <div className="pi-segments" aria-label="数据诊断视图">{([['cause', '按根因整合'], ['asset', '按标的查看'], ['results', '本批结果']] as const).map(([key,label]) => <Button key={key} aria-pressed={view === key} onClick={() => setView(key)}>{label}</Button>)}</div>
      <div className="pi-panel-actions pi-sticky-actions"><span>{submissionBlocksNew(submission)?'先核查原请求，不重复提交。':active ? "原任务仍在执行，可查看进度或停止后续步骤。" : !actualSelection.length ? "请先选择可执行对象或步骤。" : "仅执行本人确认范围，成功后保留重检结论。"}</span><Button mutation type="primary" disabled={Boolean(submissionError)||submissionBlocksNew(submission)||active || !actualSelection.length || actualSelection.some(x => !x.enabled)} loading={busy} onClick={() => prepare()}>预览批量修复（{actualSelection.length} 步）</Button></div>
      <details className="pi-disclosure"><summary>已持久保存的修复任务 · {recentTasks.length} 条</summary><Button onClick={() => void loadRecent()}>刷新任务记录</Button>{recentError && <p role="alert">{recentError}</p>}{recentTasks.map(t => <div className="pi-recent-task" key={t.task_id}><span>{TASK_LABELS[t.status] ?? "待核实"} · {t.created_at ?? "创建时点未保留"}<small>{t.message} · {t.task_id}</small></span><Button onClick={() => setTaskPreview(t)}>查看原任务</Button></div>)}{taskCursor && <Button onClick={() => void loadRecent(taskCursor)}>加载更早任务</Button>}<p>再次进入可续看原任务；不会自动重复提交。</p></details>
      {view === "results" ? task ? <section className="pi-task-results"><h3>{task.message || "本批任务结果"}</h3><p>本批 {task.total} 步 · 完成 {task.processed} · 成功 {task.ok_count} · 失败 {task.failed_count}</p><Progress percent={Math.max(0, Math.min(100, task.percent))} status={taskFailed ? "exception" : terminal(task.status) && reinspectionPassed ? "success" : "active"} />
        <div className="pi-table-wrap"><table><thead><tr><th>对象 / 步骤</th><th>产物执行</th><th>数据重检</th><th>原因与处理</th></tr></thead><tbody>{task.objects.map((object, i) => <tr key={String(object.operation_id ?? i)}><td>{String(object.label ?? object.symbol ?? "检查对象")}</td><td>{TASK_LABELS[String(object.status)] ?? "待核实"}</td><td>{object.reinspection_status ? <strong>{TASK_LABELS[String(object.reinspection_status)] ?? "重检待核实"}</strong> : "尚未重检"}</td><td><p>{repairObjectMessage(object,terminal(task.status))}</p>{typeof object.source_label==="string"&&<small>数据来源：{object.source_label}</small>}{["failed","dependency_failed","cancelled"].includes(String(object.status))&&<Button type="link" onClick={()=>setFailureDetail({task,object})}>查看失败详情</Button>}</td></tr>)}</tbody></table></div>
        {terminal(task.status) && <div className={`pi-callout ${reinspectionPassed ? "pi-success" : "pi-warning"}`}><strong>{reinspectionPassed ? "本次数据重检通过" : reinspection?.status === "READ_ERROR" ? "产物处理已结束，重检读取失败" : "产物处理已结束，数据仍需继续核实"}</strong><p>{reinspection?.reason ?? (reinspection ? `当前仍有 ${reinspectionFailures.length} 项标的证据、${reinspectionGlobals.length} 项组合级证据未通过。` : "尚未取得有效重检报告，请重新检测；任务完成不能代替数据校验。")}</p></div>}
        <p className="pi-muted">任务执行成功与数据重检通过分别保留；成功步骤不会被失败重试重复执行。{!plan && "当前诊断未读到，仍可查看或停止原任务；重新取得有效计划后才能重试。"}</p>
        {task.recovery?.reason && <p>{task.recovery.reason}</p>}<Button icon={<RefreshCw size={14}/>} onClick={() => void readTask()}>刷新原任务进度</Button>{task.can_cancel && <Button mutation loading={busy} onClick={() => void cancelTask()}>停止尚未开始的步骤</Button>}{task.cancel_requested && <p>已请求停止；当前步骤完成后停止后续步骤，已成功产物保留。</p>}{terminal(task.status) && task.retryable_operation_ids.length > 0 && <Button mutation disabled={!plan || busy} onClick={() => prepare(true)}>仅重试失败与依赖受阻步骤</Button>}
      </section> : <div className="pi-empty">尚未提交修复任务。先选择对象，预览限定计划。</div> : <>
        <Input aria-label="搜索待修复标的" placeholder="搜索标的名称 / 代码" value={query} onChange={e => setQuery(e.target.value)} />
        <div className="pi-batch-filters">{([
          ["cause", "根因", [...new Map(symbols.flatMap(s => roots(s)).map(e => [e.check_code, { value: e.check_code, label: e.label }])).values()]],
          ["date", "交易日", [...new Set(symbols.map(s => s.expected_trade_date).filter(Boolean))].map(value => ({ value, label: value }))],
          ["config", "实际评分配置", [...new Set(symbols.map(configKey))].map(value => ({ value, label: value }))],
          ["repair", "处理方式", [{ value: "auto", label: "可批量修复" }, { value: "manual", label: "需人工核实" }]],
          ["process", "步骤状态", ["pending","running","succeeded","failed","dependency_failed","cancelled"].map(value => ({ value, label: TASK_LABELS[value] }))],
        ] as const).map(([key, label, options]) => <Select key={key} aria-label={`筛选${label}`} placeholder={label} allowClear value={filters[key] || undefined} options={[...options]} onChange={value => setFilters(old => ({ ...old, [key]: value ?? "" }))} />)}</div>
        <p className="pi-muted">{symbols.length} 个标的 · {symbols.length} 个实际检查对象 · {new Set(symbols.flatMap(s => roots(s).map(e => e.check_code))).size} 类根因 · {symbols.filter(s => operations.some(op => op.symbol_id === s.symbol_id && op.enabled)).length} 个可修复 / {symbols.filter(s => !operations.some(op => op.symbol_id === s.symbol_id && op.enabled)).length} 个需核实。当前实际配置各占一个检查对象，同一对象可涉及多个根因，计数按对象去重。</p>
        <div className="pi-selection"><span>已选 {selectedAssets.size} 个标的 / {actualSelection.length} 个去重步骤{selectedHidden ? ` · 含筛选外 ${selectedHidden} 步` : ""}</span><Checkbox disabled={active || !visibleIds.length} checked={visibleIds.length > 0 && visibleIds.every(id => selected.includes(id))} indeterminate={visibleIds.some(id => selected.includes(id)) && !visibleIds.every(id => selected.includes(id))} onChange={e => toggleIds(visibleIds, e.target.checked)}>全选筛选结果</Checkbox><Button type="link" disabled={active} onClick={() => setSelected(old => old.filter(id => visibleIds.includes(id)))}>仅保留可见选择</Button><Button type="link" disabled={active} onClick={() => setSelected([])}>清空全部选择</Button></div>
        {view === "asset" ? visible.map(asset) : [...grouped.entries()].map(([code, rows]) => { const ids = operations.filter(op => op.enabled && rows.some(s => s.symbol_id === op.symbol_id) && op.resolves_evidence_ids?.some(id => rows.some(s => s.evidence.some(e => e.evidence_id === id && e.check_code === code)))).map(op => op.operation_id); return <details className="pi-root-group" key={code} open><summary><Database size={16}/><strong>{rows[0].evidence.find(e => e.check_code === code)?.label ?? "待核实的数据依赖"}</strong><span>{rows.length} 个标的</span><ChevronDown size={14}/></summary><Checkbox disabled={active || !ids.length} checked={ids.length > 0 && ids.every(id => selected.includes(id))} indeterminate={ids.some(id => selected.includes(id)) && !ids.every(id => selected.includes(id))} onChange={e => toggleIds(ids, e.target.checked)}>选择本根因相关修复步骤</Checkbox>{rows.map(asset)}</details>; })}
        {!visible.length && <div className="pi-empty">当前条件没有待处理标的。可调整搜索，或查看组合级证据。</div>}
      </>}
      {taskError && <div role="alert" className="pi-callout pi-error">{taskError}</div>}
      {Boolean(plan?.limitations?.length) && <details className="pi-disclosure"><summary>当前修复能力与限制</summary>{plan?.limitations.map((x,i) => <p key={i}>{x}</p>)}</details>}

    </>}
    <InspectionDrawer open={Boolean(taskPreview)&&!covered} active={!failureDetail&&!confirm} zIndex={1500} title="已保存修复任务详情" context="数据中心 / 组合数据修复 / 任务记录"
      onClose={()=>setTaskPreview(null)} onBack={()=>setTaskPreview(null)} backLabel="返回任务列表">
      {taskPreview&&<section><h3>{taskPreview.message||"原修复任务"}</h3><p>{TASK_LABELS[taskPreview.status]??"状态待核实"} · 总计 {taskPreview.total} 步 · 已处理 {taskPreview.processed} · 成功 {taskPreview.ok_count} · 失败 {taskPreview.failed_count}</p>
        <div className="pi-table-wrap"><table><thead><tr><th>对象 / 步骤</th><th>执行结果</th><th>说明</th></tr></thead><tbody>{taskPreview.objects.map((object,index)=><tr key={String(object.operation_id??index)}><td>{String(object.label??object.symbol??"检查对象")}</td><td>{TASK_LABELS[String(object.status)]??"待核实"}</td><td>{repairObjectMessage(object,terminal(taskPreview.status))}{["failed","dependency_failed"].includes(String(object.status))&&<Button type="link" onClick={()=>setFailureDetail({task:taskPreview,object})}>查看失败详情</Button>}</td></tr>)}</tbody></table></div>
        <p className="pi-muted">原任务执行结果和数据重检结论分别保留。查看记录不会重试、取消或重新提交任务。</p>
        <Button onClick={()=>{setTask(taskPreview);setView("results");setTaskPreview(null);}}>继续处理此原任务</Button>
      </section>}
    </InspectionDrawer>
    <InspectionDrawer open={detailSymbolId!==null&&!covered} active={!confirm&&!covered&&!failureDetail} zIndex={1500} title="标的数据证据与修复步骤" context="数据中心 / 组合数据修复 / 标的详情"
      onClose={()=>setDetailSymbolId(null)} onBack={()=>setDetailSymbolId(null)} backLabel="返回批量列表">
      {data?.symbols.find(symbol=>symbol.symbol_id===detailSymbolId)?evidenceTable(data.symbols.find(symbol=>symbol.symbol_id===detailSymbolId)!):<p>标的已不在当前诊断中，请返回列表重新选择。</p>}
    </InspectionDrawer>
    <InspectionDrawer open={Boolean(failureDetail)&&!covered&&!confirm} title="同步失败详情" context="数据中心 / 组合数据修复 / 原任务结果" zIndex={1560}
      onClose={()=>setFailureDetail(null)} onBack={()=>setFailureDetail(null)} backLabel={taskPreview?"返回原任务详情":"返回本批结果"}>
      {failureDetail&&<section aria-label="修复步骤失败详情"><h3>{String(failureDetail.object.label??failureDetail.object.symbol??"失败步骤")}</h3>
        <div className="pi-callout pi-warning"><strong>失败原因</strong><p>{repairObjectMessage(failureDetail.object,true)}</p></div>
        <dl className="pi-facts"><div><dt>数据来源</dt><dd>{String(failureDetail.object.source_label??"原任务未保留具体来源")}</dd></div><div><dt>本次同步范围</dt><dd>{String(failureDetail.object.start_date??failureDetail.object.trade_date??"待核实")} 至 {String(failureDetail.object.end_date??failureDetail.object.trade_date??"待核实")}</dd></div></dl>
        <h3>下一步怎么处理</h3><p>{repairNextAction(failureDetail.object)}</p><p className="pi-muted">查看详情不会重新同步。失败对象不会显示为通过，已成功的其他标的数据保留。</p>
        <details className="pi-disclosure"><summary>查看原任务定位信息</summary><p>任务引用：{failureDetail.task.task_id}</p><p>问题定位：{failureDetail.task.correlation_id??"未保留"}</p></details>
      </section>}
    </InspectionDrawer>
    <Modal mutation zIndex={1600} focusTriggerAfterClose={false} open={confirm && confirmationReady} afterOpenChange={opened => { if (opened) document.getElementById(`pi-repair-title-${portfolioId}`)?.closest(".ant-modal")?.querySelector<HTMLButtonElement>("button")?.focus(); }} aria-labelledby={`pi-repair-title-${portfolioId}`} title={<span id={`pi-repair-title-${portfolioId}`}>{retryMode ? "确认仅重试失败步骤" : "确认本批数据修复计划"}</span>} onCancel={() => !busy && setConfirm(false)} onOk={() => void submit()} okText={retryMode ? "确认重试" : "确认执行本批计划"} confirmLoading={busy} okButtonProps={{ disabled: !ack || busy }} destroyOnHidden={false}>
      <p>组合 #{portfolioId} · {retryMode ? `${pending.current?.retryCount ?? 0} 个失败或受阻步骤` : `${pending.current?.assetCount ?? 0} 个标的 / ${pending.current?.operations.length ?? 0} 个去重步骤`}</p>
      {!retryMode && <ul className="pi-plan-list">{pending.current?.operations.map(op => <li key={op.operation_id}><strong>{op.symbol ?? "组合级"} · {op.label}</strong><span>{op.trade_date ?? "日期待核实"} · {op.expected_effect}</span></li>)}</ul>}
      {taskError && <div role="alert" className="pi-error-text"><p>{taskError}</p><Button onClick={()=>{setConfirm(false);setAck(false);setView("results");void loadRecent();}}>读取原任务记录</Button></div>}
      <Checkbox checked={ack} onChange={e => setAck(e.target.checked)}>我已核对标的、交易日、实际配置与计划范围，同意执行并逐对象重检。</Checkbox>
    </Modal>
  </div>;
}
