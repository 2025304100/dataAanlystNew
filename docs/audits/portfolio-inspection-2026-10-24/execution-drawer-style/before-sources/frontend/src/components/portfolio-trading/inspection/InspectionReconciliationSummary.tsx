import { Button } from "antd";
import type { ReconciliationResponse } from "../../../types";
import { historyGaps, reconciliationWaiting } from "./reconciliationGaps";

export default function InspectionReconciliationSummary({ report, onHistory, onGap }: {
  report: ReconciliationResponse; onHistory: () => void; onGap: () => void;
}) {
  const gaps = historyGaps(report);
  const waiting = reconciliationWaiting(report);
  const mismatch = report.items.some(item => /CASH|NAV|POSITION_MISMATCH/.test(item.dimension));
  const queried = report.activity_evidence?.filter(item => ["sim_orders", "sim_trades", "cash_ledger"].includes(item.source_code) && item.query_status === "QUERIED");
  return <section className="pi-recon-focus">
    <header data-testid="recon-verdict">
      <div className="pi-recon-heading-meta"><small>核对交易日 · {report.trade_date}</small><Button type="link" size="small" onClick={onHistory}>本日处理记录</Button></div>
      <h3>{report.zero_sum_check_passed ? "当天对账已通过" : waiting ? "等待当天执行结果" : mismatch ? "发现账实差异，先核实差异来源" : gaps.length ? "历史执行资料不完整" : "当天执行情况尚未核实"}</h3>
      <p>{waiting ? "结果到达后由系统核对，目前无需人工审核。" : gaps.length && !report.zero_sum_check_passed ? "系统已检查现有来源，仍无法完整还原当日执行。" : "核对当天准备怎样交易、实际执行了什么，以及资金与持仓是否对得上。"}</p>
    </header>
    {gaps.length > 0 && !waiting && <div className="pi-gap-entry" aria-label="历史缺口摘要"><Button type="primary" onClick={onGap}>查看并处理历史缺口</Button><small>缺失依据、已查来源和后续处理集中在详情中查看。</small></div>}
    <p className="pi-recon-review-condition">{waiting ? "等待实际结果，无需逐项登记材料。" : report.zero_sum_check_passed ? "本日核对已通过，继续核实其余运行条件。" : gaps.length ? "历史缺口尚未解决，当前核对结果不能确认通过。" : "本日核对尚未通过，请处理实际差异后重检。"}</p>
    <details className="pi-disclosure"><summary>查看已读取的核对结果</summary><div className="pi-recon-known"><p>{queried?.length ? queried.map(item => `${item.source_label} ${item.count ?? "未知"} 条`).join(" · ") : "已读取的来源详见核查明细。"}</p>{queried?.some(item => item.count === 0) && <small>查到 0 条仅表示该来源没有记录，不能据此认定当天无需交易或执行成功。</small>}{report.cash_audit?.status === "CONSISTENT" && <p>当前完整账本的资金核对一致；这不代表该交易日的历史余额与持仓已核实。</p>}<small>连续已核验至 {report.last_reconciled_trade_date ?? "尚无连续核验依据"} · {report.read_only ? "只读预览" : "本次重检结果"}</small></div></details>
  </section>;
}
