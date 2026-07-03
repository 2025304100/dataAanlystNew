// 图表主题常量：统一 DetailModal / InvestmentCenter 共用的样式与颜色映射，避免重复定义

// 未来买入计划的区域样式映射（对应原始 FUTURE_PLAN_STYLE）
export const FUTURE_PLAN_STYLE_MAP: Record<string, { fill: string; stroke: string; dash: string }> = {
  avoid: { fill: "rgba(180, 35, 24, 0.12)", stroke: "#b42318", dash: "6,4" },
  high: { fill: "rgba(15, 118, 110, 0.16)", stroke: "#0f766e", dash: "" },
  low: { fill: "rgba(37, 99, 235, 0.11)", stroke: "#2563eb", dash: "4,4" },
  normal: { fill: "rgba(15, 118, 110, 0.10)", stroke: "#0f766e", dash: "4,4" },
};

// 图表信号标记线颜色映射（stop=红, target=青, buy-zone=紫）
export const SIGNAL_COLOR_MAP: Record<string, string> = {
  stop: "#b42318",
  target: "#0f766e",
  "buy-zone": "#7c3aed",
};
