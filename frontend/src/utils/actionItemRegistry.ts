import type { ActionItem, ActionPrimaryAction } from "../types";

// P0 行动项执行层注册表：语义由后端下发（title/impact/primary_action），
// 这里只负责「动作能否在当前前端落地」的白名单校验与安全兜底，
// 任何未验证 target 一律降级 none，杜绝跳转到不存在的页面。

export const PORTFOLIO_TAB_TARGETS = [
  "overview",
  "members",
  "strategy",
  "backtest",
  "governance",
] as const;
export type PortfolioTabTarget = (typeof PORTFOLIO_TAB_TARGETS)[number];
export const GOVERNANCE_TAB_TARGETS = ["overview", "state", "reconciliation", "audit"] as const;
export type GovernanceTabTarget = (typeof GOVERNANCE_TAB_TARGETS)[number];
export const SUBTAB_TARGETS = [
  ...PORTFOLIO_TAB_TARGETS,
  "governance:overview",
  "governance:state",
  "governance:reconciliation",
  "governance:audit",
] as const;
export type SubtabTarget = (typeof SUBTAB_TARGETS)[number];

/** 主页与治理内部页用不同目标，避免内部页名被当成主页而回退总览。 */
export function resolvePortfolioNavigation(target: string | null | undefined): {
  tab: PortfolioTabTarget;
  governanceTab?: GovernanceTabTarget;
} | null {
  if (!target) return null;
  if ((PORTFOLIO_TAB_TARGETS as readonly string[]).includes(target)) {
    return { tab: target as PortfolioTabTarget };
  }
  if (target.startsWith("governance:")) {
    const tab = target.slice("governance:".length);
    if ((GOVERNANCE_TAB_TARGETS as readonly string[]).includes(tab)) {
      return { tab: "governance", governanceTab: tab as GovernanceTabTarget };
    }
  }
  return null;
}

const REDIRECT_TARGETS = new Set<string>([
  "settings:data-center",
  "settings:tasks",
  "settings:factor-center",
]);

const MODAL_TARGETS = new Set<string>(["add-candidate", "model-binding-review"]);

export type ResolvedAction =
  | { kind: "none" }
  | { kind: "toggle" }
  | { kind: "subtab"; tab: SubtabTarget }
  | { kind: "redirect"; target: string }
  | { kind: "open-modal"; modal: string }
  | { kind: "guided"; flow: "reconciliation" };

const FALLBACK_TITLE = "检测到一项待处理条件";
const RAW_ENUM_RE = /^[A-Z][A-Z0-9_]{3,}$/;

export function isRawEnum(value: string): boolean {
  return RAW_ENUM_RE.test(value.trim());
}

export function safeTitle(item: ActionItem): string {
  const title = (item.title ?? "").trim();
  return title && !isRawEnum(title) ? title : FALLBACK_TITLE;
}

export function safeImpact(item: ActionItem): string {
  const impact = (item.impact ?? "").trim();
  return impact && !isRawEnum(impact) ? impact : safeTitle(item);
}

export function resolveAction(
  action: ActionPrimaryAction | null | undefined,
): ResolvedAction {
  if (!action) return { kind: "none" };
  const target = (action.target ?? "").trim();
  switch (action.action_type) {
    case "toggle":
      return { kind: "toggle" };
    case "guided":
      return target === "governance:reconciliation"
        ? { kind: "guided", flow: "reconciliation" }
        : { kind: "none" };
    case "open-modal":
      return MODAL_TARGETS.has(target)
        ? { kind: "open-modal", modal: target }
        : { kind: "none" };
    case "subtab":
      return (SUBTAB_TARGETS as readonly string[]).includes(target)
        ? { kind: "subtab", tab: target as SubtabTarget }
        : { kind: "none" };
    case "redirect":
      return REDIRECT_TARGETS.has(target)
        ? { kind: "redirect", target }
        : { kind: "none" };
    case "none":
    default:
      return { kind: "none" };
  }
}

export function resolveItemAction(item: ActionItem): ResolvedAction {
  return resolveAction(item.primary_action);
}
