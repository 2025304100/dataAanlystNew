import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Collapse,
  Empty,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
} from "antd";
import {
  BellOutlined,
  CheckOutlined,
  DeleteOutlined,
  EditOutlined,
  ExclamationCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import { t, template } from "../i18n";
import type { AlertRule, AlertEvent as AlertEventType } from "../types";

const SEVERITY_COLORS: Record<string, string> = {
  info: "#1570ef",
  warn: "#d97706",
  error: "#b42318",
};

const ALERT_TYPE_OPTIONS = [
  { value: "score_drop", label: t("alertTypeScoreDrop") },
  { value: "data_stale", label: t("alertTypeDataStale") },
  { value: "task_failed", label: t("alertTypeTaskFailed") },
  { value: "indicator_trigger", label: t("alertTypeIndicatorTrigger") },
  { value: "watchlist_signal", label: t("alertTypeWatchlistSignal") },
];

const SEVERITY_OPTIONS = [
  { value: "info", label: t("alertSeverityInfo") },
  { value: "warn", label: t("alertSeverityWarn") },
  { value: "error", label: t("alertSeverityError") },
];

function alertTypeLabel(type: string): string {
  const found = ALERT_TYPE_OPTIONS.find((o) => o.value === type);
  return found ? found.label : type;
}

function formatTime(iso?: string): string {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

interface RuleFormData {
  name: string;
  alert_type: string;
  severity: string;
  enabled: boolean;
  cooldown_minutes: number;
  threshold: number;
  stale_days: number;
  lookback_hours: number;
}

const DEFAULT_FORM: RuleFormData = {
  name: "",
  alert_type: "score_drop",
  severity: "warn",
  enabled: true,
  cooldown_minutes: 60,
  threshold: 40,
  stale_days: 7,
  lookback_hours: 24,
};

export default function AlertCenter() {
  const { showToast } = useApp();
  const [activeTab, setActiveTab] = useState<"events" | "rules">("events");
  const [events, setEvents] = useState<AlertEventType[]>([]);
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [loading, setLoading] = useState(false);
  const [showRuleModal, setShowRuleModal] = useState(false);
  const [editingRule, setEditingRule] = useState<AlertRule | null>(null);
  const [form, setForm] = useState<RuleFormData>(DEFAULT_FORM);

  const loadEvents = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getActiveAlerts(100);
      setEvents(data.events);
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  }, []);

  const loadRules = useCallback(async () => {
    try {
      const data = await api.getAlertRules();
      setRules(data);
    } catch {
      // silent
    }
  }, []);

  useEffect(() => {
    loadEvents();
    loadRules();
  }, [loadEvents, loadRules]);

  const handleEvaluate = useCallback(async () => {
    try {
      const result = await api.evaluateAlerts();
      await loadEvents();
      showToast("success", template("alertEvaluated", { count: result.new_events }));
    } catch (error: any) {
      showToast("error", error?.message || t("loadFailed"));
    }
  }, [loadEvents, showToast]);

  const handleAcknowledge = useCallback(async (eventId: number) => {
    try {
      await api.acknowledgeAlert(eventId);
      setEvents((prev) => prev.filter((e) => e.id !== eventId));
    } catch {
      // silent
    }
  }, []);

  const handleAcknowledgeAll = useCallback(async () => {
    try {
      await api.acknowledgeAllAlerts();
      setEvents([]);
    } catch {
      // silent
    }
  }, []);

  const handleDeleteRule = useCallback(async (id: number) => {
    if (!window.confirm(t("alertConfirmDelete"))) return;
    try {
      await api.deleteAlertRule(id);
      setRules((prev) => prev.filter((r) => r.id !== id));
      showToast("success", t("alertRuleDeleted"));
    } catch (error: any) {
      showToast("error", error?.message || t("loadFailed"));
    }
  }, [showToast]);

  const handleToggleRule = useCallback(async (rule: AlertRule) => {
    try {
      await api.updateAlertRule(rule.id, { enabled: !rule.enabled });
      setRules((prev) => prev.map((r) => r.id === rule.id ? { ...r, enabled: r.enabled ? 0 : 1 } : r));
      showToast("success", t("alertRuleUpdated"));
    } catch (error: any) {
      showToast("error", error?.message || t("loadFailed"));
    }
  }, [showToast]);

  const openNewRule = () => {
    setEditingRule(null);
    setForm(DEFAULT_FORM);
    setShowRuleModal(true);
  };

  const openEditRule = (rule: AlertRule) => {
    setEditingRule(rule);
    let config: Record<string, any> = {};
    try {
      config = rule.config_json ? JSON.parse(rule.config_json) : {};
    } catch { /* ignore */ }
    setForm({
      name: rule.name,
      alert_type: rule.alert_type,
      severity: rule.severity,
      enabled: !!rule.enabled,
      cooldown_minutes: rule.cooldown_minutes,
      threshold: config.threshold ?? 40,
      stale_days: config.stale_days ?? 7,
      lookback_hours: config.lookback_hours ?? 24,
    });
    setShowRuleModal(true);
  };

  const handleSaveRule = useCallback(async () => {
    if (!form.name.trim()) {
      showToast("error", t("alertRuleName"));
      return;
    }
    const config: Record<string, any> = {};
    if (form.alert_type === "score_drop") config.threshold = form.threshold;
    if (form.alert_type === "data_stale") config.stale_days = form.stale_days;
    if (form.alert_type === "task_failed") config.lookback_hours = form.lookback_hours;

    const payload = {
      name: form.name,
      alert_type: form.alert_type,
      severity: form.severity,
      enabled: form.enabled,
      cooldown_minutes: form.cooldown_minutes,
      config,
    };
    try {
      if (editingRule) {
        await api.updateAlertRule(editingRule.id, payload);
        showToast("success", t("alertRuleUpdated"));
      } else {
        await api.createAlertRule(payload);
        showToast("success", t("alertRuleCreated"));
      }
      setShowRuleModal(false);
      await loadRules();
    } catch (error: any) {
      showToast("error", error?.message || t("loadFailed"));
    }
  }, [form, editingRule, showToast, loadRules]);

  const unackCount = events.filter((event) => !event.resolved).length;

  return (
    <div className="alert-center-container">
      {/* Header */}
      <div className="alert-center-header">
        <div className="alert-center-header-left">
          <BellOutlined style={{ fontSize: 18, color: "#0f766e" }} />
          <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>{t("alertCenter")}</h3>
          {unackCount > 0 && (
            <Badge count={unackCount} style={{ backgroundColor: "#b42318" }} />
          )}
        </div>
        <Space>
          <div className="alert-tab-group">
            <button className={`alert-tab ${activeTab === "events" ? "active" : ""}`} onClick={() => setActiveTab("events")}>
              {t("alertEvents")} {unackCount > 0 && <Badge count={unackCount} size="small" />}
            </button>
            <button className={`alert-tab ${activeTab === "rules" ? "active" : ""}`} onClick={() => setActiveTab("rules")}>
              {t("alertRules")}
            </button>
          </div>
        </Space>
      </div>

      {/* Events tab */}
      {activeTab === "events" && (
        <div className="alert-tab-content">
          <div className="alert-actions-bar">
            <Space>
              <Button icon={<ThunderboltOutlined />} onClick={handleEvaluate}>{t("alertEvaluate")}</Button>
              {events.length > 0 && (
                <Button icon={<CheckOutlined />} onClick={handleAcknowledgeAll}>{t("alertAcknowledgeAll")}</Button>
              )}
              <Button icon={<ReloadOutlined />} onClick={loadEvents}>{t("refresh")}</Button>
            </Space>
          </div>

          {loading ? (
            <div style={{ textAlign: "center", padding: 40 }}><Spin /></div>
          ) : events.length === 0 ? (
            <Empty description={t("alertNoEvents")} style={{ padding: 40 }} />
          ) : (
            <div className="alert-event-list">
              {events.map((ev) => (
                <Alert
                  key={ev.id}
                  type={ev.resolved ? "success" : ev.severity === "error" ? "error" : ev.severity === "warn" ? "warning" : "info"}
                  showIcon
                  icon={!ev.resolved && ev.severity === "error" ? <ExclamationCircleOutlined /> : undefined}
                  message={
                    <div className="alert-event-header">
                      <span className="alert-event-title">
                        {ev.title}
                        {ev.resolved && <Tag color="green" style={{ marginLeft: 8 }}>已恢复</Tag>}
                      </span>
                      <span className="alert-event-time">{formatTime(ev.created_at)}</span>
                    </div>
                  }
                  description={
                    <div className="alert-event-body">
                      <p>{ev.message}</p>
                      <Space>
                        <Tag color={ev.resolved ? "green" : SEVERITY_COLORS[ev.severity]}>{alertTypeLabel(ev.alert_type)}</Tag>
                        {ev.symbol && <Tag>{ev.symbol.symbol} {ev.symbol.name}</Tag>}
                      </Space>
                      {ev.resolved_at && (
                        <div style={{ marginTop: 8, color: "#389e0d" }}>
                          恢复时间：{formatTime(ev.resolved_at)}
                        </div>
                      )}
                      {ev.technical_details && (
                        <Collapse
                          ghost
                          size="small"
                          style={{ marginTop: 8 }}
                          items={[{
                            key: `details-${ev.id}`,
                            label: "技术详情",
                            children: (
                              <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0, maxHeight: 240, overflow: "auto" }}>
                                {ev.technical_details}
                              </pre>
                            ),
                          }]}
                        />
                      )}
                      <div className="alert-event-actions">
                        <Button size="small" icon={<CheckOutlined />} onClick={() => handleAcknowledge(ev.id)}>
                          {t("alertAcknowledge")}
                        </Button>
                      </div>
                    </div>
                  }
                  style={{ marginBottom: 8 }}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Rules tab */}
      {activeTab === "rules" && (
        <div className="alert-tab-content">
          <div className="alert-actions-bar">
            <Button type="primary" icon={<PlusOutlined />} onClick={openNewRule}>{t("alertNewRule")}</Button>
          </div>

          {rules.length === 0 ? (
            <Empty description={t("alertNoRules")} style={{ padding: 40 }} />
          ) : (
            <div className="alert-rule-list">
              {rules.map((rule) => {
                let config: Record<string, any> = {};
                try { config = rule.config_json ? JSON.parse(rule.config_json) : {}; } catch { /* */ }
                return (
                  <div key={rule.id} className="alert-rule-item">
                    <div className="alert-rule-item-head">
                      <div className="alert-rule-item-info">
                        <Tag color={SEVERITY_COLORS[rule.severity]}>{alertTypeLabel(rule.alert_type)}</Tag>
                        <span className="alert-rule-item-name">{rule.name}</span>
                        <Switch size="small" checked={!!rule.enabled} onChange={() => handleToggleRule(rule)} />
                      </div>
                      <Space>
                        <Button size="small" icon={<EditOutlined />} onClick={() => openEditRule(rule)} />
                        <Button size="small" icon={<DeleteOutlined />} onClick={() => handleDeleteRule(rule.id)} />
                      </Space>
                    </div>
                    <div className="alert-rule-item-meta">
                      <span>{t("alertCooldown")}: {rule.cooldown_minutes}min</span>
                      <span>{t("alertLastTriggered")}: {rule.last_triggered_at ? formatTime(rule.last_triggered_at) : t("alertNeverTriggered")}</span>
                      {config.threshold != null && <span>{t("alertThreshold")}: {config.threshold}</span>}
                      {config.stale_days != null && <span>{t("alertStaleDays")}: {config.stale_days}</span>}
                      {config.lookback_hours != null && <span>{t("alertLookbackHours")}: {config.lookback_hours}</span>}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Rule Modal */}
      <Modal
        title={editingRule ? t("alertEditRule") : t("alertNewRule")}
        open={showRuleModal}
        onOk={handleSaveRule}
        onCancel={() => setShowRuleModal(false)}
        okText={t("save") || "Save"}
        cancelText={t("cancel") || "Cancel"}
      >
        <div className="alert-rule-form">
          <label className="alert-form-field">
            <span>{t("alertRuleName")}</span>
            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </label>
          <label className="alert-form-field">
            <span>{t("alertType")}</span>
            <Select value={form.alert_type} onChange={(v) => setForm({ ...form, alert_type: v })} options={ALERT_TYPE_OPTIONS} style={{ width: "100%" }} />
          </label>
          <label className="alert-form-field">
            <span>{t("alertSeverity")}</span>
            <Select value={form.severity} onChange={(v) => setForm({ ...form, severity: v })} options={SEVERITY_OPTIONS} style={{ width: "100%" }} />
          </label>
          <label className="alert-form-field">
            <span>{t("alertCooldown")}</span>
            <InputNumber min={1} max={1440} value={form.cooldown_minutes} onChange={(v) => setForm({ ...form, cooldown_minutes: v ?? 60 })} style={{ width: "100%" }} />
          </label>
          {form.alert_type === "score_drop" && (
            <label className="alert-form-field">
              <span>{t("alertThreshold")}</span>
              <InputNumber min={0} max={100} value={form.threshold} onChange={(v) => setForm({ ...form, threshold: v ?? 40 })} style={{ width: "100%" }} />
            </label>
          )}
          {form.alert_type === "data_stale" && (
            <label className="alert-form-field">
              <span>{t("alertStaleDays")}</span>
              <InputNumber min={1} max={90} value={form.stale_days} onChange={(v) => setForm({ ...form, stale_days: v ?? 7 })} style={{ width: "100%" }} />
            </label>
          )}
          {form.alert_type === "task_failed" && (
            <label className="alert-form-field">
              <span>{t("alertLookbackHours")}</span>
              <InputNumber min={1} max={168} value={form.lookback_hours} onChange={(v) => setForm({ ...form, lookback_hours: v ?? 24 })} style={{ width: "100%" }} />
            </label>
          )}
        </div>
      </Modal>
    </div>
  );
}
