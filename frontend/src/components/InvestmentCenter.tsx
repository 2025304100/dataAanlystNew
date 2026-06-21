import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import ReactECharts from "echarts-for-react";
import { Button, Input } from "antd";
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

interface InvestmentCenterProps {
  openMetricModal: (type: string) => void;
}

/* ════════════════════════════════════════════════════════════
   以下工具函数与 DetailModal.tsx 完全一致（原封不动复制）
   ════════════════════════════════════════════════════════════ */

function computeMA(values: number[], period: number): (number | null)[] {
  const result: (number | null)[] = [];
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) { result.push(null); continue; }
    let sum = 0;
    for (let j = 0; j < period; j++) sum += values[i - j];
    result.push(sum / period);
  }
  return result;
}

function formatVolume(val: number): string {
  if (val >= 1e8) return `${(val / 1e8).toFixed(2)}${t("yiUnit")}`;
  if (val >= 1e4) return `${(val / 1e4).toFixed(2)}${t("wanUnit")}`;
  return String(val);
}

// --- Future buy plan scenario helpers（与 DetailModal 一致）---

function planWithRatio(plan: FutureBuyPlan, ratio: number): FutureBuyPlan {
  return {
    ...plan,
    position_pct: Number((Number(plan.position_pct || 0) * ratio).toFixed(4)),
    amount: Number((Number(plan.amount || 0) * ratio).toFixed(2)),
  };
}

function invalidFuturePlan(setup: TradeSetup | null): FutureBuyPlan | undefined {
  return (setup?.future_buy_plan ?? []).find(
    (plan) => plan.priority === "avoid" || plan.label === "invalid_below_stop"
  );
}

function buildLongFuturePlan(setup: TradeSetup | null): FutureBuyPlan[] {
  if (!setup) return [];
  const ma20 = setup.moving_averages?.ma20;
  const anchor = Number(ma20 || setup.entry_min || setup.entry_max || 0);
  if (!(anchor > 0)) return setup.future_buy_plan ?? [];
  const positionPct = Math.max(Number(setup.recommended_position_pct || 0) * 0.5, 0);
  const amount = Number(setup.recommended_position_amount || 0) * 0.5;
  const plans: FutureBuyPlan[] = [
    {
      label: "long_accumulate_zone",
      horizon_days: 30,
      zone_min: Number((anchor * 0.97).toFixed(2)),
      zone_max: Number((anchor * 1.02).toFixed(2)),
      priority: "low",
      position_pct: Number(positionPct.toFixed(4)),
      amount: Number(amount.toFixed(2)),
      trigger: "",
    },
  ];
  const invalid = invalidFuturePlan(setup);
  if (invalid) plans.push(invalid);
  return plans;
}

function buildCustomFuturePlan(
  setup: TradeSetup | null,
  custom: { horizonDays: number; pullbackPct: number; positionPct: number },
  totalCapital: number,
  investableRatio: number
): FutureBuyPlan[] {
  if (!setup) return [];
  const base = Number(setup.entry_max || setup.entry_min || setup.moving_averages?.ma20 || 0);
  if (!(base > 0)) return setup.future_buy_plan ?? [];
  const pullback = Math.max(0, Number(custom.pullbackPct || 0)) / 100;
  const zoneMax = base * (1 - pullback);
  const zoneMin = zoneMax * 0.985;
  const positionPct = Math.max(0, Number(custom.positionPct || 0)) / 100;
  const amount = Number(totalCapital || 0) * Number(investableRatio || 1) * positionPct;
  const plans: FutureBuyPlan[] = [
    {
      label: "custom_buy_zone",
      horizon_days: Math.max(1, Number(custom.horizonDays || 20)),
      zone_min: Number(zoneMin.toFixed(2)),
      zone_max: Number(zoneMax.toFixed(2)),
      priority: "normal",
      position_pct: Number(positionPct.toFixed(4)),
      amount: Number(amount.toFixed(2)),
      trigger: "",
    },
  ];
  const invalid = invalidFuturePlan(setup);
  if (invalid) plans.push(invalid);
  return plans;
}

function getActiveFutureBuyPlan(
  setup: TradeSetup | null,
  scenario: string,
  custom: { horizonDays: number; pullbackPct: number; positionPct: number },
  totalCapital: number,
  investableRatio: number
): FutureBuyPlan[] {
  const basePlan = setup?.future_buy_plan ?? [];
  if (scenario === "short") {
    return basePlan
      .filter((plan) => plan.priority === "high" || plan.horizon_days <= 5 || plan.priority === "avoid")
      .map((plan) => (plan.priority === "avoid" ? plan : planWithRatio(plan, 0.7)));
  }
  if (scenario === "long") return buildLongFuturePlan(setup);
  if (scenario === "custom") return buildCustomFuturePlan(setup, custom, totalCapital, investableRatio);
  if (scenario === "mid") {
    const midPlans = basePlan
      .filter((plan) => plan.priority === "avoid" || plan.priority !== "high" || plan.horizon_days >= 5)
      .map((plan) => (plan.priority === "avoid" ? plan : planWithRatio(plan, 0.9)));
    return midPlans.length ? midPlans : basePlan;
  }
  return basePlan;
}

/* ── Future Buy Plan SVG Overlay（与 DetailModal 完全一致）── */
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

  // K线本地缓存：当后端返回的bar数量不够时，懒加载更多并缓存在这里（按symbolId索引）
  const [extraBarsCache, setExtraBarsCache] = useState<Record<number, any[]>>({});
  const loadingBarsRef = useRef<Set<number>>(new Set());
  const windowInitializedRef = useRef(false); // 只初始化一次窗口大小

  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 1024);
    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, []);

  // 投资中心默认显示180根K线（只执行一次，后续用户可自由调整）
  useEffect(() => {
    if (ctx.detail && ctx.activeTab === "investment" && !windowInitializedRef.current) {
      windowInitializedRef.current = true;
      ctx.setChartWindowSize(180);
    }
  }, [ctx.detail, ctx.activeTab, ctx.setChartWindowSize]); // eslint-disable-line

  // ── 懒加载K线数据：当窗口大小超过已有bar数量时自动加载更多 ──
  useEffect(() => {
    const symbolId = ctx.activeSymbolId;
    if (!symbolId || !detail?.bars?.length) return;

    const neededBars = ctx.chartWindowSize; // 需要显示的K线数
    const existingBars = detail.bars.length; // 后端已返回的数量
    const extraBars = extraBarsCache[symbolId] ?? []; // 已缓存追加的
    const totalAvailable = existingBars + extraBars.length;

    if (neededBars <= totalAvailable) return; // 够了，不需要加载
    if (loadingBarsRef.current.has(symbolId)) return; // 正在加载中

    loadingBarsRef.current.add(symbolId);

    api.getBars(symbolId, Math.max(neededBars + 60, 250))
      .then((fetched: any[]) => {
        if (!fetched || !fetched.length) return;
        // 过滤掉已有的bar（按trade_date去重），只保留新增的
        const existingDates = new Set(detail.bars.map((b: any) => b.trade_date));
        const newBars = fetched.filter((b: any) => !existingDates.has(b.trade_date));
        if (newBars.length > 0) {
          setExtraBarsCache((prev) => ({ ...prev, [symbolId]: [...newBars, ...(prev[symbolId] ?? [])] }));
        }
      })
      .catch(() => {})
      .finally(() => { loadingBarsRef.current.delete(symbolId); });
  }, [ctx.activeSymbolId, ctx.chartWindowSize, detail?.bars?.length]); // eslint-disable-line

  // 搜索逻辑：防抖调用 API
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    if (!searchQuery.trim()) { setSearchResults([]); return; }
    setSearching(true);
    searchTimerRef.current = setTimeout(async () => {
      try {
        const results = await api.getSymbols(searchQuery.trim());
        setSearchResults(results.slice(0, 20));
      } catch {
        setSearchResults([]);
      } finally {
        setSearching(false);
      }
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
     以下数据计算与渲染逻辑与 DetailModal.tsx 完全一致
     ════════════════════════════════════════════════════ */

  const setup = detail?.latest_trade_setup ?? null;

  // Entry price & quantity（与 DetailModal 一致）
  const baseScenarios = setup?.return_scenarios;
  const _refPrice = baseScenarios?.reference_price ?? computeSuggestedPrice(detail);
  const entryPrice = Number(ctx.simPrice) > 0 ? Number(ctx.simPrice) : Number(_refPrice);
  const quantity = Number(ctx.simQuantity) > 0
    ? Number(ctx.simQuantity)
    : Number(baseScenarios?.planned_order?.quantity ?? 0);

  // Order scenario preview - adjusted by active future plan scenario（与 DetailModal 一致）
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

  // renderScenarioBox（与 DetailModal 一致）
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

  // Trigger formatting functions（与 DetailModal 一致，locale-aware）
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

  // Chart data memo（与 DetailModal 一致，使用 aggregateWeeklyBars，支持本地缓存合并）
  const chartData = useMemo(() => {
    if (!detail?.bars?.length) return null;
    // 合并后端返回的bar和本地懒加载缓存的extra bar
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
    const ma10 = computeMA(closes, 10);
    const ma20 = computeMA(closes, 20);
    return { bars, dates, closes, candlestick, volume, ma10, ma20 };
  }, [detail?.bars, extraBarsCache, ctx.activeSymbolId, ctx.chartTimeframe, ctx.chartWindowSize]);

  // Active future buy plan（与 DetailModal 一致）
  const activeFutureBuyPlan = useMemo(() => {
    const s = detail?.latest_trade_setup ?? null;
    if (!s) return [];
    const totalCapital = ctx.workbench?.portfolio?.total_capital ?? 0;
    const investableRatio = ctx.workbench?.portfolio?.investable_ratio ?? 1;
    return getActiveFutureBuyPlan(s, ctx.futurePlanScenario, ctx.futurePlanCustom, totalCapital, investableRatio);
  }, [detail?.latest_trade_setup, ctx.futurePlanScenario, ctx.futurePlanCustom, ctx.workbench?.portfolio]);

  // Candlestick chart option memo（与 DetailModal 一致）
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

    // 计算Y轴价格范围：必须包含K线、均线、目标价、止损价、未来计划区间
    const allPrices = chartData.candlestick.flatMap((c) => [c[1], c[2], c[3], c[4]]);
    const maValues = [...chartData.ma10, ...chartData.ma20].filter((v): v is number => v !== null);
    // 加入目标价和止损价（关键！否则这些水平线会被挤出可视区域）
    const keyPrices: number[] = [];
    if (setup?.target_price != null) keyPrices.push(setup.target_price);
    if (setup?.stop_loss != null) keyPrices.push(setup.stop_loss);
    // 加入未来买入计划区间
    const futureZones = activeFutureBuyPlan.flatMap((p) =>
      p.zone_min !== null && p.zone_max !== null ? [p.zone_min, p.zone_max] : []
    );
    // 加入信号线价格
    const signalPrices = signals.map((s) => s.price).filter((v): v is number => v != null);

    const allRelevant = [...allPrices, ...maValues, ...keyPrices, ...futureZones, ...signalPrices]
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
          return html;
        },
      },
      legend: { data: ["K", "MA10", "MA20", t("volume")], top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 50, right: 180, top: 30, bottom: 24 },
      // 投资中心不使用 dataZoom slider（用按钮控制窗口大小），避免与手动缩放冲突
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
     渲染交易计划区块（与 DetailModal JSX 一致）
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

              {/* Tranche plan */}
              {setup.tranche_plan && setup.tranche_plan.length > 0 && (
                <div className="detail-card" style={{ marginTop: 10 }}>
                  <p className="panel-kicker">{t("tranchePlan")}</p>
                  {setup.tranche_plan.map((tranche, i) => (
                    <div key={i} className="item-subline">
                      <strong style={{ color: "#0f766e" }}>{trancheLabel(tranche.label)}</strong>
                      {" | "}
                      {joinParts([
                        `${t("tranchePct")}: ${percent(tranche.position_pct)}`,
                        `${t("positionAmount")}: ${money(tranche.amount)}`,
                        `${t("trigger")}: ${trancheTrigger(tranche.trigger ?? "")}`,
                      ])}
                    </div>
                  ))}
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
                      <label>
                        <span>{t("customHorizon")}</span>
                        <input type="number" value={ctx.futurePlanCustom.horizonDays}
                          onChange={(e) =>
                            ctx.setFuturePlanCustom({ ...ctx.futurePlanCustom, horizonDays: Number(e.target.value) })
                          } />
                      </label>
                      <label>
                        <span>{t("customPullback")}</span>
                        <input type="number" value={ctx.futurePlanCustom.pullbackPct}
                          onChange={(e) =>
                            ctx.setFuturePlanCustom({ ...ctx.futurePlanCustom, pullbackPct: Number(e.target.value) })
                          } />
                      </label>
                      <label>
                        <span>{t("customPosition")}</span>
                        <input type="number" value={ctx.futurePlanCustom.positionPct}
                          onChange={(e) =>
                            ctx.setFuturePlanCustom({ ...ctx.futurePlanCustom, positionPct: Number(e.target.value) })
                          } />
                      </label>
                    </div>
                  )}

                  {activeFutureBuyPlan.map((plan, i) => (
                    <div key={i} className="item-subline">
                      {joinParts([
                        futureBuyLabel(plan.label),
                        `${t("futureZone")}: ${
                          plan.zone_min !== null ? score(plan.zone_min) : "-"
                        } - ${plan.zone_max !== null ? score(plan.zone_max) : "-"}`,
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
     渲染K线图区块（与 DetailModal JSX 一致）
     ════════════════════════════════════════════════════ */
  const renderChartSection = () => (
    <section className="ic__section ic__chart-section">
      <div className="panel">
        <div className="detail-card-head">
          <h3>{t("recentBars")}</h3>
          <div className="chart-toolbar">
            <p className="panel-meta" id="chartMeta">
              {lastBar
                ? `${lastBar.trade_date}${DOT}${t("close")}: ${score(lastBar.close)}`
                : "-"}
            </p>
            <div className="detail-actions chart-actions">
              <div className="chart-timeframe-group">
                <Button size="small" type={ctx.chartTimeframe === "daily" ? "primary" : "default"}
                  onClick={() => ctx.setChartTimeframe("daily")}>{t("chartDaily")}</Button>
                <Button size="small" type={ctx.chartTimeframe === "weekly" ? "primary" : "default"}
                  onClick={() => ctx.setChartTimeframe("weekly")}>{t("chartWeekly")}</Button>
              </div>
              <span className="chart-window-pill">{ctx.chartWindowSize}{t("barsUnit")}</span>
              <Button size="small" onClick={() =>
                ctx.setChartWindowSize(Math.max(20, ctx.chartWindowSize - 20))
              }>{t("zoomIn")}</Button>
              <Button size="small" onClick={() =>
                ctx.setChartWindowSize(Math.min(250, ctx.chartWindowSize + 20))
              }>{t("zoomOut")}</Button>
              <Button size="small" onClick={() => {
                ctx.setChartWindowSize(60);
                ctx.setChartRange(null);
              }}>{t("resetZoom")}</Button>
            </div>
          </div>
        </div>

        <div id="detailChart" className={`chart-surface${ctx.chartExpanded ? " expanded" : ""}`}>
          {chartOption ? (
            <>
              <div style={{ position: "relative" }}>
                <ReactECharts
                  option={chartOption}
                  style={{
                    height: ctx.chartExpanded ? "780px" : "560px",
                    width: "100%",
                  }}
                />
                {chartData && activeFutureBuyPlan.length > 0 && (
                  <FuturePlanOverlay
                    plans={activeFutureBuyPlan}
                    lastClose={chartData.closes[chartData.closes.length - 1]}
                    priceMin={(() => {
                      const allPrices = chartData.candlestick.flatMap((c) => [c[1], c[2], c[3], c[4]]);
                      const maValues = [...chartData.ma10, ...chartData.ma20].filter((v): v is number => v !== null);
                      const futureZones = activeFutureBuyPlan.flatMap((p) =>
                        p.zone_min !== null && p.zone_max !== null ? [p.zone_min, p.zone_max] : []
                      );
                      const all = [...allPrices, ...maValues, ...futureZones].filter((v): v is number => v != null && !isNaN(v));
                      return Math.min(...all) * 0.985;
                    })()}
                    priceMax={(() => {
                      const allPrices = chartData.candlestick.flatMap((c) => [c[1], c[2], c[3], c[4]]);
                      const maValues = [...chartData.ma10, ...chartData.ma20].filter((v): v is number => v !== null);
                      const futureZones = activeFutureBuyPlan.flatMap((p) =>
                        p.zone_min !== null && p.zone_max !== null ? [p.zone_min, p.zone_max] : []
                      );
                      const all = [...allPrices, ...maValues, ...futureZones].filter((v): v is number => v != null && !isNaN(v));
                      return Math.max(...all) * 1.015;
                    })()}
                    height={ctx.chartExpanded ? 780 : 560}
                  />
                )}
              </div>
              {chartData && (
                <div className="chart-legend">
                  {lastBar && (
                    <span>O:{score(lastBar.open)} H:{score(lastBar.high)} L:{score(lastBar.low)} C:{score(lastBar.close)}</span>
                  )}
                  <span>{t("ma10")}: {lastMAValue(chartData.ma10)}</span>
                  <span>{t("ma20")}: {lastMAValue(chartData.ma20)}</span>
                </div>
              )}
            </>
          ) : (
            <div className="empty">{t("noChart")}</div>
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
            onPressEnter={() => {
              if (searchResults.length > 0) handleSelectSymbol(searchResults[0].id);
            }}
          />
          {searchResults.length > 0 && (
            <div className="ic__search-dropdown">
              {searchResults.map((item) => (
                <button key={item.id} className="ic__search-result-item"
                  onClick={() => handleSelectSymbol(item.id)}>
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
          onPressEnter={() => {
            if (searchResults.length > 0) handleSelectSymbol(searchResults[0].id);
          }}
          className="ic__top-input"
        />
        {searchResults.length > 0 && (
          <div className="ic__search-dropdown">
            {searchResults.map((item) => (
              <button key={item.id} className="ic__search-result-item"
                onClick={() => handleSelectSymbol(item.id)}>
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
        {renderTradePlan()}
        {renderChartSection()}
      </main>
    </div>
  );
}
