const DOT = " | ";

import zhCN from "./zh-CN";
import enUS from "./en-US";

const I18N: Record<string, Record<string, string>> = {
  "zh-CN": zhCN,
  "en-US": enUS,
};

export const STAGE_LABELS: Record<string, Record<string, string>> = {
  "zh-CN": { accel: "趋势加速", cooldown: "降温观察", overheat: "高位过热", start: "启动确认" },
  "en-US": { accel: "Accel", cooldown: "Cooldown", overheat: "Overheat", start: "Start" },
};

export const ACTION_LABELS: Record<string, Record<string, string>> = {
  "zh-CN": { buy_dip: "回落低吸", exit: "退出观望", hold: "持有观察", open: "试探建仓", reduce: "减仓保护" },
  "en-US": { buy_dip: "Buy Dip", exit: "Exit", hold: "Hold", open: "Open", reduce: "Reduce" },
};

export const ASSET_LABELS: Record<string, Record<string, string>> = {
  "zh-CN": { etf: "ETF", stock: "股票" },
  "en-US": { etf: "ETF", stock: "Stock" },
};

export const WATCHLIST_TYPE_LABELS: Record<string, Record<string, string>> = {
  "zh-CN": { custom: "自定义", eliminate: "排除", trade: "交易", watch: "观察" },
  "en-US": { custom: "Custom", eliminate: "Eliminate", trade: "Trade", watch: "Watch" },
};

export type Locale = "zh-CN" | "en-US";

let currentLocale: Locale = "zh-CN";

export function setLocale(locale: Locale) {
  currentLocale = locale;
}

export function getLocale(): Locale {
  return currentLocale;
}

export function t(key: string): string {
  // 查找当前语言文案，回退到 en-US
  const value = I18N[currentLocale]?.[key] ?? I18N["en-US"]?.[key];
  if (value === undefined) {
    // 开发环境下对缺失 key 进行告警，便于发现遗漏的翻译条目
    if (import.meta.env.DEV) {
      console.warn(`[i18n] missing key: ${key}`);
    }
    return key;
  }
  return value;
}

export function enumLabel(prefix: string, value: string | null | undefined, fallback = "-"): string {
  if (!value) return fallback;
  const key = `${prefix}_${value}`;
  const translated = t(key);
  return translated === key ? value : translated;
}

export function template(key: string, params: Record<string, string | number> = {}): string {
  return t(key).replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? ""));
}

export function stageLabel(value: string | null | undefined): string {
  return STAGE_LABELS[currentLocale]?.[value ?? ""] ?? value ?? "-";
}

export function actionLabel(value: string | null | undefined): string {
  return ACTION_LABELS[currentLocale]?.[value ?? ""] ?? value ?? "-";
}

export function assetTypeLabel(value: string | null | undefined): string {
  return ASSET_LABELS[currentLocale]?.[value ?? ""] ?? value ?? t("unknown");
}

export function watchlistTypeLabel(value: string | null | undefined): string {
  return WATCHLIST_TYPE_LABELS[currentLocale]?.[value ?? ""] ?? value ?? t("unknown");
}

export function sentimentLabel(value: string | null | undefined): string {
  const labels: Record<string, Record<string, string>> = {
    "zh-CN": { positive: "偏利好", negative: "偏利空", neutral: "中性" },
    "en-US": { positive: "Positive", negative: "Negative", neutral: "Neutral" },
  };
  return labels[currentLocale]?.[value ?? ""] ?? value ?? "-";
}

export function riskLabel(value: string | null | undefined): string {
  const labels: Record<string, Record<string, string>> = {
    "zh-CN": { high: "高风险", medium: "中风险", low: "低风险" },
    "en-US": { high: "High risk", medium: "Medium risk", low: "Low risk" },
  };
  return labels[currentLocale]?.[value ?? ""] ?? value ?? "-";
}

export function newsSourceLabel(value: string | null | undefined): string {
  const labels: Record<string, Record<string, string>> = {
    "zh-CN": { cninfo: "巨潮公告", notice: "公告", "eastmoney-news": "东方财富", "macro-news": "宏观新闻" },
    "en-US": { cninfo: "CNInfo", notice: "Notice", "eastmoney-news": "Eastmoney", "macro-news": "Macro" },
  };
  return labels[currentLocale]?.[value ?? ""] ?? value ?? "-";
}

export function regionShortLabel(value: string | null | undefined): string {
  if (value === "cn") return "CN";
  if (value === "us") return "US";
  return value || "-";
}

export function regionLongLabel(value: string | null | undefined): string {
  if (value === "cn") return t("chinaMainland");
  if (value === "us") return t("unitedStates");
  return value || t("unknown");
}

export function sideLabel(value: string | null | undefined): string {
  if (value === "buy") return t("buySide");
  if (value === "sell") return t("sellSide");
  return value ?? "-";
}

export function futureBuyLabel(value: string | null | undefined): string {
  const v = value ?? "-";
  return t(v) === v ? v : t(v);
}

export function futurePriorityLabel(value: string | null | undefined): string {
  const key = `priority_${value}`;
  return t(key) === key ? value ?? "-" : t(key);
}

export function futureTriggerLabel(plan: { label: string; trigger: string } | null | undefined): string {
  const key = `futureTrigger_${plan?.label}`;
  return t(key) === key ? plan?.trigger ?? "-" : t(key);
}

export function factorLabel(code: string | null | undefined, fallbackName?: string): string {
  if (!code) return fallbackName ?? "-";
  return FACTOR_NAME_LABELS[currentLocale]?.[code] ?? fallbackName ?? code;
}

export function factorCategoryLabel(value: string | null | undefined): string {
  if (!value) return "-";
  return FACTOR_CATEGORY_LABELS[currentLocale]?.[value] ?? value;
}

export function factorDirectionLabel(value: string | null | undefined): string {
  if (!value) return "-";
  return FACTOR_DIRECTION_LABELS[currentLocale]?.[value] ?? value;
}

// Translate tranche plan labels (Starter/Trend/Momentum)
const TRANCHE_LABEL_MAP: Record<string, string> = {
  Starter: "trancheLabelStarter",
  Trend: "trancheLabelTrend",
  Momentum: "trancheLabelMomentum",
  Probe: "trancheLabelProbe",
  Confirm: "trancheLabelConfirm",
  Expand: "trancheLabelExpand",
  Custom: "custom",
  custom: "custom",
};
export function trancheLabel(value: string): string {
  const key = TRANCHE_LABEL_MAP[value];
  if (key === "custom") {
    return WATCHLIST_TYPE_LABELS[currentLocale]?.custom ?? value;
  }
  return key ? t(key) : value;
}

// Translate tranche triggers from English backend text
export function trancheTrigger(trigger: string): string {
  if (!trigger || currentLocale !== "zh-CN") return trigger;
  if (trigger.toLowerCase().includes("pullback")) {
    // Extract zone range like "19.03 - 20.58"
    const match = trigger.match(/[\d.]+\s*-\s*[\d.]+/);
    const zone = match ? match[0] : "";
    return t("trancheTriggerPullback").replace("{zone}", zone);
  }
  if (trigger.toLowerCase().includes("breakout") && trigger.toLowerCase().includes("retest")) {
    return t("trancheTriggerBreakout");
  }
  if (trigger.toLowerCase().includes("pulls back near MA10")) {
    return t("trancheTriggerTrendPullback");
  }
  if (trigger.toLowerCase().includes("breakout extends")) {
    const match = trigger.match(/([\d.]+)/g);
    const price = match?.[match.length - 1] ?? "";
    return t("trancheTriggerBreakoutExtends").replace("{price}", price);
  }
  return trigger;
}

export const FACTOR_NAME_LABELS: Record<string, Record<string, string>> = {
  "zh-CN": {
    ep_ttm: "市盈率倒数 (TTM)",
    negative_pb: "负市净率",
    roe_yoy_growth: "ROE 同比增长 (百分点)",
    main_inflow_5d_ratio: "5日主力净流入 / 成交额",
    lhb_institution_net_ratio: "龙虎榜机构净买入 / 成交额",
    turnover_z20: "20日换手率 Z 分数",
    hot_rank_attention: "东方财富热度排名关注度",
    tail_accumulation_proxy: "尾盘 accumulation 代理指标",
  },
  "en-US": {
    ep_ttm: "E/P (TTM)",
    negative_pb: "Negative PB",
    roe_yoy_growth: "ROE YoY Growth (ppt)",
    main_inflow_5d_ratio: "5-day Main Inflow / Turnover",
    lhb_institution_net_ratio: "LHB Institution Net / Turnover",
    turnover_z20: "20-day Turnover Z-Score",
    hot_rank_attention: "EastMoney Hot-Rank Attention",
    tail_accumulation_proxy: "Tail-session Accumulation Proxy",
  },
};

export const FACTOR_CATEGORY_LABELS: Record<string, Record<string, string>> = {
  "zh-CN": { fundamental: "基本面", capital_flow: "资金面", sentiment: "情绪面", technical: "技术面" },
  "en-US": { fundamental: "Fundamental", capital_flow: "Capital Flow", sentiment: "Sentiment", technical: "Technical" },
};

export const FACTOR_DIRECTION_LABELS: Record<string, Record<string, string>> = {
  "zh-CN": { higher_better: "高者为佳", lower_better: "低者为佳", nonlinear: "非线性" },
  "en-US": { higher_better: "Higher is Better", lower_better: "Lower is Better", nonlinear: "Non-linear" },
};

export { DOT };
