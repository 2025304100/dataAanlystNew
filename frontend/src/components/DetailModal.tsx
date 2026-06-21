import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";
import ReactECharts from "echarts-for-react";
import { Modal, InputNumber, Button } from "antd";
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
  statPct,
  clamp,
  aggregateWeeklyBars,
  computeSuggestedPrice,
  signalLabel,
  formatDate,
  sideBadgeClass,
} from "../utils/format";
import type { FutureBuyPlan, TradeSetup, JournalEntry, TradeRecord } from "../types";

interface DetailModalProps {
  open: boolean;
  onClose: () => void;
}

function computeMA(values: number[], period: number): (number | null)[] {
  const result: (number | null)[] = [];
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) {
      result.push(null);
      continue;
    }
    let sum = 0;
    for (let j = 0; j < period; j++) {
      sum += values[i - j];
    }
    result.push(sum / period);
  }
  return result;
}

function formatVolume(val: number): string {
  if (val >= 1e8) return `${(val / 1e8).toFixed(2)}${t("yiUnit")}`;
  if (val >= 1e4) return `${(val / 1e4).toFixed(2)}${t("wanUnit")}`;
  return String(val);
}

// --- Future buy plan scenario helpers (ported from original workbench.js) ---

function planWithRatio(plan: FutureBuyPlan, ratio: number): FutureBuyPlan {
  return {
    ...plan,
    position_pct: Number((Number(plan.position_pct || 0) * ratio).toFixed(4)),
    amount: Number((Number(plan.amount || 0) * ratio).toFixed(2)),
  };
}

function invalidFuturePlan(setup: TradeSetup | null): FutureBuyPlan | undefined {
  return (setup?.future_buy_plan ?? []).find((plan) => plan.priority === "avoid" || plan.label === "invalid_below_stop");
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
  if (scenario === "long") {
    return buildLongFuturePlan(setup);
  }
  if (scenario === "custom") {
    return buildCustomFuturePlan(setup, custom, totalCapital, investableRatio);
  }
  if (scenario === "mid") {
    const midPlans = basePlan
      .filter((plan) => plan.priority === "avoid" || plan.priority !== "high" || plan.horizon_days >= 5)
      .map((plan) => (plan.priority === "avoid" ? plan : planWithRatio(plan, 0.9)));
    return midPlans.length ? midPlans : basePlan;
  }
  return basePlan;
}

// --- Future Buy Plan SVG Overlay Component ---
// Renders a panel on the right side of the chart with colored bands, labels, and dashed curve
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
  const padding = 8; // space between chart edge and panel
  const bandPadding = 6; // horizontal padding inside panel for bands

  // Price-to-Y converter (maps price to pixel Y position within plot area)
  const plotTop = 30; // grid top offset (matches ECharts grid.top)
  const plotBottom = 50; // grid bottom offset (matches ECharts grid.bottom)
  const legendHeight = 28; // bottom legend area
  const plotHeight = height - plotTop - plotBottom - legendHeight;
  const priceSpan = Math.max(priceMax - priceMin, 0.01);

  const priceToY = (price: number) => {
    const ratio = (priceMax - price) / priceSpan;
    return plotTop + ratio * plotHeight;
  };

  // Style map matching original FUTURE_PLAN_STYLE
  const styleMap: Record<string, { fill: string; stroke: string; dash: string }> = {
    avoid: { fill: "rgba(180, 35, 24, 0.12)", stroke: "#b42318", dash: "6,4" },
    high: { fill: "rgba(15, 118, 110, 0.16)", stroke: "#0f766e", dash: "" },
    low: { fill: "rgba(37, 99, 235, 0.11)", stroke: "#2563eb", dash: "4,4" },
    normal: { fill: "rgba(15, 118, 110, 0.10)", stroke: "#0f766e", dash: "4,4" },
  };

  // Filter usable plans (those with zone_min and zone_max)
  const usablePlans = plans.filter((plan) => plan.zone_min !== null && plan.zone_max !== null);
  const nonAvoidPlans = usablePlans.filter((p) => p.priority !== "avoid").slice(0, 3);

  // SVG viewBox width = full container width, panel is positioned on the right
  // We use position:absolute with right:0 so the SVG only needs to cover the panel area
  const svgWidth = panelWidth + padding * 2;

  return (
    <svg
      style={{
        position: "absolute",
        right: 0,
        top: 0,
        width: svgWidth,
        height: height,
        pointerEvents: "none",
        overflow: "visible",
      }}
      viewBox={`0 0 ${svgWidth} ${height}`}
    >
      {/* Panel background */}
      <rect
        x={padding}
        y={plotTop}
        width={panelWidth - padding}
        height={plotHeight}
        rx={8}
        ry={8}
        fill="rgba(31,41,51,0.035)"
        stroke="rgba(31,41,51,0.10)"
        strokeWidth={1}
        strokeDasharray="4,4"
      />

      {/* Panel title */}
      <text x={padding + 6} y={plotTop + 18} fontSize={11} fill="#6b7280">
        {t("futureBuyPlan")}
      </text>

      {/* Colored bands and labels for each plan */}
      {usablePlans.map((plan) => {
        const style = styleMap[plan.priority] ?? styleMap.normal;
        const yMin = priceToY(Math.min(plan.zone_min!, plan.zone_max!));
        const yMax = priceToY(Math.max(plan.zone_min!, plan.zone_max!));
        const bandH = Math.max(Math.abs(yMax - yMin), 12);
        const bandY = Math.min(yMin, yMax);

        return (
          <g key={plan.label}>
            {/* Band rectangle */}
            <rect
              x={padding + bandPadding}
              y={bandY}
              width={panelWidth - padding * 2 - bandPadding}
              height={bandH}
              rx={6}
              ry={6}
              fill={style.fill}
              stroke={style.stroke}
              strokeWidth={1}
              strokeDasharray={style.dash || undefined}
            />
            {/* Plan name label (right-aligned) */}
            <text
              x={padding + panelWidth - padding - 2}
              y={bandY + bandH / 2}
              fontSize={11}
              fill={style.stroke}
              textAnchor="end"
              dominantBaseline="middle"
            >
              {futureBuyLabel(plan.label)}
            </text>
            {/* Price range label (left-aligned inside band) */}
            <text
              x={padding + bandPadding + 3}
              y={bandY + bandH / 2}
              fontSize={10}
              fill={style.stroke}
              dominantBaseline="middle"
            >
              {score(plan.zone_min)}-{score(plan.zone_max)}
            </text>
          </g>
        );
      })}

      {/* Dashed curve from latest close to plan midpoints */}
      {nonAvoidPlans.length > 0 && (() => {
        const startX = padding + 2;
        const startY = priceToY(lastClose);

        // Calculate curve points
        const curvePoints = nonAvoidPlans.map((plan) => {
          const midPrice = (Number(plan.zone_min) + Number(plan.zone_max)) / 2;
          return {
            x: padding + panelWidth - padding - 20, // right side of panel
            y: priceToY(midPrice),
          };
        });

        // Build path string
        const pathParts = [`M ${startX} ${startY}`];
        curvePoints.forEach((pt) => pathParts.push(`L ${pt.x} ${pt.y}`));

        return (
          <>
            <path
              d={pathParts.join(" ")}
              fill="none"
              stroke="#111827"
              strokeWidth={1.4}
              strokeDasharray="5,5"
              opacity={0.75}
            />
            {/* Circle at start point (close price) */}
            <circle cx={startX} cy={startY} r={3.5} fill="#111827" />
            {/* Circles at each plan midpoint */}
            {curvePoints.map((pt, i) => (
              <circle key={i} cx={pt.x} cy={pt.y} r={3.5} fill="#111827" />
            ))}
          </>
        );
      })()}
    </svg>
  );
}

export default function DetailModal({ open, onClose }: DetailModalProps) {
  const ctx = useApp();
  const detail = ctx.detail;
  const chartSurfaceRef = useRef<HTMLDivElement>(null);
  const ctxRef = useRef(ctx);
  ctxRef.current = ctx;
  const [sampleLimitInput, setSampleLimitInput] = useState("");
  const sampleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ESC key handler
  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [open, onClose]);

  // Sync sample limit input with context
  useEffect(() => {
    setSampleLimitInput(ctx.signalSampleLimit != null ? String(ctx.signalSampleLimit) : "");
  }, [ctx.signalSampleLimit]);

  // Debounced sample limit change -> reload detail
  useEffect(() => {
    if (sampleLimitInput === "") return;
    if (sampleTimer.current) clearTimeout(sampleTimer.current);
    sampleTimer.current = setTimeout(() => {
      const value = Number(sampleLimitInput);
      if (value > 0 && value !== ctx.signalSampleLimit) {
        ctx.setSignalSampleLimit(value);
        if (ctx.activeSymbolId) {
          ctx.loadSymbolDetail(ctx.activeSymbolId, { force: true }).catch(() => {});
        }
      }
    }, 500);
    return () => {
      if (sampleTimer.current) clearTimeout(sampleTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sampleLimitInput]);

  // Chart wheel handler (native listener to allow preventDefault)
  useEffect(() => {
    const el = chartSurfaceRef.current;
    if (!el) return;
    const handleWheel = (e: WheelEvent) => {
      e.preventDefault();
      const c = ctxRef.current;
      if (e.deltaY < 0) {
        c.setChartWindowSize(Math.max(20, c.chartWindowSize - 10));
      } else {
        c.setChartWindowSize(Math.min(250, c.chartWindowSize + 10));
      }
    };
    el.addEventListener("wheel", handleWheel, { passive: false });
    return () => el.removeEventListener("wheel", handleWheel);
  }, []);

  // Format trigger helpers
  const formatOpenTrigger = useCallback(
    (item: any) => {
      if (item.entry_min === null || item.entry_max === null) return "-";
      return ctx.locale === "zh-CN"
        ? `仅在 ${item.entry_min} - ${item.entry_max} 区间内开仓`
        : `Open only inside ${item.entry_min} - ${item.entry_max}`;
    },
    [ctx.locale]
  );

  const formatAddTrigger = useCallback(
    (item: any) => {
      if (!item.allow_add_position) return "-";
      return ctx.locale === "zh-CN"
        ? "满足加仓条件后分批加仓"
        : "Add in tranches when conditions met";
    },
    [ctx.locale]
  );

  const formatStopTrigger = useCallback(
    (item: any) => {
      if (item.stop_loss === null) return "-";
      return ctx.locale === "zh-CN"
        ? `跌破 ${item.stop_loss} 止损`
        : `Stop if breaks below ${item.stop_loss}`;
    },
    [ctx.locale]
  );

  const formatTrimTrigger = useCallback(
    (item: any) => {
      if (item.target_price === null) return "-";
      return ctx.locale === "zh-CN"
        ? `触及 ${item.target_price} 减仓`
        : `Trim near ${item.target_price}`;
    },
    [ctx.locale]
  );

  // Backdrop click handler
  const handleBackdropClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose]
  );

  // Chart data memo (candlestick, MA lines, volume)
  const chartData = useMemo(() => {
    if (!detail?.bars?.length) return null;
    const allBars =
      ctx.chartTimeframe === "weekly" ? aggregateWeeklyBars(detail.bars) : detail.bars;
    const windowSize = ctx.chartWindowSize;
    const bars = allBars.slice(Math.max(0, allBars.length - windowSize));
    const dates = bars.map((b) => b.trade_date);
    const closes = bars.map((b) => b.close);
    const candlestick = bars.map((b) => [b.open, b.close, b.low, b.high]);
    const volume = bars.map((b) => ({
      value: b.volume,
      itemStyle: {
        color: b.close >= b.open ? "rgba(15, 118, 110, 0.5)" : "rgba(180, 35, 24, 0.5)",
      },
    }));
    const ma10 = computeMA(closes, 10);
    const ma20 = computeMA(closes, 20);
    return { bars, dates, closes, candlestick, volume, ma10, ma20 };
  }, [detail?.bars, ctx.chartTimeframe, ctx.chartWindowSize]);

  // Radar option memo
  const radarOption = useMemo(() => {
    if (!detail?.latest_score) return null;
    const s = detail.latest_score;
    const values = [
      clamp(Number(s.trend_score ?? 0), 0, 100),
      clamp(Number(s.momentum_score ?? 0), 0, 100),
      clamp(Number(s.volatility_score ?? 0), 0, 100),
      clamp(Number(s.liquidity_score ?? 0), 0, 100),
      clamp(Number(s.breadth_score ?? 0), 0, 100),
      clamp(Number(s.event_score ?? 0), 0, 100),
    ];
    return {
      radar: {
        indicator: [
          { name: t("trendScore"), max: 100 },
          { name: t("momentumScore"), max: 100 },
          { name: t("volatilityScore"), max: 100 },
          { name: t("liquidityScore"), max: 100 },
          { name: t("breadthScore"), max: 100 },
          { name: t("eventScore"), max: 100 },
        ],
      },
      series: [
        {
          type: "radar",
          data: [{ value: values }],
          areaStyle: { color: "rgba(15, 118, 110, 0.25)" },
          lineStyle: { color: "#0f766e" },
          itemStyle: { color: "#0f766e" },
        },
      ],
    };
  }, [detail?.latest_score]);

  // Active future buy plan (scenario-filtered)
  const activeFutureBuyPlan = useMemo(() => {
    const setup = detail?.latest_trade_setup ?? null;
    if (!setup) return [];
    const totalCapital = ctx.workbench?.portfolio?.total_capital ?? 0;
    const investableRatio = ctx.workbench?.portfolio?.investable_ratio ?? 1;
    return getActiveFutureBuyPlan(setup, ctx.futurePlanScenario, ctx.futurePlanCustom, totalCapital, investableRatio);
  }, [detail?.latest_trade_setup, ctx.futurePlanScenario, ctx.futurePlanCustom, ctx.workbench?.portfolio]);

  // Candlestick chart option memo
  const chartOption = useMemo(() => {
    if (!chartData) return null;
    const signals = detail?.latest_trade_setup?.chart_signals ?? [];
    const markLineData = signals.map((sig) => {
      // Distinct colors per signal kind: stop=red, target=teal, buy-zone=purple, default=green
      const colorMap: Record<string, string> = {
        stop: "#b42318",
        target: "#0f766e",
        "buy-zone": "#7c3aed",
      };
      const lineColor = colorMap[sig.kind] ?? "#059669";
      return {
        yAxis: sig.price,
        label: {
          formatter: signalLabel(sig),
          position: "end" as const,
          backgroundColor: sig.kind === "buy-zone" ? "rgba(124,58,237,0.12)" : sig.kind === "stop" ? "rgba(180,35,24,0.08)" : undefined,
          borderColor: lineColor,
          borderWidth: 1,
          borderRadius: 4,
          padding: [2, 6],
          color: lineColor,
        },
        lineStyle: {
          color: lineColor,
          type: sig.kind === "stop" ? "dashed" : "dashed",
          width: sig.kind === "buy-zone" ? 1.5 : 1,
        },
      };
    });

    // Compute price domain for Y-axis mapping and future plan overlay
    const allPrices = chartData.candlestick.flatMap((c: number[]) => [c[1], c[2], c[3], c[4]]);
    const maValues = [...chartData.ma10, ...chartData.ma20].filter((v): v is number => v !== null);
    const signalPrices = signals.map((s) => s.price);
    const futureZones = activeFutureBuyPlan.flatMap((p) =>
      p.zone_min !== null && p.zone_max !== null ? [p.zone_min, p.zone_max] : []
    );
    const allRelevant = [...allPrices, ...maValues, ...signalPrices, ...futureZones].filter((v): v is number => v != null && !isNaN(v));
    const priceMin = Math.min(...allRelevant) * 0.985;
    const priceMax = Math.max(...allRelevant) * 1.015;
    const priceSpan = Math.max(priceMax - priceMin, 0.01);

    // Future plan panel dimensions - reserve space on right side of grid
    const hasFuturePlans = activeFutureBuyPlan.length > 0;
    const futurePanelWidth = hasFuturePlans ? 168 : 0;

    return {
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (params: any) => {
          if (!params || !Array.isArray(params) || params.length === 0) return "";
          const p = Array.isArray(params) ? params[0] : params;
          const date = p.axisValue ?? "";
          const kData = chartData.candlestick[p.dataIndex];
          if (!kData) return date;
          // ECharts candlestick format: [open, close, low, high]
          const [open, close, low, high] = kData;
          const volRaw = chartData.volume?.[p.dataIndex];
          const vol = typeof volRaw === "number" ? volRaw : Number(volRaw?.value ?? volRaw ?? 0);
          const ma10Val = chartData.ma10?.[p.dataIndex];
          const ma20Val = chartData.ma20?.[p.dataIndex];
          let html = `<div style="font-size:12px;font-weight:bold;margin-bottom:6px">${date}</div>`;
          html += `<div style="display:flex;align-items:center;gap:4px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#0f766e"></span><span>K</span></div>`;
          html += `<div style="padding-left:14px">${t("chartOpen")}: ${score(open)}</div>`;
          html += `<div style="padding-left:14px">${t("chartClose")}: ${score(close)}</div>`;
          html += `<div style="padding-left:14px">${t("chartLow")}: ${score(low)}</div>`;
          html += `<div style="padding-left:14px">${t("chartHigh")}: ${score(high)}</div>`;
          if (vol > 0) {
            const volWan = formatVolume(vol);
            html += `<div style="display:flex;align-items:center;gap:4px;margin-top:4px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#94a3b8"></span><span>${t("volume")}: ${volWan}</span></div>`;
          }
          if (ma10Val != null) html += `<div style="color:#f59e0b;padding-left:14px">MA10: ${score(ma10Val)}</div>`;
          if (ma20Val != null) html += `<div style="color:#3b82f6;padding-left:14px">MA20: ${score(ma20Val)}</div>`;
          return html;
        },
      },
      legend: { data: ["K", "MA10", "MA20", t("volume")], top: 0 },
      grid: { left: 50, right: futurePanelWidth + 16, top: 30, bottom: 50 },
      xAxis: { type: "category", data: chartData.dates, axisLabel: { fontSize: 10 } },
      yAxis: [
        { type: "value", scale: true, min: priceMin, max: priceMax, axisLabel: { fontSize: 10 } },
        {
          type: "value",
          scale: true,
          axisLabel: {
            fontSize: 10,
            formatter: (val: any) => formatVolume(typeof val === "number" ? val : Number(val?.value ?? val)),
          },
          splitLine: { show: false },
        },
      ],
      dataZoom: [{ type: "slider", start: 0, end: 100, height: 20, bottom: 8 }],
      series: [
        {
          name: "K",
          type: "candlestick",
          data: chartData.candlestick,
          itemStyle: {
            color: "#0f766e",
            color0: "#b42318",
            borderColor: "#0f766e",
            borderColor0: "#b42318",
          },
          markLine: markLineData.length ? { symbol: "none", data: markLineData } : undefined,
        },
        {
          name: "MA10",
          type: "line",
          data: chartData.ma10,
          smooth: true,
          lineStyle: { width: 1, color: "#f59e0b" },
          symbol: "none",
        },
        {
          name: "MA20",
          type: "line",
          data: chartData.ma20,
          smooth: true,
          lineStyle: { width: 1, color: "#3b82f6" },
          symbol: "none",
        },
        {
          name: t("volume"),
          type: "bar",
          yAxisIndex: 1,
          data: chartData.volume,
        },
      ],
    };
  }, [chartData, detail?.latest_trade_setup, activeFutureBuyPlan]);

  if (!detail) return null;

  const symbol = detail.symbol;
  const latestScore = detail.latest_score;
  const setup = detail.latest_trade_setup;
  const signalStats = detail.signal_stats;
  const position = detail.position;
  const scoreHistory = detail.score_history ?? [];
  const lastBar = detail.bars?.[detail.bars.length - 1];

  const title = `${symbol.symbol} | ${symbol.name}`;
  const meta = `${t("marketLabel")}: ${symbol.market}${DOT}${t("regionLabel")}: ${regionLongLabel(
    symbol.region
  )}${DOT}${t("assetLabel")}: ${assetTypeLabel(symbol.asset_type)}`;

  // Entry price & quantity (must be computed before scenarios which depends on them)
  const baseScenarios = setup?.return_scenarios;
  const _refPrice = baseScenarios?.reference_price ?? computeSuggestedPrice(detail);
  const entryPrice = Number(ctx.simPrice) > 0 ? Number(ctx.simPrice) : Number(_refPrice);
  const quantity = Number(ctx.simQuantity) > 0
    ? Number(ctx.simQuantity)
    : Number(baseScenarios?.planned_order?.quantity ?? 0);

  // Order scenario preview - adjusted by active future plan scenario
  const scenarios = useMemo(() => {
    if (!baseScenarios) return null;
    const refPrice = baseScenarios.reference_price ?? entryPrice;
    if (!(refPrice > 0)) return baseScenarios;

    const baseRiskUnit = Math.max(refPrice * 0.015, 0.01);
    const baseStop = setup?.stop_loss;
    const riskFromStop = baseStop != null && baseStop < refPrice ? refPrice - baseStop : baseRiskUnit;
    const effectiveRisk = Math.max(baseRiskUnit, riskFromStop);

    // Scenario-specific adjustments
    const scenarioConfig: Record<string, { horizonMult: number; targetMult: number; stopMult: number; confidenceAdj: number }> = {
      general: { horizonMult: 1.0, targetMult: 1.0, stopMult: 1.0, confidenceAdj: 0 },
      short:   { horizonMult: 0.25, targetMult: 0.50, stopMult: 0.65, confidenceAdj: -8 },  // 短线：紧目标、紧止损、低信心
      mid:     { horizonMult: 0.75, targetMult: 0.82, stopMult: 0.88, confidenceAdj: -3 },  // 中期：略收缩
      long:    { horizonMult: 1.5,  targetMult: 1.30, stopMult: 1.20, confidenceAdj: 5 },   // 长线：宽目标、宽止损、高信心
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
        <span className="item-subline">
          {t("projectedValue")}: {money(projectedValue)}
        </span>
        <span className="item-subline">
          {t("target")}: {score(exitPrice)}
        </span>
      </div>
    );
  };

  const lastMAValue = (arr: (number | null)[]) => {
    const v = arr[arr.length - 1];
    return v !== null && v !== undefined ? score(v) : "-";
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width="100vw"
      title={
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontWeight: 700, fontSize: 16 }}>{title}</span>
          <span style={{ color: "var(--muted)", fontSize: 13 }}>{meta}</span>
        </div>
      }
      footer={null}
      closable={true}
      maskClosable={true}
      styles={{
        body: { padding: "12px 24px 24px", maxHeight: "calc(90vh - 56px)", overflowY: "auto", overflowX: "hidden" },
        header: { borderBottom: "1px solid var(--line)", padding: "12px 24px" },
      }}
      destroyOnClose
    >
      <div className="panel wide detail-panel">
          {/* Detail Dock: Recent viewed symbols */}
          <div className="detail-dock">
            <div className="detail-dock-head">
              <h3>{t("recentViewed")}</h3>
            </div>
            <div className="detail-quick-rail">
              {ctx.detailOrder.map((symbolId) => {
                const cached = ctx.detailCache[symbolId];
                if (!cached) return null;
                const isActive = symbolId === ctx.activeSymbolId;
                const cs = cached.latest_score;
                const cb = cached.bars?.[cached.bars.length - 1];
                return (
                  <div
                    key={symbolId}
                    className={`detail-chip ${isActive ? "active" : ""}`}
                    onClick={() => ctx.loadSymbolDetail(symbolId)}
                  >
                    <div className="detail-chip-top">
                      <div>
                        <div className="detail-chip-title">{cached.symbol.symbol}</div>
                        <div className="detail-chip-meta">{cached.symbol.name}</div>
                      </div>
                      <Button
                        size="small"
                        type="text"
                        className="detail-chip-close"
                        onClick={(e) => {
                          e.stopPropagation();
                          ctx.removeDetailFromDock(symbolId);
                        }}
                      >
                        ×
                      </Button>
                    </div>
                    <div className="detail-chip-note">
                      {joinParts([
                        regionLongLabel(cached.symbol.region),
                        assetTypeLabel(cached.symbol.asset_type),
                        cs ? stageLabel(cs.stage) : null,
                        cs ? actionLabel(cs.action) : null,
                      ])}
                    </div>
                    <div className="detail-chip-note">
                      {joinParts([
                        cs ? `${t("quality")}: ${score(cs.quality_score)}` : null,
                        cs ? `${t("timing")}: ${score(cs.timing_score)}` : null,
                        cb ? `${t("close")}: ${score(cb.close)}` : null,
                      ])}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="detail-grid">
            {/* Latest Assessment */}
            <div className="detail-card">
              <h3>{t("latestAssessment")}</h3>
              <div className="list">
                {latestScore && (
                  <>
                    <div className="item-subline">
                      {joinParts([
                        `${t("date")}: ${latestScore.trade_date}`,
                        `${t("quality")}: ${score(latestScore.quality_score)}`,
                        `${t("timing")}: ${score(latestScore.timing_score)}`,
                      ])}
                    </div>
                    <div className="item-subline">
                      {joinParts([
                        `${t("stage")}: ${stageLabel(latestScore.stage)}`,
                        `${t("action")}: ${actionLabel(latestScore.action)}`,
                      ])}
                    </div>
                  </>
                )}
                {signalStats && (
                  <div className="detail-card" style={{ marginTop: 10 }}>
                    <p className="panel-kicker">{t("similarSignalStats")}</p>
                    <div className="item-subline">
                      {joinParts([
                        `${t("similarSamples")}: ${signalStats.sample_count}`,
                        `${t("matchedSignals")}: ${signalStats.matched_count}`,
                      ])}
                    </div>
                    <div className="item-subline">
                      {joinParts([
                        `${t("win5d")}: ${statPct(signalStats.win_rate_5d)}`,
                        `${t("win20d")}: ${statPct(signalStats.win_rate_20d)}`,
                        `${t("avgReturn20d")}: ${statPct(signalStats.avg_return_20d)}`,
                      ])}
                    </div>
                    <div className="item-subline">
                      {joinParts([
                        `${t("maxGain20d")}: ${statPct(signalStats.avg_max_gain_20d)}`,
                        `${t("maxDrawdown20d")}: ${statPct(signalStats.avg_max_drawdown_20d)}`,
                      ])}
                    </div>
                    <div className="item-subline">
                      {joinParts([
                        `${t("best20d")}: ${statPct(signalStats.best_return_20d)}`,
                        `${t("worst20d")}: ${statPct(signalStats.worst_return_20d)}`,
                      ])}
                    </div>
                    <label
                      style={{
                        display: "grid",
                        gap: 4,
                        marginTop: 8,
                        fontSize: 12,
                        color: "var(--muted)",
                      }}
                    >
                      <span>{t("sampleLimit")}</span>
                      <input
                        type="number"
                        min={1}
                        value={sampleLimitInput}
                        onChange={(e) => setSampleLimitInput(e.target.value)}
                        placeholder={t("sampleLimitTip")}
                      />
                    </label>
                  </div>
                )}
                {position && (
                  <div className="detail-card" style={{ marginTop: 10 }}>
                    <p className="panel-kicker">{t("currentPosition")}</p>
                    <div className="item-subline">
                      {joinParts([
                        `${t("weight")}: ${percent(position.position_pct)}`,
                        `${t("holdingQty")}: ${position.quantity}`,
                      ])}
                    </div>
                    <div className="item-subline">
                      {joinParts([
                        `${t("avgCost")}: ${score(position.avg_cost)}`,
                        `${t("currentPrice")}: ${score(position.latest_price)}`,
                      ])}
                    </div>
                    <div className="item-subline">
                      {t("accountMarketValue")}: {money(position.market_value)}
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* Score Radar (ECharts) */}
            <div className="detail-card">
              <h3>{t("scoreRadar")}</h3>
              <div id="detailRadar" className="radar-canvas">
                {radarOption && (
                  <ReactECharts option={radarOption} style={{ height: "260px", width: "100%" }} />
                )}
              </div>
            </div>

            {/* Trade Setup */}
            <div className="detail-card wide">
              <div className="detail-card-head">
                <h3>{t("tradeSetup")}</h3>
                <div className="detail-actions">
                  <Button
                    size="small"
                    onClick={() => ctx.generateTradeSetup()}
                  >
                    {t("refreshPlan")}
                  </Button>
                </div>
              </div>

              {/* Order scenario preview */}
              {scenarios && (
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
              )}

              <div className="list" id="detailSetup">
                {setup ? (
                  <>
                    {/* Compact: buy zone + stop + target on one line */}
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

                    {/* Compact: position info + risk */}
                    <div className="item-subline">
                      {joinParts([
                        `${t("recommendedPosition")}: ${percent(setup.recommended_position_pct)}`,
                        `${t("positionAmount")}: ${money(setup.recommended_position_amount)}`,
                        `${t("riskReward")}: ${setup.risk_reward_ratio != null ? score(setup.risk_reward_ratio) : "-"}`,
                        `${t("allowAdd")}: ${setup.allow_add_position ? t("yes") : t("no")}`,
                      ])}
                    </div>

                    {/* Compact: stage/cap/position/risk in one line */}
                    <div className="item-subline">
                      {joinParts([
                        `${t("stageCap")}: ${setup.stage_cap_pct != null ? percent(setup.stage_cap_pct) : "-"}`,
                        `${t("stageRoom")}: ${setup.remaining_stage_pct != null ? percent(setup.remaining_stage_pct) : "-"}`,
                        `${t("currentPosition")}: ${setup.current_position_pct != null ? percent(setup.current_position_pct) : "-"}`,
                        `${t("riskBudget")}: ${setup.risk_budget_amount != null ? money(setup.risk_budget_amount) : "-"}`,
                        `${t("riskShare")}: ${setup.risk_per_share != null ? score(setup.risk_per_share) : "-"}`,
                      ])}
                    </div>

                    {/* Stage + action + guardrails */}
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

                    {/* Triggers - compact single line each */}
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
                              <Button
                                key={sc}
                                size="small"
                                type={ctx.futurePlanScenario === sc ? "primary" : "default"}
                                onClick={() => ctx.setFuturePlanScenario(sc)}
                              >
                                {t(
                                  `futureScenario${
                                    sc.charAt(0).toUpperCase() + sc.slice(1)
                                  }`
                                )}
                              </Button>
                            ))}
                          </div>
                        </div>
                        {ctx.futurePlanScenario === "custom" && (
                          <div className="future-custom-grid">
                            <label>
                              <span>{t("customHorizon")}</span>
                              <input
                                type="number"
                                value={ctx.futurePlanCustom.horizonDays}
                                onChange={(e) =>
                                  ctx.setFuturePlanCustom({
                                    ...ctx.futurePlanCustom,
                                    horizonDays: Number(e.target.value),
                                  })
                                }
                              />
                            </label>
                            <label>
                              <span>{t("customPullback")}</span>
                              <input
                                type="number"
                                value={ctx.futurePlanCustom.pullbackPct}
                                onChange={(e) =>
                                  ctx.setFuturePlanCustom({
                                    ...ctx.futurePlanCustom,
                                    pullbackPct: Number(e.target.value),
                                  })
                                }
                              />
                            </label>
                            <label>
                              <span>{t("customPosition")}</span>
                              <input
                                type="number"
                                value={ctx.futurePlanCustom.positionPct}
                                onChange={(e) =>
                                  ctx.setFuturePlanCustom({
                                    ...ctx.futurePlanCustom,
                                    positionPct: Number(e.target.value),
                                  })
                                }
                              />
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

            {/* Recent Bars (Candlestick Chart - ECharts) */}
            <div
              className={`detail-card wide ${ctx.chartExpanded ? "chart-card-expanded" : ""}`}
              id="chartCard"
            >
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
                      <Button
                        size="small"
                        type={ctx.chartTimeframe === "daily" ? "primary" : "default"}
                        onClick={() => ctx.setChartTimeframe("daily")}
                      >
                        {t("chartDaily")}
                      </Button>
                      <Button
                        size="small"
                        type={ctx.chartTimeframe === "weekly" ? "primary" : "default"}
                        onClick={() => ctx.setChartTimeframe("weekly")}
                      >
                        {t("chartWeekly")}
                      </Button>
                    </div>
                    <span className="chart-window-pill">
                      {ctx.chartWindowSize}
                      {t("barsUnit")}
                    </span>
                    <Button
                      size="small"
                      onClick={() =>
                        ctx.setChartWindowSize(Math.max(20, ctx.chartWindowSize - 20))
                      }
                    >
                      {t("zoomIn")}
                    </Button>
                    <Button
                      size="small"
                      onClick={() =>
                        ctx.setChartWindowSize(Math.min(250, ctx.chartWindowSize + 20))
                      }
                    >
                      {t("zoomOut")}
                    </Button>
                    <Button
                      size="small"
                      onClick={() => {
                        ctx.setChartWindowSize(60);
                        ctx.setChartRange(null);
                      }}
                    >
                      {t("resetZoom")}
                    </Button>
                    <Button
                      size="small"
                      onClick={() => ctx.setChartExpanded(!ctx.chartExpanded)}
                    >
                      {ctx.chartExpanded ? t("collapseChart") : t("expandChart")}
                    </Button>
                  </div>
                </div>
              </div>
              <p className="panel-meta chart-hint" id="chartHint">
                {t("chartDragHint")}
              </p>
              <div
                ref={chartSurfaceRef}
                id="detailChart"
                className={`chart-surface ${ctx.chartExpanded ? "expanded" : ""}`}
              >
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
                      {/* Future Buy Plan SVG Overlay - positioned over right side of chart */}
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
                          <span>
                            O:{score(lastBar.open)} H:{score(lastBar.high)} L:
                            {score(lastBar.low)} C:{score(lastBar.close)}
                          </span>
                        )}
                        <span>
                          {t("ma10")}: {lastMAValue(chartData.ma10)}
                        </span>
                        <span>
                          {t("ma20")}: {lastMAValue(chartData.ma20)}
                        </span>
                      </div>
                    )}
                  </>
                ) : (
                  <div className="empty">{t("noChart")}</div>
                )}
              </div>
            </div>

            {/* Score History */}
            <div className="detail-card wide">
              <h3>{t("scoreHistory")}</h3>
              <table>
                <thead>
                  <tr>
                    <th style={{width:110}}>{t("date")}</th>
                    <th style={{width:80}}>{t("quality")}</th>
                    <th style={{width:80}}>{t("timing")}</th>
                    <th style={{width:100}}>{t("stage")}</th>
                    <th style={{width:100}}>{t("action")}</th>
                  </tr>
                </thead>
                <tbody>
                  {scoreHistory.map((row: any) => (
                  <tr key={row.id}>
                    <td>{row.trade_date}</td>
                    <td>{score(row.quality_score)}</td>
                    <td>{score(row.timing_score)}</td>
                    <td><span className={badgeClass(row.stage)}>{stageLabel(row.stage)}</span></td>
                    <td><span className={badgeClass(row.action)}>{actionLabel(row.action)}</span></td>
                  </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Recent Trades */}
            <div className="detail-card wide">
              <h3>{t("recentTrades")}</h3>
              <div className="list" id="detailTrades">
                {detail.recent_trades && detail.recent_trades.length > 0 ? (
                  detail.recent_trades.map((trade: TradeRecord) => (
                    <article key={trade.id} className="list-item">
                      <div className="item-topline">
                        <strong>{trade.symbol}{DOT}{trade.name}</strong>
                        <span className={sideBadgeClass(trade.side)}>{sideLabel(trade.side)}</span>
                      </div>
                      <div className="item-subline">
                        {joinParts([
                          `${t("orderQty")}: ${trade.quantity}`,
                          `${t("orderPrice")}: ${score(trade.price)}`,
                          `${t("positionAmount")}: ${money(trade.amount)}`,
                        ])}
                      </div>
                      <div className={`item-subline ${pnlClass(trade.realized_pnl)}`}>
                        {joinParts([
                          `${t("accountRealizedPnl")}: ${money(trade.realized_pnl)}`,
                          formatDate(trade.created_at),
                        ])}
                      </div>
                    </article>
                  ))
                ) : (
                  <div className="empty">{t("noTrades")}</div>
                )}
              </div>
            </div>

            {/* Journals */}
            <div className="detail-card wide">
              <h3>{t("journals")}</h3>
              <div className="list" id="detailJournals">
                {detail.journals && detail.journals.length > 0 ? (
                  detail.journals.map((journal: JournalEntry) => (
                    <article key={journal.id} className="list-item">
                      <div className="item-topline">
                        <strong>{journal.title}</strong>
                        <span className="badge">{journal.entry_type}</span>
                      </div>
                      <div className="item-subline">{formatDate(journal.created_at)}</div>
                    </article>
                  ))
                ) : (
                  <div className="empty">{t("noJournals")}</div>
                )}
              </div>
            </div>
          </div>
      </div>
    </Modal>
  );
}
