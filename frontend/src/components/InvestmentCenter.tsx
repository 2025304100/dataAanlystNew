import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import ReactECharts from "echarts-for-react";
import { Button, Input, Progress } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import { useApp } from "../context/AppContext";
import {
  t,
  template,
  DOT,
  regionLongLabel,
  assetTypeLabel,
  stageLabel,
  actionLabel,
  futureBuyLabel,
  futurePriorityLabel,
  futureTriggerLabel,
  sideLabel,
  trancheLabel,
  trancheTrigger,
} from "../i18n";
import {
  percent,
  score,
  money,
  joinParts,
  badgeClass,
  pnlClass,
  clamp,
  aggregateWeeklyBars,
  computeSuggestedPrice,
  signalLabel,
} from "../utils/format";
import type { FutureBuyPlan, TradeSetup } from "../types";
import { api } from "../api/client";

// ─── 共享工具库导入 ───
import {
  computeMA,
  formatVolume,
  computeMACD,
  detectMACDCross,
  computeRSI,
  detectRSIExtreme,
  computeBOLL,
  computeATR,
  type MACDResult,
  type CrossSignal,
  type RSIExtremePoint,
  type BOLLResult,
} from "../utils/indicators";
import {
  planWithRatio,
  invalidFuturePlan,
  buildLongFuturePlan,
  buildCustomFuturePlan,
  getActiveFutureBuyPlan,
} from "../utils/trade-plan";

interface InvestmentCenterProps {
  openMetricModal: (type: string) => void;
}

/* ════════════════════════════════════════════════════════════
   Future Buy Plan SVG Overlay（与 DetailModal 一致）
   ════════════════════════════════════════════════════════════ */
function FuturePlanOverlay({
  plans,
  lastClose,
  priceMin,
  priceMax,
  height,
}: {
  plans: FutureBuyPlan[];
  lastClose: number;
  priceMin: number;
  priceMax: number;
  height: number;
}) {
  const panelWidth = 168;
  const padding = 8;
  const bandPadding = 6;
  const plotTop = 30;
  const plotBottom = 50;
  const legendHeight = 28;
  const plotHeight = height - plotTop - plotBottom - legendHeight;
  const priceSpan = Math.max(priceMax - priceMin, 0.01);

  const priceToY = (price: number) => {
    const ratio = (priceMax - price) / priceSpan;
    return plotTop + ratio * plotHeight;
  };

  const styleMap: Record<string, { fill: string; stroke: string; dash: string }> = {
    avoid: { fill: "rgba(180, 35, 24, 0.12)", stroke: "#b42318", dash: "6,4" },
    high: { fill: "rgba(15, 118, 110, 0.16)", stroke: "#0f766e", dash: "" },
    low: { fill: "rgba(37, 99, 235, 0.11)", stroke: "#2563eb", dash: "4,4" },
    normal: { fill: "rgba(15, 118, 110, 0.10)", stroke: "#0f766e", dash: "4,4" },
  };

  const usablePlans = plans.filter((plan) => plan.zone_min !== null && plan.zone_max !== null);
  const nonAvoidPlans = usablePlans.filter((p) => p.priority !== "avoid").slice(0, 3);

  const svgWidth = panelWidth + padding * 2;

  return (
    <svg
      style={{
        position: "absolute", right: 0, top: 0,
        width: svgWidth, height, pointerEvents: "none", overflow: "visible",
      }}
      viewBox={`0 0 ${svgWidth} ${height}`}
    >
      <rect x={padding} y={plotTop} width={panelWidth - padding} height={plotHeight}
        rx={8} ry={8} fill="rgba(31,41,51,0.035)" stroke="rgba(31,41,51,0.10)" strokeWidth={1} strokeDasharray="4,4" />
      <text x={padding + 6} y={plotTop + 18} fontSize={11} fill="#6b7280">{t("futureBuyPlan")}</text>

      {usablePlans.map((plan) => {
        const style = styleMap[plan.priority] ?? styleMap.normal;
        const yMin = priceToY(Math.min(plan.zone_min!, plan.zone_max!));
        const yMax = priceToY(Math.max(plan.zone_min!, plan.zone_max!));
        const bandH = Math.max(Math.abs(yMax - yMin), 12);
        const bandY = Math.min(yMin, yMax);
        return (
          <g key={plan.label}>
            <rect x={padding + bandPadding} y={bandY}
              width={panelWidth - padding * 2 - bandPadding} height={bandH}
              rx={6} ry={6} fill={style.fill} stroke={style.stroke}
              strokeWidth={1} strokeDasharray={style.dash || undefined} />
            <text x={padding + panelWidth - padding - 2} y={bandY + bandH / 2}
              fontSize={11} fill={style.stroke} textAnchor="end" dominantBaseline="middle">
              {futureBuyLabel(plan.label)}
            </text>
            <text x={padding + bandPadding + 3} y={bandY + bandH / 2}
              fontSize={10} fill={style.stroke} dominantBaseline="middle">
              {score(plan.zone_min)}-{score(plan.zone_max)}
            </text>
          </g>
        );
      })}

      {/* Dashed curve */}
      {nonAvoidPlans.length > 0 && (() => {
        const startX = padding + 2;
        const startY = priceToY(lastClose);
        const curvePoints = nonAvoidPlans.map((plan) => ({
          x: padding + panelWidth - padding - 20,
          y: priceToY((Number(plan.zone_min) + Number(plan.zone_max)) / 2),
        }));
        const pathParts = [`M ${startX} ${startY}`];
        curvePoints.forEach((pt) => pathParts.push(`L ${pt.x} ${pt.y}`));
        return (
          <>
            <path d={pathParts.join(" ")} fill="none" stroke="#111827"
              strokeWidth={1.4} strokeDasharray="5,5" opacity={0.75} />
            <circle cx={startX} cy={startY} r={3.5} fill="#111827" />
            {curvePoints.map((pt, i) => (<circle key={i} cx={pt.x} cy={pt.y} r={3.5} fill="#111827" />))}
          </>
        );
      })()}
    </svg>
  );
}

/* ════════════════════════════════════════════════════════════
   InvestmentCenter 主组件
   ════════════════════════════════════════════════════════════ */

export default function InvestmentCenter({ openMetricModal }: InvestmentCenterProps) {
  const ctx = useApp();
  const workbench = ctx.workbench;
  const detail = ctx.detail;

  // 搜索状态
  const [isMobile, setIsMobile] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // K线本地缓存：懒加载更多数据
  const [extraBarsCache, setExtraBarsCache] = useState<Record<number, any[]>>({});
  const loadingBarsRef = useRef<Set<number>>(new Set());
  const windowInitializedRef = useRef(false);

  // 指标面板开关
  const [showMACD, setShowMACD] = useState(true);
  const [showRSI, setShowRSI] = useState(true);

  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 1024);
    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, []);

  // 投资中心默认显示180根K线（只执行一次）
  useEffect(() => {
    if (ctx.detail && ctx.activeTab === "investment" && !windowInitializedRef.current) {
      windowInitializedRef.current = true;
      ctx.setChartWindowSize(180);
    }
  }, [ctx.detail, ctx.activeTab, ctx.setChartWindowSize]); // eslint-disable-line

  // ── 懒加载K线数据 ──
  useEffect(() => {
    const symbolId = ctx.activeSymbolId;
    if (!symbolId || !detail?.bars?.length) return;
    const neededBars = ctx.chartWindowSize;
    const existingBars = detail.bars.length;
    const extraBars = extraBarsCache[symbolId] ?? [];
    if (neededBars <= existingBars + extraBars.length) return;
    if (loadingBarsRef.current.has(symbolId)) return;
    loadingBarsRef.current.add(symbolId);

    api.getBars(symbolId, Math.max(neededBars + 60, 250))
      .then((fetched: any[]) => {
        if (!fetched || !fetched.length) return;
        const existingDates = new Set(detail.bars.map((b: any) => b.trade_date));
        const newBars = fetched.filter((b: any) => !existingDates.has(b.trade_date));
        if (newBars.length > 0) {
          setExtraBarsCache((prev) => ({ ...prev, [symbolId]: [...newBars, ...(prev[symbolId] ?? [])] }));
        }
      })
      .catch(() => {})
      .finally(() => { loadingBarsRef.current.delete(symbolId); });
  }, [ctx.activeSymbolId, ctx.chartWindowSize, detail?.bars?.length, extraBarsCache]); // eslint-disable-line

  // 搜索逻辑
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    if (!searchQuery.trim()) { setSearchResults([]); return; }
    setSearching(true);
    searchTimerRef.current = setTimeout(async () => {
      try {
        const results = await api.getSymbols(searchQuery.trim());
        setSearchResults(results.slice(0, 20));
      } catch { setSearchResults([]); }
      finally { setSearching(false); }
    }, 300);
    return () => { if (searchTimerRef.current) clearTimeout(searchTimerRef.current); };
  }, [searchQuery]);

  // 快捷标的（持仓 + 最新评分）
  const quickSymbols = useMemo(() => {
    if (!workbench) return [];
    const map = new Map<number, any>();
    for (const p of workbench.positions)
      map.set(p.symbol_id, { symbol_id: p.symbol_id, symbol: p.symbol, name: p.name });
    for (const s of workbench.latest_scores) {
      if (!map.has(s.symbol_id))
        map.set(s.symbol_id, { symbol_id: s.symbol_id, symbol: s.symbol, name: s.name });
    }
    return Array.from(map.values());
  }, [workbench]);

  const handleSelectSymbol = useCallback((symbolId: number) => {
    ctx.loadSymbolDetail(symbolId, { focus: true, barLimit: 180 });
    setSearchQuery("");
    setSearchResults([]);
  }, [ctx]);

  // 自动加载第一个快捷标的
  useEffect(() => {
    if (quickSymbols.length > 0 && !ctx.activeSymbolId && !ctx.detail) {
      ctx.loadSymbolDetail(quickSymbols[0].symbol_id, { focus: true, barLimit: 180 });
    }
  }, [quickSymbols.length]); // eslint-disable-line

  /* ════════════════════════════════════════════════════
     数据计算层
     ════════════════════════════════════════════════════ */

  const setup = detail?.latest_trade_setup ?? null;

  // Entry price & quantity
  const baseScenarios = setup?.return_scenarios;
  const _refPrice = baseScenarios?.reference_price ?? computeSuggestedPrice(detail);
  const entryPrice = Number(ctx.simPrice) > 0 ? Number(ctx.simPrice) : Number(_refPrice);
  const quantity = Number(ctx.simQuantity) > 0
    ? Number(ctx.simQuantity)
    : Number(baseScenarios?.planned_order?.quantity ?? 0);

  // Order scenario preview（与 DetailModal 一致）
  const scenarios = useMemo(() => {
    if (!baseScenarios) return null;
    const refPrice = baseScenarios.reference_price ?? entryPrice;
    if (!(refPrice > 0)) return baseScenarios;

    const baseRiskUnit = Math.max(refPrice * 0.015, 0.01);
    const baseStop = setup?.stop_loss;
    const riskFromStop = baseStop != null && baseStop < refPrice ? refPrice - baseStop : baseRiskUnit;
    const effectiveRisk = Math.max(baseRiskUnit, riskFromStop);

    const scenarioConfig: Record<string, { horizonMult: number; targetMult: number; stopMult: number; confidenceAdj: number }> = {
      general: { horizonMult: 1.0, targetMult: 1.0, stopMult: 1.0, confidenceAdj: 0 },
      short:   { horizonMult: 0.25, targetMult: 0.50, stopMult: 0.65, confidenceAdj: -8 },
      mid:     { horizonMult: 0.75, targetMult: 0.82, stopMult: 0.88, confidenceAdj: -3 },
      long:    { horizonMult: 1.5,  targetMult: 1.30, stopMult: 1.20, confidenceAdj: 5 },
      custom:  { horizonMult: 1.0, targetMult: 1.0,  stopMult: 1.0,  confidenceAdj: 0 },
    };
    const cfg = scenarioConfig[ctx.futurePlanScenario] ?? scenarioConfig.general;

    const adjHorizon = Math.max(3, Math.round((baseScenarios.horizon_days ?? 20) * cfg.horizonMult));
    const adjRisk = effectiveRisk * cfg.targetMult;
    const adjStopOffset = effectiveRisk * cfg.stopMult;
    const adjStop = _round_price(refPrice - adjStopOffset);
    const adjTarget = _round_price(refPrice + adjRisk);
    const adjConfidence = _clamp((baseScenarios.confidence_pct ?? 60) + cfg.confidenceAdj, 35, 82) / 100;
    const pessimisticPrice = _round_price(adjStop);
    const optimisticAnchor = adjTarget + Math.max(effectiveRisk * 0.4, refPrice * 0.02);
    const optimisticPrice = _round_price(optimisticAnchor);
    const expectedPrice = _round_price(adjTarget * adjConfidence + pessimisticPrice * (1 - adjConfidence));

    function _round_price(v: number | undefined | null): number {
      return v != null ? Math.round(v * 100) / 100 : 0;
    }
    function _clamp(v: number, lo: number, hi: number): number {
      return Math.max(lo, Math.min(hi, v));
    }

    return {
      ...baseScenarios,
      horizon_days: adjHorizon,
      confidence_pct: Math.round(adjConfidence * 100),
      expected: { ...baseScenarios.expected, exit_price: expectedPrice },
      optimistic: { ...baseScenarios.optimistic, exit_price: optimisticPrice },
      pessimistic: { ...baseScenarios.pessimistic, exit_price: pessimisticPrice },
      _adjStop: adjStop,
      _adjTarget: adjTarget,
    };
  }, [baseScenarios, setup?.stop_loss, entryPrice, ctx.futurePlanScenario]);

  // ── Chart data memo（含全部技术指标） ──
  interface ChartDataExt {
    bars: any[]; dates: string[]; closes: number[];
    candlestick: any[]; volume: any[];
    ma10: (number | null)[]; ma20: (number | null)[];
    macd: MACDResult | null;
    rsi: (number | null)[];
    boll: BOLLResult | null;
    atr: (number | null)[];
    macdSignals: CrossSignal[];
    rsiSignals: RSIExtremePoint[];
  }

  const chartData = useMemo<ChartDataExt | null>(() => {
    if (!detail?.bars?.length) return null;
    const extra = extraBarsCache[ctx.activeSymbolId ?? 0] ?? [];
    const mergedBars = [...detail.bars, ...extra];
    const allBars =
      ctx.chartTimeframe === "weekly" ? aggregateWeeklyBars(mergedBars) : mergedBars;
    const windowSize = ctx.chartWindowSize;
    const bars = allBars.slice(Math.max(0, allBars.length - windowSize));
    const dates = bars.map((b) => b.trade_date);
    const closes = bars.map((b) => b.close);
    const candlestick = bars.map((b) => [b.open, b.close, b.low, b.high]);
    const volume = bars.map((b) => ({
      value: b.volume,
      itemStyle: { color: b.close >= b.open ? "rgba(15, 118, 110, 0.5)" : "rgba(180, 35, 24, 0.5)" },
    }));

    // 基础均线
    const ma10 = computeMA(closes, 10);
    const ma20 = computeMA(closes, 20);

    // 技术指标
    const macd = computeMACD(closes);
    const rsi = computeRSI(closes);
    const boll = computeBOLL(closes);
    const atr = computeATR(bars.map((b) => ({ high: b.high, low: b.low, close: b.close })));

    // 信号检测
    const macdSignals = detectMACDCross(macd.dif, macd.dea);
    const rsiSignals = detectRSIExtreme(rsi);

    return { bars, dates, closes, candlestick, volume, ma10, ma20, macd, rsi, boll, atr, macdSignals, rsiSignals };
  }, [detail?.bars, extraBarsCache, ctx.activeSymbolId, ctx.chartTimeframe, ctx.chartWindowSize]);

  // Active future buy plan
  const activeFutureBuyPlan = useMemo(() => {
    const s = detail?.latest_trade_setup ?? null;
    if (!s) return [];
    const totalCapital = ctx.workbench?.portfolio?.total_capital ?? 0;
    const investableRatio = ctx.workbench?.portfolio?.investable_ratio ?? 1;
    return getActiveFutureBuyPlan(s, ctx.futurePlanScenario, ctx.futurePlanCustom, totalCapital, investableRatio);
  }, [detail?.latest_trade_setup, ctx.futurePlanScenario, ctx.futurePlanCustom, ctx.workbench?.portfolio]);

  // ── 风险指标计算 ──
  const riskMetrics = useMemo(() => {
    if (!setup || !(entryPrice > 0)) return null;
    const stop = (scenarios as any)?._adjStop ?? setup.stop_loss;
    const target = (scenarios as any)?._adjTarget ?? setup.target_price;
    const currentPrice = detail?.bars?.[detail.bars.length - 1]?.close ?? entryPrice;

    // 盈亏比 = (目标价 - 入场价) / (入场价 - 止损价)
    const reward = target != null ? target - entryPrice : 0;
    const riskAmount = stop != null ? entryPrice - stop : 0;
    const rrRatio = riskAmount > 0 ? reward / riskAmount : 0;

    // 止损距离 (%)
    const stopDistancePct = stop != null && entryPrice > 0 ? ((entryPrice - stop) / entryPrice) * 100 : 0;

    // 当前距止损距离
    const currentStopDist = stop != null && currentPrice > 0 ? ((currentPrice - stop) / currentPrice) * 100 : 0;

    // ATR止损参考 (2倍ATR作为合理止损)
    const lastATR = chartData?.atr ? chartData.atr.filter((v): v is number => v != null).pop() ?? null : null;
    const atrStopRef = lastATR != null ? lastATR * 2 : null;

    // 仓位集中度估算
    const positionPct = setup.recommended_position_pct ?? 0;
    const concentrationLevel =
      positionPct >= 20 ? "high" : positionPct >= 10 ? "medium" : "low";

    // 单笔最大亏损金额
    const maxLossPerShare = stop != null ? entryPrice - stop : 0;
    const maxLossAmount = maxLossPerShare * quantity;

    return {
      rrRatio, stopDistancePct, currentStopDist, atrStopRef,
      concentrationLevel, maxLossAmount, maxLossPerShare,
      reward, riskAmount, currentPrice, stop, target,
    };
  }, [setup, scenarios, entryPrice, quantity, chartData?.atr, detail?.bars]);

  // ── renderScenarioBox ──
  const renderScenarioBox = (label: string, exitPrice: number) => {
    if (!(exitPrice > 0) || !(entryPrice > 0)) return null;
    const returnPct = (exitPrice - entryPrice) / entryPrice;
    const projectedPnl = (exitPrice - entryPrice) * quantity;
    const projectedValue = exitPrice * quantity;
    return (
      <div className="scenario-box" key={label}>
        <strong>{label}</strong>
        <span className="metric-value">{percent(returnPct)}</span>
        <span className={`item-subline ${pnlClass(projectedPnl)}`}>{money(projectedPnl)}</span>
        <span className="item-subline">{t("projectedValue")}: {money(projectedValue)}</span>
        <span className="item-subline">{t("target")}: {score(exitPrice)}</span>
      </div>
    );
  };

  const lastMAValue = (arr: (number | null)[]) => {
    const v = arr[arr.length - 1];
    return v !== null && v !== undefined ? score(v) : "-";
  };

  // Trigger formatting functions
  const formatOpenTrigger = useCallback((item: any) => {
    if (item.entry_min === null || item.entry_max === null) return "-";
    return ctx.locale === "zh-CN"
      ? `仅在 ${item.entry_min} - ${item.entry_max} 区间内开仓`
      : `Open only inside ${item.entry_min} - ${item.entry_max}`;
  }, [ctx.locale]);

  const formatAddTrigger = useCallback((item: any) => {
    if (!item.allow_add_position) return "-";
    return ctx.locale === "zh-CN"
      ? "满足加仓条件后分批加仓"
      : "Add in tranches when conditions met";
  }, [ctx.locale]);

  const formatStopTrigger = useCallback((item: any) => {
    if (item.stop_loss === null) return "-";
    return ctx.locale === "zh-CN"
      ? `跌破 ${item.stop_loss} 止损`
      : `Stop if breaks below ${item.stop_loss}`;
  }, [ctx.locale]);

  const formatTrimTrigger = useCallback((item: any) => {
    if (item.target_price === null) return "-";
    return ctx.locale === "zh-CN"
      ? `触及 ${item.target_price} 减仓`
      : `Trim near ${item.target_price}`;
  }, [ctx.locale]);

  // ── Candlestick chart option ──
  const chartOption = useMemo(() => {
    if (!chartData) return null;
    const signals = detail?.latest_trade_setup?.chart_signals ?? [];

    const markLineData = signals.map((sig) => {
      const colorMap: Record<string, string> = {
        stop: "#b42318", target: "#0f766e", "buy-zone": "#7c3aed",
      };
      const lineColor = colorMap[sig.kind] ?? "#059669";
      return {
        yAxis: sig.price,
        label: { formatter: signalLabel(sig), position: "end" as const, color: lineColor },
        lineStyle: { color: lineColor, type: "dashed" as const, width: 1 },
      };
    });

    // Y轴范围计算
    const allPrices = chartData.candlestick.flatMap((c) => [c[1], c[2], c[3], c[4]]);
    const maValues = [...chartData.ma10, ...chartData.ma20].filter((v): v is number => v !== null);
    const keyPrices: number[] = [];
    if (setup?.target_price != null) keyPrices.push(setup.target_price);
    if (setup?.stop_loss != null) keyPrices.push(setup.stop_loss);
    const futureZones = activeFutureBuyPlan.flatMap((p) =>
      p.zone_min !== null && p.zone_max !== null ? [p.zone_min, p.zone_max] : []
    );
    const signalPrices = signals.map((s) => s.price).filter((v): v is number => v != null);

    const allRelevant = [...allPrices, ...maValues, ...keyPrices, ...signalPrices]
      .filter((v): v is number => v != null && !isNaN(v) && isFinite(v));
    const priceMin = allRelevant.length ? Math.min(...allRelevant) * 0.985 : 0;
    const priceMax = allRelevant.length ? Math.max(...allRelevant) * 1.015 : 100;

    return {
      animation: false,
      tooltip: {
        trigger: "axis" as const, axisPointer: { type: "cross" },
        formatter: (params: any) => {
          if (!params || !Array.isArray(params) || !params.length) return "";
          const p = params[0];
          const kData = chartData.candlestick[p.dataIndex];
          if (!kData) return "";
          const [open, close, low, high] = kData;
          const volRaw = chartData.volume?.[p.dataIndex];
          const vol = typeof volRaw === "number" ? volRaw : volRaw?.value ?? 0;
          const ma10Val = chartData.ma10?.[p.dataIndex];
          const ma20Val = chartData.ma20?.[p.dataIndex];
          let html = `<div style="font-size:12px;font-weight:bold;margin-bottom:4px">${p.axisValue}</div>`;
          html += `<div>O:${score(open)} C:${score(close)} L:${score(low)} H:${score(high)}</div>`;
          if (vol > 0) html += `<div>${t("volume")}: ${formatVolume(vol)}</div>`;
          if (ma10Val != null) html += `<div style="color:#f59e0b">MA10: ${score(ma10Val)}</div>`;
          if (ma20Val != null) html += `<div style="color:#3b82f6">MA20: ${score(ma20Val)}</div>`;
          // MACD tooltip
          const dif = chartData.macd?.dif[p.dataIndex];
          const dea = chartData.macd?.dea[p.dataIndex];
          const hist = chartData.macd?.macdHist[p.dataIndex];
          if (dif != null) html += `<div style="color:#c084fc">DIF: ${score(dif)}</div>`;
          if (dea != null) html += `<div style="color:#f472b6">DEA: ${score(dea)}</div>`;
          if (hist != null) html += `<div style="color:${hist >= 0 ? '#0f766e' : '#b42318'}">MACD: ${hist >= 0 ? '' : ''}${score(hist)}</div>`;
          // RSI tooltip
          const rsiVal = chartData.rsi?.[p.dataIndex];
          if (rsiVal != null) {
            const rsiColor = rsiVal > 70 ? "#b42318" : rsiVal < 30 ? "#0f766e" : "#6b7280";
            html += `<div style="color:${rsiColor}">RSI(${rsiVal.toFixed(1)})</div>`;
          }
          return html;
        },
      },
      legend: { data: ["K", "MA10", "MA20", t("volume")], top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 50, right: 180, top: 30, bottom: 24 },
      xAxis: { type: "category", data: chartData.dates, axisLabel: { fontSize: 10 } },
      yAxis: [
        { type: "value", min: priceMin, max: priceMax, axisLabel: { fontSize: 10 } },
        { type: "value", scale: true, splitLine: { show: false }, axisLabel: { fontSize: 10, formatter: (v: any) => formatVolume(Number(v)) } },
      ],
      series: [
        { name: "K", type: "candlestick", data: chartData.candlestick,
          itemStyle: { color: "#0f766e", color0: "#b42318", borderColor: "#0f766e", borderColor0: "#b42318" },
          markLine: markLineData.length ? { symbol: "none", data: markLineData } : undefined },
        { name: "MA10", type: "line", data: chartData.ma10, smooth: true, lineStyle: { width: 1, color: "#f59e0b" }, symbol: "none" },
        { name: "MA20", type: "line", data: chartData.ma20, smooth: true, lineStyle: { width: 1, color: "#3b82f6" }, symbol: "none" },
        { name: t("volume"), type: "bar", yAxisIndex: 1, data: chartData.volume },
      ],
    };
  }, [chartData, detail?.latest_trade_setup, activeFutureBuyPlan, setup]);

  // ── MACD 副图 option ──
  const macdOption = useMemo(() => {
    if (!chartData?.macd || !showMACD) return null;
    const { dif, dea, macdHist } = chartData.macd;
    return {
      animation: false,
      grid: { left: 48, right: 12, top: 8, bottom: 24 },
      xAxis: { type: "category", data: chartData.dates, axisLabel: { show: false } },
      yAxis: { type: "value", splitLine: { lineStyle: { type: "dashed", opacity: 0.3 } }, axisLabel: { fontSize: 9 } },
      tooltip: { trigger: "axis" as const, confine: true },
      legend: { data: ["DIF", "DEA", "MACD"], top: 0, textStyle: { fontSize: 10 }, itemWidth: 14, itemHeight: 8 },
      series: [
        { name: "DIF", type: "line", data: dif, lineStyle: { width: 1.2, color: "#c084fc" }, symbol: "none", showSymbol: false },
        { name: "DEA", type: "line", data: dea, lineStyle: { width: 1.2, color: "#f472b6" }, symbol: "none", showSymbol: false },
        { name: "MACD", type: "bar", data: macdHist.map((v) =>
          v == null ? null : { value: v, itemStyle: { color: v >= 0 ? "rgba(15,118,110,0.7)" : "rgba(180,35,24,0.7)" } }
        ), barMaxWidth: 6 },
      ],
    };
  }, [chartData?.macd, showMACD, chartData?.dates]);

  // ── RSI 副图 option ──
  const rsiOption = useMemo(() => {
    if (!chartData?.rsi || !showRSI) return null;
    return {
      animation: false,
      grid: { left: 48, right: 12, top: 8, bottom: 24 },
      xAxis: { type: "category", data: chartData.dates, axisLabel: { show: false } },
      yAxis: {
        type: "value", min: 0, max: 100,
        splitLine: { lineStyle: { type: "dashed", opacity: 0.3 } },
        axisLabel: { fontSize: 9 },
        // 超买超卖参考线
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { type: "solid", width: 1, opacity: 0.4 },
          data: [
            { yAxis: 70, lineStyle: { color: "#b42318" }, label: { formatter: "OB(70)", fontSize: 9 } },
            { yAxis: 30, lineStyle: { color: "#0f766e" }, label: { formatter: "OS(30)", fontSize: 9 } },
            { yAxis: 50, lineStyle: { color: "#6b7280", type: "dashed" } },
          ],
        },
      },
      tooltip: { trigger: "axis" as const, confine: true },
      series: [{
        name: "RSI", type: "line", data: chartData.rsi,
        lineStyle: { width: 1.2, color: "#8b5cf6" }, symbol: "none", showSymbol: false,
        areaStyle: { color: "rgba(139,92,246,0.08)" },
        markPoint: {
          data: chartData.rsiSignals.map((s) => ({
            coord: [s.index, s.value],
            value: s.type === "overbought" ? "超买" : "超卖",
            symbol: s.type === "overbought" ? "triangle" : "triangle",
            symbolSize: 8,
            symbolRotate: s.type === "overbought" ? 180 : 0,
            itemStyle: { color: s.type === "overbought" ? "#b42318" : "#0f766e" },
            label: { fontSize: 9, color: s.type === "overbought" ? "#b42318" : "#0f766e" },
          })),
        } as any,
      }],
    };
  }, [chartData?.rsi, showRSI, chartData?.dates, chartData?.rsiSignals]);

  const lastBar = detail?.bars?.[detail.bars.length - 1];

  // 空状态
  if (!workbench) {
    return (
      <div className="investment-center ic__empty">
        <div className="empty">{t("noScanYet")}</div>
      </div>
    );
  }

  /* ════════════════════════════════════════════════════
     渲染：风险仪表盘
     ════════════════════════════════════════════════════ */
  const renderRiskDashboard = () => {
    if (!riskMetrics) return null;
    const rm = riskMetrics;
    const concColor = rm.concentrationLevel === "high" ? "#b42318" : rm.concentrationLevel === "medium" ? "#f59e0b" : "#0f766e";
    const concLabel = rm.concentrationLevel === "high" ? t("riskHigh") : rm.concentrationLevel === "medium" ? t("riskMedium") : t("riskLow");
    const rrColor = rm.rrRatio >= 2 ? "#0f766e" : rm.rrRatio >= 1 ? "#f59e0b" : "#b42318";
    const stopColor = rm.currentStopDist <= 5 ? "#b42318" : rm.currentStopDist <= 10 ? "#f59e0b" : "#0f766e";

    return (
      <section className="ic__section ic__risk-section">
        <div className="panel">
          <h3>{t("riskDashboard")}</h3>
          <div className="ic__risk-grid">
            {/* 盈亏比 */}
            <div className="ic__risk-card">
              <span className="ic__risk-label">{t("riskRewardRatio")}</span>
              <span className="ic__risk-value" style={{ color: rrColor }}>
                {rm.rrRatio >= 0 ? rm.rrRatio.toFixed(2) : "-"}
                <small>:1</small>
              </span>
              <div className="ic__risk-detail">
                <span>{t("reward")}: +{percent(rm.reward / entryPrice)}</span>
                <span>{t("risk")}: -{percent(rm.riskAmount / entryPrice)}</span>
              </div>
            </div>

            {/* 止损距离 */}
            <div className="ic__risk-card">
              <span className="ic__risk-label">{t("stopLossDistance")}</span>
              <span className="ic__risk-value" style={{ color: stopColor }}>
                {rm.currentStopDist.toFixed(1)}%
              </span>
              <div className="ic__risk-detail">
                <span>当前: {score(rm.currentPrice)}</span>
                <span>止损: {score(rm.stop)}</span>
                {rm.atrStopRef != null && <span>2×ATR: {score(rm.atrStopRef)}</span>}
              </div>
            </div>

            {/* 仓位集中度 */}
            <div className="ic__risk-card">
              <span className="ic__risk-label">{t("positionConcentration")}</span>
              <span className="ic__risk-value" style={{ color: concColor }}>
                {percent(setup?.recommended_position_pct ?? 0)}
              </span>
              <div className="ic__risk-bar">
                <Progress
                  percent={Math.min(100, (setup?.recommended_position_pct ?? 0) * 5)}
                  size="small"
                  strokeColor={concColor}
                  showInfo={false}
                />
                <span className="ic__risk-level">{concLabel}</span>
              </div>
            </div>

            {/* 单笔最大亏损 */}
            <div className="ic__risk-card">
              <span className="ic__risk-label">{t("maxLossPerTrade")}</span>
              <span className="ic__risk-value" style={{ color: rm.maxLossAmount > 0 ? "#b42318" : "#6b7280" }}>
                {rm.maxLossAmount > 0 ? `-¥${rm.maxLossAmount.toFixed(0)}` : "-"}
              </span>
              <div className="ic__risk-detail">
                <span>每股亏损: {score(rm.maxLossPerShare)}</span>
                <span>数量: {quantity}</span>
              </div>
            </div>
          </div>
        </div>
      </section>
    );
  };

  /* ════════════════════════════════════════════════════
     渲染：交易计划区块
     ════════════════════════════════════════════════════ */
  const renderTradePlan = () => (
    <section className="ic__section ic__trade-plan-section">
      <div className="panel">
        <div className="detail-card-head">
          <h3>{t("tradeSetup")}</h3>
          <Button size="small" onClick={() => ctx.generateTradeSetup()}>{t("refreshPlan")}</Button>
        </div>

        {scenarios ? (
          <div id="orderScenarioPreview" className="scenario-preview">
            <div className="scenario-preview-card">
              <div className="scenario-preview-head">
                <strong>{t("scenarioPreview")}</strong>
                <span className="scenario-preview-meta">
                  {joinParts([
                    `${t("plannedOrder")}: ${quantity}`,
                    `${t("referencePrice")}: ${score(entryPrice)}`,
                    `${t("estimateConfidence")}: ${percent(scenarios.confidence_pct)}`,
                    `${t("estimateHorizon")}: ${scenarios.horizon_days}${t("daysUnit")}`,
                  ])}
                </span>
              </div>
              <div className="scenario-grid">
                {renderScenarioBox(t("expectedCase"), scenarios.expected.exit_price)}
                {renderScenarioBox(t("optimisticCase"), scenarios.optimistic.exit_price)}
                {renderScenarioBox(t("pessimisticCase"), scenarios.pessimistic.exit_price)}
              </div>
            </div>
          </div>
        ) : null}

        <div className="list" id="detailSetup">
          {setup ? (
            <>
              <div className="item-topline">
                <strong>{t("buyZone")}</strong>
                <span>
                  {setup.entry_min !== null ? score(setup.entry_min) : "-"} -{" "}
                  {setup.entry_max !== null ? score(setup.entry_max) : "-"}
                </span>
                <span className="badge" style={{ marginLeft: 8, background: "rgba(15,118,110,0.10)", color: "#0f766e" }}>
                  {t("stopLoss")}: {score((scenarios as any)?._adjStop ?? setup.stop_loss)}
                </span>
                <span className="badge" style={{ marginLeft: 4, background: "rgba(180,35,24,0.08)", color: "#b42318" }}>
                  {t("target")}: {score((scenarios as any)?._adjTarget ?? setup.target_price)}
                </span>
              </div>

              <div className="item-subline">
                {joinParts([
                  `${t("recommendedPosition")}: ${percent(setup.recommended_position_pct)}`,
                  `${t("positionAmount")}: ${money(setup.recommended_position_amount)}`,
                  `${t("riskReward")}: ${setup.risk_reward_ratio != null ? score(setup.risk_reward_ratio) : "-"}`,
                  `${t("allowAdd")}: ${setup.allow_add_position ? t("yes") : t("no")}`,
                ])}
              </div>

              <div className="item-subline">
                {joinParts([
                  `${t("stageCap")}: ${setup.stage_cap_pct != null ? percent(setup.stage_cap_pct) : "-"}`,
                  `${t("stageRoom")}: ${setup.remaining_stage_pct != null ? percent(setup.remaining_stage_pct) : "-"}`,
                  `${t("currentPosition")}: ${setup.current_position_pct != null ? percent(setup.current_position_pct) : "-"}`,
                  `${t("riskBudget")}: ${setup.risk_budget_amount != null ? money(setup.risk_budget_amount) : "-"}`,
                  `${t("riskShare")}: ${setup.risk_per_share != null ? score(setup.risk_per_share) : "-"}`,
                ])}
              </div>

              <div className="item-subline">
                {joinParts([
                  `${t("stage")}: ${stageLabel(setup.stage)}`,
                  `${t("action")}: ${actionLabel(setup.action)}`,
                  `${t("guardrails")}: ${joinParts([
                    setup.is_sector_overweight ? t("sectorOverweight") : null,
                    setup.is_asset_overweight ? t("assetOverweight") : null,
                  ]) || t("stable")}`,
                ])}
              </div>

              <div className="item-subline">
                <strong style={{ fontSize: 11 }}>{t("openTrigger")}:</strong>{" "}
                <span style={{ fontSize: 11 }}>{formatOpenTrigger(setup)}</span>
                {" | "}
                <strong style={{ fontSize: 11 }}>{t("addTrigger")}:</strong>{" "}
                <span style={{ fontSize: 11 }}>{formatAddTrigger(setup)}</span>
              </div>
              <div className="item-subline">
                <strong style={{ fontSize: 11 }}>{t("stopTrigger")}:</strong>{" "}
                <span style={{ fontSize: 11 }}>{formatStopTrigger(setup)}</span>
                {" | "}
                <strong style={{ fontSize: 11 }}>{t("trimTrigger")}:</strong>{" "}
                <span style={{ fontSize: 11 }}>{formatTrimTrigger(setup)}</span>
              </div>

              {/* Tranche plan with timeline view */}
              {setup.tranche_plan && setup.tranche_plan.length > 0 && (
                <div className="detail-card" style={{ marginTop: 10 }}>
                  <p className="panel-kicker">{t("tranchePlan")}</p>
                  {/* Timeline view */}
                  <div className="ic__tranche-timeline">
                    {setup.tranche_plan.map((tranche, i) => (
                      <div key={i} className={`ic__tranche-node ic__tranche--${tranche.label}`}>
                        <div className="ic__tranche-dot" />
                        {(i < setup.tranche_plan!.length - 1) && <div className="ic__tranche-line" />}
                        <div className="ic__tranche-content">
                          <strong style={{ color: "#0f766e" }}>{trancheLabel(tranche.label)}</strong>
                          <span className="ic__tranche-meta">
                            {joinParts([
                              `${t("tranchePct")}: ${percent(tranche.position_pct)}`,
                              `${t("positionAmount")}: ${money(tranche.amount)}`,
                              `${t("trigger")}: ${trancheTrigger(tranche.trigger ?? "")}`,
                            ])}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Future buy plan */}
              {activeFutureBuyPlan.length > 0 && (
                <div className="detail-card" style={{ marginTop: 10 }}>
                  <div className="future-plan-head">
                    <p className="panel-kicker">{t("futureBuyPlan")}</p>
                    <div className="future-scenario-tabs">
                      {["general", "short", "mid", "long", "custom"].map((sc) => (
                        <Button key={sc} size="small"
                          type={ctx.futurePlanScenario === sc ? "primary" : "default"}
                          onClick={() => ctx.setFuturePlanScenario(sc)}>
                          {t(`futureScenario${sc.charAt(0).toUpperCase() + sc.slice(1)}`)}
                        </Button>
                      ))}
                    </div>
                  </div>

                  {ctx.futurePlanScenario === "custom" && (
                    <div className="future-custom-grid">
                      <label><span>{t("customHorizon")}</span><input type="number" value={ctx.futurePlanCustom.horizonDays}
                        onChange={(e) => ctx.setFuturePlanCustom({ ...ctx.futurePlanCustom, horizonDays: Number(e.target.value) })} /></label>
                      <label><span>{t("customPullback")}</span><input type="number" value={ctx.futurePlanCustom.pullbackPct}
                        onChange={(e) => ctx.setFuturePlanCustom({ ...ctx.futurePlanCustom, pullbackPct: Number(e.target.value) })} /></label>
                      <label><span>{t("customPosition")}</span><input type="number" value={ctx.futurePlanCustom.positionPct}
                        onChange={(e) => ctx.setFuturePlanCustom({ ...ctx.futurePlanCustom, positionPct: Number(e.target.value) })} /></label>
                    </div>
                  )}

                  {activeFutureBuyPlan.map((plan, i) => (
                    <div key={i} className="item-subline">
                      {joinParts([
                        futureBuyLabel(plan.label),
                        `${t("futureZone")}: ${plan.zone_min !== null ? score(plan.zone_min) : "-"} - ${plan.zone_max !== null ? score(plan.zone_max) : "-"}`,
                        `${t("futureHorizon")}: ${plan.horizon_days}${t("daysUnit")}`,
                        `${t("futurePriority")}: ${futurePriorityLabel(plan.priority)}`,
                        `${t("recommendedPosition")}: ${percent(plan.position_pct)}`,
                        `${t("positionAmount")}: ${money(plan.amount)}`,
                        `${t("trigger")}: ${futureTriggerLabel(plan)}`,
                      ])}
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="empty">{t("latestTradeSetupEmpty")}</div>
          )}
        </div>
      </div>
    </section>
  );

  /* ════════════════════════════════════════════════════
     渲染：K线图区块（含MACD+RSI副图）
     ════════════════════════════════════════════════════ */
  const renderChartSection = () => (
    <section className="ic__section ic__chart-section">
      <div className="panel">
        <div className="detail-card-head">
          <h3>{t("recentBars")}</h3>
          <div className="chart-toolbar">
            <p className="panel-meta" id="chartMeta">
              {lastBar ? `${lastBar.trade_date}${DOT}${t("close")}: ${score(lastBar.close)}` : "-"}
            </p>
            <div className="detail-actions chart-actions">
              <div className="chart-timeframe-group">
                <Button size="small" type={ctx.chartTimeframe === "daily" ? "primary" : "default"}
                  onClick={() => ctx.setChartTimeframe("daily")}>{t("chartDaily")}</Button>
                <Button size="small" type={ctx.chartTimeframe === "weekly" ? "primary" : "default"}
                  onClick={() => ctx.setChartTimeframe("weekly")}>{t("chartWeekly")}</Button>
              </div>
              <span className="chart-window-pill">{ctx.chartWindowSize}{t("barsUnit")}</span>
              <Button size="small" onClick={() => ctx.setChartWindowSize(Math.max(20, ctx.chartWindowSize - 20))}>{t("zoomIn")}</Button>
              <Button size="small" onClick={() => ctx.setChartWindowSize(Math.min(250, ctx.chartWindowSize + 20))}>{t("zoomOut")}</Button>
              <Button size="small" onClick={() => { ctx.setChartWindowSize(60); ctx.setChartRange(null); }}>{t("resetZoom")}</Button>
            </div>
          </div>
          {/* 指标面板开关 */}
          <div className="ic__indicator-toggles">
            <button className={`ic__toggle-btn${showMACD ? " active" : ""}`} onClick={() => setShowMACD(!showMACD)}>MACD</button>
            <button className={`ic__toggle-btn${showRSI ? " active" : ""}`} onClick={() => setShowRSI(!showRSI)}>RSI</button>
          </div>
        </div>

        <div id="detailChart" className={`chart-surface${ctx.chartExpanded ? " expanded" : ""}`}>
          {chartOption ? (
            <div className="ic__charts-stack">
              {/* 主K线图 */}
              <div style={{ position: "relative" }}>
                <ReactECharts
                  option={chartOption}
                  style={{ height: ctx.chartExpanded ? 520 : 400, width: "100%" }}
                />
                {chartData && activeFutureBuyPlan.length > 0 && (
                  <FuturePlanOverlay
                    plans={activeFutureBuyPlan}
                    lastClose={chartData.closes[chartData.closes.length - 1]}
                    priceMin={(() => {
                      const ap = chartData.candlestick.flatMap((c) => [c[1], c[2], c[3], c[4]]);
                      const mv = [...chartData.ma10, ...chartData.ma20].filter((v): v is number => v != null);
                      const all = [...ap, ...mv].filter((v): v is number => v != null && !isNaN(v));
                      return all.length > 0 ? Math.min(...all) * 0.985 : 0;
                    })()}
                    priceMax={(() => {
                      const ap = chartData.candlestick.flatMap((c) => [c[1], c[2], c[3], c[4]]);
                      const mv = [...chartData.ma10, ...chartData.ma20].filter((v): v is number => v != null);
                      const all = [...ap, ...mv].filter((v): v is number => v != null && !isNaN(v));
                      return all.length > 0 ? Math.max(...all) * 1.015 : 100;
                    })()}
                    height={ctx.chartExpanded ? 520 : 400}
                  />
                )}
              </div>
              {/* MACD 副图 */}
              {macdOption && (
                <div className="ic__sub-chart">
                  <ReactECharts option={macdOption} style={{ height: 140, width: "100%" }} />
                </div>
              )}
              {/* RSI 副图 */}
              {rsiOption && (
                <div className="ic__sub-chart">
                  <ReactECharts option={rsiOption} style={{ height: 140, width: "100%" }} />
                </div>
              )}
            </div>
          ) : (
            <div className="empty">{t("noChart")}</div>
          )}
          {chartData && (
            <div className="chart-legend">
              {lastBar && (
                <span>O:{score(lastBar.open)} H:{score(lastBar.high)} L:{score(lastBar.low)} C:{score(lastBar.close)}</span>
              )}
              <span>{t("ma10")}: {lastMAValue(chartData.ma10)}</span>
              <span>{t("ma20")}: {lastMAValue(chartData.ma20)}</span>
              {chartData.macd && <span>DIF: {lastMAValue(chartData.macd.dif)} DEA: {lastMAValue(chartData.macd.dea)}</span>}
              {chartData.rsi && <span>RSI: {lastMAValue(chartData.rsi)}</span>}
            </div>
          )}
        </div>
      </div>
    </section>
  );

  /* ════════════════════════════════════════════════════
     布局渲染
     ════════════════════════════════════════════════════ */

  // 移动端布局
  if (isMobile) {
    return (
      <div className="investment-center ic__mobile-layout">
        <div className="ic__search-bar">
          <Input prefix={<SearchOutlined style={{ color: "var(--muted)" }} />}
            placeholder={t("searchSymbolPlaceholder")}
            allowClear value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onPressEnter={() => { if (searchResults.length > 0) handleSelectSymbol(searchResults[0].id); }}
          />
          {searchResults.length > 0 && (
            <div className="ic__search-dropdown">
              {searchResults.map((item) => (
                <button key={item.id} className="ic__search-result-item" onClick={() => handleSelectSymbol(item.id)}>
                  <span className="symbol-code">{item.symbol}</span>
                  <span className="symbol-name">{item.name}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        {quickSymbols.length > 0 && !searchQuery && (
          <div className="ic__quick-chips">
            {quickSymbols.slice(0, 8).map((item) => (
              <button key={item.symbol_id} className={`ic__chip${ctx.activeSymbolId === item.symbol_id ? " active" : ""}`}
                onClick={() => handleSelectSymbol(item.symbol_id)}>
                {item.symbol} {item.name}
              </button>
            ))}
          </div>
        )}
        {renderRiskDashboard()}
        {renderTradePlan()}
        {renderChartSection()}
      </div>
    );
  }

  // 桌面端布局
  return (
    <div className="investment-center ic__desktop-layout ic__layout--fullwidth">
      <header className="ic__top-search">
        <Input prefix={<SearchOutlined style={{ color: "var(--muted)", fontSize: 15 }} />}
          placeholder={t("searchSymbolPlaceholder")}
          allowClear size="large" value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          onPressEnter={() => { if (searchResults.length > 0) handleSelectSymbol(searchResults[0].id); }}
          className="ic__top-input"
        />
        {searchResults.length > 0 && (
          <div className="ic__search-dropdown">
            {searchResults.map((item) => (
              <button key={item.id} className="ic__search-result-item" onClick={() => handleSelectSymbol(item.id)}>
                <span className="symbol-code">{item.symbol}</span>
                <span className="symbol-name">{item.name}</span>
              </button>
            ))}
          </div>
        )}
      </header>

      {!searchQuery && quickSymbols.length > 0 && (
        <nav className="ic__quick-bar">
          {quickSymbols.map((item) => (
            <button key={item.symbol_id} className={`ic__chip${ctx.activeSymbolId === item.symbol_id ? " active" : ""}`}
              onClick={() => handleSelectSymbol(item.symbol_id)}>
              {item.symbol} {item.name}
            </button>
          ))}
        </nav>
      )}

      <main className="ic__main-content">
        {detail && (
          <div className="ic__symbol-header">
            <div className="symbol-title">
              <span className="symbol-code">{detail.symbol.symbol}</span>
              <span className="symbol-name">{detail.symbol.name}</span>
              <span className="symbol-meta">
                {`${t("marketLabel")}: ${detail.symbol.market}${DOT}${t("regionLabel")}: ${regionLongLabel(detail.symbol.region)}${DOT}${t("assetLabel")}: ${assetTypeLabel(detail.symbol.asset_type)}`}
              </span>
              {detail.position && (
                <>
                  <span className="badge" style={{ marginLeft: 8 }}>{t("holdingQty")}: {detail.position.quantity}</span>
                  <span className="badge">{t("avgCost")}: {score(detail.position.avg_cost)}</span>
                </>
              )}
            </div>
          </div>
        )}
        {renderRiskDashboard()}
        {renderTradePlan()}
        {renderChartSection()}
      </main>
    </div>
  );
}
