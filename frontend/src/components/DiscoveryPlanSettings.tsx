import { useEffect, useMemo, useState } from "react";
import { Button, Card, Empty, Form, Input, InputNumber, Popconfirm, Select, Space, Switch, Table, Tag, Tooltip, message } from "antd";
import { CopyOutlined, DeleteOutlined, PlusOutlined, ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { OPERATOR_LABELS } from "../constants/conditionFields";
import { t, template } from "../i18n";
import type { CustomIndicator, DiscoveryPlan, DiscoveryPlanFilter, DiscoveryPlanPayload } from "../types";

const NUMBER_OPERATORS: DiscoveryPlanFilter["operator"][] = ["gt", "gte", "lt", "lte", "eq", "neq"];
const BOOLEAN_OPERATORS: DiscoveryPlanFilter["operator"][] = ["eq", "neq"];
const EMPTY_FORM: DiscoveryPlanPayload = {
  name: "",
  logic: "AND",
  pool_tab: "actionable",
  filters: [],
};

function makeId(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
}

function defaultOperator(valueType?: CustomIndicator["value_type"]): DiscoveryPlanFilter["operator"] {
  return valueType === "number" ? "gte" : "eq";
}

function makeEmptyFilter(defaultIndicator?: CustomIndicator): DiscoveryPlanFilter {
  return {
    id: makeId("filter"),
    indicator_key: defaultIndicator?.key,
    operator: defaultOperator(defaultIndicator?.value_type),
    number_value: 0,
    boolean_value: true,
  };
}

function normalizeOperator(operator: DiscoveryPlanFilter["operator"] | undefined, valueType?: CustomIndicator["value_type"]) {
  const options = valueType === "number" ? NUMBER_OPERATORS : BOOLEAN_OPERATORS;
  const fallback = defaultOperator(valueType);
  return options.includes(operator ?? fallback) ? (operator ?? fallback) : fallback;
}

export default function DiscoveryPlanSettings() {
  const [rows, setRows] = useState<DiscoveryPlan[]>([]);
  const [customIndicators, setCustomIndicators] = useState<CustomIndicator[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [form, setForm] = useState<DiscoveryPlanPayload>(EMPTY_FORM);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);

  const POOL_OPTIONS: Array<{ label: string; value: DiscoveryPlan["pool_tab"] }> = [
    { label: t("dpPoolAll"), value: "all" },
    { label: t("dpPoolHighQuality"), value: "highQuality" },
    { label: t("dpPoolHighTiming"), value: "highTiming" },
    { label: t("dpPoolActionable"), value: "actionable" },
    { label: t("dpPoolOverheatRisk"), value: "overheatRisk" },
    { label: t("dpPoolLowCredibility"), value: "lowCredibility" },
  ];
  const logicOptions = [
    { label: t("dpLogicAnd"), value: "AND" },
    { label: t("dpLogicOr"), value: "OR" },
  ];

  const indicatorMap = useMemo(() => {
    return customIndicators.reduce<Record<string, CustomIndicator>>((acc, item) => {
      acc[item.key] = item;
      return acc;
    }, {});
  }, [customIndicators]);
  const defaultIndicator = customIndicators[0];
  const selected = useMemo(() => rows.find((row) => row.id === selectedId) ?? null, [rows, selectedId]);
  const usedIndicators = useMemo(() => {
    const keys = Array.from(new Set((form.filters || []).map((filter) => filter.indicator_key).filter(Boolean) as string[]));
    return keys.map((key) => indicatorMap[key]).filter(Boolean);
  }, [form.filters, indicatorMap]);

  const loadRows = async () => {
    setLoading(true);
    try {
      const [plans, indicators] = await Promise.all([
        api.getDiscoveryPlans(),
        api.getCustomIndicators({ scope: "discovery" }),
      ]);
      setRows(plans as DiscoveryPlan[]);
      setCustomIndicators(indicators as CustomIndicator[]);
    } catch (error: any) {
      message.error(error?.message || t("dpLoadFailed"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadRows();
  }, []);

  useEffect(() => {
    if (!selectedId && form.filters.length === 0 && defaultIndicator) {
      setForm((prev) => (prev.filters.length === 0 ? { ...prev, filters: [makeEmptyFilter(defaultIndicator)] } : prev));
    }
  }, [defaultIndicator, form.filters.length, selectedId]);

  useEffect(() => {
    const nextFilters = form.filters.map((filter) => {
      const indicator = filter.indicator_key ? indicatorMap[filter.indicator_key] : undefined;
      const operator = normalizeOperator(filter.operator, indicator?.value_type);
      return operator === filter.operator ? filter : { ...filter, operator };
    });
    if (nextFilters.some((filter, index) => filter !== form.filters[index])) {
      setForm((prev) => ({ ...prev, filters: nextFilters }));
    }
  }, [form.filters, indicatorMap]);

  const startCreate = () => {
    setSelectedId(null);
    setForm({ ...EMPTY_FORM, filters: [makeEmptyFilter(defaultIndicator)] });
  };

  const loadPlan = (row: DiscoveryPlan) => {
    setSelectedId(row.id);
    setForm({
      name: row.name,
      logic: row.logic,
      pool_tab: row.pool_tab,
      filters: (row.filters?.length ? row.filters : [makeEmptyFilter(defaultIndicator)]).map((filter) => {
        const indicator = filter.indicator_key ? indicatorMap[filter.indicator_key] : undefined;
        return {
          ...filter,
          operator: normalizeOperator(filter.operator, indicator?.value_type),
        };
      }),
    });
  };

  const updateFilter = (filterId: string, updater: (filter: DiscoveryPlanFilter) => DiscoveryPlanFilter) => {
    setForm((prev) => ({
      ...prev,
      filters: prev.filters.map((filter) => (filter.id === filterId ? updater(filter) : filter)),
    }));
  };

  const addFilter = () => {
    setForm((prev) => ({ ...prev, filters: [...prev.filters, makeEmptyFilter(defaultIndicator)] }));
  };

  const removeFilter = (filterId: string) => {
    setForm((prev) => {
      const next = prev.filters.filter((filter) => filter.id !== filterId);
      return { ...prev, filters: next.length ? next : [makeEmptyFilter(defaultIndicator)] };
    });
  };

  const handleIndicatorChange = (filterId: string, indicatorKey?: string) => {
    updateFilter(filterId, (filter) => {
      const indicator = indicatorKey ? indicatorMap[indicatorKey] : undefined;
      return {
        ...filter,
        indicator_key: indicatorKey,
        operator: normalizeOperator(filter.operator, indicator?.value_type),
      };
    });
  };

  const save = async () => {
    if (!form.name.trim()) {
      setNameError(t("dpNameRequired"));
      message.warning(t("dpNameRequired"));
      return;
    }
    setNameError(null);
    setSaving(true);
    try {
      const payload: DiscoveryPlanPayload = {
        name: form.name.trim(),
        logic: form.logic,
        pool_tab: form.pool_tab,
        filters: form.filters,
      };
      const saved = selectedId
        ? await api.updateDiscoveryPlan(selectedId, payload)
        : await api.createDiscoveryPlan(payload);
      const row = saved as DiscoveryPlan;
      setRows((prev) => [row, ...prev.filter((item) => item.id !== row.id)]);
      setSelectedId(row.id);
      loadPlan(row);
      message.success(t("dpPlanSaved"));
    } catch (error: any) {
      message.error(error?.message || t("dpSaveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const duplicate = async () => {
    const name = `${form.name || selected?.name || t("dpName")} ${t("dpCopySuffix")}`;
    setSaving(true);
    try {
      const saved = await api.createDiscoveryPlan({
        ...form,
        name,
        filters: form.filters.map((filter) => ({ ...filter, id: makeId("filter") })),
      });
      const row = saved as DiscoveryPlan;
      setRows((prev) => [row, ...prev]);
      setSelectedId(row.id);
      loadPlan(row);
      message.success(t("dpPlanCopied"));
    } catch (error: any) {
      message.error(error?.message || t("dpCopyFailed"));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id: number) => {
    try {
      await api.deleteDiscoveryPlan(id);
      message.success(t("dpPlanDeleted"));
      setRows((prev) => prev.filter((row) => row.id !== id));
      if (selectedId === id) startCreate();
    } catch (error: any) {
      message.error(error?.message || t("dpDeleteFailed"));
    }
  };

  return (
    <div className="indicator-settings-grid indicator-settings-grid--plans">
      <Card
        className="indicator-card indicator-card--library"
        size="small"
        title={t("dpDiscoveryPlans")}
        extra={<Space size={8} className="indicator-card__toolbar"><Tag>{rows.length} {t("items")}</Tag><Button size="small" icon={<ReloadOutlined />} onClick={loadRows} aria-label={t("refresh")}>{t("refresh")}</Button><Button size="small" icon={<PlusOutlined />} onClick={startCreate} aria-label={t("dpNew")}>{t("dpNew")}</Button></Space>}
      >
        <Table<DiscoveryPlan>
          className="indicator-library-table"
          size="small"
          rowKey="id"
          loading={loading}
          dataSource={rows}
          pagination={{ pageSize: 8 }}
          scroll={{ x: 760 }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("dpEmptyPlans")} /> }}
          onRow={(record) => ({
            onClick: () => loadPlan(record),
            onKeyDown: (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                loadPlan(record);
              }
            },
            tabIndex: 0,
            role: "button",
            "aria-label": template("dpLoadPlanAria", { name: record.name }),
          })}
          rowClassName={(record) => (record.id === selectedId ? "selected-row" : "")}
          columns={[
            { title: t("dpName"), dataIndex: "name" },
            { title: t("dpLogic"), dataIndex: "logic", width: 110, render: (value: string) => <Tag>{logicOptions.find((item) => item.value === value)?.label ?? value}</Tag> },
            { title: t("dpPool"), dataIndex: "pool_tab", width: 130, render: (value: DiscoveryPlan["pool_tab"]) => POOL_OPTIONS.find((item) => item.value === value)?.label ?? value },
            { title: t("dpRuleCount"), dataIndex: "filters", width: 80, render: (filters: DiscoveryPlanFilter[]) => filters?.length ?? 0 },
            { title: t("dpUpdatedTime"), dataIndex: "updated_at", width: 180, render: (value: string) => value ? new Date(value).toLocaleString() : "-" },
            {
              title: t("operations"),
              key: "actions",
              width: 120,
              render: (_, row) => (
                <Popconfirm title={t("dpConfirmDeletePlan")} onConfirm={() => remove(row.id)}>
                  <Button danger size="small" icon={<DeleteOutlined />}>{t("dpDelete")}</Button>
                </Popconfirm>
              ),
            },
          ]}
        />
      </Card>

      <Card className="indicator-card indicator-card--editor" size="small" title={selected ? template("dpEditPlan", { name: selected.name }) : t("dpNewPlan")}>
        <Form layout="vertical" className="discovery-plan-form">
          <div className="indicator-editor-head">
            <Space wrap className="indicator-editor-meta">
              <Tag>{logicOptions.find((item) => item.value === form.logic)?.label ?? form.logic}</Tag>
              <Tag>{POOL_OPTIONS.find((item) => item.value === form.pool_tab)?.label ?? form.pool_tab}</Tag>
              <Tag color="blue">{form.filters.length} {t("items")}</Tag>
            </Space>
          </div>
          <div className="item-subline indicator-editor-summary discovery-plan-summary">{template("dpEnabledIndicators", { count: customIndicators.length })}</div>
          <div className="indicator-form-grid discovery-plan-grid">
            <Form.Item label={t("dpName")} required validateStatus={nameError ? "error" : ""} help={nameError}>
              <Input
                value={form.name}
                onChange={(event) => {
                  setForm((prev) => ({ ...prev, name: event.target.value }));
                  if (nameError) setNameError(null);
                }}
                placeholder={t("dpNamePlaceholder")}
                aria-label={t("dpName")}
              />
            </Form.Item>
            <Form.Item label={t("dpLogic")}>
              <Select value={form.logic} options={logicOptions} onChange={(value) => setForm((prev) => ({ ...prev, logic: value }))} aria-label={t("dpLogic")} />
            </Form.Item>
            <Form.Item label={t("dpOpportunityPool")}>
              <Select value={form.pool_tab} options={POOL_OPTIONS} onChange={(value) => setForm((prev) => ({ ...prev, pool_tab: value }))} aria-label={t("dpOpportunityPool")} />
            </Form.Item>
          </div>

          {usedIndicators.length > 0 && (
            <Card className="indicator-usage-card" size="small" title={t("dpCurrentIndicators")} style={{ marginBottom: 16 }}>
              <Space wrap>
                {usedIndicators.map((indicator) => (
                  <Tag key={indicator.key} color={indicator.value_type === "number" ? "gold" : "blue"}>{indicator.name}</Tag>
                ))}
              </Space>
            </Card>
          )}

          <div className="discovery-filter-list">
            {form.filters.map((filter, index) => {
              const indicator = filter.indicator_key ? indicatorMap[filter.indicator_key] : undefined;
              const operatorOptions = indicator?.value_type === "number" ? NUMBER_OPERATORS : BOOLEAN_OPERATORS;
              return (
                <div key={filter.id} className="discovery-filter-card">
                  <div className="discovery-filter-head">
                    <div className="discovery-filter-title">
                      <strong>{template("dpRuleN", { n: index + 1 })}</strong>
                      {indicator && <Tag color={indicator.value_type === "number" ? "gold" : "blue"}>{indicator.name}</Tag>}
                    </div>
                    <Tooltip title={t("dpDeleteRule")}>
                      <Button
                        type="text"
                        danger
                        size="small"
                        icon={<DeleteOutlined />}
                        aria-label={template("dpDeleteRuleAria", { n: index + 1 })}
                        onClick={() => removeFilter(filter.id)}
                      />
                    </Tooltip>
                  </div>
                  <div className="discovery-filter-grid">
                    <label className="inline-control inline-control--wide discovery-filter-control discovery-filter-control--wide">
                      <span>{t("dpSelectIndicator")}</span>
                      <Select allowClear placeholder={t("dpSelectIndicator")} value={filter.indicator_key} onChange={(value) => handleIndicatorChange(filter.id, value)} options={customIndicators.map((item) => ({ label: item.name, value: item.key }))} aria-label={t("dpSelectIndicator")} />
                    </label>
                    <label className="inline-control discovery-filter-control">
                      <span>{t("dpOperator")}</span>
                      <Select value={filter.operator} onChange={(value) => updateFilter(filter.id, (current) => ({ ...current, operator: value }))} options={operatorOptions.map((item) => ({ label: OPERATOR_LABELS[item] ? t(OPERATOR_LABELS[item]) : item, value: item }))} disabled={!indicator} aria-label={t("dpOperator")} />
                    </label>
                    {indicator?.value_type === "number" ? (
                      <label className="inline-control discovery-filter-control">
                        <span>{t("dpThreshold")}</span>
                        <InputNumber className="discovery-filter-number" value={filter.number_value} onChange={(value) => updateFilter(filter.id, (current) => ({ ...current, number_value: Number(value ?? 0) }))} aria-label={t("dpThreshold")} />
                      </label>
                    ) : (
                      <label className="inline-control discovery-filter-control">
                        <span>{t("dpTargetValue")}</span>
                        <Switch checked={filter.boolean_value} checkedChildren={t("yes")} unCheckedChildren={t("no")} onChange={(value) => updateFilter(filter.id, (current) => ({ ...current, boolean_value: value }))} disabled={!indicator} aria-label={t("dpTargetValue")} />
                      </label>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          <Space wrap className="indicator-action-row">
            <Button icon={<PlusOutlined />} onClick={addFilter} disabled={customIndicators.length === 0} aria-label={t("dpAddRule")}>{t("dpAddRule")}</Button>
            <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={save} aria-label={t("dpSavePlan")}>{t("dpSavePlan")}</Button>
            <Button icon={<CopyOutlined />} loading={saving} onClick={duplicate} aria-label={t("dpCopy")}>{t("dpCopy")}</Button>
            {selectedId && (
              <Popconfirm title={t("dpConfirmDeletePlan")} onConfirm={() => remove(selectedId)}>
                <Button danger icon={<DeleteOutlined />} aria-label={t("dpDelete")}>{t("dpDelete")}</Button>
              </Popconfirm>
            )}
          </Space>
        </Form>
      </Card>
    </div>
  );
}
