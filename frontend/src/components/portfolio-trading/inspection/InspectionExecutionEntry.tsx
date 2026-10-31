import { Button } from "antd";

interface Props {
  pendingDays: number | null; earliestDate?: string | null; verifiedThrough?: string | null;
  needsReview: boolean; error?: string | null; onJob: () => void;
}

export default function InspectionExecutionEntry({pendingDays, earliestDate, verifiedThrough, needsReview, error, onJob}: Props) {
  return <section className="pi-execution-module" aria-label="执行与对账模块">
    <div><h3>执行与对账 <span className={`pi-tag ${needsReview ? "pi-warn" : "pi-neutral"}`}>{error ? "读取待核实" : pendingDays != null && pendingDays > 0 ? `${pendingDays} 个交易日待核对` : needsReview ? "审核前需核实" : "查看核对结果"}</span></h3>
      <p>按交易日核对计划与实际执行。每日核对统一从上方「对账连续性」进：点任意一天即可打开当天的执行、材料与原订单关联。这里只保留作业进度与调度结果。</p>
      <small>{error || `连续已核验至 ${verifiedThrough ?? "尚无可信范围"}${earliestDate ? ` · 下一个待核对日 ${earliestDate}` : ""}`}</small>
    </div>
    <div className="pi-module-actions"><Button onClick={onJob}>查看作业</Button></div>
  </section>;
}
