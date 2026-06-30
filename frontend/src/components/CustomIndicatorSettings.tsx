import { useEffect, useMemo, useState } from "react";
import { Button, Card, Checkbox, DatePicker, Form, Input, Popconfirm, Select, Space, Switch, Table, Tag, message } from "antd";
import { DeleteOutlined, EyeOutlined, PlusOutlined, ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import type { Dayjs } from "dayjs";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import { t } from "../i18n";
import type {
  BacktestRuleConfigV2,
  CustomIndicator,
  CustomIndicatorPayload,
  CustomIndicatorPreviewResult,
  DiscoveryPlan,
  RuleTemplate,
  Symbol,
} from "../types";

const DEFAULT_FORM: CustomIndicatorPayload = {
  name: "",
  key: "",
  description: "",
  category: "custom",
  formula: "sma(20) > sma(60) and rsi(14) < 70",
  value_type: "boolean",
  params: [],
  scope: ["backtest", "discovery"],
  enabled: true,
};

function keyFromName(name: string): string {
  const ascii = name.trim().replace(/[^a-zA-Z0-9_]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase();
  return ascii || "custom_indicator";
}

function mergeSymbols(current: Symbol[], next: Symbol[]): Symbol[] {
  const map = new Map<number, Symbol>();
  [...current, ...next].forEach((item) => map.set(item.id, item));
  return Array.from(map.values());
}

function collectBacktestIndicatorKeys(node: unknown, keys: Set<string>) {
  if (!node) return;
  if (Array.isArray(node)) {
    node.forEach((item) => collectBacktestIndicatorKeys(item, keys));
    return;
  }
  if (typeof node !== "object") return;
  const record = node as Record<string, unknown>;
  if (record.field === "custom_indicator") {
    const params = record.params as Record<string, unknown> | undefined;
    const indicatorKey = typeof params?.indicator_key === "string" ? params.indicator_key : "";
    if (indicatorKey) keys.add(indicatorKey);
  }
  Object.values(record).forEach((value) => collectBacktestIndicatorKeys(value, keys));
}

function scoreValue(value?: number | null): string {
  if (value == null || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(2);
}

function previewValue(result: CustomIndicatorPreviewResult): string {
  if (result.value_type === "number") {
    return result.result_number != null ? String(result.result_number) : result.display_value;
  }
  return result.result_boolean ? t("yes") : t("no");
}

export default function CustomIndicatorSettings() {
  const ctx = useApp();
  const [rows, setRows] = useState<CustomIndicator[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [form, setForm] = useState<CustomIndicatorPayload>(DEFAULT_FORM);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewResult, setPreviewResult] = useState<CustomIndicatorPreviewResult | null>(null);
  const [previewSymbolId, setPreviewSymbolId] = useState<number | null>(ctx.activeSymbolId);
  const [previewTradeDate, setPreviewTradeDate] = useState<Dayjs | null>(null);
  const [previewSymbolOptions, setPreviewSymbolOptions] = useState<Symbol[]>([]);
  const [discoveryPlans, setDiscoveryPlans] = useState<DiscoveryPlan[]>([]);
  const [ruleTemplates, setRuleTemplates] = useState<RuleTemplate[]>([]);

  const categoryOptions = useMemo(() => [
    { label: t("ciCatTrend"), value: "trend" },
    { label: t("ciCatMomentum"), value: "momentum" },
    { label: t("ciCatVolatility"), value: "volatility" },
    { label: t("ciCatVolume"), value: "volume" },
    { label: t("ciCatRisk"), value: "risk" },
    { label: t("ciCatCustom"), value: "custom" },
  ], [ctx.locale]);

  const scopeOptions = useMemo(() => [
    { label: t("ciScopeBacktest"), value: "backtest" },
    { label: t("ciScopeDiscovery"), value: "discovery" },
    { label: t("ciScopeAlert"), value: "alert" },
    { label: t("ciScopeReview"), value: "review" },
  ], [ctx.locale]);

  const selected = useMemo(() => rows.find((row) => row.id === selectedId) ?? null, [rows, selectedId]);
  const activeSymbol = useMemo<Symbol | null>(() => {
    if (!ctx.activeSymbolId) return null;
    if (ctx.detail?.symbol?.id === ctx.activeSymbolId) return ctx.detail.symbol;
    return ctx.symbolDirectory[ctx.activeSymbolId] ?? null;
  }, [ctx.activeSymbolId, ctx.detail, ctx.symbolDirectory]);
  const previewSymbol = useMemo<Symbol | null>(() => {
    if (!previewSymbolId) return null;
    return previewSymbolOptions.find((item) => item.id === previewSymbolId)
      ?? (activeSymbol?.id === previewSymbolId ? activeSymbol : null)
      ?? ctx.symbolDirectory[previewSymbolId]
      ?? null;
  }, [activeSymbol, ctx.symbolDirectory, previewSymbolId, previewSymbolOptions]);

  const loadRows = async () => {
    setLoading(true);
    try {
      const [indicators, plans, templates] = await Promise.all([
        api.getCustomIndicators(),
        api.getDiscoveryPlans(),
        api.getBacktestTemplates(),
      ]);
      setRows(indicators as CustomIndicator[]);
      setDiscoveryPlans(plans as DiscoveryPlan[]);
      setRuleTemplates(templates as RuleTemplate[]);
    } catch (error: any) {
      message.error(error?.message || t("ciLoadFailed"));
    } finally {
      setLoading(false);
    }
  };

  const searchPreviewSymbols = async (keyword?: string) => {
    try {
      const rows = await api.getSymbols(keyword, { page: 1, pageSize: 20 });
      setPreviewSymbolOptions((prev) => mergeSymbols(prev, rows as Symbol[]));
    } catch {
      // ignore preview search failures
    }
  };

  useEffect(() => {
    loadRows();
    searchPreviewSymbols().catch(() => {});
  }, []);

  useEffect(() => {
    if (!activeSymbol) return;
    setPreviewSymbolOptions((prev) => mergeSymbols(prev, [activeSymbol]));
    setPreviewSymbolId((prev) => prev ?? activeSymbol.id);
  }, [activeSymbol]);

  useEffect(() => {
    setPreviewResult(null);
  }, [form.formula, form.value_type, previewSymbolId, previewTradeDate]);

  const editorMeta = useMemo(() => ({
    category: categoryOptions.find((item) => item.value === form.category)?.label ?? form.category,
    valueType: form.value_type === "number" ? t("ciNumber") : t("ciBoolean"),
  }), [categoryOptions, form.category, form.value_type, ctx.locale]);

  const usageSummary = useMemo(() => {
    if (!selected?.key) {
      return { discovery: [] as DiscoveryPlan[], backtest: [] as RuleTemplate[] };
    }
    const discovery = discoveryPlans.filter((plan) => (plan.filters || []).some((filter) => filter.indicator_key === selected.key));
    const backtest = ruleTemplates.filter((template) => {
      const keys = new Set<string>();
      collectBacktestIndicatorKeys((template.rule_config as BacktestRuleConfigV2 | Record<string, unknown>) ?? {}, keys);
      return keys.has(selected.key);
    });
    return { discovery, backtest };
  }, [discoveryPlans, ruleTemplates, selected]);

  const startCreate = () => {
    setSelectedId(null);
    setForm(DEFAULT_FORM);
    setPreviewResult(null);
  };

  const loadIndicator = (row: CustomIndicator) => {
    setSelectedId(row.id);
    setForm({
      name: row.name,
      key: row.key,
      description: row.description,
      category: row.category,
      formula: row.formula,
      value_type: row.value_type,
      params: row.params ?? [],
      scope: row.scope ?? ["backtest"],
      enabled: row.enabled,
    });
    setPreviewResult(null);
  };

  const previewFormula = async () => {
    if (!previewSymbolId) {
      message.info(t("ciSelectSymbolFirst"));
      return;
    }
    if (!form.formula.trim()) {
      message.warning(t("ciFormulaRequired"));
      return;
    }
    setPreviewLoading(true);
    try {
      const result = await api.previewCustomIndicator({
        symbol_id: previewSymbolId,
        formula: form.formula,
        value_type: form.value_type,
        trade_date: previewTradeDate ? previewTradeDate.format("YYYY-MM-DD") : undefined,
      });
      setPreviewResult(result as CustomIndicatorPreviewResult);
      message.success(t("ciPreviewUpdated"));
    } catch (error: any) {
      setPreviewResult(null);
      message.error(error?.message || t("ciPreviewFailed"));
    } finally {
      setPreviewLoading(false);
    }
  };

  const save = async () => {
    if (!form.name.trim() || !form.key.trim() || !form.formula.trim()) {
      message.warning(t("ciNameKeyFormulaRequired"));
      return;
    }
    setSaving(true);
    try {
      if (selectedId) {
        const updated = await api.updateCustomIndicator(selectedId, form);
        message.success(t("ciIndicatorSaved"));
        setRows((prev) => prev.map((row) => (row.id === selectedId ? updated : row)));
      } else {
        const created = await api.createCustomIndicator(form);
        message.success(t("ciIndicatorCreated"));
        setRows((prev) => [created, ...prev]);
        setSelectedId(created.id);
      }
    } catch (error: any) {
      message.error(error?.message || t("ciSaveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id: number) => {
    try {
      await api.deleteCustomIndicator(id);
      message.success(t("ciIndicatorDeleted"));
      setRows((prev) => prev.filter((row) => row.id !== id));
      if (selectedId === id) startCreate();
    } catch (error: any) {
      message.error(error?.message || t("ciDeleteFailed"));
    }
  };

  return (
    <div className="indicator-settings-grid indicator-settings-grid--formula">
      <Card
        className="indicator-card indicator-card--library"
        size="small"
        title={t("ciFormulaLibrary")}
        extra={(
          <Space size={8} className="indicator-card__toolbar">
            <Tag>{rows.length} {t("items")}</Tag>
            <Button size="small" icon={<ReloadOutlined />} onClick={loadRows}>{t("refresh")}</Button>
            <Button size="small" icon={<PlusOutlined />} onClick={startCreate}>{t("ciNew")}</Button>
          </Space>
        )}
      >
        <Table<CustomIndicator>
          className="indicator-library-table"
          size="small"
          rowKey="id"
          loading={loading}
          dataSource={rows}
          pagination={{ pageSize: 8 }}
          scroll={{ x: 640 }}
          onRow={(record) => ({ onClick: () => loadIndicator(record) })}
          rowClassName={(record) => (record.id === selectedId ? "selected-row" : "")}
          columns={[
            {
              title: t("ciName"),
              dataIndex: "name",
              render: (value, row) => <Space size={6}><span>{value}</span>{!row.enabled && <Tag>{t("ciDisabled")}</Tag>}</Space>,
            },
            { title: t("ciKey"), dataIndex: "key" },
            { title: t("ciScope"), dataIndex: "scope", render: (scope: string[]) => (scope || []).map((item) => <Tag key={item}>{scopeOptions.find((opt) => opt.value === item)?.label ?? item}</Tag>) },
            { title: t("ciVersion"), dataIndex: "version", width: 64 },
          ]}
        />
      </Card>

      <Card className="indicator-card indicator-card--editor" size="small" title={selected ? `${t("ciEditIndicator")}${selected.name}` : t("ciNewIndicator")}>
        <Form layout="vertical">
          <div className="indicator-editor-head">
            <Space wrap className="indicator-editor-meta">
              <Tag>{editorMeta.category}</Tag>
              <Tag color={form.value_type === "number" ? "gold" : "blue"}>{editorMeta.valueType}</Tag>
              <Tag color={form.enabled ? "green" : "default"}>{form.enabled ? t("ciEnabled") : t("ciDisabled")}</Tag>
              {selected && <Tag>{t("ciVersion")} {selected.version}</Tag>}
            </Space>
          </div>
          <div className="item-subline indicator-editor-summary">
            {t("ciKey")}: {form.key || "-"} {selected ? `· ID ${selected.id}` : `· ${t("ciNew")}`} · {t("ciScope")}: {form.scope.length} {t("items")}
          </div>
          <div className="indicator-form-grid">
            <Form.Item label={t("ciName")}>
              <Input
                value={form.name}
                onChange={(event) => {
                  const name = event.target.value;
                  setForm((prev) => ({ ...prev, name, key: prev.key || keyFromName(name) }));
                }}
                placeholder={t("ciNamePlaceholder")}
              />
            </Form.Item>
            <Form.Item label={t("ciKey")}>
              <Input value={form.key} onChange={(event) => setForm((prev) => ({ ...prev, key: event.target.value }))} placeholder="strong_trend_filter" />
            </Form.Item>
            <Form.Item label={t("ciCategory")}>
              <Select value={form.category} options={categoryOptions} onChange={(value) => setForm((prev) => ({ ...prev, category: value }))} />
            </Form.Item>
            <Form.Item label={t("ciReturnType")}>
              <Select
                value={form.value_type}
                options={[{ label: t("ciBoolean"), value: "boolean" }, { label: t("ciNumber"), value: "number" }]}
                onChange={(value) => setForm((prev) => ({ ...prev, value_type: value }))}
              />
            </Form.Item>
          </div>
          <Form.Item label={t("ciDescription")}>
            <Input value={form.description} onChange={(event) => setForm((prev) => ({ ...prev, description: event.target.value }))} />
          </Form.Item>
          <Form.Item label={t("ciFormula")}>
            <Input.TextArea
              rows={5}
              value={form.formula}
              onChange={(event) => setForm((prev) => ({ ...prev, formula: event.target.value }))}
              placeholder="sma(20) > sma(60) and rsi(14) < 70"
            />
          </Form.Item>
          <div className="indicator-help">
            {t("ciAvailableFunctions")}sma/ema/rsi/macd/macd_signal/macd_hist/boll_upper/boll_mid/boll_lower/atr/kdj_k/kdj_d/kdj_j/highest/lowest/ref/pct_change/volume_ratio/cross_over/cross_under/abs/min/max/round。
          </div>
          <Card className="indicator-preview-card" size="small" title={t("ciFormulaPreview")} style={{ marginBottom: 16 }}>
            <div className="indicator-preview-panel">
              <div className="indicator-form-grid compact">
                <Form.Item label={t("ciPreviewSymbol")} style={{ marginBottom: 0 }}>
                  <Select
                    showSearch
                    allowClear
                    placeholder={t("ciSearchCodeOrName")}
                    value={previewSymbolId ?? undefined}
                    filterOption={false}
                    onSearch={(value) => { searchPreviewSymbols(value).catch(() => {}); }}
                    onChange={(value) => setPreviewSymbolId(value ?? null)}
                    options={previewSymbolOptions.map((item) => ({ label: `${item.symbol} | ${item.name}`, value: item.id }))}
                  />
                </Form.Item>
                <Form.Item label={t("ciPreviewDate")} style={{ marginBottom: 0 }}>
                  <DatePicker value={previewTradeDate} onChange={setPreviewTradeDate} placeholder={t("ciDefaultLatestBar")} allowClear />
                </Form.Item>
              </div>
              <div className="item-subline">
                {previewSymbol
                  ? `${t("ciPreviewCurrentLabel")}${previewSymbol.symbol} | ${previewSymbol.name}${previewTradeDate ? `${t("ciPreviewDateLabel")}${previewTradeDate.format("YYYY-MM-DD")}` : t("ciPreviewLatestBar")}`
                  : t("ciPreviewHint")}
              </div>
              <Space wrap className="indicator-preview-actions">
                {activeSymbol && (
                  <Button onClick={() => setPreviewSymbolId(activeSymbol.id)}>
                    {t("ciUseCurrentSymbol")}
                  </Button>
                )}
                <Button icon={<EyeOutlined />} loading={previewLoading} onClick={previewFormula} disabled={!previewSymbolId}>
                  {t("ciRunPreview")}
                </Button>
                {previewResult && <Tag color="blue">{t("ciBarDate")} {previewResult.trade_date}</Tag>}
                {previewResult && <Tag color={previewResult.value_type === "number" ? "gold" : "green"}>{previewResult.value_type === "number" ? t("ciNumber") : t("ciBoolean")}</Tag>}
              </Space>
              {previewResult ? (
                <div className="indicator-preview-grid">
                  <div>
                    <div className="metric-label">{t("ciPreviewResult")}</div>
                    <strong>{previewValue(previewResult)}</strong>
                  </div>
                  <div>
                    <div className="metric-label">{t("ciClose")}</div>
                    <strong>{scoreValue(previewResult.latest_bar.close)}</strong>
                  </div>
                  <div>
                    <div className="metric-label">{t("ciOpen")}</div>
                    <strong>{scoreValue(previewResult.latest_bar.open)}</strong>
                  </div>
                  <div>
                    <div className="metric-label">{t("ciQualityScore")}</div>
                    <strong>{scoreValue(previewResult.score_snapshot?.quality_score)}</strong>
                  </div>
                  <div>
                    <div className="metric-label">{t("ciTimingScore")}</div>
                    <strong>{scoreValue(previewResult.score_snapshot?.timing_score)}</strong>
                  </div>
                  <div>
                    <div className="metric-label">{t("ciTrendScore")}</div>
                    <strong>{scoreValue(previewResult.score_snapshot?.trend_score)}</strong>
                  </div>
                  <div>
                    <div className="metric-label">{t("ciMomentumScore")}</div>
                    <strong>{scoreValue(previewResult.score_snapshot?.momentum_score)}</strong>
                  </div>
                </div>
              ) : (
                <div className="item-subline">{t("ciPreviewExplanation")}</div>
              )}
            </div>
          </Card>
          <div className="indicator-form-grid compact">
            <Form.Item label={t("ciScope")}>
              <Checkbox.Group
                options={scopeOptions}
                value={form.scope}
                onChange={(value) => setForm((prev) => ({ ...prev, scope: value as string[] }))}
              />
            </Form.Item>
            <Form.Item label={t("ciEnabled")}>
              <Switch checked={form.enabled} onChange={(checked) => setForm((prev) => ({ ...prev, enabled: checked }))} />
            </Form.Item>
          </div>

          {selected && (
            <Card className="indicator-usage-card" size="small" title={t("ciUsage")} style={{ marginBottom: 16 }}>
              <div className="indicator-usage-grid">
                <div>
                  <div className="metric-label">{t("ciDiscoveryPlan")}</div>
                  <Space wrap className="indicator-preview-actions">
                    {usageSummary.discovery.length > 0
                      ? usageSummary.discovery.map((plan) => <Tag key={`plan_${plan.id}`}>{plan.name}</Tag>)
                      : <span className="item-subline">{t("ciNoReferences")}</span>}
                  </Space>
                </div>
                <div>
                  <div className="metric-label">{t("ciBacktestTemplate")}</div>
                  <Space wrap className="indicator-preview-actions">
                    {usageSummary.backtest.length > 0
                      ? usageSummary.backtest.map((template) => <Tag key={`tpl_${template.id}`} color="blue">{template.name}</Tag>)
                      : <span className="item-subline">{t("ciNoReferences")}</span>}
                  </Space>
                </div>
              </div>
            </Card>
          )}

          <Space wrap className="indicator-action-row">
            <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={save}>{t("ciSaveIndicator")}</Button>
            {selectedId && (
              <Popconfirm title={t("ciConfirmDeleteIndicator")} onConfirm={() => remove(selectedId)}>
                <Button danger icon={<DeleteOutlined />}>{t("ciDelete")}</Button>
              </Popconfirm>
            )}
          </Space>
        </Form>
      </Card>
    </div>
  );
}
