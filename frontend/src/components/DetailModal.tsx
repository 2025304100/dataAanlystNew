import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";
import ReactECharts from "echarts-for-react";
import { Modal, Button, Input, Select, Tag, Space, Tooltip } from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
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
  roundPrice,
  aggregateWeeklyBars,
  computeSuggestedPrice,
  signalLabel,
  formatDate,
  sideBadgeClass,
} from "../utils/format";
import type { FutureBuyPlan, TradeSetup, JournalEntry, TradeRecord } from "../types";
import { useChartDrawings } from "../hooks/useChartDrawings";
// 复用 utils/indicators 中的共享指标工具，避免重复定义
import { computeMA } from "../utils/indicators";
// 复用 constants/chartTheme 中统一导出的图表样式与颜色映射
import { FUTURE_PLAN_STYLE_MAP, SIGNAL_COLOR_MAP } from "../constants/chartTheme";

interface DetailModalProps {
  open: boolean;
  onClose: () => void;
}

// 说明：此处保留本地 formatVolume 实现，未与 utils/indicators.formatVolume 统一。
// 本版本使用 t() 进行 i18n 本地化（亿/万单位随语言切换），而 indicators 版本为硬编码中文，
// 两者行为存在差异，故保持现状不强行替换。
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

function setupDecisionTone(setup: TradeSetup): "ok" | "wait" | "blocked" {
  if (setup.can_open || setup.decision === "buy_allowed") return "ok";
  if (setup.decision === "blocked") return "blocked";
  return "wait";
}

function setupDecisionLabel(setup: TradeSetup): string {
  const decision = setup.decision ?? (setup.can_open ? "buy_allowed" : "wait");
  if (decision === "buy_allowed") return t("buyAllowed");
  if (decision === "blocked") return t("blocked");
  return t("waitSignal");
}

function constraintLabel(key: string): string {
  const label = t(`constraint_${key}`);
  return label === `constraint_${key}` ? key : label;
}

function blockedReasonLabel(reason: string): string {
  const label = t(`blocked_${reason}`);
  return label === `blocked_${reason}` ? reason : label;
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

  // 复用 constants/chartTheme 中统一导出的样式映射，避免重复定义
  const styleMap = FUTURE_PLAN_STYLE_MAP;

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
  const [scoreBackfillStarting, setScoreBackfillStarting] = useState(false);
  const sampleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── P3: Review management state ──
  const [reviewFormOpen, setReviewFormOpen] = useState(false);
  const [reviewTitle, setReviewTitle] = useState("");
  const [reviewType, setReviewType] = useState("review");
  const [reviewActualAction, setReviewActualAction] = useState("follow_system");
  const [reviewOutcome, setReviewOutcome] = useState("pending");
  const [reviewNote, setReviewNote] = useState("");
  const [reviewTags, setReviewTags] = useState<string[]>([]);
  const [reviewEditingId, setReviewEditingId] = useState<number | null>(null);
  const [reviewSaving, setReviewSaving] = useState(false);
  const [reviewList, setReviewList] = useState<JournalEntry[]>([]);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [deletingReviewId, setDeletingReviewId] = useState<number | null>(null);
  const [refreshingPlan, setRefreshingPlan] = useState(false);
  const [signalValidationStats, setSignalValidationStats] = useState<any>(null);

  // ── Chart drawing tools ──
  const {
    drawings,
    drawingMode,
    pendingPoint,
    handleChartClick: drawingClickHandler,
    startDrawing,
    cancelDrawing,
    removeLastDrawing,
    clearAllDrawings,
    drawingLineSeries,
    pendingSeries,
    drawingPrices,
  } = useChartDrawings(ctx.activeSymbolId);

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

  const handleLocalScoreBackfill = async () => {
    const symbolId = ctx.activeSymbolId;
    if (!symbolId || scoreBackfillStarting) return;
    setScoreBackfillStarting(true);
    try {
      await api.startHistoryInitialization({
        preset: "3y",
        adjust: "qfq",
        symbol_ids: [symbolId],
        repair_mode: "scores",
        symbol_source: "all",
        auto_scan: false,
        portfolio_id: ctx.portfolioId,
      });
      ctx.showToast(
        "success",
        ctx.locale === "zh-CN"
          ? "已启动本地评分回补：只读取现有K线，不会重新拉取行情。"
          : "Local score backfill started. Existing bars will be used without refetching market data."
      );
    } catch (error: any) {
      ctx.showToast(
        "error",
        error?.message || (ctx.locale === "zh-CN" ? "评分回补启动失败" : "Failed to start score backfill")
      );
    } finally {
      setScoreBackfillStarting(false);
    }
  };

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
        c.setChartWindowSize(Math.min(500, c.chartWindowSize + 10));
      }
    };
    el.addEventListener("wheel", handleWheel, { passive: false });
    return () => el.removeEventListener("wheel", handleWheel);
  }, []);

  // ── P3: Load review list & signal validation stats when symbol changes ──
  const reviewPortfolioId = ctx.portfolioId ?? 1;
  const reviewSymbolId = ctx.activeSymbolId;

  const loadReviewList = useCallback(async () => {
    if (!reviewSymbolId) {
      setReviewList([]);
      return;
    }
    try {
      setReviewLoading(true);
      const data = await api.getJournals(reviewPortfolioId, reviewSymbolId);
      setReviewList(Array.isArray(data) ? (data as JournalEntry[]) : []);
    } catch {
      setReviewList([]);
    } finally {
      setReviewLoading(false);
    }
  }, [reviewPortfolioId, reviewSymbolId]);

  const loadSignalValidationStats = useCallback(async () => {
    if (!reviewSymbolId) {
      setSignalValidationStats(null);
      return;
    }
    try {
      const data = await api.getSignalStats(reviewSymbolId, reviewPortfolioId);
      setSignalValidationStats(data ?? null);
    } catch {
      setSignalValidationStats(null);
    }
  }, [reviewPortfolioId, reviewSymbolId]);

  useEffect(() => {
    if (!open || !reviewSymbolId) {
      setReviewList([]);
      setSignalValidationStats(null);
      return;
    }
    loadReviewList();
    loadSignalValidationStats();
  }, [open, reviewSymbolId, loadReviewList, loadSignalValidationStats]);

  // Reset review form when opening fresh
  const resetReviewForm = useCallback(() => {
    setReviewTitle("");
    setReviewType("review");
    setReviewActualAction("follow_system");
    setReviewOutcome("pending");
    setReviewNote("");
    setReviewTags([]);
    setReviewEditingId(null);
  }, []);

  const handleStartEditReview = useCallback((journal: JournalEntry) => {
    setReviewEditingId(journal.id);
    setReviewTitle(journal.title || "");
    setReviewType(journal.entry_type || "review");
    setReviewActualAction(journal.actual_action || "follow_system");
    setReviewOutcome(journal.outcome || "pending");
    setReviewNote(journal.review_note || "");
    try {
      setReviewTags(journal.review_tags ? JSON.parse(journal.review_tags) : []);
    } catch {
      setReviewTags([]);
    }
    setReviewFormOpen(true);
  }, []);

  const handleSubmitReview = useCallback(async () => {
    if (!reviewSymbolId) return;
    const title = reviewTitle.trim();
    if (!title) {
      ctx.showToast("error", t("reviewSaveFailed"));
      return;
    }
    const score = detail?.latest_score;
    const payload: any = {
      portfolio_id: reviewPortfolioId,
      symbol_id: reviewSymbolId,
      title,
      entry_type: reviewType,
      actual_action: reviewActualAction,
      outcome: reviewOutcome,
      review_note: reviewNote,
      review_tags: reviewTags,
      score_id: score?.id ?? null,
      stage: score?.stage ?? null,
      action: score?.action ?? null,
    };
    try {
      setReviewSaving(true);
      if (reviewEditingId != null) {
        await api.updateJournal(reviewEditingId, payload);
        ctx.showToast("success", t("reviewUpdated"));
      } else {
        await api.createJournal(payload);
        ctx.showToast("success", t("reviewCreated"));
      }
      resetReviewForm();
      setReviewFormOpen(false);
      await loadReviewList();
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("reviewSaveFailed"));
    } finally {
      setReviewSaving(false);
    }
  }, [
    reviewSymbolId,
    reviewTitle,
    reviewType,
    reviewActualAction,
    reviewOutcome,
    reviewNote,
    reviewEditingId,
    reviewPortfolioId,
    detail?.latest_score,
    ctx,
    resetReviewForm,
    loadReviewList,
  ]);

  const handleDeleteReview = useCallback(
    async (journalId: number) => {
      setDeletingReviewId(journalId);
      try {
        await api.deleteJournal(journalId);
        ctx.showToast("success", t("reviewDeleted"));
        await loadReviewList();
      } catch (error: any) {
        ctx.showToast("error", error?.message || t("reviewSaveFailed"));
      } finally {
        setDeletingReviewId(null);
      }
    },
    [ctx, loadReviewList]
  );

  const handleRefreshPlan = useCallback(async () => {
    setRefreshingPlan(true);
    try {
      await ctx.generateTradeSetup();
    } catch (error: any) {
      // generateTradeSetup 内部已显示 error Toast，避免重复
    } finally {
      setRefreshingPlan(false);
    }
  }, [ctx]);

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
      tooltip: {
        trigger: "item",
        valueFormatter: (value: number) => score(value),
      },
      animationDuration: 500,
      radar: {
        center: ["50%", "52%"],
        radius: "68%",
        splitNumber: 4,
        indicator: [
          { name: t("trendScore"), max: 100 },
          { name: t("momentumScore"), max: 100 },
          { name: t("volatilityScore"), max: 100 },
          { name: t("liquidityScore"), max: 100 },
          { name: t("breadthScore"), max: 100 },
          { name: t("eventScore"), max: 100 },
        ],
        axisName: { color: "#7b8490", fontSize: 12 },
        axisLine: { lineStyle: { color: "rgba(15, 118, 110, 0.20)" } },
        splitLine: { lineStyle: { color: "rgba(15, 118, 110, 0.16)" } },
        splitArea: {
          areaStyle: { color: ["rgba(255,255,255,0.74)", "rgba(15,118,110,0.035)"] },
        },
      },
      series: [
        {
          type: "radar",
          data: [{ value: values }],
          symbolSize: 7,
          areaStyle: { color: "rgba(15, 118, 110, 0.20)" },
          lineStyle: { color: "#0f766e", width: 2.5 },
          itemStyle: { color: "#0f766e" },
        },
      ],
    };
  }, [detail?.latest_score]);

  // ════════════════════════════════════════════════
  // Score Breakdown: 分项评分权重+贡献+解释
  // ════════════════════════════════════════════════
  const scoreBreakdown = useMemo(() => {
    if (!detail?.latest_score) return null;
    const s = detail.latest_score;
    const isZh = ctx.locale === "zh-CN";

    // P1：优先使用评分时保存的配置快照 + dimension_scores_json
    let dimScoresMap: Record<string, number> = {};
    let configSnapshot: { dimensions?: Array<{ key: string; name: string; enabled?: boolean; weight?: number; score_bucket?: string }> } | null = null;
    try {
      if (s.dimension_scores_json) dimScoresMap = JSON.parse(s.dimension_scores_json);
      if (s.scoring_config_snapshot_json) configSnapshot = JSON.parse(s.scoring_config_snapshot_json);
    } catch { /* ignore */ }

    const hasConfigSnapshot = !!(configSnapshot && configSnapshot.dimensions && configSnapshot.dimensions.length > 0 && Object.keys(dimScoresMap).length > 0);

    // 分项配置：权重 + 解释规则
    const legacyFactors = [
      {
        key: "trend",
        label: t("trendScore"),
        score: Number(s.trend_score ?? 0),
        weight: 0.25,
        explain: (v: number) =>
          v >= 70 ? (isZh ? "趋势向上突破MA50，均线多头排列" : "Trend breakout above MA50, bullish alignment")
          : v >= 55 ? (isZh ? "趋势偏强，价格在MA50上方" : "Moderate trend, price above MA50")
          : v >= 40 ? (isZh ? "趋势中性，均线纠缠" : "Neutral trend, MA lines intertwined")
          : (isZh ? "趋势偏弱，价格低于MA50" : "Weak trend, price below MA50"),
      },
      {
        key: "momentum",
        label: t("momentumScore"),
        score: Number(s.momentum_score ?? 0),
        weight: 0.20,
        explain: (v: number) =>
          v >= 70 ? (isZh ? "动量强劲，20日涨幅>8%" : "Strong momentum, 20-day gain >8%")
          : v >= 55 ? (isZh ? "动量偏多，短期有上涨动能" : "Moderate momentum, bullish short-term")
          : v >= 40 ? (isZh ? "动量中性，涨跌平衡" : "Neutral momentum")
          : (isZh ? "动量偏空，短期下跌趋势" : "Weak momentum, bearish short-term"),
      },
      {
        key: "volatility",
        label: t("volatilityScore"),
        score: Number(s.volatility_score ?? 0),
        weight: 0.15,
        explain: (v: number) =>
          v >= 65 ? (isZh ? "波动健康，涨跌有序无异常震荡" : "Healthy volatility, orderly movement")
          : v >= 50 ? (isZh ? "波动适中，风险可控" : "Moderate volatility")
          : v >= 35 ? (isZh ? "波动偏大，需注意止损" : "Higher volatility, watch stop-loss")
          : (isZh ? "波动剧烈，风险较高" : "High volatility, high risk"),
      },
      {
        key: "liquidity",
        label: t("liquidityScore"),
        score: Number(s.liquidity_score ?? 0),
        weight: 0.15,
        explain: (v: number) =>
          v >= 60 ? (isZh ? "流动性充足，成交活跃" : "High liquidity, active trading")
          : v >= 45 ? (isZh ? "流动性适中，交易正常" : "Moderate liquidity")
          : v >= 30 ? (isZh ? "流动性偏低，买卖可能滑点" : "Lower liquidity, possible slippage")
          : (isZh ? "流动性不足，交易困难" : "Low liquidity, hard to trade"),
      },
      {
        key: "breadth",
        label: t("breadthScore"),
        score: Number(s.breadth_score ?? 0),
        weight: 0.15,
        explain: (v: number) =>
          v >= 65 ? (isZh ? "题材/板块强度高，市场关注度高" : "Strong theme/sector, high attention")
          : v >= 55 ? (isZh ? "题材偏强，有板块效应" : "Moderate theme, sector effect")
          : (isZh ? "无明显题材或板块效应" : "No significant theme or sector"),
      },
      {
        key: "event",
        label: t("eventScore"),
        score: Number(s.event_score ?? 0),
        weight: 0.10,
        explain: (v: number) =>
          v >= 65 ? (isZh ? "有重大利好事件催化" : "Major bullish event catalyst")
          : v >= 55 ? (isZh ? "有一般性利好消息" : "Moderate bullish news")
          : v >= 45 ? (isZh ? "事件中性无明显影响" : "Neutral event impact")
          : (isZh ? "有潜在风险事件或利空" : "Potential risk or bearish event"),
      },
    ];

    // legacy 解释函数复用：按维度 key 复用对应文案
    const explainByDimKey = (dimKey: string, v: number): string => {
      const legacy = legacyFactors.find((f) => f.key === dimKey || f.key === dimKey.replace("_score", ""));
      return legacy ? legacy.explain(v) : (v >= 60 ? (isZh ? "表现偏强" : "Strong") : v >= 40 ? (isZh ? "表现中性" : "Neutral") : (isZh ? "表现偏弱" : "Weak"));
    };

    let factors: Array<{ key: string; label: string; score: number; weight: number; explain: (v: number) => string }>;
    if (hasConfigSnapshot && configSnapshot) {
      // P1：按配置快照构建分项（只展示属于 quality bucket 的启用维度）
      const enabledQualityDims = configSnapshot.dimensions!.filter((d) => d.enabled !== false && d.score_bucket === "quality");
      const totalW = enabledQualityDims.reduce((sum, d) => sum + (Number(d.weight) || 0), 0) || 1;
      factors = enabledQualityDims.map((d) => {
        const dimScore = dimScoresMap[d.key] ?? 50;
        return {
          key: d.key,
          label: d.name || d.key,
          score: Number(dimScore) || 0,
          weight: (Number(d.weight) || 0) / totalW,
          explain: (v: number) => explainByDimKey(d.key, v),
        };
      });
      // 若配置中无 quality bucket 维度（罕见），回退 legacy
      if (factors.length === 0) factors = legacyFactors;
    } else {
      factors = legacyFactors;
    }

    // 计算贡献值并排序
    const withContribution = factors.map((f) => ({
      ...f,
      contribution: f.score * f.weight,
      displayScore: clamp(f.score, 0, 100),
    }));

    // 找出最高分项和最低分项
    const sorted = [...withContribution].sort((a, b) => b.displayScore - a.displayScore);
    const topFactor = sorted[0];
    const bottomFactor = sorted[sorted.length - 1];

    // 总分验证（quality_score 应等于贡献之和）
    const totalContribution = withContribution.reduce((sum, f) => sum + f.contribution, 0);

    return {
      factors: withContribution,
      topFactor,
      bottomFactor,
      totalContribution: Math.round(totalContribution * 100) / 100,
      qualityScore: s.quality_score,
      // P1：附加配置快照信息（用于在 UI 上展示当前预设和版本）
      presetName: s.scoring_preset_name ?? null,
      presetVersion: s.scoring_config_version ?? null,
      hasConfigSnapshot,
    };
  }, [detail?.latest_score, ctx.locale]);

  // ════════════════════════════════════════════════
  // Timing Breakdown: 时点评分分项+阶段判断依据
  // ════════════════════════════════════════════════
  const timingBreakdown = useMemo(() => {
    if (!detail?.latest_score) return null;
    const s = detail.latest_score;
    const isZh = ctx.locale === "zh-CN";

    // P1：优先使用评分时保存的配置快照 + dimension_scores_json
    let dimScoresMap: Record<string, number> = {};
    let configSnapshot: { dimensions?: Array<{ key: string; name: string; enabled?: boolean; weight?: number; score_bucket?: string; factors?: Array<{ key: string }> }> } | null = null;
    try {
      if (s.dimension_scores_json) dimScoresMap = JSON.parse(s.dimension_scores_json);
      if (s.scoring_config_snapshot_json) configSnapshot = JSON.parse(s.scoring_config_snapshot_json);
    } catch { /* ignore */ }
    const hasConfigSnapshot = !!(configSnapshot && configSnapshot.dimensions && configSnapshot.dimensions.length > 0 && Object.keys(dimScoresMap).length > 0);

    // 时点评分分项配置（legacy 兜底）
    const legacyFactors = [
      {
        key: "breakout",
        label: isZh ? "突破信号" : "Breakout",
        score: Number(s.breakout_score ?? 0),
        weight: 0.30,
        explain: (v: number) =>
          v >= 70 ? (isZh ? "价格突破20日高点，形成新高信号" : "Price broke 20-day high")
          : (isZh ? "未突破近期高点，无明显突破" : "No breakout signal"),
      },
      {
        key: "momentum",
        label: t("momentumScore"),
        score: Number(s.momentum_score ?? 0),
        weight: 0.20,
        explain: (v: number) =>
          v >= 70 ? (isZh ? "动量强劲，短期上涨动能充足" : "Strong momentum")
          : v >= 55 ? (isZh ? "动量偏多，有上涨动力" : "Moderate momentum")
          : v >= 40 ? (isZh ? "动量中性" : "Neutral momentum")
          : (isZh ? "动量偏空" : "Weak momentum"),
      },
      {
        key: "liquidity",
        label: t("liquidityScore"),
        score: Number(s.liquidity_score ?? 0),
        weight: 0.15,
        explain: (v: number) =>
          v >= 60 ? (isZh ? "流动性充足，交易活跃" : "High liquidity")
          : v >= 45 ? (isZh ? "流动性适中" : "Moderate liquidity")
          : (isZh ? "流动性偏低" : "Low liquidity"),
      },
      {
        key: "pullback",
        label: isZh ? "回调结构" : "Pullback",
        score: Number(s.pullback_score ?? 0),
        weight: 0.15,
        explain: (v: number) =>
          v >= 65 ? (isZh ? "回调结构良好，价格站稳MA20附近" : "Healthy pullback near MA20")
          : (isZh ? "回调结构不佳或跌破MA20" : "Weak pullback structure"),
      },
      {
        key: "event",
        label: t("eventScore"),
        score: Number(s.event_score ?? 0),
        weight: 0.10,
        explain: (v: number) =>
          v >= 65 ? (isZh ? "有事件催化利好" : "Bullish event catalyst")
          : v >= 45 ? (isZh ? "事件影响中性" : "Neutral event")
          : (isZh ? "有风险事件或利空" : "Risk event"),
      },
      {
        key: "overheat",
        label: isZh ? "过热惩罚" : "Overheat",
        score: 100 - Number(s.overheat_penalty ?? 0), // 转换为正向分数
        weight: 0.10,
        rawPenalty: Number(s.overheat_penalty ?? 0),
        explain: (penalty: number) =>
          penalty >= 20 ? (isZh ? "短期涨幅过大(>12%)，存在过热风险" : "Overheated (>12% gain)")
          : penalty >= 10 ? (isZh ? "涨幅较大，需关注回调风险" : "Watch for pullback")
          : (isZh ? "无明显过热" : "No overheat"),
      },
    ];

    // P1：按配置快照构建 timing 分项（取 score_bucket === 'timing' 的启用维度）
    const explainByDimKey = (dimKey: string, v: number): string => {
      const legacy = legacyFactors.find((f) => f.key === dimKey || f.key === dimKey.replace("_score", ""));
      return legacy ? legacy.explain(v) : (v >= 60 ? (isZh ? "表现偏强" : "Strong") : v >= 40 ? (isZh ? "表现中性" : "Neutral") : (isZh ? "表现偏弱" : "Weak"));
    };

    let factors: Array<{ key: string; label: string; score: number; weight: number; explain: (v: number) => string }>;
    if (hasConfigSnapshot && configSnapshot) {
      const enabledTimingDims = configSnapshot.dimensions!.filter((d) => d.enabled !== false && d.score_bucket === "timing");
      const totalW = enabledTimingDims.reduce((sum, d) => sum + (Number(d.weight) || 0), 0) || 1;
      factors = enabledTimingDims.map((d) => {
        const dimScore = dimScoresMap[d.key] ?? 50;
        return {
          key: d.key,
          label: d.name || d.key,
          score: Number(dimScore) || 0,
          weight: (Number(d.weight) || 0) / totalW,
          explain: (v: number) => explainByDimKey(d.key, v),
        };
      });
      if (factors.length === 0) factors = legacyFactors;
    } else {
      factors = legacyFactors;
    }

    // 计算贡献值
    const withContribution = factors.map((f) => ({
      ...f,
      contribution: f.score * f.weight,
      displayScore: clamp(f.score, 0, 100),
    }));

    // 阶段判断依据
    const stageExplanation = (() => {
      const stage = s.stage;
      const action = s.action;
      const overheat = Number(s.overheat_penalty ?? 0);
      const momentum = Number(s.momentum_score ?? 0);
      const breakout = Number(s.breakout_score ?? 0);

      if (stage === "overheat") {
        return {
          reason: isZh ? "价格超过MA20 12%以上，短期涨幅过大" : "Price >12% above MA20",
          risk: isZh ? "存在短期回调风险，不建议追高" : "Risk of pullback, avoid chasing",
          suggestion: isZh ? "建议减仓或观望，等待回调企稳" : "Reduce position or wait",
        };
      }
      if (stage === "accel") {
        return {
          reason: isZh ? "趋势加速：价格站上MA20和MA50，动量>8%" : "Acceleration: price above MA20&MA50, momentum >8%",
          risk: isZh ? "上涨动能充足，但需防过热" : "Strong uptrend, watch for overheat",
          suggestion: isZh ? "可持有观察，关注是否出现过热信号" : "Hold and monitor",
        };
      }
      if (stage === "start") {
        return {
          reason: isZh ? "启动信号：价格站上MA20，动量转正" : "Start signal: price above MA20, momentum positive",
          risk: isZh ? "趋势刚启动，需确认支撑有效" : "Early trend, confirm support",
          suggestion: isZh ? "可考虑开仓，设置止损保护" : "Consider opening with stop-loss",
        };
      }
      return {
        reason: isZh ? "退潮阶段：趋势偏弱，动量不足" : "Cooldown: weak trend, low momentum",
        risk: isZh ? "趋势不明朗，不适合开新仓" : "Unclear trend, avoid new positions",
        suggestion: isZh ? "观望为主，等待明确信号" : "Wait for clear signals",
      };
    })();

    return {
      factors: withContribution,
      stageExplanation,
      timingScore: s.timing_score,
      stage: s.stage,
      action: s.action,
      presetName: s.scoring_preset_name ?? null,
      presetVersion: s.scoring_config_version ?? null,
      hasConfigSnapshot,
    };
  }, [detail?.latest_score, ctx.locale]);

  // ════════════════════════════════════════════════
  // P2: External Data Factors（PE/PB 估值、资金流、ETF 溢价折价）
  // ════════════════════════════════════════════════
  const externalFactors = useMemo(() => {
    if (!detail?.latest_score?.factor_scores_json) return [];
    let factorDetail: Record<string, { normalized_value?: number; weight?: number; contribution?: number }> = {};
    try {
      factorDetail = JSON.parse(detail.latest_score.factor_scores_json);
    } catch { /* ignore */ }
    const isZh = ctx.locale === "zh-CN";
    const EXTERNAL_KEYS: Record<string, string> = {
      pe_score: isZh ? "PE 估值分" : "PE Valuation Score",
      main_net_inflow_score: isZh ? "主力净流入分" : "Main Net Inflow Score",
      premium_discount_score: isZh ? "溢价折价分" : "Premium/Discount Score",
    };
    return Object.entries(factorDetail)
      .filter(([key]) => key in EXTERNAL_KEYS)
      .map(([key, info]) => ({
        key,
        label: EXTERNAL_KEYS[key],
        score: Number(info.normalized_value ?? 0),
        weight: Number(info.weight ?? 0),
        contribution: Number(info.contribution ?? 0),
      }));
  }, [detail?.latest_score?.factor_scores_json, ctx.locale]);

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
      // 复用 constants/chartTheme 中统一导出的信号颜色映射
      const colorMap = SIGNAL_COLOR_MAP;
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
    const allRelevant = [...allPrices, ...maValues, ...signalPrices, ...futureZones, ...drawingPrices].filter((v): v is number => v != null && !isNaN(v));
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
        ...(drawingLineSeries as any[]),
        ...(pendingSeries ? [pendingSeries as any] : []),
      ],
    };
  }, [chartData, detail?.latest_trade_setup, activeFutureBuyPlan, drawingLineSeries, pendingSeries, drawingPrices]);

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
    // 复用 utils/format 中导出的 roundPrice / clamp，删除本地重复定义
    const adjStop = roundPrice(refPrice - adjStopOffset);
    const adjTarget = roundPrice(refPrice + adjRisk);
    const adjConfidence = clamp((baseScenarios.confidence_pct ?? 60) + cfg.confidenceAdj, 35, 82) / 100;
    const pessimisticPrice = roundPrice(adjStop);
    const optimisticAnchor = adjTarget + Math.max(effectiveRisk * 0.4, refPrice * 0.02);
    const optimisticPrice = roundPrice(optimisticAnchor);
    const expectedPrice = roundPrice(adjTarget * adjConfidence + pessimisticPrice * (1 - adjConfidence));

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
      width="76vw"
      className="detail-dashboard-modal"
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
      destroyOnHidden
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
            <div className="detail-card detail-overview-card wide">
              <h3>{t("latestAssessment")}</h3>
              {latestScore && (
                <div className="assessment-metrics">
                  <div className="assessment-stat">
                    <span>{t("date")}</span>
                    <strong>{latestScore.trade_date || "-"}</strong>
                  </div>
                  <div className="assessment-stat assessment-stat--score">
                    <span>{t("quality")}</span>
                    <strong>{score(latestScore.quality_score)}</strong>
                  </div>
                  <div className="assessment-stat assessment-stat--score">
                    <span>{t("timing")}</span>
                    <strong>{score(latestScore.timing_score)}</strong>
                  </div>
                  <div className="assessment-stat">
                    <span>{t("stage")}</span>
                    <strong>{stageLabel(latestScore.stage)}</strong>
                  </div>
                  <div className="assessment-stat assessment-stat--action">
                    <span>{t("action")}</span>
                    <strong>{actionLabel(latestScore.action)}</strong>
                  </div>
                </div>
              )}
              <div className="overview-secondary-grid">
                {signalStats && (
                  <div className="signal-stats-card">
                    <div className="signal-stats-head">
                      <p className="panel-kicker">{t("similarSignalStats")}</p>
                      <label className="sample-limit-control" title={t("sampleLimitTip")}>
                        <span>{t("sampleLimit")}</span>
                        <input
                          className="sample-limit-input"
                          type="number"
                          min={1}
                          value={sampleLimitInput}
                          onChange={(e) => setSampleLimitInput(e.target.value)}
                          placeholder={t("sampleLimitTip")}
                        />
                      </label>
                    </div>
                    <div className="signal-stats-grid">
                      <div><span>{t("similarSamples")}</span><strong>{signalStats.sample_count}</strong></div>
                      <div><span>{t("matchedSignals")}</span><strong>{signalStats.matched_count}</strong></div>
                      <div><span>{t("win5d")}</span><strong>{statPct(signalStats.win_rate_5d)}</strong></div>
                      <div><span>{t("win20d")}</span><strong>{statPct(signalStats.win_rate_20d)}</strong></div>
                      <div><span>{t("avgReturn20d")}</span><strong>{statPct(signalStats.avg_return_20d)}</strong></div>
                      <div><span>{t("maxGain20d")}</span><strong>{statPct(signalStats.avg_max_gain_20d)}</strong></div>
                      <div><span>{t("maxDrawdown20d")}</span><strong>{statPct(signalStats.avg_max_drawdown_20d)}</strong></div>
                      <div><span>{t("best20d")} / {t("worst20d")}</span><strong>{statPct(signalStats.best_return_20d)} / {statPct(signalStats.worst_return_20d)}</strong></div>
                    </div>
                    {signalStats.sample_count === 0 && (
                      <div className="signal-stats-empty">
                        <strong>{ctx.locale === "zh-CN" ? "暂无可回测样本" : "No backtestable samples yet"}</strong>
                        <span>
                          {ctx.locale === "zh-CN"
                            ? signalStats.matched_count > 0
                              ? `已匹配 ${signalStats.matched_count} 条历史评分，但缺少可用于回测的逐日评分。现有K线无需重拉，可直接生成历史评分。`
                              : "当前规则下没有足够相似的历史评分。现有K线无需重拉，可先生成逐日历史评分。"
                            : signalStats.matched_count > 0
                              ? `${signalStats.matched_count} matching scores were found, but daily historical scores are missing. Existing bars can be reused without refetching.`
                              : "No sufficiently similar historical scores. Generate daily scores from the existing local bars first."}
                        </span>
                        <Button size="small" type="primary" loading={scoreBackfillStarting} onClick={handleLocalScoreBackfill}>
                          {ctx.locale === "zh-CN" ? "基于现有K线生成评分" : "Generate Scores from Local Bars"}
                        </Button>
                      </div>
                    )}
                    {signalStats.sample_count > 0 && signalStats.sample_count < signalStats.min_sample_count && (
                      <div className="signal-stats-empty signal-stats-empty--warning">
                        <strong>{ctx.locale === "zh-CN" ? "样本量不足" : "Insufficient sample size"}</strong>
                        <span>
                          {ctx.locale === "zh-CN"
                            ? `当前仅 ${signalStats.sample_count} 个可回测样本，低于建议的 ${signalStats.min_sample_count} 个，统计结果仅供参考。`
                            : `${signalStats.sample_count} samples are available, below the recommended ${signalStats.min_sample_count}; treat the statistics as indicative only.`}
                        </span>
                      </div>
                    )}
                  </div>
                )}
                {position && (
                  <div className="position-summary-card">
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

            {/* Score Radar + Breakdown (ECharts) */}
            <div className="detail-card score-analysis-card wide">
              <h3>{t("scoreRadar")}</h3>
              {detail?.latest_score?.data_credibility != null && detail.latest_score.data_credibility < 0.5 && (
                <div className="credibility-warning">
                  <span className="credibility-warning-icon">&#9888;</span>
                  {ctx.locale === "zh-CN"
                    ? `数据可信度低 (${(detail.latest_score.data_credibility * 100).toFixed(0)}%)，评分仅供参考`
                    : `Low data credibility (${(detail.latest_score.data_credibility * 100).toFixed(0)}%), scores for reference only`}
                </div>
              )}
              <div className="score-radar-breakdown-grid">
                <div className="radar-panel">
                  {radarOption && (
                    <ReactECharts option={radarOption} style={{ height: "320px", width: "100%" }} />
                  )}
                </div>
                {scoreBreakdown && (
                  <div className="breakdown-panel">
                    <div className="breakdown-header">
                      <span className="breakdown-title">{ctx.locale === "zh-CN" ? "分项评分解析" : "Score Breakdown"}</span>
                      <span className="breakdown-total">
                        {ctx.locale === "zh-CN" ? "总分" : "Total"}: {score(scoreBreakdown.qualityScore ?? 0)}
                      </span>
                    </div>
                    {scoreBreakdown.presetName && (
                      <div style={{ marginBottom: 8, fontSize: 12, color: "#888", display: "flex", gap: 8, alignItems: "center" }}>
                        <Tag color="blue" style={{ margin: 0 }}>
                          {scoreBreakdown.presetName} v{scoreBreakdown.presetVersion}
                        </Tag>
                        <Tooltip title={ctx.locale === "zh-CN" ? "本评分按该预设版本计算并保存快照，切换预设不会重算历史评分。" : "Score computed with this preset version (snapshot saved). Switching presets will not recompute historical scores."}>
                          <span style={{ cursor: "help" }}><QuestionCircleOutlined /></span>
                        </Tooltip>
                      </div>
                    )}
                    <div className="breakdown-list">
                      {scoreBreakdown.factors.map((f) => (
                        <div
                          key={f.key}
                          className={`breakdown-item ${
                            f.key === scoreBreakdown.topFactor.key ? "breakdown-item--top" : ""
                          } ${
                            f.key === scoreBreakdown.bottomFactor.key ? "breakdown-item--bottom" : ""
                          }`}
                        >
                          <div className="breakdown-item-head">
                            <span className="breakdown-label">{f.label}</span>
                            <span className="breakdown-weight">{(f.weight * 100).toFixed(0)}%</span>
                          </div>
                          <div className="breakdown-item-row">
                            <span className="breakdown-score">{score(f.displayScore)}</span>
                            <div className="breakdown-bar-wrap">
                              <div
                                className="breakdown-bar"
                                style={{ width: `${f.displayScore}%`, backgroundColor: f.displayScore >= 60 ? "#0f766e" : f.displayScore >= 40 ? "#d97706" : "#b42318" }}
                              />
                            </div>
                            <span className="breakdown-contribution">+{(f.contribution).toFixed(1)}</span>
                          </div>
                          <div className="breakdown-explain">{f.explain(f.displayScore)}</div>
                        </div>
                      ))}
                    </div>
                    <div className="breakdown-summary">
                      <span className="breakdown-highlight-green">
                        {ctx.locale === "zh-CN" ? "主要加分" : "Top Strength"}: {scoreBreakdown.topFactor.label} (+{scoreBreakdown.topFactor.contribution.toFixed(1)})
                      </span>
                      <span className="breakdown-highlight-red">
                        {ctx.locale === "zh-CN" ? "主要扣分" : "Main Risk"}: {scoreBreakdown.bottomFactor.label} (+{scoreBreakdown.bottomFactor.contribution.toFixed(1)})
                      </span>
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* Timing Score Breakdown */}
            {timingBreakdown && (
              <div className="detail-card timing-analysis-card wide">
                <h3>{ctx.locale === "zh-CN" ? "时点评分解析" : "Timing Score Breakdown"}</h3>
                <div className="timing-breakdown-grid">
                  <div className="timing-scores-panel">
                    <div className="timing-header">
                      <span className="timing-title">{ctx.locale === "zh-CN" ? "分项评分" : "Score Factors"}</span>
                      <span className="timing-total">
                        {ctx.locale === "zh-CN" ? "时点分" : "Timing"}: {score(timingBreakdown.timingScore ?? 0)}
                      </span>
                    </div>
                    <div className="timing-list">
                      {timingBreakdown.factors.map((f) => (
                        <div key={f.key} className="timing-item">
                          <div className="timing-item-head">
                            <span className="timing-label">{f.label}</span>
                            <span className="timing-weight">{(f.weight * 100).toFixed(0)}%</span>
                          </div>
                          <div className="timing-item-row">
                            <span className="timing-score">{score(f.displayScore)}</span>
                            <div className="timing-bar-wrap">
                              <div
                                className="timing-bar"
                                style={{
                                  width: `${f.displayScore}%`,
                                  backgroundColor: f.key === "overheat" && (f as any).rawPenalty >= 20
                                    ? "#b42318"
                                    : f.displayScore >= 60 ? "#0f766e" : f.displayScore >= 40 ? "#d97706" : "#b42318",
                                }}
                              />
                            </div>
                            <span className="timing-contribution">+{(f.contribution).toFixed(1)}</span>
                          </div>
                          <div className="timing-explain">
                            {f.key === "overheat" ? (f as any).explain((f as any).rawPenalty) : f.explain(f.displayScore)}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div className="stage-panel">
                    <div className="stage-header">
                      <span className="stage-title">{ctx.locale === "zh-CN" ? "阶段判断" : "Stage Analysis"}</span>
                      <span className={`stage-badge stage-badge--${timingBreakdown.stage}`}>
                        {stageLabel(timingBreakdown.stage)}
                      </span>
                    </div>
                    <div className="stage-content">
                      <div className="stage-row">
                        <span className="stage-label">{ctx.locale === "zh-CN" ? "判定依据" : "Reason"}:</span>
                        <span className="stage-text">{timingBreakdown.stageExplanation.reason}</span>
                      </div>
                      <div className="stage-row">
                        <span className="stage-label">{ctx.locale === "zh-CN" ? "风险提示" : "Risk"}:</span>
                        <span className="stage-text stage-text--risk">{timingBreakdown.stageExplanation.risk}</span>
                      </div>
                      <div className="stage-row">
                        <span className="stage-label">{ctx.locale === "zh-CN" ? "操作建议" : "Suggestion"}:</span>
                        <span className="stage-text stage-text--suggestion">{timingBreakdown.stageExplanation.suggestion}</span>
                      </div>
                    </div>
                    <div className="stage-action-bar">
                      <span className={`stage-action stage-action--${timingBreakdown.action}`}>
                        {ctx.locale === "zh-CN" ? "建议动作" : "Action"}: {actionLabel(timingBreakdown.action)}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* P2: External Data Factors */}
            <div className="detail-card wide">
              <div className="detail-card-head">
                <h3>{t("extFactorCardTitle")}</h3>
                <Tooltip title={t("extFactorTip")}>
                  <span style={{ cursor: "help", color: "#888" }}>
                    <QuestionCircleOutlined />
                  </span>
                </Tooltip>
              </div>
              {externalFactors.length > 0 ? (
                <div className="external-factors-grid">
                  {externalFactors.map((f) => (
                    <div key={f.key} className="breakdown-item">
                      <div className="breakdown-item-head">
                        <span className="breakdown-label">{f.label}</span>
                        <span className="breakdown-weight">{(f.weight * 100).toFixed(0)}%</span>
                      </div>
                      <div className="breakdown-item-row">
                        <span className="breakdown-score">{score(f.score)}</span>
                        <div className="breakdown-bar-wrap">
                          <div
                            className="breakdown-bar"
                            style={{
                              width: `${Math.max(0, Math.min(100, f.score))}%`,
                              backgroundColor: f.score >= 60 ? "#0f766e" : f.score >= 40 ? "#d97706" : "#b42318",
                            }}
                          />
                        </div>
                        <span className="breakdown-contribution">+{f.contribution.toFixed(1)}</span>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty">{t("extFactorEmpty")}</div>
              )}
            </div>

            {/* Trade Setup */}
            <div className="detail-card wide">
              <div className="detail-card-head">
                <h3>{t("tradeSetup")}</h3>
                <div className="detail-actions">
                  <Button
                    size="small"
                    loading={refreshingPlan}
                    onClick={handleRefreshPlan}
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
                    <div className={`position-decision-panel position-decision-panel--${setupDecisionTone(setup)}`}>
                      <div className="position-decision-head">
                        <div>
                          <p className="panel-kicker">{t("planDecision")}</p>
                          <strong>{setupDecisionLabel(setup)}</strong>
                        </div>
                        <span className={`badge ${setupDecisionTone(setup) === "blocked" ? "danger" : setupDecisionTone(setup) === "wait" ? "warn" : ""}`}>
                          {setup.open_slots_remaining != null
                            ? `${t("openSlots")}: ${setup.open_slots_remaining}`
                            : t("portfolioConstraint")}
                        </span>
                      </div>

                      <div className="position-decision-metrics">
                        <span>
                          <b>{t("suggestedBuyAmount")}</b>
                          <strong>{money(setup.suggested_buy_amount ?? setup.recommended_position_amount)}</strong>
                        </span>
                        <span>
                          <b>{t("suggestedBuyPct")}</b>
                          <strong>{percent(setup.suggested_buy_pct ?? setup.recommended_position_pct)}</strong>
                        </span>
                        <span>
                          <b>{t("plannedOrder")}</b>
                          <strong>{quantity > 0 ? quantity : "-"}</strong>
                        </span>
                        <span>
                          <b>{t("investableRemaining")}</b>
                          <strong>{setup.allocation_snapshot?.investable_remaining_amount != null ? money(setup.allocation_snapshot.investable_remaining_amount) : "-"}</strong>
                        </span>
                        <span>
                          <b>{t("riskCapAmount")}</b>
                          <strong>{setup.risk_capped_amount != null ? money(setup.risk_capped_amount) : "-"}</strong>
                        </span>
                      </div>

                      {setup.blocked_reasons && setup.blocked_reasons.length > 0 && (
                        <div className="position-blocked-reasons">
                          {setup.blocked_reasons.map((reason) => (
                            <span key={reason} className="badge danger">{blockedReasonLabel(reason)}</span>
                          ))}
                        </div>
                      )}

                      {setup.position_constraints && setup.position_constraints.length > 0 && (
                        <div className="position-constraint-grid">
                          {setup.position_constraints.map((constraint) => (
                            <div key={constraint.key} className="position-constraint-row">
                              <strong>{constraintLabel(constraint.key)}</strong>
                              <span>{t("limit")}: {percent(constraint.limit_pct)}</span>
                              <span>{t("used")}: {percent(constraint.used_pct)}</span>
                              <span>{t("remaining")}: {percent(constraint.remaining_pct)}</span>
                              {constraint.amount != null && <span>{t("amount")}: {money(constraint.amount)}</span>}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
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
                        ctx.setChartWindowSize(Math.min(500, ctx.chartWindowSize + 20))
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
                    <span className="drawing-divider" />
                    <Button
                      size="small"
                      type={drawingMode === "trendline" ? "primary" : "default"}
                      onClick={() => drawingMode === "trendline" ? cancelDrawing() : startDrawing("trendline")}
                    >
                      {t("drawTrendLine")}
                    </Button>
                    <Button
                      size="small"
                      type={drawingMode === "horizontal" ? "primary" : "default"}
                      onClick={() => drawingMode === "horizontal" ? cancelDrawing() : startDrawing("horizontal")}
                    >
                      {t("drawHorizontal")}
                    </Button>
                    <Button
                      size="small"
                      onClick={removeLastDrawing}
                      disabled={drawings.length === 0}
                    >
                      {t("drawUndo")}
                    </Button>
                    <Button
                      size="small"
                      onClick={clearAllDrawings}
                      disabled={drawings.length === 0}
                    >
                      {t("drawClear")}
                    </Button>
                  </div>
                </div>
              </div>
              <p className="panel-meta chart-hint" id="chartHint">
                {drawingMode !== "none"
                  ? (!pendingPoint ? t("drawHintFirst") : t("drawHintSecond"))
                  : t("chartDragHint")}
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
                          cursor: drawingMode !== "none" ? "crosshair" : undefined,
                        }}
                        onEvents={{
                          click: (params: any, chartInstance: any) => {
                            drawingClickHandler(params, chartInstance);
                          },
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

            {/* P3: Signal Validation */}
            <div className="detail-card wide">
              <h3>{t("signalValidation")}</h3>
              <div className="signal-validation-grid">
                {[
                  { label: t("winRate3d"), win: signalValidationStats?.win_rate_3d ?? signalStats?.win_rate_3d, ret: signalValidationStats?.avg_return_3d, gain: signalValidationStats?.avg_max_gain_3d, dd: signalValidationStats?.avg_max_drawdown_3d },
                  { label: t("winRate5d"), win: signalValidationStats?.win_rate_5d ?? signalStats?.win_rate_5d, ret: signalValidationStats?.avg_return_5d ?? signalStats?.avg_return_20d, gain: signalValidationStats?.avg_max_gain_5d ?? signalStats?.avg_max_gain_20d, dd: signalValidationStats?.avg_max_drawdown_5d ?? signalStats?.avg_max_drawdown_20d },
                  { label: t("winRate10d"), win: signalValidationStats?.win_rate_10d ?? signalStats?.win_rate_10d, ret: signalValidationStats?.avg_return_10d, gain: signalValidationStats?.avg_max_gain_10d, dd: signalValidationStats?.avg_max_drawdown_10d },
                  { label: t("winRate20d"), win: signalValidationStats?.win_rate_20d ?? signalStats?.win_rate_20d, ret: signalValidationStats?.avg_return_20d ?? signalStats?.avg_return_20d, gain: signalValidationStats?.avg_max_gain_20d ?? signalStats?.avg_max_gain_20d, dd: signalValidationStats?.avg_max_drawdown_20d ?? signalStats?.avg_max_drawdown_20d },
                ].map((card, idx) => (
                  <div key={idx} className="signal-stat-card">
                    <div className="signal-stat-card-title">{card.label}</div>
                    <div className="signal-stat-card-row">
                      <span className="metric-label">{t("winRate")}</span>
                      <span className="metric-value">{statPct(card.win)}</span>
                    </div>
                    <div className="signal-stat-card-row">
                      <span className="metric-label">{t("avgReturn")}</span>
                      <span className="metric-value">{statPct(card.ret)}</span>
                    </div>
                    <div className="signal-stat-card-row">
                      <span className="metric-label">{t("maxGain")}</span>
                      <span className="metric-value">{statPct(card.gain)}</span>
                    </div>
                    <div className="signal-stat-card-row">
                      <span className="metric-label">{t("maxDrawdown")}</span>
                      <span className="metric-value">{statPct(card.dd)}</span>
                    </div>
                  </div>
                ))}
              </div>
              {signalValidationStats && (
                <div className="item-subline" style={{ marginTop: 8 }}>
                  {joinParts([
                    `${t("sampleCount")}: ${signalValidationStats.sample_count ?? signalStats?.sample_count ?? 0}`,
                    `${t("matchedSignals")}: ${signalValidationStats.matched_count ?? signalStats?.matched_count ?? 0}`,
                  ])}
                </div>
              )}
            </div>

            {/* P3: Review Management (replaces simple journals list) */}
            <div className="detail-card wide review-panel" id="detailJournals">
              <div className="review-panel-head">
                <h3>{t("reviewSection")}</h3>
                <Button
                  size="small"
                  onClick={() => {
                    if (reviewFormOpen) {
                      resetReviewForm();
                    } else {
                      resetReviewForm();
                    }
                    setReviewFormOpen(!reviewFormOpen);
                  }}
                >
                  {reviewFormOpen ? t("closeBtn") : t("addReview")}
                </Button>
              </div>

              {reviewFormOpen && (
                <div className="review-form">
                  <label className="review-form-field">
                    <span>{t("reviewTitle")}</span>
                    <Input
                      value={reviewTitle}
                      onChange={(e) => setReviewTitle(e.target.value)}
                      placeholder={t("reviewTitle")}
                    />
                  </label>
                  <label className="review-form-field">
                    <span>{t("reviewType")}</span>
                    <Select
                      value={reviewType}
                      onChange={(v) => setReviewType(v)}
                      style={{ width: "100%" }}
                      options={[
                        { value: "buy", label: t("buy") },
                        { value: "sell", label: t("sell") },
                        { value: "review", label: t("reviewSection") },
                        { value: "note", label: t("recentNotes") },
                      ]}
                    />
                  </label>
                  <label className="review-form-field">
                    <span>{t("actualAction")}</span>
                    <Select
                      value={reviewActualAction}
                      onChange={(v) => setReviewActualAction(v)}
                      style={{ width: "100%" }}
                      options={[
                        { value: "follow_system", label: t("followSystem") },
                        { value: "override", label: t("override") },
                        { value: "wait", label: t("wait") },
                        { value: "no_action", label: t("noAction") },
                      ]}
                    />
                  </label>
                  <label className="review-form-field">
                    <span>{t("reviewResult")}</span>
                    <Select
                      value={reviewOutcome}
                      onChange={(v) => setReviewOutcome(v)}
                      style={{ width: "100%" }}
                      options={[
                        { value: "profit", label: t("profit") },
                        { value: "loss", label: t("loss") },
                        { value: "breakeven", label: t("breakeven") },
                        { value: "pending", label: t("pending") },
                      ]}
                    />
                  </label>
                  <label className="review-form-field review-form-field-wide">
                    <span>{t("reviewTags")}</span>
                    <Select
                      mode="multiple"
                      value={reviewTags}
                      onChange={setReviewTags}
                      placeholder={t("reviewTagsPlaceholder")}
                      style={{ width: "100%" }}
                      options={[
                        { value: "pattern_valid", label: t("reviewTagPatternValid") },
                        { value: "execution_deviation", label: t("reviewTagExecDeviation") },
                        { value: "data_misjudge", label: t("reviewTagDataMisjudge") },
                        { value: "risk_uncontrolled", label: t("reviewTagRiskUncontrolled") },
                        { value: "market_change", label: t("reviewTagMarketChange") },
                      ]}
                    />
                  </label>
                  <label className="review-form-field review-form-field-wide">
                    <span>{t("reviewNote")}</span>
                    <Input.TextArea
                      value={reviewNote}
                      onChange={(e) => setReviewNote(e.target.value)}
                      placeholder={t("reviewNote")}
                      autoSize={{ minRows: 2, maxRows: 5 }}
                    />
                  </label>
                  <div className="review-form-actions">
                    <Button
                      type="primary"
                      loading={reviewSaving}
                      onClick={handleSubmitReview}
                    >
                      {reviewEditingId != null ? t("editReview") : t("addReview")}
                    </Button>
                    <Button
                      onClick={() => {
                        resetReviewForm();
                        setReviewFormOpen(false);
                      }}
                    >
                      {t("closeBtn")}
                    </Button>
                  </div>
                </div>
              )}

              <div className="review-list">
                {reviewLoading ? (
                  <div className="empty">{t("loading") || "..."}</div>
                ) : reviewList.length > 0 ? (
                  reviewList.map((journal: JournalEntry) => (
                    <article key={journal.id} className="review-item">
                      <div className="review-item-header">
                        <strong>{journal.title}</strong>
                        <Space size={4} wrap>
                          {journal.entry_type && (
                            <Tag className={badgeClass(journal.entry_type)}>{journal.entry_type}</Tag>
                          )}
                          {journal.stage && (
                            <Tag className={badgeClass(journal.stage)}>{stageLabel(journal.stage)}</Tag>
                          )}
                          {journal.action && (
                            <Tag className={badgeClass(journal.action)}>{actionLabel(journal.action)}</Tag>
                          )}
                          {journal.actual_action && (
                            <Tag color="blue">{journal.actual_action}</Tag>
                          )}
                          {journal.outcome && (
                            <Tag color={journal.outcome === "profit" ? "green" : journal.outcome === "loss" ? "red" : "default"}>
                              {journal.outcome}
                            </Tag>
                          )}
                          {journal.review_tags && (() => {
                            try {
                              const tags: string[] = JSON.parse(journal.review_tags);
                              const tagLabels: Record<string, string> = {
                                pattern_valid: t("reviewTagPatternValid"),
                                execution_deviation: t("reviewTagExecDeviation"),
                                data_misjudge: t("reviewTagDataMisjudge"),
                                risk_uncontrolled: t("reviewTagRiskUncontrolled"),
                                market_change: t("reviewTagMarketChange"),
                              };
                              return tags.map((tag) => <Tag key={tag} color={tag === "pattern_valid" ? "green" : tag === "risk_uncontrolled" ? "red" : "orange"}>{tagLabels[tag] || tag}</Tag>);
                            } catch { return null; }
                          })()}
                        </Space>
                      </div>
                      {journal.review_note && (
                        <div className="review-item-meta">{journal.review_note}</div>
                      )}
                      <div className="review-item-actions">
                        <span className="item-subline">{formatDate(journal.created_at)}</span>
                        <Space size={4}>
                          <Button size="small" onClick={() => handleStartEditReview(journal)}>
                            {t("editReview")}
                          </Button>
                          <Button
                            size="small"
                            danger
                            loading={deletingReviewId === journal.id}
                            onClick={() => handleDeleteReview(journal.id)}
                          >
                            {t("deleteReview")}
                          </Button>
                        </Space>
                      </div>
                    </article>
                  ))
                ) : detail.journals && detail.journals.length > 0 ? (
                  detail.journals.map((journal: JournalEntry) => (
                    <article key={journal.id} className="review-item">
                      <div className="review-item-header">
                        <strong>{journal.title}</strong>
                        {journal.entry_type && (
                          <Tag className={badgeClass(journal.entry_type)}>{journal.entry_type}</Tag>
                        )}
                      </div>
                      <div className="review-item-actions">
                        <span className="item-subline">{formatDate(journal.created_at)}</span>
                      </div>
                    </article>
                  ))
                ) : (
                  <div className="empty">{t("noReviews")}</div>
                )}
              </div>
            </div>
          </div>
      </div>
    </Modal>
  );
}
