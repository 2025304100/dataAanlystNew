import { Form, Input, InputNumber, Select, Switch } from "antd";
import { t } from "../../i18n";

/**
 * AI 草稿字段渲染器注册表（Batch 1：note / alert / filter）。
 *
 * 设计：通用确认 Modal 只负责「加载 → 差异 → 校验 → 确认 → 执行」骨架，
 * 各 draft_type 的字段形态由这里的渲染器提供，避免为每类草稿复制一份 Modal。
 * indicator / review / order 三类按既定约束延后迭代（未注册 → UI 显示不支持提示）。
 *
 * 枚举选项复用既有 i18n 键（与 AlertCenter / SymbolAlertSummary 同源文案），
 * 不新增硬编码中文。后续若抽公共枚举模块，此处改为引用即可。
 */

export type DraftValues = Record<string, unknown>;
export type DraftUpdater = (key: string, value: unknown) => void;

export interface DraftRenderer {
  /** 标题 i18n key */
  titleKey: string;
  /** 差异对比字段（key + label i18n key） */
  diffFields: Array<{ key: string; labelKey: string }>;
  /** 表单渲染（须在 Form 内使用） */
  render: (values: DraftValues, update: DraftUpdater) => React.ReactNode;
}

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

const SCOPE_OPTIONS = [
  { value: "universe", label: t("aiDraftScopeUniverse") },
  { value: "portfolio", label: t("aiDraftScopePortfolio") },
  { value: "watchlist", label: t("aiDraftScopeWatchlist") },
];

const SORT_MODE_OPTIONS = [
  { value: "quality_desc", label: t("aiDraftSortQuality") },
  { value: "timing_desc", label: t("aiDraftSortTiming") },
  { value: "priority_desc", label: t("aiDraftSortPriority") },
  { value: "score_desc", label: t("aiDraftSortScore") },
  { value: "rank_asc", label: t("aiDraftSortRank") },
];

function asText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function asTags(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string" && value.trim()) {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) return parsed.map(String);
    } catch {
      // 非 JSON 字符串：按逗号拆
    }
    return value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  }
  return [];
}

function asNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

export const DRAFT_RENDERERS: Record<string, DraftRenderer> = {
  draft_note: {
    titleKey: "aiDraftTypeNote",
    diffFields: [
      { key: "watchlist_item_id", labelKey: "aiDraftFieldWatchlistItem" },
      { key: "note", labelKey: "aiDraftFieldNote" },
      { key: "tags", labelKey: "aiDraftFieldTags" },
      { key: "priority", labelKey: "aiDraftFieldPriority" },
    ],
    render: (values, update) => (
      <>
        <Form.Item label={t("aiDraftFieldWatchlistItem")} required>
          <InputNumber
            min={1}
            style={{ width: 200 }}
            value={asNumber(values.watchlist_item_id)}
            onChange={(value) => update("watchlist_item_id", value)}
            data-testid="ai-draft-field-watchlist-item"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldNote")}>
          <Input.TextArea
            rows={4}
            value={asText(values.note)}
            onChange={(e) => update("note", e.target.value)}
            data-testid="ai-draft-field-note"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldTags")}>
          <Select
            mode="tags"
            value={asTags(values.tags)}
            onChange={(value) => update("tags", value)}
            tokenSeparators={[",", "，"]}
            data-testid="ai-draft-field-tags"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldPriority")}>
          <InputNumber
            min={0}
            max={9}
            value={asNumber(values.priority)}
            onChange={(value) => update("priority", value)}
            data-testid="ai-draft-field-priority"
          />
        </Form.Item>
      </>
    ),
  },

  draft_alert: {
    titleKey: "aiDraftTypeAlert",
    diffFields: [
      { key: "name", labelKey: "aiDraftFieldAlertName" },
      { key: "alert_type", labelKey: "aiDraftFieldAlertType" },
      { key: "severity", labelKey: "aiDraftFieldSeverity" },
      { key: "cooldown_minutes", labelKey: "aiDraftFieldCooldown" },
      { key: "enabled", labelKey: "aiDraftFieldEnabled" },
      { key: "config_json", labelKey: "aiDraftFieldConfigJson" },
    ],
    render: (values, update) => (
      <>
        <Form.Item label={t("aiDraftFieldAlertName")} required>
          <Input
            value={asText(values.name)}
            onChange={(e) => update("name", e.target.value)}
            data-testid="ai-draft-field-alert-name"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldAlertType")} required>
          <Select
            value={asText(values.alert_type) || undefined}
            options={ALERT_TYPE_OPTIONS}
            onChange={(value) => update("alert_type", value)}
            data-testid="ai-draft-field-alert-type"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldSeverity")}>
          <Select
            value={asText(values.severity) || undefined}
            options={SEVERITY_OPTIONS}
            onChange={(value) => update("severity", value)}
            data-testid="ai-draft-field-severity"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldCooldown")}>
          <InputNumber
            min={0}
            max={10080}
            value={asNumber(values.cooldown_minutes)}
            onChange={(value) => update("cooldown_minutes", value)}
            data-testid="ai-draft-field-cooldown"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldEnabled")}>
          <Switch
            checked={values.enabled !== false}
            onChange={(checked) => update("enabled", checked)}
            data-testid="ai-draft-field-enabled"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldConfigJson")}>
          <Input.TextArea
            rows={4}
            style={{ fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}
            value={asText(values.config_json)}
            onChange={(e) => update("config_json", e.target.value)}
            data-testid="ai-draft-field-config-json"
          />
        </Form.Item>
      </>
    ),
  },

  draft_filter: {
    titleKey: "aiDraftTypeFilter",
    diffFields: [
      { key: "name", labelKey: "aiDraftFieldFilterName" },
      { key: "scope_type", labelKey: "aiDraftFieldScopeType" },
      { key: "markets", labelKey: "aiDraftFieldMarkets" },
      { key: "boards", labelKey: "aiDraftFieldBoards" },
      { key: "sort_mode", labelKey: "aiDraftFieldSortMode" },
      { key: "filters_json", labelKey: "aiDraftFieldFiltersJson" },
      { key: "description", labelKey: "aiDraftFieldDescription" },
    ],
    render: (values, update) => (
      <>
        <Form.Item label={t("aiDraftFieldFilterName")} required>
          <Input
            value={asText(values.name)}
            onChange={(e) => update("name", e.target.value)}
            data-testid="ai-draft-field-filter-name"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldScopeType")} required>
          <Select
            value={asText(values.scope_type) || undefined}
            options={SCOPE_OPTIONS}
            onChange={(value) => update("scope_type", value)}
            data-testid="ai-draft-field-scope-type"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldMarkets")}>
          <Select
            mode="tags"
            value={asTags(values.markets)}
            onChange={(value) => update("markets", value)}
            tokenSeparators={[",", "，"]}
            data-testid="ai-draft-field-markets"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldBoards")}>
          <Select
            mode="tags"
            value={asTags(values.boards)}
            onChange={(value) => update("boards", value)}
            tokenSeparators={[",", "，"]}
            data-testid="ai-draft-field-boards"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldSortMode")}>
          <Select
            value={asText(values.sort_mode) || undefined}
            options={SORT_MODE_OPTIONS}
            onChange={(value) => update("sort_mode", value)}
            data-testid="ai-draft-field-sort-mode"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldFiltersJson")}>
          <Input.TextArea
            rows={4}
            style={{ fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}
            value={asText(values.filters_json)}
            onChange={(e) => update("filters_json", e.target.value)}
            data-testid="ai-draft-field-filters-json"
          />
        </Form.Item>
        <Form.Item label={t("aiDraftFieldDescription")}>
          <Input.TextArea
            rows={2}
            value={asText(values.description)}
            onChange={(e) => update("description", e.target.value)}
            data-testid="ai-draft-field-description"
          />
        </Form.Item>
      </>
    ),
  },
};

/** 已开放确认界面的草稿类型（约束：本批仅 note/alert/filter）。 */
export const SUPPORTED_DRAFT_TYPES = ["draft_note", "draft_alert", "draft_filter"] as const;

export function getDraftRenderer(draftType: string | null | undefined): DraftRenderer | null {
  if (!draftType) return null;
  return DRAFT_RENDERERS[draftType] ?? null;
}

export function isSupportedDraftType(draftType: string | null | undefined): boolean {
  return Boolean(draftType && draftType in DRAFT_RENDERERS);
}
