import { useState, useEffect } from "react";
import { useApp } from "../context/AppContext";
import { t, DOT } from "../i18n";
import SettingsNav from "./settings/SettingsNav";
import UnsupportedFeaturesToggle from "./settings/UnsupportedFeaturesToggle";
import { statPct, pnlClass, clamp } from "../utils/format";
import { Input, InputNumber, Checkbox, Button, Space, Card, Form } from "antd";
import { CalendarOutlined, DatabaseOutlined, ExperimentOutlined, FunctionOutlined, SettingOutlined, SyncOutlined, MedicineBoxOutlined, UnorderedListOutlined, BellOutlined, TrophyOutlined, ApiOutlined, RobotOutlined, NotificationOutlined, AppstoreOutlined } from "@ant-design/icons";
import DbConfigSection from "./DbConfigSection";
import CustomIndicatorSettings from "./CustomIndicatorSettings";
import DiscoveryPlanSettings from "./DiscoveryPlanSettings";
import HistoryInitSection from "./HistoryInitSection";
import DataDiagnosticPanel from "./DataDiagnosticPanel";
import TaskCenter from "./TaskCenter";
import ScoringConfigSettings from "./ScoringConfigSettings";
import AkshareApiManager from "./AkshareApiManager";
import DataCenter from "./DataCenter";
import FactorModelSettings from "./FactorModelSettings";
import FactorCenter from "./factors/FactorCenter";
import MiningShell from "./factors/mining/MiningShell";
import ScheduledTaskManager from "./ScheduledTaskManager";
import AiConfigSection from "./AiConfigSection";
import AISettings from "./ai/AISettings";
import FactorBasicSettings from "./settings/FactorBasicSettings";
import AppSettingsSection from "./settings/AppSettingsSection";
import MessageCenter from "./messageCenter/MessageCenter";
import { backtestMissingLabels, clearBacktestRepair, readBacktestRepair } from "../utils/backtestDraft";

export default function Settings() {
  const ctx = useApp();
  const rule = ctx.signalRule;
  const presets = ctx.signalRulePresets;
  const preview = ctx.signalRulePreview;
  const activeSymbolId = ctx.activeSymbolId;
  const [saving, setSaving] = useState(false);
  const [activeSection, setActiveSection] = useState<"rules" | "indicators" | "history" | "diagnostic" | "tasks" | "schedules" | "message-center" | "scoring" | "factor-model" | "factor-center" | "data-center" | "api-mgmt" | "db" | "ai" | "factor-mining" | "factor-basic" | "app-settings">(() => {
    if (typeof window === "undefined") return "rules";
    const stored = window.localStorage.getItem("settings_active_section");
    // 「告警中心」与「消息管理」已合并为「消息中心」，旧持久化值继续可达。
    if (stored === "alerts" || stored === "notifications") return "message-center";
    if (stored === "external" || stored === "universe") return "data-center";
    return stored === "rules" || stored === "indicators" || stored === "history" || stored === "diagnostic" || stored === "tasks" || stored === "schedules" || stored === "message-center" || stored === "scoring" || stored === "factor-model" || stored === "factor-center" || stored === "data-center" || stored === "api-mgmt" || stored === "db" || stored === "ai" || stored === "factor-mining" || stored === "factor-basic" || stored === "app-settings" ? stored : "rules";
  });
  const [messageCenterTab, setMessageCenterTab] = useState<"alerts" | "inbox" | "delivery">(() => {
    if (typeof window === "undefined") return "alerts";
    const stored = window.localStorage.getItem("settings_message_center_tab");
    return stored === "alerts" || stored === "inbox" || stored === "delivery" ? stored : "alerts";
  });
  const [activeIndicatorTab, setActiveIndicatorTab] = useState<"formulas" | "plans">(() => {
    if (typeof window === "undefined") return "formulas";
    const stored = window.localStorage.getItem("settings_indicator_subtab");
    return stored === "formulas" || stored === "plans" ? stored : "formulas";
  });
  const [activeNotificationTab, setActiveNotificationTab] = useState<"channels" | "policies" | "templates" | "deliveries">(() => {
    if (typeof window === "undefined") return "channels";
    const stored = window.localStorage.getItem("settings_notification_subtab");
    return stored === "channels" || stored === "policies" || stored === "templates" || stored === "deliveries" ? stored : "channels";
  });
  const [activeAiTab, setActiveAiTab] = useState<"config" | "profiles">(() => {
    if (typeof window === "undefined") return "config";
    const stored = window.localStorage.getItem("settings_ai_subtab");
    return stored === "config" || stored === "profiles" ? stored : "config";
  });
  const [historyFocusSignal, setHistoryFocusSignal] = useState(0);
  const [diagnosticSymbolId, setDiagnosticSymbolId] = useState<number | null>(null);
  const [historyInitContext, setHistoryInitContext] = useState<{ symbolId?: number | null; symbolLabel?: string | null; repairMode?: "both" | "bars" | "scores" } | null>(null);
  const [backtestRepair, setBacktestRepair] = useState(readBacktestRepair);
  const [backtestReturnError, setBacktestReturnError] = useState("");
  const historyRepair = backtestRepair?.target === "history" ? backtestRepair : null;

  useEffect(() => {
    if (rule && activeSymbolId) {
      ctx.loadSignalRulePreview().catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSymbolId]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem("settings_active_section", activeSection);
    }
  }, [activeSection]);

  useEffect(() => {
    const handleSettingsNavigate = (event: Event) => {
      // detail 兼容字符串（旧调用方）与 { section, subtab }（消息通知「查看全部」用）
      const detail = (event as CustomEvent<string | { section: string; subtab?: string }>).detail;
      const raw = typeof detail === "string" ? detail : detail?.section;
      const subtab = typeof detail === "object" && detail ? detail.subtab : undefined;
      const section = raw === "api-management" ? "api-mgmt" : raw;
      // 「告警中心」「消息管理」两个入口已合并进「消息中心」：旧调用方
      // （铃铛「查看全部」传 notifications+deliveries）继续可用，只是落到新壳层。
      const legacyTab = section === "alerts" ? "alerts" : section === "notifications" ? "delivery" : null;
      const targetSection = legacyTab ? "message-center" : section;
      if (["factor-model", "factor-center", "factor-mining", "data-center", "history", "api-mgmt", "tasks", "message-center"].includes(targetSection)) {
        setActiveSection(targetSection as typeof activeSection);
        setBacktestRepair(readBacktestRepair());
        if (legacyTab) {
          setMessageCenterTab(legacyTab);
        }
        if (subtab === "deliveries") {
          setActiveNotificationTab("deliveries");
        }
      }
    };
    window.addEventListener("settings:navigate", handleSettingsNavigate);
    return () => window.removeEventListener("settings:navigate", handleSettingsNavigate);
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem("settings_indicator_subtab", activeIndicatorTab);
    }
  }, [activeIndicatorTab]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem("settings_message_center_tab", messageCenterTab);
    }
  }, [messageCenterTab]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem("settings_notification_subtab", activeNotificationTab);
    }
  }, [activeNotificationTab]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem("settings_ai_subtab", activeAiTab);
    }
  }, [activeAiTab]);

  const applyPreset = (mode: string) => {
    const preset = presets.find((p) => p.mode === mode);
    if (!preset) return;
    ctx.updateSignalRule({
      rule_name: preset.rule_name,
      mode: preset.mode,
      quality_tolerance: preset.quality_tolerance,
      timing_tolerance: preset.timing_tolerance,
      min_sample_count: preset.min_sample_count,
      max_samples: preset.max_samples,
      same_region: preset.same_region,
      same_asset_type: preset.same_asset_type,
      same_stage: preset.same_stage,
      same_action: preset.same_action,
    });
  };

  const updateNumberField = (
    key: "quality_tolerance" | "timing_tolerance" | "min_sample_count" | "max_samples",
    rawValue: number,
    min: number,
    max: number
  ) => {
    const value = clamp(rawValue, min, max);
    ctx.updateSignalRule({ [key]: value, mode: "expert", rule_name: t("expertMode") });
  };

  const updateBooleanField = (
    key: "same_region" | "same_asset_type" | "same_stage" | "same_action",
    value: boolean
  ) => {
    ctx.updateSignalRule({ [key]: value, mode: "expert", rule_name: t("expertMode") });
  };

  const handleSave = () => {
    setSaving(true);
    ctx
      .saveSignalRule()
      .catch((error: any) => {
        ctx.showToast("error", error.message);
      })
      .finally(() => setSaving(false));
  };
  const summary = rule ? `${rule.rule_name}${DOT}${rule.mode}` : "-";

  const openHistoryInitialization = (context?: { symbolId?: number | null; symbolLabel?: string | null; repairMode?: "both" | "bars" | "scores" }) => {
    clearBacktestRepair(); setBacktestRepair(null);
    setHistoryInitContext(context ?? null);
    setActiveSection("history");
    setHistoryFocusSignal((value) => value + 1);
  };

  const renderPreview = () => {
    if (!activeSymbolId) {
      return <div className="item-subline">{t("rulePreviewIdle")}</div>;
    }
    if (!preview) {
      return <div className="item-subline">{t("rulePreview")}...</div>;
    }
    if (!preview.stats) {
      return (
        <div className="modal-action-title">
          <strong>{preview.symbol ? `${preview.symbol}${DOT}${preview.name}` : t("rulePreview")}</strong>
          <span className="item-subline">{preview.message}</span>
        </div>
      );
    }
    const stats = preview.stats;
    const minSamples = stats.min_sample_count ?? stats.scope?.min_sample_count ?? "-";
    return (
      <div>
        <div className="modal-action-title">
          <strong>{t("rulePreview")}{DOT}{preview.symbol}{DOT}{preview.name}</strong>
          <span className="item-subline">{t("previewDiagnosis")}: {preview.message}</span>
        </div>
        <div className="rule-preview-grid">
          <div className="rule-preview-stat">
            <span className="metric-label">{t("previewSamples")}</span>
            <strong>{stats.sample_count ?? 0}/{minSamples}</strong>
          </div>
          <div className="rule-preview-stat">
            <span className="metric-label">{t("previewMatched")}</span>
            <strong>{stats.matched_count ?? 0}</strong>
          </div>
          <div className="rule-preview-stat">
            <span className="metric-label">{t("previewWin20d")}</span>
            <strong>{statPct(stats.win_rate_20d)}</strong>
          </div>
          <div className="rule-preview-stat">
            <span className="metric-label">{t("previewAvg20d")}</span>
            <strong className={pnlClass(stats.avg_return_20d)}>{statPct(stats.avg_return_20d)}</strong>
          </div>
          <div className="rule-preview-stat">
            <span className="metric-label">{t("previewDrawdown")}</span>
            <strong className={pnlClass(stats.avg_max_drawdown_20d)}>{statPct(stats.avg_max_drawdown_20d)}</strong>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="tab-container" data-tab-content="settings">
      <div className="settings-layout">
        <SettingsNav active={activeSection} onSelect={setActiveSection} />
        <div className="settings-content">
          <UnsupportedFeaturesToggle />
          {backtestRepair && (activeSection === "history" || activeSection === "data-center") && <section
            className="panel" role="region" aria-label="本次回测需要的资料" style={{ marginBottom: 16 }}
          >
            <h3>本次回测需要的资料</h3>
            <p>组合：{backtestRepair.portfolioName}；标的：{backtestRepair.symbols.length
              ? backtestRepair.symbols.map((symbol) => `${symbol.symbol}${symbol.name ? ` ${symbol.name}` : ""}`).join("、")
              : backtestRepair.missingTypes.includes("symbols") ? "当前还没有可回测标的，请先检查组合范围和行情评分资料。" : "按本次组合范围核对，返回后会重新检查标的。"}</p>
            <p>日期：{backtestRepair.startDate} 至 {backtestRepair.endDate}；缺少：{backtestRepair.missingTypes.map((key) => backtestMissingLabels[key]).join("、")}</p>
            <p>参数草稿已保留。返回后会重新检查资料，开始回测仍需手动点击。</p>
            {backtestReturnError && <p role="alert">{backtestReturnError}</p>}
            <Button onClick={() => {
              if (!ctx.portfolios.some((portfolio) => portfolio.id === backtestRepair.portfolioId)) {
                setBacktestReturnError("找不到原来的组合，请先确认组合是否还在。参数草稿仍保留在本机。"); return;
              }
              const query = new URLSearchParams(window.location.search);
              query.delete("bt_run"); query.set("tab", "portfolio"); query.set("subtab", "backtest");
              window.history.replaceState(window.history.state, "", `${window.location.pathname}?${query}${window.location.hash}`);
              if (ctx.portfolioId !== backtestRepair.portfolioId) void ctx.switchPortfolio(backtestRepair.portfolioId);
              clearBacktestRepair();
              ctx.setActiveSubTab("backtest"); ctx.setActiveTab("portfolio");
            }}>返回回测并检查资料</Button>
          </section>}
          {activeSection === "rules" && (
          <div className="settings-tab-container" data-settings-content="settings-rules">
            <section className="band rule-config-band">
              <div className="panel">
                <div className="panel-head">
                  <div>
                    <p className="panel-kicker">{t("signalRuleKicker")}</p>
                    <h2>{t("ruleConfig")}</h2>
                  </div>
                  <p className="panel-meta">{summary}</p>
                </div>
                <div className="rule-config-grid">
                  <div className="preset-column">
                    <div className="preset-list">
                      {presets.map((preset) => (
                        <Button
                          key={preset.mode}
                          type={rule?.mode === preset.mode ? "primary" : "default"}
                          className={`preset-button ${rule?.mode === preset.mode ? "active" : ""}`}
                          onClick={() => applyPreset(preset.mode)}
                        >
                          <span className="preset-title">{preset.rule_name}</span>
                        </Button>
                      ))}
                    </div>
                  </div>
                  <form
                    className="rule-form"
                    onSubmit={(e) => {
                      e.preventDefault();
                      handleSave();
                    }}
                  >
                    <div className="rule-form-grid">
                      <label>
                        <span>
                          <span>{t("qualityTolerance")}</span>
                          <button type="button" className="tip-icon" data-tip={t("qualityToleranceTip")}>?</button>
                        </span>
                        <InputNumber
                          min={1}
                          max={40}
                          step={1}
                          value={rule?.quality_tolerance ?? 0}
                          onChange={(value) => updateNumberField("quality_tolerance", value ?? 0, 1, 40)}
                        />
                      </label>
                      <label>
                        <span>
                          <span>{t("timingTolerance")}</span>
                          <button type="button" className="tip-icon" data-tip={t("timingToleranceTip")}>?</button>
                        </span>
                        <InputNumber
                          min={1}
                          max={40}
                          step={1}
                          value={rule?.timing_tolerance ?? 0}
                          onChange={(value) => updateNumberField("timing_tolerance", value ?? 0, 1, 40)}
                        />
                      </label>
                      <label>
                        <span>
                          <span>{t("minSamples")}</span>
                          <button type="button" className="tip-icon" data-tip={t("minSamplesTip")}>?</button>
                        </span>
                        <InputNumber
                          min={1}
                          max={50}
                          step={1}
                          value={rule?.min_sample_count ?? 0}
                          onChange={(value) => updateNumberField("min_sample_count", value ?? 0, 1, 50)}
                        />
                      </label>
                      <label>
                        <span>
                          <span>{t("maxSamples")}</span>
                          <button type="button" className="tip-icon" data-tip={t("maxSamplesTip")}>?</button>
                        </span>
                        <InputNumber
                          min={5}
                          max={240}
                          step={5}
                          value={rule?.max_samples ?? 0}
                          onChange={(value) => updateNumberField("max_samples", value ?? 0, 5, 240)}
                        />
                      </label>
                    </div>
                    <div className="rule-check-grid">
                      <label>
                        <Checkbox
                          checked={rule?.same_region ?? false}
                          onChange={(e) => updateBooleanField("same_region", e.target.checked)}
                        />
                        <span>{t("sameRegion")}</span>
                        <button type="button" className="tip-icon" data-tip={t("sameRegionTip")}>?</button>
                      </label>
                      <label>
                        <Checkbox
                          checked={rule?.same_asset_type ?? false}
                          onChange={(e) => updateBooleanField("same_asset_type", e.target.checked)}
                        />
                        <span>{t("sameAsset")}</span>
                        <button type="button" className="tip-icon" data-tip={t("sameAssetTip")}>?</button>
                      </label>
                      <label>
                        <Checkbox
                          checked={rule?.same_stage ?? false}
                          onChange={(e) => updateBooleanField("same_stage", e.target.checked)}
                        />
                        <span>{t("sameStage")}</span>
                        <button type="button" className="tip-icon" data-tip={t("sameStageTip")}>?</button>
                      </label>
                      <label>
                        <Checkbox
                          checked={rule?.same_action ?? false}
                          onChange={(e) => updateBooleanField("same_action", e.target.checked)}
                        />
                        <span>{t("sameAction")}</span>
                        <button type="button" className="tip-icon" data-tip={t("sameActionTip")}>?</button>
                      </label>
                    </div>
                    <div className="detail-actions">
                      <Button type="primary" htmlType="submit" className="ghost-button detail-action" disabled={!rule || saving} loading={saving}>
                        {t("saveRule")}
                      </Button>
                    </div>
                    <div className={`rule-preview ${preview?.status || ""}`.trim()}>
                      {renderPreview()}
                    </div>
                  </form>
                </div>
              </div>
            </section>
          </div>
          )}
          {activeSection === "indicators" && (
            <div className="settings-tab-container" data-settings-content="settings-indicators">
              <section className="band">
                <div className="settings-indicator-stack">
                  <div className="sub-tabs" aria-label={t("tabIndicators")}>
                    <button
                      type="button"
                      className={`sub-tab ${activeIndicatorTab === "formulas" ? "active" : ""}`}
                      aria-current={activeIndicatorTab === "formulas" ? "page" : undefined}
                      onClick={() => setActiveIndicatorTab("formulas")}
                    >
                      {t("settingsIndicatorTabFormulas")}
                    </button>
                    <button
                      type="button"
                      className={`sub-tab ${activeIndicatorTab === "plans" ? "active" : ""}`}
                      aria-current={activeIndicatorTab === "plans" ? "page" : undefined}
                      onClick={() => setActiveIndicatorTab("plans")}
                    >
                      {t("settingsIndicatorTabPlans")}
                    </button>
                  </div>
                  <div className="sub-tab-container" hidden={activeIndicatorTab !== "formulas"}>
                    <CustomIndicatorSettings onOpenHistoryInit={openHistoryInitialization} />
                  </div>
                  <div className="sub-tab-container" hidden={activeIndicatorTab !== "plans"}>
                    <DiscoveryPlanSettings />
                  </div>
                </div>
              </section>
            </div>
          )}
          {activeSection === "history" && (
            <div className="settings-tab-container" data-settings-content="settings-history">
              <section className="band">
                <HistoryInitSection focusSignal={historyFocusSignal} context={historyRepair ? {
                  symbolIds: historyRepair.symbols.map((symbol) => symbol.id),
                  symbolId: historyRepair.symbols.length === 1 ? historyRepair.symbols[0].id : null,
                  symbolLabel: historyRepair.symbols.length === 1 ? historyRepair.symbols[0].symbol : null,
                  repairMode: historyRepair.missingTypes.includes("scores") && !historyRepair.missingTypes.includes("bars") ? "scores" : historyRepair.missingTypes.includes("bars") && !historyRepair.missingTypes.includes("scores") ? "bars" : "both",
                  startDate: historyRepair.startDate, endDate: historyRepair.endDate, portfolioId: historyRepair.portfolioId,
                } : historyInitContext} onClearContext={historyRepair ? undefined : () => setHistoryInitContext(null)} onOpenDiagnostic={(symbolId) => { setDiagnosticSymbolId(symbolId); setActiveSection("diagnostic"); }} />
              </section>
            </div>
          )}
          {activeSection === "diagnostic" && (
            <div className="settings-tab-container" data-settings-content="settings-diagnostic">
              <section className="band">
                <DataDiagnosticPanel symbolId={diagnosticSymbolId} onOpenHistoryInit={openHistoryInitialization} />
              </section>
            </div>
          )}
          {activeSection === "tasks" && (
            <div className="settings-tab-container" data-settings-content="settings-tasks">
              <section className="band">
                <TaskCenter />
              </section>
            </div>
          )}
          {activeSection === "message-center" && (
            <div className="settings-tab-container" data-settings-content="settings-message-center">
              <section className="band">
                <MessageCenter
                  tab={messageCenterTab}
                  onTabChange={setMessageCenterTab}
                  deliverySubTab={activeNotificationTab}
                  onDeliverySubTabChange={setActiveNotificationTab}
                />
              </section>
            </div>
          )}
          {activeSection === "scoring" && (
            <div className="settings-tab-container" data-settings-content="settings-scoring">
              <section className="band">
                <ScoringConfigSettings />
              </section>
            </div>
          )}
          {activeSection === "schedules" && (
            <div className="settings-tab-container" data-settings-content="settings-schedules">
              <section className="band">
                <ScheduledTaskManager />
              </section>
            </div>
          )}
          {activeSection === "factor-model" && (
            <div className="settings-tab-container" data-settings-content="settings-factor-model">
              <section className="band">
                <FactorModelSettings />
              </section>
            </div>
          )}
          {activeSection === "factor-center" && (
            <div className="settings-tab-container" data-settings-content="settings-factor-center">
              <section className="band">
                <FactorCenter />
              </section>
            </div>
          )}
          {activeSection === "factor-mining" && (
            <div className="settings-tab-container" data-settings-content="settings-factor-mining">
              <section className="band">
                <MiningShell />
              </section>
            </div>
          )}
          {activeSection === "data-center" && (
            <div className="settings-tab-container" data-settings-content="settings-data-center">
              <section className="band">
                <DataCenter backtestRepair={backtestRepair} />
              </section>
            </div>
          )}
          {activeSection === "app-settings" && (
            <div className="settings-tab-container" data-settings-content="settings-app-settings">
              <section className="band">
                <AppSettingsSection />
              </section>
            </div>
          )}
          {activeSection === "api-mgmt" && (
            <div className="settings-tab-container" data-settings-content="settings-api-mgmt">
              <section className="band">
                <AkshareApiManager />
              </section>
            </div>
          )}
          {activeSection === "db" && (
            <div className="settings-tab-container" data-settings-content="settings-db">
              <section className="band">
                <DbConfigSection />
              </section>
            </div>
          )}
          {activeSection === "ai" && (
            <div className="settings-tab-container" data-settings-content="settings-ai">
              <section className="band">
                <div className="settings-indicator-stack">
                  <div className="sub-tabs" aria-label={t("aiConfigTabTitle")}>
                    <button
                      type="button"
                      className={`sub-tab ${activeAiTab === "config" ? "active" : ""}`}
                      aria-current={activeAiTab === "config" ? "page" : undefined}
                      onClick={() => setActiveAiTab("config")}
                    >
                      {t("aiConfigTabTitle")}
                    </button>
                    <button
                      type="button"
                      className={`sub-tab ${activeAiTab === "profiles" ? "active" : ""}`}
                      aria-current={activeAiTab === "profiles" ? "page" : undefined}
                      onClick={() => setActiveAiTab("profiles")}
                    >
                      {t("aiSettings.profiles")}
                    </button>
                  </div>
                  <div className="sub-tab-container" hidden={activeAiTab !== "config"}>
                    <AiConfigSection />
                  </div>
                  <div className="sub-tab-container" hidden={activeAiTab !== "profiles"}>
                    <AISettings embedded />
                  </div>
                </div>
              </section>
            </div>
          )}
          {activeSection === "factor-basic" && (
            <div className="settings-tab-container" data-settings-content="settings-factor-basic">
              <section className="band">
                <FactorBasicSettings />
              </section>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}




