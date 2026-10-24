import { Button } from "antd";
import type { ReactNode } from "react";
import { ArrowRight, CheckCircle2, Clock3, FileClock, FolderSearch, ListChecks, ShieldAlert } from "lucide-react";
import type { ReconciliationResponse } from "../../../types";
import { historyGaps, reconciliationWaiting } from "./reconciliationGaps";
import { InspectionConclusion, InspectionDisclosure } from "./InspectionSurface";
import "./inspectionExecution.css";

export default function InspectionReconciliationSummary({ report, onHistory, onGap, onJob, reviewAction, tools }: {
  report: ReconciliationResponse; onHistory: () => void; onGap: () => void; onJob?: () => void; reviewAction?: ReactNode; tools?: ReactNode;
}) {
  const gaps = historyGaps(report);
  const waiting = reconciliationWaiting(report);
  const mismatch = report.items.some(item => /CASH|NAV|POSITION_MISMATCH/.test(item.dimension));
  const queried = report.activity_evidence?.filter(item => ["sim_orders", "sim_trades", "cash_ledger"].includes(item.source_code) && item.query_status === "QUERIED");
  return <section className="pi-reconciliation-summary pi-surface-panel">
    <div data-testid="recon-verdict"><InspectionConclusion label="本日执行核对结论" badge={report.trade_date}
      title={report.zero_sum_check_passed ? "当天对账已通过" : waiting ? "等待当天执行结果" : mismatch ? "发现账实差异，先核实差异来源" : gaps.length ? "历史执行资料不完整" : "当天执行情况尚未核实"}
      icon={report.zero_sum_check_passed?<CheckCircle2 size={20}/>:waiting?<Clock3 size={20}/>:<ShieldAlert size={20}/>}
      tone={report.zero_sum_check_passed?"success":waiting?"neutral":"warning"}
      description={waiting ? "结果到达后由系统核对，目前无需人工审核。" : gaps.length && !report.zero_sum_check_passed ? "系统已检查现有来源，仍无法完整还原当日执行。" : "核对当天准备怎样交易、实际执行了什么，以及资金与持仓是否对得上。"}
      meta={<>连续已核验至 <strong>{report.last_reconciled_trade_date??"尚无连续核验依据"}</strong> · {report.read_only?"只读预览":"本次重检结果"}</>}
      actions={<>{gaps.length>0&&!waiting&&<span aria-label="历史缺口摘要"><Button onClick={onGap}>查看并处理历史缺口<ArrowRight size={14} aria-hidden="true"/></Button></span>}{waiting&&onJob&&<Button onClick={onJob}>查看等待进度<ArrowRight size={14} aria-hidden="true"/></Button>}{reviewAction}</>}>
      <p className="pi-execution-review-condition">{waiting ? "等待实际结果，无需逐项登记材料。" : report.zero_sum_check_passed ? "本日核对已通过，继续核实其余运行条件。" : gaps.length ? "历史缺口尚未解决，当前核对结果不能确认通过。" : "本日核对尚未通过，请处理实际差异后重检。"}</p>
    </InspectionConclusion></div>
    <div className="pi-surface-tools"><Button type="text" icon={<FileClock size={14} aria-hidden="true"/>} onClick={onHistory}>本日处理记录</Button>{tools}</div>
    <InspectionDisclosure title="查看已读取的核对结果" description="来源数量与适用范围" icon={<ListChecks size={16}/>}>
      <div className="pi-recon-known"><p>{queried?.length ? queried.map(item => `${item.source_label} ${item.count ?? "未知"} 条`).join(" · ") : "已读取的来源详见核查明细。"}</p>{queried?.some(item => item.count === 0) && <small>查到 0 条仅表示该来源没有记录，不能据此认定当天无需交易或执行成功。</small>}{report.cash_audit?.status === "CONSISTENT" && <p>当前完整账本的资金核对一致；这不代表该交易日的历史余额与持仓已核实。</p>}</div>
    </InspectionDisclosure>
    <InspectionDisclosure title="执行核对说明" description="核对的作用与处理顺序" icon={<FolderSearch size={16}/>}><p>这里处理执行记录与历史材料；行情、指标和评分请在“数据修复”处理。</p><p>先核查缺失来源或实际差异，再重新核对本日。历史记录用于追溯；交易权限仍由统一运行审核决定。</p></InspectionDisclosure>
  </section>;
}
