/* ════════════════════════════════════════════════════════════
   技术指标工具库
   供 InvestmentCenter / DetailModal 共用
   ════════════════════════════════════════════════════════════ */

// ─── 基础指标 ───

/** 简单移动平均线 Simple Moving Average */
export function computeMA(values: (number | null)[], period: number): (number | null)[] {
  const result: (number | null)[] = [];
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) { result.push(null); continue; }
    let sum = 0;
    let valid = true;
    for (let j = 0; j < period; j++) {
      const v = values[i - j];
      if (v == null || isNaN(v)) { result.push(null); valid = false; break; }
      sum += v;
    }
    if (!valid) continue;
    result.push(sum / period);
  }
  return result;
}

/** 指数移动平均线 Exponential Moving Average */
export function computeEMA(values: number[], period: number): (number | null)[] {
  const k = 2 / (period + 1);
  const result: (number | null)[] = [];
  let prevEma: number | null = null;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v == null || isNaN(v)) { result.push(null); continue; }
    if (prevEma == null) {
      // 第一个有效值用SMA初始化
      let sum = 0, count = 0;
      for (let j = 0; j <= i && j < period; j++) {
        const vv = values[i - j];
        if (vv != null && !isNaN(vv)) { sum += vv; count++; }
      }
      prevEma = count > 0 ? sum / count : v;
    } else {
      prevEma = v * k + prevEma * (1 - k);
    }
    result.push(prevEma);
  }
  return result;
}

// ─── MACD ───

export interface MACDResult {
  dif: (number | null)[];
  dea: (number | null)[];
  macdHist: (number | null)[];
}

/**
 * MACD (12, 26, 9)
 * DIF = EMA(12) - EMA(26)
 * DEA = EMA(DIF, 9)
 * MACD柱 = 2 * (DIF - DEA)
 */
export function computeMACD(closes: number[], fastPeriod = 12, slowPeriod = 26, signalPeriod = 9): MACDResult {
  const emaFast = computeEMA(closes, fastPeriod);
  const emaSlow = computeEMA(closes, slowPeriod);

  const dif: (number | null)[] = [];
  for (let i = 0; i < closes.length; i++) {
    const f = emaFast[i], s = emaSlow[i];
    if (f != null && s != null) dif.push(f - s);
    else dif.push(null);
  }

  // DEA 是 DIF 的 EMA
  const deaRaw: number[] = dif.filter((v): v is number => v != null);
  const deaFull = computeEMA(deaRaw, signalPeriod);

  // 将 DEA 映射回原始长度
  const dea: (number | null)[] = [];
  let deaIdx = 0;
  for (let i = 0; i < closes.length; i++) {
    if (dif[i] == null) { dea.push(null); continue; }
    dea.push(deaFull[deaIdx] ?? null);
    deaIdx++;
  }

  const macdHist: (number | null)[] = [];
  for (let i = 0; i < closes.length; i++) {
    const d = dif[i], e = dea[i];
    if (d != null && e != null) macdHist.push(2 * (d - e));
    else macdHist.push(null);
  }

  return { dif, dea, macdHist };
}

/** 检测MACD金叉/死叉信号点 */
export interface CrossSignal {
  index: number;
  type: "golden" | "dead"; // 金叉 | 死叉
  value: number;
}
export function detectMACDCross(dif: (number | null)[], dea: (number | null)[]): CrossSignal[] {
  const signals: CrossSignal[] = [];
  for (let i = 1; i < dif.length; i++) {
    const d0 = dif[i - 1], d1 = dif[i];
    const e0 = dea[i - 1], e1 = dea[i];
    if (d0 == null || d1 == null || e0 == null || e1 == null) continue;
    // 金叉: DIF从下往上穿过DEA
    if (d0 <= e0 && d1 > e1) signals.push({ index: i, type: "golden", value: d1 });
    // 死叉: DIF从上往下穿过DEA
    if (d0 >= e0 && d1 < e1) signals.push({ index: i, type: "dead", value: d1 });
  }
  return signals;
}

// ─── RSI ───

/**
 * RSI 相对强弱指数 (默认14周期)
 * RS = 平均涨幅 / 平均跌幅
 * RSI = 100 - 100 / (1 + RS)
 */
export function computeRSI(closes: number[], period = 14): (number | null)[] {
  const result: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length < period + 1) return result;

  let avgGain = 0, avgLoss = 0;

  // 第一段：简单平均
  for (let i = 1; i <= period; i++) {
    const change = closes[i] - closes[i - 1];
    if (change > 0) avgGain += change;
    else avgLoss -= change;
  }
  avgGain /= period;
  avgLoss /= period;
  result[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);

  // 后续：EMA平滑
  for (let i = period + 1; i < closes.length; i++) {
    const change = closes[i] - closes[i - 1];
    const gain = change > 0 ? change : 0;
    const loss = change < 0 ? -change : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    result[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }

  return result;
}

/** RSI超买超卖检测 */
export interface RSIExtremePoint {
  index: number;
  type: "overbought" | "oversold";
  value: number;
}
export function detectRSIExtreme(rsi: (number | null)[], overbought = 70, oversold = 30): RSIExtremePoint[] {
  const points: RSIExtremePoint[] = [];
  for (let i = 1; i < rsi.length; i++) {
    const prev = rsi[i - 1], curr = rsi[i];
    if (prev == null || curr == null) continue;
    // 从正常区进入超买
    if (prev < overbought && curr >= overbought) points.push({ index: i, type: "overbought", value: curr });
    // 从超卖回到正常区（脱离超卖）
    if (prev <= oversold && curr > oversold) points.push({ index: i, type: "oversold", value: curr });
  }
  return points;
}

// ─── BOLL 布林带 ───

export interface BOLLResult {
  upper: (number | null)[];
  mid: (number | null)[];   // MA(20)
  lower: (number | null)[];
}

/**
 * BOLLINGER BANDS (20, 2)
 * 中轨 = MA(20)
 * 上轨 = MA(20) + 2 * 标准差
 * 下轨 = MA(20) - 2 * 标准差
 */
export function computeBOLL(closes: number[], period = 20, multiplier = 2): BOLLResult {
  const mid = computeMA(closes.map((v) => v ?? null), period);
  const upper: (number | null)[] = [];
  const lower: (number | null)[] = [];

  for (let i = 0; i < closes.length; i++) {
    const m = mid[i];
    if (m == null) { upper.push(null); lower.push(null); continue; }

    // 计算标准差
    let sumSq = 0;
    let count = 0;
    for (let j = 0; j < period && i - j >= 0; j++) {
      const v = closes[i - j];
      if (v != null && !isNaN(v)) { sumSq += (v - m) ** 2; count++; }
    }
    const std = count > 0 ? Math.sqrt(sumSq / count) : 0;
    upper.push(m + multiplier * std);
    lower.push(m - multiplier * std);
  }

  return { upper, mid, lower };
}

/** 检测价格突破布林带上轨/下轨 */
export interface BreakoutSignal {
  index: number;
  type: "upper_break" | "lower_break";
  price: number;
  bandValue: number;
}
export function detectBollBreakout(
  highs: number[], lows: number[],
  boll: BOLLResult
): BreakoutSignal[] {
  const signals: BreakoutSignal[] = [];
  for (let i = 1; i < highs.length; i++) {
    const u = boll.upper[i], lo = boll.lower[i];
    const prevU = boll.upper[i - 1], prevLo = boll.lower[i - 1];
    if (u == null || lo == null || prevU == null || prevLo == null) continue;

    // 突破上轨
    if (highs[i] > u && highs[i - 1] <= prevU) {
      signals.push({ index: i, type: "upper_break", price: highs[i], bandValue: u });
    }
    // 跌破下轨
    if (lows[i] < lo && lows[i - 1] >= prevLo) {
      signals.push({ index: i, type: "lower_break", price: lows[i], bandValue: lo });
    }
  }
  return signals;
}

// ─── ATR 平均真实波幅 ───

/**
 * ATR Average True Range (默认14周期)
 * 用于止损距离参考、波动率衡量
 */
export function computeATR(
  bars: { high: number; low: number; close: number }[],
  period = 14
): (number | null)[] {
  const result: (number | null)[] = new Array(bars.length).fill(null);
  if (bars.length < 2) return result;

  // TR = max(H-L, |H-PreC|, |L-PreC|)
  const trValues: number[] = [bars[0].high - bars[0].low];
  for (let i = 1; i < bars.length; i++) {
    const hl = bars[i].high - bars[i].low;
    const hc = Math.abs(bars[i].high - bars[i - 1].close);
    const lc = Math.abs(bars[i].low - bars[i - 1].close);
    trValues.push(Math.max(hl, hc, lc));
  }

  // ATR = SMA(TR, period)
  let atr = 0;
  for (let i = 0; i < period && i < trValues.length; i++) atr += trValues[i];
  atr /= Math.min(period, trValues.length);
  result[period - 1] = atr;

  for (let i = period; i < trValues.length; i++) {
    atr = (atr * (period - 1) + trValues[i]) / period;
    result[i] = atr;
  }

  return result;
}

// ─── 统计工具 ───

/** 计算最大回撤 */
export function computeMaxDrawdown(equityCurve: number[]): { maxDrawdown: number; maxDrawdownPct: number; peakIndex: number; troughIndex: number } {
  let peak = equityCurve[0] ?? 0, maxDD = 0, maxDDPct = 0;
  let peakIdx = 0, troughIdx = 0;
  for (let i = 1; i < equityCurve.length; i++) {
    const v = equityCurve[i];
    if (v > peak) { peak = v; peakIdx = i; }
    const dd = peak - v;
    const ddp = peak > 0 ? dd / peak : 0;
    if (dd > maxDD) { maxDD = dd; maxDDPct = ddp; troughIdx = i; }
  }
  return { maxDrawdown: maxDD, maxDrawdownPct: maxDDPct, peakIndex: peakIdx, troughIndex: troughIdx };
}

/** 计算夏普比率 (年化, 默认无风险利率3%) */
export function computeSharpeRatio(returns: number[], riskFreeRate = 0.03): number {
  if (returns.length === 0) return 0;
  const mean = returns.reduce((s, r) => s + r, 0) / returns.length;
  const variance = returns.reduce((s, r) => s + (r - mean) ** 2, 0) / returns.length;
  const std = Math.sqrt(variance);
  if (std === 0) return 0;
  // 年化: 日收益 * 252
  const annualizedMean = mean * 252;
  const annualizedStd = std * Math.sqrt(252);
  return (annualizedMean - riskFreeRate) / annualizedStd;
}

/** 计算胜率 */
export function computeWinRate(pnls: number[]): { winRate: number; winCount: number; lossCount: number; avgWin: number; avgLoss: number; profitFactor: number } {
  const wins = pnls.filter((p) => p > 0);
  const losses = pnls.filter((p) => p < 0);
  const winCount = wins.length;
  const lossCount = losses.length;
  const total = winCount + lossCount;
  const winRate = total > 0 ? winCount / total : 0;
  const avgWin = wins.length > 0 ? wins.reduce((s, w) => s + w, 0) / wins.length : 0;
  const avgLoss = losses.length > 0 ? Math.abs(losses.reduce((s, l) => s + l, 0) / losses.length) : 0;
  const profitFactor = avgLoss > 0 ? (avgWin * winCount) / (avgLoss * lossCount) : Infinity;
  return { winRate, winCount, lossCount, avgWin, avgLoss, profitFactor };
}

/** 成交量格式化 */
export function formatVolume(val: number): string {
  if (val >= 1e8) return `${(val / 1e8).toFixed(2)}亿`;
  if (val >= 1e4) return `${(val / 1e4).toFixed(2)}万`;
  return String(val);
}
