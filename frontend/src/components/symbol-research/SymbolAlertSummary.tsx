// WP5.1：标的研究告警摘要
//
// 设计说明：
// - 任务清单 #5：告警摘要（正式提醒归告警中心，本组件只展示研究态预警）
// - 从 InvestmentCenter.tsx 抽出 renderPriceAlertSection JSX 块
// - 价格预警检测逻辑（基于 chartData/setup/alertSettings）保留在 Shell
// - alertSettings 持久化逻辑（localStorage）保留在 Shell
//
// 关键约束：
// - 正式提醒归告警中心（AlertCenter.tsx），本组件只展示研究态预警
// - 不重写告警规则（止损/目标/RSI/MACD 判定算法不动）
// - alertSettings 通过 props 传入，更新通过 onUpdateSettings 回调
//
// WP5.2：
// - 即时提醒标注"未持久化" Tag
// - 新增"创建正式告警规则"按钮 + Modal，通过 POST /alerts/rules 持久化
import { useState } from "react";
import { Button, Input, InputNumber, Modal, Select, Switch, Tag, message } from "antd";
import { t, template } from "../../i18n";
import { api } from "../../api/client";
import type { SymbolAlertSummaryProps } from "./types";

// AlertRule alert_type 枚举（必须与后端 app/schemas/alert.py 对齐）
const ALERT_TYPE_OPTIONS = [
  { value: "score_drop", labelKey: "alertTypeScoreDrop" },
  { value: "data_stale", labelKey: "alertTypeDataStale" },
  { value: "indicator_trigger", labelKey: "alertTypeIndicatorTrigger" },
  { value: "task_failed", labelKey: "alertTypeTaskFailed" },
  { value: "watchlist_signal", labelKey: "alertTypeWatchlistSignal" },
];

const SEVERITY_OPTIONS = [
  { value: "info", labelKey: "severityInfo" },
  { value: "warn", labelKey: "severityWarn" },
  { value: "error", labelKey: "severityError" },
];

/**
 * 告警摘要面板：展示当前研究态告警条目 + 告警阈值设置。
 * - alerts 数据由 Shell 检测生成
 * - alertSettings 通过 props 传入，支持就地编辑
 * - WP5.2：每条告警标注"未持久化" Tag；提供"创建正式告警规则"入口
 */
export default function SymbolAlertSummary({
  alerts,
  alertSettings,
  settingsOpen,
  symbolId,
  onToggleSettings,
  onUpdateSettings,
}: SymbolAlertSummaryProps) {
  const [createRuleModalOpen, setCreateRuleModalOpen] = useState(false);
  const [ruleName, setRuleName] = useState("");
  const [ruleType, setRuleType] = useState<string>("indicator_trigger");
  const [ruleSeverity, setRuleSeverity] = useState<string>("warn");
  const [ruleCooldown, setRuleCooldown] = useState<number>(30);
  const [creating, setCreating] = useState(false);

  const handleOpenCreateModal = () => {
    // 默认填充规则名：当前标的 + 默认类型
    setRuleName(
      symbolId != null
        ? `${t("symbolResearchAlertRuleModalTitle")}-${symbolId}`
        : t("symbolResearchAlertRuleModalTitle"),
    );
    setCreateRuleModalOpen(true);
  };

  const handleSubmitCreateRule = async () => {
    if (!symbolId) {
      message.warning(t("symbolResearchJumpMissingPortfolio"));
      return;
    }
    setCreating(true);
    try {
      // 构造 AlertRule payload：config 内嵌 symbol_id 以便后端按标的触发
      const payload = {
        name: ruleName.trim() || t("symbolResearchAlertRuleModalTitle"),
        alert_type: ruleType,
        enabled: true,
        severity: ruleSeverity,
        cooldown_minutes: ruleCooldown,
        config: {
          symbol_id: symbolId,
          source: "research",
          // 透传当前研究态告警阈值（便于后端复现触发条件）
          stop_near_pct: alertSettings.stopNearPct,
          target_near_pct: alertSettings.targetNearPct,
          rsi_overbought: alertSettings.rsiOverbought,
          rsi_oversold: alertSettings.rsiOversold,
          enable_stop_loss: alertSettings.enableStopLoss,
          enable_target: alertSettings.enableTarget,
          enable_rsi: alertSettings.enableRsi,
          enable_macd: alertSettings.enableMacd,
        },
      };
      const result = await api.createAlertRule(payload);
      const ruleId = (result as { id?: number })?.id;
      if (ruleId != null) {
        message.success(template("symbolResearchAlertRuleCreated", { id: ruleId }));
      } else {
        message.success(t("symbolResearchAlertRuleCreated").replace("#{id}", ""));
      }
      setCreateRuleModalOpen(false);
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);
      message.error(`${t("symbolResearchAlertRuleCreateFailed")}: ${errMsg}`);
    } finally {
      setCreating(false);
    }
  };

  return (
    <section className="ic__price-alerts symbol-research-alert-summary">
      <div className="detail-card-head">
        <h4>{t("priceAlerts")}</h4>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Button size="small" onClick={onToggleSettings}>
            {t("icAlertSettings")}
          </Button>
          <Button
            size="small"
            type="primary"
            onClick={handleOpenCreateModal}
            disabled={symbolId == null}
            data-testid="create-alert-rule-button"
          >
            {t("symbolResearchCreateAlertRule")}
          </Button>
        </div>
      </div>
      {settingsOpen && (
        <div className="ic__settings-grid ic__settings-grid--alerts">
          <label>
            <span>{t("icStopNearPct")}</span>
            <InputNumber
              size="small"
              min={0}
              max={50}
              value={alertSettings.stopNearPct}
              onChange={(v) => onUpdateSettings({ stopNearPct: Number(v ?? 3) })}
            />
          </label>
          <label>
            <span>{t("icTargetNearPct")}</span>
            <InputNumber
              size="small"
              min={0}
              max={50}
              value={alertSettings.targetNearPct}
              onChange={(v) => onUpdateSettings({ targetNearPct: Number(v ?? 5) })}
            />
          </label>
          <label>
            <span>{t("icRsiOverbought")}</span>
            <InputNumber
              size="small"
              min={50}
              max={100}
              value={alertSettings.rsiOverbought}
              onChange={(v) => onUpdateSettings({ rsiOverbought: Number(v ?? 75) })}
            />
          </label>
          <label>
            <span>{t("icRsiOversold")}</span>
            <InputNumber
              size="small"
              min={0}
              max={50}
              value={alertSettings.rsiOversold}
              onChange={(v) => onUpdateSettings({ rsiOversold: Number(v ?? 25) })}
            />
          </label>
          <label className="ic__switch-field">
            <span>{t("icEnableStopLoss")}</span>
            <Switch
              size="small"
              checked={alertSettings.enableStopLoss}
              onChange={(checked) => onUpdateSettings({ enableStopLoss: checked })}
            />
          </label>
          <label className="ic__switch-field">
            <span>{t("icEnableTarget")}</span>
            <Switch
              size="small"
              checked={alertSettings.enableTarget}
              onChange={(checked) => onUpdateSettings({ enableTarget: checked })}
            />
          </label>
          <label className="ic__switch-field">
            <span>{t("icEnableRsi")}</span>
            <Switch
              size="small"
              checked={alertSettings.enableRsi}
              onChange={(checked) => onUpdateSettings({ enableRsi: checked })}
            />
          </label>
          <label className="ic__switch-field">
            <span>{t("icEnableMacd")}</span>
            <Switch
              size="small"
              checked={alertSettings.enableMacd}
              onChange={(checked) => onUpdateSettings({ enableMacd: checked })}
            />
          </label>
        </div>
      )}
      <div className="item-subline" style={{ marginTop: 4, fontSize: 12 }}>
        {/* WP9.3：即时提醒显式标注"即时计算（未持久化）"，区别于告警中心的正式告警规则 */}
        <Tag color="orange" style={{ marginRight: 8, fontSize: 11 }} data-testid="instant-calc-unpersisted-label">
          {t("wp9.instantCalculationNotPersisted")}
        </Tag>
        {t("symbolResearchInstantAlertHint")}
      </div>
      {alerts.length > 0 && (
        <div className="ic__alert-list">
          {alerts.map((alert) => (
            <div key={alert.id} className={`ic__alert-item ic__alert--${alert.level}`}>
              <span className="ic__alert-icon">
                {alert.level === "danger" ? "!" : alert.level === "warning" ? "!" : "i"}
              </span>
              <div className="ic__alert-body">
                <strong>
                  {alert.message}
                  <Tag
                    color="orange"
                    style={{ marginLeft: 8, fontSize: 11 }}
                    data-testid={`unpersisted-tag-${alert.id}`}
                  >
                    {t("symbolResearchUnpersistedTag")}
                  </Tag>
                </strong>
                <span>{alert.detail}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      <Modal
        open={createRuleModalOpen}
        title={t("symbolResearchAlertRuleModalTitle")}
        okText={t("symbolResearchAlertRuleSubmit")}
        cancelText={t("cancel")}
        confirmLoading={creating}
        onCancel={() => setCreateRuleModalOpen(false)}
        onOk={handleSubmitCreateRule}
        data-testid="create-alert-rule-modal"
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: "12px 0" }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span>{t("symbolResearchAlertRuleNameLabel")}</span>
            <Input
              value={ruleName}
              onChange={(e) => setRuleName(e.target.value)}
              placeholder={t("symbolResearchAlertRuleNameLabel")}
              data-testid="alert-rule-name-input"
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span>{t("symbolResearchAlertRuleTypeLabel")}</span>
            <Select
              value={ruleType}
              onChange={(v) => setRuleType(v)}
              options={ALERT_TYPE_OPTIONS.map((opt) => ({
                value: opt.value,
                label: t(opt.labelKey) !== opt.labelKey ? t(opt.labelKey) : opt.value,
              }))}
              data-testid="alert-rule-type-select"
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span>{t("symbolResearchAlertRuleSeverityLabel")}</span>
            <Select
              value={ruleSeverity}
              onChange={(v) => setRuleSeverity(v)}
              options={SEVERITY_OPTIONS.map((opt) => ({
                value: opt.value,
                label: t(opt.labelKey) !== opt.labelKey ? t(opt.labelKey) : opt.value,
              }))}
              data-testid="alert-rule-severity-select"
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span>{t("alertCooldown")}</span>
            <InputNumber
              min={0}
              max={1440}
              value={ruleCooldown}
              onChange={(v) => setRuleCooldown(Number(v ?? 30))}
              data-testid="alert-rule-cooldown-input"
            />
          </label>
          <div className="item-subline" style={{ fontSize: 12 }}>
            {t("symbolResearchAlertRuleConfigLabel")}: symbol_id={symbolId ?? "-"} · source=research
          </div>
        </div>
      </Modal>
    </section>
  );
}
