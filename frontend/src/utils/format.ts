import { t, getLocale, DOT } from "../i18n";

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

// 价格保留两位小数；null/undefined 时返回 0
export function roundPrice(v: number | null | undefined): number {
  return v != null ? Math.round(v * 100) / 100 : 0;
}

export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "-";
  return `${(value * 100).toFixed(1)}%`;
}

export function statPct(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : percent(value);
}

export function score(value: number | null | undefined, digits = 2): string {
  // 处理 NaN/Infinity，避免 ECharts 渲染异常
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "-";
  return Number(value).toFixed(digits);
}

let currencyCache: string = "CNY";
export function setCurrency(c: string) {
  currencyCache = c;
}

export function money(value: number | null | undefined, digits = 0): string {
  // 处理 NaN/Infinity，避免 ECharts 渲染异常
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "-";
  const currency = currencyCache || "CNY";
  try {
    return new Intl.NumberFormat(getLocale(), {
      style: "currency",
      currency,
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  } catch {
    return new Intl.NumberFormat(getLocale(), {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  }
}

export function compactNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return new Intl.NumberFormat(getLocale(), { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  return new Date(value).toLocaleString(getLocale());
}

export function formatRelativeTime(value: string | null | undefined): string {
  if (!value) return "";
  const now = Date.now();
  const then = new Date(value).getTime();
  const diff = Math.max(0, now - then);
  const minutes = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days = Math.floor(diff / 86400000);
  if (minutes < 1) return t("justNow");
  if (minutes < 60) return `${minutes}${t("minutesAgo")}`;
  if (hours < 24) return `${hours}${t("hoursAgo")}`;
  if (days < 30) return `${days}${t("daysAgo")}`;
  return formatDate(value);
}

export function ageDays(value: string | null | undefined): number {
  if (!value) return 0;
  const diff = Date.now() - new Date(value).getTime();
  return Math.max(0, Math.floor(diff / 86400000));
}

export function discoveryFreshness(item: { created_at?: string; is_frozen?: boolean; warning_days?: number }) {
  const days = ageDays(item.created_at);
  if (item.is_frozen) {
    return {
      className: "frozen",
      label: `${t("frozen")}${DOT}${days ? t("freshnessDays").replace("{days}", String(days)) : t("freshnessToday")}`,
    };
  }
  const warningDays = Number(item.warning_days ?? 3);
  const label = days ? t("freshnessDays").replace("{days}", String(days)) : t("freshnessToday");
  return {
    className: days >= warningDays ? "warning" : "",
    label: days >= warningDays ? `${label}${DOT}${t("freshnessWarn")}` : label,
  };
}

export function badgeClass(value: string | null | undefined): string {
  if (value === "overheat" || value === "exit" || value === "reduce") return "badge danger";
  if (value === "cooldown" || value === "hold") return "badge warn";
  return "badge";
}

export function sideBadgeClass(side: string): string {
  return side === "sell" ? "badge warn" : "badge";
}

export function pnlClass(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  if (value > 0) return "pnl-positive";
  if (value < 0) return "pnl-negative";
  return "";
}

export function sentimentClass(value: string | null | undefined): string {
  if (value === "positive") return "sentiment-positive";
  if (value === "negative") return "sentiment-negative";
  return "sentiment-neutral";
}

export function riskClass(value: string | null | undefined): string {
  if (value === "high") return "risk-high";
  if (value === "medium") return "risk-medium";
  return "risk-low";
}

export function joinParts(parts: (string | null | undefined | false)[]): string {
  return parts.filter(Boolean).join(" | ");
}

export function signalLabel(signal: { kind: string; label: string }): string {
  if (signal.kind === "buy-zone") return t("markerBuy");
  if (signal.kind === "stop") return t("markerStop");
  if (signal.kind === "target") return t("markerTarget");
  if (signal.label === "MA10") return t("ma10");
  if (signal.label === "MA20") return t("ma20");
  return signal.label;
}

export function opportunityScoreValue(item: any): number {
  return item.final_opportunity_score ?? item.priority_score ?? item.timing_score ?? item.quality_score ?? 0;
}

export function baseOpportunityScoreValue(item: any): number {
  return item.base_opportunity_score ?? item.priority_score ?? item.timing_score ?? item.quality_score ?? 0;
}

export function withFinalOpportunityScore(item: any, newsSnapshot: any): any {
  const baseScore = Number(baseOpportunityScoreValue(item) ?? 0);
  const news = (newsSnapshot?.symbols ?? []).find((n: any) => Number(n.symbol_id) === Number(item.symbol_id)) ?? null;
  const messageScore = Number(news?.message_score ?? 0);
  const confidence = Number(news?.confidence ?? 0.35);
  const newsAdjustmentPct = clamp((messageScore * confidence) / 100, -0.12, 0.12);
  const newsMultiplierValue = 1 + newsAdjustmentPct;
  return {
    ...item,
    base_opportunity_score: baseScore,
    final_opportunity_score: Number(clamp(baseScore * newsMultiplierValue, 0, 100).toFixed(2)),
    news_message_score: messageScore,
    news_confidence: confidence,
    news_multiplier: Number(newsMultiplierValue.toFixed(4)),
    news_adjustment_pct: Number(newsAdjustmentPct.toFixed(4)),
  };
}

export function lotSizeForDetail(detail: any): number {
  return detail?.symbol?.region === "cn" ? 100 : 1;
}

export function computeSuggestedPrice(detail: any): number {
  const setup = detail?.latest_trade_setup;
  const lastBar = detail?.bars?.[detail.bars.length - 1];
  return setup?.entry_min ?? lastBar?.close ?? detail?.position?.latest_price ?? 0;
}

export function computeSuggestedBuyQuantity(detail: any): number {
  const price = computeSuggestedPrice(detail);
  const lotSize = lotSizeForDetail(detail);
  const budget =
    detail?.latest_trade_setup?.tranche_plan?.[0]?.amount ??
    detail?.latest_trade_setup?.remaining_stage_amount ??
    detail?.latest_trade_setup?.recommended_position_amount ??
    0;
  if (!(price > 0) || !(budget > 0)) return 0;
  return Math.floor(budget / price / lotSize) * lotSize;
}

export function computeDefaultSellQuantity(detail: any): number {
  return Math.floor(detail?.position?.quantity ?? 0);
}

export function parseTradeDate(value: string): Date {
  const [year, month, day] = String(value).split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day));
}

export function weekKeyForDate(value: string): string {
  const date = parseTradeDate(value);
  const day = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const weekNo = Math.ceil(((date.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
  return `${date.getUTCFullYear()}-${String(weekNo).padStart(2, "0")}`;
}

export function aggregateWeeklyBars(bars: any[]): any[] {
  const weeks: any[] = [];
  let current: any = null;
  bars.forEach((bar) => {
    const weekKey = weekKeyForDate(bar.trade_date);
    if (!current || current.week_key !== weekKey) {
      current = {
        week_key: weekKey,
        trade_date: bar.trade_date,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
        volume: bar.volume || 0,
      };
      weeks.push(current);
      return;
    }
    current.trade_date = bar.trade_date;
    current.high = Math.max(current.high, bar.high);
    current.low = Math.min(current.low, bar.low);
    current.close = bar.close;
    current.volume = (current.volume || 0) + (bar.volume || 0);
  });
  return weeks;
}

export function inferSymbolPayload(rawCode: string) {
  const symbol = rawCode.trim().toUpperCase();
  if (!symbol) {
    throw new Error(t("symbolCodeRequired"));
  }
  if (/^\d{6}$/.test(symbol)) {
    const isEtf = symbol.startsWith("5") || symbol.startsWith("15") || symbol.startsWith("16") || symbol.startsWith("18");
    const market = symbol.startsWith("6") || symbol.startsWith("5") ? "sh" : "sz";
    return {
      symbol,
      name: symbol,
      asset_type: isEtf ? "etf" : "stock",
      market,
      board: "main",
      theme: isEtf ? "custom-etf" : "custom-stock",
    };
  }
  if (/^[A-Z.]{1,10}$/.test(symbol)) {
    const usEtfs = new Set(["DIA", "GLD", "IWM", "QQQ", "SLV", "SPY", "TLT", "VTI", "VOO", "XLK", "XLF", "XLE"]);
    return {
      symbol,
      name: symbol,
      asset_type: usEtfs.has(symbol) ? "etf" : "stock",
      market: "us",
      board: "custom",
      theme: usEtfs.has(symbol) ? "custom-etf" : "custom-stock",
    };
  }
  throw new Error(t("symbolCodeRequired"));
}
