// WP-S-FIX.2: reason_code → 中文文案映射
// 未知 reason_code 回退到 user_message（由调用方处理）

export const REASON_CODE_MESSAGES: Record<string, string> = {
  // 基础数据
  symbols_empty: "标的库为空，请先添加标的",
  stale_bars: "行情数据过期，请同步最新行情",
  no_recent_scan: "尚未执行扫描，请先运行扫描",
  // 评分
  scoring_not_configured: "评分配置未激活，请先激活评分配置",
  no_active_scoring_config: "无活动评分配置",
  // 因子
  factor_ridge_not_ready: "Ridge 因子仓库未就绪",
  no_active_factor_model: "无活动因子模型",
  // 机会扫描
  no_ready_snapshot: "无就绪评分快照，请先执行数据准备",
  // 组合
  no_active_portfolio: "无活动组合，请先创建组合",
  no_active_portfolio_rule: "无活动组合规则",
  // 自动交易
  auto_trade_not_enabled: "自动交易未开启",
  data_health_failed: "数据健康检查未通过",
  // AI
  ai_not_configured: "AI 未配置，请先在设置中配置 AI Profile",
  // 消息渠道
  no_message_channel: "未配置消息渠道",
};

export function getReasonMessage(reasonCode: string | null, fallback: string): string {
  if (!reasonCode) return fallback;
  return REASON_CODE_MESSAGES[reasonCode] ?? fallback;
}
