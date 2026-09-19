// WP5.1：标的研究壳层组件
//
// 设计说明：
// - 任务清单 #1：壳层组件，接收来源上下文参数，负责整体布局和子组件编排
// - 持有 InvestmentCenter.tsx 原有的全部状态与副作用
// - JSX 渲染委托给 9 个子组件（SymbolSearchHeader / SymbolRelationshipBar /
//   FactorExplanationPanel / SymbolAlertSummary / RiskReferencePanel /
//   TradePlanPanel / SymbolChartPanel / SingleSymbolBacktestPanel）
// - 保持现有功能不丢失：搜索/详情/加入观察/加入组合/下单/回测/告警/绘图
//
// 关键约束（来自 spec 13.4）：
// - 不重写已稳定的业务逻辑（因子计算/回测引擎/告警规则/风控规则/模拟撮合不动）
// - 只做组件职责拆分
// - 保持 props 接口最小化
// - 不删除 ic_favorites / ic_risk_settings（WP5.2 才进入只读回退期）
import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import { Alert, Button, message, Modal } from "antd";
import { ArrowLeftOutlined } from "@ant-design/icons";
import { useApp } from "../../context/AppContext";
import { t, template } from "../../i18n";
import {
  percent,
  score,
  money,
  clamp,
  roundPrice,
  aggregateWeeklyBars,
  computeSuggestedPrice,
  signalLabel,
} from "../../utils/format";
import {
  resolveSourceContext,
  clearSourceContext,
  popReturnState,
  tabForReturnTo,
} from "../../utils/sourceContext";
import type {
  BacktestRun,
  FuturePlanTuning,
  Symbol as SymbolInfo,
  TradeSetup,
  TradeSetupOverrides,
  WorkbenchBar,
} from "../../types";
import { api, requestJson, type SymbolFactorExplanation } from "../../api/client";
import {
  computeMA,
  computeMACD,
  detectMACDCross,
  computeRSI,
  detectRSIExtreme,
  computeBOLL,
  computeATR,
} from "../../utils/indicators";
import { SIGNAL_COLOR_MAP } from "../../constants/chartTheme";
import { getActiveFutureBuyPlan } from "../../utils/trade-plan";
import { formatVolume } from "../../utils/indicators";

import SymbolSearchHeader from "./SymbolSearchHeader";
import SymbolRelationshipBar from "./SymbolRelationshipBar";
import FactorExplanationPanel from "./FactorExplanationPanel";
import SymbolAlertSummary from "./SymbolAlertSummary";
import RiskReferencePanel from "./RiskReferencePanel";
import TradePlanPanel from "./TradePlanPanel";
import SymbolChartPanel from "./SymbolChartPanel";
import SingleSymbolBacktestPanel from "./SingleSymbolBacktestPanel";
// WP-AI.7：让 AI 解释按钮
import ExplainButton from "../ai/ExplainButton";
import type {
  AlertSettings,
  ChartDataExt,
  PriceAlert,
  RiskMetrics,
  RiskSettings,
  ScenarioPreview,
  SourceContext,
  SymbolQuickRef,
  TradePlanDraft,
  TrancheDraft,
  FuturePlanTunings,
  SymbolResearchShellProps,
} from "./types";
import {
  DEFAULT_ALERT_SETTINGS,
  DEFAULT_FUTURE_TUNINGS,
  DEFAULT_RISK_SETTINGS,
  normalizeTrancheDrafts,
  planDraftFromSetup,
  readStoredObject,
  trancheDraftFromSetup,
} from "./types";
import { trancheLabel } from "../../i18n";

// WP9.2：ic_favorites 已停用，仅保留只读回退。
// - 写入已在 WP5.2 全部迁移到后端观察池 API（POST /observations / archive）
// - 本地仅在网络失败时显示历史收藏标记，不再持久化任何变更
// - 恢复/导入工具见 opportunity/LocalFavoritesMigration.tsx（保留）
// 开发环境一次性 deprecation 提示，避免静默依赖已停用的本地存储。
let __icFavoritesDeprecationWarned = false;
function warnIcFavoritesDeprecated(): void {
  if (__icFavoritesDeprecationWarned) return;
  __icFavoritesDeprecationWarned = true;
  console.warn(
    "[WP9.2] localStorage.ic_favorites is deprecated and read-only; favorites are now persisted via the Watchlist/Observation API. " +
      "See opportunity/LocalFavoritesMigration.tsx for the one-time migration tool.",
  );
}

/**
 * 标的研究壳层：编排 9 个子组件，持有全部状态与副作用。
 */
export default function SymbolResearchShell({
  sourceContext: sourceContextProp,
  initialSymbolId,
  openMetricModal,
  children,
}: SymbolResearchShellProps) {
  const ctx = useApp();
  const workbench = ctx.workbench;
  const detail = ctx.detail;

  // WP5.3：解析来源上下文（prop > URL > sessionStorage > 默认 legacy）
  const resolvedSourceContext = useMemo(
    () => resolveSourceContext(sourceContextProp ?? null),
    [sourceContextProp],
  );

  // WP5.3：首次挂载时恢复返回状态（滚动位置等），仅消费一次
  const returnStateRestoredRef = useRef(false);
  useEffect(() => {
    if (returnStateRestoredRef.current) return;
    returnStateRestoredRef.current = true;
    const returnTo = resolvedSourceContext.return_to;
    if (!returnTo) return;
    const saved = popReturnState(returnTo);
    if (saved && typeof saved.scrollY === "number" && typeof window !== "undefined") {
      // 异步恢复滚动位置：等待子组件渲染完成
      requestAnimationFrame(() => {
        try {
          window.scrollTo({ top: Number(saved.scrollY), behavior: "auto" });
        } catch {
          /* ignore */
        }
      });
    }
  }, [resolvedSourceContext.return_to]);

  // WP5.3：若 SourceContext 携带 symbol_id 且当前无激活标的，则加载该标的
  const initialSourceSymbolId = resolvedSourceContext.symbol_id;
  const sourceSymbolLoadedRef = useRef(false);
  useEffect(() => {
    if (sourceSymbolLoadedRef.current) return;
    if (initialSourceSymbolId && !ctx.activeSymbolId && workbench) {
      sourceSymbolLoadedRef.current = true;
      ctx.loadSymbolDetail(initialSourceSymbolId, { focus: true, barLimit: 500 });
    }
  }, [initialSourceSymbolId, ctx.activeSymbolId, workbench, ctx.loadSymbolDetail]);

  // WP5.3：返回来源页面
  const handleReturnToSource = useCallback(() => {
    const returnTo = resolvedSourceContext.return_to;
    if (!returnTo) return;
    const targetTab = tabForReturnTo(returnTo);
    // 清理 SourceContext，避免下次直接进入研究时仍带旧来源
    clearSourceContext();
    if (ctx.activeTab !== targetTab) {
      ctx.setActiveTab(targetTab);
    }
  }, [resolvedSourceContext.return_to, ctx]);

  // ── 搜索状态 ──
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

  // 搜索历史（最近5个）+ 收藏
  const [searchHistory, setSearchHistory] = useState<SymbolQuickRef[]>(() => {
    try {
      return JSON.parse(localStorage.getItem("ic_search_history") || "[]");
    } catch {
      return [];
    }
  });
  // WP9.2：ic_favorites 已停止写入，仅保留读取兼容（只读回退期数据源）。
  // 网络失败时显示本地历史收藏标记；新增/取消收藏均走后端观察池 API，不写回本地。
  const [favorites, setFavorites] = useState<Set<number>>(() => {
    warnIcFavoritesDeprecated();
    try {
      return new Set(JSON.parse(localStorage.getItem("ic_favorites") || "[]"));
    } catch {
      return new Set();
    }
  });
  // 后端观察池拉取失败时进入只读回退模式（仅显示本地收藏，不允许新增/删除本地项）
  const [favoritesFallbackMode, setFavoritesFallbackMode] = useState(false);
  // observation_id 缓存：取消收藏时需要按 item_id 调用归档接口
  const [observationItemMap, setObservationItemMap] = useState<Record<number, number>>({});

  // 图表点击设置的入场价
  const [chartEntryPrice, setChartEntryPrice] = useState<number | null>(null);

  // 刷新交易计划 loading
  const [refreshingPlan, setRefreshingPlan] = useState(false);

  // 价格预警列表
  const [priceAlerts, setPriceAlerts] = useState<PriceAlert[]>([]);

  // 交易计划编辑态
  const [tradePlanEditing, setTradePlanEditing] = useState(false);
  const [tradePlanDraft, setTradePlanDraft] = useState<TradePlanDraft | null>(null);
  const [trancheEditing, setTrancheEditing] = useState(false);
  const [trancheDrafts, setTrancheDrafts] = useState<TrancheDraft[]>([]);

  // 设置面板开关
  const [riskSettingsOpen, setRiskSettingsOpen] = useState(false);
  const [alertSettingsOpen, setAlertSettingsOpen] = useState(false);

  // 设置持久化（ic_risk_settings / ic_alert_settings / ic_future_plan_tunings 不删除）
  const [riskSettings, setRiskSettings] = useState<RiskSettings>(() =>
    readStoredObject("ic_risk_settings", DEFAULT_RISK_SETTINGS),
  );
  const [alertSettings, setAlertSettings] = useState<AlertSettings>(() =>
    readStoredObject("ic_alert_settings", DEFAULT_ALERT_SETTINGS),
  );
  const [futurePlanTunings, setFuturePlanTunings] = useState<FuturePlanTunings>(() =>
    readStoredObject("ic_future_plan_tunings", DEFAULT_FUTURE_TUNINGS),
  );

  // 回测结果
  const [backtestResult, setBacktestResult] = useState<BacktestRun | null>(null);

  // 因子解释
  const [factorExplanation, setFactorExplanation] = useState<SymbolFactorExplanation | null>(null);
  const [factorExplanationLoading, setFactorExplanationLoading] = useState(false);
  const [factorExplanationError, setFactorExplanationError] = useState<string | null>(null);
  const factorExplanationRequestRef = useRef(0);

  // ── 副作用：移动端检测 ──
  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 1024);
    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, []);

  // ── 因子解释加载 ──
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
      const msg = String(err?.message || "");
      if (!msg.toLowerCase().includes("not found")) {
        setFactorExplanationError(msg);
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

    api
      .getBars(symbolId, Math.max(neededBars + 60, 500))
      .then((fetched: WorkbenchBar[]) => {
        if (!fetched || !fetched.length) return;
        const existingDates = new Set(detail.bars.map((b: WorkbenchBar) => b.trade_date));
        const newBars = fetched.filter((b: WorkbenchBar) => !existingDates.has(b.trade_date));
        if (newBars.length > 0) {
          setExtraBarsCache((prev) => ({
            ...prev,
            [symbolId]: [...newBars, ...(prev[symbolId] ?? [])],
          }));
        }
      })
      .catch(() => {})
      .finally(() => {
        loadingBarsRef.current.delete(symbolId);
      });
  }, [ctx.activeSymbolId, ctx.chartWindowSize, detail?.bars?.length, extraBarsCache]); // eslint-disable-line

  // ── 搜索逻辑（debounce 300ms） ──
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    if (!searchQuery.trim()) {
      setSearchResults([]);
      return;
    }
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
    return () => {
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    };
  }, [searchQuery]);

  // ── 快捷标的（持仓 + 最新评分） ──
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

  // ── 选择标的事件 ──
  const handleSelectSymbol = useCallback(
    (symbolId: number, symbolInfo?: Pick<SymbolInfo, "symbol" | "name">) => {
      ctx.loadSymbolDetail(symbolId, { focus: true, barLimit: 500 });
      setSearchQuery("");
      setSearchResults([]);
      if (symbolInfo) {
        setSearchHistory((prev) => {
          const filtered = prev.filter((h) => h.symbol_id !== symbolId);
          const updated: SymbolQuickRef[] = [
            { symbol_id: symbolId, symbol: symbolInfo.symbol, name: symbolInfo.name },
            ...filtered,
          ].slice(0, 5);
          try {
            localStorage.setItem("ic_search_history", JSON.stringify(updated));
          } catch {
            /* ignore */
          }
          return updated;
        });
      }
      setChartEntryPrice(null);
    },
    [ctx],
  );

  // ── WP5.2 / WP9.2：收藏迁移到后端观察池，ic_favorites 已停止写入 ──
  // 读：mount 时拉取后端观察池覆盖本地 favorites；失败则进入只读回退（仅显示本地收藏）
  // 写：加入调用 POST /observations（origin_type=manual, reason 含 research 标记）；
  //     取消调用 POST /observations/{item_id}/archive（需先按 symbol_id 解析 item_id）
  // 不再写 localStorage：ic_favorites 仅作只读回退期数据源（已停止写入，仅保留读取兼容）
  const refreshFavoritesFromBackend = useCallback(async () => {
    const watchlistId = ctx.activeWatchlistId;
    if (!watchlistId) return;
    try {
      const items = await requestJson<Array<{ watchlist_item_id: number; symbol_id: number; status?: string }>>(
        `/api/v1/watchlists/${watchlistId}/observations?limit=500&status=watching`,
      );
      const next = new Set<number>();
      const map: Record<number, number> = {};
      for (const item of items ?? []) {
        if (item.status && item.status !== "watching" && item.status !== "ready") continue;
        next.add(item.symbol_id);
        map[item.symbol_id] = item.watchlist_item_id;
      }
      setFavorites(next);
      setObservationItemMap(map);
      setFavoritesFallbackMode(false);
    } catch {
      // 后端拉取失败：保持本地 favorites 只读回退
      setFavoritesFallbackMode(true);
    }
  }, [ctx.activeWatchlistId]);

  // 切换收藏：所有写操作走后端观察池 API，不再写 localStorage
  const toggleFavorite = useCallback(
    async (symbolId: number) => {
      const isFav = favorites.has(symbolId);
      const watchlistId = ctx.activeWatchlistId;
      if (!watchlistId) {
        // 无活跃观察池：进入只读回退模式，仅提示
        setFavoritesFallbackMode(true);
        message.warning(t("symbolResearchFavoritesFallbackNotice"));
        return;
      }
      if (isFav) {
        // 取消收藏：调用归档接口（按 observation item_id）
        const itemId = observationItemMap[symbolId];
        if (!itemId) {
          // 本地回退数据没有 item_id，无法归档：提示并退出
          message.warning(t("symbolResearchFavoritesFallbackNotice"));
          return;
        }
        try {
          await requestJson(
            `/api/v1/watchlists/${watchlistId}/observations/${itemId}/archive`,
            { method: "POST" },
          );
          setFavorites((prev) => {
            const next = new Set(prev);
            next.delete(symbolId);
            return next;
          });
          setObservationItemMap((prev) => {
            const next = { ...prev };
            delete next[symbolId];
            return next;
          });
          message.success(t("icFavoriteRemovedFromObservation"));
        } catch {
          message.error(t("symbolResearchFavoritesFallbackNotice"));
        }
        return;
      }
      // 加入收藏：调用幂等 observations 接口（origin_type=manual, reason 标记 research 来源）
      try {
        const created = await requestJson<{ watchlist_item_id: number }>(
          `/api/v1/watchlists/${watchlistId}/observations`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              symbol_id: symbolId,
              origin_type: "manual",
              reason: { source: "research", text: "从标的研究加入" },
              note: t("icFavoriteObservationNote"),
            }),
          },
        );
        setFavorites((prev) => {
          const next = new Set(prev);
          next.add(symbolId);
          return next;
        });
        if (created?.watchlist_item_id) {
          setObservationItemMap((prev) => ({ ...prev, [symbolId]: created.watchlist_item_id }));
        }
        message.success(t("icFavoriteAddedToObservation"));
      } catch {
        // 后端失败：进入只读回退模式，不写本地
        setFavoritesFallbackMode(true);
        message.warning(t("symbolResearchFavoritesFallbackNotice"));
      }
    },
    [ctx.activeWatchlistId, favorites, observationItemMap],
  );

  // 模拟下单跳转：切换到组合 tab + 交易子 tab + 聚焦当前标的
  const handleJumpToPortfolioTrade = useCallback(() => {
    if (!ctx.portfolioId) {
      message.warning(t("symbolResearchJumpMissingPortfolio"));
      return;
    }
    const portfolioName =
      ctx.portfolios?.find((p) => p.id === ctx.portfolioId)?.name ?? `#${ctx.portfolioId}`;
    Modal.confirm({
      title: t("symbolResearchJumpConfirmTitle"),
      content: template("symbolResearchJumpConfirm", { name: portfolioName }),
      okText: t("ok"),
      cancelText: t("cancel"),
      onOk: () => {
        // 跳转目标：组合交易上下文（tab=portfolio, subtab=portfolio-trading, symbol_id 聚焦）
        const symbolId = ctx.activeSymbolId;
        try {
          const url = new URL(window.location.href);
          url.searchParams.set("tab", "portfolio");
          url.searchParams.set("subtab", "portfolio-trading");
          if (symbolId != null) url.searchParams.set("symbol_id", String(symbolId));
          url.searchParams.set("source", "research");
          window.history.pushState({}, "", url.toString());
        } catch {
          /* ignore URL failures */
        }
        ctx.setActiveTab("portfolio");
        ctx.setActiveSubTab("portfolio-trading");
        if (symbolId != null) ctx.setActiveSymbolId(symbolId);
      },
    });
  }, [ctx]);

  // 跳转到组合管理 PortfolioRule 配置（与模拟下单同一机制：切换 tab + URL 标记）
  const handleGoToPortfolioRule = useCallback(() => {
    if (!ctx.portfolioId) {
      message.warning(t("symbolResearchJumpMissingPortfolio"));
      return;
    }
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("tab", "portfolio");
      url.searchParams.set("subtab", "portfolio-workbench");
      url.searchParams.set("focus", "portfolio_rule");
      window.history.pushState({}, "", url.toString());
    } catch {
      /* ignore */
    }
    ctx.setActiveTab("portfolio");
    ctx.setActiveSubTab("portfolio-workbench");
  }, [ctx]);

  // ── 刷新交易计划 ──
  const handleRefreshPlan = useCallback(async () => {
    setRefreshingPlan(true);
    try {
      await ctx.generateTradeSetup();
    } catch {
      /* 错误提示由 AppContext.generateTradeSetup 统一处理 */
    } finally {
      setRefreshingPlan(false);
    }
  }, [ctx]);

  // ── 自动加载第一个快捷标的 ──
  useEffect(() => {
    if (quickSymbols.length > 0 && !ctx.activeSymbolId && !ctx.detail) {
      ctx.loadSymbolDetail(quickSymbols[0].symbol_id, { focus: true, barLimit: 500 });
    }
  }, [quickSymbols.length]); // eslint-disable-line

  // WP5.2：mount 时从后端观察池刷新收藏状态（失败则进入只读回退）
  useEffect(() => {
    refreshFavoritesFromBackend();
  }, [refreshFavoritesFromBackend]);

  // ── 数据计算层 ──
  const setup = detail?.latest_trade_setup ?? null;

  // 同步交易计划草稿
  useEffect(() => {
    if (setup && !tradePlanEditing) setTradePlanDraft(planDraftFromSetup(setup));
  }, [
    setup?.id,
    setup?.entry_min,
    setup?.entry_max,
    setup?.stop_loss,
    setup?.target_price,
    setup?.recommended_position_pct,
    setup?.recommended_position_amount,
    tradePlanEditing,
  ]); // eslint-disable-line

  // 同步分批计划草稿
  useEffect(() => {
    if (setup && !trancheEditing) {
      // 调用本地版本（trancheLabel 由 i18n 模块提供，在闭包内应用）
      const drafts = (setup.tranche_plan ?? []).map((item) => ({
        label: item.label || trancheLabel("Custom"),
        position_pct: Math.round(Number(item.position_pct || 0) * 10000) / 100,
        amount: Number(item.amount || 0),
        trigger: item.trigger || "",
      }));
      setTrancheDrafts(drafts);
    }
  }, [setup?.id, setup?.manual_tranche_plan_json, setup?.tranche_plan, trancheEditing]); // eslint-disable-line

  // 持久化设置
  useEffect(() => {
    try {
      localStorage.setItem("ic_risk_settings", JSON.stringify(riskSettings));
    } catch {
      /* ignore */
    }
  }, [riskSettings]);

  useEffect(() => {
    try {
      localStorage.setItem("ic_alert_settings", JSON.stringify(alertSettings));
    } catch {
      /* ignore */
    }
  }, [alertSettings]);

  // ── 交易计划草稿编辑回调 ──
  const updateTradePlanDraft = useCallback(
    (field: keyof TradePlanDraft, value: number | null) => {
      setTradePlanDraft((prev) => ({ ...(prev ?? ({} as TradePlanDraft)), [field]: value }));
    },
    [],
  );

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
    if (
      tradePlanDraft.entry_min != null &&
      tradePlanDraft.entry_max != null &&
      tradePlanDraft.entry_min > tradePlanDraft.entry_max
    ) {
      ctx.showToast("error", t("icInvalidPlan"));
      return;
    }
    const overrides: TradeSetupOverrides = {
      entry_min: tradePlanDraft.entry_min,
      entry_max: tradePlanDraft.entry_max,
      stop_loss: tradePlanDraft.stop_loss,
      target_price: tradePlanDraft.target_price,
      recommended_position_pct:
        tradePlanDraft.recommended_position_pct != null
          ? tradePlanDraft.recommended_position_pct / 100
          : null,
      recommended_position_amount: tradePlanDraft.recommended_position_amount,
    };
    setRefreshingPlan(true);
    try {
      await ctx.generateTradeSetup(overrides);
      setTradePlanEditing(false);
    } finally {
      setRefreshingPlan(false);
    }
  }, [ctx, tradePlanDraft]);

  // ── 分批计划编辑回调 ──
  const updateTrancheDraft = useCallback(
    (index: number, field: keyof TrancheDraft, value: string | number) => {
      setTrancheDrafts((prev) =>
        prev.map((item, i) => (i === index ? { ...item, [field]: value } : item)),
      );
    },
    [],
  );

  const handleEditTranches = useCallback(() => {
    if (!setup) return;
    const drafts = (setup.tranche_plan ?? []).map((item) => ({
      label: item.label || trancheLabel("Custom"),
      position_pct: Math.round(Number(item.position_pct || 0) * 10000) / 100,
      amount: Number(item.amount || 0),
      trigger: item.trigger || "",
    }));
    setTrancheDrafts(drafts);
    setTrancheEditing(true);
  }, [setup]);

  const handleCancelTranches = useCallback(() => {
    setTrancheEditing(false);
  }, []);

  const handleSaveTranches = useCallback(async () => {
    if (!setup || !ctx.activeSymbolId) return;
    const payload = normalizeTrancheDrafts(trancheDrafts);
    const totalPct = payload.reduce((sum, item) => sum + item.position_pct, 0);
    if (totalPct > (setup.recommended_position_pct ?? 0) + 0.0001) {
      ctx.showToast("error", `${t("icTranches")} > ${percent(setup.recommended_position_pct)}`);
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
  }, [ctx, setup, trancheDrafts]);

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

  const handleAddTranche = useCallback(() => {
    setTrancheDrafts((prev) => [
      ...prev,
      { label: trancheLabel("Custom"), position_pct: 0, amount: 0, trigger: "" },
    ]);
  }, []);

  const handleRemoveTranche = useCallback((index: number) => {
    setTrancheDrafts((prev) => prev.filter((_, idx) => idx !== index));
  }, []);

  // ── 未来计划调参 ──
  const updateFutureTuning = useCallback(
    (field: keyof FuturePlanTuning, value: number | null) => {
      const scenario = ctx.futurePlanScenario;
      setFuturePlanTunings((prev) => ({
        ...prev,
        [scenario]: {
          ...(DEFAULT_FUTURE_TUNINGS[scenario] ?? {}),
          ...(prev[scenario] ?? {}),
          [field]: value ?? undefined,
        },
      }));
    },
    [ctx.futurePlanScenario],
  );

  // ── 入场价 & 数量 ──
  const baseScenarios = setup?.return_scenarios;
  const _refPrice = baseScenarios?.reference_price ?? computeSuggestedPrice(detail);
  const effectiveEntryPrice =
    chartEntryPrice ?? (Number(ctx.simPrice) > 0 ? Number(ctx.simPrice) : Number(_refPrice));
  const entryPrice = effectiveEntryPrice;
  const quantity =
    Number(ctx.simQuantity) > 0
      ? Number(ctx.simQuantity)
      : Number(baseScenarios?.planned_order?.quantity ?? 0);

  // ── 场景预演 ──
  const scenarios = useMemo<ScenarioPreview | null>(() => {
    if (!baseScenarios) return null;
    const refPrice = baseScenarios.reference_price ?? entryPrice;
    if (!(refPrice > 0)) return baseScenarios;

    const baseRiskUnit = Math.max(refPrice * 0.015, 0.01);
    const baseStop = setup?.stop_loss;
    const riskFromStop = baseStop != null && baseStop < refPrice ? refPrice - baseStop : baseRiskUnit;
    const effectiveRisk = Math.max(baseRiskUnit, riskFromStop);

    const scenarioConfig: Record<
      string,
      { horizonMult: number; targetMult: number; stopMult: number; confidenceAdj: number }
    > = {
      general: { horizonMult: 1.0, targetMult: 1.0, stopMult: 1.0, confidenceAdj: 0 },
      short: { horizonMult: 0.25, targetMult: 0.5, stopMult: 0.65, confidenceAdj: -8 },
      mid: { horizonMult: 0.75, targetMult: 0.82, stopMult: 0.88, confidenceAdj: -3 },
      long: { horizonMult: 1.5, targetMult: 1.3, stopMult: 1.2, confidenceAdj: 5 },
      custom: { horizonMult: 1.0, targetMult: 1.0, stopMult: 1.0, confidenceAdj: 0 },
    };
    const cfg = scenarioConfig[ctx.futurePlanScenario] ?? scenarioConfig.general;

    const adjHorizon = Math.max(3, Math.round((baseScenarios.horizon_days ?? 20) * cfg.horizonMult));
    const adjRisk = effectiveRisk * cfg.targetMult;
    const adjStopOffset = effectiveRisk * cfg.stopMult;
    const adjStop = roundPrice(refPrice - adjStopOffset);
    const adjTarget = roundPrice(refPrice + adjRisk);
    const adjConfidence =
      clamp((baseScenarios.confidence_pct ?? 60) + cfg.confidenceAdj, 35, 82) / 100;
    const pessimisticPrice = roundPrice(adjStop);
    const optimisticAnchor = adjTarget + Math.max(effectiveRisk * 0.4, refPrice * 0.02);
    const optimisticPrice = roundPrice(optimisticAnchor);
    const expectedPrice = roundPrice(
      adjTarget * adjConfidence + pessimisticPrice * (1 - adjConfidence),
    );

    return {
      ...baseScenarios,
      horizon_days: adjHorizon,
      confidence_pct: Math.round(adjConfidence * 100),
      expected: { ...baseScenarios.expected, exit_price: expectedPrice },
      optimistic: { ...baseScenarios.optimistic, exit_price: optimisticPrice },
      pessimistic: { ...baseScenarios.pessimistic, exit_price: pessimisticPrice },
      _adjStop: adjStop,
      _adjTarget: adjTarget,
    } as ScenarioPreview;
  }, [baseScenarios, setup?.stop_loss, entryPrice, ctx.futurePlanScenario]); // eslint-disable-line

  // ── Chart data memo（含全部技术指标） ──
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
      itemStyle: {
        color: b.close >= b.open ? "rgba(15, 118, 110, 0.5)" : "rgba(180, 35, 24, 0.5)",
      },
    }));

    const ma10 = computeMA(closes, 10);
    const ma20 = computeMA(closes, 20);
    const macd = computeMACD(closes);
    const rsi = computeRSI(closes);
    const boll = computeBOLL(closes);
    const atr = computeATR(bars.map((b) => ({ high: b.high, low: b.low, close: b.close })));
    const macdSignals = detectMACDCross(macd.dif, macd.dea);
    const rsiSignals = detectRSIExtreme(rsi);

    return {
      bars,
      dates,
      closes,
      candlestick,
      volume,
      ma10,
      ma20,
      macd,
      rsi,
      boll,
      atr,
      macdSignals,
      rsiSignals,
    };
  }, [detail?.bars, extraBarsCache, ctx.activeSymbolId, ctx.chartTimeframe, ctx.chartWindowSize]); // eslint-disable-line

  // ── 价格预警检测 ──
  useEffect(() => {
    if (!chartData || !setup || !detail?.bars?.length) return;
    const lastClose = detail.bars[detail.bars.length - 1].close;
    const lastDate = detail.bars[detail.bars.length - 1].trade_date;
    const alerts: PriceAlert[] = [];
    const stopLoss = scenarios?._adjStop ?? setup.stop_loss;
    const targetPrice = scenarios?._adjTarget ?? setup.target_price;

    if (alertSettings.enableStopLoss && stopLoss != null && lastClose <= stopLoss) {
      alerts.push({
        id: `stop_${lastDate}`,
        type: "stop_loss",
        level: "danger",
        message: template("alertStopLoss", { price: score(lastClose), stop: score(stopLoss) }),
        detail: template("icPriceBelowStop", { price: score(lastClose), stop: score(stopLoss) }),
        timestamp: Date.now(),
      });
    } else if (
      alertSettings.enableStopLoss &&
      stopLoss != null &&
      lastClose > 0 &&
      (lastClose - stopLoss) / lastClose < alertSettings.stopNearPct / 100
    ) {
      alerts.push({
        id: `stop_near_${lastDate}`,
        type: "stop_near",
        level: "warning",
        message: template("alertNearStopLoss", { price: score(lastClose), stop: score(stopLoss) }),
        detail: template("icNearStopLoss", { pct: percent((lastClose - stopLoss) / lastClose) }),
        timestamp: Date.now(),
      });
    }

    if (alertSettings.enableTarget && targetPrice != null && lastClose >= targetPrice) {
      alerts.push({
        id: `target_${lastDate}`,
        type: "target_hit",
        level: "info",
        message: template("alertTargetHit", { price: score(lastClose), target: score(targetPrice) }),
        detail: template("icPriceAboveTarget", {
          price: score(lastClose),
          target: score(targetPrice),
        }),
        timestamp: Date.now(),
      });
    } else if (
      alertSettings.enableTarget &&
      targetPrice != null &&
      lastClose > 0 &&
      (targetPrice - lastClose) / targetPrice < alertSettings.targetNearPct / 100
    ) {
      alerts.push({
        id: `target_near_${lastDate}`,
        type: "target_near",
        level: "info",
        message: template("alertNearTarget", { price: score(lastClose), target: score(targetPrice) }),
        detail: template("icNearTarget", { pct: percent((targetPrice - lastClose) / targetPrice) }),
        timestamp: Date.now(),
      });
    }

    const lastRSI = chartData.rsi ? chartData.rsi[chartData.rsi.length - 1] : null;
    if (alertSettings.enableRsi && lastRSI != null && lastRSI > alertSettings.rsiOverbought) {
      alerts.push({
        id: `rsi_ob_${lastDate}`,
        type: "rsi_overbought",
        level: "warning",
        message: template("alertRSIOverbought", { rsi: lastRSI.toFixed(1) }),
        detail: template("icRSIOverboughtWarn", {
          period: lastRSI.toFixed(1),
          threshold: alertSettings.rsiOverbought,
        }),
        timestamp: Date.now(),
      });
    } else if (alertSettings.enableRsi && lastRSI != null && lastRSI < alertSettings.rsiOversold) {
      alerts.push({
        id: `rsi_os_${lastDate}`,
        type: "rsi_oversold",
        level: "info",
        message: template("alertRSIOversold", { rsi: lastRSI.toFixed(1) }),
        detail: template("icRSIOversoldOpportunity", {
          period: lastRSI.toFixed(1),
          threshold: alertSettings.rsiOversold,
        }),
        timestamp: Date.now(),
      });
    }

    if (alertSettings.enableMacd && chartData.macdSignals.length > 0) {
      const latestSignal = chartData.macdSignals[chartData.macdSignals.length - 1];
      const signalAge = chartData.dates.length - 1 - latestSignal.index;
      if (signalAge <= 2) {
        alerts.push({
          id: `macd_${latestSignal.type}_${lastDate}`,
          type: latestSignal.type === "golden" ? "macd_golden" : "macd_death",
          level: latestSignal.type === "golden" ? "info" : "warning",
          message: latestSignal.type === "golden" ? t("alertMACDGolden") : t("alertMACDDeath"),
          detail: template("icMACDCrossSignal", {
            tense:
              latestSignal.index === chartData.dates.length - 1 ? t("icLatest") : t("icRecent"),
            type:
              latestSignal.type === "golden" ? t("icGoldenCross") : t("icDeathCross"),
          }),
          timestamp: Date.now(),
        });
      }
    }

    setPriceAlerts(alerts);
  }, [chartData, scenarios, setup, detail?.bars, alertSettings]); // eslint-disable-line

  // ── 激活的未来买入计划 ──
  const activeFutureBuyPlan = useMemo(() => {
    const s = detail?.latest_trade_setup ?? null;
    if (!s) return [];
    const totalCapital = ctx.workbench?.portfolio?.total_capital ?? 0;
    const investableRatio = ctx.workbench?.portfolio?.investable_ratio ?? 1;
    return getActiveFutureBuyPlan(
      s,
      ctx.futurePlanScenario,
      ctx.futurePlanCustom,
      totalCapital,
      investableRatio,
      futurePlanTunings[ctx.futurePlanScenario],
    );
  }, [
    detail?.latest_trade_setup,
    ctx.futurePlanScenario,
    ctx.futurePlanCustom,
    ctx.workbench?.portfolio,
    futurePlanTunings,
  ]); // eslint-disable-line

  // ── 风险指标计算 ──
  const riskMetrics = useMemo<RiskMetrics | null>(() => {
    if (!setup || !(entryPrice > 0)) return null;
    const stop = scenarios?._adjStop ?? setup.stop_loss;
    const target = scenarios?._adjTarget ?? setup.target_price;
    const currentPrice = detail?.bars?.[detail.bars.length - 1]?.close ?? entryPrice;

    const reward = target != null ? target - entryPrice : 0;
    const riskAmount = stop != null ? entryPrice - stop : 0;
    const rrRatio = riskAmount > 0 ? reward / riskAmount : 0;
    const stopDistancePct =
      stop != null && entryPrice > 0 ? ((entryPrice - stop) / entryPrice) * 100 : 0;
    const currentStopDist =
      stop != null && currentPrice > 0 ? ((currentPrice - stop) / currentPrice) * 100 : 0;
    const lastATR = chartData?.atr
      ? chartData.atr.filter((v): v is number => v != null).pop() ?? null
      : null;
    const atrStopRef = lastATR != null ? lastATR * riskSettings.atrMultiplier : null;
    const positionPct = setup.recommended_position_pct ?? 0;
    const concentrationLevel =
      positionPct >= riskSettings.concentrationHighPct
        ? "high"
        : positionPct >= riskSettings.concentrationMediumPct
          ? "medium"
          : "low";
    const maxLossPerShare = stop != null ? entryPrice - stop : 0;
    const maxLossAmount = maxLossPerShare * quantity;

    return {
      rrRatio,
      stopDistancePct,
      currentStopDist,
      atrStopRef,
      concentrationLevel,
      maxLossAmount,
      maxLossPerShare,
      reward,
      riskAmount,
      currentPrice,
      stop: stop ?? null,
      target: target ?? null,
      maxLossLimitAmount:
        (ctx.workbench?.portfolio?.total_capital ?? 0) * (riskSettings.maxLossPct / 100),
    };
  }, [
    setup,
    scenarios,
    entryPrice,
    quantity,
    chartData?.atr,
    detail?.bars,
    riskSettings,
    ctx.workbench?.portfolio?.total_capital,
  ]); // eslint-disable-line

  // ── 主图 option ──
  const chartOption = useMemo(() => {
    if (!chartData) return null;
    const signals = detail?.latest_trade_setup?.chart_signals ?? [];

    const markLineData = signals.map((sig) => {
      const colorMap = SIGNAL_COLOR_MAP;
      const lineColor = colorMap[sig.kind] ?? "#059669";
      return {
        yAxis: sig.price,
        label: { formatter: signalLabel(sig), position: "end" as const, color: lineColor },
        lineStyle: { color: lineColor, type: "dashed" as const, width: 1 },
      };
    });

    const allPrices = chartData.candlestick.flatMap((c) => [c[1], c[2], c[3], c[4]]);
    const maValues = [...chartData.ma10, ...chartData.ma20].filter((v): v is number => v !== null);
    const keyPrices: number[] = [];
    if (setup?.target_price != null) keyPrices.push(setup.target_price);
    if (setup?.stop_loss != null) keyPrices.push(setup.stop_loss);
    const futureZones = activeFutureBuyPlan.flatMap((p) =>
      p.zone_min !== null && p.zone_max !== null ? [p.zone_min, p.zone_max] : [],
    );
    const signalPrices = signals.map((s) => s.price).filter((v): v is number => v != null);

    const allRelevant = [...allPrices, ...maValues, ...keyPrices, ...signalPrices].filter(
      (v): v is number => v != null && !isNaN(v) && isFinite(v),
    );
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
          const dif = chartData.macd?.dif[p.dataIndex];
          const dea = chartData.macd?.dea[p.dataIndex];
          const hist = chartData.macd?.macdHist[p.dataIndex];
          if (dif != null) html += `<div style="color:#c084fc">DIF: ${score(dif)}</div>`;
          if (dea != null) html += `<div style="color:#f472b6">DEA: ${score(dea)}</div>`;
          if (hist != null)
            html += `<div style="color:${hist >= 0 ? "#0f766e" : "#b42318"}">MACD: ${score(hist)}</div>`;
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
        {
          type: "value",
          scale: true,
          splitLine: { show: false },
          axisLabel: { fontSize: 10, formatter: (v: any) => formatVolume(Number(v)) },
        },
      ],
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
        { name: t("volume"), type: "bar", yAxisIndex: 1, data: chartData.volume },
      ],
      // futureZones 不直接渲染（由 FuturePlanOverlay SVG 处理），仅参与 Y 轴范围计算
      _futureZones: futureZones,
    };
  }, [chartData, detail?.latest_trade_setup, activeFutureBuyPlan, setup, ctx.locale]); // eslint-disable-line

  // ── MACD 副图 option ──
  const macdOption = useMemo(() => {
    if (!chartData?.macd || !showMACD) return null;
    const { dif, dea, macdHist } = chartData.macd;
    return {
      animation: false,
      grid: { left: 48, right: 12, top: 8, bottom: 24 },
      xAxis: { type: "category", data: chartData.dates, axisLabel: { show: false } },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { type: "dashed", opacity: 0.3 } },
        axisLabel: { fontSize: 9 },
      },
      tooltip: { trigger: "axis" as const, confine: true },
      legend: {
        data: ["DIF", "DEA", "MACD"],
        top: 0,
        textStyle: { fontSize: 10 },
        itemWidth: 14,
        itemHeight: 8,
      },
      series: [
        {
          name: "DIF",
          type: "line",
          data: dif,
          lineStyle: { width: 1.2, color: "#c084fc" },
          symbol: "none",
          showSymbol: false,
        },
        {
          name: "DEA",
          type: "line",
          data: dea,
          lineStyle: { width: 1.2, color: "#f472b6" },
          symbol: "none",
          showSymbol: false,
        },
        {
          name: "MACD",
          type: "bar",
          data: macdHist.map((v) =>
            v == null
              ? null
              : { value: v, itemStyle: { color: v >= 0 ? "rgba(15,118,110,0.7)" : "rgba(180,35,24,0.7)" } },
          ),
          barMaxWidth: 6,
        },
      ],
    };
  }, [chartData?.macd, showMACD, chartData?.dates, ctx.locale]); // eslint-disable-line

  // ── RSI 副图 option ──
  const rsiOption = useMemo(() => {
    if (!chartData?.rsi || !showRSI) return null;
    return {
      animation: false,
      grid: { left: 48, right: 12, top: 8, bottom: 24 },
      xAxis: { type: "category", data: chartData.dates, axisLabel: { show: false } },
      yAxis: {
        type: "value",
        min: 0,
        max: 100,
        splitLine: { lineStyle: { type: "dashed", opacity: 0.3 } },
        axisLabel: { fontSize: 9 },
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { type: "solid", width: 1, opacity: 0.4 },
          data: [
            {
              yAxis: 70,
              lineStyle: { color: "#b42318" },
              label: { formatter: t("rsiOBLineLabel"), fontSize: 9 },
            },
            {
              yAxis: 30,
              lineStyle: { color: "#0f766e" },
              label: { formatter: t("rsiOSLineLabel"), fontSize: 9 },
            },
            { yAxis: 50, lineStyle: { color: "#6b7280", type: "dashed" } },
          ],
        },
      },
      tooltip: { trigger: "axis" as const, confine: true },
      series: [
        {
          name: "RSI",
          type: "line",
          data: chartData.rsi,
          lineStyle: { width: 1.2, color: "#8b5cf6" },
          symbol: "none",
          showSymbol: false,
          areaStyle: { color: "rgba(139,92,246,0.08)" },
          markPoint: {
            data: chartData.rsiSignals.map((s) => ({
              coord: [s.index, s.value],
              value: s.type === "overbought" ? t("icRsiOverbought") : t("icRsiOversold"),
              symbol: "triangle",
              symbolSize: 8,
              symbolRotate: s.type === "overbought" ? 180 : 0,
              itemStyle: { color: s.type === "overbought" ? "#b42318" : "#0f766e" },
              label: { fontSize: 9, color: s.type === "overbought" ? "#b42318" : "#0f766e" },
            })),
          } as any, // TODO: 待后续类型强化——ECharts markPoint 配置类型暂保留 as any
        },
      ],
    };
  }, [chartData?.rsi, showRSI, chartData?.dates, chartData?.rsiSignals]); // eslint-disable-line

  const lastBar = detail?.bars?.[detail.bars.length - 1];

  // ── 图表点击事件 ──
  const handleChartClick = useCallback(
    (params: unknown) => {
      const p = params as { dataIndex?: number };
      if (p.dataIndex != null && chartData?.closes[p.dataIndex] != null) {
        const clickedClose = chartData.closes[p.dataIndex];
        setChartEntryPrice(clickedClose);
        ctx.setSimPrice(String(clickedClose));
      }
    },
    [chartData, ctx],
  );

  // ── 重置图表 ──
  const handleResetChart = useCallback(() => {
    ctx.setChartWindowSize(60);
    ctx.setChartRange(null);
  }, [ctx]);

  // ── 空状态 ──
  if (!workbench) {
    return (
      <div className="investment-center ic__empty symbol-research-shell">
        <div className="empty">{t("noScanYet")}</div>
      </div>
    );
  }

  // 渲染迁移提示横幅（WP1.4：旧入口暂留；WP5.2：收藏迁移到后端观察池）
  const migrationBanner = (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 8 }}>
      <Alert
        type="warning"
        showIcon
        banner
        message={t("icMigrationNotice")}
        description={t("icMigrationDesc")}
      />
      <Alert
        type={favoritesFallbackMode ? "warning" : "info"}
        showIcon
        banner
        message={
          favoritesFallbackMode
            ? t("symbolResearchFavoritesFallbackNotice")
            : t("symbolResearchFavoritesMigratedNotice")
        }
        data-testid="favorites-migration-banner"
        data-fallback-mode={String(favoritesFallbackMode)}
      />
    </div>
  );

  // WP5.3：来源上下文面包屑 + 返回按钮
  // 仅当 source_type 非 legacy/undefined 时显示（避免对旧入口造成视觉干扰）
  const renderSourceBreadcrumb = () => {
    const sourceType = resolvedSourceContext.source_type;
    if (!sourceType || sourceType === "legacy") return null;

    const sourceLabelKey = `symbolResearchSourceBreadcrumb.${sourceType}`;
    const sourceLabel = t(sourceLabelKey);
    // 若 i18n 缺失，回退到默认来源类型标签
    const fallbackLabel = t(`symbolResearchSource${sourceType.charAt(0).toUpperCase()}${sourceType.slice(1)}`);
    const label = sourceLabel !== sourceLabelKey ? sourceLabel : fallbackLabel;

    const sourceId = resolvedSourceContext.source_id;
    const portfolioId = resolvedSourceContext.portfolio_id;
    const returnTo = resolvedSourceContext.return_to;

    const returnLabelKey = `symbolResearchReturnTo.${returnTo}`;
    const returnLabel = t(returnLabelKey);
    const fallbackReturnLabel = t(`symbolResearchReturnToDefault`);

    const detailParts: string[] = [];
    if (sourceId != null) {
      detailParts.push(`#${sourceId}`);
    }
    if (portfolioId != null) {
      const portfolioName =
        ctx.portfolios?.find((p) => p.id === portfolioId)?.name ?? `portfolio#${portfolioId}`;
      detailParts.push(portfolioName);
    }

    const detailText = detailParts.length > 0 ? detailParts.join(" · ") : null;

    return (
      <div
        className="symbol-research-source-breadcrumb"
        data-source-type={sourceType}
        data-source-id={sourceId ?? ""}
        data-portfolio-id={portfolioId ?? ""}
        data-return-to={returnTo ?? ""}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "6px 12px",
          background: "var(--bg-elevated, #fafafa)",
          border: "1px solid var(--border-color, #f0f0f0)",
          borderRadius: 4,
          marginBottom: 8,
          fontSize: 13,
          flexWrap: "wrap",
        }}
      >
        <span style={{ color: "var(--muted, #6b7280)" }}>
          {t("symbolResearchSourceFromLabel")}:
        </span>
        <strong style={{ color: "var(--text-primary, #111827)" }}>{label}</strong>
        {detailText && (
          <span style={{ color: "var(--muted, #6b7280)" }}>{detailText}</span>
        )}
        {returnTo && returnTo !== "research" && returnLabel !== returnLabelKey && (
          <Button
            size="small"
            type="link"
            icon={<ArrowLeftOutlined />}
            onClick={handleReturnToSource}
            data-testid="symbol-research-return-button"
            style={{ marginLeft: "auto", padding: 0 }}
          >
            {returnLabel !== returnLabelKey ? returnLabel : fallbackReturnLabel}
          </Button>
        )}
      </div>
    );
  };

  // 共用主体内容（移动端与桌面端共用）
  const mainContent = (
    <>
      <FactorExplanationPanel
        symbolId={ctx.activeSymbolId}
        factorExplanation={factorExplanation}
        loading={factorExplanationLoading}
        error={factorExplanationError}
        onRefresh={loadFactorExplanation}
      />
      {/* SymbolRelationshipBar：复用 OpportunityStatusBadges 的关联状态展示 */}
      {ctx.activeSymbolId && (
        <div className="ic__relationship-bar-wrapper symbol-research-relationship-wrapper">
          <SymbolRelationshipBar symbolId={ctx.activeSymbolId} />
        </div>
      )}
      <SymbolAlertSummary
        alerts={priceAlerts}
        alertSettings={alertSettings}
        settingsOpen={alertSettingsOpen}
        symbolId={ctx.activeSymbolId}
        onToggleSettings={() => setAlertSettingsOpen((v) => !v)}
        onUpdateSettings={(patch) => setAlertSettings((prev) => ({ ...prev, ...patch }))}
      />
      <RiskReferencePanel
        riskMetrics={riskMetrics}
        riskSettings={riskSettings}
        settingsOpen={riskSettingsOpen}
        entryPrice={entryPrice}
        quantity={quantity}
        setup={setup}
        portfolioId={ctx.portfolioId}
        onToggleSettings={() => setRiskSettingsOpen((v) => !v)}
        onUpdateSettings={(patch) => setRiskSettings((prev) => ({ ...prev, ...patch }))}
        onGoToPortfolioRule={handleGoToPortfolioRule}
      />
      <TradePlanPanel
        setup={setup}
        scenarios={scenarios}
        entryPrice={entryPrice}
        quantity={quantity}
        chartEntryPrice={chartEntryPrice}
        tradePlanEditing={tradePlanEditing}
        tradePlanDraft={tradePlanDraft}
        trancheEditing={trancheEditing}
        trancheDrafts={trancheDrafts}
        refreshingPlan={refreshingPlan}
        futurePlanScenario={ctx.futurePlanScenario}
        futurePlanTunings={futurePlanTunings}
        activeFutureBuyPlan={activeFutureBuyPlan}
        onEditPlan={handleEditPlan}
        onCancelPlanEdit={handleCancelPlanEdit}
        onApplyPlanOverrides={handleApplyPlanOverrides}
        onRefreshPlan={handleRefreshPlan}
        onUpdateTradePlanDraft={updateTradePlanDraft}
        onEditTranches={handleEditTranches}
        onCancelTranches={handleCancelTranches}
        onSaveTranches={handleSaveTranches}
        onResetTranches={handleResetTranches}
        onUpdateTrancheDraft={updateTrancheDraft}
        onAddTranche={handleAddTranche}
        onRemoveTranche={handleRemoveTranche}
        onUpdateFutureTuning={updateFutureTuning}
        onSetFuturePlanScenario={ctx.setFuturePlanScenario}
        onJumpToPortfolioTrade={handleJumpToPortfolioTrade}
      />
      <SymbolChartPanel
        chartData={chartData}
        chartOption={chartOption}
        macdOption={macdOption}
        rsiOption={rsiOption}
        showMACD={showMACD}
        showRSI={showRSI}
        chartExpanded={ctx.chartExpanded}
        chartTimeframe={ctx.chartTimeframe}
        chartWindowSize={ctx.chartWindowSize}
        lastBar={lastBar}
        setup={setup}
        activeFutureBuyPlan={activeFutureBuyPlan}
        onToggleMACD={() => setShowMACD((v) => !v)}
        onToggleRSI={() => setShowRSI((v) => !v)}
        onSetChartTimeframe={ctx.setChartTimeframe}
        onSetChartWindowSize={ctx.setChartWindowSize}
        onResetChart={handleResetChart}
        onChartClick={handleChartClick}
      />
      <SingleSymbolBacktestPanel
        portfolioId={ctx.portfolioId}
        activeSymbolId={ctx.activeSymbolId}
        sourceContext={resolvedSourceContext}
        backtestResult={backtestResult}
        onSetBacktestResult={setBacktestResult}
        onAppliedToPortfolio={() => ctx.loadWorkbench()}
      />
    </>
  );

  // 标的头部（桌面端独有，含收藏切换）
  const renderSymbolHeader = () => {
    if (!detail) return null;
    return (
      <div className="ic__symbol-header">
        <div className="symbol-title">
          <span className="symbol-code">{detail.symbol.symbol}</span>
          <span className="symbol-name">{detail.symbol.name}</span>
          <span
            className={`ic__fav-star ic__fav-star--lg${favorites.has(detail.symbol.id) ? " active" : ""}`}
            onClick={() => toggleFavorite(detail.symbol.id)}
          >
            {favorites.has(detail.symbol.id) ? "★" : "☆"}
          </span>
          <span className="symbol-meta">
            {`${t("marketLabel")}: ${detail.symbol.market}${" | "}${t("regionLabel")}: ${regionLongLabelLocal(detail.symbol.region)}${" | "}${t("assetLabel")}: ${assetTypeLabelLocal(detail.symbol.asset_type)}`}
          </span>
          {/* WP-AI.7：让 AI 解释（携带 symbol_id） */}
          <ExplainButton
            sourcePage="symbol_research"
            references={{ symbol_id: detail.symbol.id }}
          />
          {detail.position && (
            <>
              <span className="badge" style={{ marginLeft: 8 }}>
                {t("holdingQty")}: {detail.position.quantity}
              </span>
              <span className="badge">
                {t("avgCost")}: {score(detail.position.avg_cost)}
              </span>
            </>
          )}
        </div>
        {/* 图表入场价提示 */}
        {chartEntryPrice && (
          <div className="ic__entry-price-hint">
            <span>
              {t("chartEntry")}: <strong>{score(chartEntryPrice)}</strong>
            </span>
            <button
              type="button"
              onClick={() => {
                setChartEntryPrice(null);
                ctx.setSimPrice("");
              }}
              className="ic__hint-close"
            >
              x
            </button>
          </div>
        )}
      </div>
    );
  };

  // 移动端布局
  if (isMobile) {
    return (
      <div className="investment-center ic__mobile-layout symbol-research-shell symbol-research-shell--mobile">
        {renderSourceBreadcrumb()}
        {migrationBanner}
        <SymbolSearchHeader
          isMobile={isMobile}
          searchQuery={searchQuery}
          searchResults={searchResults}
          searching={searching}
          searchHistory={searchHistory}
          favorites={favorites}
          quickSymbols={quickSymbols}
          activeSymbolId={ctx.activeSymbolId}
          onSearchQueryChange={setSearchQuery}
          onSelectSymbol={handleSelectSymbol}
          onToggleFavorite={toggleFavorite}
        />
        {mainContent}
        {children}
      </div>
    );
  }

  // 桌面端布局
  return (
    <div className="investment-center ic__desktop-layout ic__layout--fullwidth symbol-research-shell symbol-research-shell--desktop">
      {renderSourceBreadcrumb()}
      {migrationBanner}
      <SymbolSearchHeader
        isMobile={isMobile}
        searchQuery={searchQuery}
        searchResults={searchResults}
        searching={searching}
        searchHistory={searchHistory}
        favorites={favorites}
        quickSymbols={quickSymbols}
        activeSymbolId={ctx.activeSymbolId}
        onSearchQueryChange={setSearchQuery}
        onSelectSymbol={handleSelectSymbol}
        onToggleFavorite={toggleFavorite}
      />
      <main className="ic__main-content">
        {renderSymbolHeader()}
        {mainContent}
        {children}
      </main>
    </div>
  );
}

// 内部辅助：避免循环依赖，从 i18n 就地引入 region/asset 标签
import { regionLongLabel, assetTypeLabel } from "../../i18n";
const regionLongLabelLocal = regionLongLabel;
const assetTypeLabelLocal = assetTypeLabel;
