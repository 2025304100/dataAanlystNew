import { useCallback, useMemo } from "react";
import { Button, Input, InputNumber, Select, Space, Switch, Segmented, Tag } from "antd";
import { PlusOutlined, DeleteOutlined } from "@ant-design/icons";
import type {
  ConditionGroup,
  ConditionLeaf,
  ConditionOperator,
  ConditionFieldDef,
  CustomIndicator,
  LogicOperator,
} from "../types";
import { isConditionGroup } from "../types";
import { t, template } from "../i18n";
import {
  CONDITION_FIELDS,
  OPERATOR_LABELS,
  CATEGORY_LABELS,
  STAGE_OPTIONS,
  ACTION_OPTIONS,
} from "../constants/conditionFields";

interface ConditionBuilderProps {
  value: ConditionGroup;
  onChange: (value: ConditionGroup) => void;
  side: "buy" | "sell";
  depth?: number;
  maxDepth?: number;
  customIndicators?: CustomIndicator[];
}

const GROUP_BG: Record<LogicOperator, string> = {
  AND: "#e6f7ff",
  OR: "#f6ffed",
};

const GROUP_BORDER: Record<LogicOperator, string> = {
  AND: "#91d5ff",
  OR: "#b7eb8f",
};

function getCustomIndicatorMeta(customIndicators: CustomIndicator[] = [], indicatorKey?: string | number) {
  const key = typeof indicatorKey === "string" ? indicatorKey : "";
  return customIndicators.find((item) => item.key === key) ?? customIndicators[0];
}

function buildCustomIndicatorField(customIndicators: CustomIndicator[] = [], indicatorKey?: string | number): ConditionFieldDef {
  const indicator = getCustomIndicatorMeta(customIndicators, indicatorKey);
  const isNumber = indicator?.value_type === "number";
  return {
    key: "custom_indicator",
    label: t("cbCustomIndicator"),
    category: "technical",
    valueType: isNumber ? "number" : "boolean",
    operators: isNumber ? ["gt", "gte", "lt", "lte", "eq", "neq"] : ["eq", "neq"],
    requiresHistory: true,
    side: "both",
    params: [{ key: "indicator_key", label: t("cbIndicator"), type: "select", default: indicator?.key ?? "" }],
  };
}

function getFieldDef(
  key: string,
  customIndicators: CustomIndicator[] = [],
  indicatorKey?: string | number,
): ConditionFieldDef | undefined {
  if (key === "custom_indicator") {
    return buildCustomIndicatorField(customIndicators, indicatorKey);
  }
  return CONDITION_FIELDS.find((field) => field.key === key);
}

function getAvailableFields(side: "buy" | "sell", customIndicators: CustomIndicator[] = []): ConditionFieldDef[] {
  const base = CONDITION_FIELDS.filter((field) => field.side === "both" || field.side === side);
  if (!customIndicators.length) return base;
  return [...base, buildCustomIndicatorField(customIndicators)];
}

function makeEmptyLeaf(side: "buy" | "sell", customIndicators: CustomIndicator[] = []): ConditionLeaf {
  const fields = getAvailableFields(side, customIndicators);
  const first = fields[0];
  return {
    field: first?.key ?? "quality_score",
    operator: (first?.operators[0] ?? "gte") as ConditionOperator,
    value: first?.valueType === "boolean" ? true : 0,
  };
}

interface LeafRowProps {
  leaf: ConditionLeaf;
  side: "buy" | "sell";
  onChange: (leaf: ConditionLeaf) => void;
  onDelete: () => void;
  customIndicators: CustomIndicator[];
}

function ConditionLeafRow({ leaf, side, onChange, onDelete, customIndicators }: LeafRowProps) {
  const fields = useMemo(() => getAvailableFields(side, customIndicators), [side, customIndicators]);
  const selectedCustomIndicator = useMemo(
    () => getCustomIndicatorMeta(customIndicators, leaf.params?.indicator_key),
    [customIndicators, leaf.params],
  );
  const fieldDef = useMemo(
    () => getFieldDef(leaf.field, customIndicators, leaf.params?.indicator_key),
    [leaf.field, customIndicators, leaf.params],
  );

  const groupedOptions = useMemo(() => {
    const groups: Record<string, { label: string; options: { label: string; value: string }[] }> = {};
    for (const field of fields) {
      const category = field.category;
      if (!groups[category]) groups[category] = { label: CATEGORY_LABELS[category] || category, options: [] };
      groups[category].options.push({ label: field.label, value: field.key });
    }
    return Object.values(groups);
  }, [fields]);

  const handleFieldChange = useCallback(
    (key: string) => {
      const def = getFieldDef(key, customIndicators);
      const nextParams = def?.params
        ? Object.fromEntries(def.params.map((param) => [param.key, param.default]))
        : undefined;
      let nextValue: ConditionLeaf["value"] = 0;
      if (def?.valueType === "boolean") nextValue = true;
      else if (def?.valueType === "string") nextValue = "";
      else if (def?.valueType === "string_list") nextValue = [];
      onChange({
        field: key,
        operator: (def?.operators[0] ?? "gte") as ConditionOperator,
        value: nextValue,
        params: nextParams,
      });
    },
    [customIndicators, onChange],
  );

  const operatorOptions = useMemo(
    () => (fieldDef?.operators ?? ["gte"]).map((op) => ({ label: OPERATOR_LABELS[op] ?? op, value: op })),
    [fieldDef],
  );

  const renderValueInput = () => {
    if (!fieldDef) return null;
    switch (fieldDef.valueType) {
      case "number":
        return (
          <InputNumber
            size="small"
            style={{ width: 100 }}
            value={typeof leaf.value === "number" ? leaf.value : 0}
            onChange={(value) => onChange({ ...leaf, value: Number(value ?? 0) })}
          />
        );
      case "boolean":
        return (
          <Switch
            size="small"
            checked={leaf.value === true}
            onChange={(checked) => onChange({ ...leaf, value: checked })}
          />
        );
      case "string_list": {
        const options = leaf.field === "stage"
          ? STAGE_OPTIONS
          : leaf.field === "action" || leaf.field === "score_action"
            ? ACTION_OPTIONS
            : [];
        const value = Array.isArray(leaf.value) ? (leaf.value as string[]) : [];
        return (
          <Select
            size="small"
            mode="multiple"
            style={{ minWidth: 140 }}
            value={value}
            options={options}
            onChange={(next) => onChange({ ...leaf, value: next })}
            placeholder={t("cbPleaseSelect")}
          />
        );
      }
      case "string":
        return (
          <Input
            size="small"
            style={{ width: 160 }}
            value={typeof leaf.value === "string" ? leaf.value : ""}
            onChange={(event) => onChange({ ...leaf, value: event.target.value })}
          />
        );
      default:
        return null;
    }
  };

  const renderParams = () => {
    if (!fieldDef?.params?.length) return null;
    return fieldDef.params.map((param) => {
      if (param.type === "select") {
        const options = customIndicators.map((item) => ({ label: item.name, value: item.key }));
        return (
          <Select
            key={param.key}
            size="small"
            style={{ minWidth: 180 }}
            value={String(leaf.params?.[param.key] ?? param.default ?? "")}
            options={options}
            onChange={(value) => {
              const nextIndicator = getCustomIndicatorMeta(customIndicators, value);
              const nextFieldDef = buildCustomIndicatorField(customIndicators, value);
              const nextValue = nextIndicator?.value_type === "number"
                ? (typeof leaf.value === "number" ? leaf.value : 0)
                : leaf.value === true;
              onChange({
                ...leaf,
                operator: nextFieldDef.operators.includes(leaf.operator) ? leaf.operator : nextFieldDef.operators[0],
                value: nextValue,
                params: { ...leaf.params, [param.key]: value },
              });
            }}
          />
        );
      }
      if (param.type === "text") {
        return (
          <Input
            key={param.key}
            size="small"
            style={{ width: 360 }}
            addonBefore={param.label}
            value={String(leaf.params?.[param.key] ?? param.default ?? "")}
            placeholder={param.placeholder}
            onChange={(event) =>
              onChange({
                ...leaf,
                params: { ...leaf.params, [param.key]: event.target.value },
              })
            }
          />
        );
      }
      return (
        <InputNumber
          key={param.key}
          size="small"
          style={{ width: 88 }}
          addonBefore={param.label}
          value={Number(leaf.params?.[param.key] ?? param.default)}
          onChange={(value) =>
            onChange({
              ...leaf,
              params: { ...leaf.params, [param.key]: Number(value ?? param.default) },
            })
          }
        />
      );
    });
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0", flexWrap: "wrap" }}>
      <Select
        size="small"
        style={{ width: 150 }}
        value={leaf.field}
        onChange={handleFieldChange}
        options={groupedOptions}
      />
      <Select
        size="small"
        style={{ width: 76 }}
        value={leaf.operator}
        options={operatorOptions}
        onChange={(value) => onChange({ ...leaf, operator: value as ConditionOperator })}
      />
      {renderValueInput()}
      {renderParams()}
      {leaf.field === "custom_indicator" && selectedCustomIndicator && (
        <Tag color={selectedCustomIndicator.value_type === "number" ? "gold" : "blue"} style={{ margin: 0 }}>
          {selectedCustomIndicator.value_type === "number" ? t("cbNumber") : t("cbBoolean")}
        </Tag>
      )}
      <Button type="text" danger size="small" icon={<DeleteOutlined />} onClick={onDelete} />
    </div>
  );
}

interface GroupNodeProps {
  group: ConditionGroup;
  onChange: (group: ConditionGroup) => void;
  side: "buy" | "sell";
  depth: number;
  maxDepth: number;
  canDelete?: boolean;
  onDelete?: () => void;
  customIndicators: CustomIndicator[];
}

function ConditionGroupNode({
  group,
  onChange,
  side,
  depth,
  maxDepth,
  canDelete,
  onDelete,
  customIndicators,
}: GroupNodeProps) {
  const updateChild = useCallback(
    (index: number, updated: ConditionLeaf | ConditionGroup) => {
      const next = [...group.conditions];
      next[index] = updated;
      onChange({ ...group, conditions: next });
    },
    [group, onChange],
  );

  const removeChild = useCallback(
    (index: number) => {
      const next = group.conditions.filter((_, itemIndex) => itemIndex !== index);
      onChange({ ...group, conditions: next });
    },
    [group, onChange],
  );

  const addLeaf = useCallback(() => {
    onChange({ ...group, conditions: [...group.conditions, makeEmptyLeaf(side, customIndicators)] });
  }, [group, onChange, side, customIndicators]);

  const addGroup = useCallback(() => {
    if (depth >= maxDepth) return;
    onChange({
      ...group,
      conditions: [...group.conditions, { logic: "AND", conditions: [makeEmptyLeaf(side, customIndicators)] }],
    });
  }, [depth, group, maxDepth, onChange, side, customIndicators]);

  return (
    <div
      style={{
        background: GROUP_BG[group.logic],
        border: `1px solid ${GROUP_BORDER[group.logic]}`,
        borderRadius: 8,
        padding: "8px 12px",
        marginLeft: depth > 0 ? 16 : 0,
        marginBottom: 8,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <Segmented
          size="small"
          value={group.logic}
          options={[
            { label: "AND", value: "AND" },
            { label: "OR", value: "OR" },
          ]}
          onChange={(value) => onChange({ ...group, logic: value as LogicOperator })}
        />
        <Tag color={group.logic === "AND" ? "blue" : "green"} style={{ margin: 0 }}>
          {template("cbConditionCount", { count: group.conditions.length })}
        </Tag>
        <div style={{ flex: 1 }} />
        {canDelete && onDelete && (
          <Button type="text" danger size="small" icon={<DeleteOutlined />} onClick={onDelete}>
            {t("cbDeleteGroup")}
          </Button>
        )}
      </div>

      {group.conditions.map((child, index) =>
        isConditionGroup(child) ? (
          <ConditionGroupNode
            key={`g-${index}`}
            group={child}
            onChange={(updated) => updateChild(index, updated)}
            side={side}
            depth={depth + 1}
            maxDepth={maxDepth}
            canDelete
            onDelete={() => removeChild(index)}
            customIndicators={customIndicators}
          />
        ) : (
          <ConditionLeafRow
            key={`l-${index}`}
            leaf={child}
            side={side}
            onChange={(updated) => updateChild(index, updated)}
            onDelete={() => removeChild(index)}
            customIndicators={customIndicators}
          />
        ),
      )}

      <Space size={8} style={{ marginTop: 4 }}>
        <Button type="dashed" size="small" icon={<PlusOutlined />} onClick={addLeaf}>
          {t("cbAddCondition")}
        </Button>
        {depth < maxDepth && (
          <Button type="dashed" size="small" icon={<PlusOutlined />} onClick={addGroup}>
            {t("cbAddGroup")}
          </Button>
        )}
      </Space>
    </div>
  );
}

export default function ConditionBuilder({
  value,
  onChange,
  side,
  depth = 0,
  maxDepth = 4,
  customIndicators = [],
}: ConditionBuilderProps) {
  return (
    <ConditionGroupNode
      group={value}
      onChange={onChange}
      side={side}
      depth={depth}
      maxDepth={maxDepth}
      customIndicators={customIndicators}
    />
  );
}
