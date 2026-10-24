// Raw diagnostics stay in the response and audit trail, never in business copy.
const internalDetails = /\b[a-z][a-z\d]*_[a-z\d_]+\b|\b(?:DecisionRun|DecisionEvidence|SUCCEEDED|NOT_CHECKED)\b|\b[\da-f]{32,}\b|[{}]|\b[a-z][a-z\d_]*\s*=/i;

export function reconciliationText(value: string | null | undefined, fallback: string): string {
  const text = value?.trim();
  return text && !internalDetails.test(text) ? text : fallback;
}

const reasons: Record<string, string> = {
  DECISION_RUN_NOT_FOUND: "尚未找到当天可核验的自动运行记录或决策明细，当前无法确认该日的完整执行情况。",
  MISSING_ORDER_PLAN: "已有交易决策，但尚未找到对应的订单计划或执行记录，请核查原始材料后重新核对。",
  UNFILLED_PLAN: "计划执行的数量与已成交数量尚未核对一致，请核查订单进度和已有成交。",
  POSITION_MISMATCH: "目标持仓与实际持仓或成本不一致，请核查持仓变化和相关成交。",
  NAV_BROKEN: "资金流水、成交金额与账户余额尚未核对一致，请核查资金收支。",
  NOT_CHECKED: "必要的核对材料尚未齐全，请核查资金、持仓或执行来源后重新检查。",
  NOT_DUE: "尚未到预计执行时间，请等待实际执行结果后再核对。",
  EXECUTION_IN_PROGRESS: "当天的执行任务仍在排队或处理中，请等待实际结果。",
  EXECUTION_FAILED: "当天的执行尝试未成功，请先核查已有成交与未完成事项。",
  READ_ERROR: "核对来源读取失败，结果暂时无法确认，请恢复来源读取后重试。",
};

export function reconciliationReason(code?: string | null, detail?: string | null): string {
  const fallback = reasons[code ?? ""] ?? "核对材料仍需核实，请查看相关来源后重新检查。";
  // Legacy missing-run messages mix internal identifiers with assumptions about
  // whether execution was required. Present the evidence gap without that claim.
  return code === "DECISION_RUN_NOT_FOUND" ? fallback : reconciliationText(detail, fallback);
}

const valueLabels: Record<string, string> = {
  target_qty_delta: "计划变动数量", filled_delta: "实际成交变动数量",
  target_quantity: "目标持仓数量", final_position_quantity: "实际持仓数量",
  quantity: "持仓数量", avg_cost: "持仓成本", cash: "资金余额",
};

export function reconciliationValue(value: unknown): string {
  if (value == null || value === "") return "待核实";
  if (typeof value === "number") return Number.isFinite(value) ? value.toLocaleString("zh-CN", {maximumFractionDigits: 4}) : "待核实";
  if (typeof value === "string") {
    try { return reconciliationValue(JSON.parse(value)); } catch { /* Plain business text. */ }
    return reconciliationText(value, "待核实");
  }
  if (typeof value === "object" && !Array.isArray(value)) {
    const items = Object.entries(value).flatMap(([key, raw]) => {
      if (!valueLabels[key] || (typeof raw !== "number" && typeof raw !== "string") || String(raw).trim() === "") return [];
      const number = Number(raw);
      return Number.isFinite(number) ? [`${valueLabels[key]} ${reconciliationValue(number)}`] : [];
    });
    return items.join("；") || "待核实";
  }
  return "待核实";
}
