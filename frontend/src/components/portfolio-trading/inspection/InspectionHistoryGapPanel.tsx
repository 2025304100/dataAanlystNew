import { Button } from "antd";
import { ArrowRight, Database, FileClock, FolderSearch, HelpCircle, ListChecks, RefreshCw, ShieldAlert, Workflow } from "lucide-react";
import type { ReconciliationResponse } from "../../../types";
import InspectionActivityEvidence from "./InspectionActivityEvidence";
import { MATERIAL_GUIDES } from "./materialGuides";
import { historyGaps, reconciliationWaiting } from "./reconciliationGaps";
import { InspectionConclusion, InspectionDisclosure } from "./InspectionSurface";
import "./inspectionExecution.css";

interface Props {
  report: ReconciliationResponse;
  onHistory: () => void;
  onEvidence: () => void;
  onRefresh: () => void;
  onCoverage: () => void;
  onJob?: () => void;
  loading?: boolean;
}

export default function InspectionHistoryGapPanel({ report, onHistory, onEvidence, onRefresh, onCoverage, onJob, loading=false }: Props) {
  const gaps = historyGaps(report);
  const waiting = reconciliationWaiting(report);
  return <section className="pi-history-gap pi-surface-panel" aria-label="历史缺口详情">
    <section aria-label="历史缺口后续处理">
      <InspectionConclusion label="历史缺口结论" badge={report.trade_date} icon={<ShieldAlert size={20}/>} tone={gaps.length&&!waiting?"warning":"neutral"}
        title={waiting ? "等待执行结果到达" : gaps.length ? "当日执行资料尚不完整" : "当前报告没有列出材料缺口"}
        description={waiting ? "本日仍在等待结果，暂不需要人工补齐历史材料。" : gaps.length ? "先查看真实原订单或核查原始备份，来源恢复后再核对本日。缺失依据统一查看，无需逐项登记。" : "可回到执行与对账查看完整结论；没有材料缺口不代表其他核对项目已经通过。"}
        meta={waiting?"等待结果，不创建补材料任务":`${gaps.length} 类依据待核实 · 保留原始记录与历史未知`}
        actions={<>{!waiting&&gaps.some(row=>row.kind==="execution")&&<Button onClick={onEvidence}>查看可关联的原订单<ArrowRight size={14} aria-hidden="true"/></Button>}{waiting&&onJob&&<Button onClick={onJob} icon={<Workflow size={14} aria-hidden="true"/>}>查看等待进度</Button>}</>}/>
      <div className="pi-surface-tools"><Button type="text" loading={loading} icon={<RefreshCw size={14} aria-hidden="true"/>} onClick={onRefresh}>重新读取现有来源</Button><Button type="text" icon={<FileClock size={14} aria-hidden="true"/>} onClick={onHistory}>查看该日查找与处理记录</Button></div>
    </section>
    {gaps.length > 0 && <><div className="pi-surface-section-head"><h3><ListChecks size={16} aria-hidden="true"/>缺失的历史依据 <span className="pi-surface-count">{gaps.length}</span></h3><span>按需展开查找方法</span></div><ul className="pi-gap-list" aria-label="系统发现的历史缺口">{gaps.map((row,index) => {
      const guide = row.kind ? MATERIAL_GUIDES[row.kind] : null;
      return <li key={row.key} aria-label={row.title}><span className="pi-execution-row-number" aria-hidden="true">{index+1}</span><div><div className="pi-gap-row-title"><h4>{row.title}</h4><span className="pi-tag pi-warn">{waiting ? "等待结果" : "来源不完整"}</span></div><p>{row.reason}</p>{guide && <InspectionDisclosure title="查看这项依据的来源与查找方法" icon={<FolderSearch size={14}/>}><p>{guide.purpose}</p><ul>{guide.sources.map(source => <li key={source}>{source}</li>)}</ul><p className="pi-muted">外部备份需由维护人员核验并恢复到原来源，随后刷新核对结果；此处不要求逐项填写登记表。</p><InspectionDisclosure title="可核验的原始资料需要包含什么"><ul>{guide.required.map(item => <li key={item}>{item}</li>)}</ul></InspectionDisclosure></InspectionDisclosure>}</div></li>;
    })}</ul></>}
    {!waiting&&<>
      <InspectionDisclosure title="资料在原系统或备份中" description="如何恢复可信来源" icon={<FolderSearch size={16}/>}><p>由维护人员核验并恢复原始来源；来源恢复后重新读取。本页保留缺口，不要求你为三类材料分别登记。</p><p>补证保留正式申请与审核，关联原订单后仍需重新核对账实差异。</p></InspectionDisclosure>
      <InspectionDisclosure title="资料确实无法恢复时" description="保留历史未知，查看后续处置条件" icon={<HelpCircle size={16}/>}><p>该日保留历史未知。仅填写说明或反复重检不能让缺口通过；可以先继续处理当前数据与绑定问题。</p><p>新的账户核验范围需核实当前资金、逐标的持仓及未完成事项，并另行申请和审核。查看处置条件不会接受新基线或恢复运行。</p><Button onClick={onCoverage}>查看历史缺口处置条件</Button></InspectionDisclosure>
    </>}
    <InspectionDisclosure title="查看系统已检查的来源明细" description="当日订单、成交、流水及原任务" icon={<Database size={16}/>}><InspectionActivityEvidence facts={report.activity_evidence}/></InspectionDisclosure>
  </section>;
}
