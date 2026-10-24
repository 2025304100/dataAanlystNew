import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button, Checkbox, Input, Modal } from "antd";
import { ArrowRight, CheckCircle2, CircleHelp, Clock3, Compass, GitCompareArrows, History, Link2, ListChecks, RefreshCw, ShieldAlert, ShieldCheck, Sparkles, Workflow } from "lucide-react";
import { api } from "../../../api/client";
import { useApp } from "../../../context/AppContext";
import type { ActionItem, ActionItemsResponse, AIResponse, ModelBindingReviewResponse, PortfolioStatusResponse, ReconciliationResponse } from "../../../types";
import type { CoverageActivationReview, InspectionGuide, InspectionJob, InspectionReconciliationQueue, InspectionRuntime, InspectionSymbol, PortfolioInspectionDiagnostics } from "../../../types/portfolioInspection";
import { safeImpact, safeTitle } from "../../../utils/actionItemRegistry";
import { openSettingsSection } from "../../../utils/navigate";
import { reconDimLabel } from "../../../utils/reconDimLabel";
import InspectionDataOverview from "./InspectionDataOverview";
import InspectionDrawer from "./InspectionDrawer";
import InspectionCoveragePanel from "./InspectionCoveragePanel";
import InspectionHistoryPanel from "./InspectionHistoryPanel";
import InspectionEvidencePanel from "./InspectionEvidencePanel";
import InspectionHistoryGapPanel from "./InspectionHistoryGapPanel";
import InspectionRuntimeReview from "./InspectionRuntimeReview";
import { historyGaps, reconciliationWaiting } from "./reconciliationGaps";
import InspectionExecutionEntry from "./InspectionExecutionEntry";
import InspectionExecutionToolbar from "./InspectionExecutionToolbar";
import InspectionReconciliationSummary from "./InspectionReconciliationSummary";
import { reconciliationReason, reconciliationText, reconciliationValue } from "./reconciliationPresentation";
import "./inspection.css";

type Panel = "material" | "guide" | "reconciliation" | "evidence" | "data" | "binding" | "history" | "dual" | "operations" | "job" | "help" | "ai" | "coverage";
type Step = 0 | 1 | 2 | 3;
const SURFACES: Panel[] = ["guide","data","binding","reconciliation","job","material","evidence","history","dual","operations","help","ai","coverage"];
type HistoryContext = { issue_id?: string; correlation_id?: string; report_ref?: string; report_version?: string; security_ids?: string[]; trade_date?: string; label?: string };
interface Props {
  portfolioId: number;
  navigation?: { tab: string; requestId: number };
  onNavigate?: (target: string) => void;
  onGovernanceChanged?: (portfolioId: number) => void | Promise<void>;
  renderDualRun: (onOverlayChange:(open:boolean)=>void, visible:boolean) => ReactNode;
  renderOperations?: (onOverlayChange:(open:boolean)=>void, visible:boolean) => ReactNode;
}
type TaskGroup = { key: string; panel: Panel; title: string; impact: string; details: string[]; items: ActionItem[]; severity: string };
const STEP_TITLES = ["核实执行", "批量修复数据", "确认运行绑定", "本人审核恢复"];
const errorText = (e: unknown) => e instanceof Error ? e.message : "读取失败，请重试";
const QUEUE_STATUS: Record<string,string> = { PASSED:"核对通过", BLOCKED:"有待处理差异", EVIDENCE_INCOMPLETE:"证据不完整", DIFF_FOUND:"发现账差", NOT_DUE:"尚未到期", EXECUTION_IN_PROGRESS:"执行中", WAITING_EXECUTION:"等待执行证据", NOT_CHECKED:"尚未核对", HISTORICAL_EVIDENCE_UNKNOWN:"历史证据未知", READ_ERROR:"读取失败" };
const LIMITATION_LABELS: Record<string,string> = { NO_ACCEPTED_ACCOUNT_BASELINE:"尚未建立本人接受的账户核验基线。", LEGACY_CHECKPOINT_NOT_FULL_HISTORY_PROOF:"旧版已核验日期不能作为全部历史已通过的证明。", LEGACY_REPORT_AUDIT_ADAPTER:"旧期仅能回看已保留的审计证据。", CALENDAR_UNKNOWN:"可信交易日历尚未核实，不能猜测应核对日期。", CALENDAR_PROVENANCE_NOT_VERSIONED:"此期间的日历来源版本未完整保留。", HISTORICAL_COVERAGE_UNKNOWN:"历史核验范围未知，需先核查证据或申请新覆盖起点。" };
const textValue = (v: unknown) => v == null ? "未知" : typeof v === "object" ? JSON.stringify(v) : String(v);
const displayTime = (value: string) => {
  const instant = new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`);
  return Number.isNaN(instant.getTime()) ? value : instant.toLocaleString("zh-CN", {timeZone:"Asia/Shanghai"});
};
function checkedResponse<T>(value: T, portfolioId: number, valid: (value: any) => boolean): T {
  if (!value || !valid(value) || ((value as any).portfolio_id != null && (value as any).portfolio_id !== portfolioId)) throw new Error("返回的检查材料不完整或不属于当前组合，请重新读取");
  return value;
}
const evidenceOkay = (status: string) => ["PASSED", "NOT_APPLICABLE", "NOT_DUE"].includes(status);
function itemPanel(item: ActionItem): Panel {
  const family = item.family ?? "";
  if (family.startsWith("g7_ops") || item.source === "g7_ops" || item.scope?.kind === "engine_admission") return "dual";
  if (family === "data-freshness" || ["MARKET_DATA_STALE", "SCORE_STALE", "DATA_INCOMPLETE_PAUSED"].includes(item.code)) return "data";
  if (family === "reconciliation" || family.startsWith("recon:") || item.code === "RECONCILIATION_BLOCKED" || item.scope?.kind === "account_date" || item.primary_action?.target === "governance:reconciliation") return "reconciliation";
  if (family === "model" || item.primary_action?.target === "model-binding-review" || /BINDING|MODEL_INACTIVE/.test(item.code)) return "binding";
  return "guide";
}

export default function PortfolioInspectionWorkbench({ portfolioId, navigation, onNavigate, onGovernanceChanged, renderDualRun, renderOperations }: Props) {
  const { showToast, setActiveTab } = useApp();
  const [confirmationReady, setConfirmationReady] = useState(false);
  const handleDataConfirmation = useCallback((visible: boolean) => { if (visible) setConfirmationReady(false); setDataConfirm(visible); }, []);
  const [dataConfirm, setDataConfirm] = useState(false);
  const [coverageVisited, setCoverageVisited] = useState(false);
  const [historyVisited, setHistoryVisited] = useState(false);
  const [dualVisited, setDualVisited] = useState(false);
  const [evidenceVisited, setEvidenceVisited] = useState(false);
  const [evidenceMaterialOrigin, setEvidenceMaterialOrigin] = useState(false);
  const [verifiedProofs, setVerifiedProofs] = useState<Record<string,string>>({});
  const frozenGuide = useRef<{guide:InspectionGuide;action:string;key:string}|null>(null);
  const [layers, setLayers] = useState<Panel[]>([]);
  const [seenPanels,setSeenPanels] = useState<Panel[]>([]);
  const panel = layers[layers.length-1] ?? null;
  const setPanel = useCallback((target: Panel | null) => { if(target)setSeenPanels(previous=>previous.includes(target)?previous:[...previous,target]); setLayers(previous => {
    if (!target) return [];
    const index = previous.lastIndexOf(target);
    return index >= 0 ? previous.slice(0,index+1) : [...previous,target];
  }); }, []);
  const [detailOverlays,setDetailOverlays] = useState<Partial<Record<Panel,boolean>>>({});
  const overlayHandlers=useMemo(()=>Object.fromEntries(SURFACES.map(surface=>[surface,(shown:boolean)=>setDetailOverlays(previous=>previous[surface]===shown?previous:{...previous,[surface]:shown})])) as Record<Panel,(shown:boolean)=>void>,[]);
  const clearDetailOverlay=()=>{if(panel)overlayHandlers[panel](false);};
  const scrollPositions = useRef<Record<string,number>>({});
  const restoredNavigation = useRef(false);
  const restoredDraft = useRef<Record<string,any>|null>(null);
  const pageScrollToRestore=useRef<number|null>(null);
  const [step, setStep] = useState<Step>(0);
  const [items, setItems] = useState<ActionItemsResponse | null>(null);
  const [status, setStatus] = useState<PortfolioStatusResponse | null>(null);
  const [runtime, setRuntime] = useState<InspectionRuntime | null>(null);
  const [diagnostics, setDiagnostics] = useState<PortfolioInspectionDiagnostics | null>(null);
  const [binding, setBinding] = useState<ModelBindingReviewResponse | null>(null);
  const [report, setReport] = useState<ReconciliationResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [historyContext, setHistoryContext] = useState<HistoryContext | undefined>();
  const [historyOrigin, setHistoryOrigin] = useState<{ panel: Panel | null; step: Step; trigger: HTMLElement | null } | null>(null);
  const [activation, setActivation] = useState<CoverageActivationReview | null>(null);
  const activationKey = useRef("");
  const [reviewNote, setReviewNote] = useState("");
  const [modal, setModal] = useState<"reconcile" | "binding" | "resume" | null>(null);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [queue, setQueue] = useState<InspectionReconciliationQueue | null>(null);
  const [job, setJob] = useState<InspectionJob | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  const [ai, setAI] = useState<AIResponse | null>(null);
  const [aiBusy, setAIBusy] = useState(false);
  const [aiError, setAIError] = useState<string | null>(null);
  const [aiStale, setAIStale] = useState(false);
  const [aiScope, setAIScope] = useState<{ symbol_ids?: number[]; trade_date?: string } | null>(null);
  const [selectedTradeDate, setSelectedTradeDate] = useState<string | null>(null);
  const selectedDateRef=useRef<string|null>(null);
  selectedDateRef.current=selectedTradeDate;
  const epoch = useRef(0);
  const request = useRef(0);
  const currentPortfolio = useRef(portfolioId);
  currentPortfolio.current = portfolioId;
  const returnFocus = useRef<HTMLElement | null>(null);
  const guideDraft = useRef<"guide" | null>(null);
  const pendingReview = useRef<{ reportId: string | null; reportVersion: string | null; bindingSnapshot: string | null; tradeDate: string | null; action: string | null } | null>(null);
  const returnKey = "portfolio-inspection-return-v1";

  const load = useCallback(async () => {
    const id = ++request.current;
    setLoading(true);
    const results = await Promise.allSettled([
      Promise.resolve().then(() => api.getPortfolioActionItems(portfolioId, { for_schedule: true })).then(v => checkedResponse(v, portfolioId, x => Array.isArray(x.items))), Promise.resolve().then(() => api.getPortfolioStatus(portfolioId)).then(v => checkedResponse(v, portfolioId, x => typeof x.current_state === "string")),
      Promise.resolve().then(() => api.getPortfolioInspectionRuntime(portfolioId)).then(v => checkedResponse(v, portfolioId, x => x.permissions && x.recovery_summary && Array.isArray(x.recovery_summary.checks) && x.capability_reasons)), Promise.resolve().then(() => api.getPortfolioInspectionDiagnostics(portfolioId)).then(v => checkedResponse(v, portfolioId, x => Array.isArray(x.symbols) && Array.isArray(x.evidence) && x.repair_plan)),
      Promise.resolve().then(() => api.getPortfolioModelBindingReview(portfolioId)).then(v => checkedResponse(v, portfolioId, x => typeof x.can_confirm === "boolean")), Promise.resolve().then(() => selectedDateRef.current ? api.previewPortfolioReconciliation(portfolioId,selectedDateRef.current) : api.previewPortfolioReconciliation(portfolioId)).then(v => checkedResponse(v, portfolioId, x => typeof x.trade_date === "string" && Array.isArray(x.items))),
      Promise.resolve().then(() => api.getPortfolioInspectionJob(portfolioId)).then(v => checkedResponse(v, portfolioId, x => typeof x.enabled === "boolean" && x.queue && Array.isArray(x.queue.days) && Array.isArray(x.queue.limitations))),
    ]);
    if (id !== request.current || currentPortfolio.current !== portfolioId) return;
    const names = ["actions", "status", "runtime", "data", "binding", "reconciliation", "job"];
    const failures: Record<string, string> = {};
    results.forEach((result, index) => { if (result.status === "rejected") failures[names[index]] = errorText(result.reason); });
    setItems(results[0].status === "fulfilled" ? results[0].value : null);
    setStatus(results[1].status === "fulfilled" ? results[1].value : null);
    setRuntime(results[2].status === "fulfilled" ? results[2].value : null);
    if (results[2].status === "fulfilled" && results[2].value.guide) {
      const proof = results[2].value.guide!;
      setVerifiedProofs(previous => ({...Object.fromEntries(proof.steps.filter(row => ["PASSED","NOT_APPLICABLE"].includes(row.status)).map(row => [row.key,row.proof_hash])),...previous}));
    }
    setDiagnostics(results[3].status === "fulfilled" ? results[3].value : null);
    setBinding(results[4].status === "fulfilled" ? results[4].value : null);
    setReport(results[5].status === "fulfilled" ? results[5].value : null);
    if (results[6].status === "fulfilled") { const value = results[6].value as InspectionJob; setJob(value); setQueue(value.queue); setJobError(null); }
    else { setJob(null); setQueue(null); setJobError(errorText(results[6].reason)); }
    setAIStale(true);
    setErrors(failures); setLoading(false);
    return results[2].status === "fulfilled" ? results[2].value : null;
  }, [portfolioId]);
  useEffect(() => {
    epoch.current++;
    selectedDateRef.current=null;setSelectedTradeDate(null);
    setEvidenceMaterialOrigin(false); setEvidenceVisited(false); setVerifiedProofs({}); frozenGuide.current=null;
    setActivation(null); setDualVisited(false); setAIScope(null); setAIBusy(false); setConfirmationReady(false); setDataConfirm(false); setCoverageVisited(false); setHistoryVisited(false); setPanel(null); setSeenPanels([]); setDetailOverlays({}); scrollPositions.current={}; setStep(0); setItems(null); setStatus(null); setRuntime(null); setDiagnostics(null); setBinding(null); setReport(null); setReviewNote(""); setModal(null); setAck(false); setBusy(false); setHistoryContext(undefined); setHistoryOrigin(null); setQueue(null); setJob(null); setAI(null); setAIError(null); setWriteError(null); guideDraft.current = null;
    try {
      if(restoredDraft.current?.portfolioId!==portfolioId)restoredDraft.current=null;
      const saved = restoredDraft.current?.portfolioId===portfolioId ? restoredDraft.current : JSON.parse(sessionStorage.getItem(returnKey) ?? "null");
      if (saved?.portfolioId === portfolioId && Date.now() - saved.savedAt < 30 * 60 * 1000) {
        restoredDraft.current=saved; sessionStorage.removeItem(returnKey);
        const restored = SURFACES.includes(saved.panel) ? saved.panel as Panel : "guide";
        const savedLayers:Panel[]=Array.isArray(saved.layers) && saved.layers.length && saved.layers.every((value:Panel)=>SURFACES.includes(value)) ? saved.layers : [restored];
        setLayers(savedLayers);setSeenPanels(savedLayers);pageScrollToRestore.current=Number(saved.pageScroll)||0;
        setHistoryVisited(savedLayers.includes("history"));setEvidenceVisited(savedLayers.includes("evidence"));setCoverageVisited(savedLayers.includes("coverage"));setDualVisited(savedLayers.includes("dual"));
        if(saved.historyContext)setHistoryContext(saved.historyContext);
        if(saved.historyOrigin)setHistoryOrigin({...saved.historyOrigin,trigger:null});
        setEvidenceMaterialOrigin(saved.evidenceMaterialOrigin===true);
        scrollPositions.current=saved.scrollPositions??{}; restoredNavigation.current=true;
        setStep(Math.max(0, Math.min(3, saved.step ?? 0)) as Step);
        setReviewNote(typeof saved.reviewNote === "string" ? saved.reviewNote : ""); guideDraft.current = saved.layers?.includes("guide")||restored==="guide"?"guide":null;
        if(saved.verifiedProofs&&typeof saved.verifiedProofs==="object")setVerifiedProofs(saved.verifiedProofs);
        if(typeof saved.selectedTradeDate==="string"&&/^\d{4}-\d{2}-\d{2}$/.test(saved.selectedTradeDate)){selectedDateRef.current=saved.selectedTradeDate;setSelectedTradeDate(saved.selectedTradeDate);}
      }
    } catch { /* Storage is optional; fresh evidence still loads. */ }
    void load(); return () => { request.current++; epoch.current++; };
  }, [load]);
  useEffect(()=>{if(!loading&&pageScrollToRestore.current!==null){const top=pageScrollToRestore.current;pageScrollToRestore.current=null;const frame=requestAnimationFrame(()=>window.scrollTo?.({top}));return()=>cancelAnimationFrame(frame);}},[loading]);
  useEffect(() => { if (panel === "history") setHistoryVisited(true); if (panel === "coverage") setCoverageVisited(true); if (panel === "dual") setDualVisited(true); }, [panel]);
  useEffect(()=>{if(panel==="evidence")setEvidenceVisited(true);},[panel]);
  useEffect(() => {
    if (restoredNavigation.current) { restoredNavigation.current=false; return; }
    if (!navigation || navigation.tab === "overview") return;
    setPanel(({ state: "guide", operations: "guide", reconciliation: "reconciliation", audit: "history", g5: "dual" } as Record<string, Panel>)[navigation.tab] ?? null);
  }, [navigation?.requestId, navigation?.tab, portfolioId]);

  const groups = useMemo(() => {
    const map = new Map<string, TaskGroup>();
    for (const item of items?.items ?? []) {
      const target = itemPanel(item);
      const key = target === "guide" || target === "dual" ? item.family || item.id : target;
      const current = map.get(key);
      if (current) { current.items.push(item); current.details.push(item.detail ?? safeImpact(item)); if (item.severity === "blocker") current.severity = "blocker"; }
      else map.set(key, { key, panel: target, title: safeTitle(item), impact: safeImpact(item), details: [item.detail ?? safeImpact(item)], items: [item], severity: item.severity });
    }
    const reconciliationGroup = map.get("reconciliation");
    if (reconciliationGroup && reconciliationGroup.items.length > 1) {
      reconciliationGroup.items.sort((a,b) => (a.trade_date ?? "9999").localeCompare(b.trade_date ?? "9999"));
      const dates = [...new Set(reconciliationGroup.items.map(item => item.trade_date).filter(Boolean))];
      reconciliationGroup.title = `执行与账户核对待完成 · ${dates.length} 个交易日`;
      reconciliationGroup.impact = `优先核查 ${dates[0] ?? "当前"} 的执行来源；其余日期在作业抽屉集中查看，未完成前不能跳到后日确认。`;
    }
    // Runtime evidence can expose a newer prerequisite than legacy action items.
    // Keep that real server reason actionable even when the old aggregate is empty.
    for (const [key, target, label] of [["data", "data", "数据证据尚待核实"], ["binding", "binding", "实际运行绑定尚待审核"], ["reconciliation", "reconciliation", "执行与账户证据尚待核实"]] as const) {
      const prerequisite = runtime?.recovery_summary.checks.find(x => x.key === key);
      if (![...map.values()].some(group => group.panel === target) && prerequisite && prerequisite.passed !== true && !(key === "reconciliation" && report?.work_state?.startsWith("waiting"))) {
        map.set(target, { key: target, panel: target, title: label, impact: prerequisite.reason, details: [prerequisite.reason], items: [], severity: "blocker" });
      }
    }
    return [...map.values()];
  }, [items, runtime, report]);
  const isDailyBlocker = (group: TaskGroup) => group.severity === "blocker" && (group.items.length === 0 || group.items.some(item => item.portfolio_wide !== false && item.source !== "g7_ops" && item.scope?.kind !== "engine_admission"));
  const blockers = groups.filter(isDailyBlocker).sort((a,b) =>
    (["reconciliation","data","binding","guide"].indexOf(a.panel) + 1 || 99) -
    (["reconciliation","data","binding","guide"].indexOf(b.panel) + 1 || 99));
  const otherGroups = groups.filter(x => !isDailyBlocker(x));
  const initialScope = runtime?.recovery_summary.confirmation_action === "INITIAL_COVERAGE_REVIEW";
  const stepTitles = initialScope ? ["核验当前账户", "核实当前数据", "核实当前绑定", "本人启用新范围"] : STEP_TITLES;
  const checks = runtime?.recovery_summary.checks ?? [];
  const check = (key: string) => checks.find(x => x.key === key);
  const guideSteps = runtime?.guide?.steps;
  const guideState = (index:number) => {const row=guideSteps?.[index];return row && ["PASSED","NOT_APPLICABLE"].includes(row.status) && verifiedProofs[row.key] && verifiedProofs[row.key]!==row.proof_hash ? "NEEDS_RECHECK" : row?.status;};
  const steps = [check("reconciliation")?.passed, check("data")?.passed, check("binding")?.passed, runtime?.permissions.allow_new_buys === true].map((value,i)=>guideSteps?.[i] ? ["PASSED","NOT_APPLICABLE"].includes(guideState(i)??"") : value);
  const proofsNeedRecheck=guideSteps?.slice(0,3).some((_,i)=>guideState(i)==="NEEDS_RECHECK")??false;
  const visibleSteps:Step[] = initialScope ? [0,1,2,3] : [1,2,3];
  const firstStep = ():Step => visibleSteps.find(index=>steps[index]!==true) ?? 3;
  const stepPosition = Math.max(1,visibleSteps.indexOf(step)+1);
  useEffect(()=>{if(panel==="guide" && step===0 && !initialScope && runtime)setStep(firstStep());},[panel,step,initialScope,runtime]);
  const currentBlockers=blockers.filter(group=>group.panel!=="reconciliation");
  const executionGroup=groups.find(group=>group.panel==="reconciliation");
  const pendingDays=queue?.days.filter(day=>!day.history_only&&!["PASSED","NOT_DUE"].includes(day.status)).length ?? null;
  const canBuy = runtime?.permissions.allow_new_buys;
  const waiting = runtime?.recovery_summary.phase?.startsWith("WAITING") || report?.work_state?.startsWith("waiting");
  const noReview = runtime?.recovery_summary.recommended_action === "NONE";
  const unknown = Boolean(errors.runtime || errors.actions) || !runtime;
  const title = loading && !runtime ? "正在核实当前运行条件" : unknown ? "当前交易状态待核实" : canBuy ? "当前运行条件已满足" : waiting && !blockers.length ? "等待运行条件到期" : runtime.recovery_summary.can_confirm ? "阻断已清除，等待本人审核" : "新买入已暂停";
  const open = (target: Panel, guided = false, asDetail=false) => { if(!panel)returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; clearDetailOverlay(); setWriteError(null); if (target === "ai" && !guided) { setAIScope(null); setAIStale(true); } if (target === "guide") { guideDraft.current = "guide"; setStep(firstStep()); } else if (!guided && panel!=="guide") guideDraft.current = null; if(asDetail&&target!==panel){setSeenPanels(previous=>previous.includes(target)?previous:[...previous,target]);setLayers(previous=>[...previous,target]);}else setPanel(target); };
  const close = () => { clearDetailOverlay(); if(layers.length>1){setLayers(previous=>previous.slice(0,-1));return;} setPanel(null); guideDraft.current = null; setTimeout(() => returnFocus.current?.focus({preventScroll:true}), 0); };
  const navigate = (target: string) => {
    if (target.startsWith("settings:") || ["overview","members","strategy","backtest"].includes(target) || /factor|scoring|model|data/.test(target)) {
      try { sessionStorage.setItem(returnKey, JSON.stringify({portfolioId,panel,layers,step,reviewNote,verifiedProofs,selectedTradeDate,evidenceMaterialOrigin,historyContext,historyOrigin:historyOrigin?{panel:historyOrigin.panel,step:historyOrigin.step}:null,scrollPositions:scrollPositions.current,pageScroll:window.scrollY,savedAt:Date.now(),destination:target})); } catch { /* Continue with the real destination. */ }
    }
    if (target.startsWith("settings:")) { openSettingsSection(target==="settings:data-center:inspection-repair"?"data-center":target.slice(9)); setActiveTab?.("settings"); return; }
    if (["overview", "members", "strategy", "backtest"].includes(target)) { onNavigate?.(target); return; }
    if (target.includes("factor") || target.includes("scoring") || target.includes("model")) { openSettingsSection("factor-center"); setActiveTab?.("settings"); return; }
    if (target.includes("data")) { openSettingsSection("data-center"); setActiveTab?.("settings"); return; }
    setWriteError("该动作暂未提供可验证的执行入口，请查看具体原因并重新检测。");
  };
  const showHistory = (group?: TaskGroup, symbol?: InspectionSymbol, tradeDate?: string) => {
    const attributes = (tradeDate ? group?.items.find(item => item.trade_date === tradeDate) : group?.items[0]) as (ActionItem & { issue_id?: string; issue_ids?: string[]; correlation_id?: string; report_ref?: string; report_refs?: string[]; report_version?: string; snapshot_id?: string }) | undefined;
    const contextDate = tradeDate ?? symbol?.expected_trade_date ?? attributes?.trade_date ?? (group?.panel === "reconciliation" ? report?.trade_date : undefined);
    const matchingReport = group?.panel === "reconciliation" && contextDate === report?.trade_date;
    setHistoryOrigin({ panel, step, trigger: document.activeElement instanceof HTMLElement ? document.activeElement : null });
    setHistoryContext(group || symbol ? {
      issue_id: attributes?.issue_id ?? symbol?.evidence.find(e => !evidenceOkay(e.status))?.evidence_id,
      correlation_id: attributes?.correlation_id,
      report_ref: attributes?.report_ref ?? (group?.panel === "reconciliation" ? attributes?.report_refs?.[0] ?? (matchingReport ? report?.report_id ?? undefined : undefined) : undefined),
      report_version: attributes?.report_version ?? (group?.panel === "reconciliation" ? attributes?.snapshot_id ?? (matchingReport ? report?.report_version ?? undefined : undefined) : undefined),
      security_ids: symbol ? [`${symbol.market}:${symbol.symbol}`] : group?.panel === "data" ? diagnostics?.symbols.filter(x => x.evidence.some(e => !evidenceOkay(e.status))).map(x => `${x.market}:${x.symbol}`) : undefined,
      label: group?.panel === "data" && !symbol && !attributes?.issue_id ? "当前数据阻断涉及的标的（未保留直接关联编号时，按标的查找）" : undefined,
      trade_date: contextDate,
    } : undefined);
    setPanel("history");
  };
  const backHistory = () => { clearDetailOverlay(); if (!historyOrigin) { close(); return; } setPanel(historyOrigin.panel); setStep(historyOrigin.step); setTimeout(() => historyOrigin.trigger?.isConnected && historyOrigin.trigger.focus({preventScroll:true}), 0); };
  const loadJob = useCallback(async () => {
    const pid = portfolioId; const activeEpoch = epoch.current; setJobError(null);
    const results = await Promise.allSettled([
      Promise.resolve().then(() => api.getPortfolioInspectionQueue(pid)).then(value => checkedResponse(value, pid, x => Array.isArray(x.days) && Array.isArray(x.limitations))),
      Promise.resolve().then(() => api.getPortfolioInspectionJob(pid)).then(value => checkedResponse(value, pid, x => typeof x.enabled === "boolean")),
    ]);
    if (currentPortfolio.current !== pid || activeEpoch !== epoch.current) return;
    if (results[0].status === "fulfilled") setQueue(results[0].value); else setJobError(errorText(results[0].reason));
    if (results[1].status === "fulfilled") setJob(results[1].value); else setJobError(errorText(results[1].reason));
  }, [portfolioId]);
  useEffect(() => { if (panel === "job") void loadJob(); }, [panel, loadJob]);
  useEffect(()=>{if(panel!=="job"||!job)return;
    const active=["queued","running"].includes(job.status)||job.work_items?.some(row=>["queued","running"].includes(row.work_state))||job.enabled&&Boolean(job.source_events?.count);
    if(!active)return;const timer=setTimeout(()=>void loadJob(),3000);return()=>clearTimeout(timer);
  },[panel,job,loadJob]);
  const beginConfirm = async (type: "reconcile" | "binding" | "resume") => {
    const pid = portfolioId; const activeEpoch = epoch.current;
    setAck(false); setWriteError(null); setActivation(null); setConfirmationReady(false);
    if(type==="resume"&&proofsNeedRecheck){setWriteError("步骤依据已变化，请先重新核实提示的步骤；审核理由会保留。");return;}
    frozenGuide.current=type==="resume"&&runtime?.guide&&runtime.recovery_summary.confirmation_action?
      {guide:runtime.guide,action:runtime.recovery_summary.confirmation_action,key:`guide-${crypto.randomUUID()}`}:null;
    pendingReview.current = { reportId: report?.report_id ?? null, reportVersion: report?.report_version ?? null,
      bindingSnapshot: binding?.snapshot_id ?? null, tradeDate: report?.trade_date ?? null,
      action: runtime?.recovery_summary.confirmation_action ?? null };
    if(type==="resume"&&frozenGuide.current){const proof=frozenGuide.current.guide.steps[0]?.proof.check as Record<string,unknown>|undefined;
      pendingReview.current.reportId=proof?.report_id==null?null:String(proof.report_id);
      pendingReview.current.reportVersion=proof?.report_version==null?null:String(proof.report_version);
      pendingReview.current.tradeDate=proof?.trade_date==null?null:String(proof.trade_date);
    }
    if (type === "resume" && pendingReview.current.action === "INITIAL_COVERAGE_REVIEW") {
      setBusy(true);
      try {
        const value = await api.getPortfolioCoverageActivationReview(pid);
        if (currentPortfolio.current !== pid || epoch.current !== activeEpoch) return;
        setActivation(value);
        activationKey.current = `coverage-activate-${crypto.randomUUID()}`;
        setModal(type);
      } catch (e) { if (currentPortfolio.current === pid) setWriteError(errorText(e)); }
      finally { if (currentPortfolio.current === pid) setBusy(false); }
    } else setModal(type);
  };
  const changed = useCallback(async () => { await Promise.allSettled([load(), Promise.resolve(onGovernanceChanged?.(portfolioId))]); }, [load, onGovernanceChanged, portfolioId]);
  const confirm = async () => {
    if (!modal || !ack || busy || reviewNote.trim().length < 10) return;
    const pid = portfolioId; const activeEpoch = epoch.current; setBusy(true); setWriteError(null);
    try {
      if (modal === "resume" && frozenGuide.current) {
        await api.confirmPortfolioInspectionGuide(pid,{expected_snapshot_id:frozenGuide.current.guide.snapshot_id,
          expected_action:frozenGuide.current.action,idempotency_key:frozenGuide.current.key,review_note:reviewNote.trim(),acknowledge_current_proofs:true});
      } else if (modal === "resume" && pendingReview.current?.action === "INITIAL_COVERAGE_REVIEW") {
        if (!activation?.can_confirm || !activation.baseline_id || activation.generation == null || activation.cursor_row_version == null || !activation.evidence_fingerprint) throw new Error("新范围启用材料尚不完整，请重新读取并处理前置条件。");
        await api.activatePortfolioCoverage(pid, { baseline_id: activation.baseline_id, generation: activation.generation,
          expected_cursor_row_version: activation.cursor_row_version, expected_evidence_fingerprint: activation.evidence_fingerprint,
          activation_key: activationKey.current, acknowledge_new_scope: true, review_note: reviewNote.trim() });
      } else if (modal === "binding") {
        if (!binding?.can_confirm || !binding.snapshot_id || binding.snapshot_id !== pendingReview.current?.bindingSnapshot) throw new Error("绑定依据已变化，请重新读取并核实。");
        await api.confirmPortfolioModelBinding(pid, { snapshot_id: binding.snapshot_id, model_run_id: binding.model_run_id, ack: true, review_note: reviewNote.trim() });
      } else if (modal === "reconcile" || pendingReview.current?.action === "RECONCILIATION_CONFIRM") {
        if (!report?.report_id || !report.report_version || !report.zero_sum_check_passed) throw new Error("请先重检，取得有效报告与版本；证据缺失不能确认通过。");
        await api.confirmPortfolioReconciliation(pid, { trade_date: pendingReview.current?.tradeDate ?? undefined, ack: true, review_note: reviewNote.trim(), report_id: pendingReview.current?.reportId, report_version: pendingReview.current?.reportVersion, force_skip: false });
      } else {
        if (!runtime?.recovery_summary.can_confirm) throw new Error("尚有未通过或未知的前置条件，请先处理。");
        await api.transitionPortfolioState(pid, { target_state: "READY", trigger_reason: "本人审核当前运行条件后恢复", review_note: reviewNote.trim() });
      }
      if (currentPortfolio.current !== pid || epoch.current !== activeEpoch) return;
      setModal(null); setAck(false); showToast("success", "已保存本次审核，正在重新核实其余运行条件。"); await changed();
    } catch (e) { if (currentPortfolio.current === pid && epoch.current === activeEpoch) { setWriteError(errorText(e)); setAck(false); } }
    finally { if (currentPortfolio.current === pid && epoch.current === activeEpoch) setBusy(false); }
  };
  const rerun = async () => {
    if (busy) return; const pid = portfolioId; setBusy(true); setWriteError(null);
    try { const result = await api.triggerPortfolioReconcile(pid, { trade_date: selectedTradeDate ?? report?.trade_date }); if (currentPortfolio.current === pid) { await changed(); setReport(result); setSelectedTradeDate(result.trade_date); } }
    catch (e) { if (currentPortfolio.current === pid) setWriteError(errorText(e)); }
    finally { if (currentPortfolio.current === pid) setBusy(false); }
  };
  const analyse = async () => {
    if (aiBusy) return; const pid = portfolioId; const activeEpoch = epoch.current; const factsVersion = request.current; setAIBusy(true); setAIError(null);
    try {
      const result = await api.analysePortfolioInspection(pid, { step: guideDraft.current ? ["reconciliation","data","binding","runtime"][step] : undefined, trade_date: selectedTradeDate ?? report?.trade_date, ...aiScope });
      if (currentPortfolio.current === pid && epoch.current === activeEpoch) { setAI(result.response); setAIStale(factsVersion !== request.current); }
    } catch (e) { if (currentPortfolio.current === pid && epoch.current === activeEpoch) setAIError(errorText(e)); }
    finally { if (currentPortfolio.current === pid && epoch.current === activeEpoch) setAIBusy(false); }
  };
  const inspectDay = async (tradeDate: string) => {
    const current = epoch.current; setWriteError(null);
    try { const value = await api.previewPortfolioReconciliation(portfolioId, tradeDate); if (current === epoch.current) { setReport(value); setSelectedTradeDate(tradeDate); open("reconciliation",Boolean(guideDraft.current),panel==="job"); } }
    catch (e) { if (current === epoch.current) setWriteError(errorText(e)); }
  };
  const continueGuide = async () => { const fresh = await load(); if (!fresh) return; const next=([1,2] as Step[]).find(index=>fresh.recovery_summary.checks.find(x=>x.key===(["reconciliation","data","binding"])[index])?.passed!==true); setStep(next??3); setPanel("guide"); guideDraft.current = "guide"; };
  const stepLabel = (index: number) => (({PASSED:"已满足",NOT_APPLICABLE:"本次不适用",NEEDS_RECHECK:"依据变化，需重检",RUNNING:"处理中",UNAVAILABLE:"证据未知",WAITING:"等待到期",INCOMPLETE:"待处理"} as Record<string,string>)[guideState(index)??""] ?? (steps[index] === true ? "已满足" : index === 0 && waiting ? "等待到期" : steps[index] == null ? "待核实" : "待处理"));
  const recheckGuideStep=async()=>{const current=step;const fresh=await load();const proof=fresh?.guide?.steps[current];if(proof&&["PASSED","NOT_APPLICABLE"].includes(proof.status))setVerifiedProofs(old=>({...old,[proof.key]:proof.proof_hash}));};
  const recheckSources=async()=>{const pid=portfolioId;setBusy(true);setWriteError(null);try{const result=await api.recheckPortfolioSources(pid,selectedTradeDate??undefined);if(currentPortfolio.current===pid){await changed();if(result.dispatch_status==="WAITING_RETRY")setWriteError(result.message??"重检需求已保存，任务尚未派发，请重试。");else{setPanel("job");showToast("success","已提交证据核对任务，请查看逐日进度；不会执行交易。");}}}catch(e){if(currentPortfolio.current===pid)setWriteError(errorText(e));}finally{if(currentPortfolio.current===pid)setBusy(false);}};
  const proofSummary=(guide:InspectionGuide)=><section className="pi-completion-proofs"><h3>本次审核的完成证明</h3>{guide.completion_proofs.map((row,i)=>{const checkProof=row.proof.check as Record<string,unknown>|undefined;return <details key={row.key}><summary>{stepTitles[i]} · {({PASSED:"已核实",NOT_APPLICABLE:"本次不适用",INCOMPLETE:"待处理",RUNNING:"处理中",NEEDS_RECHECK:"依据变化，需重检",UNAVAILABLE:"证据未知",WAITING:"等待到期"} as Record<string,string>)[row.status]??"待核实"}</summary><p>{row.reason}</p><dl className="pi-facts"><div><dt>完成证明指纹</dt><dd>{row.proof_hash}</dd></div>{checkProof?.report_id!=null&&<div><dt>交易日报告 / 版本</dt><dd>{textValue(checkProof.trade_date)} · {textValue(checkProof.report_id)}<small>{textValue(checkProof.report_version)}</small></dd></div>}{checkProof?.snapshot_id!=null&&<div><dt>当前检查快照</dt><dd>{textValue(checkProof.snapshot_id)}</dd></div>}</dl>{Array.isArray(row.proof.reports)&&row.proof.reports.length>0&&<div className="pi-table-wrap"><table><thead><tr><th>已保留的核对交易日</th><th>报告 / 版本</th></tr></thead><tbody>{row.proof.reports.map((item:any)=><tr key={item.trade_date}><td>{item.trade_date}</td><td>{textValue(item.persistent_report_id??item.report_id)}<small>版本 {textValue(item.persistent_report_version??item.report_version)} · {QUEUE_STATUS[item.status]??"状态待核实"}</small></td></tr>)}</tbody></table></div>}</details>;})}<p>保留限制：{guide.effects.historical_unknown_retained?"旧期历史未知继续保留；":""}例外恢复关闭；风险退出与其他独立门禁继续核验。</p><small>审核快照 {guide.snapshot_id}</small></section>;

  // Keep the legacy surface key so saved drawer trails return to the same day.
  const openHistoryGap = () => {
    if (!report) return;
    selectedDateRef.current=report.trade_date; setSelectedTradeDate(report.trade_date);
    open("material",true);
  };
  const refreshMaterialReport = async () => {
    if (!report) return;
    const pid=portfolioId, activeEpoch=epoch.current, date=report.trade_date;
    try {const value=checkedResponse(await api.previewPortfolioReconciliation(pid,date),pid,x=>x.trade_date===date&&Array.isArray(x.items));
      if(currentPortfolio.current===pid&&epoch.current===activeEpoch){setReport(value);setWriteError(null);}
    } catch(e){if(currentPortfolio.current===pid&&epoch.current===activeEpoch)setWriteError(errorText(e));}
  };
  const comparisonItems = report?.items.filter(item => item.expected_value != null || item.actual_value != null || item.diff_value != null ||
    !report.missing_evidence?.some(missing => typeof missing !== "string" && missing.code === item.dimension)) ?? [];
  const reconciliationBody = <section data-testid="reconcile-panel">
    {errors.reconciliation && <div role="alert" className="pi-callout pi-error">{errors.reconciliation}<Button onClick={() => void load()}>重新读取核对证据</Button></div>}
    {report && <><InspectionReconciliationSummary report={report} onGap={openHistoryGap} onHistory={() => showHistory(groups.find(x => x.panel === "reconciliation"),undefined,selectedTradeDate??report.trade_date)}/>
    {comparisonItems.length > 0 && <div className="pi-table-wrap"><table><thead><tr><th>检查项目</th><th>应有 / 实际</th><th>结论与原因</th></tr></thead><tbody>{comparisonItems.map((item,i) => <tr key={`${item.dimension}-${i}`}><td>{reconDimLabel(item.dimension)}</td><td>{reconciliationValue(item.expected_value)} / {reconciliationValue(item.actual_value)}</td><td>{item.diff_value == null ? "材料不完整，暂不能计算差异" : `差异 ${reconciliationValue(item.diff_value)}`}<small>{reconciliationReason(item.dimension,item.explain_note)}</small></td></tr>)}</tbody></table></div>}
    {report.cash_audit && <div className="pi-callout"><strong>实际资金核验</strong><p>应有 {report.cash_audit.expected_cash?.toLocaleString("zh-CN", { minimumFractionDigits: 2 }) ?? "未知"} · 实际 {report.cash_audit.actual_cash?.toLocaleString("zh-CN", { minimumFractionDigits: 2 }) ?? "未知"} · 差异 {report.cash_audit.delta?.toFixed(2) ?? "未知"}</p></div>}
    <p className="pi-muted">核查记录可在相关历史中追溯。材料发生变化后，需要重新核查。</p>
    <div className="pi-panel-actions"><Button onClick={() => void load()}>刷新只读核对</Button><Button loading={busy} onClick={() => void rerun()}>重跑当前交易日核对</Button>{report.zero_sum_check_passed && runtime?.recovery_summary.confirmation_action === "RECONCILIATION_CONFIRM" && runtime.recovery_summary.review_required && <Button type="primary" disabled={!report.report_id || !report.report_version} onClick={() => beginConfirm("reconcile")}>审核本次对账结果</Button>}</div></>}
  </section>;
  const bindingBody = <section><div className="pi-callout"><strong>已保存、已审核、已切换分别留痕</strong><p>这里核实本次实际生效的模型与策略绑定，不代替双跑验收，也不会自动应用候选引擎。</p></div>{errors.binding && <p role="alert">{errors.binding}</p>}{binding && <><p>{binding.change_summary}</p><div className="pi-binding-grid">{([["之前明确接受", binding.before], ["当前实际应用", binding.after]] as const).map(([label,value]) => <div key={label}><h3>{label}</h3>{value ? <dl className="pi-facts"><div><dt>策略快照</dt><dd>{value.snapshot_no != null ? `S${value.snapshot_no}` : value.snapshot_id}</dd></div><div><dt>实际模型</dt><dd>{value.model_name ?? (value.model_run_id ? "名称未保留，详见版本引用" : "未绑定")}</dd></div><div><dt>执行模式</dt><dd>{value.run_mode ?? "未知"}</dd></div></dl> : <p>没有可核实的历史接受记录。</p>}</div>)}</div>{binding.blockers.length > 0 && <div className="pi-callout pi-warning">{binding.blockers.map((x,i) => <p key={i}>{x}</p>)}</div>}<div className="pi-panel-actions"><Button onClick={() => navigate("strategy")}>前往策略核实绑定</Button><Button type="primary" disabled={!binding.can_confirm} onClick={() => beginConfirm("binding")}>审核并确认当前绑定</Button></div></>}</section>;
  const reportHasGaps = Boolean(report && !reconciliationWaiting(report) && historyGaps(report).length);
  const staleProofKeys = guideSteps?.slice(0,3).filter((_,index)=>guideState(index)==="NEEDS_RECHECK").map(row=>row.key) ?? [];
  const renderReview = (summaryOnly=false) => <InspectionRuntimeReview runtime={runtime} error={errors.runtime} staleProofKeys={staleProofKeys} summaryOnly={summaryOnly} onShowReview={()=>setStep(3)}
    historyGap={reportHasGaps ? `${pendingDays ? `${pendingDays} 个交易日待核对；` : ""}${report!.trade_date} 的历史执行资料不完整。` : undefined}
    onProblem={key=>{if(key==="reconciliation"){if(reportHasGaps)openHistoryGap();else open("reconciliation",true);}else if(key==="data")open("data",true);else if(key==="binding")setStep(2);else if(key==="coverage")open("coverage",true);else void load();}}/>;
  const titleFor: Record<Panel,string> = { material: "历史缺口", guide: initialScope ? "审核并启用新的账户核验范围" : "运行状态审核", reconciliation: "执行与对账", evidence: "补证关联与本人审核", data: "整合数据缺口与批量修复", binding: "审核模型与策略运行绑定", history: "处理记录与审核历史", dual: "双跑验收", operations: "引擎运行与切换", job: "自动对账作业与逐日记录", help: "这页怎么用", ai: "AI 诊断与修复引导", coverage: "历史未知与账户新覆盖起点" };
  return <div className="pi-workbench">
    <header className="pi-page-head"><div><h2><Compass size={20}/>检查与对账</h2><p>修复当前数据、审核运行状态；执行对账与操作记录分别管理</p></div><div><Button type="text" icon={<CircleHelp size={15}/>} onClick={() => open("help")}>页面说明</Button><Button type="text" icon={<Sparkles size={15}/>} onClick={() => open("ai")}>AI 辅助</Button></div></header>
    <section className={`pi-hero ${canBuy === true && !unknown ? "pi-hero-ok" : ""}`}><div>{canBuy === true && !unknown ? <ShieldCheck size={28}/> : <ShieldAlert size={28}/>}<div><h1>{title}</h1><p>{unknown ? "结果读取不完整，不能把未知显示成正常运行。" : blockers.length ? `${blockers.length} 类运行条件待处理。当前数据与状态在下方处理，执行历史在“执行与对账”中核实。` : "实际权限与所需材料由服务端判定，详情可查看完整依据。"}</p></div></div><div className="pi-hero-actions"><Button type="primary" disabled={loading && !runtime} icon={<ArrowRight size={15}/>} onClick={() => open(waiting && !blockers.length ? "job" : "guide")}>{canBuy === true ? "查看运行依据" : waiting && !blockers.length ? "查看等待进度" : "开始恢复引导"}</Button></div></section>
    <div className="pi-capabilities">{([['allow_new_buys','新买入'],['allow_auto_recovery','自动恢复'],['allow_risk_exits','风险退出']] as const).map(([key,label]) => <span key={key}>{label}<b className={runtime?.permissions[key] === true ? "pi-ok-text" : "pi-warning-text"}>{runtime?.permissions[key] === true ? "允许" : runtime?.permissions[key] === false ? "不允许" : "未知"}</b></span>)}<Button type="text" icon={<RefreshCw size={14}/>} loading={loading} onClick={() => void load()}>重新检测</Button></div>
    {errors.actions && <div className="pi-callout pi-error" role="alert"><strong>待办读取失败</strong><p>{errors.actions}</p><Button onClick={() => void load()}>重试读取</Button></div>}
    <section aria-label="数据修复与状态审核模块">
      <div className="pi-section-head"><div><h3>数据修复与状态审核</h3><p className="pi-muted">处理当前行情、指标、评分和运行绑定，再审核实际运行权限。</p></div><div className="pi-module-actions"><Button onClick={()=>open("data")}>打开数据修复</Button><Button onClick={()=>{open("guide");setStep(3);}}>运行状态审核</Button></div></div>
      <div className="pi-task-list">{currentBlockers.map((group,i) => <article className="pi-task-row" key={group.key}><span className="pi-task-number">{i+1}</span><div><h3>{group.title}</h3><p>{group.impact}</p><small>{group.panel === "data" ? "补齐缺口 → 逐对象重检 → 查看结果" : group.panel === "binding" ? "核实当前模型与策略绑定 → 本人审核" : group.details[0]}</small></div><div className="pi-task-actions"><Button onClick={() => {if(group.panel==="data")open("data");else{open("guide");setStep(group.panel==="binding"?2:firstStep());}}}>{group.panel === "data" ? "整合查看与批量处理" : group.panel === "binding" ? "审核策略绑定" : "查看处理依据"}</Button></div></article>)}</div>
      {!loading && !errors.actions && !currentBlockers.length && <div className="pi-empty"><CheckCircle2 size={22}/><div><strong>{canBuy === true ? "当前没有需要处理的阻断" : "当前数据与绑定暂无修复待办"}</strong><p>运行审核仍需核实完整条件，执行对账结果在下方独立管理。</p></div></div>}
    </section>
    <InspectionExecutionEntry pendingDays={pendingDays} earliestDate={report?.trade_date??executionGroup?.items[0]?.trade_date} verifiedThrough={queue?.verified_through} needsReview={check("reconciliation")?.passed!==true} error={jobError} onOpen={()=>{const date=executionGroup?.items[0]?.trade_date;if(date)void inspectDay(date);else open("reconciliation");}} onJob={()=>open("job")}/>
    <footer className="pi-secondary"><span>操作记录</span><Button type="text" icon={<History size={16}/>} onClick={()=>{setHistoryOrigin(null);setHistoryContext(undefined);open("history");}}>审计记录</Button><small>按标的、交易日回看处理与审核</small></footer>
    <details className="pi-disclosure"><summary>引擎专项验收 · 切换引擎前使用</summary><p>独立核验旧、新引擎链的实际决策，日常数据修复与状态审核无需在这里操作。</p><div className="pi-module-actions"><Button icon={<GitCompareArrows size={16}/>} onClick={()=>open("dual")}>双跑验收</Button>{renderOperations && <Button onClick={()=>open("operations")}>引擎运行与切换</Button>}</div></details>
    {otherGroups.length > 0 && <details className="pi-disclosure"><summary>其他提醒与信息 · {otherGroups.length} 项</summary>{otherGroups.map(group => <div key={group.key}><h3>{group.title}</h3><p>{group.impact}</p><Button onClick={() => open(group.panel)}>查看依据与处理入口</Button></div>)}</details>}
    {SURFACES.map((surface) => <InspectionDrawer key={surface} open={layers.includes(surface) && !modal && !dataConfirm} active={panel===surface&&!detailOverlays[surface]}
      focused={["guide","history","evidence"].includes(surface)} icon={surface==="history"?<History size={20}/>:surface==="evidence"?<Link2 size={20}/>:<ShieldCheck size={20}/>}
      zIndex={1000+Math.max(0,layers.lastIndexOf(surface))*20} title={titleFor[surface]}
      context={`检查与对账 / ${["reconciliation","material","evidence","job","coverage"].includes(surface)?"执行与对账":surface==="history"?"操作记录":["dual","operations"].includes(surface)?"引擎验收":"当前数据与状态"}`}
      onClose={close} onBack={layers.lastIndexOf(surface)>0?close:undefined} backLabel={layers.lastIndexOf(surface)>0?`返回${titleFor[layers[layers.lastIndexOf(surface)-1]]}`:undefined}
      initialScroll={scrollPositions.current[surface]??0} onScrollChange={top=>{scrollPositions.current[surface]=top;}}
      afterOpenChange={opened=>{if(surface===panel)setConfirmationReady(!opened);}}
      footer={surface===panel?<div className="pi-drawer-footer"><span>{surface === "guide" ? `第 ${stepPosition} 步 · ${stepLabel(step)}` : surface === "material" ? "历史缺口统一查看，无需逐项登记。" : "关闭本层后回到原位置，已填写内容保留。"}</span>{surface === "guide" && step < 3 && <Button onClick={() => setStep((step+1) as Step)}>{steps[step] === true ? "本步已满足，继续下一步" : "查看下一独立步骤"}</Button>}{surface === "guide" && step === 3 && <Button type="primary" disabled={!runtime?.recovery_summary.can_confirm || proofsNeedRecheck || noReview || Boolean(waiting) || busy} onClick={() => beginConfirm("resume")}>{runtime?.recovery_summary.confirmation_action === "INITIAL_COVERAGE_REVIEW" ? "审核并启用新范围" : "审核并确认恢复"}</Button>}{guideDraft.current && surface !== "guide" && surface !== "history" && surface !== "material" && <Button onClick={() => {setPanel("guide");void load();}}>返回状态审核并刷新</Button>}{surface === "history" && historyOrigin ? <Button type="primary" onClick={backHistory}>返回原问题处理</Button> : <Button onClick={() => void load()} icon={<RefreshCw size={14}/>}>重新检测当前条件</Button>}</div>:undefined}>
      {seenPanels.includes(surface)&&<>
        {surface === "guide" && <>
          {renderReview(step!==3)}
          <div className="pi-review-guide-tools">
          <div className="pi-guide-summary"><span>{initialScope?"核验已接受的新范围":"先修复当前数据，再核实绑定与运行权限"}</span><strong>{visibleSteps.filter(index=>steps[index]===true).length} / {visibleSteps.length} 步已满足</strong></div>
          <nav className={`pi-guide-steps ${initialScope?"":"pi-guide-three"}`} aria-label="恢复运行步骤">{visibleSteps.map((index,position)=><button key={index} type="button" aria-current={step===index?"step":undefined} className={`${step===index?"is-current":""} ${steps[index]===true?"is-passed":""}`} onClick={()=>setStep(index)}><span>{steps[index]===true?"✓":position+1}</span><strong>{stepTitles[index]}</strong><small>{stepLabel(index)}</small></button>)}</nav>
          <div className="pi-guide-links"><span>当前步骤 · {stepTitles[step]}</span><Button type="link" onClick={()=>open("ai",true)}>AI 解释当前步骤</Button></div>
          </div>
        </>}
        {surface === "guide" && guideState(step)==="NEEDS_RECHECK" && <div role="alert" className="pi-callout pi-warning"><strong>本步来源或依据已变化</strong><p>保留原来的选择与审核理由。请重新核实当前材料；原通过证明不用于本次恢复。</p><Button onClick={() => void recheckGuideStep()}>重新核实本步依据</Button>{step===0&&<Button loading={busy} onClick={() => void recheckSources()}>处理来源事件并登记重检</Button>}</div>}
        {surface === "guide" && ["RUNNING","WAITING"].includes(guideState(step)??"") && <Button onClick={() => open("job",true)}>查看本步任务与等待进度</Button>}
        {surface === "guide" && step === 1 && <section className="pi-callout"><strong>核实当前行情、指标与评分</strong><p>{diagnostics?.symbols.filter(symbol=>symbol.evidence.some(row=>!evidenceOkay(row.status))).length??"待核实"} 个标的有待核实数据。查看诊断可定位到具体标的与指标；复杂批量修复进入数据中心。</p><Button onClick={()=>open("data",true)}>查看当前数据缺口</Button></section>}
        {surface === "data" && <InspectionDataOverview key={portfolioId} portfolioId={portfolioId} data={diagnostics} loading={loading} error={errors.data} visible={panel==="data"} onReload={()=>void changed()} onRepair={()=>navigate("settings:data-center:inspection-repair")} onOverlayChange={overlayHandlers.data} onHistory={symbol=>showHistory(groups.find(group=>group.panel==="data"),symbol)} onAnalyse={symbol=>{setAIScope({symbol_ids:[symbol.symbol_id],trade_date:symbol.expected_trade_date??undefined});setAI(null);open("ai",true);}}/>}
        {surface === "reconciliation" && <><InspectionExecutionToolbar date={report?.trade_date} days={queue?.days??[]} loading={loading} onDate={date=>void inspectDay(date)} onJob={()=>open("job",Boolean(guideDraft.current))}/>{reconciliationBody}</>}
        {surface === "guide" && step === 0 && initialScope && <section className="pi-callout"><strong>当前账户基线已被本人接受，待独立审核新范围</strong><p>这里核验当前账户的来源与范围，不表示旧期执行已通过。旧历史与旧差异继续保留，已核验日期不会因此推进。</p><Button onClick={() => open("coverage")}>查看本人接受的账户基线与历史缺口</Button></section>}
        {(surface === "binding" || surface === "guide" && step === 2) && bindingBody}
        {surface==="material" && (report ? <InspectionHistoryGapPanel report={report} onHistory={()=>showHistory(groups.find(x=>x.panel==="reconciliation"),undefined,report.trade_date)} onEvidence={()=>{setEvidenceMaterialOrigin(true);open("evidence",true);}} onRefresh={()=>void refreshMaterialReport()} onCoverage={()=>open("coverage",true)}/> : <div className="pi-callout pi-warning" role={loading?"status":"alert"}><strong>{loading?"正在读取历史缺口…":"历史缺口暂时无法读取"}</strong><p>{errors.reconciliation?reconciliationText(errors.reconciliation,"原始核对报告读取失败，请重试。 "):"尚未取得本日核对报告，请重新读取。"}</p><Button loading={loading} onClick={()=>void load()}>重新读取历史缺口</Button></div>)}
        {surface==="evidence" && evidenceVisited && <div hidden={surface!=="evidence"}><InspectionEvidencePanel portfolioId={portfolioId} tradeDate={selectedTradeDate??report?.trade_date??null} visible={panel==="evidence"} onChanged={changed} onRecheck={rerun} onHistory={()=>showHistory(groups.find(x=>x.panel==="reconciliation"),undefined,selectedTradeDate??report?.trade_date)} backLabel={evidenceMaterialOrigin?"返回历史缺口":"返回执行与对账"} onBack={()=>setPanel(evidenceMaterialOrigin?"material":"reconciliation")} onMissingMaterial={()=>{setEvidenceMaterialOrigin(true);setPanel("material");}} onConfirmationChange={handleDataConfirmation} confirmationReady={confirmationReady}/></div>}
        {surface==="history" && historyVisited && <div hidden={surface !== "history"}><InspectionHistoryPanel portfolioId={portfolioId} visible={panel === "history"} onOverlayChange={overlayHandlers.history} context={historyContext} onReturn={historyOrigin ? backHistory : undefined}/></div>}
        {surface==="coverage" && coverageVisited && <div hidden={surface !== "coverage"}><InspectionCoveragePanel portfolioId={portfolioId} hostVisible={panel==="coverage"} onOverlayChange={overlayHandlers.coverage} onAccepted={() => { void changed(); setPanel("guide"); setStep(3); }} onRepairAccount={() => setPanel("reconciliation")} onViewAudit={() => showHistory()} /></div>}
        {surface==="dual" && dualVisited && <div hidden={surface !== "dual"}><div className="pi-callout"><strong>切换引擎前，对照旧 / 新引擎链的实际决策</strong><p>同一模型与冻结输入做两侧对比，不下真实订单。日常对账无需进行双跑；验收通过也不会自动切换。能力不足时明确展示原因。</p></div>{renderDualRun(overlayHandlers.dual,panel==="dual")}</div>}
        {surface === "operations" && renderOperations?.(overlayHandlers.operations,panel==="operations")}
        {surface === "job" && <section><div className="pi-module-subnav"><Button onClick={()=>open("reconciliation",Boolean(guideDraft.current))}>返回按日核对</Button><span>自动核对作业</span></div><div className="pi-callout"><strong>{job?.enabled ? "自动核对已开启" : "核对作业状态"}</strong><p>{job?.reason ?? "正在读取实际作业与连续覆盖范围…"}</p>{job?.check_summary && <p>本次核对：通过 {job.check_summary.passed_count} · 等待材料 {job.check_summary.waiting_count} · 账实差异 {job.check_summary.difference_count} · 读取失败 {job.check_summary.error_count}。扫描结束后，仍以这些核对结论决定下一步。</p>}{job?.next_run_at && <p>下次检查（北京时间）：{displayTime(job.next_run_at)}</p>}</div>{jobError && <div className="pi-callout pi-error" role="alert">{jobError}</div>}{queue && <><dl className="pi-facts"><div><dt>覆盖起点</dt><dd>{queue.coverage_start ?? "无已接受基线"}</dd></div><div><dt>{queue.coverage_source==="legacy_checkpoint"?"旧版核验标记":"连续已核验至"}</dt><dd>{queue.verified_through ?? "未知 / 尚未核验"}</dd></div><div><dt>目标交易日</dt><dd>{queue.target_trade_date ?? "待日历核实"}</dd></div></dl><div className="pi-table-wrap"><table><thead><tr><th>所属交易日</th><th>状态</th><th>依据 / 下一步</th></tr></thead><tbody>{queue.days.map(day => <tr key={day.trade_date}><td>{day.trade_date}</td><td>{QUEUE_STATUS[day.status] ?? "待核实"}</td><td>{day.reason.split("；").map(reason => /^[A-Z][A-Z0-9_]+$/.test(reason) ? reconDimLabel(reason) : reason).join("；")}<Button type="link" onClick={() => void inspectDay(day.trade_date)}>核查此交易日</Button></td></tr>)}</tbody></table></div>{queue.limitations.map((x,i) => <p key={i} className="pi-muted">{LIMITATION_LABELS[x] ?? "此范围仍缺少可核验证据，请查看来源与处理记录。"}</p>)}</>}{job?.source_events && <section><h3>来源事件待消费 · {job.source_events.count} 项</h3><p>账户、执行结果或补证变化先保留事件，再按真实交易日重检；旧报告与审核继续留存。</p>{job.source_events.events.map(event=><details key={event.id}><summary>{({cash_ledger:"资金流水",sim_orders:"订单",sim_trades:"成交",positions:"持仓",decision_runs:"执行结果",decision_evidence:"决策明细",decision_order_plans:"执行计划",async_tasks:"任务结果",inspection_evidence_decisions:"补证审核"} as Record<string,string>)[event.source_table]??"实际来源"} · {event.source_key??"待定位来源"}</summary><p>来源事件 {event.id} · {event.status} · {displayTime(event.created_at)}</p></details>)}{job.source_events.waiting_for_coverage?<div className="pi-callout pi-warning"><p>来源事件已保留。需先核查可信账户覆盖范围，不能把旧期缺证据直接记为通过。</p><Button onClick={()=>open("coverage",true)}>核查来源与账户覆盖范围</Button></div>:<Button loading={busy} onClick={()=>void recheckSources()}>处理来源事件并登记重检</Button>}</section>}{Boolean(job?.work_items?.length) && <section><h3>持久核对工作单</h3>{job?.work_items?.map(work => <div className="pi-callout" key={work.trade_date}><strong>{work.trade_date} · {work.retry_exhausted ? "自动重试已达上限，需人工核查" : QUEUE_STATUS[work.work_state] ?? "核对进行中"}</strong><p>已尝试 {work.attempt_count} 次 · 需求版本 {work.requested_evidence_version} / 已处理 {work.processed_evidence_version}</p>{work.next_check_at && <p>下次重试（北京时间） {displayTime(work.next_check_at)}</p>}{work.last_error_code && <details><summary>查看本次错误与任务引用</summary><p>{work.last_error_code} · {work.task_id ?? "未派发"}</p></details>}<Button onClick={() => void inspectDay(work.trade_date)}>核查该日证据与处理入口</Button></div>)}</section>}{job&&!job.enabled&&<Button onClick={()=>navigate("settings:schedules")}>配置自动核对计划</Button>}<Button onClick={() => void loadJob()}>重新读取作业与逐日记录</Button></section>}
        {surface === "help" && <section><h3>按职责处理当前问题</h3><ol><li>数据修复：按标的补齐行情、指标与评分；状态审核：核实绑定和实际权限。</li><li>执行与对账：按日核对历史执行、查找材料、关联原订单和查看自动作业。</li><li>审计记录：筛选与回看已经发生的处理和审核，不用于直接授予权限。</li><li>预览限定修复计划，再确认提交；成功步骤保留，失败项继续处理。</li><li>全部适用条件满足后，审核当前报告与实际运行绑定，恢复当前范围。</li></ol><p>历史只用于追溯；旧审批、空数据与未知结果不会成为当前授权。</p><Button onClick={() => open("guide")}>开始引导修复</Button></section>}
        {surface === "ai" && <section><div className="pi-callout"><strong>{aiScope?.symbol_ids?.length ? "分析所选标的的当前数据缺口" : "基于当前环境分析，并接着处理"}</strong><p>AI 读取本组合当前检查快照辅助解释。执行任务和恢复仍由你在正式确认入口提交，AI 不修改交易状态。</p></div><Button loading={aiBusy} onClick={() => void analyse()}>分析当前阻断</Button>{aiError && <div role="alert" className="pi-callout pi-error"><p>{aiError}</p><p>AI 不可用时仍可继续完整人工流程。</p></div>}{ai && <div className="pi-ai-answer"><h3>诊断说明</h3>{aiStale && <p role="alert" className="pi-warning-text">检查材料已刷新，本次建议需要重新分析；正式修复会重新校验范围和版本。</p>}<small>服务端快照 {String(ai.metadata?.inspection_snapshot_id ?? "未保留")} · {String(ai.metadata?.generated_at ?? "时点未保留")}</small><p>{ai.answer}</p>{ai.evidence?.length > 0 && <details className="pi-disclosure"><summary>AI 引用的证据（以正式来源为准）</summary>{ai.evidence.map((e,i) => <p key={i}>{e.content}<small>{e.source}</small></p>)}</details>}{Array.isArray(ai.metadata?.unknowns) && <details className="pi-disclosure"><summary>服务端明确的未知与待处理材料</summary>{ai.metadata.unknowns.map((x: unknown,i: number) => <p key={i}>{textValue(x)}</p>)}</details>}{ai.suggested_actions?.map((action,i) => { const allowed = Array.isArray(ai.metadata?.available_actions) && ai.metadata.available_actions.some((a: any) => a.action_type === action.action_type); const targets: Record<string,Panel> = {VIEW_EXECUTION:"reconciliation",PREVIEW_DATA_REPAIR:"data",REVIEW_BINDING:"binding",VIEW_JOB:"job",VIEW_HISTORY:"history",REVIEW_COVERAGE:"coverage"}; return <div key={i}><p>{action.description}</p>{allowed && targets[action.action_type] && <Button disabled={aiStale} onClick={() => open(targets[action.action_type], true)}>查看依据与正式处理入口</Button>}</div>; })}{ai.warnings?.map((x,i) => <p className="pi-warning-text" key={i}>{textValue(x)}</p>)}</div>}<div className="pi-task-list">{blockers.map(group => <div className="pi-task-row" key={group.key}><div><h3>{group.title}</h3><p>{group.impact}</p></div><Button onClick={() => {if(group.panel==="reconciliation"){open("reconciliation",true);return;}setPanel("guide");setStep(({data:1,binding:2} as Record<string,Step>)[group.panel]??firstStep());}}>进入对应修复步骤</Button></div>)}</div><div className="pi-panel-actions"><Button type="primary" onClick={() => void continueGuide()}>{guideDraft.current ? "继续引导修复" : "开始引导修复"}</Button></div></section>}
        {surface===panel && writeError && !modal && <div className="pi-callout pi-error" role="alert">{writeError}</div>}
      </>}
    </InspectionDrawer>)}
    <Modal focusTriggerAfterClose={false} open={modal !== null && confirmationReady} afterOpenChange={opened => { if (opened) document.getElementById(`pi-review-title-${portfolioId}`)?.closest(".ant-modal")?.querySelector<HTMLButtonElement>("button")?.focus(); }} aria-labelledby={`pi-review-title-${portfolioId}`} title={<span id={`pi-review-title-${portfolioId}`}>{modal === "binding" ? "确认本次实际运行绑定" : modal === "reconcile" ? "审核并确认当前对账结果" : activation ? "审核并启用账户新覆盖范围" : "本人审核并恢复当前范围"}</span>} onCancel={() => { if (!busy) setModal(null); }} onOk={() => void confirm()} okText="确认本次审核" confirmLoading={busy} okButtonProps={{ disabled: !ack || reviewNote.trim().length < 10 || busy || Boolean(activation && !activation.can_confirm) }} destroyOnHidden={false}>
      <div className="pi-workbench">{modal==="resume"&&frozenGuide.current&&<details className="pi-disclosure"><summary>查看本次审核引用的核验结果</summary>{proofSummary(frozenGuide.current.guide)}</details>}{activation && <div className="pi-callout pi-warning"><strong>仅启用第 {activation.generation} 代 · 从 {activation.coverage_start_trade_date} 开始</strong><p>旧期间保持历史未知。本次不会生成历史通过记录，也不会推进任何已核验日期。</p>{activation.checks.map(x => <p key={x.key}>{x.passed ? "已核实" : "待处理"}：{x.reason}</p>)}{activation.blockers.map(x => <p key={x.code} className="pi-error-text">{x.message}</p>)}<small>基线 {activation.baseline_id} · 游标版本 {activation.cursor_row_version}</small></div>}<p>组合 #{portfolioId} · {modal === "binding" ? binding?.change_summary : activation ? `新范围从 ${activation.coverage_start_trade_date} 开始` : `交易日 ${pendingReview.current?.tradeDate ?? "当前范围"}`}</p><dl className="pi-facts"><div><dt>{activation ? "账户基线 / 指纹" : "报告 / 版本"}</dt><dd>{activation ? activation.baseline_id : modal === "binding" ? binding?.snapshot_id : pendingReview.current?.reportId ?? "以当前运行状态及核验条件为准"}<small>{activation ? activation.evidence_fingerprint : modal !== "binding" && pendingReview.current?.reportVersion}</small></dd></div><div><dt>本次影响</dt><dd>仅解除本次已解决条件；其他数据、模型、人工暂停和引擎准入继续独立校验。</dd></div></dl><label className="pi-input-label">审核理由（至少 10 个字）<Input.TextArea value={reviewNote} rows={3} disabled={busy} onChange={e => setReviewNote(e.target.value)} placeholder="写明已核实的证据与本次判断" /></label><Checkbox checked={ack} disabled={busy} onChange={e => setAck(e.target.checked)}>{activation ? "我已核实当前账户基线、代次与指纹，仅启用新起点之后的范围；旧期仍保持未知，不推进任何已核验日期。" : "我已核对当前范围、报告与版本，确认本次处理。材料变化后需重新审核。"}</Checkbox>{writeError && <p role="alert" className="pi-error-text">{writeError}</p>}</div>
    </Modal>
  </div>;
}
