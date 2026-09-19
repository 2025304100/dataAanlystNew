import React, { useState, useCallback, useRef, useEffect } from "react";
import { Spin, Alert } from "antd";
import { useApp } from "./context/AppContext";
import { t } from "./i18n";
import InvestmentCenter from "./components/InvestmentCenter";
import TodayDecision from "./components/TodayDecision";
// WP1.1：机会中心主入口（信息架构壳层，四页签：候选池/观察池/已排除/扫描记录）
import OpportunityCenter from "./components/OpportunityCenter";
import MacroData from "./components/MacroData";
import MarketNews from "./components/MarketNews";  // 行情消息
import Settings from "./components/Settings";
import DetailModal from "./components/DetailModal";
import MetricModal from "./components/MetricModal";
// WP-AI.7：AI 助手前端入口（浮动按钮 + 抽屉）
import { AIAssistantProvider } from "./components/ai/AIAssistantContext";
import AIAssistant from "./components/ai/AIAssistant";
// WP0.4：旧路由与 activeTab 兼容映射
import { resolveTabFromUrl } from "./utils/tabCompatibility";
// 组合交易主壳（Task 3）：替代旧「工作台/模拟交易」双 sub-tab
import PortfolioTradingShell from "./components/portfolio-trading/PortfolioTradingShell";
// 顶部栏右侧通知/帮助下拉（Task 12）
import NotificationDropdown from "./components/portfolio-trading/NotificationDropdown";
import HelpDropdown from "./components/portfolio-trading/HelpDropdown";

export default function App() {
  const ctx = useApp();
  const [detailModalOpen, setDetailModalOpen] = useState(false);
  const [metricModalType, setMetricModalType] = useState<string | null>(null);
  // 记录已处理过的 detailFocusRequest 编号，避免切换标签回来时重复弹窗
  const lastHandledFocusRef = useRef(0);
  // P1-09：机会挖掘 → 机会中心 兼容跳转迁移提示标记
  // 仅在用户点击旧"机会挖掘"入口时置 true，渲染迁移 Alert；关闭或直接进入机会中心时清除
  const [showDiscoveryMigration, setShowDiscoveryMigration] = useState(false);

  // SubTask 12.5：通知/帮助互斥状态（开一个关另一个，通过 forceClose + onOpenChange 协作）
  const [notificationOpen, setNotificationOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);

  // WP0.4：应用启动时读取 URL 中的 ?tab=... 并解析为有效 tab（旧深链接兼容）
  // 仅在挂载时执行一次，不修改用户后续手动切换 tab 的行为
  useEffect(() => {
    const resolved = resolveTabFromUrl();
    if (resolved && resolved !== ctx.activeTab) {
      ctx.setActiveTab(resolved);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Portfolio and discovery use the modal; investment center renders detail inline.
  // WP1.1：opportunity tab 中的候选池复用 Discovery，同样需要触发详情弹窗
  useEffect(() => {
    if (
      ctx.activeSymbolId
      && ctx.detail
      && ctx.detailFocusRequest > 0
      && ctx.detailFocusRequest !== lastHandledFocusRef.current
      && ["portfolio", "opportunity"].includes(ctx.activeTab)
    ) {
      lastHandledFocusRef.current = ctx.detailFocusRequest;
      setDetailModalOpen(true);
    }
  }, [ctx.activeSymbolId, ctx.detail, ctx.detailFocusRequest, ctx.activeTab]);

  const closeDetailModal = useCallback(() => {
    setDetailModalOpen(false);
  }, []);

  const closeMetricModal = useCallback(() => {
    setMetricModalType(null);
  }, []);

  const openMetricModal = useCallback((type: string) => {
    setMetricModalType(type);
  }, []);

  return (
    <AIAssistantProvider>
    <div className="shell" data-active-tab={ctx.activeTab}>
      {ctx.globalLoading && (
        <div
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            zIndex: 1000,
            background: "linear-gradient(90deg, #0f766e 0%, #0d9488 100%)",
            color: "#fff",
            padding: "5px 12px",
            textAlign: "center",
            fontSize: 12,
            fontWeight: 500,
            pointerEvents: "none",
            boxShadow: "0 2px 8px rgba(0,0,0,0.12)",
          }}
        >
          <Spin size="small" style={{ marginRight: 8 }} className="global-loading-spin" />
          {t("loading")}
        </div>
      )}

      {/* 顶部全局导航栏：左侧一级 Tab 导航（结构不动）+ 右侧通知/帮助（SubTask 12.5 新增）
          外层 sticky 容器接管原 .view-tabs 的 sticky 行为，使整个顶部栏（含右侧）固定置顶。
          .pt-theme-scope 包裹右侧通知/帮助，使 --pt-* 变量在该容器内生效（变量定义在组合交易 Tab scoped 主题内）。 */}
      <div className="app-global-nav" style={{ position: "sticky", top: 0, zIndex: 50, display: "flex", alignItems: "flex-start", gap: 12 }}>
        <nav className="view-tabs" aria-label={t("ariaMainViews")} style={{ flex: 1, minWidth: 0, position: "static" }}>
          <button
            className={`view-tab${ctx.activeTab === "decision" ? " active" : ""}`}
            onClick={() => ctx.setActiveTab("decision")}
          >
            {t("tabTodayDecision")}
          </button>

          {/* WP9.1：旧"投资中心"一级入口已移除，统一由"机会中心/标的研究"承接。
              ?tab=investment 深链接仍可通过 tabCompatibility 兼容路由访问（见下方渲染块）。 */}
          <button
            className={`view-tab${ctx.activeTab === "portfolio" ? " active" : ""}`}
            onClick={() => ctx.setActiveTab("portfolio")}
          >
            {t("tabPortfolio")}
          </button>
          {/* WP1.1：新增"机会中心"一级入口（信息架构壳层：候选池/观察池/已排除/扫描记录） */}
          <button
            className={`view-tab${ctx.activeTab === "opportunity" ? " active" : ""}`}
            onClick={() => {
              ctx.setActiveTab("opportunity");
              // P1-09：直接进入机会中心时清除迁移提示（仅旧入口跳转时显示）
              setShowDiscoveryMigration(false);
            }}
          >
            {t("tabOpportunity")}
          </button>
          {/* P1-09：旧"机会挖掘"入口已移除（2026-07-24）。
              功能已统一到"机会中心"，保留旧入口会造成用户困扰。
              旧深链接 ?tab=discovery 仍通过 tabCompatibility 兼容路由到 opportunity。 */}
          <button
            className={`view-tab${ctx.activeTab === "macro" ? " active" : ""}`}
            onClick={() => ctx.setActiveTab("macro")}
          >
            {t("tabMacro")}
          </button>
          <button
            className={`view-tab${ctx.activeTab === "news" ? " active" : ""}`}
            onClick={() => ctx.setActiveTab("news")}
          >
            {t("tabMarketNews")}
          </button>
          <button
            className={`view-tab${ctx.activeTab === "settings" ? " active" : ""}`}
            onClick={() => ctx.setActiveTab("settings")}
          >
            {t("tabSettings")}
          </button>
        </nav>

        {/* SubTask 12.5：顶部栏右侧通知/帮助（全局功能，所有 Tab 下均显示）
            互斥逻辑：打开一个时主动关闭另一个，避免 forceClose 持续 true 阻止打开 */}
        <div className="pt-theme-scope global-nav-tools" style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0, marginTop: 6 }}>
          <NotificationDropdown
            forceClose={helpOpen}
            onViewAll={() => {
              window.localStorage.setItem("settings_active_section", "notifications");
              window.localStorage.setItem("settings_notification_subtab", "deliveries");
              ctx.setActiveTab("settings");
            }}
            onOpenChange={(open) => {
              setNotificationOpen(open);
              if (open) setHelpOpen(false);
            }}
          />
          <HelpDropdown
            forceClose={notificationOpen}
            onOpenChange={(open) => {
              setHelpOpen(open);
              if (open) setNotificationOpen(false);
            }}
          />
        </div>
      </div>

      <main className="layout">
        {ctx.activeTab === "decision" && <TodayDecision />}

        {/* WP9.1：投资中心一级入口已从导航移除，此处仅保留兼容渲染。
            用户直接访问 ?tab=investment 时，tabCompatibility.resolveLegacyTab 仍解析为 "investment"，
            渲染 InvestmentCenter 薄壳（= SymbolResearchShell），即"标的研究"视图。 */}
        {ctx.activeTab === "investment" && (
          <div className="tab-container" data-tab-content="investment-legacy">
            <p
              className="panel-meta"
              style={{ margin: "8px 0", padding: "4px 12px", color: "#b7791f", background: "#fffbeb", borderRadius: 4, fontSize: 12 }}
              role="note"
            >
              {t("wp9.legacyEntryRemoved")}
            </p>
            <InvestmentCenter openMetricModal={openMetricModal} />
          </div>
        )}

        {/* 组合交易一级 Tab：直接渲染 PortfolioTradingShell（Task 3 主壳）
            移除旧「工作台/模拟交易」双 sub-tab 中间层 + 内联组合管理工具栏 + 内联新建组合 Modal。
            新建组合/组合切换/组合编辑均由 PortfolioTradingShell 内部浮层管理。
            PortfolioWorkbench.tsx / Trading.tsx 文件保留但不再从此路径渲染。 */}
        {ctx.activeTab === "portfolio" && <PortfolioTradingShell />}

        {/* WP1.1：机会中心主入口（信息架构壳层） */}
        {/* P1-09：旧"机会挖掘"入口已移除，?tab=discovery 深链接通过 tabCompatibility 路由到 opportunity。
            候选池统一由 OpportunityCenter 承接，Discovery 组件在 CandidatePool 中渲染。 */}
        {ctx.activeTab === "opportunity" && (
          <div className="tab-container" data-tab-content="opportunity">
            {showDiscoveryMigration && (
              <Alert
                type="info"
                showIcon
                message={t("discoveryMigrationTitle")}
                description={t("discoveryMigrationDesc")}
                closable
                onClose={() => setShowDiscoveryMigration(false)}
                style={{ margin: "8px 0" }}
              />
            )}
            <OpportunityCenter />
          </div>
        )}

        {ctx.activeTab === "macro" && <MacroData />}

        {ctx.activeTab === "news" && <MarketNews />}

        {ctx.activeTab === "settings" && <Settings />}
      </main>

      {detailModalOpen && ctx.detail && (
        <DetailModal open={detailModalOpen} onClose={closeDetailModal} />
      )}

      {metricModalType && (
        <MetricModal type={metricModalType} onClose={closeMetricModal} />
      )}

      {/* WP-AI.7：全局 AI 助手浮动按钮 + 抽屉（跳转到设置页的 AI 配置区） */}
      <AIAssistant
        activeTab={ctx.activeTab}
        onGoToSettings={() => {
          ctx.setActiveTab("settings");
          setTimeout(() => {
            const el = document.querySelector('[data-settings-content="settings-ai"]');
            if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
          }, 50);
        }}
      />
    </div>
    </AIAssistantProvider>
  );
}
