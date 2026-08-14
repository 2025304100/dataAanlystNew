/**
 * WP0.4：旧路由与 activeTab 兼容映射
 *
 * 机会中心改造项目第一阶段：建立 URL/Tab 兼容映射表，暂不强制改名。
 * 旧深链接（如 ?tab=discovery）通过 resolveLegacyTab 解析后自动重定向到有效 tab，
 * 不返回 404。当前阶段 OpportunityCenter / Research 组件尚未落地，
 * 因此映射后的新值若不在 VALID_TABS 中，会回退到原 legacy 值，确保不破坏现有功能。
 *
 * 后续工作包新增 "opportunity" / "research" tab 后，只需把它们加入 VALID_TABS，
 * resolveLegacyTab 会自动开始返回新值，无需改动调用方。
 */

/**
 * 旧 tab 值 → 新 tab 值的映射表（第一阶段只建表，暂不强制改名）。
 * - portfolio → portfolio（保留，但页面名称后续会改为"组合交易"）
 * - discovery → opportunity（旧机会挖掘链接重定向到新机会中心）
 * - investment → research（投资中心后续改为标的研究）
 * - 其他现有 tab 值保持不变
 *
 * WP9.1：investment 一级入口已从导航移除，但 ?tab=investment 深链接仍需可用。
 * 因 "research" 尚未作为独立 tab 落地，resolveLegacyTab 对 investment 暂回退到
 * "investment"（渲染 InvestmentCenter 薄壳 = SymbolResearchShell，即标的研究视图）。
 */
export const LEGACY_TAB_MAPPING: Record<string, string> = {
  portfolio: "portfolio",
  discovery: "opportunity",
  investment: "research",
  "ai-settings": "settings",
};

/**
 * 当前应用中实际存在的 tab 值集合。
 * 后续新增 "opportunity" / "research" 时，需同步加入此集合，
 * resolveLegacyTab 才会真正切换到新 tab。
 *
 * WP1.1：已加入 "opportunity"，旧 ?tab=discovery 深链接现可重定向到新机会中心入口。
 * 注意：旧 discovery 入口在 App.tsx 仍保留，但用户主动访问新入口时会切到 opportunity。
 *
 * WP9.1："investment" 保留在此集合中仅为兼容深链接（?tab=investment），
 * 一级导航按钮已在 App.tsx 移除。用户访问旧链接时仍渲染标的研究视图，不会 404。
 */
export const VALID_TABS: ReadonlySet<string> = new Set([
  "decision",
  "investment",
  "portfolio",
  "discovery",
  "opportunity",
  "macro",
  "news",
  "settings",
  "ai-settings",
]);

/** 默认 tab，当传入值既不是有效 tab 也无法映射时的兜底。 */
export const DEFAULT_TAB = "decision";

/**
 * 将旧 tab 值解析为当前有效的 tab 值。
 *
 * 解析规则：
 * 1. 先查 LEGACY_TAB_MAPPING 得到「目标新值」；
 * 2. 若目标新值在 VALID_TABS 中，直接返回（未来新组件落地后生效）；
 * 3. 否则若原 legacy 值仍在 VALID_TABS 中，返回 legacy 值（当前阶段保持兼容）；
 * 4. 都不匹配时返回 DEFAULT_TAB，避免渲染空白页 / 404。
 *
 * @param legacy 旧 tab 值（来自 URL ?tab=... 或外部深链接）
 * @returns 当前应用可渲染的有效 tab 值
 */
export function resolveLegacyTab(legacy: string): string {
  if (!legacy || typeof legacy !== "string") {
    return DEFAULT_TAB;
  }
  const mapped = LEGACY_TAB_MAPPING[legacy] ?? legacy;
  if (VALID_TABS.has(mapped)) {
    return mapped;
  }
  if (VALID_TABS.has(legacy)) {
    return legacy;
  }
  return DEFAULT_TAB;
}

/**
 * 从当前页面 URL 的 query string 中读取 `tab` 参数。
 * 不存在时返回 null。
 */
export function getTabFromUrl(): string | null {
  if (typeof window === "undefined" || !window.location) {
    return null;
  }
  const params = new URLSearchParams(window.location.search);
  const raw = params.get("tab");
  return raw;
}

/**
 * 在应用启动时调用：读取 URL 中的 ?tab=... 并解析为有效 tab 值。
 * 返回 null 表示 URL 未指定 tab（调用方应保持原有默认行为）。
 */
export function resolveTabFromUrl(): string | null {
  const raw = getTabFromUrl();
  if (raw === null) {
    return null;
  }
  return resolveLegacyTab(raw);
}
