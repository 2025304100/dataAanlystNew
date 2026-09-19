// WP5.1：标的关联状态栏
//
// 设计说明：
// - 任务清单 #3：候选/观察/组合成员/持仓/告警关联状态
// - 现有 OpportunityStatusBadges（src/components/opportunity/）已实现五段状态徽标，
//   通过 useSymbolRelationships hook 调用 GET /api/v1/symbols/{id}/relationships
// - 本组件作为薄包装层：保持入口在 symbol-research/ 下，便于壳层统一编排
// - 行为与 OpportunityStatusBadges 完全一致，不重写已稳定逻辑
//
// 关键约束：
// - 接口失败时降级显示"状态未知"，不误报"未加入"
// - 五段状态：candidate / observation / portfolio_member / position / alert
// - 不复制 AppContext：直接复用 OpportunityStatusBadges 的 hook 链
import { OpportunityStatusBadges } from "../opportunity/OpportunityStatusBadges";
import type { SymbolRelationshipBarProps } from "./types";

/**
 * 标的关联状态栏。
 * 直接复用 OpportunityStatusBadges 组件，保持行为一致。
 */
export default function SymbolRelationshipBar({
  symbolId,
  showInactive = false,
  compact = false,
  className,
  onOpenDetail,
}: SymbolRelationshipBarProps) {
  return (
    <span
      className={`symbol-research-relationship-bar ${className ?? ""}`}
      data-symbol-id={symbolId ?? ""}
    >
      <OpportunityStatusBadges
        symbolId={symbolId}
        showInactive={showInactive}
        compact={compact}
        onOpenDetail={onOpenDetail}
      />
    </span>
  );
}
