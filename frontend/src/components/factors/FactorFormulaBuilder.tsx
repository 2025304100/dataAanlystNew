import { Alert, Button, Empty, Input, Skeleton, Space, Tabs, Tag, Tooltip, Typography } from "antd";
import {
  AppstoreOutlined,
  BarChartOutlined,
  DatabaseOutlined,
  FunctionOutlined,
  ReloadOutlined,
  SearchOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { useMemo, useState } from "react";
import type {
  FactorFormulaCatalog,
  FactorFormulaCatalogField,
  FactorFormulaCatalogFunction,
  FactorFormulaCatalogOperator,
  FactorFormulaCatalogTemplate,
} from "../../api/client";

export type FormulaInspectorItem =
  | ({ kind: "field" } & FactorFormulaCatalogField)
  | ({ kind: "function" } & FactorFormulaCatalogFunction)
  | ({ kind: "operator" } & FactorFormulaCatalogOperator)
  | ({ kind: "template" } & FactorFormulaCatalogTemplate);

type FactorFormulaBuilderProps = {
  isZh: boolean;
  catalog: FactorFormulaCatalog | null;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  onInsert: (snippet: string) => void;
  onUseExample: (formula: string) => void;
  onInspect?: (item: FormulaInspectorItem) => void;
};

function containsKeyword(values: Array<unknown>, keyword: string) {
  if (!keyword) return true;
  return values.join(" ").toLowerCase().includes(keyword);
}

function availabilityMeta(field: FactorFormulaCatalogField, isZh: boolean) {
  const labels = {
    available: isZh ? "可评价" : "Ready",
    limited: isZh ? "覆盖受限" : "Limited",
    event: isZh ? "仅事件" : "Event",
    snapshot: isZh ? "仅快照" : "Snapshot",
    blocked: isZh ? "缺数据" : "Blocked",
    unknown: isZh ? "待检查" : "Unknown",
  };
  const colors: Record<string, string> = {
    available: "success",
    limited: "warning",
    event: "processing",
    snapshot: "purple",
    blocked: "error",
    unknown: "default",
  };
  return {
    label: labels[field.availability] ?? field.availability,
    color: colors[field.availability] ?? "default",
  };
}

function functionIcon(category: string) {
  if (category === "rolling") return <BarChartOutlined />;
  if (category === "cross_section") return <AppstoreOutlined />;
  return <FunctionOutlined />;
}

export default function FactorFormulaBuilder({
  isZh,
  catalog,
  loading = false,
  error,
  onRetry,
  onInsert,
  onUseExample,
  onInspect,
}: FactorFormulaBuilderProps) {
  const [keyword, setKeyword] = useState("");
  const normalizedKeyword = keyword.trim().toLowerCase();

  const fields = useMemo(
    () => (catalog?.fields ?? []).filter((item) => containsKeyword([
      item.key,
      item.label_zh,
      item.label_en,
      item.description,
      item.dtype,
      item.source_table,
      item.availability,
      item.status_reason,
    ], normalizedKeyword)),
    [catalog, normalizedKeyword],
  );

  const functions = useMemo(
    () => [...(catalog?.functions ?? []), ...(catalog?.disabled_functions ?? [])].filter((item) =>
      containsKeyword([
        item.key,
        item.label_zh,
        item.label_en,
        item.description,
        item.signature,
        item.category,
      ], normalizedKeyword)),
    [catalog, normalizedKeyword],
  );

  const operators = useMemo(
    () => (catalog?.operators ?? []).filter((item) =>
      containsKeyword([item.key, item.label, item.description, item.snippet], normalizedKeyword)),
    [catalog, normalizedKeyword],
  );

  const templates = useMemo(
    () => (catalog?.templates ?? []).filter((item) =>
      containsKeyword([item.key, item.name_zh, item.name_en, item.formula], normalizedKeyword)),
    [catalog, normalizedKeyword],
  );

  const inspect = (item: FormulaInspectorItem) => {
    onInspect?.(item);
  };

  if (loading && !catalog) {
    return <Skeleton active paragraph={{ rows: 12 }} className="formula-builder-loading" />;
  }

  if (error && !catalog) {
    return (
      <Alert
        type="error"
        showIcon
        message={isZh ? "公式能力目录加载失败" : "Formula catalog failed to load"}
        description={error}
        action={onRetry ? (
          <Button size="small" icon={<ReloadOutlined />} onClick={onRetry}>
            {isZh ? "重试" : "Retry"}
          </Button>
        ) : null}
        className="formula-builder-error"
      />
    );
  }

  const fieldPanel = fields.length ? (
    <div className="formula-catalog-list">
      {fields.map((item) => {
        const inspector: FormulaInspectorItem = { ...item, kind: "field" };
        const availability = availabilityMeta(item, isZh);
        const canDraft = item.draft_enabled !== false;
        return (
          <Tooltip
            key={item.key}
            mouseEnterDelay={0.25}
            title={
              <div className="formula-hover-card">
                <strong>{isZh ? item.label_zh : item.label_en}</strong>
                <code>{item.key}</code>
                <span>{item.description}</span>
                <span>{item.status_reason}</span>
                <small>
                  {item.dtype} · {item.source_table}
                  {item.first_date || item.latest_date ? ` · ${item.first_date ?? "?"} → ${item.latest_date ?? "?"}` : ""}
                </small>
              </div>
            }
          >
            <button
              type="button"
              className={`formula-catalog-item availability-${item.availability}`}
              aria-label={`${item.key} ${isZh ? item.label_zh : item.label_en}`}
              aria-disabled={!canDraft}
              onMouseEnter={() => inspect(inspector)}
              onFocus={() => inspect(inspector)}
              onClick={() => {
                inspect(inspector);
                if (canDraft) onInsert(item.snippet);
              }}
            >
              <span className="formula-catalog-item-icon field"><DatabaseOutlined /></span>
              <span className="formula-catalog-item-copy">
                <strong>{isZh ? item.label_zh : item.label_en}</strong>
                <code>{item.key}</code>
              </span>
              <Tag color={availability.color}>{availability.label}</Tag>
            </button>
          </Tooltip>
        );
      })}
    </div>
  ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={isZh ? "没有匹配字段" : "No matching fields"} />;

  const functionPanel = functions.length ? (
    <div className="formula-catalog-list">
      {functions.map((item) => {
        const inspector: FormulaInspectorItem = { ...item, kind: "function" };
        const disabled = item.enabled === false;
        return (
          <Tooltip
            key={item.key}
            mouseEnterDelay={0.25}
            title={
              <div className="formula-hover-card">
                <strong>{isZh ? item.label_zh : item.label_en}</strong>
                <code>{item.signature}</code>
                <span>{disabled ? item.disabled_reason : item.description}</span>
                {item.window_arg_index !== null && item.window_arg_index !== undefined ? (
                  <small>{isZh ? "包含交易日窗口参数" : "Contains a trading-day window"}</small>
                ) : null}
              </div>
            }
          >
            <button
              type="button"
              className={`formula-catalog-item ${disabled ? "disabled" : ""}`}
              aria-label={`${item.key} ${isZh ? item.label_zh : item.label_en}`}
              aria-disabled={disabled}
              onMouseEnter={() => inspect(inspector)}
              onFocus={() => inspect(inspector)}
              onClick={() => {
                inspect(inspector);
                if (!disabled) onInsert(item.snippet);
              }}
            >
              <span className="formula-catalog-item-icon function">{functionIcon(item.category)}</span>
              <span className="formula-catalog-item-copy">
                <strong>{isZh ? item.label_zh : item.label_en}</strong>
                <code>{item.signature}</code>
              </span>
              {disabled ? <Tag>{isZh ? "暂不可用" : "Unavailable"}</Tag> : <Tag color="green">{item.category}</Tag>}
            </button>
          </Tooltip>
        );
      })}
    </div>
  ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={isZh ? "没有匹配函数" : "No matching functions"} />;

  const operatorPanel = operators.length ? (
    <div className="formula-operator-grid">
      {operators.map((item) => {
        const inspector: FormulaInspectorItem = { ...item, kind: "operator" };
        return (
          <Tooltip key={item.key} mouseEnterDelay={0.25} title={item.description}>
            <Button
              onMouseEnter={() => inspect(inspector)}
              onFocus={() => inspect(inspector)}
              onClick={() => onInsert(item.snippet)}
            >
              <strong>{item.label}</strong>
              <span>{item.description}</span>
            </Button>
          </Tooltip>
        );
      })}
    </div>
  ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={isZh ? "没有匹配运算符" : "No matching operators"} />;

  const templatePanel = templates.length ? (
    <div className="formula-template-list">
      {templates.map((item) => {
        const inspector: FormulaInspectorItem = { ...item, kind: "template" };
        return (
          <div
            key={item.key}
            className="formula-template-card"
            onMouseEnter={() => inspect(inspector)}
          >
            <Space direction="vertical" size={4}>
              <Typography.Text strong>{isZh ? item.name_zh : item.name_en}</Typography.Text>
              <Typography.Text code>{item.formula}</Typography.Text>
              <Button size="small" type="link" onFocus={() => inspect(inspector)} onClick={() => onUseExample(item.formula)}>
                {isZh ? "使用模板" : "Use template"}
              </Button>
            </Space>
          </div>
        );
      })}
    </div>
  ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={isZh ? "没有匹配模板" : "No matching templates"} />;

  return (
    <div className="formula-builder-redesign">
      <div className="formula-builder-search">
        <Input
          allowClear
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          prefix={<SearchOutlined />}
          placeholder={isZh ? "搜索字段、函数、运算符或模板" : "Search fields, functions, operators, or templates"}
        />
      </div>
      <Tabs
        size="small"
        className="formula-catalog-tabs"
        items={[
          { key: "fields", label: `${isZh ? "字段" : "Fields"} (${catalog?.fields.length ?? 0})`, children: fieldPanel },
          { key: "functions", label: `${isZh ? "函数" : "Functions"} (${(catalog?.functions.length ?? 0) + (catalog?.disabled_functions.length ?? 0)})`, children: functionPanel },
          { key: "operators", label: isZh ? "运算" : "Operators", children: operatorPanel },
          { key: "templates", label: isZh ? "模板" : "Templates", children: templatePanel },
        ]}
      />
      <div className="formula-builder-footer">
        <span><ThunderboltOutlined /> {isZh ? "点击后插入当前光标位置" : "Insert at the current cursor"}</span>
        <Tag>{catalog?.dsl_version ? `DSL ${catalog.dsl_version}` : "DSL"}</Tag>
      </div>
    </div>
  );
}
