import React, { useState, useCallback, useRef, useEffect } from "react";
import { Button, Input, Select } from "antd";
import type { InputRef } from "antd";
import { useApp } from "./context/AppContext";
import { useActiveRequests } from "./hooks/useActiveRequests";
import { t, template } from "./i18n";
import { regionLongLabel } from "./i18n";
import PortfolioWorkbench from "./components/PortfolioWorkbench";
import Trading from "./components/Trading";
import InvestmentCenter from "./components/InvestmentCenter";
import TodayDecision from "./components/TodayDecision";
import Discovery from "./components/Discovery";
import MacroData from "./components/MacroData";
import MarketNews from "./components/MarketNews";  // 行情消息
import Settings from "./components/Settings";
import DetailModal from "./components/DetailModal";
import MetricModal from "./components/MetricModal";

const DOT = " | ";

export default function App() {
  const ctx = useApp();
  const activeRequests = useActiveRequests();
  const [symbolCode, setSymbolCode] = useState("");
  const [busyButton, setBusyButton] = useState<string | null>(null);
  const [detailModalOpen, setDetailModalOpen] = useState(false);
  const [metricModalType, setMetricModalType] = useState<string | null>(null);
  const symbolCodeRef = useRef<InputRef>(null);

  // Open detail modal when activeSymbolId changes (only in portfolio tab, not in investment center where detail is inline)
  useEffect(() => {
    if (ctx.activeSymbolId && ctx.detail && ctx.activeTab === "portfolio") {
      setDetailModalOpen(true);
    }
  }, [ctx.activeSymbolId, ctx.detail, ctx.activeTab]);

  const handleAddSymbol = useCallback(async () => {
    if (!symbolCode.trim()) return;
    setBusyButton("add");
    try {
      await ctx.addSymbolFromInput(symbolCode.trim());
      setSymbolCode("");
    } catch (error: any) {
      ctx.showToast("error", `${t("symbolAddFailed")}: ${error.message}`);
    } finally {
      setBusyButton(null);
    }
  }, [symbolCode, ctx]);

  const handleSync = useCallback(async () => {
    setBusyButton("sync");
    try {
      await ctx.runSync();
    } catch (error: any) {
      ctx.showToast("error", `${t("syncFailed")}: ${error.message}`);
    } finally {
      setBusyButton(null);
    }
  }, [ctx]);

  const handleScan = useCallback(async () => {
    setBusyButton("scan");
    try {
      await ctx.runScan();
    } catch (error: any) {
      ctx.showToast("error", `${t("scanFailed")}: ${error.message}`);
    } finally {
      setBusyButton(null);
    }
  }, [ctx]);

  const handleNews = useCallback(async () => {
    setBusyButton("news");
    try {
      await ctx.runNewsUpdate();
    } catch (error: any) {
      ctx.showToast("error", `${t("newsFailed")}: ${error.message}`);
    } finally {
      setBusyButton(null);
    }
  }, [ctx]);

  const handleRefresh = useCallback(async () => {
    setBusyButton("refresh");
    try {
      await ctx.loadWorkbench();
    } catch (error: any) {
      ctx.showToast("error", error.message);
    } finally {
      setBusyButton(null);
    }
  }, [ctx]);

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
    <div className="shell" data-active-tab={ctx.activeTab}>
      {activeRequests > 0 && <div className="request-indicator" />}

      <nav className="view-tabs" aria-label="Main views">
        <button
          className={`view-tab${ctx.activeTab === "decision" ? " active" : ""}`}
          onClick={() => ctx.setActiveTab("decision")}
        >
          {ctx.locale === "en-US" ? "Today Decision" : "\u4eca\u65e5\u51b3\u7b56"}
        </button>

        {/* ✨ 新增：投资中心导航（放在第一位） */}
        <button
          className={`view-tab${ctx.activeTab === "investment" ? " active" : ""}`}
          onClick={() => ctx.setActiveTab("investment")}
        >
          {t("tabInvestmentCenter")}
        </button>

        {/* 原有导航保持不变 */}
        <button
          className={`view-tab${ctx.activeTab === "portfolio" ? " active" : ""}`}
          onClick={() => ctx.setActiveTab("portfolio")}
        >
          {t("tabPortfolio")}
        </button>
        <button
          className={`view-tab${ctx.activeTab === "discovery" ? " active" : ""}`}
          onClick={() => ctx.setActiveTab("discovery")}
        >
          {t("tabDiscovery")}
        </button>
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

      <main className="layout">
        {ctx.activeTab === "decision" && <TodayDecision />}

        {/* ✨ 新增：投资中心整合视图 */}
        {ctx.activeTab === "investment" && (
          <InvestmentCenter openMetricModal={openMetricModal} />
        )}

        {/* 原有逻辑完全保留 */}
        {ctx.activeTab === "portfolio" && (
          <div className="tab-container" data-tab-content="portfolio">
            <header className="topbar">
              <div className="toolbar">
                <div className="toolbar-fields">
                  <label className="portfolio-picker">
                    <span>{t("portfolio")}</span>
                    <Select
                      value={ctx.portfolioId}
                      onChange={(id) => { if(id) window.location.reload(); }}
                      style={{ width: 140 }}
                      options={ctx.portfolios.map(p => ({ label: p.name, value: p.id }))}
                    />
                  </label>
                  <label className="portfolio-picker">
                    <span>{t("language")}</span>
                    <Select
                      value={ctx.locale}
                      onChange={(v) => ctx.setLocaleValue(v)}
                      style={{ width: 120 }}
                      options={[{label:"简体中文",value:"zh-CN"},{label:"English",value:"en-US"}]}
                    />
                  </label>
                  <label className="portfolio-picker">
                    <span>{t("market")}</span>
                    <Select
                      value={ctx.marketGroup}
                      onChange={(v) => { ctx.setMarketGroup(v); ctx.loadWorkbench(); }}
                      style={{ width: 100 }}
                      options={[{label:t("all"),value:"all"},{label:t("chinaMainland"),value:"cn"},{label:t("unitedStates"),value:"us"}]}
                    />
                  </label>
                  <label className="portfolio-picker symbol-add">
                    <span>{t("symbolCode")}</span>
                    <Input
                      ref={symbolCodeRef}
                      placeholder="600519 / QQQ"
                      value={symbolCode}
                      onChange={(e) => setSymbolCode(e.target.value)}
                      onPressEnter={handleAddSymbol}
                      style={{ width: 150 }}
                    />
                  </label>
                </div>
                <div className="toolbar-actions">
                  <Button onClick={handleAddSymbol} loading={busyButton === "add"}>
                    {busyButton === "add" ? t("addingSymbol") : t("addSymbol")}
                  </Button>
                  <Button onClick={handleSync} loading={busyButton === "sync"}>
                    {busyButton === "sync" ? t("syncing") : t("sync")}
                  </Button>
                  <Button onClick={handleScan} loading={busyButton === "scan"}>
                    {busyButton === "scan" ? t("scanning") : t("scan")}
                  </Button>
                  <Button onClick={handleNews} loading={busyButton === "news"}>
                    {busyButton === "news" ? t("newsUpdating") : t("newsUpdate")}
                  </Button>
                  <Button onClick={handleRefresh} loading={busyButton === "refresh"}>
                    {t("refresh")}
                  </Button>
                </div>
              </div>
            </header>

            <nav className="sub-tabs" aria-label="Portfolio sub-views">
              <button
                className={`sub-tab${ctx.activeSubTab === "portfolio-workbench" ? " active" : ""}`}
                onClick={() => ctx.setActiveSubTab("portfolio-workbench")}
              >
                {t("tabWorkbench")}
              </button>
              <button
                className={`sub-tab${ctx.activeSubTab === "portfolio-trading" ? " active" : ""}`}
                onClick={() => ctx.setActiveSubTab("portfolio-trading")}
              >
                {t("tabTrading")}
              </button>
            </nav>

            {ctx.activeSubTab === "portfolio-workbench" && (
              <PortfolioWorkbench openMetricModal={openMetricModal} />
            )}
            {ctx.activeSubTab === "portfolio-trading" && <Trading />}
          </div>
        )}

        {ctx.activeTab === "discovery" && <Discovery />}

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
    </div>
  );
}
