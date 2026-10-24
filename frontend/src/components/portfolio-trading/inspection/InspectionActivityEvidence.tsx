import type { ReconciliationActivityEvidence } from "../../../types";
import { reconciliationText } from "./reconciliationPresentation";

const statusLabels: Record<string, string> = {
  QUERIED: "已读取当日来源", CURRENT_ONLY: "仅当前观察",
  CONTEXT_ONLY: "仅调度关联，不能证明执行", CONTEXT_UNAVAILABLE: "调度来源未知",
  READ_ERROR: "读取失败，数量未知",
};
const taskStatusLabels: Record<string, string> = {
  queued:"等待处理", running:"处理中", done:"处理结束", partial:"部分处理完成",
  failed:"处理失败", cancelled:"已取消",
};
const time = (value?: string | null) => {
  if (!value) return "未保留";
  const instant = new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`);
  return Number.isNaN(instant.getTime()) ? value : instant.toLocaleString("zh-CN", {timeZone: "Asia/Shanghai"});
};

export default function InspectionActivityEvidence({facts}: {facts?: ReconciliationActivityEvidence[]}) {
  if (!facts?.length) return null;
  return <section aria-label="实际来源核查"><h3>实际来源核查</h3>
    <p className="pi-muted">当日订单、成交与流水分别核实。当前持仓和调度完成状态各有适用范围，不能代替历史执行证明。</p>
    {facts.map(fact => <details className="pi-disclosure" key={fact.evidence_id}
      open={fact.records?.some(row => row.cross_day_task_reused || row.stale_terminal_task_reused) || undefined}>
      <summary>{fact.source_label} · {fact.count == null ? "数量未知" : `${fact.count} 条`} · {statusLabels[fact.query_status] ?? "来源状态待核实"}</summary>
      <p>{reconciliationText(fact.reason,"当前来源尚需核实，不能据此确认当天的完整执行情况。")}</p>
      {fact.source_code === "execution_dispatch_history" && fact.records?.map(row => <div className={`pi-callout ${row.cross_day_task_reused || row.stale_terminal_task_reused ? "pi-warning" : ""}`} key={row.id}>
        <strong>{row.stale_terminal_task_reused ? "本次调度引用了以前已结束的任务" : row.cross_day_task_reused ? "本次调度复用了早期创建的任务" : "原任务关联记录"}</strong>
        <dl className="pi-facts"><div><dt>本次调度（北京时间）</dt><dd>{time(row.dispatch_created_at)}</dd></div>
          <div><dt>任务创建 / 当前结束时间（北京时间）</dt><dd>{time(row.task_created_at)} / {time(row.task_finished_at)}</dd></div>
          <div><dt>原任务当前状态</dt><dd>{taskStatusLabels[row.task_status??""]??"尚未核实"}<small>仅反映该原任务当前保留的状态</small></dd></div></dl>
        {row.original_scoped_result?.portfolio_results?.map((result, i) => result.error ? <p key={i}>原任务中本组合的结果：{reconciliationText(result.error,"原任务的执行结果需要进一步核实。")}</p> : null)}
        <p>此关联不能用于补造本日执行、无活动结案或消除缺证。</p>
        {row.cross_day_task_reused && <p>这里只保留原任务的当前结果；当前结束时间不能用来倒推每次历史调度的结果。</p>}
      </div>)}
    </details>)}
  </section>;
}
