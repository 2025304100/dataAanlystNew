/**
 * 因子语义类别（类型）枚举 → 标签 i18n key 的唯一映射。
 *
 * 取值来源：后端 `rule_config.category`、经典底座类别、F1 经验库 `category`，
 * 固定 6 类（设计 §6.3.11 的 25 个系统模板按此分布）。
 *
 * 存在理由：原先 `ClassicalBaseConfig` 与模板配置页各写一份映射，容易漂移；
 * 且验收报告 P2-14 指出类别曾以英文 key（`trend` / `reversal` / `volatility`…）
 * 直接上屏，违反 §3.4 i18n 红线。统一到这里后新增页面只需 `t(categoryLabelKey(x))`。
 */

export const ALL_CATEGORIES = [
  "trend",
  "reversal",
  "volatility",
  "valuation",
  "quality",
  "volume_price",
] as const;

export type FactorCategory = (typeof ALL_CATEGORIES)[number];

export const CATEGORY_LABEL_KEY: Record<string, string> = {
  trend: "miningEvoCatTrend",
  reversal: "miningEvoCatReversal",
  volatility: "miningEvoCatVolatility",
  valuation: "miningEvoCatValuation",
  quality: "miningEvoCatQuality",
  volume_price: "miningEvoCatVolumePrice",
};

/**
 * 类别 → i18n key。
 * **未知取值返回 null**（调用方原样展示后端值，不臆造译名）。
 */
export function categoryLabelKey(
  category: string | null | undefined,
): string | null {
  if (!category) return null;
  return CATEGORY_LABEL_KEY[String(category)] ?? null;
}

/** 类别 → 已翻译标签；未知取值原样返回。 */
export function categoryLabel(
  category: string | null | undefined,
  translate: (key: string) => string,
): string {
  if (!category) return "-";
  const key = categoryLabelKey(category);
  return key ? translate(key) : String(category);
}
