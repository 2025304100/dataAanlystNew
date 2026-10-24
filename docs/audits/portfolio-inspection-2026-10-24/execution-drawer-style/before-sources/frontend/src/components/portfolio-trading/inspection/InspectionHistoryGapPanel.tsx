import { Button } from "antd";
import type { ReconciliationResponse } from "../../../types";
import InspectionActivityEvidence from "./InspectionActivityEvidence";
import { MATERIAL_GUIDES } from "./materialGuides";
import { historyGaps, reconciliationWaiting } from "./reconciliationGaps";

interface Props {
  report: ReconciliationResponse;
  onHistory: () => void;
  onEvidence: () => void;
  onRefresh: () => void;
  onCoverage: () => void;
}

export default function InspectionHistoryGapPanel({ report, onHistory, onEvidence, onRefresh, onCoverage }: Props) {
  const gaps = historyGaps(report);
  const waiting = reconciliationWaiting(report);
  return <section className="pi-history-gap" aria-label="历史缺口详情">
    <header className="pi-gap-heading"><small>交易日 · {report.trade_date}</small><h3>{waiting ? "等待执行结果到达" : gaps.length ? "当日执行资料尚不完整" : "当前报告没有列出材料缺口"}</h3><p>{waiting ? "本日仍在等待结果，暂不需要人工补齐历史材料。" : gaps.length ? "以下是系统核对发现的缺失依据，统一查看和处理，无需分别登记查找结果。" : "可回到执行与对账查看完整结论；没有材料缺口不代表其他核对项目已经通过。"}</p></header>
    {gaps.length > 0 && <ul className="pi-gap-list" aria-label="系统发现的历史缺口">{gaps.map(row => {
      const guide = row.kind ? MATERIAL_GUIDES[row.kind] : null;
      return <li key={row.key} aria-label={row.title}><h4>{row.title}<span className="pi-tag pi-warn">{waiting ? "等待结果" : "来源不完整"}</span></h4><p>{row.reason}</p>{guide && <details className="pi-disclosure"><summary>查看这项依据的来源与查找方法</summary><p>{guide.purpose}</p><ul>{guide.sources.map(source => <li key={source}>{source}</li>)}</ul><p className="pi-muted">外部备份需由维护人员核验并恢复到原来源，随后刷新核对结果；此处不要求逐项填写登记表。</p><details className="pi-disclosure"><summary>可核验的原始资料需要包含什么</summary><ul>{guide.required.map(item => <li key={item}>{item}</li>)}</ul></details></details>}</li>;
    })}</ul>}
    <section className="pi-gap-next" aria-label="历史缺口后续处理"><h3>接下来怎么处理</h3>
      {waiting ? <p>等待本日实际结果到达后刷新核对；不需要为尚未到达的结果填写历史说明。</p> : <>
        {gaps.some(row => row.kind === "execution") && <div><strong>已有原订单可以关联</strong><p>查看系统识别出的真实原订单，按现有补证流程核验关联，再重新核对。补证保留正式申请与审核。</p><Button type="primary" onClick={onEvidence}>查看可关联的原订单</Button></div>}
        <div><strong>资料在原系统或备份中</strong><p>由维护人员核验并恢复原始来源；来源恢复后重新读取。本页保留缺口，不要求你为三类材料分别登记。</p><Button onClick={onRefresh}>重新读取现有来源</Button></div>
      </>}
      <div><strong>需要回查以前的处理</strong><p>按当前组合、交易日和报告定位已有核查、补证与审核记录。</p><Button onClick={onHistory}>查看该日查找与处理记录</Button></div>
      {!waiting && <details className="pi-disclosure"><summary>资料确实无法恢复时</summary><p>该日保留历史未知。仅填写说明或反复重检不能让缺口通过；可以先继续处理当前数据与绑定问题。</p><p>新的账户核验范围需核实当前资金、逐标的持仓及未完成事项，并另行申请和审核。查看处置条件不会接受新基线或恢复运行。</p><Button onClick={onCoverage}>查看历史缺口处置条件</Button></details>}
    </section>
    <details className="pi-disclosure"><summary>查看系统已检查的来源明细</summary><InspectionActivityEvidence facts={report.activity_evidence}/></details>
  </section>;
}
