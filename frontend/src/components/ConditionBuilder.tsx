import { useCallback, useMemo } from "react";
import { Button, Input, InputNumber, Select, Space, Switch, Segmented, Tag } from "antd";
import { PlusOutlined, DeleteOutlined } from "@ant-design/icons";
import type {
  ConditionGroup,
  ConditionLeaf,
  ConditionOperator,
  ConditionFieldDef,
  LogicOperator,
} from "../types";
import { isConditionGroup } from "../types";
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
}

const GROUP_BG: Record<LogicOperator, string> = {
  AND: "#e6f7ff",
  OR: "#f6ffed",
};

const GROUP_BORDER: Record<LogicOperator, string> = {
  AND: "#91d5ff",
  OR: "#b7eb8f",
};

function getFieldDef(key: string): ConditionFieldDef | undefined {
  return CONDITION_FIELDS.find((f) => f.key === key);
}

function getAvailableFields(side: "buy" | "sell"): ConditionFieldDef[] {
  return CONDITION_FIELDS.filter(
    (f) => f.side === "both" || f.side === side
  );
}

function makeEmptyLeaf(side: "buy" | "sell"): ConditionLeaf {
  const fields = getAvailableFields(side);
  const first = fields[0];
  return {
    field: first?.key ?? "quality_score",
    operator: (first?.operators[0] ?? "gte") as ConditionOperator,
    value: 0,
  };
}

function makeEmptyGroup(): ConditionGroup {
  return { logic: "AND", conditions: [] };
}

// ── Leaf Row ──────────────────────────────────────────────

interface LeafRowProps {
  leaf: ConditionLeaf;
  side: "buy" | "sell";
  onChange: (leaf: ConditionLeaf) => void;
  onDelete: () => void;
}

function ConditionLeafRow({ leaf, side, onChange, onDelete }: LeafRowProps) {
  const fields = useMemo(() => getAvailableFields(side), [side]);
  const fieldDef = useMemo(() => getFieldDef(leaf.field), [leaf.field]);

  const groupedOptions = useMemo(() => {
    const groups: Record<string, { label: string; options: { label: string; value: string }[] }> = {};
    for (const f of fields) {
      const cat = f.category;
      if (!groups[cat]) groups[cat] = { label: CATEGORY_LABELS[cat] || cat, options: [] };
      groups[cat].options.push({ label: f.label, value: f.key });
    }
    return Object.values(groups);
  }, [fields]);

  const handleFieldChange = useCallback(
    (key: string) => {
      const def = getFieldDef(key);
      const op = def?.operators[0] ?? "gte";
      let val: number | string | boolean | (string | number)[] = 0;
      if (def?.valueType === "boolean") val = true;
      else if (def?.valueType === "string_list") val = [];
      else if (def?.valueType === "string") val = "";
      const params = def?.params
        ? Object.fromEntries(def.params.map((p) => [p.key, p.default]))
        : undefined;
      onChange({ field: key, operator: op as ConditionOperator, value: val, params });
    },
    [onChange],
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
            onChange={(v) => onChange({ ...leaf, value: Number(v ?? 0) })}
          />
        );
      case "boolean":
        return (
          <Switch
            size="small"
            checked={leaf.value === true}
            onChange={(v) => onChange({ ...leaf, value: v })}
          />
        );
      case "string_list": {
        const opts = leaf.field === "stage" ? STAGE_OPTIONS
          : leaf.field === "action" || leaf.field === "score_action" ? ACTION_OPTIONS
          : [];
        const val = Array.isArray(leaf.value) ? leaf.value as string[] : [];
        return (
          <Select
            size="small"
            mode="multiple"
            style={{ minWidth: 140 }}
            value={val}
            options={opts}
            onChange={(v) => onChange({ ...leaf, value: v })}
            placeholder="Select..."
          />
        );
      }
      case "string":
        return (
          <Input
            size="small"
            style={{ width: 160 }}
            value={typeof leaf.value === "string" ? leaf.value : ""}
            onChange={(e) => onChange({ ...leaf, value: e.target.value })}
          />
        );
      default:
        return null;
    }
  };

  const renderParams = () => {
    if (!fieldDef?.params?.length) return null;
    return fieldDef.params.map((p) => {
      if (p.type === "text") {
        return (
          <Input
            key={p.key}
            size="small"
            style={{ width: 360 }}
            addonBefore={p.label}
            value={String(leaf.params?.[p.key] ?? p.default ?? "")}
            placeholder={p.placeholder}
            onChange={(e) =>
              onChange({
                ...leaf,
                params: { ...leaf.params, [p.key]: e.target.value },
              })
            }
          />
        );
      }
      return (
        <InputNumber
          key={p.key}
          size="small"
          style={{ width: 88 }}
          addonBefore={p.label}
          value={Number(leaf.params?.[p.key] ?? p.default)}
          onChange={(v) =>
            onChange({
              ...leaf,
              params: { ...leaf.params, [p.key]: Number(v ?? p.default) },
            })
          }
        />
      );
    });
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0" }}>
      <Select
        size="small"
        style={{ width: 150 }}
        value={leaf.field}
        onChange={handleFieldChange}
        options={groupedOptions}
      />
      <Select
        size="small"
        style={{ width: 70 }}
        value={leaf.operator}
        options={operatorOptions}
        onChange={(v) => onChange({ ...leaf, operator: v as ConditionOperator })}
      />
      {renderValueInput()}
      {renderParams()}
      <Button type="text" danger size="small" icon={<DeleteOutlined />} onClick={onDelete} />
    </div>
  );
}

// ── Group Node (recursive) ────────────────────────────────

interface GroupNodeProps {
  group: ConditionGroup;
  onChange: (group: ConditionGroup) => void;
  side: "buy" | "sell";
  depth: number;
  maxDepth: number;
  canDelete?: boolean;
  onDelete?: () => void;
}

function ConditionGroupNode({
  group,
  onChange,
  side,
  depth,
  maxDepth,
  canDelete,
  onDelete,
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
      const next = group.conditions.filter((_, i) => i !== index);
      onChange({ ...group, conditions: next });
    },
    [group, onChange],
  );

  const addLeaf = useCallback(() => {
    onChange({ ...group, conditions: [...group.conditions, makeEmptyLeaf(side)] });
  }, [group, onChange, side]);

  const addGroup = useCallback(() => {
    if (depth >= maxDepth) return;
    onChange({
      ...group,
      conditions: [...group.conditions, { logic: "AND", conditions: [makeEmptyLeaf(side)] }],
    });
  }, [group, onChange, side, depth, maxDepth]);

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
            { label: "且 (AND)", value: "AND" },
            { label: "或 (OR)", value: "OR" },
          ]}
          onChange={(v) => onChange({ ...group, logic: v as LogicOperator })}
        />
        <Tag color={group.logic === "AND" ? "blue" : "green"} style={{ margin: 0 }}>
          {group.conditions.length} 个条件
        </Tag>
        <div style={{ flex: 1 }} />
        {canDelete && onDelete && (
          <Button type="text" danger size="small" icon={<DeleteOutlined />} onClick={onDelete}>
            删除组
          </Button>
        )}
      </div>

      {group.conditions.map((child, i) =>
        isConditionGroup(child) ? (
          <ConditionGroupNode
            key={`g-${i}`}
            group={child}
            onChange={(updated) => updateChild(i, updated)}
            side={side}
            depth={depth + 1}
            maxDepth={maxDepth}
            canDelete
            onDelete={() => removeChild(i)}
          />
        ) : (
          <ConditionLeafRow
            key={`l-${i}`}
            leaf={child}
            side={side}
            onChange={(updated) => updateChild(i, updated)}
            onDelete={() => removeChild(i)}
          />
        ),
      )}

      <Space size={8} style={{ marginTop: 4 }}>
        <Button type="dashed" size="small" icon={<PlusOutlined />} onClick={addLeaf}>
          添加条件
        </Button>
        {depth < maxDepth && (
          <Button type="dashed" size="small" icon={<PlusOutlined />} onClick={addGroup}>
            添加条件组
          </Button>
        )}
      </Space>
    </div>
  );
}

// ── Main Export ────────────────────────────────────────────

export default function ConditionBuilder({
  value,
  onChange,
  side,
  depth = 0,
  maxDepth = 4,
}: ConditionBuilderProps) {
  return (
    <ConditionGroupNode
      group={value}
      onChange={onChange}
      side={side}
      depth={depth}
      maxDepth={maxDepth}
    />
  );
}