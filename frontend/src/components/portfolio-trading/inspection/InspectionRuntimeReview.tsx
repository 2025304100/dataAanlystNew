import { Button } from "antd";
import { ArrowRight, CheckCircle2, ListChecks, ShieldAlert, ShieldCheck } from "lucide-react";
import type { InspectionRuntime } from "../../../types/portfolioInspection";
import { reconciliationText } from "./reconciliationPresentation";
import { InspectionConclusion } from "./InspectionSurface";

const LABELS: Record<string, string> = { reconciliation: "执行对账", data: "当前数据", binding: "运行绑定", runtime: "审核权限", coverage: "账户核验范围" };
const EFFECTS: Record<string, string> = {
  RECONCILIATION_CONFIRM: "确认已通过的对账结果，解除本次对应的对账暂停。",
  RESUME_ADMIN_PAUSE: "确认当前条件后，解除本次人工暂停。",
  INITIAL_REVIEW: "完成当前组合的首次运行审核。",
  INITIAL_COVERAGE_REVIEW: "仅启用已接受的新账户核验范围，旧期间继续保留历史未知。",
};
interface Props {
  runtime: InspectionRuntime | null;
  error?: string;
  staleProofKeys: string[];
  historyGap?: string;
  onProblem: (key: string) => void;
  summaryOnly?: boolean;
  onShowReview?: () => void;
}

export default function InspectionRuntimeReview({ runtime, error, staleProofKeys, historyGap, onProblem, summaryOnly = false, onShowReview }: Props) {
  const summary = runtime?.recovery_summary;
  const checks = summary?.checks ?? [];
  const problems = checks.filter(row => row.passed !== true || staleProofKeys.includes(row.key));
  for (const proof of runtime?.guide?.steps.slice(0, 3) ?? []) if (!["PASSED", "NOT_APPLICABLE"].includes(proof.status) && !problems.some(row => row.key === proof.key)) problems.push({ key: proof.key, passed: false, reason: proof.reason });
  for (const key of staleProofKeys) if (!problems.some(row => row.key === key)) problems.push({ key, passed: false, reason: "核验依据已变化，需重新检查本步。" });
  const otherProblems = [...new Set(summary?.blockers.filter(reason => !checks.some(row => row.reason === reason)) ?? [])];
  const ready = summary?.can_confirm && !staleProofKeys.length;
  const noReview = summary?.recommended_action === "NONE" || summary?.review_required === false;
  const remaining = problems.length + otherProblems.length + (runtime ? 0 : 1);
  return <div className="pi-runtime-review pi-surface-panel">
    <InspectionConclusion label="当前审核结论" title={!runtime ? "当前运行条件暂时无法确认" : runtime.permissions.allow_new_buys ? "当前新买入条件允许" : ready ? "适用条件已满足，等待本人确认" : noReview ? "当前无需提交恢复审核" : "仍有问题未解决，暂不能确认恢复"}
      icon={ready || runtime?.permissions.allow_new_buys ? <ShieldCheck size={20}/> : <ShieldAlert size={20}/>} tone={error?"error":ready||runtime?.permissions.allow_new_buys?"success":"warning"}
      description={error ? reconciliationText(error, "运行检查读取失败，请重新检测。") : summary?.waiting_reason ?? (ready ? "本次检查结果已汇总，可核实下方影响后提交一次恢复确认。" : noReview ? "查看当前核对结论，等待实际运行结果或适用条件满足。" : "先处理下方剩余问题，系统重新核验后再提交恢复确认。")}
      badge={remaining ? `${remaining} 项待处理` : ready ? "待本人确认" : "当前检查结果"}
      meta={summaryOnly?"当前结论随实际核验结果更新":"确认时仍会重新校验全部适用条件"}
      actions={summaryOnly&&onShowReview&&<Button onClick={onShowReview}>查看剩余问题与确认影响<ArrowRight size={14} aria-hidden="true"/></Button>}>
      <div className="pi-review-permissions">{([['allow_new_buys','新买入'],['allow_auto_recovery','自动恢复'],['allow_risk_exits','风险退出']] as const).map(([key,label])=><span key={key}>{label}<strong className={runtime?.permissions[key]===true?"is-allowed":""}>{runtime?.permissions[key]===true?"允许":runtime?.permissions[key]===false?"不允许":"待核实"}</strong></span>)}</div>
    </InspectionConclusion>
    {!summaryOnly&&<>
    <section aria-label="审核剩余问题"><div className="pi-surface-section-head"><h3><ListChecks size={16} aria-hidden="true"/>剩余问题 <span className="pi-surface-count">{remaining}</span></h3><span>按当前依据逐项处理</span></div><div className="pi-review-problems">
      {!runtime && <div><span className="pi-tag pi-warn">未知</span><span className="pi-review-check-copy"><strong>当前运行检查</strong><small>检查结果尚未完整读取，不能据此确认恢复。</small></span><Button type="link" onClick={() => onProblem("runtime")}>重新读取检查</Button></div>}
      {problems.map(row => <div key={row.key}><span className="pi-tag pi-warn">{staleProofKeys.includes(row.key) ? "需重检" : row.passed == null ? "待核实" : "未通过"}</span><span className="pi-review-check-copy"><strong>{LABELS[row.key] ?? "其他运行条件"}</strong><small>{staleProofKeys.includes(row.key) ? "来源或核验依据已变化，原通过结果需重新检查。" : row.key === "reconciliation" && historyGap ? historyGap : reconciliationText(row.reason, "此项条件仍待核实，请重新检测当前条件。")}</small></span><Button className="pi-review-check-action" type="link" onClick={() => onProblem(row.key)}>{row.key === "reconciliation" ? historyGap ? "查看历史缺口" : "查看执行对账" : row.key === "data" ? "查看数据问题" : row.key === "binding" ? "处理运行绑定" : "查看处理条件"}<ArrowRight size={13} aria-hidden="true"/></Button></div>)}
      {otherProblems.map(reason => <p className="pi-error-text" key={reason}>{reconciliationText(reason, "仍有独立条件未满足，请重新检测。")}</p>)}
      {runtime && problems.length === 0 && otherProblems.length === 0 && <p className="pi-ok-text"><CheckCircle2 size={15} aria-hidden="true"/>当前适用的前置条件已满足。</p>}
    </div></section>
    <section aria-label="确认后的影响" className="pi-review-effect"><h3><ShieldCheck size={16} aria-hidden="true"/>确认后的影响</h3>{noReview ? <p>当前无需额外确认；后续权限仍随实际检查结果更新。</p> : <><p>{EFFECTS[summary?.confirmation_action ?? ""] ?? "恢复本次已核实范围的运行状态。"}</p><p className="pi-muted">提交时会重新校验当前依据。新买入、自动恢复和风险退出以提交后的实际权限为准；其他未解决的限制继续保留。</p></>}</section>
    </>}
  </div>;
}
