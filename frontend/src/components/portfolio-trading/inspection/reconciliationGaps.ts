import type { ReconciliationResponse } from "../../../types";
import type { InspectionMaterialKind } from "../../../types/portfolioInspection";
import { reconciliationReason } from "./reconciliationPresentation";

export interface HistoryGap {
  key: string;
  kind: InspectionMaterialKind | null;
  title: string;
  reason: string;
}

export function reconciliationWaiting(report: ReconciliationResponse) {
  return report.verdict === "WAITING_EXECUTION" || Boolean(report.work_state?.startsWith("waiting"));
}

/** Present the server's missing evidence without creating another approval task. */
export function historyGaps(report: ReconciliationResponse): HistoryGap[] {
  const rows: HistoryGap[] = (report.missing_evidence ?? []).map((item, index) => {
    const code = typeof item === "string" ? undefined : item.code;
    return {
      key: `missing-${index}`,
      kind: code === "DECISION_RUN_NOT_FOUND" || code === "MISSING_ORDER_PLAN" ? "execution" : null,
      title: code === "DECISION_RUN_NOT_FOUND" ? "当天的自动运行记录与决策明细" : code === "MISSING_ORDER_PLAN" ? "交易决策对应的订单计划" : "其他待核实材料",
      reason: reconciliationReason(code, typeof item === "string" ? item : item.detail),
    };
  });
  if (report.execution_expected_evidence?.some(item => item.status === "UNKNOWN")) rows.push({
    key: "schedule", kind: "schedule", title: "当天生效的自动接管与调度配置",
    reason: "尚不能确定当天是否应自动执行；当前开关不能证明历史设置。",
  });
  if (report.checks?.some(item => item.check_code === "POSITIONS" && ["NOT_CHECKED", "UNKNOWN"].includes(item.status))) rows.push({
    key: "positions", kind: "positions", title: "该交易日的持仓明细",
    reason: "历史持仓尚未核验，当前持仓不能代替当日持仓。",
  });
  return rows;
}
