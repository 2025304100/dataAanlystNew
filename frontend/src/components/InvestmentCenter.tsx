import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import ReactECharts from "echarts-for-react";
import { Button, Input, InputNumber, Progress, Switch, Tabs, Tag } from "antd";
import { ExperimentOutlined, ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import { useApp } from "../context/AppContext";
import BacktestConfig from "./BacktestConfig";
import BacktestResult from "./BacktestResult";
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
  pnlClass,
  clamp,
  roundPrice,
  aggregateWeeklyBars,
  computeSuggestedPrice,
  signalLabel,
} from "../utils/format";
import type { BacktestRun, FutureBuyPlan, FuturePlanTuning, ReturnScenarios, Symbol as SymbolInfo, TradeSetup, TradeSetupOverrides, TradeSetupTranche, WorkbenchBar } from "../types";
import { api, type SymbolFactorExplanation } from "../api/client";

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
// 复用 constants/chartTheme 中统一导出的图表样式与颜色映射
import { FUTURE_PLAN_STYLE_MAP, SIGNAL_COLOR_MAP } from "../constants/chartTheme";
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

type TradePlanDraft = {
  entry_min: number | null;
  entry_max: number | null;
  stop_loss: number | null;
  target_price: number | null;
  recommended_position_pct: number | null;
  recommended_position_amount: number | null;
};

// 场景预演扩展类型：在 ReturnScenarios 基础上附加调整后的止损/目标价（_adjStop/_adjTarget）
type ScenarioPreview = ReturnScenarios & { _adjStop?: number; _adjTarget?: number };

// 标的快捷引用（仅包含展示所需的最少字段，用于搜索历史/收藏等）
type SymbolQuickRef = { symbol_id: number; symbol: string; name: string };

type TrancheDraft = TradeSetupTranche;
type FuturePlanTunings = Record<string, FuturePlanTuning>;

type RiskSettings = {
  atrMultiplier: number;
  concentrationMediumPct: number;
  concentrationHighPct: number;
  maxLossPct: number;
};

type AlertSettings = {
  enableStopLoss: boolean;
  enableTarget: boolean;
  enableRsi: boolean;
  enableMacd: boolean;
  stopNearPct: number;
  targetNearPct: number;
  rsiOverbought: number;
  rsiOversold: number;
};

const DEFAULT_RISK_SETTINGS: RiskSettings = {
  atrMultiplier: 2,
  concentrationMediumPct: 10,
  concentrationHighPct: 20,
  maxLossPct: 2,
};

const DEFAULT_ALERT_SETTINGS: AlertSettings = {
  enableStopLoss: true,
  enableTarget: true,
  enableRsi: true,
  enableMacd: true,
  stopNearPct: 3,
  targetNearPct: 5,
  rsiOverbought: 75,
  rsiOversold: 25,
};

const DEFAULT_FUTURE_TUNINGS: FuturePlanTunings = {
  general: { scalePct: 100, bandPct: 1.5 },
  short: { horizonDays: 5, scalePct: 70, bandPct: 1.2 },
  mid: { horizonDays: 15, scalePct: 90, bandPct: 1.5 },
  long: { horizonDays: 30, pullbackPct: 0, bandPct: 5 },
  custom: { horizonDays: 20, pullbackPct: 3, positionPct: 5, bandPct: 1.5 },
};

function readStoredObject<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? { ...fallback, ...JSON.parse(raw) } : fallback;
  } catch {
    return fallback;
  }
}

function planDraftFromSetup(setup: TradeSetup): TradePlanDraft {
  return {
    entry_min: setup.entry_min,
    entry_max: setup.entry_max,
    stop_loss: setup.stop_loss,
    target_price: setup.target_price,
    recommended_position_pct: Math.round((setup.recommended_position_pct ?? 0) * 10000) / 100,
    recommended_position_amount: setup.recommended_position_amount,
  };
}

function trancheDraftFromSetup(setup: TradeSetup): TrancheDraft[] {
  return (setup.tranche_plan ?? []).map((item) => ({
    label: item.label || trancheLabel("Custom"),
    position_pct: Math.round(Number(item.position_pct || 0) * 10000) / 100,
    amount: Number(item.amount || 0),
    trigger: item.trigger || "",
  }));
}

function normalizeTrancheDrafts(drafts: TrancheDraft[]): TradeSetupTranche[] {
  return drafts
    .filter((item) => Number(item.position_pct || 0) > 0 || Number(item.amount || 0) > 0 || item.trigger.trim())
    .map((item) => ({
      label: item.label.trim() || trancheLabel("Custom"),
      position_pct: Math.max(0, Number(item.position_pct || 0)) / 100,
      amount: Math.max(0, Number(item.amount || 0)),
      trigger: item.trigger.trim(),
    }));
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

  // 复用 constants/chartTheme 中统一导出的样式映射，避免重复定义
  const styleMap = FUTURE_PLAN_STYLE_MAP;

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
  const [searchResults, setSearchResults] = useState<SymbolInfo[]>([]);
  const [searching, setSearching] = useState(false);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // K线本地缓存：懒加载更多数据
  const [extraBarsCache, setExtraBarsCache] = useState<Record<number, WorkbenchBar[]>>({});
  const loadingBarsRef = useRef<Set<number>>(new Set());
  const windowInitializedRef = useRef(false);

  // 指标面板开关
  const [showMACD, setShowMACD] = useState(true);
  const [showRSI, setShowRSI] = useState(true);

  // 可折叠区块状态（默认收起：收益预演、分批执行、触发条件）
  const [collapsedSections, setCollapsedSections] = useState<Record<string, boolean>>({
    scenarioPreview: true,
    tranchePlan: true,
    triggers: true,
  });
  const toggleSection = useCallback((key: string) => {
    setCollapsedSections((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  // 搜索历史（最近5个）+ 收藏
  const [searchHistory, setSearchHistory] = useState<SymbolQuickRef[]>(() => {
    try { return JSON.parse(localStorage.getItem("ic_search_history") || "[]"); } catch { return []; }
  });
  const [favorites, setFavorites] = useState<Set<number>>(() => {
    try { return new Set(JSON.parse(localStorage.getItem("ic_favorites") || "[]")); } catch { return new Set(); }
  });
  const [showHistoryModal, setShowHistoryModal] = useState(false);

  // 图表点击设置的入场价
  const [chartEntryPrice, setChartEntryPrice] = useState<number | null>(null);

  // 刷新交易计划 loading
  const [refreshingPlan, setRefreshingPlan] = useState(false);

  // 价格预警列表
  const [priceAlerts, setPriceAlerts] = useState<Array<{
    id: string; type: string; level: "warning" | "danger" | "info";
    message: string; detail: string; timestamp: number;
  }>>([]);

  const [tradePlanEditing, setTradePlanEditing] = useState(false);
  const [tradePlanDraft, setTradePlanDraft] = useState<TradePlanDraft | null>(null);
  const [trancheEditing, setTrancheEditing] = useState(false);
  const [trancheDrafts, setTrancheDrafts] = useState<TrancheDraft[]>([]);
  const [riskSettingsOpen, setRiskSettingsOpen] = useState(false);
  const [alertSettingsOpen, setAlertSettingsOpen] = useState(false);
  const [riskSettings, setRiskSettings] = useState<RiskSettings>(() => readStoredObject("ic_risk_settings", DEFAULT_RISK_SETTINGS));
  const [alertSettings, setAlertSettings] = useState<AlertSettings>(() => readStoredObject("ic_alert_settings", DEFAULT_ALERT_SETTINGS));
  const [futurePlanTunings, setFuturePlanTunings] = useState<FuturePlanTunings>(() => readStoredObject("ic_future_plan_tunings", DEFAULT_FUTURE_TUNINGS));
  const [backtestResult, setBacktestResult] = useState<BacktestRun | null>(null);
  const [factorExplanation, setFactorExplanation] = useState<SymbolFactorExplanation | null>(null);
  const [factorExplanationLoading, setFactorExplanationLoading] = useState(false);
  const [factorExplanationError, setFactorExplanationError] = useState<string | null>(null);
  const factorExplanationRequestRef = useRef(0);

  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 1024);
    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, []);

  const loadFactorExplanation = useCallback(async () => {
    const requestId = factorExplanationRequestRef.current + 1;
    factorExplanationRequestRef.current = requestId;
    if (!ctx.activeSymbolId) {
      setFactorExplanation(null);
      setFactorExplanationError(null);
      setFactorExplanationLoading(false);
      return;
    }
    setFactorExplanationLoading(true);
    setFactorExplanationError(null);
    try {
      const result = await api.getSymbolFactorExplanation(ctx.activeSymbolId);
      if (requestId === factorExplanationRequestRef.current) {
        setFactorExplanation(result);
      }
    } catch (err: any) {
      if (requestId !== factorExplanationRequestRef.current) return;
      setFactorExplanation(null);
      const message = String(err?.message || "");
      if (!message.toLowerCase().includes("not found")) {
        setFactorExplanationError(message);
      }
    } finally {
      if (requestId === factorExplanationRequestRef.current) {
        setFactorExplanationLoading(false);
      }
    }
  }, [ctx.activeSymbolId]);

  useEffect(() => {
    loadFactorExplanation();
  }, [loadFactorExplanation]);

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

    api.getBars(symbolId, Math.max(neededBars + 60, 500))
      .then((fetched: WorkbenchBar[]) => {
        if (!fetched || !fetched.length) return;
        const existingDates = new Set(detail.bars.map((b: WorkbenchBar) => b.trade_date));
        const newBars = fetched.filter((b: WorkbenchBar) => !existingDates.has(b.trade_date));
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
  const quickSymbols = useMemo<SymbolQuickRef[]>(() => {
    if (!workbench) return [];
    const map = new Map<number, SymbolQuickRef>();
    for (const p of workbench.positions)
      map.set(p.symbol_id, { symbol_id: p.symbol_id, symbol: p.symbol, name: p.name });
    for (const s of workbench.latest_scores) {
      if (!map.has(s.symbol_id))
        map.set(s.symbol_id, { symbol_id: s.symbol_id, symbol: s.symbol, name: s.name });
    }
    return Array.from(map.values());
  }, [workbench]);

  const handleSelectSymbol = useCallback((symbolId: number, symbolInfo?: Pick<SymbolInfo, "symbol" | "name">) => {
    ctx.loadSymbolDetail(symbolId, { focus: true, barLimit: 500 });
    setSearchQuery("");
    setSearchResults([]);
    // 记录搜索历史（最近5个）
    if (symbolInfo) {
      setSearchHistory((prev) => {
        const filtered = prev.filter((h) => h.symbol_id !== symbolId);
        const updated: SymbolQuickRef[] = [{ symbol_id: symbolId, symbol: symbolInfo.symbol, name: symbolInfo.name }, ...filtered].slice(0, 5);
        try { localStorage.setItem("ic_search_history", JSON.stringify(updated)); } catch {}
        return updated;
      });
    }
    // 清除图表设置的入场价
    setChartEntryPrice(null);
  }, [ctx]);

  // 切换收藏
  const toggleFavorite = useCallback((symbolId: number) => {
    setFavorites((prev) => {
      const next = new Set(prev);
      if (next.has(symbolId)) next.delete(symbolId); else next.add(symbolId);
      try { localStorage.setItem("ic_favorites", JSON.stringify([...next])); } catch {}
      return next;
    });
  }, []);

  // 清空搜索历史
  const clearSearchHistory = useCallback(() => {
    setSearchHistory([]);
    try { localStorage.removeItem("ic_search_history"); } catch {}
  }, []);

  // 从历史中删除单个项目
  const removeFromHistory = useCallback((symbolId: number) => {
    setSearchHistory((prev) => {
      const updated = prev.filter((h) => h.symbol_id !== symbolId);
      try { localStorage.setItem("ic_search_history", JSON.stringify(updated)); } catch {}
      return updated;
    });
  }, []);

  // 清空收藏
  const clearFavorites = useCallback(() => {
    setFavorites(new Set());
    try { localStorage.removeItem("ic_favorites"); } catch {}
  }, []);

  // 从收藏中移除
  const removeFromFavorites = useCallback((symbolId: number) => {
    setFavorites((prev) => {
      const next = new Set(prev);
      next.delete(symbolId);
      try { localStorage.setItem("ic_favorites", JSON.stringify([...next])); } catch {}
      return next;
    });
  }, []);

  // 刷新交易计划
  const handleRefreshPlan = useCallback(async () => {
    setRefreshingPlan(true);
    try {
      await ctx.generateTradeSetup();
    } catch {
      // 错误提示由 AppContext.generateTradeSetup 统一处理
    } finally {
      setRefreshingPlan(false);
    }
  }, [ctx]);

  // 自动加载第一个快捷标的
  useEffect(() => {
    if (quickSymbols.length > 0 && !ctx.activeSymbolId && !ctx.detail) {
      ctx.loadSymbolDetail(quickSymbols[0].symbol_id, { focus: true, barLimit: 500 });
    }
  }, [quickSymbols.length]); // eslint-disable-line

  /* ════════════════════════════════════════════════════
     数据计算层
     ════════════════════════════════════════════════════ */

  const setup = detail?.latest_trade_setup ?? null;
  const icText = {
    editPlan: t("icEditPlan"),
    exitEdit: t("icExitEdit"),
    savePlan: t("icSavePlan"),
    cancelEdit: t("icCancelEdit"),
    manual: t("icManual"),
    system: t("icSystem"),
    overview: t("icOverview"),
    scenario: t("icScenario"),
    tranches: t("icTranches"),
    future: t("icFuture"),
    riskSettings: t("icRiskSettings"),
    alertSettings: t("icAlertSettings"),
    atrMultiplier: t("icAtrMultiplier"),
    mediumPosition: t("icMediumPosition"),
    highPosition: t("icHighPosition"),
    maxLossPct: t("icMaxLossPct"),
    stopNearPct: t("icStopNearPct"),
    targetNearPct: t("icTargetNearPct"),
    rsiOverbought: t("icRsiOverbought"),
    rsiOversold: t("icRsiOversold"),
    enableStopLoss: t("icEnableStopLoss"),
    enableTarget: t("icEnableTarget"),
    enableRsi: t("icEnableRsi"),
    enableMacd: t("icEnableMacd"),
    invalidPlan: t("icInvalidPlan"),
    editTranches: t("icEditTranches"),
    saveTranches: t("icSaveTranches"),
    addTranche: t("icAddTranche"),
    resetTranches: t("icResetTranches"),
    trancheLabel: t("icTrancheLabel"),
    futureTuning: t("icFutureTuning"),
    horizonDays: t("icHorizonDays"),
    pullbackPct: t("icPullbackPct"),
    positionPct: t("icPositionPct"),
    bandPct: t("icBandPct"),
    scalePct: t("icScalePct"),
  };

  useEffect(() => {
    if (setup && !tradePlanEditing) setTradePlanDraft(planDraftFromSetup(setup));
  }, [setup?.id, setup?.entry_min, setup?.entry_max, setup?.stop_loss, setup?.target_price, setup?.recommended_position_pct, setup?.recommended_position_amount, tradePlanEditing]);

  useEffect(() => {
    if (setup && !trancheEditing) setTrancheDrafts(trancheDraftFromSetup(setup));
  }, [setup?.id, setup?.manual_tranche_plan_json, setup?.tranche_plan, trancheEditing]);

  useEffect(() => {
    try { localStorage.setItem("ic_risk_settings", JSON.stringify(riskSettings)); } catch {}
  }, [riskSettings]);

  useEffect(() => {
    try { localStorage.setItem("ic_alert_settings", JSON.stringify(alertSettings)); } catch {}
  }, [alertSettings]);

  const updateTradePlanDraft = useCallback((field: keyof TradePlanDraft, value: number | null) => {
    setTradePlanDraft((prev) => ({ ...(prev ?? {} as TradePlanDraft), [field]: value }));
  }, []);

  const handleEditPlan = useCallback(() => {
    if (!setup) return;
    setTradePlanDraft(planDraftFromSetup(setup));
    setTradePlanEditing(true);
  }, [setup]);

  const handleCancelPlanEdit = useCallback(() => {
    if (setup) setTradePlanDraft(planDraftFromSetup(setup));
    setTradePlanEditing(false);
  }, [setup]);

  const handleApplyPlanOverrides = useCallback(async () => {
    if (!tradePlanDraft) return;
    if (tradePlanDraft.entry_min != null && tradePlanDraft.entry_max != null && tradePlanDraft.entry_min > tradePlanDraft.entry_max) {
      ctx.showToast("error", icText.invalidPlan);
      return;
    }
    const overrides: TradeSetupOverrides = {
      entry_min: tradePlanDraft.entry_min,
      entry_max: tradePlanDraft.entry_max,
      stop_loss: tradePlanDraft.stop_loss,
      target_price: tradePlanDraft.target_price,
      recommended_position_pct: tradePlanDraft.recommended_position_pct != null ? tradePlanDraft.recommended_position_pct / 100 : null,
      recommended_position_amount: tradePlanDraft.recommended_position_amount,
    };
    setRefreshingPlan(true);
    try {
      await ctx.generateTradeSetup(overrides);
      setTradePlanEditing(false);
    } finally {
      setRefreshingPlan(false);
    }
  }, [ctx, icText.invalidPlan, tradePlanDraft]);

  const updateTrancheDraft = useCallback((index: number, field: keyof TrancheDraft, value: string | number) => {
    setTrancheDrafts((prev) => prev.map((item, i) => (i === index ? { ...item, [field]: value } : item)));
  }, []);

  const handleEditTranches = useCallback(() => {
    if (!setup) return;
    setTrancheDrafts(trancheDraftFromSetup(setup));
    setTrancheEditing(true);
  }, [setup]);

  const handleSaveTranches = useCallback(async () => {
    if (!setup || !ctx.activeSymbolId) return;
    const payload = normalizeTrancheDrafts(trancheDrafts);
    const totalPct = payload.reduce((sum, item) => sum + item.position_pct, 0);
    if (totalPct > (setup.recommended_position_pct ?? 0) + 0.0001) {
      ctx.showToast("error", `${icText.tranches} > ${percent(setup.recommended_position_pct)}`);
      return;
    }
    setRefreshingPlan(true);
    try {
      await api.saveTradeSetupTranches(setup.id, { tranche_plan: payload });
      await ctx.loadSymbolDetail(ctx.activeSymbolId, { force: true });
      setTrancheEditing(false);
    } finally {
      setRefreshingPlan(false);
    }
  }, [ctx, icText.tranches, setup, trancheDrafts]);

  const handleResetTranches = useCallback(async () => {
    if (!setup || !ctx.activeSymbolId) return;
    setRefreshingPlan(true);
    try {
      await api.saveTradeSetupTranches(setup.id, { tranche_plan: [] });
      await ctx.loadSymbolDetail(ctx.activeSymbolId, { force: true });
      setTrancheEditing(false);
    } finally {
      setRefreshingPlan(false);
    }
  }, [ctx, setup]);

  const updateFutureTuning = useCallback((field: keyof FuturePlanTuning, value: number | null) => {
    const scenario = ctx.futurePlanScenario;
    setFuturePlanTunings((prev) => ({
      ...prev,
      [scenario]: { ...(DEFAULT_FUTURE_TUNINGS[scenario] ?? {}), ...(prev[scenario] ?? {}), [field]: value ?? undefined },
    }));
  }, [ctx.futurePlanScenario]);

  const fieldSourceBadge = useCallback((field: string) => {
    if (setup?.field_sources?.[field] !== "manual") return null;
    return <span className="ic__manual-badge">{icText.manual}</span>;
  }, [setup?.field_sources, icText.manual]);

  // Entry price & quantity（图表点击设置的入场价优先）
  const baseScenarios = setup?.return_scenarios;
  const _refPrice = baseScenarios?.reference_price ?? computeSuggestedPrice(detail);
  const effectiveEntryPrice = chartEntryPrice ?? (Number(ctx.simPrice) > 0 ? Number(ctx.simPrice) : Number(_refPrice));
  const entryPrice = effectiveEntryPrice;
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

  // ── Chart data memo（含全部技术指标） ──
  interface ChartDataExt {
    bars: WorkbenchBar[]; dates: string[]; closes: number[];
    candlestick: number[][]; volume: Array<{ value: number; itemStyle: { color: string } }>;
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

  // ── 价格预警检测 ──
  useEffect(() => {
    if (!chartData || !setup || !detail?.bars?.length) return;
    const lastClose = detail.bars[detail.bars.length - 1].close;
    const lastDate = detail.bars[detail.bars.length - 1].trade_date;
    const alerts: typeof priceAlerts = [];
    const stopLoss = (scenarios as ScenarioPreview)?._adjStop ?? setup.stop_loss;
    const targetPrice = (scenarios as ScenarioPreview)?._adjTarget ?? setup.target_price;

    // 1. 跌破止损
    if (alertSettings.enableStopLoss && stopLoss != null && lastClose <= stopLoss) {
      alerts.push({
        id: `stop_${lastDate}`, type: "stop_loss", level: "danger",
        message: template("alertStopLoss", { price: score(lastClose), stop: score(stopLoss) }),
        detail: template("icPriceBelowStop", { price: score(lastClose), stop: score(stopLoss) }),
        timestamp: Date.now(),
      });
    }
    // 2. 接近止损
    else if (alertSettings.enableStopLoss && stopLoss != null && lastClose > 0 && ((lastClose - stopLoss) / lastClose) < alertSettings.stopNearPct / 100) {
      alerts.push({
        id: `stop_near_${lastDate}`, type: "stop_near", level: "warning",
        message: template("alertNearStopLoss", { price: score(lastClose), stop: score(stopLoss) }),
        detail: template("icNearStopLoss", { pct: percent((lastClose - stopLoss) / lastClose) }),
        timestamp: Date.now(),
      });
    }

    // 3. 突破目标价
    if (alertSettings.enableTarget && targetPrice != null && lastClose >= targetPrice) {
      alerts.push({
        id: `target_${lastDate}`, type: "target_hit", level: "info",
        message: template("alertTargetHit", { price: score(lastClose), target: score(targetPrice) }),
        detail: template("icPriceAboveTarget", { price: score(lastClose), target: score(targetPrice) }),
        timestamp: Date.now(),
      });
    }
    // 4. 接近目标
    else if (alertSettings.enableTarget && targetPrice != null && lastClose > 0 && ((targetPrice - lastClose) / targetPrice) < alertSettings.targetNearPct / 100) {
      alerts.push({
        id: `target_near_${lastDate}`, type: "target_near", level: "info",
        message: template("alertNearTarget", { price: score(lastClose), target: score(targetPrice) }),
        detail: template("icNearTarget", { pct: percent((targetPrice - lastClose) / targetPrice) }),
        timestamp: Date.now(),
      });
    }

    // 5. RSI 超买超卖
    const lastRSI = chartData.rsi ? chartData.rsi[chartData.rsi.length - 1] : null;
    if (alertSettings.enableRsi && lastRSI != null && lastRSI > alertSettings.rsiOverbought) {
      alerts.push({
        id: `rsi_ob_${lastDate}`, type: "rsi_overbought", level: "warning",
        message: template("alertRSIOverbought", { rsi: lastRSI.toFixed(1) }),
        detail: template("icRSIOverboughtWarn", { period: lastRSI.toFixed(1), threshold: alertSettings.rsiOverbought }),
        timestamp: Date.now(),
      });
    } else if (alertSettings.enableRsi && lastRSI != null && lastRSI < alertSettings.rsiOversold) {
      alerts.push({
        id: `rsi_os_${lastDate}`, type: "rsi_oversold", level: "info",
        message: template("alertRSIOversold", { rsi: lastRSI.toFixed(1) }),
        detail: template("icRSIOversoldOpportunity", { period: lastRSI.toFixed(1), threshold: alertSettings.rsiOversold }),
        timestamp: Date.now(),
      });
    }

    // 6. MACD 金叉/死叉（最近一根K线）
    if (alertSettings.enableMacd && chartData.macdSignals.length > 0) {
      const latestSignal = chartData.macdSignals[chartData.macdSignals.length - 1];
      const signalAge = chartData.dates.length - 1 - latestSignal.index;
      if (signalAge <= 2) { // 最近2根K线内的信号
        alerts.push({
          id: `macd_${latestSignal.type}_${lastDate}`, type: latestSignal.type === "golden" ? "macd_golden" : "macd_death",
          level: latestSignal.type === "golden" ? "info" : "warning",
          message: latestSignal.type === "golden" ? t("alertMACDGolden") : t("alertMACDDeath"),
          detail: template("icMACDCrossSignal", {
            tense: latestSignal.index === chartData.dates.length - 1 ? t("icLatest") : t("icRecent"),
            type: latestSignal.type === "golden" ? t("icGoldenCross") : t("icDeathCross"),
          }),
          timestamp: Date.now(),
        });
      }
    }

    setPriceAlerts(alerts);
  }, [chartData, scenarios, setup, detail?.bars, alertSettings]); // eslint-disable-line

  // Active future buy plan
  const activeFutureBuyPlan = useMemo(() => {
    const s = detail?.latest_trade_setup ?? null;
    if (!s) return [];
    const totalCapital = ctx.workbench?.portfolio?.total_capital ?? 0;
    const investableRatio = ctx.workbench?.portfolio?.investable_ratio ?? 1;
    return getActiveFutureBuyPlan(s, ctx.futurePlanScenario, ctx.futurePlanCustom, totalCapital, investableRatio, futurePlanTunings[ctx.futurePlanScenario]);
  }, [detail?.latest_trade_setup, ctx.futurePlanScenario, ctx.futurePlanCustom, ctx.workbench?.portfolio, futurePlanTunings]);

  // ── 风险指标计算 ──
  const riskMetrics = useMemo(() => {
    if (!setup || !(entryPrice > 0)) return null;
    const stop = (scenarios as ScenarioPreview)?._adjStop ?? setup.stop_loss;
    const target = (scenarios as ScenarioPreview)?._adjTarget ?? setup.target_price;
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
    const atrStopRef = lastATR != null ? lastATR * riskSettings.atrMultiplier : null;

    // 仓位集中度估算
    const positionPct = setup.recommended_position_pct ?? 0;
    const concentrationLevel =
      positionPct >= riskSettings.concentrationHighPct ? "high" : positionPct >= riskSettings.concentrationMediumPct ? "medium" : "low";

    // 单笔最大亏损金额
    const maxLossPerShare = stop != null ? entryPrice - stop : 0;
    const maxLossAmount = maxLossPerShare * quantity;

    return {
      rrRatio, stopDistancePct, currentStopDist, atrStopRef,
      concentrationLevel, maxLossAmount, maxLossPerShare,
      reward, riskAmount, currentPrice, stop, target,
      maxLossLimitAmount: (ctx.workbench?.portfolio?.total_capital ?? 0) * (riskSettings.maxLossPct / 100),
    };
  }, [setup, scenarios, entryPrice, quantity, chartData?.atr, detail?.bars, riskSettings, ctx.workbench?.portfolio?.total_capital]);

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

  // 渲染搜索历史和收藏管理弹窗
  const renderHistoryModal = () => {
    if (!showHistoryModal) return null;

    // 获取收藏的标的详情（从quickSymbols和searchHistory中查找）
    const favoriteSymbols = Array.from(favorites).map((id) => {
      const fromQuick = quickSymbols.find((s) => s.symbol_id === id);
      if (fromQuick) return fromQuick;
      const fromHistory = searchHistory.find((s) => s.symbol_id === id);
      return fromHistory || { symbol_id: id, symbol: `ID:${id}`, name: t("unknown") };
    });

    return (
      <div className="ic__modal-overlay" onClick={() => setShowHistoryModal(false)}>
        <div className="ic__modal-content" onClick={(e) => e.stopPropagation()}>
          <div className="ic__modal-header">
            <h3>{t("historyFavorites")}</h3>
            <button type="button" className="ic__modal-close" onClick={() => setShowHistoryModal(false)}>×</button>
          </div>

          <div className="ic__modal-body">
            {/* 收藏列表 */}
            <section className="ic__modal-section">
              <div className="ic__section-header">
                <h4>{t("favorites")} ({favorites.size})</h4>
                {favorites.size > 0 && (
                  <Button size="small" danger onClick={clearFavorites}>
                    {t("clearAll")}
                  </Button>
                )}
              </div>
              {favorites.size === 0 ? (
                <div className="ic__empty-state">{t("noFavorites")}</div>
              ) : (
                <div className="ic__symbol-list">
                  {favoriteSymbols.map((item) => (
                    <div key={item.symbol_id} className="ic__symbol-item">
                      <button type="button" className="ic__symbol-btn" onClick={() => { handleSelectSymbol(item.symbol_id); setShowHistoryModal(false); }}>
                        <span className="symbol-code">{item.symbol}</span>
                        <span className="symbol-name">{item.name}</span>
                      </button>
                      <button type="button" className="ic__remove-btn" onClick={() => removeFromFavorites(item.symbol_id)}>
                        {t("remove")}
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </section>

            {/* 搜索历史 */}
            <section className="ic__modal-section">
              <div className="ic__section-header">
                <h4>{t("searchHistory")} ({searchHistory.length})</h4>
                {searchHistory.length > 0 && (
                  <Button size="small" danger onClick={clearSearchHistory}>
                    {t("clearAll")}
                  </Button>
                )}
              </div>
              {searchHistory.length === 0 ? (
                <div className="ic__empty-state">{t("noSearchHistory")}</div>
              ) : (
                <div className="ic__symbol-list">
                  {searchHistory.map((item) => (
                    <div key={item.symbol_id} className="ic__symbol-item">
                      <button type="button" className="ic__symbol-btn" onClick={() => { handleSelectSymbol(item.symbol_id, item); setShowHistoryModal(false); }}>
                        <span className="symbol-code">{item.symbol}</span>
                        <span className="symbol-name">{item.name}</span>
                      </button>
                      <div className="ic__symbol-actions">
                        <button type="button" className="ic__fav-toggle" onClick={() => toggleFavorite(item.symbol_id)}>
                          {favorites.has(item.symbol_id) ? "★" : "☆"}
                        </button>
                        <button type="button" className="ic__remove-btn" onClick={() => removeFromHistory(item.symbol_id)}>
                          {t("delete")}
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>
        </div>
      </div>
    );
  };

  // Trigger formatting functions
  const formatOpenTrigger = useCallback((item: TradeSetup) => {
    if (item.entry_min === null || item.entry_max === null) return "-";
    return template("icOpenTrigger", { min: item.entry_min, max: item.entry_max });
  }, [ctx.locale]);

  const formatAddTrigger = useCallback((item: TradeSetup) => {
    if (!item.allow_add_position) return "-";
    return t("icAddTrigger");
  }, [ctx.locale]);

  const formatStopTrigger = useCallback((item: TradeSetup) => {
    if (item.stop_loss === null) return "-";
    return template("icStopTrigger", { price: item.stop_loss });
  }, [ctx.locale]);

  const formatTrimTrigger = useCallback((item: TradeSetup) => {
    if (item.target_price === null) return "-";
    return template("icTrimTrigger", { price: item.target_price });
  }, [ctx.locale]);

  // ── Candlestick chart option ──
  const chartOption = useMemo(() => {
    if (!chartData) return null;
    const signals = detail?.latest_trade_setup?.chart_signals ?? [];

    const markLineData = signals.map((sig) => {
      // 复用 constants/chartTheme 中统一导出的信号颜色映射
      const colorMap = SIGNAL_COLOR_MAP;
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
        trigger: "axis" as const,
        axisPointer: {
          type: "cross",
          crossStyle: { color: "rgba(100,100,100,0.25)", width: 1 },
          label: { backgroundColor: "#333", fontSize: 10 },
        },
        // TODO: 待后续类型强化——ECharts tooltip 回调参数类型较复杂，暂保留 any
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
          html += `<div>${t("chartOpen")}:${score(open)} ${t("chartClose")}:${score(close)} ${t("chartLow")}:${score(low)} ${t("chartHigh")}:${score(high)}</div>`;
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
        { type: "value", scale: true, splitLine: { show: false }, axisLabel: { fontSize: 10, formatter: (v: any) => formatVolume(Number(v)) } }, // TODO: 待后续类型强化——ECharts axisLabel 回调参数暂保留 any
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
  }, [chartData, detail?.latest_trade_setup, activeFutureBuyPlan, setup, ctx.locale]);

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
  }, [chartData?.macd, showMACD, chartData?.dates, ctx.locale]);

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
            { yAxis: 70, lineStyle: { color: "#b42318" }, label: { formatter: t("rsiOBLineLabel"), fontSize: 9 } },
            { yAxis: 30, lineStyle: { color: "#0f766e" }, label: { formatter: t("rsiOSLineLabel"), fontSize: 9 } },
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
            value: s.type === "overbought" ? t("icRsiOverbought") : t("icRsiOversold"),
            symbol: s.type === "overbought" ? "triangle" : "triangle",
            symbolSize: 8,
            symbolRotate: s.type === "overbought" ? 180 : 0,
            itemStyle: { color: s.type === "overbought" ? "#b42318" : "#0f766e" },
            label: { fontSize: 9, color: s.type === "overbought" ? "#b42318" : "#0f766e" },
          })),
        } as any, // TODO: 待后续类型强化——ECharts markPoint 配置类型暂保留 as any
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

  const renderFactorExplanation = () => {
    if (!ctx.activeSymbolId) return null;
    const labels = ctx.locale === "en-US" ? {
      title: "Dynamic Factor Explanation",
      mode: "Mode",
      model: "Model",
      tradeDate: "Trade Date",
      cutoff: "Data Cutoff",
      quality: "Factor Quality",
      timing: "Factor Timing",
      alpha: "Model Alpha",
      macro: "Macro Regime",
      multiplier: "Position Multiplier",
      marketLiquidity: "Market Liquidity Score",
      marketAmountChange: "Market Turnover Change",
      marketAmountZ: "Turnover Z20",
      advancingRatio: "Advancing Ratio",
      leverageDivergence: "Leverage/Turnover Divergence",
      factor: "Factor",
      raw: "Raw",
      normalized: "Normalized",
      coefficient: "Coefficient",
      contribution: "Contribution",
      imputed: "Imputed",
      unavailable: "No dynamic factor snapshot for this symbol.",
      refresh: "Refresh factor explanation",
    } : {
      title: "动态因子解释",
      mode: "模式",
      model: "模型",
      tradeDate: "交易日",
      cutoff: "数据截止",
      quality: "因子质量分",
      timing: "因子择时分",
      alpha: "模型 Alpha",
      macro: "宏观状态",
      multiplier: "仓位乘数",
      marketLiquidity: "市场流动性分",
      marketAmountChange: "全市场成交额变化",
      marketAmountZ: "成交额 Z20",
      advancingRatio: "上涨家数占比",
      leverageDivergence: "杠杆/成交额背离",
      factor: "因子",
      raw: "原值",
      normalized: "标准化",
      coefficient: "系数",
      contribution: "贡献",
      imputed: "已填充",
      unavailable: "该标的暂无动态因子快照。",
      refresh: "刷新因子解释",
    };
    const factorNames: Record<string, [string, string]> = {
      ep_ttm: ["盈利收益率 E/P", "Earnings Yield E/P"],
      roe_growth: ["ROE 同比增速", "ROE Growth"],
      pb: ["市净率 PB", "Price-to-Book"],
      negative_pb: ["市净率 PB（反向）", "Negative Price-to-Book"],
      fund_flow_5d_ratio: ["5日主力净流入比", "5D Main Fund Flow"],
      main_inflow_5d_ratio: ["5日主力净流入比", "5D Main Fund Flow"],
      lhb_institution_net_ratio: ["龙虎榜机构净买额比", "Institution LHB Net Ratio"],
      turnover_zscore_20d: ["换手率 Z-Score", "Turnover Z-Score"],
      turnover_z20: ["换手率 Z-Score", "Turnover Z-Score"],
      hot_rank_percentile: ["人气榜分位", "Popularity Percentile"],
      hot_rank_attention: ["人气榜关注度", "Hot-Rank Attention"],
      tail_accumulation_proxy: ["尾盘量价抢筹代理", "Tail Accumulation Proxy"],
      cn_10y_change: ["中国10年国债变化", "CN 10Y Yield Change"],
      us_10y_change: ["美国10年国债变化", "US 10Y Yield Change"],
      margin_balance_change: ["两融余额变化", "Margin Balance Change"],
    };
    const factors = [
      ...Object.entries(factorExplanation?.explanation.factors ?? {}),
      ...Object.entries(factorExplanation?.explanation.event_factors ?? {}),
    ]
      .sort(([, left], [, right]) => Math.abs(Number(right.contribution ?? 0)) - Math.abs(Number(left.contribution ?? 0)));
    const factorLabel = (code: string) => factorNames[code]?.[ctx.locale === "en-US" ? 1 : 0] ?? code;
    const macroRegime = factorExplanation?.macro_regime
      ?? factorExplanation?.explanation.macro?.regime
      ?? "-";
    const multiplier = factorExplanation?.macro_position_multiplier
      ?? factorExplanation?.explanation.macro?.position_multiplier;
    const macroDetail = factorExplanation?.explanation.macro;

    return (
      <section className="ic__section ic__factor-section">
        <div className="panel">
          <div className="detail-card-head">
            <h3><ExperimentOutlined /> {labels.title}</h3>
            <div className="detail-actions">
              {factorExplanation && (
                <>
                  <Tag color={factorExplanation.weight_mode === "ridge" ? "green" : "blue"}>{factorExplanation.weight_mode}</Tag>
                  <Tag>{factorExplanation.trade_date}</Tag>
                </>
              )}
              <Button
                size="small"
                aria-label={labels.refresh}
                title={labels.refresh}
                icon={<ReloadOutlined />}
                loading={factorExplanationLoading}
                onClick={loadFactorExplanation}
              />
            </div>
          </div>
          {factorExplanationLoading && !factorExplanation ? (
            <div className="empty">{labels.refresh}...</div>
          ) : !factorExplanation ? (
            <div className="empty">{factorExplanationError || labels.unavailable}</div>
          ) : (
            <>
              <div className="ic__factor-summary">
                <div><span>{labels.mode}</span><strong>{factorExplanation.weight_mode}</strong></div>
                <div title={factorExplanation.model_run_id}><span>{labels.model}</span><strong>{factorExplanation.model_run_id.slice(0, 18)}</strong></div>
                <div><span>{labels.tradeDate}</span><strong>{factorExplanation.trade_date}</strong></div>
                <div><span>{labels.cutoff}</span><strong>{factorExplanation.factor_data_cutoff_at?.replace("T", " ").slice(0, 19) ?? "-"}</strong></div>
                <div><span>{labels.quality}</span><strong>{score(factorExplanation.factor_quality_score, 1)}</strong></div>
                <div><span>{labels.timing}</span><strong>{score(factorExplanation.factor_timing_score, 1)}</strong></div>
                <div><span>{labels.alpha}</span><strong>{score(factorExplanation.model_alpha_score, 1)}</strong></div>
                <div><span>{labels.macro}</span><strong>{macroRegime}</strong></div>
                <div><span>{labels.multiplier}</span><strong>{multiplier == null ? "-" : `${score(multiplier, 2)}x`}</strong></div>
                <div><span>{labels.marketLiquidity}</span><strong>{score(macroDetail?.liquidity_score, 1)}</strong></div>
                <div><span>{labels.marketAmountChange}</span><strong>{macroDetail?.market_amount_change_ratio == null ? "-" : `${score(macroDetail.market_amount_change_ratio * 100, 2)}%`}</strong></div>
                <div><span>{labels.marketAmountZ}</span><strong>{score(macroDetail?.market_amount_z20, 2)}</strong></div>
                <div><span>{labels.advancingRatio}</span><strong>{macroDetail?.advancing_ratio == null ? "-" : `${score(macroDetail.advancing_ratio * 100, 1)}%`}</strong></div>
                <div><span>{labels.leverageDivergence}</span><strong>{macroDetail?.margin_amount_divergence == null ? "-" : `${score(macroDetail.margin_amount_divergence * 100, 2)}%`}</strong></div>
              </div>
              <div className="ic__factor-table" role="table">
                <div className="ic__factor-row ic__factor-row--head" role="row">
                  <span>{labels.factor}</span>
                  <span>{labels.raw}</span>
                  <span>{labels.normalized}</span>
                  <span>{labels.coefficient}</span>
                  <span>{labels.contribution}</span>
                </div>
                {factors.map(([code, item]) => (
                  <div key={code} className="ic__factor-row" role="row">
                    <span><strong>{factorLabel(code)}</strong><small>{code}{item.is_imputed ? ` · ${labels.imputed}` : ""}</small></span>
                    <span>{score(item.raw_value, 4)}</span>
                    <span>{score(item.normalized_value, 3)}</span>
                    <span className={pnlClass(item.coefficient)}>{score(item.coefficient, 4)}</span>
                    <span className={pnlClass(item.contribution)}>{score(item.contribution, 4)}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </section>
    );
  };

  const renderPriceAlertSection = () => {
    if (!detail) return null;
    return (
      <section className="ic__price-alerts">
        <div className="detail-card-head">
          <h4>{t("priceAlerts")}</h4>
          <Button size="small" onClick={() => setAlertSettingsOpen((v) => !v)}>{icText.alertSettings}</Button>
        </div>
        {alertSettingsOpen && (
          <div className="ic__settings-grid ic__settings-grid--alerts">
            <label><span>{icText.stopNearPct}</span><InputNumber size="small" min={0} max={50} value={alertSettings.stopNearPct} onChange={(v) => setAlertSettings((prev) => ({ ...prev, stopNearPct: Number(v ?? 3) }))} /></label>
            <label><span>{icText.targetNearPct}</span><InputNumber size="small" min={0} max={50} value={alertSettings.targetNearPct} onChange={(v) => setAlertSettings((prev) => ({ ...prev, targetNearPct: Number(v ?? 5) }))} /></label>
            <label><span>{icText.rsiOverbought}</span><InputNumber size="small" min={50} max={100} value={alertSettings.rsiOverbought} onChange={(v) => setAlertSettings((prev) => ({ ...prev, rsiOverbought: Number(v ?? 75) }))} /></label>
            <label><span>{icText.rsiOversold}</span><InputNumber size="small" min={0} max={50} value={alertSettings.rsiOversold} onChange={(v) => setAlertSettings((prev) => ({ ...prev, rsiOversold: Number(v ?? 25) }))} /></label>
            <label className="ic__switch-field"><span>{icText.enableStopLoss}</span><Switch size="small" checked={alertSettings.enableStopLoss} onChange={(checked) => setAlertSettings((prev) => ({ ...prev, enableStopLoss: checked }))} /></label>
            <label className="ic__switch-field"><span>{icText.enableTarget}</span><Switch size="small" checked={alertSettings.enableTarget} onChange={(checked) => setAlertSettings((prev) => ({ ...prev, enableTarget: checked }))} /></label>
            <label className="ic__switch-field"><span>{icText.enableRsi}</span><Switch size="small" checked={alertSettings.enableRsi} onChange={(checked) => setAlertSettings((prev) => ({ ...prev, enableRsi: checked }))} /></label>
            <label className="ic__switch-field"><span>{icText.enableMacd}</span><Switch size="small" checked={alertSettings.enableMacd} onChange={(checked) => setAlertSettings((prev) => ({ ...prev, enableMacd: checked }))} /></label>
          </div>
        )}
        {priceAlerts.length > 0 && (
          <div className="ic__alert-list">
            {priceAlerts.map((alert) => (
              <div key={alert.id} className={`ic__alert-item ic__alert--${alert.level}`}>
                <span className="ic__alert-icon">{alert.level === "danger" ? "!" : alert.level === "warning" ? "!" : "i"}</span>
                <div className="ic__alert-body">
                  <strong>{alert.message}</strong>
                  <span>{alert.detail}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    );
  };

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
          <div className="detail-card-head">
            <h3>{t("riskDashboard")}</h3>
            <Button size="small" onClick={() => setRiskSettingsOpen((v) => !v)}>{icText.riskSettings}</Button>
          </div>
          {riskSettingsOpen && (
            <div className="ic__settings-grid ic__settings-grid--compact">
              <label><span>{icText.atrMultiplier}</span><InputNumber size="small" min={0.5} max={5} step={0.5} value={riskSettings.atrMultiplier} onChange={(v) => setRiskSettings((prev) => ({ ...prev, atrMultiplier: Number(v ?? 2) }))} /></label>
              <label><span>{icText.mediumPosition}</span><InputNumber size="small" min={0} max={100} value={riskSettings.concentrationMediumPct} onChange={(v) => setRiskSettings((prev) => ({ ...prev, concentrationMediumPct: Number(v ?? 10) }))} /></label>
              <label><span>{icText.highPosition}</span><InputNumber size="small" min={0} max={100} value={riskSettings.concentrationHighPct} onChange={(v) => setRiskSettings((prev) => ({ ...prev, concentrationHighPct: Number(v ?? 20) }))} /></label>
              <label><span>{icText.maxLossPct}</span><InputNumber size="small" min={0} max={100} step={0.5} value={riskSettings.maxLossPct} onChange={(v) => setRiskSettings((prev) => ({ ...prev, maxLossPct: Number(v ?? 2) }))} /></label>
            </div>
          )}
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
                <span>{t("icCurrent")}: {score(rm.currentPrice)}</span>
                <span>{t("icStopLossLabel")}: {score(rm.stop)}</span>
                {rm.atrStopRef != null && <span>{riskSettings.atrMultiplier}×ATR: {score(rm.atrStopRef)}</span>}
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
                  percent={Math.min(100, ((setup?.recommended_position_pct ?? 0) * 100 / Math.max(riskSettings.concentrationHighPct, 1)) * 100)}
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
              <span className="ic__risk-value" style={{ color: rm.maxLossLimitAmount > 0 && rm.maxLossAmount > rm.maxLossLimitAmount ? "#b42318" : rm.maxLossAmount > 0 ? "#f59e0b" : "#6b7280" }}>
                {rm.maxLossAmount > 0 ? `-¥${rm.maxLossAmount.toFixed(0)}` : "-"}
              </span>
              <div className="ic__risk-detail">
                <span>{t("icLossPerShare")}: {score(rm.maxLossPerShare)}</span>
                <span>{t("icQuantityLabel")}: {quantity}</span>
                {rm.maxLossLimitAmount > 0 && <span>{t("icUpperLimit")}: {money(rm.maxLossLimitAmount)}</span>}
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
  const renderTradePlan = () => {
    const draft = tradePlanDraft;
    const currentFutureTuning = { ...(DEFAULT_FUTURE_TUNINGS[ctx.futurePlanScenario] ?? {}), ...(futurePlanTunings[ctx.futurePlanScenario] ?? {}) };
    const renderPlanEditNumber = (label: string, field: keyof TradePlanDraft, suffix?: string) => (
      <label className="ic__plan-edit-field">
        <span>{label}</span>
        <InputNumber
          size="small"
          min={0}
          value={draft?.[field] ?? null}
          addonAfter={suffix}
          onChange={(value) => updateTradePlanDraft(field, value == null ? null : Number(value))}
        />
      </label>
    );

    const planOverview = setup ? (
      <div className="ic__plan-tab-panel">
        {tradePlanEditing && draft ? (
          <div className="ic__plan-edit-grid">
            {renderPlanEditNumber(t("buyZone") + " 下限", "entry_min")}
            {renderPlanEditNumber(t("buyZone") + " 上限", "entry_max")}
            {renderPlanEditNumber(t("stopLoss"), "stop_loss")}
            {renderPlanEditNumber(t("target"), "target_price")}
            {renderPlanEditNumber(t("recommendedPosition"), "recommended_position_pct", "%")}
            {renderPlanEditNumber(t("positionAmount"), "recommended_position_amount")}
          </div>
        ) : (
          <div className="ic__plan-summary-grid">
            <div className="ic__plan-summary-card">
              <span>{t("buyZone")}</span>
              <strong>{setup.entry_min !== null ? score(setup.entry_min) : "-"} - {setup.entry_max !== null ? score(setup.entry_max) : "-"}</strong>
              <em>{fieldSourceBadge("entry_min")}{fieldSourceBadge("entry_max")}</em>
            </div>
            <div className="ic__plan-summary-card">
              <span>{t("stopLoss")}</span>
              <strong>{score((scenarios as ScenarioPreview)?._adjStop ?? setup.stop_loss)}</strong>
              <em>{fieldSourceBadge("stop_loss")}</em>
            </div>
            <div className="ic__plan-summary-card">
              <span>{t("target")}</span>
              <strong>{score((scenarios as ScenarioPreview)?._adjTarget ?? setup.target_price)}</strong>
              <em>{fieldSourceBadge("target_price")}</em>
            </div>
            <div className="ic__plan-summary-card">
              <span>{t("recommendedPosition")}</span>
              <strong>{percent(setup.recommended_position_pct)}</strong>
              <em>{money(setup.recommended_position_amount)} {fieldSourceBadge("recommended_position_pct")}{fieldSourceBadge("recommended_position_amount")}</em>
            </div>
          </div>
        )}

        <div className="item-subline ic__plan-meta-line">
          {joinParts([
            `${t("riskReward")}: ${setup.risk_reward_ratio != null ? score(setup.risk_reward_ratio) : "-"}`,
            `${t("allowAdd")}: ${setup.allow_add_position ? t("yes") : t("no")}`,
            `${t("stage")}: ${stageLabel(setup.stage)}`,
            `${t("action")}: ${actionLabel(setup.action)}`,
          ])}
        </div>

        <div className="item-subline ic__plan-meta-line">
          {joinParts([
            `${t("stageCap")}: ${setup.stage_cap_pct != null ? percent(setup.stage_cap_pct) : "-"}`,
            `${t("stageRoom")}: ${setup.remaining_stage_pct != null ? percent(setup.remaining_stage_pct) : "-"}`,
            `${t("currentPosition")}: ${setup.current_position_pct != null ? percent(setup.current_position_pct) : "-"}`,
            `${t("riskBudget")}: ${setup.risk_budget_amount != null ? money(setup.risk_budget_amount) : "-"}`,
            `${t("riskShare")}: ${setup.risk_per_share != null ? score(setup.risk_per_share) : "-"}`,
          ])}
        </div>

        <div className="ic__trigger-grid">
          <div><strong>{t("openTrigger")}</strong><span>{formatOpenTrigger(setup)}</span></div>
          <div><strong>{t("addTrigger")}</strong><span>{formatAddTrigger(setup)}</span></div>
          <div><strong>{t("stopTrigger")}</strong><span>{formatStopTrigger(setup)}</span></div>
          <div><strong>{t("trimTrigger")}</strong><span>{formatTrimTrigger(setup)}</span></div>
        </div>
      </div>
    ) : <div className="empty">{t("latestTradeSetupEmpty")}</div>;

    const scenarioPane = scenarios ? (
      <div id="orderScenarioPreview" className="scenario-preview-card ic__plan-tab-panel">
        <div className="scenario-preview-head">
          <span className="scenario-preview-meta">
            {joinParts([
              `${t("plannedOrder")}: ${quantity}`,
              `${t("referencePrice")}: ${score(entryPrice)}${chartEntryPrice ? ` (${t("chartEntry")})` : ""}`,
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
    ) : <div className="empty">{t("latestTradeSetupEmpty")}</div>;

    const tranchePane = setup ? (
      <div className="detail-card ic__plan-tab-panel">
        <div className="ic__tab-toolbar">
          <span className="panel-meta">{setup.manual_tranche_plan_json ? icText.manual : icText.system}</span>
          <div>
            {trancheEditing ? (
              <>
                <Button size="small" onClick={() => { setTrancheDrafts(trancheDraftFromSetup(setup)); setTrancheEditing(false); }}>{icText.cancelEdit}</Button>
                <Button size="small" type="primary" loading={refreshingPlan} onClick={handleSaveTranches}>{icText.saveTranches}</Button>
              </>
            ) : (
              <Button size="small" onClick={handleEditTranches}>{icText.editTranches}</Button>
            )}
            {setup.manual_tranche_plan_json && <Button size="small" loading={refreshingPlan} onClick={handleResetTranches}>{icText.resetTranches}</Button>}
          </div>
        </div>

        {trancheEditing ? (
          <div className="ic__tranche-editor">
            {trancheDrafts.map((tranche, i) => (
              <div key={i} className="ic__tranche-edit-row">
                <label><span>{icText.trancheLabel}</span><Input size="small" value={tranche.label} onChange={(e) => updateTrancheDraft(i, "label", e.target.value)} /></label>
                <label><span>{t("tranchePct")}</span><InputNumber size="small" min={0} max={100} value={tranche.position_pct} addonAfter="%" onChange={(v) => updateTrancheDraft(i, "position_pct", Number(v ?? 0))} /></label>
                <label><span>{t("positionAmount")}</span><InputNumber size="small" min={0} value={tranche.amount} onChange={(v) => updateTrancheDraft(i, "amount", Number(v ?? 0))} /></label>
                <label><span>{t("trigger")}</span><Input size="small" value={tranche.trigger} onChange={(e) => updateTrancheDraft(i, "trigger", e.target.value)} /></label>
                <Button size="small" danger onClick={() => setTrancheDrafts((prev) => prev.filter((_, idx) => idx !== i))}>{t("delete")}</Button>
              </div>
            ))}
            <Button size="small" onClick={() => setTrancheDrafts((prev) => [...prev, { label: trancheLabel("Custom"), position_pct: 0, amount: 0, trigger: "" }])}>{icText.addTranche}</Button>
            <div className="item-subline">
              {joinParts([
                `${t("recommendedPosition")}: ${percent(setup.recommended_position_pct)}`,
                `${t("tranchePct")}: ${percent(normalizeTrancheDrafts(trancheDrafts).reduce((sum, item) => sum + item.position_pct, 0))}`,
              ])}
            </div>
          </div>
        ) : setup.tranche_plan && setup.tranche_plan.length > 0 ? (
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
        ) : <div className="empty">{t("latestTradeSetupEmpty")}</div>}
      </div>
    ) : <div className="empty">{t("latestTradeSetupEmpty")}</div>;

    const futurePane = activeFutureBuyPlan.length > 0 ? (
      <div className="detail-card ic__plan-tab-panel">
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

        <div className="future-custom-grid ic__future-tuning-grid">
          <label><span>{icText.horizonDays}</span><InputNumber size="small" min={1} max={365} value={currentFutureTuning.horizonDays} onChange={(v) => updateFutureTuning("horizonDays", v == null ? null : Number(v))} /></label>
          <label><span>{icText.pullbackPct}</span><InputNumber size="small" min={0} max={50} step={0.5} value={currentFutureTuning.pullbackPct} onChange={(v) => updateFutureTuning("pullbackPct", v == null ? null : Number(v))} /></label>
          <label><span>{icText.positionPct}</span><InputNumber size="small" min={0} max={100} step={0.5} value={currentFutureTuning.positionPct} onChange={(v) => updateFutureTuning("positionPct", v == null ? null : Number(v))} /></label>
          <label><span>{icText.bandPct}</span><InputNumber size="small" min={0.1} max={50} step={0.1} value={currentFutureTuning.bandPct} onChange={(v) => updateFutureTuning("bandPct", v == null ? null : Number(v))} /></label>
          <label><span>{icText.scalePct}</span><InputNumber size="small" min={0} max={300} step={5} value={currentFutureTuning.scalePct} onChange={(v) => updateFutureTuning("scalePct", v == null ? null : Number(v))} /></label>
        </div>

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
    ) : <div className="empty">{t("latestTradeSetupEmpty")}</div>;

    return (
      <section className="ic__section ic__trade-plan-section">
        <div className="panel">
          <div className="detail-card-head">
            <h3>{t("tradeSetup")}</h3>
            <div className="detail-actions">
              {setup && (tradePlanEditing ? (
                <>
                  <Button size="small" onClick={handleCancelPlanEdit}>{icText.cancelEdit}</Button>
                  <Button size="small" type="primary" loading={refreshingPlan} onClick={handleApplyPlanOverrides}>{icText.savePlan}</Button>
                </>
              ) : (
                <Button size="small" onClick={handleEditPlan}>{icText.editPlan}</Button>
              ))}
              <Button size="small" loading={refreshingPlan} onClick={handleRefreshPlan}>{t("refreshPlan")}</Button>
            </div>
          </div>
          <Tabs
            className="ic__trade-tabs"
            size="small"
            items={[
              { key: "overview", label: icText.overview, children: planOverview },
              { key: "scenario", label: icText.scenario, children: scenarioPane },
              { key: "tranches", label: icText.tranches, children: tranchePane },
              { key: "future", label: icText.future, children: futurePane },
            ]}
          />
        </div>
      </section>
    );
  };

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
              <Button size="small" onClick={() => ctx.setChartWindowSize(Math.min(500, ctx.chartWindowSize + 20))}>{t("zoomOut")}</Button>
              <Button size="small" onClick={() => { ctx.setChartWindowSize(60); ctx.setChartRange(null); }}>{t("resetZoom")}</Button>
            </div>
          </div>
          {/* 指标面板开关 */}
          <div className="ic__indicator-toggles">
            <button type="button" className={`ic__toggle-btn${showMACD ? " active" : ""}`} onClick={() => setShowMACD(!showMACD)}>MACD</button>
            <button type="button" className={`ic__toggle-btn${showRSI ? " active" : ""}`} onClick={() => setShowRSI(!showRSI)}>RSI</button>
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
                  onEvents={{
                    // TODO: 待后续类型强化——ECharts 事件回调参数类型暂保留 any
                    click: (params: any) => {
                      if (params.dataIndex != null && chartData?.closes[params.dataIndex] != null) {
                        const clickedClose = chartData.closes[params.dataIndex];
                        setChartEntryPrice(clickedClose);
                        // 同步到全局模拟价格
                        ctx.setSimPrice(String(clickedClose));
                      }
                    },
                  }}
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
                <span>{t("chartOpen")}:{score(lastBar.open)} {t("chartHigh")}:{score(lastBar.high)} {t("chartLow")}:{score(lastBar.low)} {t("chartClose")}:{score(lastBar.close)}</span>
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

  const renderBacktestSection = () => {
    if (!ctx.portfolioId) return null;
    return (
      <section className="ic__section ic__backtest-section">
        <div className="panel">
          <div className="detail-card-head">
            <h3>{t("backtestValidation")}</h3>
            <div className="detail-actions">
              {backtestResult && (
                <Button size="small" onClick={() => setBacktestResult(null)}>{t("clear")}</Button>
              )}
            </div>
          </div>
          <div className="ic__backtest-layout">
            <BacktestConfig
              portfolioId={ctx.portfolioId}
              activeSymbolId={ctx.activeSymbolId}
              onResult={setBacktestResult}
            />
            <BacktestResult result={backtestResult} />
          </div>
        </div>
      </section>
    );
  };

  if (isMobile) {
    return (
      <div className="investment-center ic__mobile-layout">
        <div className="ic__search-bar">
          <Input prefix={<SearchOutlined style={{ color: "var(--muted)" }} />}
            placeholder={t("searchSymbolPlaceholder")}
            allowClear value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onPressEnter={() => { if (searchResults.length > 0) handleSelectSymbol(searchResults[0].id, searchResults[0]); }}
          />
          {/* 搜索结果 */}
          {searchResults.length > 0 && (
            <div className="ic__search-dropdown">
              {searchResults.map((item) => (
                <button type="button" key={item.id} className="ic__search-result-item" onClick={() => handleSelectSymbol(item.id, item)}>
                  <span className={`ic__fav-star${favorites.has(item.id) ? " active" : ""}`} onClick={(e) => { e.stopPropagation(); toggleFavorite(item.id); }}>
                    {favorites.has(item.id) ? "★" : "☆"}
                  </span>
                  <span className="symbol-code">{item.symbol}</span>
                  <span className="symbol-name">{item.name}</span>
                </button>
              ))}
            </div>
          )}
          {/* 搜索历史（无搜索时显示） */}
          {!searchQuery && searchHistory.length > 0 && (
            <div className="ic__search-dropdown ic__search-history">
              <div className="ic__history-header">{t("searchHistory")}</div>
              {searchHistory.map((item) => (
                <button type="button" key={item.symbol_id} className="ic__search-result-item" onClick={() => handleSelectSymbol(item.symbol_id, item)}>
                  <span className={`ic__fav-star${favorites.has(item.symbol_id) ? " active" : ""}`} onClick={(e) => { e.stopPropagation(); toggleFavorite(item.symbol_id); }}>
                    {favorites.has(item.symbol_id) ? "★" : "☆"}
                  </span>
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
              <button type="button" key={item.symbol_id} className={`ic__chip${ctx.activeSymbolId === item.symbol_id ? " active" : ""}`}
                onClick={() => handleSelectSymbol(item.symbol_id)}>
                {item.symbol} {item.name}
              </button>
            ))}
          </div>
        )}
        {renderFactorExplanation()}
        {renderPriceAlertSection()}
        {renderRiskDashboard()}
        {renderTradePlan()}
        {renderChartSection()}
        {renderBacktestSection()}
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
          onPressEnter={() => { if (searchResults.length > 0) handleSelectSymbol(searchResults[0].id, searchResults[0]); }}
          className="ic__top-input"
        />
        {/* 搜索结果 */}
        {searchResults.length > 0 && (
          <div className="ic__search-dropdown">
            {searchResults.map((item) => (
              <button type="button" key={item.id} className="ic__search-result-item" onClick={() => handleSelectSymbol(item.id, item)}>
                <span className={`ic__fav-star${favorites.has(item.id) ? " active" : ""}`} onClick={(e) => { e.stopPropagation(); toggleFavorite(item.id); }}>
                  {favorites.has(item.id) ? "★" : "☆"}
                </span>
                <span className="symbol-code">{item.symbol}</span>
                <span className="symbol-name">{item.name}</span>
              </button>
            ))}
          </div>
        )}
        {/* 搜索历史（无搜索时显示） */}
        {!searchQuery && searchHistory.length > 0 && (
          <div className="ic__search-dropdown ic__search-history">
            <div className="ic__history-header">{t("searchHistory")}</div>
            {searchHistory.map((item) => (
              <button type="button" key={item.symbol_id} className="ic__search-result-item" onClick={() => handleSelectSymbol(item.symbol_id, item)}>
                <span className={`ic__fav-star${favorites.has(item.symbol_id) ? " active" : ""}`} onClick={(e) => { e.stopPropagation(); toggleFavorite(item.symbol_id); }}>
                  {favorites.has(item.symbol_id) ? "★" : "☆"}
                </span>
                <span className="symbol-code">{item.symbol}</span>
                <span className="symbol-name">{item.name}</span>
              </button>
            ))}
          </div>
        )}
      </header>

      <div className="ic__toolbar">
        {!searchQuery && quickSymbols.length > 0 && (
          <nav className="ic__quick-bar">
            {quickSymbols.map((item) => (
              <button type="button" key={item.symbol_id} className={`ic__chip${ctx.activeSymbolId === item.symbol_id ? " active" : ""}`}
                onClick={() => handleSelectSymbol(item.symbol_id)}>
                {item.symbol} {item.name}
            </button>
          ))}
        </nav>
      )}
      </div>

      <main className="ic__main-content">
        {detail && (
          <div className="ic__symbol-header">
            <div className="symbol-title">
              <span className="symbol-code">{detail.symbol.symbol}</span>
              <span className="symbol-name">{detail.symbol.name}</span>
              <span className={`ic__fav-star ic__fav-star--lg${favorites.has(detail.symbol.id) ? " active" : ""}`}
                onClick={() => toggleFavorite(detail.symbol.id)}>
                {favorites.has(detail.symbol.id) ? "★" : "☆"}
              </span>
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
            {/* 图表入场价提示 */}
            {chartEntryPrice && (
              <div className="ic__entry-price-hint">
                <span>{t("chartEntry")}: <strong>{score(chartEntryPrice)}</strong></span>
                <button type="button" onClick={() => { setChartEntryPrice(null); ctx.setSimPrice(""); }} className="ic__hint-close">x</button>
              </div>
            )}
          </div>
        )}
        {renderFactorExplanation()}
        {renderPriceAlertSection()}
        {renderRiskDashboard()}
        {renderTradePlan()}
        {renderChartSection()}
        {renderBacktestSection()}
      </main>
    </div>
  );
}
