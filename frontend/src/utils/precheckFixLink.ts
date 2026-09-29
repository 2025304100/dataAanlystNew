import { t } from "../i18n";

/**
 * 预检阻断项 `fix_link.tab` → 人类可读的去处名称（体检报告 §三十四）。
 *
 * 背景：`PortfolioBacktestCenter` 过去把 `tab` **原样打印**在界面上，于是用户会看到
 * 「下一步：去同步基础数据（settings-data-center）」这种内部常量。项目的 G3 验收记录里
 * 已明确要求操作页不得直接展示 `members_only`、`legacy_scan` 这类内部值。
 *
 * 关键取舍：**查不到就什么都不显示**。宁可少一句去处提示，也不能把后端常量泄给用户 ——
 * 这正是当初漏掉的地方（新加一个 blocker 时忘了登记中文名，界面就会露常量）。
 * 下面的单元测试把这条钉住。
 */
const FIX_TAB_LABEL_KEYS: Record<string, string> = {
  "portfolio-backtest": "precheckFixTab.portfolioBacktest",
  "settings-data-center": "precheckFixTab.dataCenter",
  "factors": "precheckFixTab.factors",
  "settings": "precheckFixTab.settings",
};

/** 已知 tab 值集合：新增 blocker 时按这份清单补中文名与测试。 */
export const KNOWN_FIX_TABS: readonly string[] = Object.keys(FIX_TAB_LABEL_KEYS);

/** 返回可读去处名称；未知或为空时返回 null（调用方据此不渲染）。 */
export function describeFixTab(tab?: string | null): string | null {
  if (!tab) return null;
  const key = FIX_TAB_LABEL_KEYS[tab.trim()];
  return key ? t(key) : null;
}
