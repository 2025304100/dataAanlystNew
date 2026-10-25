import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, Briefcase, ChevronDown, Zap, Trophy, Plus, AlertTriangle, Lock, Activity, Database, ShieldCheck, CheckCircle2 } from "lucide-react";
import { ConfigProvider, Tooltip, theme as antdTheme } from "antd";
import { useApp } from "../../context/AppContext";
import { api } from "../../api/client";
import { t, template } from "../../i18n";
import type { ActionItemChecks, ActionItemsResponse, PortfolioStatePermissions, PortfolioStatus, PortfolioStatusResponse } from "../../types";
import PortfolioOverview from "./PortfolioOverview";
import PortfolioMembersTable from "./PortfolioMembersTable";
import PortfolioStrategyRules from "./PortfolioStrategyRules";
import PortfolioBacktestCenter from "./PortfolioBacktestCenter";
import PortfolioGovernanceTab from "./PortfolioGovernanceTab";
import CreatePortfolioModal from "./CreatePortfolioModal";
import AddCandidateModal from "./AddCandidateModal";
import PortfolioRankingDrawer from "./PortfolioRankingDrawer";
import PortfolioReviewDrawer from "./PortfolioReviewDrawer";
import PortfolioSelectorDropdown from "./PortfolioSelectorDropdown";
import ReadinessRepairDrawer from "./ReadinessRepairDrawer";
import { resolvePortfolioNavigation, type GovernanceTabTarget } from "../../utils/actionItemRegistry";
import InspectionReturnLink from "./inspection/InspectionReturnLink";

/**
 * PortfolioTradingShell — 组合交易主壳
 *
 * 一比一还原原型图「应用主框架.html」的组合头部 + 4 子 Tab 导航结构。
 * 主题根节点 .pt-theme-scope 提供 scoped 深色青色主题（Task 1 已在 workbench.css 落地）。
 *
 * 结构：
 *   <div className="pt-theme-scope">
 *     <PortfolioHeader />        // 组合头部
 *     <SubTabNav />              // 4 子 Tab nav
 *     <SubTabContent />          // 子 Tab 内容路由
 *     <CreatePortfolioModal />   // 浮层
 *     <AddCandidateModal />
 *     <PortfolioRankingDrawer />
 *     <PortfolioSelectorDropdown />
 *   </div>
 *
 * i18n：本组件使用 t('portfolioTrading.xxx') 形式调用，对应 key 由 Task 13 补全，
 * 缺失时 t() 返回 key 字符串，不阻塞编译。
 */

type SubTabKey = "overview" | "members" | "strategy" | "backtest" | "governance";

/** 将 AppContext.activeSubTab 规整为 4 子 Tab 之一，非法值回退 'overview'。 */
function resolveSubTab(raw: string | undefined): SubTabKey {
  return resolvePortfolioNavigation(raw)?.tab ?? "overview";
}

/** 实盘/模拟标签：account_type='manual' → 实盘，其余 → 模拟（默认模拟）。 */
function accountTypeLabel(accountType: string | undefined): string {
  return accountType === "manual"
    ? t("portfolioTrading.tag.live")
    : t("portfolioTrading.tag.simulated");
}

/**
 * 组合工作台专属 antd 暗色主题。
 * 全局 ConfigProvider 是浅色算法（顶栏/设置等浅底页面使用），而工作台是深色青色主题，
 * 在 .pt-theme-scope 内嵌套一层 darkAlgorithm，使 antd 的 Modal/Drawer/Select/
 * DatePicker/Button 等弹层与基础控件与工作台同色（locale 继承外层，无需重复传）。
 */
const PT_DARK_THEME = {
  algorithm: antdTheme.darkAlgorithm,
  token: {
    colorPrimary: "#06b6d4",
    colorInfo: "#3b82f6",
    colorSuccess: "#10b981",
    colorWarning: "#f59e0b",
    colorError: "#ef4444",
    colorBgBase: "#0b1120",
    colorBgContainer: "#111827",
    colorBgElevated: "#1e293b",
    colorBorder: "#374151",
    colorBorderSecondary: "#243044",
    colorText: "#f8fafc",
    colorTextSecondary: "#94a3b8",
    colorTextTertiary: "#64748b",
    // 工作台内有 z-index 1200 的自定义抽屉/遮罩，antd 弹层需在其之上
    zIndexPopupBase: 1300,
    borderRadius: 8,
    fontFamily: 'var(--pt-font-sans, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif)',
    // 控件尺寸对齐工作台自定义体系：.pt-btn/.pt-input/.pt-select 均为 36px 高、13px 字
    controlHeight: 36,
    fontSize: 13,
    controlPaddingHorizontal: 12,
  },
  components: {
    Button: {
      fontWeight: 500,
      primaryShadow: "none",
      defaultShadow: "none",
      borderRadius: 8,
      // antd 小按钮默认取 borderRadiusSM(6px)，需与 .pt-btn-sm 的 8px 圆角对齐
      borderRadiusSM: 8,
      // 默认 36/13（与 .pt-btn 对齐），small 28/12（与 .pt-btn-sm 对齐）
      controlHeight: 36,
      controlHeightSM: 28,
      paddingInline: 16,
      paddingInlineSM: 12,
      contentFontSize: 13,
      contentFontSizeSM: 12,
    },
    Modal: { contentBg: "#1e293b", headerBg: "#1e293b" },
    Table: { headerBg: "#0f172a", headerColor: "#94a3b8", rowHoverBg: "rgba(148,163,184,0.08)" },
  },
} as const;

const HEADER_CHECKS: Array<{ key: keyof ActionItemChecks; labelKey: string }> = [
  { key: "enabled", labelKey: "repairDrawer.light.enabled" },
  { key: "account_ready", labelKey: "repairDrawer.light.account" },
  { key: "data_ready", labelKey: "repairDrawer.light.data" },
  { key: "source_ready", labelKey: "repairDrawer.light.source" },
  { key: "schedule_ready", labelKey: "repairDrawer.light.schedule" },
];

/* ==========================================================================
 * FR-P0-10 / FR-P1-8a HG1：9 状态配色（与 PortfolioSelectorDropdown/GovernanceTab 严格对齐）
 * ======================================================================== */
const PORTFOLIO_STATUS_STYLES: Partial<Record<PortfolioStatus | string, {
  bg: string; fg: string; border: string; dot: string;
}>> = {
  PENDING_INITIAL_REVIEW: { bg: "rgba(245,158,11,0.15)", fg: "#fcd34d", border: "rgba(245,158,11,0.45)", dot: "#f59e0b" },
  READY:                    { bg: "rgba(16,185,129,0.15)", fg: "#86efac", border: "rgba(16,185,129,0.45)", dot: "#10b981" },
  RUNNING_AUTO_SIMULATION:  { bg: "rgba(59,130,246,0.16)", fg: "#93c5fd", border: "rgba(59,130,246,0.45)", dot: "#3b82f6" },
  RUNNING_BACKTEST:         { bg: "rgba(99,102,241,0.18)", fg: "#a5b4fc", border: "rgba(99,102,241,0.45)", dot: "#6366f1" },
  DATA_INCOMPLETE_PAUSED:   { bg: "rgba(234,179,8,0.16)", fg: "#fde047", border: "rgba(234,179,8,0.45)", dot: "#eab308" },
  RECONCILIATION_BLOCKED:   { bg: "rgba(239,68,68,0.16)", fg: "#fca5a5", border: "rgba(239,68,68,0.5)", dot: "#ef4444" },
  MODEL_INACTIVE:           { bg: "rgba(107,114,128,0.22)", fg: "#cbd5e1", border: "rgba(107,114,128,0.5)", dot: "#6b7280" },
  SCORE_STALE:              { bg: "rgba(249,115,22,0.16)", fg: "#fdba74", border: "rgba(249,115,22,0.45)", dot: "#f97316" },
  INTERRUPTED:              { bg: "rgba(217,78,239,0.17)", fg: "#f0abfc", border: "rgba(217,78,239,0.45)", dot: "#d946ef" },
  ADMIN_PAUSED:             { bg: "rgba(220,38,38,0.18)", fg: "#fca5a5", border: "rgba(220,38,38,0.55)", dot: "#dc2626" },
};
const PORTFOLIO_STATUS_FALLBACK = { bg: "rgba(107,114,128,0.22)", fg: "#cbd5e1", border: "rgba(148,163,184,0.45)", dot: "#94a3b8", title: "状态未知", hint: "未从治理 API 读取到组合状态。" };

function portfolioStatusMeta(status: PortfolioStatus | string | null | undefined) {
  const style = status ? PORTFOLIO_STATUS_STYLES[status] : undefined;
  if (!style) return PORTFOLIO_STATUS_FALLBACK;
  return {
    ...style,
    title: t(`portfolioStatus.${status}` as "portfolioStatus.READY"),
    hint: t(`portfolioStatus.${status}.desc` as "portfolioStatus.READY.desc"),
  };
}

/** FR-P1-8a 9×4 权限矩阵静态兜底（API 未返回 permissions 时使用，与 GovernanceTab 对齐）。 */
const HG1_FALLBACK_MATRIX: Record<PortfolioStatus | string, PortfolioStatePermissions> = {
  PENDING_INITIAL_REVIEW:  { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
  READY:                   { allow_new_buys: true,  allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  RUNNING_AUTO_SIMULATION: { allow_new_buys: true,  allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  RUNNING_BACKTEST:        { allow_new_buys: true,  allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  DATA_INCOMPLETE_PAUSED:  { allow_new_buys: false, allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  RECONCILIATION_BLOCKED:  { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
  MODEL_INACTIVE:          { allow_new_buys: false, allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  SCORE_STALE:             { allow_new_buys: false, allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  INTERRUPTED:             { allow_new_buys: false, allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  ADMIN_PAUSED:            { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
  UNKNOWN:                 { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
};
function resolveShellPermissions(status: PortfolioStatusResponse | null): PortfolioStatePermissions {
  if (!status) return HG1_FALLBACK_MATRIX.UNKNOWN;
  if (status.permissions) return status.permissions;
  return HG1_FALLBACK_MATRIX[status.current_state] ?? HG1_FALLBACK_MATRIX.UNKNOWN;
}

interface SubTabDef {
  key: SubTabKey;
  label: string;
}

const SUB_TABS: SubTabDef[] = [
  { key: "overview", label: t("portfolioTrading.subtab.overview") },
  { key: "members", label: t("portfolioTrading.subtab.members") },
  { key: "strategy", label: t("portfolioTrading.subtab.strategy") },
  { key: "backtest", label: t("portfolioTrading.subtab.backtest") },
  { key: "governance", label: t("portfolioTrading.subtab.governance") },
];

const PortfolioTradingShell: React.FC = () => {
  const { portfolios, portfolioId, switchPortfolio, activeSubTab, setActiveSubTab, loadWorkbench } = useApp();

  // 浮层状态
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [addCandidateModalOpen, setAddCandidateModalOpen] = useState(false);
  const [rankingDrawerOpen, setRankingDrawerOpen] = useState(false);
  // 复盘抽屉：按需打开（创建复盘会触发后端实时算归因，不放首屏）
  const [reviewsOpen, setReviewsOpen] = useState(false);
  const [selectorDropdownOpen, setSelectorDropdownOpen] = useState(false);
  const [membersRevision, setMembersRevision] = useState(0);

  // 子 Tab 状态：与 AppContext.activeSubTab 同步
  const [localSubTab, setLocalSubTab] = useState<SubTabKey>(() => resolveSubTab(activeSubTab));
  const [governanceNavigation, setGovernanceNavigation] = useState<{ tab: GovernanceTabTarget; requestId: number }>(() => ({
    tab: resolvePortfolioNavigation(activeSubTab)?.governanceTab ?? "overview",
    requestId: 0,
  }));

  // VIZ-0929-08：消费研究页"模拟下单"深链（?symbol_id=&source=research）——
  // 切到持仓成员子页并定位该标的行，替代此前无人读取、落在总览页的死链接。
  const [focusMemberSymbolId, setFocusMemberSymbolId] = useState<number | null>(null);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const sid = Number(params.get("symbol_id"));
    if (params.get("source") === "research" && Number.isFinite(sid) && sid > 0) {
      setLocalSubTab("members");
      setFocusMemberSymbolId(sid);
    }
  }, []);

  // P0：行动项「直达子页」目标面板高亮态（事件监听挂在 handleSubTabChange 之后）。
  const [highlightTab, setHighlightTab] = useState<SubTabKey | null>(null);

  // 当前组合（从 portfolios 派生）
  const currentPortfolio = useMemo(
    () => portfolios.find((p) => p.id === portfolioId) ?? portfolios[0] ?? null,
    [portfolios, portfolioId],
  );
  // VIZ-0929-16：首屏 portfolios 还没到货时，头部会闪一下"暂无组合"（其实是有组合、只是没加载完）。
  // 只在"曾经拿到过列表但为空"时才使用该空态文案。
  const portfoliosArrivedRef = useRef(false);
  if (portfolios.length > 0) portfoliosArrivedRef.current = true;
  const currentPortfolioName =
    currentPortfolio?.name
    ?? (portfoliosArrivedRef.current ? t("portfolioTrading.header.noPortfolio") : t("loading"));
  const currentAccountTypeLabel = accountTypeLabel(currentPortfolio?.account_type);
  const currentAssetScopeLabel =
    currentPortfolio?.asset_scope === "stock"
      ? "股票组合"
      : currentPortfolio?.asset_scope === "etf"
        ? "ETF 组合"
        : "混合组合";
  const effectivePortfolioId = currentPortfolio?.id ?? 0;
  const currentPortfolioIdRef = useRef(effectivePortfolioId);
  currentPortfolioIdRef.current = effectivePortfolioId;
  // P0-FIX: 从真实组合 auto_trade_enabled 派生自动接管状态（0=关闭，1=开启）
  const autoTradeEnabled = currentPortfolio ? Number(currentPortfolio.auto_trade_enabled) === 1 : false;
  const isSimulated = currentPortfolio?.account_type === "simulated";

  // FR-P0-10/P1-8a：组合治理状态 + 权限
  const [portfolioStatus, setPortfolioStatus] = useState<PortfolioStatusResponse | null>(null);
  const [statusPortfolioId, setStatusPortfolioId] = useState(0);
  const statusRequestRef = useRef(0);
  const [statusLoading, setStatusLoading] = useState(false);
  const loadPortfolioStatus = useCallback(async () => {
    const requestId = ++statusRequestRef.current;
    const isCurrent = () => requestId === statusRequestRef.current && effectivePortfolioId === currentPortfolioIdRef.current;
    if (!effectivePortfolioId) {
      setPortfolioStatus(null);
      setStatusPortfolioId(0);
      setStatusLoading(false);
      return;
    }
    setStatusLoading(true);
    try {
      const data = await api.getPortfolioStatus(effectivePortfolioId);
      if (isCurrent()) {
        setPortfolioStatus(data ?? null);
        setStatusPortfolioId(effectivePortfolioId);
      }
    } catch {
      if (isCurrent()) setPortfolioStatus(null);
    } finally {
      if (isCurrent()) setStatusLoading(false);
    }
  }, [effectivePortfolioId]);
  useEffect(() => {
    void loadPortfolioStatus();
    let handle: ReturnType<typeof setInterval> | null = null;
    if (effectivePortfolioId) handle = setInterval(() => void loadPortfolioStatus(), 45000);
    return () => { if (handle) clearInterval(handle); };
  }, [effectivePortfolioId, loadPortfolioStatus]);
  // 从 Portfolio 接口派生的状态（当 governance API 还没返回时，优先用列表接口里的 portfolio_status 显示色）
  const currentStatus = statusPortfolioId === effectivePortfolioId ? portfolioStatus : null;
  const portfolioStatusRaw: PortfolioStatus | string | null | undefined =
    currentStatus?.current_state ?? currentPortfolio?.portfolio_status;
  const perm = useMemo<PortfolioStatePermissions>(
    () => resolveShellPermissions(currentStatus),
    [currentStatus],
  );
  // FR-P1-8a：综合判定治理层是否阻断操作（需要人工确认的终态/严重态）
  const governanceBlocked = useMemo<boolean>(() => {
    if (perm.requires_manual_ack) return true;
    if (!portfolioStatusRaw) return false;
    return ["ADMIN_PAUSED", "RECONCILIATION_BLOCKED", "PENDING_INITIAL_REVIEW"].includes(String(portfolioStatusRaw));
  }, [perm, portfolioStatusRaw]);

  // P0：行动项聚合（HG1 状态机 + 就绪五维 + G7 运维），顶栏计数与修复抽屉的唯一数据源
  const [actionItems, setActionItems] = useState<ActionItemsResponse | null>(null);
  const [actionPortfolioId, setActionPortfolioId] = useState(0);
  const actionRequestRef = useRef(0);
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [toggling, setToggling] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const loadActionItems = useCallback(async () => {
    const requestId = ++actionRequestRef.current;
    const isCurrent = () => requestId === actionRequestRef.current && effectivePortfolioId === currentPortfolioIdRef.current;
    if (!effectivePortfolioId) {
      setActionItems(null);
      setActionPortfolioId(0);
      setActionLoading(false);
      return;
    }
    setActionLoading(true);
    setActionError(null);
    try {
      const data = await api.getPortfolioActionItems(effectivePortfolioId, { for_schedule: true });
      if (isCurrent()) {
        setActionItems(data);
        setActionPortfolioId(effectivePortfolioId);
      }
    } catch {
      if (isCurrent()) {
        setActionItems(null);
        setActionError(t("repairDrawer.loadFailed"));
      }
    } finally {
      if (isCurrent()) setActionLoading(false);
    }
  }, [effectivePortfolioId]);
  useEffect(() => {
    void loadActionItems();
    const handle = effectivePortfolioId
      ? setInterval(() => void loadActionItems(), 30000)
      : null;
    return () => { if (handle) clearInterval(handle); };
  }, [effectivePortfolioId, loadActionItems]);

  const currentActionItems = actionPortfolioId === effectivePortfolioId ? actionItems : null;
  const blockerCount = currentActionItems?.summary.blockers ?? 0;
  const actionReady = currentActionItems?.summary.ready === true && blockerCount === 0;
  const actionWaitingForSignal = currentActionItems?.summary.waiting_for_signal === true && blockerCount === 0;
  const actionRunning = blockerCount === 0 && (
    currentActionItems?.summary.execution_state === "running" ||
    (currentActionItems?.summary.execution_state == null && portfolioStatusRaw === "RUNNING_AUTO_SIMULATION" && (actionReady || actionWaitingForSignal))
  );
  const actionChecks: ActionItemChecks | null = currentActionItems?.checks ?? null;
  const refreshPortfolioSources = useCallback(async () => {
    await Promise.allSettled([loadActionItems(), loadPortfolioStatus(), loadWorkbench()]);
  }, [loadActionItems, loadPortfolioStatus, loadWorkbench]);
  const handleGovernanceChanged = useCallback(async (changedPortfolioId: number) => {
    if (changedPortfolioId !== currentPortfolioIdRef.current) return;
    await refreshPortfolioSources();
  }, [refreshPortfolioSources]);

  // 抽屉内原地开关仅在 HG1 健康/运行态放行；其余引导去总览，避免绕过门禁二次确认
  const canToggleInDrawer =
    portfolioStatusRaw === "READY" ||
    portfolioStatusRaw === "RUNNING_AUTO_SIMULATION" ||
    portfolioStatusRaw === "RUNNING_BACKTEST";

  const handleToggleEnabled = useCallback(async () => {
    if (!effectivePortfolioId || toggling) return;
    setToggling(true);
    setActionError(null);
    try {
      await api.updatePortfolio(effectivePortfolioId, {
        auto_trade_enabled: autoTradeEnabled ? 0 : 1,
      });
      await Promise.all([loadActionItems(), loadPortfolioStatus(), loadWorkbench()]);
    } catch (err: any) {
      setActionError(
        t("portfolioTrading.overview.autoTradeToggleFailed") +
          (err?.message ? ": " + err.message : ""),
      );
    } finally {
      setToggling(false);
    }
  }, [
    effectivePortfolioId,
    toggling,
    autoTradeEnabled,
    loadActionItems,
    loadPortfolioStatus,
    loadWorkbench,
  ]);

  // 切换子 Tab：同步本地 + AppContext
  const handleSubTabChange = useCallback(
    (target: string) => {
      const navigation = resolvePortfolioNavigation(target);
      if (!navigation) return;
      if (navigation.tab === "governance") {
        setGovernanceNavigation((previous) => ({
          tab: navigation.governanceTab ?? "overview",
          requestId: previous.requestId + 1,
        }));
      }
      setLocalSubTab(navigation.tab);
      setActiveSubTab(navigation.tab);
    },
    [setActiveSubTab],
  );

  // P0：行动项「直达子页」——监听 navigate.ts 的请求，切页并对目标面板做一次性高亮。
  useEffect(() => {
    const handler = (event: Event) => {
      const target = (event as CustomEvent<{ tab?: string }>).detail?.tab;
      const navigation = resolvePortfolioNavigation(target);
      if (!navigation || !target) return;
      const tab = navigation.tab;
      handleSubTabChange(target);
      setDrawerOpen(false);
      setHighlightTab(tab);
      window.setTimeout(() => {
        setHighlightTab((cur) => (cur === tab ? null : cur));
      }, 1800);
    };
    window.addEventListener("portfolio:subtab-navigate", handler);
    return () => window.removeEventListener("portfolio:subtab-navigate", handler);
  }, [handleSubTabChange]);

  // 组合选择下拉 → 切换组合
  const handleSelectPortfolio = useCallback(
    async (id: number) => {
      setSelectorDropdownOpen(false);
      setGovernanceNavigation(previous=>({tab:"overview",requestId:previous.requestId+1}));
      try {
        await switchPortfolio(id);
      } catch {
        /* switchPortfolio 内部已 toast */
      }
    },
    [switchPortfolio],
  );

  // 组合选择下拉 → 新建组合
  const handleCreateNewFromSelector = useCallback(() => {
    setSelectorDropdownOpen(false);
    setCreateModalOpen(true);
  }, []);

  // 新建组合成功 → 切换到新组合
  const handleCreateSuccess = useCallback(
    async (id: number) => {
      setCreateModalOpen(false);
      try {
        await switchPortfolio(id);
      } catch {
        /* ignore */
      }
    },
    [switchPortfolio],
  );

  // 排名抽屉 → 选中组合
  const handleRankingSelect = useCallback(
    async (id: number) => {
      setRankingDrawerOpen(false);
      try {
        await switchPortfolio(id);
      } catch {
        /* ignore */
      }
    },
    [switchPortfolio],
  );

  const handleCandidatesAdded = useCallback(() => {
    setMembersRevision((value) => value + 1);
    void refreshPortfolioSources();
  }, [refreshPortfolioSources]);

  return (
    <div className="pt-theme-scope pt-portfolio-workbench">
      <ConfigProvider theme={PT_DARK_THEME}>
      {/* scoped 动画（脉冲圆点 / 闪电呼吸），自包含不污染全局 */}
      <style>{`
        @keyframes pt-dot-pulse {
          0%, 100% { box-shadow: 0 0 0 0 rgba(16,185,129,0.55); }
          50% { box-shadow: 0 0 0 5px rgba(16,185,129,0); }
        }
        .pt-status-dot-pulse { animation: pt-dot-pulse 2s ease-in-out infinite; }
        @keyframes pt-bolt-pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.55; }
        }
        .pt-bolt-pulse { animation: pt-bolt-pulse 2s ease-in-out infinite; }
        @keyframes pt-highlight-flash {
          0% { box-shadow: 0 0 0 0 rgba(34,211,238,0.0); outline: 2px solid rgba(34,211,238,0.0); }
          25% { box-shadow: 0 0 0 4px rgba(34,211,238,0.18); outline: 2px solid rgba(34,211,238,0.85); border-radius: 10px; }
          100% { box-shadow: 0 0 0 0 rgba(34,211,238,0.0); outline: 2px solid rgba(34,211,238,0.0); }
        }
        .pt-theme-scope [data-pt-highlight="true"] { animation: pt-highlight-flash 1.8s ease-out 1; }
      `}</style>

      {/* ========== 组合头部 ========== */}
      <PortfolioHeader
        portfolioName={currentPortfolioName}
        accountTypeLabel={currentAccountTypeLabel}
        assetScopeLabel={currentAssetScopeLabel}
        autoTradeEnabled={autoTradeEnabled}
        actionReady={actionReady}
        actionWaitingForSignal={actionWaitingForSignal}
        actionRunning={actionRunning}
        blockerCount={blockerCount}
        actionLoading={actionLoading}
        checks={actionChecks}
        isSimulated={isSimulated}
        governanceBlocked={governanceBlocked}
        onOpenRepair={() => setDrawerOpen(true)}
        onOpenSelector={() => setSelectorDropdownOpen((v) => !v)}
        onOpenRanking={() => setRankingDrawerOpen(true)}
        onOpenReviews={() => setReviewsOpen(true)}
        onOpenAddCandidate={() => setAddCandidateModalOpen(true)}
        selectorOpen={selectorDropdownOpen}
        portfolioStatusRaw={portfolioStatusRaw}
        statusLoading={statusLoading}
        allowNewBuys={perm.allow_new_buys}
        selectorDropdown={
          <PortfolioSelectorDropdown
            open={selectorDropdownOpen}
            onClose={() => setSelectorDropdownOpen(false)}
            onSelect={handleSelectPortfolio}
            onCreateNew={handleCreateNewFromSelector}
            currentPortfolioId={currentPortfolio?.id}
          />
        }
      />

      {/* ========== 4 子 Tab nav ========== */}
      <SubTabNav active={localSubTab} onChange={handleSubTabChange} />

      {/* ========== 子 Tab 内容路由 ========== */}
      {localSubTab !== "governance" && <InspectionReturnLink />}
      <SubTabContent
        active={localSubTab}
        portfolioId={effectivePortfolioId}
        onNavigate={handleSubTabChange}
        governanceNavigation={governanceNavigation}
        onGovernanceChanged={handleGovernanceChanged}
        autoTradeEnabled={autoTradeEnabled}
        membersRevision={membersRevision}
        portfolioStatus={currentStatus}
        perm={perm}
        focusSymbolId={focusMemberSymbolId}
        highlightTab={highlightTab}
      />

      {/* ========== 浮层 ========== */}
      <CreatePortfolioModal
        open={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        onSuccess={handleCreateSuccess}
      />
      <AddCandidateModal
        open={addCandidateModalOpen}
        onClose={() => setAddCandidateModalOpen(false)}
        portfolioId={effectivePortfolioId}
        onSuccess={handleCandidatesAdded}
        portfolioStatus={currentStatus}
        perm={perm}
        statusLoading={statusLoading}
      />
      <PortfolioRankingDrawer
        open={rankingDrawerOpen}
        onClose={() => setRankingDrawerOpen(false)}
        onSelectPortfolio={handleRankingSelect}
      />
      <PortfolioReviewDrawer
        open={reviewsOpen}
        onClose={() => setReviewsOpen(false)}
        portfolioId={effectivePortfolioId}
      />
      <ReadinessRepairDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        portfolioId={effectivePortfolioId}
        data={currentActionItems}
        loading={actionLoading}
        loadError={actionError}
        isSimulated={isSimulated}
        toggling={toggling}
        canToggle={canToggleInDrawer}
        onRefresh={refreshPortfolioSources}
        onToggleEnabled={handleToggleEnabled}
        onOpenAddCandidate={() => setAddCandidateModalOpen(true)}
      />
      </ConfigProvider>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* 组合头部 —— 一比一对齐原型图 portfolio-header                       */
/* ------------------------------------------------------------------ */

interface PortfolioHeaderProps {
  portfolioName: string;
  accountTypeLabel: string;
  assetScopeLabel: string;
  autoTradeEnabled: boolean;
  actionReady: boolean;
  actionWaitingForSignal: boolean;
  actionRunning: boolean;
  blockerCount: number;
  actionLoading: boolean;
  checks: ActionItemChecks | null;
  isSimulated: boolean;
  governanceBlocked: boolean;
  onOpenRepair: () => void;
  selectorOpen: boolean;
  onOpenSelector: () => void;
  onOpenRanking: () => void;
  onOpenReviews: () => void;
  onOpenAddCandidate: () => void;
  /** 组合选择下拉面板（在触发按钮的 relative 容器内渲染，保证 position:absolute 浮在按钮正下方） */
  selectorDropdown?: React.ReactNode;
  // FR-P0-10/P1-8a HG1 字段
  portfolioStatusRaw?: PortfolioStatus | string | null;
  statusLoading?: boolean;
  allowNewBuys?: boolean;
}

const PortfolioHeader: React.FC<PortfolioHeaderProps> = ({
  portfolioName,
  accountTypeLabel,
  assetScopeLabel,
  autoTradeEnabled,
  actionReady,
  actionWaitingForSignal,
  actionRunning,
  blockerCount,
  actionLoading,
  checks,
  isSimulated,
  onOpenRepair,
  selectorOpen,
  onOpenSelector,
  onOpenRanking,
  onOpenReviews,
  onOpenAddCandidate,
  selectorDropdown,
  portfolioStatusRaw,
  statusLoading,
  governanceBlocked,
  allowNewBuys = true,
}) => {
  // P0：单 pill 语义色。数据源为 action-items 聚合（checks 五维 + summary 计数）。
  // off=开关关；notReady=HG1 硬阻塞或存在 blocker；ready=就绪；
  // enabledOnly=已开但暂不就绪且无 blocker；unknown=聚合尚未返回。
  const loaded = checks != null;
  const statusColor: "off" | "ready" | "running" | "waitingSignal" | "notReady" | "enabledOnly" | "unknown" = !autoTradeEnabled
    ? "off"
    : governanceBlocked || blockerCount > 0
    ? "notReady"
    : actionRunning
    ? "running"
    : actionWaitingForSignal
    ? "waitingSignal"
    : actionReady
    ? "ready"
    : loaded
    ? "enabledOnly"
    : "unknown";

  const colorMap: Record<typeof statusColor, { bg: string; fg: string; dot: string; pulseClassName: string }> = {
    off: {
      bg: "var(--pt-surface-3)",
      fg: "var(--pt-muted-foreground)",
      dot: "var(--pt-muted-foreground)",
      pulseClassName: "",
    },
    ready: {
      bg: "var(--pt-state-success-dim)",
      fg: "var(--pt-state-success)",
      dot: "var(--pt-state-success)",
      pulseClassName: "",
    },
    running: {
      bg: "var(--pt-state-success-dim)",
      fg: "var(--pt-state-success)",
      dot: "var(--pt-state-success)",
      pulseClassName: "pt-status-dot-pulse",
    },
    waitingSignal: {
      bg: "var(--pt-state-success-dim)",
      fg: "var(--pt-state-success)",
      dot: "var(--pt-state-success)",
      pulseClassName: "",
    },
    notReady: {
      bg: "rgba(239,68,68,0.12)",
      fg: "var(--pt-state-error, #ef4444)",
      dot: "var(--pt-state-error, #ef4444)",
      pulseClassName: "",
    },
    enabledOnly: {
      bg: "rgba(245,158,11,0.14)",
      fg: "var(--pt-state-warning, #f59e0b)",
      dot: "var(--pt-state-warning, #f59e0b)",
      pulseClassName: "",
    },
    unknown: {
      bg: "rgba(59,130,246,0.12)",
      fg: "var(--pt-primary, #3b82f6)",
      dot: "var(--pt-primary, #3b82f6)",
      pulseClassName: "",
    },
  };
  const palette = colorMap[statusColor];

  const pillText =
    statusColor === "off"
      ? t("portfolioTrading.autotrade.off")
      : statusColor === "ready"
      ? t("repairDrawer.pillWaiting")
      : statusColor === "running"
      ? t("repairDrawer.pillRunning")
      : statusColor === "waitingSignal"
      ? t("repairDrawer.pillWaitingForSignal")
      : statusColor === "notReady"
      ? // 聚合尚未返回（首屏/轮询失败）时 blockerCount 恒为 0，
        // 不能把"未加载"渲染成"0 项待处理"误导用户，回退到未知态文案。
        loaded
        ? template("repairDrawer.pillPending", { n: blockerCount })
        : t("portfolioTrading.autotrade.unknown")
      : statusColor === "enabledOnly"
      ? t("portfolioTrading.autotrade.enabledOnly")
      : t("portfolioTrading.autotrade.unknown");

  // P0：hover 只留五维灯 + 一句引导；长说明与修复动作全部进修复抽屉。
  const pillHint: React.ReactNode = !autoTradeEnabled ? (
    <span>{t("autoTradeDisabled")}</span>
  ) : (
    <div style={{ maxWidth: 320, fontSize: 12, lineHeight: 1.7 }}>
      <div style={{ fontWeight: 600, marginBottom: checks ? 6 : 0, color: statusColor === "ready" || statusColor === "running" || statusColor === "waitingSignal" ? "#86efac" : "#fecaca" }}>
        {statusColor === "running" ? t("repairDrawer.pillRunning") : statusColor === "ready" ? t("repairDrawer.header.ready") : statusColor === "waitingSignal" ? t("repairDrawer.header.waitingForSignal") : t("repairDrawer.header.notReady")}
      </div>
      {isSimulated && checks && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 10px", marginBottom: 6 }}>
          {HEADER_CHECKS.map(({ key, labelKey }) => {
            const no = !checks[key];
            return (
              <span key={key} style={{ color: no ? "#fca5a5" : "#86efac", whiteSpace: "nowrap" }}>
                {t(labelKey)}: <strong style={{ fontWeight: 700 }}>{no ? "NO" : "OK"}</strong>
              </span>
            );
          })}
        </div>
      )}
      <div style={{ color: "#94a3b8" }}>{t("repairDrawer.openHint")}</div>
    </div>
  );

  return (
    <header
      className="pt-portfolio-header"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "16px 24px",
        background: "var(--pt-surface-1)",
        borderBottom: "1px solid var(--pt-border)",
      }}
    >
      {/* 左侧：组合选择器 + 运行状态徽章 + 自动接管 pill */}
      <div className="pt-portfolio-header-primary" style={{ display: "flex", alignItems: "center", gap: 12 }}>
        {/* 组合选择器按钮 */}
        <div style={{ position: "relative" }}>
          <button
            type="button"
            className="pt-portfolio-selector"
            onClick={onOpenSelector}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              height: 36,
              padding: "0 12px",
              background: "var(--pt-surface-2)",
              border: "1px solid var(--pt-border)",
              borderRadius: "var(--pt-radius-md)",
              color: "var(--pt-foreground)",
              cursor: "pointer",
              transition: "background 0.15s ease, border-color 0.15s ease",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = "var(--pt-surface-3)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "var(--pt-surface-2)";
            }}
          >
            <Briefcase size={16} style={{ color: "var(--pt-muted-foreground)" }} />
            <span className="pt-portfolio-name" style={{ fontSize: 15, fontWeight: 600, color: "var(--pt-white)" }}>{portfolioName}</span>
            {/* 实盘/模拟标签 */}
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                fontSize: 11,
                fontWeight: 500,
                padding: "2px 6px",
                borderRadius: "var(--pt-radius-sm)",
                background: "var(--pt-state-success-dim)",
                color: "var(--pt-state-success)",
              }}
            >
              {accountTypeLabel}
            </span>
            <span
              title="组合标的范围：候选、成员、持仓、回测与买入交易均受此限制"
              style={{
                display: "inline-flex",
                alignItems: "center",
                fontSize: 11,
                fontWeight: 500,
                padding: "2px 6px",
                borderRadius: "var(--pt-radius-sm)",
                background: "rgba(59, 130, 246, 0.12)",
                color: "var(--pt-primary)",
              }}
            >
              {assetScopeLabel}
            </span>
            {/* FR-P0-10/P1-8a 9 状态徽章：与 GovernanceTab/SelectorDropdown 色板一致 */}
            {(() => {
              const meta = portfolioStatusMeta(portfolioStatusRaw);
              const isEmphasis =
                portfolioStatusRaw === "ADMIN_PAUSED" || portfolioStatusRaw === "RECONCILIATION_BLOCKED";
              return (
                <Tooltip title={`${meta.title}\n${meta.hint}`}>
                  <span
                    onClick={onOpenRepair}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      cursor: "pointer",
                      gap: 6,
                      fontSize: 11,
                      fontWeight: 500,
                      padding: "2px 10px",
                      borderRadius: "var(--pt-radius-sm)",
                      border: `1px solid ${meta.border}`,
                      background: meta.bg,
                      color: meta.fg,
                      boxShadow: isEmphasis ? "0 0 0 1px rgba(220,38,38,0.08)" : undefined,
                    }}
                  >
                    {statusLoading ? (
                      <Activity size={10} style={{ opacity: 0.7 }} />
                    ) : portfolioStatusRaw === "READY" ? (
                      <CheckCircle2 size={10} />
                    ) : portfolioStatusRaw === "ADMIN_PAUSED" ? (
                      <Lock size={10} />
                    ) : portfolioStatusRaw === "DATA_INCOMPLETE_PAUSED" || portfolioStatusRaw === "SCORE_STALE" ? (
                      <Database size={10} />
                    ) : portfolioStatusRaw === "RUNNING_AUTO_SIMULATION" || portfolioStatusRaw === "RUNNING_BACKTEST" ? (
                      <Activity size={10} />
                    ) : portfolioStatusRaw === "RECONCILIATION_BLOCKED" ? (
                      <AlertTriangle size={10} />
                    ) : portfolioStatusRaw === "READY" ? (
                      <ShieldCheck size={10} />
                    ) : (
                      <AlertTriangle size={10} />
                    )}
                    {meta.title}
                  </span>
                </Tooltip>
              );
            })()}
            <ChevronDown
              size={16}
              style={{
                color: selectorOpen ? "var(--pt-primary)" : "var(--pt-muted-foreground)",
                transition: "color 0.15s ease",
              }}
            />
          </button>
          {/* 组合选择下拉面板：在 relative 容器内渲染，position:absolute 浮在按钮正下方 */}
          {selectorDropdown}
        </div>

        {/* P0-AutoTrade: 自动接管 pill——单 pill，语义颜色 + 阻塞计数，点击开修复抽屉 */}
        <Tooltip title={pillHint}>
          <button
            type="button"
            className="pt-autotrade-pill"
            onClick={onOpenRepair}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 12px",
              borderRadius: "var(--pt-radius-full)",
              border: `1px solid ${palette.bg}`,
              background: palette.bg,
              color: palette.fg,
              fontSize: 12,
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            <Zap size={14} className={statusColor === "ready" ? "pt-bolt-pulse" : ""} />
            {pillText}
            {statusColor === "notReady" && (
              <AlertCircle size={12} style={{ opacity: 0.9 }} />
            )}
          </button>
        </Tooltip>
      </div>

      {/* 右侧：组合排名 + 添加候选标的 */}
      <div className="pt-portfolio-header-actions" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <button
          type="button"
          className="pt-btn pt-btn-secondary"
          onClick={onOpenRanking}
        >
          <Trophy size={16} style={{ color: "var(--pt-state-warning)" }} />
          <span>{t("portfolioTrading.header.ranking")}</span>
        </button>
        {/* 复盘记录抽屉（承接原孤儿面板的 reviews 能力，报告 §二十七） */}
        <button
          type="button"
          className="pt-btn pt-btn-secondary"
          onClick={onOpenReviews}
          data-testid="open-review-drawer"
        >
          <span>{t("portfolioTrading.header.reviews")}</span>
        </button>
        {/* FR-P1-8a：添加候选标的入口（NEW_BUY 入口），allow_new_buys=false 时禁用并给出明确原因 */}
        {(() => {
          const meta = portfolioStatusMeta(portfolioStatusRaw);
          const reason = allowNewBuys
            ? undefined
            : `${template("portfolioTrading.header.newBuysBlocked", { state: meta.title })}\n${meta.hint}`;
          const btn = (
            <button
              type="button"
              className="pt-btn pt-btn-primary"
              onClick={onOpenAddCandidate}
              disabled={!allowNewBuys}
              style={{ opacity: !allowNewBuys ? 0.55 : 1, cursor: !allowNewBuys ? "not-allowed" : undefined }}
            >
              <Plus size={16} />
              <span>{t("portfolioTrading.header.addCandidate")}</span>
            </button>
          );
          return !allowNewBuys ? <Tooltip title={reason ?? ""} placement="bottomRight">{btn}</Tooltip> : btn;
        })()}
      </div>
    </header>
  );
};

/* ------------------------------------------------------------------ */
/* 4 子 Tab nav                                                        */
/* ------------------------------------------------------------------ */

interface SubTabNavProps {
  active: SubTabKey;
  onChange: (tab: SubTabKey) => void;
}

const SubTabNav: React.FC<SubTabNavProps> = ({ active, onChange }) => {
  return (
    <nav
      className="pt-portfolio-subtabs"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 24,
        height: 44,
        padding: "0 24px",
        background: "var(--pt-surface-1)",
        borderBottom: "1px solid var(--pt-border)",
      }}
    >
      {SUB_TABS.map((tab) => (
        <button
          key={tab.key}
          type="button"
          className={`pt-tab${active === tab.key ? " active" : ""}`}
          onClick={() => onChange(tab.key)}
        >
          {tab.label}
        </button>
      ))}
    </nav>
  );
};

/* ------------------------------------------------------------------ */
/* 子 Tab 内容路由                                                     */
/* ------------------------------------------------------------------ */

interface SubTabContentProps {
  active: SubTabKey;
  portfolioId: number;
  onNavigate: (tab: string) => void;
  governanceNavigation: { tab: GovernanceTabTarget; requestId: number };
  onGovernanceChanged: (portfolioId: number) => void | Promise<void>;
  autoTradeEnabled: boolean;
  membersRevision: number;
  portfolioStatus: PortfolioStatusResponse | null;
  perm: PortfolioStatePermissions;
  focusSymbolId?: number | null;
  highlightTab?: SubTabKey | null;
}

const SubTabContent: React.FC<SubTabContentProps> = ({ active, portfolioId, onNavigate, governanceNavigation, onGovernanceChanged, autoTradeEnabled, membersRevision, portfolioStatus, perm, focusSymbolId, highlightTab }) => {
  let content: React.ReactNode;
  switch (active) {
    case "overview":
      content = <PortfolioOverview portfolioId={portfolioId} onNavigate={onNavigate} portfolioStatus={portfolioStatus} perm={perm} />;
      break;
    case "members":
      content = <PortfolioMembersTable key={`${portfolioId}-${membersRevision}`} portfolioId={portfolioId} onNavigate={onNavigate} portfolioStatus={portfolioStatus} perm={perm} focusSymbolId={focusSymbolId} />;
      break;
    case "strategy":
      content = <PortfolioStrategyRules portfolioId={portfolioId} onNavigate={onNavigate} portfolioStatus={portfolioStatus} perm={perm} onConfigurationChanged={onGovernanceChanged} />;
      break;
    case "backtest":
      content = (
        <PortfolioBacktestCenter
          portfolioId={portfolioId}
          onNavigate={onNavigate}
          autoTradeEnabled={autoTradeEnabled}
        />
      );
      break;
    case "governance":
      content = <PortfolioGovernanceTab key={portfolioId} portfolioId={portfolioId} onNavigate={onNavigate} navigation={governanceNavigation} onGovernanceChanged={onGovernanceChanged} />;
      break;
    default:
      content = <PortfolioOverview portfolioId={portfolioId} onNavigate={onNavigate} portfolioStatus={portfolioStatus} perm={perm} />;
  }
  // 行动项直达时对目标子页做一次不占布局的高亮（outline 动画，见顶部 keyframes）。
  return (
    <div data-pt-highlight={highlightTab === active ? "true" : undefined} style={{ minHeight: 0 }}>
      {content}
    </div>
  );
};

export default PortfolioTradingShell;
