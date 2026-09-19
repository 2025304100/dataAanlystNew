import { describe, it, expect } from "vitest";
import {
  computeMA,
  computeEMA,
  computeMACD,
  detectMACDCross,
  computeKDJ,
  computeRSI,
  detectRSIExtreme,
  computeBOLL,
  detectBollBreakout,
  computeATR,
  computeMaxDrawdown,
  computeSharpeRatio,
  computeWinRate,
  formatVolume,
  formatVolumeI18n,
  computeRelativeStrength,
} from "../indicators";

describe("computeMA", () => {
  it("正常计算移动平均", () => {
    const values = [1, 2, 3, 4, 5];
    const ma = computeMA(values, 3);
    // index 0,1 为 null；index 2 = (1+2+3)/3=2
    expect(ma[0]).toBeNull();
    expect(ma[1]).toBeNull();
    expect(ma[2]).toBe(2);
    expect(ma[3]).toBe(3);
    expect(ma[4]).toBe(4);
  });
  it("空数组返回空数组", () => {
    expect(computeMA([], 3)).toEqual([]);
  });
  it("包含 null 的位置返回 null", () => {
    const ma = computeMA([1, null, 3, 4, 5], 2);
    // index 1: values[1]=null → null
    expect(ma[1]).toBeNull();
  });
  it("period 大于数组长度时全 null", () => {
    const ma = computeMA([1, 2], 5);
    expect(ma.every((v) => v === null)).toBe(true);
  });
});

describe("computeEMA", () => {
  it("返回与输入等长", () => {
    const result = computeEMA([1, 2, 3, 4, 5], 3);
    expect(result.length).toBe(5);
  });
  it("NaN 值返回 null", () => {
    const result = computeEMA([1, NaN, 3], 2);
    expect(result[1]).toBeNull();
  });
  it("第一个有效值用 SMA 初始化（仅 1 个值可用）", () => {
    const result = computeEMA([10, 20, 30], 3);
    // i=0 时只有 values[0] 可用，SMA = 10
    expect(result[0]).toBe(10);
  });
});

describe("computeMACD", () => {
  it("返回 dif/dea/macdHist 三个等长数组", () => {
    const closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40];
    const result = computeMACD(closes);
    expect(result.dif.length).toBe(closes.length);
    expect(result.dea.length).toBe(closes.length);
    expect(result.macdHist.length).toBe(closes.length);
  });
  it("空数组返回空数组", () => {
    const result = computeMACD([]);
    expect(result.dif).toEqual([]);
    expect(result.dea).toEqual([]);
    expect(result.macdHist).toEqual([]);
  });
});

describe("detectMACDCross", () => {
  it("检测金叉", () => {
    // dif 从下往上穿过 dea
    const dif = [1, 1, 1, 2];
    const dea = [2, 2, 2, 1];
    const signals = detectMACDCross(dif, dea);
    expect(signals.some((s) => s.type === "golden")).toBe(true);
  });
  it("检测死叉", () => {
    const dif = [2, 2, 2, 1];
    const dea = [1, 1, 1, 2];
    const signals = detectMACDCross(dif, dea);
    expect(signals.some((s) => s.type === "dead")).toBe(true);
  });
  it("无交叉返回空数组", () => {
    const dif = [1, 1, 1, 1];
    const dea = [2, 2, 2, 2];
    expect(detectMACDCross(dif, dea)).toEqual([]);
  });
});

describe("computeKDJ", () => {
  it("返回 k/d/j 三个等长数组", () => {
    const highs = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19];
    const lows = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18];
    const closes = [9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5];
    const result = computeKDJ(highs, lows, closes, 9, 3, 3);
    expect(result.k.length).toBe(10);
    expect(result.d.length).toBe(10);
    expect(result.j.length).toBe(10);
  });
  it("前 n-1 个为 null", () => {
    const highs = Array(10).fill(10);
    const lows = Array(10).fill(9);
    const closes = Array(10).fill(9.5);
    const result = computeKDJ(highs, lows, closes, 9, 3, 3);
    expect(result.k[0]).toBeNull();
    expect(result.k[7]).toBeNull();
  });
});

describe("computeRSI", () => {
  it("长度不足返回全 null", () => {
    const result = computeRSI([1, 2, 3], 14);
    expect(result.every((v) => v === null)).toBe(true);
  });
  it("正常计算 RSI 在 [0, 100] 范围", () => {
    const closes = Array.from({ length: 30 }, (_, i) => 100 + i);
    const result = computeRSI(closes, 14);
    const valid = result.filter((v): v is number => v != null);
    expect(valid.length).toBeGreaterThan(0);
    valid.forEach((v) => {
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThanOrEqual(100);
    });
  });
  it("持续上涨 RSI 接近 100", () => {
    const closes = Array.from({ length: 20 }, (_, i) => 100 + i);
    const result = computeRSI(closes, 14);
    const last = result[result.length - 1];
    expect(last).not.toBeNull();
    expect(last as number).toBeGreaterThan(90);
  });
});

describe("detectRSIExtreme", () => {
  it("检测超买（上穿 70）", () => {
    const rsi = [50, 60, 69, 75];
    const points = detectRSIExtreme(rsi, 70, 30);
    expect(points.some((p) => p.type === "overbought")).toBe(true);
  });
  it("检测超卖（上穿 30）", () => {
    const rsi = [25, 28, 30, 35];
    const points = detectRSIExtreme(rsi, 70, 30);
    expect(points.some((p) => p.type === "oversold")).toBe(true);
  });
});

describe("computeBOLL", () => {
  it("返回 upper/mid/lower 三个等长数组", () => {
    const closes = Array.from({ length: 25 }, (_, i) => 100 + i);
    const result = computeBOLL(closes, 20, 2);
    expect(result.upper.length).toBe(25);
    expect(result.mid.length).toBe(25);
    expect(result.lower.length).toBe(25);
  });
  it("前 period-1 个 mid 为 null", () => {
    const closes = Array.from({ length: 25 }, (_, i) => 100 + i);
    const result = computeBOLL(closes, 20, 2);
    expect(result.mid[0]).toBeNull();
    expect(result.mid[18]).toBeNull();
  });
  it("upper >= mid >= lower", () => {
    const closes = Array.from({ length: 25 }, (_, i) => 100 + Math.sin(i) * 5);
    const result = computeBOLL(closes, 20, 2);
    for (let i = 19; i < closes.length; i++) {
      const u = result.upper[i], m = result.mid[i], l = result.lower[i];
      if (u != null && m != null && l != null) {
        expect(u).toBeGreaterThanOrEqual(m);
        expect(m).toBeGreaterThanOrEqual(l);
      }
    }
  });
});

describe("computeATR", () => {
  it("返回等长数组", () => {
    const bars = Array.from({ length: 20 }, (_, i) => ({ high: 110 + i, low: 90 + i, close: 100 + i }));
    const result = computeATR(bars, 14);
    expect(result.length).toBe(20);
  });
  it("长度 < 2 返回全 null", () => {
    const result = computeATR([{ high: 10, low: 9, close: 9.5 }], 14);
    expect(result.every((v) => v === null)).toBe(true);
  });
});

describe("computeMaxDrawdown", () => {
  it("计算最大回撤", () => {
    const equity = [100, 110, 105, 95, 100];
    const result = computeMaxDrawdown(equity);
    expect(result.maxDrawdown).toBe(15); // 110 → 95
    expect(result.maxDrawdownPct).toBeCloseTo(15 / 110, 5);
  });
  it("单调递增无回撤", () => {
    const equity = [100, 110, 120, 130];
    const result = computeMaxDrawdown(equity);
    expect(result.maxDrawdown).toBe(0);
  });
});

describe("computeSharpeRatio", () => {
  it("空数组返回 0", () => {
    expect(computeSharpeRatio([])).toBe(0);
  });
  it("全零收益返回 0（std=0）", () => {
    expect(computeSharpeRatio([0, 0, 0, 0])).toBe(0);
  });
  it("正常计算返回数值", () => {
    const returns = [0.01, -0.02, 0.03, 0.01, -0.01, 0.02];
    const result = computeSharpeRatio(returns);
    expect(typeof result).toBe("number");
    expect(Number.isFinite(result)).toBe(true);
  });
});

describe("computeWinRate", () => {
  it("空数组：winRate=0, profitFactor=null（不出现 Infinity）", () => {
    const result = computeWinRate([]);
    expect(result.winRate).toBe(0);
    expect(result.profitFactor).toBeNull();
  });
  it("全胜无亏损：profitFactor=null（不出现 Infinity）", () => {
    const result = computeWinRate([100, 200, 50]);
    expect(result.winCount).toBe(3);
    expect(result.lossCount).toBe(0);
    expect(result.winRate).toBe(1);
    expect(result.profitFactor).toBeNull();
  });
  it("正常计算胜率与盈亏比", () => {
    const result = computeWinRate([100, -50, 200, -100]);
    expect(result.winCount).toBe(2);
    expect(result.lossCount).toBe(2);
    expect(result.winRate).toBe(0.5);
    expect(result.avgWin).toBe(150);
    expect(result.avgLoss).toBe(75);
    expect(result.profitFactor).toBe((150 * 2) / (75 * 2));
  });
});

describe("formatVolume / formatVolumeI18n", () => {
  it("formatVolume: >= 1e8 用亿", () => {
    expect(formatVolume(1.5e8)).toBe("1.50亿");
  });
  it("formatVolume: >= 1e4 用万", () => {
    expect(formatVolume(15000)).toBe("1.50万");
  });
  it("formatVolume: < 1e4 原值", () => {
    expect(formatVolume(9999)).toBe("9999");
  });
  it("formatVolumeI18n 使用传入的单位", () => {
    expect(formatVolumeI18n(1.5e8, "Yi", "Wan")).toBe("1.50Yi");
    expect(formatVolumeI18n(15000, "Yi", "Wan")).toBe("1.50Wan");
  });
});

describe("computeRelativeStrength", () => {
  it("返回等长数组", () => {
    const prices = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29];
    const benchmark = Array(20).fill(10);
    const result = computeRelativeStrength(prices, benchmark, 5);
    expect(result.length).toBe(20);
  });
  it("benchmark 为 0 时该位置为 null", () => {
    const result = computeRelativeStrength([10, 11], [0, 10], 1);
    expect(result[0]).toBeNull();
  });
});
