// WP1.2：候选/已观察/组合成员/持仓/告警统一徽标组件
//
// 关键约束：
// - 接口失败时降级显示"状态未知"，不误报"未加入"
// - 五段状态：candidate / observation / portfolio_member / position / alert
// - 支持紧凑模式（only-active）和完整模式（显示未知占位）
import { memo } from "react";
import { Tag, Tooltip } from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";
import { t, template } from "../../i18n";
import { useSymbolRelationships } from "../../hooks/useSymbolRelationships";

export interface OpportunityStatusBadgesProps {
  /** 标的 ID；为 null/undefined 时不发请求，徽标全部隐藏 */
  symbolId: number | null | undefined;
  /** 是否显示未激活状态的占位徽标（默认 false：只显示激活的状态） */
  showInactive?: boolean;
  /** 紧凑模式：单个徽标合并显示（默认 false） */
  compact?: boolean;
  /** 自定义 className */
  className?: string;
  /** 点击徽标时回调（WP1-FIX.4）；宿主页面据此打开标的详情弹窗 */
  onOpenDetail?: (symbolId: number) => void;
}

/** 单个状态徽标的描述信息 */
interface BadgeItem {
  key: string;
  label: string;
  active: boolean;
  color: string;
  tooltip?: string;
}

/**
 * 标的关联状态徽标组件。
 * 数据来源：useSymbolRelationships(symbolId)
 * 降级规则：loading/degraded/error 状态下显示"状态未知" Tooltip 提示。
 */
function OpportunityStatusBadgesBase({
  symbolId,
  showInactive = false,
  compact = false,
  className,
  onOpenDetail,
}: OpportunityStatusBadgesProps) {
  const { data, loading, degraded, error } = useSymbolRelationships(symbolId);

  // 无 symbolId：不渲染任何徽标
  if (!symbolId) {
    return null;
  }

  // 加载中：显示加载提示
  if (loading && !data) {
    return (
      <span className={`opportunity-status-badges ${className ?? ""}`} data-state="loading">
        <Tag color="default">{t("opportunityBadgeLoading")}</Tag>
      </span>
    );
  }

  // 降级或错误：显示"状态未知"，不误报"未加入"
  if (degraded || (!data && error)) {
    const tooltipText = error
      ? `${t("opportunityBadgeDegraded")}: ${error}`
      : t("opportunityBadgeDegradedTooltip");
    return (
      <span className={`opportunity-status-badges ${className ?? ""}`} data-state="unknown">
        <Tooltip title={tooltipText}>
          <Tag color="default" icon={<QuestionCircleOutlined />}>
            {t("opportunityBadgeUnknown")}
          </Tag>
        </Tooltip>
      </span>
    );
  }

  // 无数据但无错误：返回 null（不应出现，但兜底）
  if (!data) {
    return null;
  }

  // 构造五段状态徽标
  const badges: BadgeItem[] = [
    {
      key: "candidate",
      label: t("opportunityBadgeCandidate"),
      active: data.candidate.has_candidate,
      color: "blue",
      tooltip: data.candidate.has_candidate
        ? `${t("opportunityBadgeCandidateTip")}: ${data.candidate.scope ?? "-"}`
        : undefined,
    },
    {
      key: "observation",
      label: t("opportunityBadgeObservation"),
      active: data.observation.has_observation,
      color: "cyan",
      tooltip: data.observation.has_observation
        ? `${t("opportunityBadgeObservationTip")}: ${data.observation.watchlist_name ?? "-"}`
        : undefined,
    },
    {
      key: "portfolio_member",
      label: t("opportunityBadgePortfolioMember"),
      active: data.portfolio_member.has_portfolio_membership,
      color: "geekblue",
      tooltip: data.portfolio_member.has_portfolio_membership
        ? `${t("opportunityBadgePortfolioMemberTip")}: ${data.portfolio_member.portfolio_name ?? "-"}`
        : undefined,
    },
    {
      key: "position",
      label: t("opportunityBadgePosition"),
      active: data.position.has_position,
      color: "green",
      tooltip: data.position.has_position
        ? `${t("opportunityBadgePositionTip")}: ${data.position.quantity ?? 0}`
        : undefined,
    },
    {
      key: "alert",
      label: t("opportunityBadgeAlert"),
      active: data.alert.has_active_alert,
      color: "orange",
      tooltip: data.alert.has_active_alert
        ? `${t("opportunityBadgeAlertTip")}: ${data.alert.active_alert_events}`
        : undefined,
    },
  ];

  // 紧凑模式：只显示一个汇总徽标
  if (compact) {
    const activeCount = badges.filter((b) => b.active).length;
    if (activeCount === 0) {
      // 五段全部未激活，但不代表"未加入"，因为这只是当前状态
      return (
        <span className={`opportunity-status-badges compact ${className ?? ""}`} data-state="empty">
          <Tag color="default">{t("opportunityBadgeNoStatus")}</Tag>
        </span>
      );
    }
    return (
      <span className={`opportunity-status-badges compact ${className ?? ""}`} data-state="mixed">
        <Tag
          color="purple"
          style={{ margin: 0, cursor: onOpenDetail ? "pointer" : undefined }}
          onClick={onOpenDetail ? () => onOpenDetail(symbolId!) : undefined}
        >
          {template("opportunityBadgeMixed", { count: String(activeCount) })}
        </Tag>
      </span>
    );
  }

  // 默认模式：只显示激活的徽标（showInactive=false 时）
  const visibleBadges = showInactive ? badges : badges.filter((b) => b.active);

  if (visibleBadges.length === 0) {
    // 不显示任何徽标，但保留 span 以便布局稳定
    return (
      <span
        className={`opportunity-status-badges ${className ?? ""}`}
        data-state="none-active"
        style={{ display: "inline-block" }}
      />
    );
  }

  return (
    <span className={`opportunity-status-badges ${className ?? ""}`} data-state="active">
      {visibleBadges.map((b) =>
        b.tooltip ? (
          <Tooltip key={b.key} title={b.tooltip}>
            <Tag
              color={b.color}
              style={{ margin: 0, cursor: onOpenDetail ? "pointer" : undefined }}
              onClick={onOpenDetail ? () => onOpenDetail(symbolId!) : undefined}
            >
              {b.label}
            </Tag>
          </Tooltip>
        ) : (
          <Tag
            key={b.key}
            color={b.color}
            style={{ margin: 0, cursor: onOpenDetail ? "pointer" : undefined }}
            onClick={onOpenDetail ? () => onOpenDetail(symbolId!) : undefined}
          >
            {b.label}
          </Tag>
        )
      )}
    </span>
  );
}

export const OpportunityStatusBadges = memo(OpportunityStatusBadgesBase);
export default OpportunityStatusBadges;
