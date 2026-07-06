import { describe, it, expect, beforeEach } from "vitest";
import {
  clamp,
  roundPrice,
  percent,
  statPct,
  score,
  setCurrency,
  money,
  compactNumber,
  formatDate,
  ageDays,
  discoveryFreshness,
  badgeClass,
  sideBadgeClass,
  pnlClass,
  sentimentClass,
  riskClass,
  joinParts,
  opportunityScoreValue,
  baseOpportunityScoreValue,
  withFinalOpportunityScore,
  lotSizeForDetail,
  computeSuggestedPrice,
  computeSuggestedBuyQuantity,
  computeDefaultSellQuantity,
  parseTradeDate,
  weekKeyForDate,
  aggregateWeeklyBars,
  inferSymbolPayload,
} from "../format";

describe("clamp", () => {
  it("限制值在 [min, max] 范围内", () => {
    expect(clamp(5, 0, 10)).toBe(5);
    expect(clamp(-1, 0, 10)).toBe(0);
    expect(clamp(11, 0, 10)).toBe(10);
  });
  it("边界值", () => {
    expect(clamp(0, 0, 10)).toBe(0);
    expect(clamp(10, 0, 10)).toBe(10);
  });
  it("min == max 时返回 min", () => {
    expect(clamp(5, 7, 7)).toBe(7);
    expect(clamp(100, 7, 7)).toBe(7);
  });
});

describe("roundPrice", () => {
  it("null/undefined 返回 0", () => {
    expect(roundPrice(null)).toBe(0);
    expect(roundPrice(undefined)).toBe(0);
  });
  it("四舍五入到两位小数", () => {
    expect(roundPrice(12.345)).toBe(12.35);
    expect(roundPrice(12.344)).toBe(12.34);
    expect(roundPrice(0.005)).toBe(0.01);
  });
});

describe("percent", () => {
  it("null/undefined 返回 '-'", () => {
    expect(percent(null)).toBe("-");
    expect(percent(undefined)).toBe("-");
  });
  it("NaN/Infinity 返回 '-'", () => {
    expect(percent(NaN)).toBe("-");
    expect(percent(Infinity)).toBe("-");
    expect(percent(-Infinity)).toBe("-");
  });
  it("正常值返回百分比字符串（保留1位小数）", () => {
    expect(percent(0.123)).toBe("12.3%");
    expect(percent(1)).toBe("100.0%");
    expect(percent(0)).toBe("0.0%");
    expect(percent(-0.05)).toBe("-5.0%");
  });
});

describe("statPct", () => {
  it("null/undefined 返回 '-'", () => {
    expect(statPct(null)).toBe("-");
    expect(statPct(undefined)).toBe("-");
  });
  it("有效值委托给 percent", () => {
    expect(statPct(0.5)).toBe("50.0%");
  });
});

describe("score", () => {
  it("null/undefined 返回 '-'", () => {
    expect(score(null)).toBe("-");
    expect(score(undefined)).toBe("-");
  });
  it("NaN/Infinity 返回 '-'", () => {
    expect(score(NaN)).toBe("-");
    expect(score(Infinity)).toBe("-");
  });
  it("默认保留 2 位小数", () => {
    expect(score(75.456)).toBe("75.46");
  });
  it("digits 参数生效", () => {
    expect(score(75.456, 0)).toBe("75");
    expect(score(75.456, 3)).toBe("75.456");
  });
});

describe("money", () => {
  beforeEach(() => setCurrency("CNY"));
  it("null/undefined 返回 '-'", () => {
    expect(money(null)).toBe("-");
    expect(money(undefined)).toBe("-");
  });
  it("NaN/Infinity 返回 '-'", () => {
    expect(money(NaN)).toBe("-");
    expect(money(Infinity)).toBe("-");
  });
  it("正常值返回货币格式（默认 digits=0 四舍五入到整数）", () => {
    const result = money(1234.56);
    expect(result).toMatch(/1/);
    expect(result).toContain("1,235");
  });
  it("digits 参数控制小数位", () => {
    const result = money(1234.5678, 2);
    expect(result).toContain("1,234.57");
  });
});

describe("compactNumber", () => {
  it("null/undefined 返回 '-'", () => {
    expect(compactNumber(null)).toBe("-");
    expect(compactNumber(undefined)).toBe("-");
  });
  it("大数使用紧凑格式", () => {
    const result = compactNumber(1500000);
    expect(result.length).toBeLessThan(10);
  });
});

describe("formatDate / ageDays", () => {
  it("formatDate 空值返回 '-'", () => {
    expect(formatDate(null)).toBe("-");
    expect(formatDate(undefined)).toBe("-");
    expect(formatDate("")).toBe("-");
  });
  it("formatDate 有效值返回字符串", () => {
    const result = formatDate("2024-01-01T00:00:00Z");
    expect(result).toMatch(/2024/);
  });
  it("ageDays 空值返回 0", () => {
    expect(ageDays(null)).toBe(0);
    expect(ageDays(undefined)).toBe(0);
  });
  it("ageDays 计算天数差", () => {
    const old = new Date(Date.now() - 3 * 86400000).toISOString();
    expect(ageDays(old)).toBe(3);
  });
});

describe("discoveryFreshness", () => {
  it("is_frozen 返回 frozen 样式", () => {
    const result = discoveryFreshness({ is_frozen: true, created_at: new Date().toISOString() });
    expect(result.className).toBe("frozen");
  });
  it("天数 >= warning_days 返回 warning 样式", () => {
    const old = new Date(Date.now() - 5 * 86400000).toISOString();
    const result = discoveryFreshness({ created_at: old, warning_days: 3 });
    expect(result.className).toBe("warning");
  });
  it("天数 < warning_days 返回空样式", () => {
    const recent = new Date().toISOString();
    const result = discoveryFreshness({ created_at: recent, warning_days: 3 });
    expect(result.className).toBe("");
  });
  it("warning_days 默认为 3", () => {
    const old = new Date(Date.now() - 5 * 86400000).toISOString();
    const result = discoveryFreshness({ created_at: old });
    expect(result.className).toBe("warning");
  });
});

describe("样式类映射函数", () => {
  it("badgeClass: overheat/exit/reduce → danger", () => {
    expect(badgeClass("overheat")).toBe("badge danger");
    expect(badgeClass("exit")).toBe("badge danger");
    expect(badgeClass("reduce")).toBe("badge danger");
  });
  it("badgeClass: cooldown/hold → warn", () => {
    expect(badgeClass("cooldown")).toBe("badge warn");
    expect(badgeClass("hold")).toBe("badge warn");
  });
  it("badgeClass: 其他 → 默认 badge", () => {
    expect(badgeClass("buy")).toBe("badge");
    expect(badgeClass(null)).toBe("badge");
  });
  it("sideBadgeClass: sell → warn", () => {
    expect(sideBadgeClass("sell")).toBe("badge warn");
    expect(sideBadgeClass("buy")).toBe("badge");
  });
  it("pnlClass: 正负零", () => {
    expect(pnlClass(100)).toBe("pnl-positive");
    expect(pnlClass(-50)).toBe("pnl-negative");
    expect(pnlClass(0)).toBe("");
    expect(pnlClass(null)).toBe("");
  });
  it("sentimentClass: positive/negative/neutral", () => {
    expect(sentimentClass("positive")).toBe("sentiment-positive");
    expect(sentimentClass("negative")).toBe("sentiment-negative");
    expect(sentimentClass("neutral")).toBe("sentiment-neutral");
    expect(sentimentClass(null)).toBe("sentiment-neutral");
  });
  it("riskClass: high/medium/low", () => {
    expect(riskClass("high")).toBe("risk-high");
    expect(riskClass("medium")).toBe("risk-medium");
    expect(riskClass("low")).toBe("risk-low");
    expect(riskClass(null)).toBe("risk-low");
  });
});

describe("joinParts", () => {
  it("过滤 falsy 值并用 | 连接", () => {
    expect(joinParts(["a", "", "b", null, undefined, false, "c"])).toBe("a | b | c");
  });
  it("全部 falsy 返回空字符串", () => {
    expect(joinParts([null, undefined, "", false])).toBe("");
  });
});

describe("opportunityScoreValue / baseOpportunityScoreValue", () => {
  it("优先返回 final_opportunity_score", () => {
    const item = { final_opportunity_score: 80, priority_score: 70, timing_score: 60 };
    expect(opportunityScoreValue(item)).toBe(80);
  });
  it("降级链路：final → priority → timing → quality → 0", () => {
    expect(opportunityScoreValue({ priority_score: 70 })).toBe(70);
    expect(opportunityScoreValue({ timing_score: 60 })).toBe(60);
    expect(opportunityScoreValue({ quality_score: 50 })).toBe(50);
    expect(opportunityScoreValue({})).toBe(0);
  });
  it("baseOpportunityScoreValue 跳过 final_opportunity_score", () => {
    const item = { final_opportunity_score: 80, priority_score: 70 };
    expect(baseOpportunityScoreValue(item)).toBe(70);
  });
});

describe("withFinalOpportunityScore", () => {
  it("newsSnapshot 为 null 时降级（multiplier=1）", () => {
    const item = { priority_score: 70 };
    const result = withFinalOpportunityScore(item, null);
    expect(result.final_opportunity_score).toBe(70);
    expect(result.news_multiplier).toBe(1);
    expect(result.news_adjustment_pct).toBe(0);
  });
  it("news_adjustment_pct 限制在 [-0.12, 0.12]", () => {
    const item = { priority_score: 70 };
    // message_score=1000, confidence=1 → 1000*1/100=10 → clamp(10, -0.12, 0.12)=0.12
    const news = { symbols: [{ symbol_id: 1, message_score: 1000, confidence: 1 }] };
    const itemWithId = { ...item, symbol_id: 1 };
    const result = withFinalOpportunityScore(itemWithId, news);
    expect(result.news_adjustment_pct).toBe(0.12);
    expect(result.news_multiplier).toBe(1.12);
  });
  it("final_opportunity_score 限制在 [0, 100]", () => {
    // 极端正向：base=100, adjustment=0.12 → 112 → clamp(112, 0, 100)=100
    const item = { priority_score: 100, symbol_id: 1 };
    const news = { symbols: [{ symbol_id: 1, message_score: 1000, confidence: 1 }] };
    const result = withFinalOpportunityScore(item, news);
    expect(result.final_opportunity_score).toBe(100);
    // 极端负向：base=0, adjustment=-0.12 → 0 → clamp(0, 0, 100)=0
    const item2 = { priority_score: 0, symbol_id: 1 };
    const news2 = { symbols: [{ symbol_id: 1, message_score: -1000, confidence: 1 }] };
    const result2 = withFinalOpportunityScore(item2, news2);
    expect(result2.final_opportunity_score).toBe(0);
  });
  it("confidence 默认 0.35", () => {
    const item = { priority_score: 50, symbol_id: 1 };
    const news = { symbols: [{ symbol_id: 1, message_score: 100 }] };
    const result = withFinalOpportunityScore(item, news);
    // adjustment = (100 * 0.35) / 100 = 0.35 → clamp(0.35, -0.12, 0.12) = 0.12
    expect(result.news_confidence).toBe(0.35);
  });
  it("newsSnapshot 中无匹配 symbol 时降级", () => {
    const item = { priority_score: 50, symbol_id: 999 };
    const news = { symbols: [{ symbol_id: 1, message_score: 100, confidence: 1 }] };
    const result = withFinalOpportunityScore(item, news);
    expect(result.final_opportunity_score).toBe(50);
    expect(result.news_multiplier).toBe(1);
  });
});

describe("lotSizeForDetail / computeSuggestedPrice / computeSuggestedBuyQuantity / computeDefaultSellQuantity", () => {
  it("lotSizeForDetail: CN=100, US=1", () => {
    expect(lotSizeForDetail({ symbol: { region: "cn" } })).toBe(100);
    expect(lotSizeForDetail({ symbol: { region: "us" } })).toBe(1);
    expect(lotSizeForDetail({})).toBe(1);
  });
  it("computeSuggestedPrice: 优先 latest_trade_setup.entry_min", () => {
    const detail = {
      latest_trade_setup: { entry_min: 10.5 },
      bars: [{ close: 11 }],
      position: { latest_price: 12 },
    };
    expect(computeSuggestedPrice(detail as any)).toBe(10.5);
  });
  it("computeSuggestedPrice: 降级到 lastBar.close", () => {
    const detail = {
      latest_trade_setup: { entry_min: null },
      bars: [{ close: 11 }, { close: 12.3 }],
    };
    expect(computeSuggestedPrice(detail as any)).toBe(12.3);
  });
  it("computeSuggestedPrice: 再降级到 position.latest_price", () => {
    const detail = {
      latest_trade_setup: {},
      bars: [],
      position: { latest_price: 15.7 },
    };
    expect(computeSuggestedPrice(detail as any)).toBe(15.7);
  });
  it("computeSuggestedPrice: 全空返回 0", () => {
    expect(computeSuggestedPrice({} as any)).toBe(0);
  });
  it("computeSuggestedBuyQuantity: price<=0 或 budget<=0 返回 0", () => {
    const detail = { latest_trade_setup: { entry_min: 0 }, symbol: { region: "cn" } };
    expect(computeSuggestedBuyQuantity(detail as any)).toBe(0);
  });
  it("computeSuggestedBuyQuantity: CN 按 100 股取整", () => {
    const detail = {
      latest_trade_setup: { entry_min: 10, tranche_plan: [{ amount: 1500 }] },
      symbol: { region: "cn" },
    };
    // 1500 / 10 = 150 → floor(150/100)*100 = 100
    expect(computeSuggestedBuyQuantity(detail as any)).toBe(100);
  });
  it("computeSuggestedBuyQuantity: US 按 1 股取整", () => {
    const detail = {
      latest_trade_setup: { entry_min: 50, recommended_position_amount: 2500 },
      symbol: { region: "us" },
    };
    // 2500 / 50 = 50 → floor(50/1)*1 = 50
    expect(computeSuggestedBuyQuantity(detail as any)).toBe(50);
  });
  it("computeDefaultSellQuantity: 返回持仓数量（取整）", () => {
    expect(computeDefaultSellQuantity({ position: { quantity: 123.7 } } as any)).toBe(123);
    expect(computeDefaultSellQuantity({ position: { quantity: 0 } } as any)).toBe(0);
    expect(computeDefaultSellQuantity({} as any)).toBe(0);
  });
});

describe("parseTradeDate / weekKeyForDate / aggregateWeeklyBars", () => {
  it("parseTradeDate: 解析 YYYY-MM-DD", () => {
    const d = parseTradeDate("2024-01-15");
    expect(d.getUTCFullYear()).toBe(2024);
    expect(d.getUTCMonth()).toBe(0); // 0=January
    expect(d.getUTCDate()).toBe(15);
  });
  it("weekKeyForDate: 返回 YYYY-WW 格式", () => {
    // 2024-01-01 是周一，属于第 1 周
    expect(weekKeyForDate("2024-01-01")).toMatch(/^2024-\d{2}$/);
  });
  it("aggregateWeeklyBars: 同周聚合", () => {
    const bars = [
      { trade_date: "2024-01-01", open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
      { trade_date: "2024-01-02", open: 10.5, high: 12, low: 10, close: 11.5, volume: 200 },
      { trade_date: "2024-01-08", open: 11.5, high: 13, low: 11, close: 12.5, volume: 150 },
    ];
    const weeks = aggregateWeeklyBars(bars);
    // 01-01 和 01-02 同周，01-08 不同周
    expect(weeks.length).toBe(2);
    expect(weeks[0].high).toBe(12);
    expect(weeks[0].low).toBe(9);
    expect(weeks[0].close).toBe(11.5);
    expect(weeks[0].volume).toBe(300);
  });
  it("aggregateWeeklyBars: 空数组返回空数组", () => {
    expect(aggregateWeeklyBars([])).toEqual([]);
  });
});

describe("inferSymbolPayload", () => {
  it("6 位数字代码：以 6/5 开头 → sh 市场", () => {
    const result = inferSymbolPayload("600000");
    expect(result.symbol).toBe("600000");
    expect(result.market).toBe("sh");
    expect(result.asset_type).toBe("stock");
  });
  it("6 位数字代码：以 5 开头 → ETF + sh", () => {
    const result = inferSymbolPayload("510300");
    expect(result.market).toBe("sh");
    expect(result.asset_type).toBe("etf");
  });
  it("6 位数字代码：以 0/3 开头 → sz 市场", () => {
    const result = inferSymbolPayload("000001");
    expect(result.market).toBe("sz");
    expect(result.asset_type).toBe("stock");
  });
  it("6 位数字代码：以 15/16/18 开头 → ETF + sz", () => {
    const result = inferSymbolPayload("159915");
    expect(result.market).toBe("sz");
    expect(result.asset_type).toBe("etf");
  });
  it("字母代码：US ETF 集合 → etf", () => {
    const result = inferSymbolPayload("SPY");
    expect(result.market).toBe("us");
    expect(result.asset_type).toBe("etf");
  });
  it("字母代码：非 ETF → stock", () => {
    const result = inferSymbolPayload("AAPL");
    expect(result.market).toBe("us");
    expect(result.asset_type).toBe("stock");
  });
  it("空字符串抛错", () => {
    expect(() => inferSymbolPayload("")).toThrow();
    expect(() => inferSymbolPayload("   ")).toThrow();
  });
  it("非法格式抛错", () => {
    expect(() => inferSymbolPayload("abc123")).toThrow();
    expect(() => inferSymbolPayload("12345")).toThrow(); // 5 位数字
  });
});
