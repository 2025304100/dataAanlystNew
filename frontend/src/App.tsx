import React, { useState, useCallback, useRef, useEffect } from "react";
import { Button, Input, Select, Spin, Modal, Form, InputNumber, Switch, Dropdown, Tag, Popconfirm } from "antd";
import type { InputRef } from "antd";
import { useApp } from "./context/AppContext";
import { t, template } from "./i18n";
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

export default function App() {
  const ctx = useApp();
  const [symbolCode, setSymbolCode] = useState("");
  const [busyButton, setBusyButton] = useState<string | null>(null);
  const [detailModalOpen, setDetailModalOpen] = useState(false);
  const [metricModalType, setMetricModalType] = useState<string | null>(null);
  const symbolCodeRef = useRef<InputRef>(null);
  // 记录已处理过的 detailFocusRequest 编号，避免切换标签回来时重复弹窗
  const lastHandledFocusRef = useRef(0);

  // P0-7：组合管理 Modal 状态
  const [portfolioModalOpen, setPortfolioModalOpen] = useState(false);
  const [portfolioModalMode, setPortfolioModalMode] = useState<"create" | "edit">("create");
  const [portfolioEditing, setPortfolioEditing] = useState<number | null>(null);
  const [portfolioForm] = Form.useForm();
  const [portfolioSaving, setPortfolioSaving] = useState(false);

  // 打开新建组合 Modal
  const openCreatePortfolioModal = useCallback(() => {
    setPortfolioModalMode("create");
    setPortfolioEditing(null);
    portfolioForm.resetFields();
    portfolioForm.setFieldsValue({
      account_type: "simulated",
      total_capital: 100000,
      investable_ratio: 0.9,
      cash_reserve_ratio: 0.1,
      currency: "CNY",
      is_default: false,
      auto_trade_enabled: false,
    });
    setPortfolioModalOpen(true);
  }, [portfolioForm]);

  // 打开编辑组合 Modal
  const openEditPortfolioModal = useCallback((portfolioId: number) => {
    const target = ctx.portfolios.find((p) => p.id === portfolioId);
    if (!target) return;
    setPortfolioModalMode("edit");
    setPortfolioEditing(portfolioId);
    portfolioForm.setFieldsValue({
      name: target.name,
      total_capital: target.total_capital,
      investable_ratio: target.investable_ratio,
      cash_reserve_ratio: target.cash_reserve_ratio,
      currency: target.currency,
      is_default: Number(target.is_default) === 1,
      auto_trade_enabled: Number(target.auto_trade_enabled) === 1,
    });
    setPortfolioModalOpen(true);
  }, [ctx.portfolios, portfolioForm]);

  // 提交组合表单（新建/编辑共用）
  const handlePortfolioSubmit = useCallback(async () => {
    try {
      const values = await portfolioForm.validateFields();
      setPortfolioSaving(true);
      if (portfolioModalMode === "create") {
        await ctx.createPortfolio(values);
      } else if (portfolioEditing !== null) {
        await ctx.updatePortfolio(portfolioEditing, values);
      }
      setPortfolioModalOpen(false);
    } catch (error: any) {
      // validateFields 失败时不关闭 Modal（表单校验提示）
      if (error?.errorFields) return;
      // 其他错误已由 context showToast 处理
    } finally {
      setPortfolioSaving(false);
    }
  }, [portfolioForm, portfolioModalMode, portfolioEditing, ctx]);

  // 删除组合
  const handleDeletePortfolio = useCallback(async (portfolioId: number) => {
    await ctx.deletePortfolio(portfolioId);
  }, [ctx]);

  // 切换组合（替代原 window.location.reload()）
  const handleSwitchPortfolio = useCallback((id: number) => {
    if (id && id !== ctx.portfolioId) {
      ctx.switchPortfolio(id).catch(() => {
        // 错误已由 context 处理
      });
    }
  }, [ctx]);

  // Portfolio and discovery use the modal; investment center renders detail inline.
  useEffect(() => {
    if (
      ctx.activeSymbolId
      && ctx.detail
      && ctx.detailFocusRequest > 0
      && ctx.detailFocusRequest !== lastHandledFocusRef.current
      && ["portfolio", "discovery"].includes(ctx.activeTab)
    ) {
      lastHandledFocusRef.current = ctx.detailFocusRequest;
      setDetailModalOpen(true);
    }
  }, [ctx.activeSymbolId, ctx.detail, ctx.detailFocusRequest, ctx.activeTab]);

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

      <nav className="view-tabs" aria-label={t("ariaMainViews")}>
        <button
          className={`view-tab${ctx.activeTab === "decision" ? " active" : ""}`}
          onClick={() => ctx.setActiveTab("decision")}
        >
          {t("tabTodayDecision")}
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
                  {/* P0-7：组合切换器 + 管理按钮（替代原 window.location.reload 暴力刷新） */}
                  <label className="portfolio-picker">
                    <span>{t("portfolio")}</span>
                    <Select
                      value={ctx.portfolioId}
                      onChange={handleSwitchPortfolio}
                      style={{ width: 160 }}
                      options={ctx.portfolios.map(p => ({
                        label: (
                          <span>
                            {p.name}
                            {Number(p.is_default) === 1 && (
                              <Tag color="gold" style={{ marginLeft: 6, fontSize: 11 }}>默认</Tag>
                            )}
                            {p.account_type === "simulated" && (
                              <Tag color="blue" style={{ marginLeft: 4, fontSize: 11 }}>模拟</Tag>
                            )}
                          </span>
                        ) as unknown as string,
                        value: p.id,
                      }))}
                    />
                  </label>
                  <Button
                    size="small"
                    onClick={openCreatePortfolioModal}
                    title={t("portfolioNew")}
                  >
                    + {t("portfolioNew")}
                  </Button>
                  {ctx.portfolioId && (
                    <Dropdown
                      menu={{
                        items: [
                          { key: "edit", label: t("portfolioEdit"), onClick: () => openEditPortfolioModal(ctx.portfolioId!) },
                          { key: "delete", label: t("portfolioDelete"), danger: true, onClick: () => {
                            const current = ctx.portfolios.find(p => p.id === ctx.portfolioId);
                            if (current && Number(current.is_default) === 1) {
                              Modal.warning({ title: t("portfolioDelete"), content: t("portfolioDeleteDefaultDenied") });
                              return;
                            }
                            Modal.confirm({
                              title: t("portfolioDelete"),
                              content: t("portfolioDeleteConfirm"),
                              okType: "danger",
                              onOk: () => handleDeletePortfolio(ctx.portfolioId!),
                            });
                          }},
                        ],
                      }}
                    >
                      <Button size="small">⚙</Button>
                    </Dropdown>
                  )}
                  <label className="portfolio-picker">
                    <span>{t("language")}</span>
                    <Select
                      value={ctx.locale}
                      onChange={(v) => ctx.setLocaleValue(v)}
                      style={{ width: 120 }}
                      options={[{label:t("langZhCN"),value:"zh-CN"},{label:t("langEnUS"),value:"en-US"}]}
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
                  <Button onClick={handleSync} loading={busyButton === "sync" || ctx.syncPolling}>
                    {ctx.syncPolling && ctx.syncTask
                      ? template("syncProgress", { current: ctx.syncTask.processed ?? 0, total: ctx.syncTask.total ?? 0 })
                      : busyButton === "sync" ? t("syncing") : t("sync")}
                  </Button>
                  {ctx.syncPolling && ctx.syncTask && (
                    <Button onClick={() => ctx.cancelSync()}>
                      {t("cancel")}
                    </Button>
                  )}
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

            <nav className="sub-tabs" aria-label={t("ariaPortfolioSubViews")}>
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

      {/* P0-7：组合管理 Modal（新建/编辑共用） */}
      <Modal
        open={portfolioModalOpen}
        title={portfolioModalMode === "create" ? t("portfolioNew") : t("portfolioEdit")}
        onCancel={() => setPortfolioModalOpen(false)}
        onOk={handlePortfolioSubmit}
        confirmLoading={portfolioSaving}
        okText={portfolioModalMode === "create" ? t("portfolioNew") : t("portfolioEdit")}
        cancelText={t("cancel")}
        destroyOnClose
        maskClosable={false}
      >
        <Form form={portfolioForm} layout="vertical" preserve={false}>
          <Form.Item
            name="name"
            label={t("portfolioName")}
            rules={[{ required: true, message: t("portfolioNameRequired") }]}
          >
            <Input placeholder={t("portfolioName")} maxLength={64} />
          </Form.Item>

          {portfolioModalMode === "create" && (
            <Form.Item
              name="account_type"
              label={t("portfolioAccountType")}
              tooltip={t("portfolioAccountTypeHelp")}
              rules={[{ required: true }]}
            >
              <Select
                options={[
                  { label: t("portfolioAccountSimulated"), value: "simulated" },
                  { label: t("portfolioAccountManual"), value: "manual" },
                ]}
              />
            </Form.Item>
          )}

          <Form.Item
            name="total_capital"
            label={t("portfolioCapital")}
            tooltip={t("portfolioCapitalHelp")}
            rules={[{ required: true, message: t("portfolioCapitalRequired") }]}
          >
            <InputNumber
              style={{ width: "100%" }}
              min={1}
              step={10000}
              precision={2}
            />
          </Form.Item>

          <Form.Item
            name="investable_ratio"
            label={t("portfolioInvestableRatio")}
            tooltip={t("portfolioInvestableRatioHelp")}
            rules={[{ required: true }]}
          >
            <InputNumber min={0} max={1} step={0.05} precision={2} style={{ width: "100%" }} />
          </Form.Item>

          <Form.Item
            name="cash_reserve_ratio"
            label={t("portfolioCashReserveRatio")}
            tooltip={t("portfolioCashReserveRatioHelp")}
            rules={[{ required: true }]}
          >
            <InputNumber min={0} max={1} step={0.05} precision={2} style={{ width: "100%" }} />
          </Form.Item>

          <Form.Item
            name="currency"
            label={t("portfolioCurrency")}
            tooltip={t("portfolioCurrencyHelp")}
          >
            <Select
              options={[
                { label: "CNY", value: "CNY" },
                { label: "USD", value: "USD" },
              ]}
            />
          </Form.Item>

          <Form.Item
            name="is_default"
            label={t("portfolioIsDefault")}
            tooltip={t("portfolioIsDefaultHelp")}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          {/* P2-3：自动交易开关，仅模拟账户显示 */}
          <Form.Item
            noStyle
            shouldUpdate={(prev, next) => prev.account_type !== next.account_type}
          >
            {() => {
              const formAccountType = portfolioForm.getFieldValue("account_type");
              // edit 模式下 account_type 字段未渲染，从 ctx.portfolios 兜底
              const editAccountType =
                portfolioEditing !== null
                  ? ctx.portfolios.find((p) => p.id === portfolioEditing)?.account_type
                  : null;
              const accountType = formAccountType || editAccountType;
              if (accountType !== "simulated") return null;
              return (
                <Form.Item
                  name="auto_trade_enabled"
                  label={t("autoTradeEnabled")}
                  valuePropName="checked"
                  tooltip={t("autoTradeEnabledHelp")}
                >
                  <Switch />
                </Form.Item>
              );
            }}
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
