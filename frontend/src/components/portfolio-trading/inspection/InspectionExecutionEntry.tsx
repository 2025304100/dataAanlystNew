import { Button } from "antd";

interface Props {
  pendingDays: number | null; earliestDate?: string | null; verifiedThrough?: string | null;
  needsReview: boolean; error?: string | null; onOpen: () => void; onJob: () => void;
}

export default function InspectionExecutionEntry({pendingDays, earliestDate, verifiedThrough, needsReview, error, onOpen, onJob}: Props) {
  return <section className="pi-execution-module" aria-label="执行与对账模块">
    <div><h3>执行与对账 <span className={`pi-tag ${needsReview ? "pi-warn" : "pi-neutral"}`}>{error ? "读取待核实" : pendingDays != null && pendingDays > 0 ? `${pendingDays} 个交易日待核对` : needsReview ? "审核前需核实" : "查看核对结果"}</span></h3>
      <p>按交易日核对计划与实际执行。历史材料查找、原订单关联和自动核对作业集中在这里处理。</p>
      <small>{error || `当前核对日 ${earliestDate ?? "待核实"} · 连续已核验至 ${verifiedThrough ?? "尚无可信范围"}`}</small>
    </div>
    <div className="pi-module-actions"><Button onClick={onOpen}>每日对账</Button><Button type="link" onClick={onJob}>查看作业</Button></div>
  </section>;
}
