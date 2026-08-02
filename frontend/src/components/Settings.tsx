import { useState, useEffect } from "react";
import { useApp } from "../context/AppContext";
import { t, DOT } from "../i18n";
import { statPct, pnlClass, clamp } from "../utils/format";
import { Input, InputNumber, Checkbox, Button, Space, Card, Form } from "antd";
import { CalendarOutlined, DatabaseOutlined, ExperimentOutlined, FunctionOutlined, SettingOutlined, SyncOutlined, MedicineBoxOutlined, UnorderedListOutlined, BellOutlined, TrophyOutlined, GlobalOutlined, ApiOutlined, RobotOutlined, NotificationOutlined, AppstoreOutlined } from "@ant-design/icons";
import DbConfigSection from "./DbConfigSection";
import CustomIndicatorSettings from "./CustomIndicatorSettings";
import DiscoveryPlanSettings from "./DiscoveryPlanSettings";
import HistoryInitSection from "./HistoryInitSection";
import DataDiagnosticPanel from "./DataDiagnosticPanel";
import TaskCenter from "./TaskCenter";
import AlertCenter from "./AlertCenter";
import ScoringConfigSettings from "./ScoringConfigSettings";
import ExternalDataSync from "./ExternalDataSync";
import AkshareApiManager from "./AkshareApiManager";
import UniverseDataPanel from "./UniverseDataPanel";
import FactorModelSettings from "./FactorModelSettings";
import FactorCenter from "./factors/FactorCenter";
import ScheduledTaskManager from "./ScheduledTaskManager";
import AiConfigSection from "./AiConfigSection";
import { ChannelConfig } from "./notifications/ChannelConfig";
import { PolicyEditor } from "./notifications/PolicyEditor";
import { TemplateEditor } from "./notifications/TemplateEditor";
import { DeliveryLog } from "./notifications/DeliveryLog";

export default function Settings() {
  const ctx = useApp();
  const rule = ctx.signalRule;
  const presets = ctx.signalRulePresets;
  const preview = ctx.signalRulePreview;
  const activeSymbolId = ctx.activeSymbolId;
  const [saving, setSaving] = useState(false);
  const [activeSection, setActiveSection] = useState<"rules" | "indicators" | "history" | "diagnostic" | "tasks" | "schedules" | "alerts" | "scoring" | "factor-model" | "factor-center" | "external" | "api-mgmt" | "universe" | "db" | "ai" | "notifications">(() => {
    if (typeof window === "undefined") return "rules";
    const stored = window.localStorage.getItem("settings_active_section");
    return stored === "rules" || stored === "indicators" || stored === "history" || stored === "diagnostic" || stored === "tasks" || stored === "schedules" || stored === "alerts" || stored === "scoring" || stored === "factor-model" || stored === "factor-center" || stored === "external" || stored === "api-mgmt" || stored === "universe" || stored === "db" || stored === "ai" || stored === "notifications" ? stored : "rules";
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
  const [historyFocusSignal, setHistoryFocusSignal] = useState(0);
  const [diagnosticSymbolId, setDiagnosticSymbolId] = useState<number | null>(null);
  const [historyInitContext, setHistoryInitContext] = useState<{ symbolId?: number | null; symbolLabel?: string | null; repairMode?: "both" | "bars" | "scores" } | null>(null);

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
    if (typeof window !== "undefined") {
      window.localStorage.setItem("settings_indicator_subtab", activeIndicatorTab);
    }
  }, [activeIndicatorTab]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem("settings_notification_subtab", activeNotificationTab);
    }
  }, [activeNotificationTab]);

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
        <nav className="settings-sidebar" aria-label={t("ariaSettingsCategories")}>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "rules" ? "active" : ""}`}
            aria-current={activeSection === "rules" ? "page" : undefined}
            onClick={() => setActiveSection("rules")}
          >
            <span className="settings-nav-icon"><SettingOutlined /></span>
            <span className="settings-nav-copy">{t("tabRules")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "indicators" ? "active" : ""}`}
            aria-current={activeSection === "indicators" ? "page" : undefined}
            onClick={() => setActiveSection("indicators")}
          >
            <span className="settings-nav-icon"><FunctionOutlined /></span>
            <span className="settings-nav-copy">{t("tabIndicators")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "history" ? "active" : ""}`}
            aria-current={activeSection === "history" ? "page" : undefined}
            onClick={() => setActiveSection("history")}
          >
            <span className="settings-nav-icon"><SyncOutlined /></span>
            <span className="settings-nav-copy">{t("histSettingsNav")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "diagnostic" ? "active" : ""}`}
            aria-current={activeSection === "diagnostic" ? "page" : undefined}
            onClick={() => setActiveSection("diagnostic")}
          >
            <span className="settings-nav-icon"><MedicineBoxOutlined /></span>
            <span className="settings-nav-copy">{t("diagTitle")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "tasks" ? "active" : ""}`}
            aria-current={activeSection === "tasks" ? "page" : undefined}
            onClick={() => setActiveSection("tasks")}
          >
            <span className="settings-nav-icon"><UnorderedListOutlined /></span>
            <span className="settings-nav-copy">{t("taskCenter")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "schedules" ? "active" : ""}`}
            aria-current={activeSection === "schedules" ? "page" : undefined}
            onClick={() => setActiveSection("schedules")}
          >
            <span className="settings-nav-icon"><CalendarOutlined /></span>
            <span className="settings-nav-copy">{t("scheduledTaskManager")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "alerts" ? "active" : ""}`}
            aria-current={activeSection === "alerts" ? "page" : undefined}
            onClick={() => setActiveSection("alerts")}
          >
            <span className="settings-nav-icon"><BellOutlined /></span>
            <span className="settings-nav-copy">{t("alertCenter")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "scoring" ? "active" : ""}`}
            aria-current={activeSection === "scoring" ? "page" : undefined}
            onClick={() => setActiveSection("scoring")}
          >
            <span className="settings-nav-icon"><TrophyOutlined /></span>
            <span className="settings-nav-copy">{t("scTabTitle")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "factor-model" ? "active" : ""}`}
            aria-current={activeSection === "factor-model" ? "page" : undefined}
            onClick={() => setActiveSection("factor-model")}
          >
            <span className="settings-nav-icon"><ExperimentOutlined /></span>
            <span className="settings-nav-copy">{t("factorModelTabTitle")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "factor-center" ? "active" : ""}`}
            aria-current={activeSection === "factor-center" ? "page" : undefined}
            onClick={() => setActiveSection("factor-center")}
          >
            <span className="settings-nav-icon"><AppstoreOutlined /></span>
            <span className="settings-nav-copy">{t("factorCenterTabTitle")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "external" ? "active" : ""}`}
            aria-current={activeSection === "external" ? "page" : undefined}
            onClick={() => setActiveSection("external")}
          >
            <span className="settings-nav-icon"><GlobalOutlined /></span>
            <span className="settings-nav-copy">{t("extTabTitle")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "api-mgmt" ? "active" : ""}`}
            aria-current={activeSection === "api-mgmt" ? "page" : undefined}
            onClick={() => setActiveSection("api-mgmt")}
          >
            <span className="settings-nav-icon"><ApiOutlined /></span>
            <span className="settings-nav-copy">{t("apiMgmtTabTitle")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "universe" ? "active" : ""}`}
            aria-current={activeSection === "universe" ? "page" : undefined}
            onClick={() => setActiveSection("universe")}
          >
            <span className="settings-nav-icon"><DatabaseOutlined /></span>
            <span className="settings-nav-copy">{t("universeTabTitle")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "db" ? "active" : ""}`}
            aria-current={activeSection === "db" ? "page" : undefined}
            onClick={() => setActiveSection("db")}
          >
            <span className="settings-nav-icon"><DatabaseOutlined /></span>
            <span className="settings-nav-copy">{t("dbTabTitle")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "ai" ? "active" : ""}`}
            aria-current={activeSection === "ai" ? "page" : undefined}
            onClick={() => setActiveSection("ai")}
          >
            <span className="settings-nav-icon"><RobotOutlined /></span>
            <span className="settings-nav-copy">{t("aiConfigTabTitle")}</span>
          </button>
          <button
            type="button"
            className={`settings-nav-item ${activeSection === "notifications" ? "active" : ""}`}
            aria-current={activeSection === "notifications" ? "page" : undefined}
            onClick={() => setActiveSection("notifications")}
          >
            <span className="settings-nav-icon"><NotificationOutlined /></span>
            <span className="settings-nav-copy">{t("messageManagement")}</span>
          </button>
        </nav>
        <div className="settings-content">
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
                      <Button type="primary" className="ghost-button detail-action" disabled={!rule || saving} loading={saving}>
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
                <HistoryInitSection focusSignal={historyFocusSignal} context={historyInitContext} onClearContext={() => setHistoryInitContext(null)} onOpenDiagnostic={(symbolId) => { setDiagnosticSymbolId(symbolId); setActiveSection("diagnostic"); }} />
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
          {activeSection === "alerts" && (
            <div className="settings-tab-container" data-settings-content="settings-alerts">
              <section className="band">
                <AlertCenter />
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
          {activeSection === "external" && (
            <div className="settings-tab-container" data-settings-content="settings-external">
              <section className="band">
                <ExternalDataSync />
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
          {activeSection === "universe" && (
            <div className="settings-tab-container" data-settings-content="settings-universe">
              <UniverseDataPanel />
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
                <AiConfigSection />
              </section>
            </div>
          )}
          {activeSection === "notifications" && (
            <div className="settings-tab-container" data-settings-content="settings-notifications">
              <section className="band">
                <div className="settings-indicator-stack">
                  <div className="sub-tabs" aria-label={t("messageManagement")}>
                    <button
                      type="button"
                      className={`sub-tab ${activeNotificationTab === "channels" ? "active" : ""}`}
                      aria-current={activeNotificationTab === "channels" ? "page" : undefined}
                      onClick={() => setActiveNotificationTab("channels")}
                    >
                      {t("channelConfig")}
                    </button>
                    <button
                      type="button"
                      className={`sub-tab ${activeNotificationTab === "policies" ? "active" : ""}`}
                      aria-current={activeNotificationTab === "policies" ? "page" : undefined}
                      onClick={() => setActiveNotificationTab("policies")}
                    >
                      {t("policyEditor")}
                    </button>
                    <button
                      type="button"
                      className={`sub-tab ${activeNotificationTab === "templates" ? "active" : ""}`}
                      aria-current={activeNotificationTab === "templates" ? "page" : undefined}
                      onClick={() => setActiveNotificationTab("templates")}
                    >
                      {t("templateEditor")}
                    </button>
                    <button
                      type="button"
                      className={`sub-tab ${activeNotificationTab === "deliveries" ? "active" : ""}`}
                      aria-current={activeNotificationTab === "deliveries" ? "page" : undefined}
                      onClick={() => setActiveNotificationTab("deliveries")}
                    >
                      {t("deliveryLog")}
                    </button>
                  </div>
                  <div className="sub-tab-container" hidden={activeNotificationTab !== "channels"}>
                    <ChannelConfig />
                  </div>
                  <div className="sub-tab-container" hidden={activeNotificationTab !== "policies"}>
                    <PolicyEditor />
                  </div>
                  <div className="sub-tab-container" hidden={activeNotificationTab !== "templates"}>
                    <TemplateEditor />
                  </div>
                  <div className="sub-tab-container" hidden={activeNotificationTab !== "deliveries"}>
                    <DeliveryLog />
                  </div>
                </div>
              </section>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}




