import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Checkbox, DatePicker, Descriptions, Empty, Form, Input, InputNumber, Modal, Popconfirm, Radio, Select, Space, Table, Tag, message } from "antd";
import { DeleteOutlined, EyeOutlined, PlusOutlined, ReloadOutlined, RiseOutlined, SaveOutlined, RobotOutlined } from "@ant-design/icons";
import type { Dayjs } from "dayjs";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import { t, template, DOT } from "../i18n";
import AiChatDrawer from "./AiChatDrawer";
import type {
  BacktestRuleConfigV2,
  CustomIndicator,
  CustomIndicatorPayload,
  CustomIndicatorPreviewRead,
  CustomIndicatorPreviewSeriesItem,
  CustomIndicatorVersion,
  DiscoveryPlan,
  RuleTemplate,
  Symbol,
} from "../types";

type FormulaTemplateDef = {
  key: string;
  nameZh: string;
  nameEn: string;
  descriptionZh: string;
  descriptionEn: string;
  formula: string;
  category: string;
  valueType: "boolean" | "number";
  scope?: string[];
};

type FormulaFunctionDoc = {
  key: string;
  snippet: string;
  descriptionZh: string;
  descriptionEn: string;
  referenceZh: string;
  referenceEn: string;
  example: string;
};

type ReferenceFieldDoc = {
  key: string;
  label: string;
  descriptionZh: string;
  descriptionEn: string;
  sourceZh: string;
  sourceEn: string;
  getValue: (result: CustomIndicatorPreviewRead) => number | null;
};

type PreviewFeedback = {
  type: "warning" | "error";
  message: string;
  description: string;
};

type CustomIndicatorSettingsProps = {
  onOpenHistoryInit?: (context?: { symbolId?: number | null; symbolLabel?: string | null }) => void;
};

type ClauseExplanation = {
  text: string;
  operator?: string;
  leftText?: string;
  rightText?: string;
  leftValue?: string | null;
  rightValue?: string | null;
  status: "passed" | "failed" | "unknown";
};

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

function previewValue(result: { value_type: "boolean" | "number"; result_boolean?: boolean | null; result_number?: number | null; display_value: string }): string {
  if (result.value_type === "number") {
    return result.result_number != null ? String(result.result_number) : result.display_value;
  }
  return result.result_boolean ? t("yes") : t("no");
}

function normalizeFormulaText(formula: string): string {
  return formula.replace(/\s+/g, " ").trim();
}

function extractFunctionKeys(formula: string): string[] {
  const matches = Array.from(normalizeFormulaText(formula).matchAll(/([a-z_][a-z0-9_]*)\s*\(/gi));
  return Array.from(new Set(matches.map((item) => item[1].toLowerCase())));
}

function splitFormulaClauses(formula: string): { clauses: string[]; connectors: string[] } {
  const text = normalizeFormulaText(formula);
  if (!text) return { clauses: [], connectors: [] };
  return {
    clauses: text.split(/\s+(?:and|or)\s+/i).map((item) => item.trim()).filter(Boolean),
    connectors: Array.from(text.matchAll(/\s+(and|or)\s+/gi)).map((item) => item[1].toUpperCase()),
  };
}

function formatOperandValue(value: string | number | boolean | null): string | null {
  if (value == null) return null;
  if (typeof value === "number") return Number.isFinite(value) ? value.toFixed(2) : String(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  return value;
}

function buildClauseExplanations(formula: string, previewResult: CustomIndicatorPreviewRead | null, referenceDocs: ReferenceFieldDoc[]): ClauseExplanation[] {
  if (!previewResult) return [];
  const referenceMap = new Map(referenceDocs.map((item) => [item.key.toLowerCase(), item]));
  const clauses = splitFormulaClauses(formula).clauses;

  const resolveOperand = (raw: string): { text: string; value: string | number | boolean | null } => {
    const text = raw.trim();
    const normalized = text.replace(/^\(+|\)+$/g, "").trim().toLowerCase();
    if (/^-?\d+(\.\d+)?$/.test(normalized)) return { text, value: Number(normalized) };
    if (normalized === "true") return { text, value: true };
    if (normalized === "false") return { text, value: false };
    if ((normalized.startsWith('"') && normalized.endsWith('"')) || (normalized.startsWith("'") && normalized.endsWith("'"))) {
      return { text, value: normalized.slice(1, -1) };
    }
    const found = referenceMap.get(normalized);
    if (found) return { text: found.label, value: found.getValue(previewResult) };
    return { text, value: null };
  };

  return clauses.map((clause) => {
    const match = clause.match(/(.+?)(>=|<=|==|!=|>|<)(.+)/);
    if (!match) return { text: clause, status: "unknown" };
    const left = resolveOperand(match[1]);
    const operator = match[2];
    const right = resolveOperand(match[3]);
    let status: ClauseExplanation["status"] = "unknown";
    if (left.value != null && right.value != null) {
      if (operator === ">") status = Number(left.value) > Number(right.value) ? "passed" : "failed";
      else if (operator === ">=") status = Number(left.value) >= Number(right.value) ? "passed" : "failed";
      else if (operator === "<") status = Number(left.value) < Number(right.value) ? "passed" : "failed";
      else if (operator === "<=") status = Number(left.value) <= Number(right.value) ? "passed" : "failed";
      else if (operator === "==") status = left.value === right.value ? "passed" : "failed";
      else if (operator === "!=") status = left.value !== right.value ? "passed" : "failed";
    }
    return {
      text: clause,
      operator,
      leftText: left.text,
      rightText: right.text,
      leftValue: formatOperandValue(left.value),
      rightValue: formatOperandValue(right.value),
      status,
    };
  });
}

export default function CustomIndicatorSettings({ onOpenHistoryInit }: CustomIndicatorSettingsProps) {
  const ctx = useApp();
  const isZh = (ctx.locale ?? "").toLowerCase().startsWith("zh");
  const [rows, setRows] = useState<CustomIndicator[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [form, setForm] = useState<CustomIndicatorPayload>(DEFAULT_FORM);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewResult, setPreviewResult] = useState<CustomIndicatorPreviewRead | null>(null);
  const [previewFeedback, setPreviewFeedback] = useState<PreviewFeedback | null>(null);
  const [previewSymbolId, setPreviewSymbolId] = useState<number | null>(ctx.activeSymbolId);
  const [previewTradeDate, setPreviewTradeDate] = useState<Dayjs | null>(null);
  const [previewRecentCount, setPreviewRecentCount] = useState(7);
  const [previewSymbolOptions, setPreviewSymbolOptions] = useState<Symbol[]>([]);
  const [discoveryPlans, setDiscoveryPlans] = useState<DiscoveryPlan[]>([]);
  const [ruleTemplates, setRuleTemplates] = useState<RuleTemplate[]>([]);
  const [libraryKeyword, setLibraryKeyword] = useState("");
  const [libraryCategoryFilter, setLibraryCategoryFilter] = useState("all");
  const [libraryScopeFilter, setLibraryScopeFilter] = useState("all");
  const [libraryStatusFilter, setLibraryStatusFilter] = useState("all");
  const [libraryValueTypeFilter, setLibraryValueTypeFilter] = useState("all");
  const [resourceKeyword, setResourceKeyword] = useState("");
  const [resourceCategoryFilter, setResourceCategoryFilter] = useState("all");
  const [resourceScopeFilter, setResourceScopeFilter] = useState("all");
  const [functionViewMode, setFunctionViewMode] = useState<"all" | "used">("all");
  const [versions, setVersions] = useState<CustomIndicatorVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [changeNote, setChangeNote] = useState("");
  const [expandedVersionId, setExpandedVersionId] = useState<number | null>(null);
  const [aiChatOpen, setAiChatOpen] = useState(false);
  // WP4-02: 数值指标提升为因子草稿
  const [promoteOpen, setPromoteOpen] = useState(false);
  const [promoteLoading, setPromoteLoading] = useState(false);
  const [promoteCode, setPromoteCode] = useState("");
  const [promoteName, setPromoteName] = useState("");
  const [promoteDirection, setPromoteDirection] = useState<"higher_better" | "lower_better" | "nonlinear">("higher_better");
  const [promoteRiskLevel, setPromoteRiskLevel] = useState<"low" | "medium" | "high">("medium");
  const [promoteChangeNote, setPromoteChangeNote] = useState("");

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

  const formulaTemplates = useMemo<FormulaTemplateDef[]>(() => [
    { key: "trend_alignment", nameZh: "趋势多头", nameEn: "Trend Alignment", descriptionZh: "均线多头排列，适合做趋势过滤", descriptionEn: "Bullish moving-average alignment for trend filtering", formula: "close > sma(20) and sma(20) > sma(60)", category: "trend", valueType: "boolean", scope: ["backtest", "discovery"] },
    { key: "pullback_entry", nameZh: "回踩择时", nameEn: "Pullback Entry", descriptionZh: "回踩但未走坏，适合找低吸位置", descriptionEn: "Pullback without breaking trend, useful for entry timing", formula: "close > sma(20) and rsi(14) < 40", category: "momentum", valueType: "boolean", scope: ["backtest", "discovery"] },
    { key: "volume_confirmation", nameZh: "放量确认", nameEn: "Volume Confirmation", descriptionZh: "放量配合趋势，适合确认信号强弱", descriptionEn: "Volume expansion to confirm signal strength", formula: "volume_ratio(5) > 1.5 and close > sma(20)", category: "volume", valueType: "boolean", scope: ["discovery", "alert"] },
    { key: "distance_score", nameZh: "乖离强度", nameEn: "Distance Score", descriptionZh: "输出价格相对均线的强弱数值", descriptionEn: "Numeric strength based on distance to moving average", formula: "round(100 * (close / sma(20) - 1), 2)", category: "trend", valueType: "number", scope: ["discovery", "review"] },
  ], [ctx.locale]);

  const functionLibrary = useMemo<FormulaFunctionDoc[]>(() => [
    { key: "sma", snippet: "sma(20)", descriptionZh: "简单均线，常用来判断趋势方向和支撑压力。", descriptionEn: "Simple moving average for trend direction and support/resistance.", referenceZh: "通常和 close、sma(60) 一起看多空结构。", referenceEn: "Often used with close and sma(60) to read structure.", example: "close > sma(20)" },
    { key: "ema", snippet: "ema(12)", descriptionZh: "指数均线，对近期价格更敏感。", descriptionEn: "Exponential moving average with more weight on recent prices.", referenceZh: "适合做短线节奏或快慢线比较。", referenceEn: "Useful for short-term rhythm or fast/slow comparisons.", example: "ema(12) > ema(26)" },
    { key: "rsi", snippet: "rsi(14)", descriptionZh: "强弱指标，适合看超买超卖和回踩力度。", descriptionEn: "Relative strength index for overbought/oversold and pullback depth.", referenceZh: "常见阈值 30/50/70。", referenceEn: "Typical thresholds are 30/50/70.", example: "rsi(14) < 40" },
    { key: "macd", snippet: "macd(12,26,9)", descriptionZh: "MACD 主线，适合判断趋势动能。", descriptionEn: "MACD main line for trend momentum.", referenceZh: "可配合 macd_signal、macd_hist 一起用。", referenceEn: "Often paired with macd_signal and macd_hist.", example: "macd(12,26,9) > macd_signal(12,26,9)" },
    { key: "volume_ratio", snippet: "volume_ratio(5)", descriptionZh: "量比，适合看放量是否有效。", descriptionEn: "Volume ratio to check if volume expansion is meaningful.", referenceZh: "大于 1 通常表示比近期平均成交更活跃。", referenceEn: "Above 1 usually means more active than recent average.", example: "volume_ratio(5) > 1.5" },
    { key: "pct_change", snippet: "pct_change(20)", descriptionZh: "区间涨跌幅，适合做强弱排序。", descriptionEn: "Percent change over a lookback window for ranking strength.", referenceZh: "适合数值型公式输出。", referenceEn: "Useful in numeric formulas.", example: "round(pct_change(20), 2)" },
    { key: "highest", snippet: "highest(20)", descriptionZh: "最近 N 天最高值，适合突破类公式。", descriptionEn: "Highest value over N bars for breakout logic.", referenceZh: "可以配合 close 判断是否突破。", referenceEn: "Use with close to detect breakouts.", example: "close >= highest(20)" },
    { key: "lowest", snippet: "lowest(20)", descriptionZh: "最近 N 天最低值，适合止损或回撤判断。", descriptionEn: "Lowest value over N bars for stop or drawdown logic.", referenceZh: "常和 ref、atr 联用。", referenceEn: "Often combined with ref and atr.", example: "close <= lowest(20)" },
    { key: "cross_over", snippet: "cross_over(close, sma(20))", descriptionZh: "上穿信号，适合捕捉突破时点。", descriptionEn: "Cross-over signal for breakout timing.", referenceZh: "适合做布尔型买点条件。", referenceEn: "Good for boolean entry conditions.", example: "cross_over(close, sma(20))" },
    { key: "cross_under", snippet: "cross_under(close, sma(20))", descriptionZh: "下穿信号，适合做离场或风险提示。", descriptionEn: "Cross-under signal for exits or alerts.", referenceZh: "可用于卖点或预警。", referenceEn: "Useful for exits or alerts.", example: "cross_under(close, sma(20))" },
  ], [ctx.locale]);

  const referenceFieldDocs = useMemo<ReferenceFieldDoc[]>(() => [
    { key: "open", label: t("ciOpen"), descriptionZh: "当前预览K线的开盘价。", descriptionEn: "Open price of the preview bar.", sourceZh: "来源：当前K线", sourceEn: "Source: preview bar", getValue: (result) => result.latest_bar.open },
    { key: "close", label: t("ciClose"), descriptionZh: "当前预览K线的收盘价。", descriptionEn: "Close price of the preview bar.", sourceZh: "来源：当前K线", sourceEn: "Source: preview bar", getValue: (result) => result.latest_bar.close },
    { key: "high", label: t("ciHigh"), descriptionZh: "当前预览K线的最高价。", descriptionEn: "High price of the preview bar.", sourceZh: "来源：当前K线", sourceEn: "Source: preview bar", getValue: (result) => result.latest_bar.high },
    { key: "low", label: t("ciLow"), descriptionZh: "当前预览K线的最低价。", descriptionEn: "Low price of the preview bar.", sourceZh: "来源：当前K线", sourceEn: "Source: preview bar", getValue: (result) => result.latest_bar.low },
    { key: "volume", label: t("ciVolume"), descriptionZh: "当前预览K线的成交量。", descriptionEn: "Volume of the preview bar.", sourceZh: "来源：当前K线", sourceEn: "Source: preview bar", getValue: (result) => result.latest_bar.volume ?? null },
    { key: "quality_score", label: t("ciQualityScore"), descriptionZh: "系统股质评分，偏向基本面与稳定性。", descriptionEn: "Workbench quality score, focused on fundamentals and stability.", sourceZh: "来源：评分快照", sourceEn: "Source: score snapshot", getValue: (result) => result.score_snapshot?.quality_score ?? null },
    { key: "timing_score", label: t("ciTimingScore"), descriptionZh: "系统时点评分，偏向当前买卖节奏。", descriptionEn: "Timing score focused on the current trading setup.", sourceZh: "来源：评分快照", sourceEn: "Source: score snapshot", getValue: (result) => result.score_snapshot?.timing_score ?? null },
    { key: "trend_score", label: t("ciTrendScore"), descriptionZh: "系统趋势评分，反映趋势方向与持续性。", descriptionEn: "Trend score representing direction and persistence.", sourceZh: "来源：评分快照", sourceEn: "Source: score snapshot", getValue: (result) => result.score_snapshot?.trend_score ?? null },
    { key: "momentum_score", label: t("ciMomentumScore"), descriptionZh: "系统动量评分，反映加速和强弱。", descriptionEn: "Momentum score reflecting acceleration and strength.", sourceZh: "来源：评分快照", sourceEn: "Source: score snapshot", getValue: (result) => result.score_snapshot?.momentum_score ?? null },
  ], [ctx.locale, isZh]);

  const selected = useMemo(() => rows.find((row) => row.id === selectedId) ?? null, [rows, selectedId]);
  const activeSymbol = useMemo<Symbol | null>(() => {
    if (!ctx.activeSymbolId) return null;
    if (ctx.detail?.symbol?.id === ctx.activeSymbolId) return ctx.detail.symbol;
    return ctx.symbolDirectory[ctx.activeSymbolId] ?? null;
  }, [ctx.activeSymbolId, ctx.detail, ctx.symbolDirectory]);
  const previewSymbol = useMemo<Symbol | null>(() => {
    if (!previewSymbolId) return null;
    return previewSymbolOptions.find((item) => item.id === previewSymbolId) ?? (activeSymbol?.id === previewSymbolId ? activeSymbol : null) ?? ctx.symbolDirectory[previewSymbolId] ?? null;
  }, [activeSymbol, ctx.symbolDirectory, previewSymbolId, previewSymbolOptions]);

  const renderPreviewFeedbackDescription = (feedback: PreviewFeedback) => (
    <div style={{ display: "grid", gap: 4 }}>
      {feedback.description ? <div>{feedback.description}</div> : null}
      {feedback.type === "warning" ? (
        onOpenHistoryInit ? (
          <>
            <div className="item-subline">{t("ciPreviewNoBarActionHint")}</div>
            <Button type="link" size="small" style={{ padding: 0, width: "fit-content", height: "auto" }} onClick={() => onOpenHistoryInit?.({ symbolId: previewSymbol?.id ?? previewSymbolId, symbolLabel: previewSymbol ? `${previewSymbol.symbol} | ${previewSymbol.name}` : null })}>{t("ciPreviewOpenHistoryInit")}</Button>
          </>
        ) : (
          <div className="item-subline">{t("ciPreviewNoBarActionHint")}</div>
        )
      ) : null}
    </div>
  );

  const buildPreviewFeedback = (detail?: string | null): PreviewFeedback => {
    const normalized = (detail ?? "").trim();
    if (/no daily bar data available/i.test(normalized)) {
      return {
        type: "warning",
        message: t("ciPreviewNoBarTitle"),
        description: previewTradeDate
          ? template("ciPreviewNoBarDateHelp", { date: previewTradeDate.format("YYYY-MM-DD") })
          : t("ciPreviewNoBarHelp"),
      };
    }
    return {
      type: "error",
      message: t("ciPreviewFailed"),
      description: normalized || t("ciPreviewFailedHelp"),
    };
  };

  const filteredRows = useMemo(() => {
    const keyword = libraryKeyword.trim().toLowerCase();
    return rows.filter((row) => {
      if (libraryCategoryFilter !== "all" && row.category !== libraryCategoryFilter) return false;
      if (libraryScopeFilter !== "all" && !(row.scope || []).includes(libraryScopeFilter)) return false;
      if (libraryStatusFilter === "enabled" && !row.enabled) return false;
      if (libraryStatusFilter === "disabled" && row.enabled) return false;
      if (libraryValueTypeFilter !== "all" && row.value_type !== libraryValueTypeFilter) return false;
      if (!keyword) return true;
      return [row.name, row.key, row.description, row.formula].join(" ").toLowerCase().includes(keyword);
    });
  }, [libraryCategoryFilter, libraryKeyword, libraryScopeFilter, libraryStatusFilter, libraryValueTypeFilter, rows]);

  const selectedFunctionDocs = useMemo(() => {
    const used = new Set(extractFunctionKeys(form.formula));
    return functionLibrary.filter((item) => used.has(item.key));
  }, [form.formula, functionLibrary]);

  const selectedReferenceDocs = useMemo(() => {
    const normalized = normalizeFormulaText(form.formula).toLowerCase();
    return referenceFieldDocs.filter((item) => new RegExp("(^|[^a-z_])" + item.key + "([^a-z_]|$)", "i").test(normalized));
  }, [form.formula, referenceFieldDocs]);

  const clauseSummary = useMemo(() => splitFormulaClauses(form.formula), [form.formula]);
  const clauseExplanations = useMemo(() => buildClauseExplanations(form.formula, previewResult, referenceFieldDocs), [form.formula, previewResult, referenceFieldDocs]);
  const previewClauseStats = useMemo(() => ({
    passed: clauseExplanations.filter((item) => item.status === "passed").length,
    failed: clauseExplanations.filter((item) => item.status === "failed").length,
    explainable: clauseExplanations.filter((item) => item.status !== "unknown").length,
  }), [clauseExplanations]);

  const usageSummary = useMemo(() => {
    if (!selected?.key) return { discovery: [] as DiscoveryPlan[], backtest: [] as RuleTemplate[] };
    const discovery = discoveryPlans.filter((plan) => (plan.filters || []).some((filter) => filter.indicator_key === selected.key));
    const backtest = ruleTemplates.filter((template) => {
      const keys = new Set<string>();
      collectBacktestIndicatorKeys((template.rule_config as BacktestRuleConfigV2 | Record<string, unknown>) ?? {}, keys);
      return keys.has(selected.key);
    });
    return { discovery, backtest };
  }, [discoveryPlans, ruleTemplates, selected]);

  const previewReferenceValues = useMemo(() => {
    if (!previewResult) return [] as Array<{ key: string; label: string; value: string; source: string }>;
    return referenceFieldDocs.map((item) => ({ key: item.key, label: item.label, value: scoreValue(item.getValue(previewResult)), source: isZh ? item.sourceZh : item.sourceEn })).filter((item) => item.value !== "-");
  }, [isZh, previewResult, referenceFieldDocs]);
  const previewRecentResults = useMemo<CustomIndicatorPreviewSeriesItem[]>(() => previewResult?.recent_results ?? [], [previewResult]);
  const formulaUseCaseLabels = useMemo(
    () => form.scope.map((item) => scopeOptions.find((option) => option.value === item)?.label ?? item),
    [form.scope, scopeOptions]
  );
  const filteredFormulaTemplates = useMemo(() => {
    const keyword = resourceKeyword.trim().toLowerCase();
    return formulaTemplates.filter((item) => {
      if (resourceCategoryFilter !== "all" && item.category !== resourceCategoryFilter) return false;
      if (resourceScopeFilter !== "all" && !(item.scope || []).includes(resourceScopeFilter)) return false;
      if (!keyword) return true;
      return [
        item.key,
        item.nameZh,
        item.nameEn,
        item.descriptionZh,
        item.descriptionEn,
        item.formula,
      ].join(" ").toLowerCase().includes(keyword);
    });
  }, [formulaTemplates, resourceCategoryFilter, resourceKeyword, resourceScopeFilter]);
  const filteredFunctionLibrary = useMemo(() => {
    const keyword = resourceKeyword.trim().toLowerCase();
    const base = functionViewMode === "used" ? selectedFunctionDocs : functionLibrary;
    return base.filter((item) => {
      if (!keyword) return true;
      return [
        item.key,
        item.snippet,
        item.descriptionZh,
        item.descriptionEn,
        item.referenceZh,
        item.referenceEn,
        item.example,
      ].join(" ").toLowerCase().includes(keyword);
    });
  }, [functionLibrary, functionViewMode, resourceKeyword, selectedFunctionDocs]);

  const loadRows = async () => {
    setLoading(true);
    try {
      const [indicators, plans, templates] = await Promise.all([api.getCustomIndicators(), api.getDiscoveryPlans(), api.getBacktestTemplates()]);
      setRows(indicators as CustomIndicator[]);
      setDiscoveryPlans(plans as DiscoveryPlan[]);
      setRuleTemplates(templates as RuleTemplate[]);
    } catch (error: any) {
      message.error(error?.message || t("ciLoadFailed"));
    } finally {
      setLoading(false);
    }
  };

  const loadVersions = async (indicatorId: number) => {
    setVersionsLoading(true);
    try {
      const data = await api.getIndicatorVersions(indicatorId);
      setVersions(data as CustomIndicatorVersion[]);
    } catch {
      setVersions([]);
    } finally {
      setVersionsLoading(false);
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
    setPreviewFeedback(null);
  }, [form.formula, form.value_type, previewSymbolId, previewTradeDate]);

  const editorMeta = useMemo(() => ({
    category: categoryOptions.find((item) => item.value === form.category)?.label ?? form.category,
    valueType: form.value_type === "number" ? t("ciNumber") : t("ciBoolean"),
  }), [categoryOptions, form.category, form.value_type, ctx.locale]);

  const startCreate = () => {
    setSelectedId(null);
    setForm(DEFAULT_FORM);
    setPreviewResult(null);
    setPreviewFeedback(null);
    setVersions([]);
    setChangeNote("");
    setExpandedVersionId(null);
  };

  const loadIndicator = (row: CustomIndicator) => {
    setSelectedId(row.id);
    setForm({ name: row.name, key: row.key, description: row.description, category: row.category, formula: row.formula, value_type: row.value_type, params: row.params ?? [], scope: row.scope ?? ["backtest"], enabled: row.enabled });
    setPreviewResult(null);
    setPreviewFeedback(null);
    setChangeNote("");
    setExpandedVersionId(null);
    loadVersions(row.id);
  };

  const insertFormulaSnippet = (snippet: string) => {
    setForm((prev) => {
      const current = prev.formula.trim();
      const formula = current ? current + (current.endsWith("(") ? "" : current.endsWith("\n") ? "" : "\n") + snippet : snippet;
      return { ...prev, formula };
    });
  };

  const applyTemplate = (item: FormulaTemplateDef) => {
    setForm((prev) => ({
      ...prev,
      formula: item.formula,
      category: item.category,
      value_type: item.valueType,
      scope: item.scope ?? prev.scope,
      name: prev.name || (isZh ? item.nameZh : item.nameEn),
      description: prev.description || (isZh ? item.descriptionZh : item.descriptionEn),
      key: prev.key || keyFromName(isZh ? item.nameZh : item.nameEn),
    }));
    message.success(template("ciTemplateApplied", { name: ctx.locale === "zh-CN" ? item.nameZh : item.nameEn }));
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
      const result = await api.previewCustomIndicator({ symbol_id: previewSymbolId, formula: form.formula, value_type: form.value_type, trade_date: previewTradeDate ? previewTradeDate.format("YYYY-MM-DD") : undefined, recent_count: previewRecentCount });
      setPreviewResult(result as CustomIndicatorPreviewRead);
      setPreviewFeedback(null);
      message.success(t("ciPreviewUpdated"));
    } catch (error: any) {
      const feedback = buildPreviewFeedback(error?.message);
      setPreviewResult(null);
      setPreviewFeedback(feedback);
      if (feedback.type === "warning") {
        message.warning(feedback.message);
      } else {
        message.error(feedback.message);
      }
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
      const payload = { ...form, change_note: changeNote || undefined };
      if (selectedId) {
        const updated = await api.updateCustomIndicator(selectedId, payload);
        message.success(t("ciIndicatorSaved"));
        setRows((prev) => prev.map((row) => row.id === selectedId ? updated : row));
        setChangeNote("");
        loadVersions(selectedId);
      } else {
        const created = await api.createCustomIndicator(payload);
        message.success(t("ciIndicatorCreated"));
        setRows((prev) => [created, ...prev]);
        setSelectedId(created.id);
        setChangeNote("");
        loadVersions(created.id);
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

  const rollbackToVersion = async (version: number) => {
    if (!selectedId) return;
    try {
      const updated = await api.rollbackIndicator(selectedId, version);
      message.success(template("ciRollbackSuccess", { version: String(version) }));
      setRows((prev) => prev.map((row) => row.id === selectedId ? updated : row));
      setForm((prev) => ({ ...prev, formula: updated.formula, value_type: updated.value_type, params: updated.params ?? prev.params }));
      loadVersions(selectedId);
    } catch (error: any) {
      message.error(error?.message || t("ciSaveFailed"));
    }
  };

  // WP4-02: 数值指标提升为因子草稿
  const openPromote = () => {
    if (!selected) return;
    if (form.value_type !== "number") {
      message.warning(t("ciPromoteOnlyNumber"));
      return;
    }
    // 预填映射：code 由 key 自动生成（小写蛇形），name 用指标名
    setPromoteCode((form.key || "").toLowerCase().replace(/[^a-z0-9_]/g, "_"));
    setPromoteName(form.name || "");
    setPromoteDirection("higher_better");
    setPromoteRiskLevel("medium");
    setPromoteChangeNote("promoted from custom indicator");
    setPromoteOpen(true);
  };

  const confirmPromote = async () => {
    if (!selectedId) return;
    setPromoteLoading(true);
    try {
      const resp = await api.promoteIndicatorToFactor(selectedId, {
        code: promoteCode.trim() || undefined,
        name: promoteName.trim() || undefined,
        direction: promoteDirection,
        risk_level: promoteRiskLevel,
        change_note: promoteChangeNote.trim() || undefined,
      });
      message.success(template("ciPromoteSuccess", { code: resp.factor_code }));
      setPromoteOpen(false);
    } catch (error: any) {
      const detail = error?.details?.detail;
      if (detail && typeof detail === "object" && detail.error_code === "indicator_not_number") {
        message.error(t("ciPromoteOnlyNumber"));
      } else if (detail && typeof detail === "object" && detail.error_code === "factor_code_conflict") {
        message.error(`${t("ciPromoteFailed")}: ${detail.user_message || detail.error_code}`);
      } else {
        message.error(error?.message || t("ciPromoteFailed"));
      }
    } finally {
      setPromoteLoading(false);
    }
  };

  const insertFormulaFromAi = (newFormula: string) => {
    setForm((prev) => ({ ...prev, formula: newFormula }));
    setAiChatOpen(false);
    message.success(t("aiFormulaInserted"));
  };

  return (
    <div className="indicator-settings-grid indicator-settings-grid--formula">
      <Card
        className="indicator-card indicator-card--library"
        size="small"
        title={t("ciFormulaLibrary")}
        extra={<Space size={8} className="indicator-card__toolbar"><Tag>{filteredRows.length}/{rows.length}</Tag><Button size="small" icon={<ReloadOutlined />} onClick={loadRows}>{t("refresh")}</Button><Button size="small" icon={<PlusOutlined />} onClick={startCreate}>{t("ciNew")}</Button></Space>}
      >
        <div className="indicator-library-panel">
          <div className="indicator-library-filters">
            <Input
              allowClear
              size="small"
              className="indicator-library-search"
              value={libraryKeyword}
              onChange={(event) => setLibraryKeyword(event.target.value)}
              placeholder={t("ciSearchNameKeyFormula")}
            />
            <Select
              size="small"
              className="indicator-library-filter"
              value={libraryCategoryFilter}
              onChange={setLibraryCategoryFilter}
              options={[{ label: t("ciAllCategories"), value: "all" }, ...categoryOptions]}
            />
            <Select
              size="small"
              className="indicator-library-filter"
              value={libraryValueTypeFilter}
              onChange={setLibraryValueTypeFilter}
              options={[{ label: t("ciAllTypes"), value: "all" }, { label: t("ciBoolean"), value: "boolean" }, { label: t("ciNumber"), value: "number" }]}
            />
            <Select
              size="small"
              className="indicator-library-filter"
              value={libraryStatusFilter}
              onChange={setLibraryStatusFilter}
              options={[
                { label: t("ciAllStatus"), value: "all" },
                { label: t("ciStatusEnabled"), value: "enabled" },
                { label: t("ciDisabled"), value: "disabled" },
              ]}
            />
            <Select
              size="small"
              className="indicator-library-filter"
              value={libraryScopeFilter}
              onChange={setLibraryScopeFilter}
              options={[{ label: t("ciAllScopes"), value: "all" }, ...scopeOptions]}
            />
          </div>
          <div className="indicator-library-summary">
            <Tag color="blue">{template("ciEnabledCount", { count: rows.filter((row) => row.enabled).length })}</Tag>
            <Tag color="gold">{template("ciNumericCount", { count: rows.filter((row) => row.value_type === "number").length })}</Tag>
            <Tag color="green">{template("ciBooleanCount", { count: rows.filter((row) => row.value_type === "boolean").length })}</Tag>
          </div>
          <div className="indicator-library-table-wrap">
            <Table<CustomIndicator>
              className="indicator-library-table"
              size="small"
              rowKey="id"
              loading={loading}
              dataSource={filteredRows}
              pagination={{ pageSize: 8, showSizeChanger: false }}
              scroll={{ x: 760 }}
              locale={{ emptyText: t("ciNoFormulaMatchFilter") }}
              onRow={(record) => ({ onClick: () => loadIndicator(record) })}
              rowClassName={(record) => record.id === selectedId ? "selected-row" : ""}
              columns={[
                { title: t("ciName"), dataIndex: "name", render: (value, row) => <Space size={6}><span>{value}</span>{!row.enabled && <Tag>{t("ciDisabled")}</Tag>}</Space> },
                { title: t("ciKey"), dataIndex: "key", width: 140 },
                { title: t("ciCategory"), dataIndex: "category", width: 110, render: (value: string) => categoryOptions.find((item) => item.value === value)?.label ?? value },
                { title: t("ciReturnType"), dataIndex: "value_type", width: 90, render: (value: string) => value === "number" ? t("ciNumber") : t("ciBoolean") },
                { title: t("ciScope"), dataIndex: "scope", render: (scope: string[]) => (scope || []).map((item) => <Tag key={item}>{scopeOptions.find((opt) => opt.value === item)?.label ?? item}</Tag>) },
                { title: t("ciVersion"), dataIndex: "version", width: 64 },
              ]}
            />
          </div>
        </div>
      </Card>

      <Card className="indicator-card indicator-card--editor" size="small" title={selected ? t("ciEditIndicator") + selected.name : t("ciNewIndicator")}>
        <Form layout="vertical">
          <div className="indicator-editor-head">
            <Space wrap className="indicator-editor-meta">
              <Tag>{editorMeta.category}</Tag>
              <Tag color={form.value_type === "number" ? "gold" : "blue"}>{editorMeta.valueType}</Tag>
              <Tag color={form.enabled ? "green" : "default"}>{form.enabled ? t("ciEnabled") : t("ciDisabled")}</Tag>
              {selected && <Tag>{t("ciVersion") + " " + selected.version}</Tag>}
            </Space>
          </div>
          <div className="item-subline indicator-editor-summary">{t("ciKey") + ": " + (form.key || "-") + DOT + (selected ? "ID " + selected.id : t("ciNew")) + DOT + t("ciScope") + ": " + form.scope.length + " " + t("items")}</div>
          <div className="formula-workbench">
            <div className="formula-workbench-main">
              <Card className="formula-section-card" size="small" title={t("ciBasicInfo")}>
                <div className="formula-section-stack">
                  <div className="indicator-form-grid">
                    <Form.Item label={t("ciName")}><Input value={form.name} onChange={(event) => { const name = event.target.value; setForm((prev) => ({ ...prev, name, key: prev.key || keyFromName(name) })); }} placeholder={t("ciNamePlaceholder")} /></Form.Item>
                    <Form.Item label={t("ciKey")}><Input value={form.key} onChange={(event) => setForm((prev) => ({ ...prev, key: event.target.value }))} placeholder="strong_trend_filter" /></Form.Item>
                    <Form.Item label={t("ciCategory")}><Select value={form.category} options={categoryOptions} onChange={(value) => setForm((prev) => ({ ...prev, category: value }))} /></Form.Item>
                    <Form.Item label={t("ciReturnType")}><Select value={form.value_type} options={[{ label: t("ciBoolean"), value: "boolean" }, { label: t("ciNumber"), value: "number" }]} onChange={(value) => setForm((prev) => ({ ...prev, value_type: value }))} /></Form.Item>
                  </div>
                  <Form.Item label={t("ciDescription")} style={{ marginBottom: 0 }}><Input value={form.description} onChange={(event) => setForm((prev) => ({ ...prev, description: event.target.value }))} /></Form.Item>
                </div>
              </Card>

              <Card className="formula-section-card" size="small" title={t("ciFormulaEditor")}>
                <div className="formula-section-stack">
                  <Space wrap className="formula-stat-row">
                    <Tag color="blue">{(clauseSummary.clauses.length || (form.formula.trim() ? 1 : 0)) + " " + t("ciFormulaStatClauses")}</Tag>
                    <Tag color="geekblue">{selectedFunctionDocs.length + " " + t("ciFormulaStatFunctions")}</Tag>
                    <Tag color="purple">{selectedReferenceDocs.length + " " + t("ciFormulaStatReferences")}</Tag>
                    <Button size="small" type="primary" ghost icon={<RobotOutlined />} onClick={() => setAiChatOpen(true)} style={{ marginLeft: "auto" }}>
                      {t("aiAskButton")}
                    </Button>
                  </Space>
                  <Form.Item label={t("ciFormula")} style={{ marginBottom: 0 }}><Input.TextArea rows={10} value={form.formula} onChange={(event) => setForm((prev) => ({ ...prev, formula: event.target.value }))} placeholder="sma(20) > sma(60) and rsi(14) < 70" /></Form.Item>
                  <div className="indicator-form-grid compact">
                    <Form.Item label={t("ciScope")}><Checkbox.Group options={scopeOptions} value={form.scope} onChange={(value) => setForm((prev) => ({ ...prev, scope: value as string[] }))} /></Form.Item>
                    <Form.Item label={t("ciEnabled")}><Checkbox checked={form.enabled} onChange={(event) => setForm((prev) => ({ ...prev, enabled: event.target.checked }))}>{form.enabled ? t("ciActiveAfterSave") : t("ciSaveAsDisabled")}</Checkbox></Form.Item>
                  </div>
                  {selectedId && <Form.Item label={t("ciChangeNote")}><Input value={changeNote} onChange={(event) => setChangeNote(event.target.value)} placeholder={t("ciChangeNotePlaceholder")} /></Form.Item>}
                  <Space wrap className="indicator-action-row"><Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={save}>{t("ciSaveIndicator")}</Button>{selectedId && form.value_type === "number" && <Button type="primary" ghost icon={<RiseOutlined />} onClick={openPromote} title={t("ciPromoteToFactorTip")}>{t("ciPromoteToFactor")}</Button>}{selectedId && <Popconfirm title={t("ciConfirmDeleteIndicator")} onConfirm={() => remove(selectedId)}><Button danger icon={<DeleteOutlined />}>{t("ciDelete")}</Button></Popconfirm>}</Space>
                </div>
              </Card>
            </div>

            <div className="formula-workbench-side">
              <Card size="small" title={t("ciTemplatesAndFunctions")} className="formula-section-card">
                <div className="formula-section-stack">
                  <div className="formula-resource-toolbar">
                    <Input
                      allowClear
                      size="small"
                      value={resourceKeyword}
                      onChange={(event) => setResourceKeyword(event.target.value)}
                      placeholder={t("ciSearchTemplateFunctionUsage")}
                    />
                    <div className="formula-resource-toolbar__filters">
                      <Select
                        size="small"
                        value={resourceCategoryFilter}
                        onChange={setResourceCategoryFilter}
                        options={[{ label: t("ciAllCategories"), value: "all" }, ...categoryOptions]}
                        style={{ width: 128 }}
                      />
                      <Select
                        size="small"
                        value={resourceScopeFilter}
                        onChange={setResourceScopeFilter}
                        options={[{ label: t("ciAllScopes"), value: "all" }, ...scopeOptions]}
                        style={{ width: 132 }}
                      />
                    </div>
                  </div>

                  <div>
                    <div className="formula-resource-section-head">
                      <div>
                        <div className="metric-label formula-section-label">{t("ciTemplates")}</div>
                        <div className="item-subline formula-section-note">
                          {t("ciTemplateHint")}
                        </div>
                      </div>
                      <Tag>{filteredFormulaTemplates.length}</Tag>
                    </div>
                    <div className="formula-resource-list">
                      {filteredFormulaTemplates.length > 0 ? filteredFormulaTemplates.map((item) => (
                        <div key={item.key} className="formula-resource-card">
                          <div className="formula-resource-card__head">
                            <div>
                              <strong>{isZh ? item.nameZh : item.nameEn}</strong>
                              <div className="item-subline">{isZh ? item.descriptionZh : item.descriptionEn}</div>
                            </div>
                            <Button size="small" type="primary" onClick={() => applyTemplate(item)}>
                              {t("ciApply")}
                            </Button>
                          </div>
                          <Space wrap size={6}>
                            <Tag>{categoryOptions.find((option) => option.value === item.category)?.label ?? item.category}</Tag>
                            <Tag color={item.valueType === "number" ? "gold" : "blue"}>{item.valueType === "number" ? t("ciNumber") : t("ciBoolean")}</Tag>
                            {(item.scope || []).map((scope) => (
                              <Tag key={scope}>{scopeOptions.find((option) => option.value === scope)?.label ?? scope}</Tag>
                            ))}
                          </Space>
                          <code className="formula-inline-code">{item.formula}</code>
                        </div>
                      )) : <div className="item-subline">{t("ciNoTemplateMatchFilter")}</div>}
                    </div>
                  </div>

                  <div>
                    <div className="formula-resource-section-head">
                      <div>
                        <div className="metric-label formula-section-label">{t("ciFunctionInsert")}</div>
                        <div className="item-subline formula-section-note">
                          {t("ciFunctionInsertHint")}
                        </div>
                      </div>
                      <Radio.Group
                        size="small"
                        optionType="button"
                        buttonStyle="solid"
                        value={functionViewMode}
                        onChange={(event) => setFunctionViewMode(event.target.value)}
                      >
                        <Radio.Button value="all">{t("ciAllFunctions")}</Radio.Button>
                        <Radio.Button value="used">{t("ciUsedOnly")}</Radio.Button>
                      </Radio.Group>
                    </div>
                    <div className="formula-resource-list">
                      {filteredFunctionLibrary.length > 0 ? filteredFunctionLibrary.map((item) => (
                        <div key={item.key} className="formula-resource-card">
                          <div className="formula-resource-card__head">
                            <Space size={8} wrap>
                              <strong>{item.key}</strong>
                              <Tag>{item.snippet}</Tag>
                            </Space>
                            <Button size="small" onClick={() => insertFormulaSnippet(item.snippet)}>
                              {t("ciInsert")}
                            </Button>
                          </div>
                          <div className="item-subline">{isZh ? item.descriptionZh : item.descriptionEn}</div>
                          <div className="item-subline">{isZh ? item.referenceZh : item.referenceEn}</div>
                          <div className="item-subline">{t("ciExampleLabel") + item.example}</div>
                        </div>
                      )) : <div className="item-subline">{functionViewMode === "used" ? t("ciNoFunctionDetected") : t("ciNoFunctionMatchFilter")}</div>}
                    </div>
                  </div>
                </div>
              </Card>

              <Card className="indicator-usage-card formula-section-card" size="small" title={t("ciFormulaGuideTitle")}>
                <div className="formula-section-stack">
                  <div>
                    <div className="metric-label formula-section-label">{t("ciFormulaWhatDoing")}</div>
                    <div className="item-subline">{form.value_type === "boolean" ? template("ciBooleanFormulaDesc", { count: String(clauseSummary.clauses.length || 1), connectors: clauseSummary.connectors.join(" / ") || t("ciSingleCondition") }) : t("ciNumericFormulaDesc")}</div>
                    <div className="formula-guide-usage">
                      <div className="formula-guide-usage__card">
                        <div className="metric-label">{t("ciUseIn")}</div>
                        <Space wrap size={6}>
                          {formulaUseCaseLabels.length > 0 ? formulaUseCaseLabels.map((label) => <Tag key={label}>{label}</Tag>) : <span className="item-subline">-</span>}
                        </Space>
                      </div>
                      <div className="formula-guide-usage__card">
                        <div className="metric-label">{t("ciCurrentStructure")}</div>
                        <div className="item-subline">
                          {template("ciStructureSummary", { clauses: String(clauseSummary.clauses.length || 1), functions: String(selectedFunctionDocs.length), references: String(selectedReferenceDocs.length) })}
                        </div>
                      </div>
                    </div>
                  </div>
                  <div>
                    <div className="metric-label formula-section-label">{t("ciFunctionsUsed")}</div>
                    {selectedFunctionDocs.length > 0 ? <div className="formula-doc-list">{selectedFunctionDocs.map((item) => <div key={item.key} className="formula-doc-item"><div className="formula-doc-item__head"><strong>{item.key}</strong><Tag>{item.snippet}</Tag></div><div className="item-subline">{isZh ? item.descriptionZh : item.descriptionEn}</div><div className="item-subline">{isZh ? item.referenceZh : item.referenceEn}</div><div className="item-subline">{t("ciExampleLabel") + item.example}</div></div>)}</div> : <div className="item-subline">{t("ciNoFunctionDetectedHint")}</div>}
                  </div>
                  <div>
                    <div className="metric-label formula-section-label">{t("ciReferencedValues")}</div>
                    {selectedReferenceDocs.length > 0 ? <div className="formula-doc-list">{selectedReferenceDocs.map((item) => <div key={item.key} className="formula-doc-item"><div className="formula-doc-item__head"><strong>{item.label}</strong><Tag>{item.key}</Tag></div><div className="item-subline">{isZh ? item.descriptionZh : item.descriptionEn}</div><div className="item-subline">{isZh ? item.sourceZh : item.sourceEn}</div></div>)}</div> : <div className="item-subline">{t("ciNoReferenceDetected")}</div>}
                  </div>
                </div>
              </Card>

              <Card className="indicator-preview-card formula-section-card" size="small" title={t("ciFormulaPreview")}>
                <div className="indicator-preview-panel">
                  <div className="indicator-form-grid compact">
                    <Form.Item label={t("ciPreviewSymbol")} style={{ marginBottom: 0 }}><Select showSearch allowClear placeholder={t("ciSearchCodeOrName")} value={previewSymbolId ?? undefined} filterOption={false} onSearch={(value) => { searchPreviewSymbols(value).catch(() => {}); }} onChange={(value) => setPreviewSymbolId(value ?? null)} options={previewSymbolOptions.map((item) => ({ label: item.symbol + " | " + item.name, value: item.id }))} /></Form.Item>
                    <Form.Item label={t("ciPreviewDate")} style={{ marginBottom: 0 }}><DatePicker value={previewTradeDate} onChange={setPreviewTradeDate} placeholder={t("ciDefaultLatestBar")} allowClear /></Form.Item>
                  </div>
                  <div className="item-subline">{previewSymbol ? t("ciPreviewCurrentLabel") + previewSymbol.symbol + " | " + previewSymbol.name + (previewTradeDate ? t("ciPreviewDateLabel") + previewTradeDate.format("YYYY-MM-DD") : t("ciPreviewLatestBar")) : t("ciPreviewHint")}</div>
                  <div className="indicator-preview-config">
                    <div className="indicator-preview-count">
                      <span className="metric-label">{t("ciRecentN")}</span>
                      <InputNumber min={1} max={30} value={previewRecentCount} onChange={(value) => setPreviewRecentCount(value ?? 7)} size="small" style={{ width: 96 }} />
                    </div>
                  </div>
                  <Space wrap className="indicator-preview-actions">
                    {activeSymbol && <Button onClick={() => setPreviewSymbolId(activeSymbol.id)}>{t("ciUseCurrentSymbol")}</Button>}
                    <Button icon={<EyeOutlined />} loading={previewLoading} onClick={previewFormula} disabled={!previewSymbolId}>{t("ciRunPreview")}</Button>
                    {previewResult && <Tag color="blue">{t("ciBarDate") + " " + previewResult.trade_date}</Tag>}
                    {previewResult && <Tag color={previewResult.value_type === "number" ? "gold" : "green"}>{previewResult.value_type === "number" ? t("ciNumber") : t("ciBoolean")}</Tag>}
                  </Space>
                  {previewFeedback && <Alert showIcon type={previewFeedback.type} message={previewFeedback.message} description={renderPreviewFeedbackDescription(previewFeedback)} style={{ marginTop: 12 }} />}
                  {previewResult ? <><div className="indicator-preview-grid"><div><div className="metric-label">{t("ciPreviewResult")}</div><strong>{previewValue(previewResult)}</strong></div><div><div className="metric-label">{t("ciClose")}</div><strong>{scoreValue(previewResult.latest_bar.close)}</strong></div><div><div className="metric-label">{t("ciOpen")}</div><strong>{scoreValue(previewResult.latest_bar.open)}</strong></div><div><div className="metric-label">{t("ciQualityScore")}</div><strong>{scoreValue(previewResult.score_snapshot?.quality_score)}</strong></div><div><div className="metric-label">{t("ciTimingScore")}</div><strong>{scoreValue(previewResult.score_snapshot?.timing_score)}</strong></div><div><div className="metric-label">{t("ciTrendScore")}</div><strong>{scoreValue(previewResult.score_snapshot?.trend_score)}</strong></div><div><div className="metric-label">{t("ciMomentumScore")}</div><strong>{scoreValue(previewResult.score_snapshot?.momentum_score)}</strong></div></div><Card size="small" title={t("ciPreviewExplanationTitle")} style={{ marginTop: 16 }}><div style={{ display: "grid", gap: 14 }}><div className="item-subline">{form.value_type === "boolean" ? template("ciBooleanResultExplain", { result: previewValue(previewResult), total: String(clauseSummary.clauses.length || 1), explainable: String(previewClauseStats.explainable) }) : template("ciNumericResultExplain", { result: previewValue(previewResult) })}</div><div className="formula-guide-usage"><div className="formula-guide-usage__card"><div className="metric-label">{t("ciExplainedClauses")}</div><strong>{previewClauseStats.explainable}</strong></div><div className="formula-guide-usage__card"><div className="metric-label">{t("ciPassingNow")}</div><strong>{previewClauseStats.passed}</strong></div><div className="formula-guide-usage__card"><div className="metric-label">{t("ciFailingNow")}</div><strong>{previewClauseStats.failed}</strong></div></div><div><div className="metric-label formula-section-label">{t("ciClauseBreakdown")}</div><div className="formula-doc-list">{clauseExplanations.length > 0 ? clauseExplanations.map((item, index) => <div key={item.text + '-' + index} className="formula-doc-item"><div className="formula-doc-item__head"><Tag color={item.status === "passed" ? "green" : item.status === "failed" ? "red" : "default"}>{item.status === "passed" ? t("ciPass") : item.status === "failed" ? t("ciFail") : t("ciNeedsManualCheck")}</Tag><strong>{item.text}</strong>{clauseSummary.connectors[index - 1] && <Tag>{clauseSummary.connectors[index - 1]}</Tag>}</div>{(item.leftText || item.rightText) && <div className="item-subline">{(item.leftText || "-") + " " + (item.operator || "") + " " + (item.rightText || "-") + ((item.leftValue != null || item.rightValue != null) ? (" ≈ " + (item.leftValue ?? "?") + " " + (item.operator || "") + " " + (item.rightValue ?? "?")) : "")}</div>}</div>) : <div className="item-subline">{t("ciNoClauseBreakdown")}</div>}</div></div><div><div className="metric-label formula-section-label">{t("ciCurrentReferenceValues")}</div><div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 8 }}>{previewReferenceValues.map((item) => <div key={item.key} className="formula-reference-card"><div className="metric-label">{item.label}</div><strong>{item.value}</strong><div className="item-subline">{item.source}</div></div>)}</div></div></div></Card>
                    <div className="indicator-preview-series">
                      <div className="formula-resource-section-head">
                        <div>
                          <div className="metric-label formula-section-label">{t("ciRecentResults")}</div>
                          <div className="item-subline">{t("ciRecentResultsHint")}</div>
                        </div>
                        <Tag>{previewRecentResults.length}</Tag>
                      </div>
                      {previewRecentResults.length > 0 ? (
                        <Table<CustomIndicatorPreviewSeriesItem>
                          className="indicator-preview-series-table"
                          size="small"
                          rowKey={(item) => item.trade_date}
                          pagination={false}
                          scroll={{ x: 760 }}
                          dataSource={previewRecentResults}
                          columns={[
                            { title: t("ciBarDate"), dataIndex: "trade_date", width: 108 },
                            { title: t("ciPreviewResult"), dataIndex: "display_value", width: 110, render: (_value, record) => previewValue(record) },
                            { title: t("ciClose"), dataIndex: ["latest_bar", "close"], width: 96, render: (_value, record) => scoreValue(record.latest_bar.close) },
                            { title: t("ciOpen"), dataIndex: ["latest_bar", "open"], width: 96, render: (_value, record) => scoreValue(record.latest_bar.open) },
                            { title: t("ciTimingScore"), dataIndex: ["score_snapshot", "timing_score"], width: 96, render: (_value, record) => scoreValue(record.score_snapshot?.timing_score) },
                            { title: t("ciTrendScore"), dataIndex: ["score_snapshot", "trend_score"], width: 96, render: (_value, record) => scoreValue(record.score_snapshot?.trend_score) },
                          ]}
                          rowClassName={(_, index) => index === 0 ? "preview-series-latest-row" : ""}
                          locale={{ emptyText: t("ciNoRecentResults") }}
                        />
                      ) : (
                        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("ciNoRecentResults")} />
                      )}
                    </div>
                  </> : <div className="item-subline">{t("ciPreviewExplanation")}</div>}
                </div>
              </Card>

              {selected && <Card className="indicator-usage-card formula-section-card" size="small" title={t("ciUsage")}><div className="indicator-usage-grid"><div><div className="metric-label">{t("ciDiscoveryPlan")}</div><Space wrap className="indicator-preview-actions">{usageSummary.discovery.length > 0 ? usageSummary.discovery.map((plan) => <Tag key={'plan_' + plan.id}>{plan.name}</Tag>) : <span className="item-subline">{t("ciNoReferences")}</span>}</Space></div><div><div className="metric-label">{t("ciBacktestTemplate")}</div><Space wrap className="indicator-preview-actions">{usageSummary.backtest.length > 0 ? usageSummary.backtest.map((tpl) => <Tag key={'tpl_' + tpl.id} color="blue">{tpl.name}</Tag>) : <span className="item-subline">{t("ciNoReferences")}</span>}</Space></div></div></Card>}

              {selected && <Card className="indicator-version-card formula-section-card" size="small" title={<Space>{t("ciVersionHistory")}<Tag>{versions.length}</Tag></Space>} extra={versionsLoading ? <Tag>{t("ciLoading")}</Tag> : <Button size="small" icon={<ReloadOutlined />} onClick={() => loadVersions(selected.id)}>{t("refresh")}</Button>}>
                {versions.length > 0 ? (
                  <div className="formula-version-list">
                    {versions.map((ver) => {
                      const isCurrent = ver.version === selected.version;
                      const isExpanded = expandedVersionId === ver.id;
                      const formulaDiff = ver.formula !== selected.formula;
                      return (
                        <div key={ver.id} className={"formula-version-item" + (isCurrent ? " formula-version-item--current" : "")}>
                          <div className="formula-version-item__head" onClick={() => setExpandedVersionId(isExpanded ? null : ver.id)} style={{ cursor: "pointer" }}>
                            <Space size={8}>
                              <Tag color={isCurrent ? "green" : "default"}>v{ver.version}</Tag>
                              {isCurrent && <Tag color="green">{t("ciVersionCurrent")}</Tag>}
                              <span className="item-subline">{new Date(ver.created_at).toLocaleString()}</span>
                            </Space>
                            {!isCurrent && <Popconfirm title={template("ciRollbackConfirm", { version: String(ver.version) })} onConfirm={(e) => { e?.stopPropagation(); rollbackToVersion(ver.version); }}><Button size="small" type="link" onClick={(e) => e.stopPropagation()}>{t("ciRollback")}</Button></Popconfirm>}
                          </div>
                          {ver.change_note && <div className="item-subline" style={{ marginTop: 4 }}>{ver.change_note}</div>}
                          {isExpanded && (
                            <div className="formula-version-item__detail" style={{ marginTop: 8 }}>
                              <div>
                                <div className="metric-label">{t("ciVersionFormula")}{formulaDiff && <Tag color="orange" style={{ marginLeft: 8 }}>{t("ciVersionDiff")}</Tag>}</div>
                                <code className="formula-inline-code" style={{ display: "block", whiteSpace: "pre-wrap", marginTop: 4 }}>{ver.formula}</code>
                              </div>
                              <div style={{ marginTop: 8 }}>
                                <div className="metric-label">{t("ciReturnType")}</div>
                                <Tag color={ver.value_type === "number" ? "gold" : "blue"}>{ver.value_type === "number" ? t("ciNumber") : t("ciBoolean")}</Tag>
                              </div>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="item-subline">{t("ciNoVersionHistory")}</div>
                )}
              </Card>}
            </div>
          </div>
        </Form>
      </Card>

      <AiChatDrawer
        open={aiChatOpen}
        formula={form.formula}
        onClose={() => setAiChatOpen(false)}
        onInsertFormula={insertFormulaFromAi}
      />

      {/* WP4-02: 提升为因子草稿 Modal（仅数值型指标显示入口） */}
      <Modal
        title={t("ciPromoteTitle")}
        open={promoteOpen}
        onOk={confirmPromote}
        onCancel={() => setPromoteOpen(false)}
        okText={t("ciPromoteConfirm")}
        cancelText={t("ciPromoteCancel")}
        okButtonProps={{ loading: promoteLoading }}
        destroyOnHidden
        width={520}
      >
        <Alert
          type="info"
          showIcon
          message={t("ciPromoteMappingHint")}
          style={{ marginBottom: 12 }}
        />
        <Descriptions
          size="small"
          column={1}
          bordered
          style={{ marginBottom: 12 }}
        >
          <Descriptions.Item label={t("ciPromoteFieldIndicatorName")}>{form.name || "—"}</Descriptions.Item>
          <Descriptions.Item label={t("ciPromoteFieldIndicatorKey")}>{form.key || "—"}</Descriptions.Item>
          <Descriptions.Item label={t("ciPromoteFieldIndicatorVersion")}>{selected?.version ?? "—"}</Descriptions.Item>
          <Descriptions.Item label={t("ciPromoteFieldFormula")}><code style={{ fontSize: 12 }}>{form.formula || "—"}</code></Descriptions.Item>
          <Descriptions.Item label={t("ciPromoteFieldCategory")}>{form.category || "—"}</Descriptions.Item>
        </Descriptions>
        <Form layout="vertical" size="small">
          <div className="indicator-form-grid compact">
            <Form.Item label={t("ciPromoteFactorCode")}>
              <Input
                value={promoteCode}
                onChange={(e) => setPromoteCode(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))}
                placeholder={t("ciPromoteFactorCodePlaceholder")}
              />
            </Form.Item>
            <Form.Item label={t("ciPromoteFactorName")}>
              <Input value={promoteName} onChange={(e) => setPromoteName(e.target.value)} placeholder={form.name} />
            </Form.Item>
            <Form.Item label={t("ciPromoteDirection")}>
              <Select
                value={promoteDirection}
                onChange={(v: "higher_better" | "lower_better" | "nonlinear") => setPromoteDirection(v)}
                options={[
                  { label: t("factorDirection_higher_better"), value: "higher_better" },
                  { label: t("factorDirection_lower_better"), value: "lower_better" },
                  { label: t("factorDirection_nonlinear"), value: "nonlinear" },
                ]}
              />
            </Form.Item>
            <Form.Item label={t("ciPromoteRiskLevel")}>
              <Select
                value={promoteRiskLevel}
                onChange={(v: "low" | "medium" | "high") => setPromoteRiskLevel(v)}
                options={[
                  { label: t("factorRiskLevel_low"), value: "low" },
                  { label: t("factorRiskLevel_medium"), value: "medium" },
                  { label: t("factorRiskLevel_high"), value: "high" },
                ]}
              />
            </Form.Item>
          </div>
          <Form.Item label={t("ciPromoteChangeNote")}>
            <Input value={promoteChangeNote} onChange={(e) => setPromoteChangeNote(e.target.value)} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}



