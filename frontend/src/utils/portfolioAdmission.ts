import type { InspectionRuntime } from "../types/portfolioInspection";

export type AdmissionKind = "checking" | "unknown" | "allowed" | "blocked" | "review" | "waiting";
export interface PortfolioAdmissionSnapshot {
  runtime: InspectionRuntime | null;
  loading: boolean;
  error: string | null;
}

/** Runtime inspection is the sole display authority. Persisted READY is audit state. */
export function portfolioAdmissionView({ runtime, loading, error }: PortfolioAdmissionSnapshot) {
  let kind: AdmissionKind;
  const waiting=runtime?.recovery_summary.phase?.startsWith("WAITING")===true;
  const unknownCheck=runtime?.recovery_summary.checks.some(check=>check.passed==null && !(waiting && check.key==="reconciliation"));
  if (loading) kind = "checking";
  else if (error || !runtime || runtime.permissions.allow_new_buys == null) kind = "unknown";
  else if (unknownCheck) kind = "unknown";
  else if (runtime.permissions.allow_new_buys === true) kind = "allowed";
  else if (runtime.recovery_summary.can_confirm && runtime.recovery_summary.checks.every(check=>check.passed===true)) kind = "review";
  else if (waiting &&
    runtime.recovery_summary.checks.filter(check => ["data", "binding"].includes(check.key)).every(check => check.passed === true)) kind = "waiting";
  else kind = "blocked";
  const labels: Record<AdmissionKind, string> = {
    checking: "正在核实当前运行条件", unknown: "当前交易状态待核实", allowed: "当前运行条件已满足",
    blocked: "新买入已暂停", review: "阻断已清除，等待本人审核", waiting: "等待运行条件到期",
  };
  const reason = error || (kind==="waiting"?runtime?.recovery_summary.waiting_reason:null) ||
    runtime?.capability_reasons?.allow_new_buys?.join("；") || "请读取当前数据、运行绑定与审核条件";
  return { kind, title: labels[kind], reason,
    hint: `${reason}。新买入、自动恢复与风险退出分别核验；具体委托仍需通过执行时门禁。` };
}
