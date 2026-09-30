// 扫描快照 scope（覆盖范围）内部代码 → 用户可读名称。
//
// 拟真走查在「机会中心 · 扫描记录」里检出：表格里直接印 `cn_stock`
// （后端 discovery_score_snapshot.scope 的取值，见该列注释 cn_stock / cn_etf）。
// 项目里 PortfolioStrategyRules 早有一份同样含义的映射，这里给扫描记录用，
// 并把"查不到就什么都不显示"作为默认，避免内部值泄到界面。
import { t } from "../i18n";

const SCOPE_LABEL_KEYS: Record<string, string> = {
  cn_stock: "scanScopeCnStock",
  csa: "scanScopeCnStock",
  cn_etf: "scanScopeCnEtf",
  etf: "scanScopeCnEtf",
};

/** 已收录的 scope 取值，供测试与守护使用。 */
export const KNOWN_SCAN_SCOPES: readonly string[] = Object.keys(SCOPE_LABEL_KEYS);

/**
 * scope 的用户可见名称。
 * @returns 中文名；未收录的取值返回 null（由调用方决定显示 "-"，不回退成原 code）
 */
export function describeScanScope(scope?: string | null): string | null {
  const value = (scope ?? "").trim().toLowerCase();
  if (!value) return null;
  const key = SCOPE_LABEL_KEYS[value];
  return key ? t(key) : null;
}
