// WP5.1：InvestmentCenter 兼容入口
//
// 设计说明：
// - 原单体组件已拆分为 9 个子组件（位于 symbol-research/ 目录）
// - 本文件保留作为兼容入口（WP5.4 要求），App.tsx 路由不动
// - 内部渲染 <SymbolResearchShell>，传递必要的全局状态
// - WP5.3：不显式传递 sourceContext，由 Shell 自行从 URL/sessionStorage 解析
//   （旧 InvestmentCenter tab 直接进入 → 默认 legacy；从其他入口跳转 → 读 sessionStorage）
//
// 关键约束：
// - 不破坏 App.tsx 中的 <InvestmentCenter openMetricModal={...} /> 调用
// - 保留 openMetricModal 透传（虽然子组件当前未直接使用，但保留接口供未来扩展）
// - 行为与原 InvestmentCenter 完全一致
import SymbolResearchShell from "./symbol-research/SymbolResearchShell";

interface InvestmentCenterProps {
  openMetricModal: (type: string) => void;
}

/**
 * InvestmentCenter 兼容入口。
 *
 * 原 1990 行单体组件已按 WP5.1 拆分为 9 个子组件：
 * - SymbolResearchShell（壳层，持有全部状态与副作用）
 * - SymbolSearchHeader（搜索框 + 快捷标的 + 搜索历史）
 * - SymbolRelationshipBar（候选/观察/组合成员/持仓/告警关联状态）
 * - FactorExplanationPanel（因子解释 + 模型 ID + 因子贡献 + 数据截止时间）
 * - SymbolAlertSummary（告警摘要，正式提醒归告警中心）
 * - RiskReferencePanel（研究情景参数，正式风控读 PortfolioRule）
 * - TradePlanPanel（交易计划刷新 + 参数覆盖 + 情景分析 + 分批计划 + 未来买入计划）
 * - SymbolChartPanel（K线 + MA10/MA20 + MACD + RSI + BOLL + 绘图 Hook 统一）
 * - SingleSymbolBacktestPanel（事件驱动回测 + 规则模板 + 成本配置 + 结果详情 + 结果应用到组合）
 *
 * 所有现有功能（搜索/详情/加入观察/加入组合/下单/回测/告警/绘图）不丢失。
 *
 * WP5.3：sourceContext 由 Shell 内部解析：
 * - URL query params（可分享链接）
 * - sessionStorage（应用内导航通道）
 * - 默认 legacy
 */
export default function InvestmentCenter({ openMetricModal }: InvestmentCenterProps) {
  return (
    <SymbolResearchShell
      openMetricModal={openMetricModal}
    />
  );
}
