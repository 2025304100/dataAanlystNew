import {AcceptanceButton as Button} from './AcceptanceControls';
import { useRef } from "react";

import { ArrowLeft, ArrowRight, CalendarDays, ClipboardList, Database, FolderSearch, ListChecks, RefreshCw, Workflow } from "lucide-react";
import type { InspectionJob, InspectionReconciliationQueue } from "../../../types/portfolioInspection";
import { reconDimLabel } from "../../../utils/reconDimLabel";
import { reconciliationText } from "./reconciliationPresentation";
import { InspectionConclusion, InspectionDisclosure } from "./InspectionSurface";
import "./inspectionExecution.css";

interface Props {
  job: InspectionJob | null;
  queue: InspectionReconciliationQueue | null;
  error?: string | null;
  busy: boolean;
  loading?: boolean;
  queueLabels: Record<string, string>;
  limitationLabels: Record<string, string>;
  formatTime: (value: string) => string;
  onBack: () => void;
  onInspectDay: (tradeDate: string) => void;
  onReload: () => void;
  onCoverage: () => void;
  onRecheckSources: () => void;
  onConfigure: () => void;
}

const SOURCE_LABELS: Record<string, string> = { cash_ledger:"资金流水", sim_orders:"订单", sim_trades:"成交", positions:"持仓", decision_runs:"执行结果", decision_evidence:"决策明细", decision_order_plans:"执行计划", async_tasks:"任务结果", inspection_evidence_decisions:"补证审核" };

/** Read-only presentation; existing mutations remain explicit callbacks on their original buttons. */
export default function InspectionJobPanel({ job, queue, error, busy, loading=false, queueLabels, limitationLabels, formatTime, onBack, onInspectDay, onReload, onCoverage, onRecheckSources, onConfigure }: Props) {
  const datesRef = useRef<HTMLDetailsElement>(null);
  const pending = queue?.days.filter(day => !day.history_only && !["PASSED", "NOT_DUE"].includes(day.status)) ?? [];
  const firstDay = pending[0];
  const summary = job?.check_summary;
  const waiting = Boolean(summary?.waiting_count);
  const retryExhausted = job?.work_items?.filter(work => work.retry_exhausted).length ?? 0;
  const title = error ? "作业信息暂时不完整" : !job ? "正在读取自动核对作业" : pending.length ? `仍有 ${pending.length} 个交易日待核对` : waiting ? "扫描已结束，仍在等待材料" : ["running","queued"].includes(job.status) ? "正在核对实际来源" : "自动核对作业状态已读取";
  const showDates = () => { if (datesRef.current) { datesRef.current.open = true; datesRef.current.scrollIntoView?.({ block:"nearest" }); } };
  const reason = (value: string) => reconciliationText(value.split("；").map(part => /^[A-Z][A-Z0-9_]+$/.test(part) ? reconDimLabel(part) : part).join("；"), "此交易日仍需核查实际来源与处理记录。");

  return <section className="pi-job-panel pi-surface-panel" aria-label="自动核对作业详情">
    <InspectionConclusion label="自动核对结论" title={title} icon={<Workflow size={20}/>} tone={error?"error":pending.length||waiting?"warning":"neutral"}
      badge={!job?"读取中":job.enabled?"计划已开启":"计划未开启"}
      description={error?"作业或逐日结果读取未完成，请重新读取后核实；已保留的记录继续可查。":reconciliationText(job?.reason,"正在读取实际作业与连续覆盖范围…")}
      meta={<>{job?.next_run_at?`计划下次检查（北京时间）：${formatTime(job.next_run_at)}`:"下一检查时间以实际计划为准"}{retryExhausted?` · ${retryExhausted} 日自动重试已达上限`:""}</>}
      actions={error||!job?<Button loading={loading} onClick={onReload}>重新读取作业信息<RefreshCw size={14} aria-hidden="true"/></Button>:firstDay?<Button onClick={()=>onInspectDay(firstDay.trade_date)}>核查 {firstDay.trade_date}<ArrowRight size={14} aria-hidden="true"/></Button>:job?.source_events?.waiting_for_coverage?<Button onClick={onCoverage}>核查来源与账户覆盖范围<ArrowRight size={14} aria-hidden="true"/></Button>:<Button onClick={showDates}>查看逐日核对记录<ArrowRight size={14} aria-hidden="true"/></Button>}>
      {summary&&<p className="pi-job-scan-summary">本次核对：通过 {summary.passed_count} · 等待材料 {summary.waiting_count} · 账实差异 {summary.difference_count} · 读取失败 {summary.error_count}。</p>}
    </InspectionConclusion>
    <div className="pi-surface-tools"><Button type="text" icon={<ArrowLeft size={14} aria-hidden="true"/>} onClick={onBack}>返回按日核对</Button><Button type="text" loading={loading} icon={<RefreshCw size={14} aria-hidden="true"/>} onClick={onReload}>重新读取作业与逐日记录</Button>{job&&!job.enabled&&<Button type="text" onClick={onConfigure}>配置自动核对计划</Button>}</div>
    {error&&<div className="pi-callout pi-error" role="alert">{reconciliationText(error,"实际作业或逐日记录读取失败，请重试。")}</div>}
    {queue&&<dl className="pi-job-progress"><div><dt>{queue.coverage_source==="legacy_checkpoint"?"旧版核验标记":"连续已核验至"}</dt><dd>{queue.verified_through??"未知 / 尚未核验"}</dd></div><div><dt>目标交易日</dt><dd>{queue.target_trade_date??"待日历核实"}</dd></div></dl>}
    <InspectionDisclosure ref={datesRef} title="逐日核对记录" description={queue?`${queue.days.length} 个交易日 · ${pending.length} 日待处理`:"逐日记录尚未读取"} icon={<CalendarDays size={16}/>}>
      {queue?<><table className="pi-job-day-list" aria-label="逐日核对记录"><thead><tr className="pi-job-day-head"><th>所属交易日</th><th>状态</th><th>依据 / 下一步</th></tr></thead><tbody>{queue.days.map(day=><tr className="pi-job-day-row" key={day.trade_date}><td className="pi-job-day-date">{day.trade_date}</td><td><span className={`pi-tag ${day.status==="PASSED"?"pi-ok":"pi-warn"}`}>{queueLabels[day.status]??"待核实"}</span></td><td><p>{reason(day.reason)}</p><Button type="link" onClick={()=>onInspectDay(day.trade_date)}>核查此交易日<ArrowRight size={13} aria-hidden="true"/></Button></td></tr>)}</tbody></table>{queue.days.length===0&&<p className="pi-muted">当前返回范围没有逐日记录；以实际覆盖范围及目标交易日为准。</p>}</>:<p className="pi-muted">尚未取得逐日记录，请重新读取作业与逐日记录。</p>}
    </InspectionDisclosure>
    {job?.source_events&&<InspectionDisclosure title="来源事件待消费" description={`${job.source_events.count} 项 · 查看来源与后续处理`} icon={<Database size={16}/>}>
      <p>账户、执行结果或补证变化先保留事件，再按真实交易日重检；旧报告与审核继续留存。</p>
      <div className="pi-job-event-list">{job.source_events.events.map(event=><InspectionDisclosure key={event.id} title={SOURCE_LABELS[event.source_table]??"实际来源"} description={formatTime(event.created_at)}><p>来源事件 {event.id} · {event.status}</p><p>确切来源：{event.source_key??"待定位来源"}</p></InspectionDisclosure>)}</div>
      {job.source_events.waiting_for_coverage?<div className="pi-callout pi-warning"><p>来源事件已保留。需先核查可信账户覆盖范围，不能把旧期缺证据直接记为通过。</p><Button onClick={onCoverage}>核查来源与账户覆盖范围</Button></div>:<Button mutation loading={busy} onClick={onRecheckSources}>处理来源事件并登记重检</Button>}
    </InspectionDisclosure>}
    {Boolean(job?.work_items?.length)&&<InspectionDisclosure title="持久核对工作单" description={`${job?.work_items?.length} 日${retryExhausted?` · ${retryExhausted} 日需人工核查`:" · 处理进度与重试记录"}`} icon={<ClipboardList size={16}/>}>
      {job?.work_items?.map(work=><article className="pi-job-work-item" key={work.trade_date}><div><strong>{work.trade_date}</strong><span className="pi-tag pi-warn">{work.retry_exhausted?"自动重试已达上限，需人工核查":queueLabels[work.work_state]??"核对进行中"}</span></div><p>已尝试 {work.attempt_count} 次</p>{work.next_check_at&&<p>下次重试（北京时间） {formatTime(work.next_check_at)}</p>}<InspectionDisclosure title="查看本次错误与任务引用"><p>需求版本 {work.requested_evidence_version} / 已处理 {work.processed_evidence_version}</p>{work.last_error_code&&<p>{work.last_error_code} · {work.task_id??"未派发"}</p>}</InspectionDisclosure><Button type="link" onClick={()=>onInspectDay(work.trade_date)}>核查该日证据与处理入口<ArrowRight size={13} aria-hidden="true"/></Button></article>)}
    </InspectionDisclosure>}
    <InspectionDisclosure title="覆盖范围与核对说明" description="连续核验、日历依据与作业时点" icon={<FolderSearch size={16}/>}>
      {queue&&<><dl className="pi-facts"><div><dt>覆盖起点</dt><dd>{queue.coverage_start??"无已接受基线"}</dd></div><div><dt>{queue.coverage_source==="legacy_checkpoint"?"旧版核验标记":"连续已核验至"}</dt><dd>{queue.verified_through??"未知 / 尚未核验"}</dd></div><div><dt>目标交易日</dt><dd>{queue.target_trade_date??"待日历核实"}</dd></div></dl>{queue.limitations.map((value,index)=><p key={index} className="pi-muted">{limitationLabels[value]??"此范围仍缺少可核验证据，请查看来源与处理记录。"}</p>)}</>}
      {job?.last_run_at&&<p>最近作业时点（北京时间）：{formatTime(job.last_run_at)}</p>}
      <p>扫描结束后，仍以逐日核对结论决定下一步。计划开启不代表本轮核对已通过，也不会恢复交易权限。</p>
    </InspectionDisclosure>
    <p className="pi-surface-note"><ListChecks size={14} aria-hidden="true"/>先核查实际来源，再处理对应日期；历史未知与其他独立限制继续保留。</p>
  </section>;
}
