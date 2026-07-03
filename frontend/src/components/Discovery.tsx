import { useState, useMemo, useCallback, useEffect } from "react";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
import { t, template, regionShortLabel, assetTypeLabel, stageLabel, actionLabel, DOT } from "../i18n";
import { Checkbox, Select, Button, Tag, Space, InputNumber, Switch, Input, Empty, Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { CustomIndicator, DiscoveryIndicatorEvaluation, DiscoveryPlan as StoredDiscoveryPlan, DiscoveryPlanFilter as StoredDiscoveryPlanFilter, WorkbenchCandidate } from "../types";
import {
  percent,
  score,
  joinParts,
  badgeClass,
  discoveryFreshness,
  withFinalOpportunityScore,
  opportunityScoreValue,
  ageDays,
} from "../utils/format";

const STEP_ORDER = ["prepare", "sync", "scan", "news", "done"] as const;
const STEP_LABEL_KEYS: Record<string, string> = {
  prepare: "stepPrepare",
  sync: "stepSync",
  scan: "stepScan",
  news: "stepNews",
  done: "stepDone",
};

type PoolTab = "all" | "highQuality" | "highTiming" | "actionable" | "overheatRisk" | "lowCredibility";
type IndicatorOperator = "gt" | "gte" | "lt" | "lte" | "eq" | "neq";
type LogicMode = "AND" | "OR";
type IndicatorFilter = StoredDiscoveryPlanFilter;
type IndicatorPlan = StoredDiscoveryPlan;

type IndicatorDraft = {
  name: string;
  logic: LogicMode;
  pool_tab: PoolTab;
  filters: IndicatorFilter[];
  selected_plan_id?: number;
};

const DRAFT_STORAGE_KEY = "discovery_indicator_draft_v1";
const NUMBER_OPERATORS: IndicatorOperator[] = ["gt", "gte", "lt", "lte", "eq", "neq"];
const BOOLEAN_OPERATORS: IndicatorOperator[] = ["eq", "neq"];
const POOL_TABS: PoolTab[] = ["all", "highQuality", "highTiming", "actionable", "overheatRisk", "lowCredibility"];

function scopeLabel(scope: string): string {
  switch (scope) {
    case "cn-stock":
      return t("discoveryScopeCnStock");
    case "cn-etf":
      return t("discoveryScopeCnEtf");
    case "us-stock":
      return t("discoveryScopeUsStock");
    case "us-etf":
      return t("discoveryScopeUsEtf");
    default:
      return scope;
  }
}

function stepClassName(task: { status: string; stage: string } | null, step: string): string {
  if (!task) return "";
  if (task.status === "done") return "done";
  const stageIdx = STEP_ORDER.indexOf(task.stage as (typeof STEP_ORDER)[number]);
  const stepIdx = STEP_ORDER.indexOf(step as (typeof STEP_ORDER)[number]);
  if (stageIdx < 0) return "";
  if (stepIdx < stageIdx) return "done";
  if (stepIdx === stageIdx) return "active";
  return "";
}

function compareIndicator(actual: boolean | number | null | undefined, operator: IndicatorOperator, expected: boolean | number) {
  if (actual == null) return false;
  if (typeof expected === "boolean") {
    if (operator === "eq") return actual === expected;
    if (operator === "neq") return actual !== expected;
    return false;
  }
  const actualNumber = typeof actual === "number" ? actual : Number(actual);
  if (Number.isNaN(actualNumber)) return false;
  switch (operator) {
    case "gt":
      return actualNumber > expected;
    case "gte":
      return actualNumber >= expected;
    case "lt":
      return actualNumber < expected;
    case "lte":
      return actualNumber <= expected;
    case "eq":
      return actualNumber === expected;
    case "neq":
      return actualNumber !== expected;
    default:
      return false;
  }
}

function makeId(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
}

function indicatorOperators(valueType?: string): IndicatorOperator[] {
  return valueType === "number" ? NUMBER_OPERATORS : BOOLEAN_OPERATORS;
}

function normalizeOperator(operator: IndicatorOperator | string | undefined, valueType?: string): IndicatorOperator {
  const options = indicatorOperators(valueType);
  if (operator && options.includes(operator as IndicatorOperator)) {
    return operator as IndicatorOperator;
  }
  return valueType === "number" ? "gte" : "eq";
}

function makeEmptyFilter(defaultIndicatorKey?: string): IndicatorFilter {
  return {
    id: makeId("filter"),
    indicator_key: defaultIndicatorKey,
    operator: defaultIndicatorKey ? "gte" : "eq",
    number_value: 0,
    boolean_value: true,
  };
}

function normalizeFilter(
  filter: Partial<IndicatorFilter> | undefined,
  indicatorMap: Record<string, CustomIndicator>,
  fallbackIndicatorKey?: string,
): IndicatorFilter {
  const indicatorKey = typeof filter?.indicator_key === "string" && filter.indicator_key
    ? filter.indicator_key
    : fallbackIndicatorKey;
  const indicator = indicatorKey ? indicatorMap[indicatorKey] : undefined;
  return {
    id: typeof filter?.id === "string" && filter.id ? filter.id : makeId("filter"),
    indicator_key: indicatorKey,
    operator: normalizeOperator(filter?.operator, indicator?.value_type),
    number_value: typeof filter?.number_value === "number" && !Number.isNaN(filter.number_value) ? filter.number_value : 0,
    boolean_value: typeof filter?.boolean_value === "boolean" ? filter.boolean_value : true,
  };
}

function normalizePlan(
  plan: Partial<IndicatorPlan> | undefined,
  indicatorMap: Record<string, CustomIndicator>,
  fallbackIndicatorKey?: string,
): IndicatorPlan {
  const filters = Array.isArray(plan?.filters) && plan?.filters.length
    ? plan.filters.map((filter) => normalizeFilter(filter, indicatorMap))
    : [makeEmptyFilter(fallbackIndicatorKey)];
  return {
    id: typeof plan?.id === "number" ? plan.id : 0,
    name: typeof plan?.name === "string" ? plan.name : "",
    logic: plan?.logic === "OR" ? "OR" : "AND",
    pool_tab: POOL_TABS.includes(plan?.pool_tab as PoolTab) ? (plan?.pool_tab as PoolTab) : "actionable",
    filters,
    created_at: typeof plan?.created_at === "string" ? plan.created_at : new Date().toISOString(),
    updated_at: typeof plan?.updated_at === "string" && plan.updated_at ? plan.updated_at : new Date().toISOString(),
  };
}

function sortPlans(plans: IndicatorPlan[]): IndicatorPlan[] {
  return [...plans].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
}

function readStorage<T>(key: string): T | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: unknown) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // ignore localStorage write failures
  }
}

function indicatorDisplayValue(value: boolean | number | null | undefined): string {
  if (value == null) return "-";
  if (typeof value === "number") return score(value);
  return String(value);
}

function indicatorExpectedValue(filter: IndicatorFilter, indicator?: CustomIndicator): boolean | number {
  return indicator?.value_type === "number" ? filter.number_value : filter.boolean_value;
}

export default function Discovery() {
  const ctx = useApp();
  const workbench = ctx.workbench;
  const task = ctx.discoveryTask;
  const scopeStats = ctx.discoveryScopeStats;

  const [scope, setScope] = useState("cn-stock");
  const [minScore, setMinScore] = useState(55);
  const [dataMode, setDataMode] = useState("cached");
  const [coverageHint, setCoverageHint] = useState<string | null>(null);
  const [batchSize, setBatchSize] = useState(20);
  const [delay, setDelay] = useState(0.25);
  const [warningDays, setWarningDays] = useState(3);
  const [validDays, setValidDays] = useState(5);
  const [includeNews, setIncludeNews] = useState(true);
  const [starting, setStarting] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [rowActionLoading, setRowActionLoading] = useState<Record<number, { watchlist?: boolean; freeze?: boolean; update?: boolean }>>({});
  const [customIndicators, setCustomIndicators] = useState<CustomIndicator[]>([]);
  const [indicatorsLoaded, setIndicatorsLoaded] = useState(false);
  const [indicatorLoading, setIndicatorLoading] = useState(false);
  const [planLoading, setPlanLoading] = useState(false);
  const [indicatorFilters, setIndicatorFilters] = useState<IndicatorFilter[]>([]);
  const [filterLogic, setFilterLogic] = useState<LogicMode>("AND");
  const [indicatorValues, setIndicatorValues] = useState<Record<number, Record<string, boolean | number | null>>>({});
  const [poolTab, setPoolTab] = useState<PoolTab>("actionable");
  const [savedPlans, setSavedPlans] = useState<IndicatorPlan[]>([]);
  const [selectedPlanId, setSelectedPlanId] = useState<number | undefined>();
  const [planName, setPlanName] = useState("");
  const [filterStateReady, setFilterStateReady] = useState(false);

  const indicatorMap = useMemo(() => {
    return customIndicators.reduce<Record<string, CustomIndicator>>((acc, item) => {
      acc[item.key] = item;
      return acc;
    }, {});
  }, [customIndicators]);
  const defaultIndicatorKey = customIndicators[0]?.key;

  useEffect(() => {
    api.getDataHealth().then((data: any) => {
      const bars = data?.bars;
      if (!bars) return;
      const pct = bars.coverage_pct ?? 100;
      if (pct < 60) {
        setCoverageHint(template("coverageLow", { pct: pct.toFixed(0) }));
      } else if (pct < 85) {
        setCoverageHint(template("coverageMid", { pct: pct.toFixed(0) }));
      } else {
        setCoverageHint(null);
      }
    }).catch(() => {});
  }, [ctx.locale]);

  useEffect(() => {
    api.getCustomIndicators({ scope: "discovery", enabled: true })
      .then((rows) => setCustomIndicators(rows as CustomIndicator[]))
      .catch(() => setCustomIndicators([]))
      .finally(() => setIndicatorsLoaded(true));
  }, []);

  useEffect(() => {
    setPlanLoading(true);
    api.getDiscoveryPlans()
      .then((rows) => setSavedPlans(sortPlans((rows as IndicatorPlan[]).map((plan) => normalizePlan(plan, indicatorMap, defaultIndicatorKey)))))
      .catch(() => setSavedPlans([]))
      .finally(() => setPlanLoading(false));
  }, [defaultIndicatorKey, indicatorMap]);

  const isTaskActive = !!task && ["queued", "running"].includes(task.status);
  const canPause = isTaskActive;
  const canResume = task?.status === "paused" && !!task?.can_resume;
  const canCancel = !!task && ["queued", "running", "paused"].includes(task.status);
  const canStart = !isTaskActive;

  const sortedResults = useMemo(() => {
    if (!workbench) return [];
    return workbench.candidates
      .map((item) => withFinalOpportunityScore(item, ctx.newsSnapshot))
      .sort((a, b) => {
        const aFrozen = a.is_frozen ? 1 : 0;
        const bFrozen = b.is_frozen ? 1 : 0;
        if (aFrozen !== bFrozen) return bFrozen - aFrozen;
        const aScore = opportunityScoreValue(a);
        const bScore = opportunityScoreValue(b);
        if (bScore !== aScore) return bScore - aScore;
        return String(b.created_at ?? "").localeCompare(String(a.created_at ?? ""));
      });
  }, [workbench, ctx.newsSnapshot]);

  const candidatePools = useMemo(() => {
    const all = sortedResults;
    const highQuality = all.filter((item: any) => (item.quality_score ?? item.latest_score?.quality_score ?? 0) >= 70);
    const highTiming = all.filter((item: any) => {
      const timingScore = item.timing_score ?? item.latest_score?.timing_score ?? 0;
      const stage = item.stage ?? item.latest_score?.stage ?? "";
      return timingScore >= 65 && ["start", "accel"].includes(stage);
    });
    const actionable = all.filter((item: any) => {
      const priorityScore = item.priority_score ?? item.latest_score?.priority_score ?? 0;
      const action = item.action ?? item.latest_score?.action ?? "";
      return priorityScore >= 60 && action === "open";
    });
    const overheatRisk = all.filter((item: any) => {
      const stage = item.stage ?? item.latest_score?.stage ?? "";
      const action = item.action ?? item.latest_score?.action ?? "";
      return stage === "overheat" && action === "reduce";
    });
    const lowCredibility = all.filter((item: any) => {
      const createdAt = item.created_at ?? item.latest_score?.created_at ?? "";
      if (!createdAt) return true;
      try {
        const age = Math.floor((Date.now() - new Date(createdAt).getTime()) / 86400000);
        return age > 7;
      } catch {
        return true;
      }
    });
    return { all, highQuality, highTiming, actionable, overheatRisk, lowCredibility };
  }, [sortedResults]);

  const poolLabels: Record<PoolTab, { zh: string; en: string; color: string }> = {
    all: { zh: "全部候选", en: "All Candidates", color: "#6b7280" },
    highQuality: { zh: "高股质", en: "High Quality", color: "#0f766e" },
    highTiming: { zh: "高时点", en: "High Timing", color: "#2563eb" },
    actionable: { zh: "可执行", en: "Actionable", color: "#d97706" },
    overheatRisk: { zh: "过热风险", en: "Overheat Risk", color: "#b42318" },
    lowCredibility: { zh: "低可信度", en: "Low Credibility", color: "#9ca3af" },
  };

  useEffect(() => {
    if (!indicatorsLoaded || filterStateReady) return;

    const draft = readStorage<Partial<IndicatorDraft>>(DRAFT_STORAGE_KEY);
    if (draft) {
      const draftFilters = Array.isArray(draft.filters) && draft.filters.length
        ? draft.filters.map((filter) => normalizeFilter(filter, indicatorMap, defaultIndicatorKey))
        : [makeEmptyFilter(defaultIndicatorKey)];
      setIndicatorFilters(draftFilters);
      setFilterLogic(draft.logic === "OR" ? "OR" : "AND");
      setPoolTab(POOL_TABS.includes(draft.pool_tab as PoolTab) ? (draft.pool_tab as PoolTab) : "actionable");
      setPlanName(typeof draft.name === "string" ? draft.name : "");
      setSelectedPlanId(typeof draft.selected_plan_id === "number" ? draft.selected_plan_id : undefined);
    } else {
      setIndicatorFilters([makeEmptyFilter(defaultIndicatorKey)]);
    }

    setFilterStateReady(true);
  }, [defaultIndicatorKey, filterStateReady, indicatorMap, indicatorsLoaded]);

  useEffect(() => {
    if (!filterStateReady) return;
    writeStorage(DRAFT_STORAGE_KEY, {
      name: planName,
      logic: filterLogic,
      pool_tab: poolTab,
      filters: indicatorFilters,
      selected_plan_id: selectedPlanId,
    } satisfies IndicatorDraft);
  }, [filterLogic, filterStateReady, indicatorFilters, planName, poolTab, selectedPlanId]);

  useEffect(() => {
    if (!filterStateReady || !customIndicators.length) return;
    setIndicatorFilters((prev) => {
      const next = prev.length ? prev : [makeEmptyFilter(defaultIndicatorKey)];
      return next.map((filter) => normalizeFilter(filter, indicatorMap, defaultIndicatorKey));
    });
  }, [customIndicators.length, defaultIndicatorKey, filterStateReady, indicatorMap]);

  const currentPool = candidatePools[poolTab] ?? candidatePools.all;
  const activeFilters = useMemo(
    () => indicatorFilters.filter((filter) => !!filter.indicator_key),
    [indicatorFilters],
  );
  const requiredIndicatorKeys = useMemo(
    () => Array.from(new Set(activeFilters.map((filter) => filter.indicator_key).filter(Boolean) as string[])),
    [activeFilters],
  );

  useEffect(() => {
    if (!requiredIndicatorKeys.length || !sortedResults.length) {
      setIndicatorValues({});
      return;
    }
    const scanResultIds = sortedResults
      .map((item: any) => Number(item.scan_result_id ?? item.id))
      .filter((id: number) => id > 0);
    if (!scanResultIds.length) {
      setIndicatorValues({});
      return;
    }
    setIndicatorLoading(true);
    api.evaluateDiscoveryIndicators({
      scan_result_ids: scanResultIds,
      indicator_keys: requiredIndicatorKeys,
    }).then((rows) => {
      const map: Record<number, Record<string, boolean | number | null>> = {};
      (rows as DiscoveryIndicatorEvaluation[]).forEach((row) => {
        map[row.scan_result_id] = row.values;
      });
      setIndicatorValues(map);
    }).catch((error: any) => {
      setIndicatorValues({});
      ctx.showToast("error", error?.message || t("indicatorEvalFailed"));
    }).finally(() => {
      setIndicatorLoading(false);
    });
  }, [ctx, requiredIndicatorKeys, sortedResults]);

  const filteredPool = useMemo(() => {
    if (!activeFilters.length) return currentPool;
    return currentPool.filter((item: any) => {
      const resultId = Number(item.scan_result_id ?? item.id);
      const matched = activeFilters.map((filter) => {
        const indicator = filter.indicator_key ? indicatorMap[filter.indicator_key] : undefined;
        if (!indicator || !filter.indicator_key) return false;
        const actual = indicatorValues[resultId]?.[filter.indicator_key];
        const expected = indicator.value_type === "number" ? filter.number_value : filter.boolean_value;
        return compareIndicator(actual, filter.operator, expected);
      });
      return filterLogic === "AND" ? matched.every(Boolean) : matched.some(Boolean);
    });
  }, [activeFilters, currentPool, filterLogic, indicatorMap, indicatorValues]);

  const indicatorColumnTitle = activeFilters.length
    ? t("dpHitIndicator")
    : t("freshness");

  const resultMeta = workbench ? `${t("candidates")}: ${filteredPool.length}/${currentPool.length}` : "-";

  const setRowLoading = (symbolId: number, key: "watchlist" | "freeze" | "update", value: boolean) => {
    setRowActionLoading((prev) => ({
      ...prev,
      [symbolId]: { ...prev[symbolId], [key]: value },
    }));
  };

  const updateFilter = useCallback((filterId: string, updater: (current: IndicatorFilter) => IndicatorFilter) => {
    setIndicatorFilters((prev) => prev.map((filter) => (filter.id === filterId ? updater(filter) : filter)));
  }, []);

  const handleAddFilter = useCallback(() => {
    setIndicatorFilters((prev) => [...prev, makeEmptyFilter(defaultIndicatorKey)]);
  }, [defaultIndicatorKey]);

  const handleRemoveFilter = useCallback((filterId: string) => {
    setIndicatorFilters((prev) => {
      if (prev.length <= 1) return [makeEmptyFilter(defaultIndicatorKey)];
      return prev.filter((filter) => filter.id !== filterId);
    });
  }, [defaultIndicatorKey]);

  const handleIndicatorChange = useCallback((filterId: string, indicatorKey?: string) => {
    updateFilter(filterId, (filter) => {
      const indicator = indicatorKey ? indicatorMap[indicatorKey] : undefined;
      return {
        ...filter,
        indicator_key: indicatorKey,
        operator: normalizeOperator(filter.operator, indicator?.value_type),
      };
    });
  }, [indicatorMap, updateFilter]);

  const handleSavePlan = useCallback(async () => {
    const name = planName.trim();
    if (!name) {
      ctx.showToast("error", t("dpNameRequired"));
      return;
    }
    const payload = {
      name,
      logic: filterLogic,
      pool_tab: poolTab,
      filters: indicatorFilters,
    };

    try {
      const saved = selectedPlanId
        ? await api.updateDiscoveryPlan(selectedPlanId, payload)
        : await api.createDiscoveryPlan(payload);
      const normalized = normalizePlan(saved as IndicatorPlan, indicatorMap, defaultIndicatorKey);
      setSavedPlans((prev) => sortPlans([normalized, ...prev.filter((item) => item.id !== normalized.id)]));
      setSelectedPlanId(normalized.id);
      setPlanName(normalized.name);
      ctx.showToast("success", t("dpPlanSaved"));
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("dpSaveFailed"));
    }
  }, [ctx, defaultIndicatorKey, filterLogic, indicatorFilters, indicatorMap, planName, poolTab, selectedPlanId]);

  const handleLoadPlan = useCallback((planId?: number) => {
    if (!planId) {
      setSelectedPlanId(undefined);
      setPlanName("");
      setFilterLogic("AND");
      setIndicatorFilters([makeEmptyFilter(defaultIndicatorKey)]);
      return;
    }
    const plan = savedPlans.find((item) => item.id === planId);
    if (!plan) return;
    const normalized = normalizePlan(plan, indicatorMap, defaultIndicatorKey);
    setSelectedPlanId(normalized.id);
    setPlanName(normalized.name);
    setFilterLogic(normalized.logic);
    setPoolTab(normalized.pool_tab);
    setIndicatorFilters(normalized.filters);
  }, [defaultIndicatorKey, indicatorMap, savedPlans]);

  const handleDeletePlan = useCallback(async () => {
    if (!selectedPlanId) return;
    try {
      await api.deleteDiscoveryPlan(selectedPlanId);
      setSavedPlans((prev) => prev.filter((plan) => plan.id !== selectedPlanId));
      setSelectedPlanId(undefined);
      setPlanName("");
      ctx.showToast("success", t("dpPlanDeleted"));
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("dpDeleteFailed"));
    }
  }, [ctx, selectedPlanId]);

  const handleResetFilters = useCallback(() => {
    setSelectedPlanId(undefined);
    setPlanName("");
    setFilterLogic("AND");
    setIndicatorFilters([makeEmptyFilter(defaultIndicatorKey)]);
  }, [defaultIndicatorKey]);

  const handleStart = useCallback(async () => {
    setStarting(true);
    try {
      await ctx.runDiscoveryMining({
        scope,
        minScore,
        dataMode,
        batchSize,
        delaySeconds: delay,
        warningDays,
        validDays,
        includeNews,
      });
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setStarting(false);
    }
  }, [batchSize, ctx, dataMode, delay, includeNews, minScore, scope, validDays, warningDays]);

  const handlePause = useCallback(async () => {
    setPausing(true);
    try {
      await ctx.sendDiscoveryTaskCommand("pause");
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setPausing(false);
    }
  }, [ctx]);

  const handleResume = useCallback(async () => {
    setResuming(true);
    try {
      await ctx.sendDiscoveryTaskCommand("resume");
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setResuming(false);
    }
  }, [ctx]);

  const handleCancel = useCallback(async () => {
    setCancelling(true);
    try {
      await ctx.sendDiscoveryTaskCommand("cancel");
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setCancelling(false);
    }
  }, [ctx]);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await ctx.refreshDiscoveryTasks();
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setRefreshing(false);
    }
  }, [ctx]);

  const handleCleanup = useCallback(async () => {
    setCleaning(true);
    try {
      await ctx.cleanupExpiredDiscoveryResults();
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setCleaning(false);
    }
  }, [ctx]);

  const handleAddToWatchlist = useCallback(async (symbolId: number) => {
    setRowLoading(symbolId, "watchlist", true);
    try {
      await ctx.addSymbolToPrimaryWatchlist(symbolId);
      await ctx.loadWorkbench();
      ctx.showToast("success", t("discoveryAddedWatchlist"));
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setRowLoading(symbolId, "watchlist", false);
    }
  }, [ctx]);

  const handleRunBacktest = useCallback((symbolId: number) => {
    ctx.setActiveSymbolId(symbolId);
    ctx.setActiveTab("investment");
    ctx.showToast("success", t("discoveryNavigatedToBacktest"));
  }, [ctx]);

  const handleCreateJournal = useCallback(async (symbolId: number, item: WorkbenchCandidate) => {
    try {
      await api.createJournal({
        portfolio_id: ctx.portfolioId,
        symbol_id: symbolId,
        title: `${item.symbol || ""}${item.name ? " | " + item.name : ""} - ${t("discoveryCreateJournal")}`,
        entry_type: "discovery",
        stage: item.stage || "watching",
        action: item.action || "hold",
        review_note: item.reason_tags?.length ? `发现标签: ${item.reason_tags.join(", ")}` : "",
      });
      ctx.showToast("success", t("discoveryJournalCreated"));
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryJournalFailed"));
    }
  }, [ctx]);

  const handleToggleFreeze = useCallback(async (resultId: number, isFrozen: boolean, symbolId: number) => {
    setRowLoading(symbolId, "freeze", true);
    try {
      await api.updateDiscoveryResult(resultId, { is_frozen: !isFrozen });
      await ctx.loadWorkbench();
      ctx.showToast("success", t("discoveryRowFrozen"));
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setRowLoading(symbolId, "freeze", false);
    }
  }, [ctx]);

  const handleUpdateRow = useCallback(async (resultId: number, symbolId: number) => {
    setRowLoading(symbolId, "update", true);
    try {
      await api.refreshDiscoveryResult(resultId);
      await ctx.loadWorkbench();
      ctx.showToast("success", t("discoveryRowUpdated"));
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setRowLoading(symbolId, "update", false);
    }
  }, [ctx]);

  const handleRowClick = useCallback((symbolId: number) => {
    ctx.loadSymbolDetail(symbolId, { focus: true });
  }, [ctx]);

  const percentValue = task?.percent ?? 0;
  const progressTitle = task?.message || t("discoveryIdle");
  const progressPct = `${percentValue}%${DOT}${template("totalProgress", {
    total: task?.total ?? 0,
    processed: task?.processed ?? 0,
  })}`;
  const meta = task
    ? `${task.status}${DOT}${template("scanCounters", {
        ok: task.ok_count,
        empty: task.empty_count,
        failed: task.failed_count,
        scored: task.scored_count,
      })}`
    : "-";

  const columns: ColumnsType<WorkbenchCandidate> = useMemo(() => {
    const rankColumn = {
      title: t("rank"),
      key: "rank",
      width: 60,
      render: (_: unknown, __: WorkbenchCandidate, index: number) => index + 1,
    };
    const symbolColumn = {
      title: t("symbol"),
      key: "symbol",
      render: (_: unknown, item: WorkbenchCandidate) => (
        <div>
          <div className="symbol-title">
            <span className="symbol-code">{item.symbol}</span>
            <span className="symbol-name">{item.name}</span>
          </div>
          <div className="item-subline">{joinParts([regionShortLabel(item.region), assetTypeLabel(item.asset_type)])}</div>
          {(item.reason_tags ?? []).length > 0 && (
            <div className="item-reason-tags">
              {(item.reason_tags ?? []).slice(0, 3).map((tag: string, tagIndex: number) => (
                <span key={tagIndex} className="reason-tag">{tag}</span>
              ))}
            </div>
          )}
        </div>
      ),
    };
    const indicatorColumn = {
      title: indicatorColumnTitle,
      key: "indicator",
      render: (_: unknown, item: WorkbenchCandidate) => {
        const resultId = Number(item.scan_result_id ?? item.id);
        const freshness = discoveryFreshness(item);
        const rowAgeDays = ageDays(item.created_at);
        const isLowCredibility = rowAgeDays > 7 || !item.created_at;
        if (!activeFilters.length) {
          return (
            <>
              <span className={`freshness-chip ${freshness.className}`}>{freshness.label}</span>
              {isLowCredibility && <span className="credibility-badge credibility-low">{t("lowCredibility")}</span>}
            </>
          );
        }
        const indicatorEntries = activeFilters.map((filter) => {
          const key = filter.indicator_key as string;
          const indicator = indicatorMap[key];
          const actualValue = indicatorValues[resultId]?.[key];
          const expectedValue = indicatorExpectedValue(filter, indicator);
          const matched = compareIndicator(actualValue, filter.operator, expectedValue);
          return {
            key,
            name: indicator?.name ?? key,
            actualText: indicatorDisplayValue(actualValue),
            expectedText: indicatorDisplayValue(expectedValue),
            operator: filter.operator,
            matched,
          };
        });
        const matchedIndicatorCount = indicatorEntries.filter((entry) => entry.matched).length;
        if (!indicatorEntries.length) return "-";
        return (
          <div style={{ display: "grid", gap: 4 }}>
            <div className="discovery-indicator-tags">
              {indicatorEntries.map((entry) => (
                <span key={entry.key} className={`discovery-indicator-tag ${entry.matched ? "is-match" : "is-miss"}`}>
                  {entry.name}: {entry.actualText}
                </span>
              ))}
            </div>
            <div className="item-subline">
              {template("matchedIndicators", {
                matched: matchedIndicatorCount,
                total: indicatorEntries.length,
              })}
              {indicatorEntries.length > 0 && ` | ${indicatorEntries.map((entry) => `${entry.name} ${entry.operator} ${entry.expectedText}`).join(DOT)}`}
            </div>
          </div>
        );
      },
    };
    const operationsColumn = {
      title: t("operations"),
      key: "operations",
      width: 360,
      render: (_: unknown, item: WorkbenchCandidate) => {
        const inWatchlist = ctx.primaryWatchlistSymbolIds.has(item.symbol_id);
        const resultId = Number(item.scan_result_id ?? item.id);
        return (
          <Space size="small" wrap onClick={(e) => e.stopPropagation()}>
            <Button
              size="small"
              loading={rowActionLoading[item.symbol_id]?.watchlist}
              disabled={inWatchlist}
              onClick={() => handleAddToWatchlist(item.symbol_id)}
              aria-label={t("discoveryAddWatchlist")}
            >
              {inWatchlist ? t("discoveryInWatchlist") : t("discoveryAddWatchlist")}
            </Button>
            <Button
              size="small"
              onClick={() => handleRunBacktest(item.symbol_id)}
            >
              {t("discoveryRunBacktest")}
            </Button>
            <Button
              size="small"
              onClick={() => handleCreateJournal(item.symbol_id, item)}
            >
              {t("discoveryCreateJournal")}
            </Button>
            <Button
              size="small"
              loading={rowActionLoading[item.symbol_id]?.freeze}
              onClick={() => resultId && handleToggleFreeze(resultId, !!item.is_frozen, item.symbol_id)}
              aria-label={item.is_frozen ? t("unfreeze") : t("freeze")}
            >
              {item.is_frozen ? t("unfreeze") : t("freeze")}
            </Button>
          </Space>
        );
      },
    };
    return [
      rankColumn,
      symbolColumn,
      {
        title: t("finalOpportunityScore"),
        dataIndex: "opportunity_score",
        key: "opportunity_score",
        sorter: (a, b) => opportunityScoreValue(a) - opportunityScoreValue(b),
        render: (_: unknown, item: WorkbenchCandidate) => score(opportunityScoreValue(item)),
        width: 120,
      },
      {
        title: t("messageScore"),
        dataIndex: "news_message_score",
        key: "news_message_score",
        sorter: (a, b) => (a.news_message_score ?? 0) - (b.news_message_score ?? 0),
        render: (v: number | undefined) => score(v),
        width: 110,
      },
      {
        title: t("quality"),
        dataIndex: "quality_score",
        key: "quality_score",
        sorter: (a, b) => a.quality_score - b.quality_score,
        render: (v: number) => score(v),
        width: 90,
      },
      {
        title: t("timing"),
        dataIndex: "timing_score",
        key: "timing_score",
        sorter: (a, b) => a.timing_score - b.timing_score,
        render: (v: number) => score(v),
        width: 90,
      },
      {
        title: t("priority"),
        dataIndex: "priority_score",
        key: "priority_score",
        sorter: (a, b) => a.priority_score - b.priority_score,
        render: (v: number) => score(v),
        width: 90,
      },
      {
        title: t("stage"),
        dataIndex: "stage",
        key: "stage",
        render: (v: string) => <Tag className={badgeClass(v)}>{stageLabel(v)}</Tag>,
        width: 90,
      },
      {
        title: t("action"),
        dataIndex: "action",
        key: "action",
        render: (v: string) => <Tag className={badgeClass(v)}>{actionLabel(v)}</Tag>,
        width: 90,
      },
      {
        title: t("position"),
        dataIndex: "recommended_position_pct",
        key: "recommended_position_pct",
        sorter: (a, b) => (a.recommended_position_pct ?? 0) - (b.recommended_position_pct ?? 0),
        render: (v: number | undefined) => percent(v),
        width: 90,
      },
      indicatorColumn,
      operationsColumn,
    ];
  }, [t, indicatorColumnTitle, activeFilters, indicatorMap, indicatorValues, ctx.locale, ctx.primaryWatchlistSymbolIds, rowActionLoading, handleAddToWatchlist, handleRunBacktest, handleCreateJournal, handleToggleFreeze, handleUpdateRow]);

  return (
    <div className="tab-container" data-tab-content="discovery">
      <section className="band discovery-band">
        <div className="panel wide">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">{t("discoveryKicker")}</p>
              <h2>{t("discoveryTitle")}</h2>
            </div>
            <p className="panel-meta">
              {meta}
              {ctx.discoveryPolling ? ` ${DOT} ...` : ""}
            </p>
          </div>

          <div className="discovery-control-bar">
            <label className="inline-control">
              <span>{t("discoveryScope")}</span>
              <Select value={scope} onChange={(value) => setScope(value)} options={[
                { value: "cn-stock", label: t("discoveryScopeCnStock") },
                { value: "cn-etf", label: t("discoveryScopeCnEtf") },
                { value: "us-stock", label: t("discoveryScopeUsStock") },
                { value: "us-etf", label: t("discoveryScopeUsEtf") },
              ]} />
            </label>
            <label className="inline-control">
              <span>{t("minOpportunityScore")}</span>
              <InputNumber min={0} max={100} value={minScore} onChange={(value) => setMinScore(value ?? 0)} />
            </label>
            <label className="inline-control">
              <span>{t("discoveryDataMode")}</span>
              <Select value={dataMode} onChange={(value) => setDataMode(value)} options={[
                { value: "cached", label: t("discoveryModeCached") },
                { value: "sync", label: t("discoveryModeSync") },
              ]} />
            </label>
            <label className="inline-control">
              <span>{t("discoveryBatchSize")}</span>
              <InputNumber min={1} value={batchSize} onChange={(value) => setBatchSize(value ?? 1)} />
            </label>
            <label className="inline-control">
              <span>{t("discoveryDelay")}</span>
              <InputNumber min={0} step={0.05} value={delay} onChange={(value) => setDelay(value ?? 0)} />
            </label>
            <label className="inline-control">
              <span>{t("warningDays")}</span>
              <InputNumber min={1} value={warningDays} onChange={(value) => setWarningDays(value ?? 1)} />
            </label>
            <label className="inline-control">
              <span>{t("validDays")}</span>
              <InputNumber min={1} value={validDays} onChange={(value) => setValidDays(value ?? 1)} />
            </label>
            <label className="discovery-check">
              <Checkbox checked={includeNews} onChange={(event) => setIncludeNews(event.target.checked)}>
                {t("includeNewsScore")}
              </Checkbox>
            </label>
            <div className="discovery-actions">
              <Button id="discoveryRunButton" type="primary" loading={starting} onClick={handleStart} disabled={!canStart}>{t("startDiscovery")}</Button>
              <Button loading={pausing} onClick={handlePause} disabled={!canPause}>{t("pauseDiscovery")}</Button>
              <Button loading={resuming} onClick={handleResume} disabled={!canResume}>{t("resumeDiscovery")}</Button>
              <Button danger loading={cancelling} onClick={handleCancel} disabled={!canCancel}>{t("cancelDiscovery")}</Button>
              <Button loading={refreshing} onClick={handleRefresh}>{t("refreshResults")}</Button>
              <Button loading={cleaning} onClick={handleCleanup}>{t("cleanupExpired")}</Button>
            </div>
          </div>

          <div className="discovery-progress">
            <div className="discovery-progress-head">
              <strong>{progressTitle}</strong>
              <span>{progressPct}</span>
            </div>
            <div className="progress-track"><div className="progress-fill" style={{ width: percentValue + "%" }} /></div>
            <div className="discovery-steps">
              {STEP_ORDER.map((step) => (
                <span key={step} data-discovery-step={step} className={stepClassName(task, step)}>{t(STEP_LABEL_KEYS[step])}</span>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section className="band metrics-band">
        <div className="metric-grid discovery-metrics">
          <div className="metric-card"><span className="metric-label">{t("discoveryScope")}</span><span className="metric-value">{scopeLabel(scope)}</span><span className="metric-note">{scope}</span></div>
          <div className="metric-card"><span className="metric-label">{t("discoveryUniverseTotal")}</span><span className="metric-value">{scopeStats?.total_symbols ?? "-"}</span><span className="metric-note">{t("items")}</span></div>
          <div className="metric-card"><span className="metric-label">{t("discoveryCachedPool")}</span><span className="metric-value">{scopeStats?.cached_symbols ?? "-"}</span><span className="metric-note">{t("items")}</span></div>
          <div className="metric-card"><span className="metric-label">{t("candidates")}</span><span className="metric-value">{workbench?.candidates.length ?? 0}</span><span className="metric-note">{t("items")}</span></div>
          <div className="metric-card"><span className="metric-label">{t("discoveryCurrentRun")}</span><span className="metric-value">{task ? `${task.processed}/${task.total}` : "0/0"}</span><span className="metric-note">{t("items")}</span></div>
          <div className="metric-card"><span className="metric-label">{t("messageScore")}</span><span className="metric-value">{ctx.newsSnapshot?.symbols_total ?? 0}</span><span className="metric-note">{t("items")}</span></div>
        </div>
      </section>

      <section className="band">
        <div className="panel wide">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">{t("discoveryResultKicker")}</p>
              <h2>{t("discoveryResults")}</h2>
            </div>
            <p className="panel-meta">{resultMeta}</p>
          </div>

          {coverageHint && <div className="coverage-hint">{coverageHint}</div>}

          <div className="pool-tabs">
            {Object.entries(poolLabels).map(([key, cfg]) => (
              <button
                key={key}
                className={`pool-tab ${poolTab === key ? "pool-tab--active" : ""}`}
                style={{ borderColor: poolTab === key ? cfg.color : "transparent" }}
                onClick={() => setPoolTab(key as PoolTab)}
                aria-pressed={poolTab === key}
                aria-label={template("poolTabLabel", { label: ctx.locale === "zh-CN" ? cfg.zh : cfg.en })}
              >
                <span className="pool-tab-label">{ctx.locale === "zh-CN" ? cfg.zh : cfg.en}</span>
                <span className="pool-tab-count" style={{ backgroundColor: cfg.color }}>{(candidatePools as Record<string, any[]>)[key]?.length ?? 0}</span>
              </button>
            ))}
          </div>

          <div className="discovery-control-bar" style={{ marginTop: 12, alignItems: "flex-end" }}>
            <label className="inline-control" style={{ minWidth: 220 }}>
              <span>{t("dpSavedPlans")}</span>
              <Select allowClear placeholder={t("dpSelectPlan")} value={selectedPlanId} onChange={(value) => handleLoadPlan(value)} loading={planLoading} options={savedPlans.map((item) => ({ label: item.name || String(item.id), value: item.id }))} />
            </label>
            <label className="inline-control" style={{ minWidth: 240 }}>
              <span>{t("dpPlanName")}</span>
              <Input value={planName} onChange={(event) => setPlanName(event.target.value)} placeholder={t("dpNamePlaceholder")} />
            </label>
            <label className="inline-control">
              <span>{t("dpLogic")}</span>
              <Select value={filterLogic} onChange={(value) => setFilterLogic(value)} options={[{ label: "AND", value: "AND" }, { label: "OR", value: "OR" }]} />
            </label>
            <div className="discovery-actions">
              <Button onClick={handleSavePlan}>{t("dpSavePlan")}</Button>
              <Button onClick={handleDeletePlan} disabled={!selectedPlanId}>{t("dpDeletePlan")}</Button>
              <Button onClick={handleResetFilters}>{t("dpReset")}</Button>
              <Button onClick={handleAddFilter}>{t("dpAddRule")}</Button>
            </div>
          </div>

          <div style={{ display: "grid", gap: 12, marginTop: 12 }}>
            {indicatorFilters.map((filter, index) => {
              const indicator = filter.indicator_key ? indicatorMap[filter.indicator_key] : undefined;
              const operatorOptions = indicatorOperators(indicator?.value_type);
              return (
                <div key={filter.id} className="discovery-control-bar" style={{ alignItems: "flex-end", marginTop: 0 }}>
                  <label className="inline-control" style={{ minWidth: 240 }}>
                    <span>{template("dpRuleN", { n: index + 1 })}</span>
                    <Select allowClear placeholder={t("dpSelectIndicator")} value={filter.indicator_key} onChange={(value) => handleIndicatorChange(filter.id, value)} options={customIndicators.map((item) => ({ label: item.name, value: item.key }))} />
                  </label>
                  <label className="inline-control">
                    <span>{t("dpOperator")}</span>
                    <Select value={filter.operator} onChange={(value) => updateFilter(filter.id, (current) => ({ ...current, operator: value }))} options={operatorOptions.map((item) => ({ label: item, value: item }))} disabled={!indicator} />
                  </label>
                  {indicator?.value_type === "number" ? (
                    <label className="inline-control">
                      <span>{t("dpThreshold")}</span>
                      <InputNumber value={filter.number_value} onChange={(value) => updateFilter(filter.id, (current) => ({ ...current, number_value: value ?? 0 }))} />
                    </label>
                  ) : (
                    <label className="inline-control">
                      <span>{t("dpTargetValue")}</span>
                      <Switch checked={filter.boolean_value} onChange={(value) => updateFilter(filter.id, (current) => ({ ...current, boolean_value: value }))} disabled={!indicator} />
                    </label>
                  )}
                  <Button danger onClick={() => handleRemoveFilter(filter.id)}>{t("dpDeleteRule")}</Button>
                  <div className="panel-meta" style={{ alignSelf: "center" }}>
                    {indicator ? `${indicator.name}${DOT}${indicator.value_type}` : t("dpPleaseSelectIndicator")}
                  </div>
                </div>
              );
            })}
          </div>

          {activeFilters.length > 0 && (
            <div className="panel-meta" style={{ marginTop: 12 }}>
              {indicatorLoading ? t("dpComputing") : template("dpEnabledIndicators", { count: requiredIndicatorKeys.length })}
            </div>
          )}

          <div className="table-wrap">
            <Table<WorkbenchCandidate>
              columns={columns}
              dataSource={filteredPool}
              rowKey="symbol_id"
              pagination={{ pageSize: 20, hideOnSinglePage: true }}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("noDiscoveryResults")} /> }}
              onRow={(record) => ({
                onClick: () => handleRowClick(record.symbol_id),
                onKeyDown: (e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    handleRowClick(record.symbol_id);
                  }
                },
                tabIndex: 0,
                role: "button",
                "aria-label": template("viewSymbolDetail", { symbol: record.symbol }),
                className: [
                  "discovery-table-row",
                  record.is_frozen ? "discovery-row-frozen" : "",
                  (() => {
                    const f = discoveryFreshness(record);
                    return f.className === "warning" ? "discovery-row-warning" : "";
                  })(),
                  ageDays(record.created_at) > 7 || !record.created_at ? "discovery-row-low-credibility" : "",
                ].filter(Boolean).join(" "),
              })}
            />
          </div>
        </div>
      </section>
    </div>
  );
}


