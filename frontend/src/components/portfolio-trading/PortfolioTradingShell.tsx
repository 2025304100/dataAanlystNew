import React, { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, Briefcase, ChevronDown, Zap, Trophy, Plus } from "lucide-react";
import { Tooltip } from "antd";
import { useApp } from "../../context/AppContext";
import { api } from "../../api/client";
import { t } from "../../i18n";
import type { AutoTradeReadiness } from "../../types";
import PortfolioOverview from "./PortfolioOverview";
import PortfolioMembersTable from "./PortfolioMembersTable";
import PortfolioStrategyRules from "./PortfolioStrategyRules";
import PortfolioBacktestCenter from "./PortfolioBacktestCenter";
import CreatePortfolioModal from "./CreatePortfolioModal";
import AddCandidateModal from "./AddCandidateModal";
import PortfolioRankingDrawer from "./PortfolioRankingDrawer";
import PortfolioSelectorDropdown from "./PortfolioSelectorDropdown";

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

type SubTabKey = "overview" | "members" | "strategy" | "backtest";

const VALID_SUB_TABS: SubTabKey[] = ["overview", "members", "strategy", "backtest"];

/** 将 AppContext.activeSubTab 规整为 4 子 Tab 之一，非法值回退 'overview'。 */
function resolveSubTab(raw: string | undefined): SubTabKey {
  if (raw && (VALID_SUB_TABS as string[]).includes(raw)) {
    return raw as SubTabKey;
  }
  return "overview";
}

/** 实盘/模拟标签：account_type='manual' → 实盘，其余 → 模拟（默认模拟）。 */
function accountTypeLabel(accountType: string | undefined): string {
  return accountType === "manual"
    ? t("portfolioTrading.tag.live")
    : t("portfolioTrading.tag.simulated");
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
];

const PortfolioTradingShell: React.FC = () => {
  const { portfolios, portfolioId, switchPortfolio, activeSubTab, setActiveSubTab, loadWorkbench } = useApp();

  // 浮层状态
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [addCandidateModalOpen, setAddCandidateModalOpen] = useState(false);
  const [rankingDrawerOpen, setRankingDrawerOpen] = useState(false);
  const [selectorDropdownOpen, setSelectorDropdownOpen] = useState(false);
  const [membersRevision, setMembersRevision] = useState(0);

  // 子 Tab 状态：与 AppContext.activeSubTab 同步
  const [localSubTab, setLocalSubTab] = useState<SubTabKey>(() => resolveSubTab(activeSubTab));

  // 当前组合（从 portfolios 派生）
  const currentPortfolio = useMemo(
    () => portfolios.find((p) => p.id === portfolioId) ?? portfolios[0] ?? null,
    [portfolios, portfolioId],
  );
  const currentPortfolioName = currentPortfolio?.name ?? t("portfolioTrading.header.noPortfolio");
  const currentAccountTypeLabel = accountTypeLabel(currentPortfolio?.account_type);
  const currentAssetScopeLabel =
    currentPortfolio?.asset_scope === "stock"
      ? "股票组合"
      : currentPortfolio?.asset_scope === "etf"
        ? "ETF 组合"
        : "混合组合";
  const effectivePortfolioId = currentPortfolio?.id ?? 0;
  // P0-FIX: 从真实组合 auto_trade_enabled 派生自动接管状态（0=关闭，1=开启）
  const autoTradeEnabled = currentPortfolio ? Number(currentPortfolio.auto_trade_enabled) === 1 : false;
  const isSimulated = currentPortfolio?.account_type === "simulated";

  // P0-AutoTrade：真实就绪状态 readdy（取代仅 autoTradeEnabled 的单色显示）
  const [readiness, setReadiness] = useState<AutoTradeReadiness | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const loadReadiness = useCallback(async () => {
    if (!effectivePortfolioId || !isSimulated) {
      setReadiness(null);
      return;
    }
    setReadinessLoading(true);
    try {
      const data = (await api.getAutoTradeReadiness(effectivePortfolioId, { for_schedule: true })) as AutoTradeReadiness;
      setReadiness(data);
    } catch {
      setReadiness(null);
    } finally {
      setReadinessLoading(false);
    }
  }, [effectivePortfolioId, isSimulated]);
  useEffect(() => {
    void loadReadiness();
    let handle: ReturnType<typeof setInterval> | null = null;
    if (effectivePortfolioId && isSimulated) {
      handle = setInterval(() => void loadReadiness(), 30000);
    }
    return () => {
      if (handle) clearInterval(handle);
    };
  }, [effectivePortfolioId, isSimulated, loadReadiness]);

  const ready = readiness?.ready === true;
  const hasBlockers = (readiness?.blockers?.length ?? 0) > 0;

  // 切换子 Tab：同步本地 + AppContext
  const handleSubTabChange = useCallback(
    (tab: SubTabKey) => {
      setLocalSubTab(tab);
      setActiveSubTab(tab);
    },
    [setActiveSubTab],
  );

  // 组合选择下拉 → 切换组合
  const handleSelectPortfolio = useCallback(
    async (id: number) => {
      setSelectorDropdownOpen(false);
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
    void loadWorkbench();
  }, [loadWorkbench]);

  return (
    <div className="pt-theme-scope">
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
      `}</style>

      {/* ========== 组合头部 ========== */}
      <PortfolioHeader
        portfolioName={currentPortfolioName}
        accountTypeLabel={currentAccountTypeLabel}
        assetScopeLabel={currentAssetScopeLabel}
        autoTradeEnabled={autoTradeEnabled}
        autoTradeReady={ready}
        autoTradeHasBlockers={hasBlockers}
        autoTradeLoading={readinessLoading}
        autoTradeReadiness={readiness}
        onRefreshReadiness={loadReadiness}
        onOpenSelector={() => setSelectorDropdownOpen((v) => !v)}
        onOpenRanking={() => setRankingDrawerOpen(true)}
        onOpenAddCandidate={() => setAddCandidateModalOpen(true)}
        selectorOpen={selectorDropdownOpen}
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
      <SubTabContent
        active={localSubTab}
        portfolioId={effectivePortfolioId}
        onNavigate={(tab) => handleSubTabChange(resolveSubTab(tab))}
        autoTradeEnabled={autoTradeEnabled}
        membersRevision={membersRevision}
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
      />
      <PortfolioRankingDrawer
        open={rankingDrawerOpen}
        onClose={() => setRankingDrawerOpen(false)}
        onSelectPortfolio={handleRankingSelect}
      />
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
  autoTradeReady?: boolean;
  autoTradeHasBlockers?: boolean;
  autoTradeLoading?: boolean;
  autoTradeReadiness?: AutoTradeReadiness | null;
  onRefreshReadiness?: () => void;
  selectorOpen: boolean;
  onOpenSelector: () => void;
  onOpenRanking: () => void;
  onOpenAddCandidate: () => void;
  /** 组合选择下拉面板（在触发按钮的 relative 容器内渲染，保证 position:absolute 浮在按钮正下方） */
  selectorDropdown?: React.ReactNode;
}

const PortfolioHeader: React.FC<PortfolioHeaderProps> = ({
  portfolioName,
  accountTypeLabel,
  assetScopeLabel,
  autoTradeEnabled,
  autoTradeReady,
  autoTradeHasBlockers,
  autoTradeLoading,
  autoTradeReadiness,
  onRefreshReadiness,
  selectorOpen,
  onOpenSelector,
  onOpenRanking,
  onOpenAddCandidate,
  selectorDropdown,
}) => {
  // P0-AutoTrade：语义颜色
  // enabled=false → 灰色 off
  // enabled=true + ready=true → 绿色 ready&enabled
  // enabled=true + ready=false + blockers → 红色 notReady
  // enabled=true + ready=false + no blockers + readiness loaded → 黄色 enabledOnly
  // readiness still loading → 蓝色/未知
  const loaded = autoTradeReadiness != null;
  const statusColor: "off" | "ready" | "notReady" | "enabledOnly" | "unknown" = !autoTradeEnabled
    ? "off"
    : autoTradeLoading && !loaded
    ? "unknown"
    : autoTradeReady
    ? "ready"
    : loaded && autoTradeHasBlockers
    ? "notReady"
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
      pulseClassName: "pt-status-dot-pulse",
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

  const statusText =
    !autoTradeEnabled
      ? t("portfolioTrading.status.paused")
      : autoTradeReady
      ? t("portfolioTrading.status.running")
      : t("portfolioTrading.status.notReady");

  const pillText =
    statusColor === "off"
      ? t("portfolioTrading.autotrade.off")
      : statusColor === "ready"
      ? t("portfolioTrading.autotrade.readyAndEnabled")
      : statusColor === "notReady"
      ? t("portfolioTrading.autotrade.notReady")
      : statusColor === "enabledOnly"
      ? t("portfolioTrading.autotrade.enabledOnly")
      : t("portfolioTrading.autotrade.unknown");

  const pillHint = useMemo(() => {
    if (!autoTradeEnabled) return t("autoTradeDisabled");
    const lines: string[] = [];
    lines.push(
      statusColor === "ready"
        ? t("autoTradeStatusReady")
        : statusColor === "notReady"
        ? t("autoTradeStatusNotReady")
        : statusColor === "enabledOnly"
        ? t("autoTradeStatusEnabledOnly")
        : t("autoTradeRunning"),
    );
    if (autoTradeReadiness) {
      if (!autoTradeReadiness.enabled) lines.push(`- ${t("autoTradeSubStatusEnabled")}: NO`);
      if (!autoTradeReadiness.account_ready) lines.push(`- ${t("autoTradeSubStatusAccount")}: NO`);
      if (!autoTradeReadiness.data_ready) lines.push(`- ${t("autoTradeSubStatusData")}: NO`);
      if (!autoTradeReadiness.source_ready) lines.push(`- ${t("autoTradeSubStatusSource")}: NO`);
      if (!autoTradeReadiness.schedule_ready) lines.push(`- ${t("autoTradeSubStatusSchedule")}: NO`);
      for (const b of autoTradeReadiness.blockers.slice(0, 6)) {
        lines.push(`• [${b.code}] ${b.message}`);
      }
    }
    return lines.join("\n");
  }, [autoTradeEnabled, autoTradeReadiness, statusColor]);

  return (
    <header
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
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        {/* 组合选择器按钮 */}
        <div style={{ position: "relative" }}>
          <button
            type="button"
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
            <span style={{ fontSize: 15, fontWeight: 600, color: "var(--pt-white)" }}>{portfolioName}</span>
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

        {/* P0-AutoTrade: 运行状态徽章——按 readiness 语义变色，非仅 autoTradeEnabled */}
        <Tooltip title={pillHint}>
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 12px",
              borderRadius: "var(--pt-radius-full)",
              background: palette.bg,
              color: palette.fg,
              fontSize: 12,
              fontWeight: 500,
            }}
          >
            <span
              className={palette.pulseClassName}
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: palette.dot,
                flexShrink: 0,
              }}
            />
            {statusText}
          </span>
        </Tooltip>

        {/* P0-AutoTrade: 自动接管 pill——语义颜色 + 阻塞告警圆点 + 点击刷新 */}
        <Tooltip title={pillHint}>
          <button
            type="button"
            className="pt-autotrade-pill"
            onClick={onRefreshReadiness}
            title={t("autoTradeRefreshReadiness")}
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
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <button
          type="button"
          className="pt-btn pt-btn-secondary"
          onClick={onOpenRanking}
        >
          <Trophy size={16} style={{ color: "var(--pt-state-warning)" }} />
          <span>{t("portfolioTrading.header.ranking")}</span>
        </button>
        <button
          type="button"
          className="pt-btn pt-btn-primary"
          onClick={onOpenAddCandidate}
        >
          <Plus size={16} />
          <span>{t("portfolioTrading.header.addCandidate")}</span>
        </button>
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
  autoTradeEnabled: boolean;
  membersRevision: number;
}

const SubTabContent: React.FC<SubTabContentProps> = ({ active, portfolioId, onNavigate, autoTradeEnabled, membersRevision }) => {
  switch (active) {
    case "overview":
      return <PortfolioOverview portfolioId={portfolioId} onNavigate={onNavigate} />;
    case "members":
      return <PortfolioMembersTable key={`${portfolioId}-${membersRevision}`} portfolioId={portfolioId} onNavigate={onNavigate} />;
    case "strategy":
      return <PortfolioStrategyRules portfolioId={portfolioId} onNavigate={onNavigate} />;
    case "backtest":
      return (
        <PortfolioBacktestCenter
          portfolioId={portfolioId}
          onNavigate={onNavigate}
          autoTradeEnabled={autoTradeEnabled}
        />
      );
    default:
      return <PortfolioOverview portfolioId={portfolioId} onNavigate={onNavigate} />;
  }
};

export default PortfolioTradingShell;
