export type BacktestDraft = {
  mode: "portfolio" | "single";
  portStart: string; portEnd: string; portRangePreset: "1y" | "3y" | "5y" | null;
  portCapital: string; portBenchmark: string; portCommission: string;
  singleSymbol: string; singleStart: string; singleEnd: string; singleRangePreset: "1y" | "3y" | "5y" | null;
  singleIndicator: string; singleCapital: string; singleCommission: string;
};

const draftKey = (portfolioId: number) => `backtest-parameters:${portfolioId}`;

export function readBacktestDraft(portfolioId: number, defaults: BacktestDraft): BacktestDraft {
  try {
    const stored = JSON.parse(localStorage.getItem(draftKey(portfolioId)) || "null");
    if (stored?.version !== 1 || stored.portfolioId !== portfolioId || !stored.parameters) return defaults;
    const parameters = stored.parameters;
    const values = { ...defaults };
    for (const key of Object.keys(defaults) as Array<keyof BacktestDraft>) {
      const value = parameters[key];
      if (key === "mode") values.mode = value === "single" ? "single" : "portfolio";
      else if (key === "portRangePreset" || key === "singleRangePreset") {
        if (value === null || ["1y", "3y", "5y"].includes(value)) values[key] = value;
      } else if (typeof value === "string" && value.length <= 200) values[key] = value;
    }
    return values;
  } catch { return defaults; }
}

export function saveBacktestDraft(portfolioId: number, parameters: BacktestDraft): boolean {
  try {
    localStorage.setItem(draftKey(portfolioId), JSON.stringify({ version: 1, portfolioId, parameters }));
    return true;
  } catch { return false; }
}

export type BacktestMissingType = "bars" | "scores" | "status" | "price_limits" | "symbols";
export const backtestMissingLabels: Record<BacktestMissingType, string> = {
  bars: "历史行情", scores: "历史评分", status: "每日停牌、ST 和上市退市状态", price_limits: "逐日涨跌停资料", symbols: "可回测标的和行情评分资料",
};
export type BacktestRepairContext = {
  version: 1; portfolioId: number; portfolioName: string; mode: "portfolio" | "single";
  symbols: Array<{ id: number; symbol: string; name?: string }>;
  startDate: string; endDate: string; missingTypes: BacktestMissingType[];
  target: "history" | "coverage" | "market";
};
const repairKey = "backtest-repair-return";
const isDate = (value: unknown): value is string => typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && !Number.isNaN(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;

export function readBacktestRepair(): BacktestRepairContext | null {
  try {
    const value = JSON.parse(localStorage.getItem(repairKey) || "null");
    if (value?.version !== 1 || !Number.isSafeInteger(value.portfolioId) || value.portfolioId < 1
      || typeof value.portfolioName !== "string" || !["portfolio", "single"].includes(value.mode)
      || !["history", "coverage", "market"].includes(value.target)
      || !isDate(value.startDate) || !isDate(value.endDate) || value.startDate > value.endDate
      || !Array.isArray(value.missingTypes) || value.missingTypes.length === 0
      || !value.missingTypes.every((key: string) => Object.prototype.hasOwnProperty.call(backtestMissingLabels, key))
      || !Array.isArray(value.symbols) || !value.symbols.every((symbol: any) => Number.isSafeInteger(symbol?.id) && symbol.id > 0 && typeof symbol.symbol === "string" && !!symbol.symbol.trim())
      || (value.mode === "single" && value.symbols.length !== 1)) return null;
    return value;
  } catch { return null; }
}

export function saveBacktestRepair(context: BacktestRepairContext): boolean {
  try { localStorage.setItem(repairKey, JSON.stringify(context)); return true; } catch { return false; }
}
export function clearBacktestRepair(): void {
  try { localStorage.removeItem(repairKey); } catch { /* The form draft is kept independently. */ }
}
