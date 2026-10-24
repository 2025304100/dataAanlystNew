import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  DatePicker,
  Modal,
  Progress,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  BankOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  FireOutlined,
  FundOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  RiseOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import { t, template } from "../i18n";
import { AppContext } from "../context/AppContext";
import {
  api,
  type ExternalDataOverview,
  type ExternalDatasetOverview,
  type ExternalDataSyncCapabilities,
  type ExternalDataSyncPlan,
  type ExternalDataSyncCoverage,
  type ExternalDataCoverage,
  type ExternalDatasetCoverage,
  type ExternalSyncDataset,
  type ExternalSyncPolicy,
  type ExternalSyncTask,
  type FactorPipelineTask,
} from "../api/client";

type SyncSource = "watchlist" | "positions" | "all";
type SyncWatchlist = { id: number; name: string; list_type: string };

const SYNC_POLICY_LABELS: Record<ExternalSyncPolicy, string> = {
  normal: "只补未更新的部分",
  missing: "只补缺失数据",
  force: "重新获取并更新已有数据",
};
const SYNC_POLICY_HINTS: Record<ExternalSyncPolicy, string> = {
  normal: "已完成的股票和日期会跳过，只更新还没有完成的部分；当天数据仍可继续更新。",
  missing: "只补所选范围内缺少的记录或数值，已有数据保留；休市日不算缺失。",
  force: "重新获取所选范围的数据，并更新已有记录。能获取的日期仍取决于数据来源。",
};

const FULL_HISTORY_START_DATE = "1990-01-01";
const MAX_HISTORY_SPAN_DAYS = 36500;
type HistoryDateRange = [Dayjs | null, Dayjs | null];
type HistoryPreset = `${number}d` | `${number}y` | "month_to_date" | "year_to_date" | "custom";
const HISTORY_DATE_PRESETS: { value: HistoryPreset; label: string; days?: number; years?: number }[] = [
  ...[7, 14, 30, 60, 90, 100, 180].map((days) => ({ value: `${days}d` as HistoryPreset, label: `${days} 日`, days })),
  ...[1, 2, 3, 5].map((years) => ({ value: `${years}y` as HistoryPreset, label: `${years} 年`, years })),
  { value: "month_to_date", label: "本月至今" },
  { value: "year_to_date", label: "今年至今" },
  { value: "custom", label: "自定义" },
];

function presetDateRange(preset: HistoryPreset, anchor: Dayjs): HistoryDateRange {
  const today = dayjs().startOf("day");
  if (preset === "month_to_date") return [today.startOf("month"), today];
  if (preset === "year_to_date") return [today.startOf("year"), today];
  const option = HISTORY_DATE_PRESETS.find((item) => item.value === preset);
  const end = anchor.startOf("day");
  if (option?.days) return [end.subtract(option.days - 1, "day"), end];
  if (option?.years) return [end.subtract(option.years, "year").add(1, "day"), end];
  return [null, null];
}

function dateRangeSpan(range: HistoryDateRange): number | null {
  if (!range[0]?.isValid() || !range[1]?.isValid()) return null;
  return range[1].startOf("day").diff(range[0].startOf("day"), "day") + 1;
}

function historyDateIssue(range: HistoryDateRange): string | null {
  const span = dateRangeSpan(range);
  if (span === null) return "请选择完整的开始日期和结束日期";
  if (span < 1) return "结束日期不能早于开始日期";
  if (range[1]!.isAfter(dayjs(), "day")) return "结束日期不能晚于今天";
  if (span > MAX_HISTORY_SPAN_DAYS) return `日期跨度不能超过 ${MAX_HISTORY_SPAN_DAYS} 日`;
  return null;
}

function matchingDatePreset(range: HistoryDateRange, preferred: HistoryPreset): HistoryPreset {
  if (historyDateIssue(range)) return "custom";
  const matches = (preset: HistoryPreset) => {
    const expected = presetDateRange(preset, range[1]!);
    return Boolean(expected[0]?.isSame(range[0], "day") && expected[1]?.isSame(range[1], "day"));
  };
  if (preferred !== "custom" && matches(preferred)) return preferred;
  return HISTORY_DATE_PRESETS.find((item) => item.value !== "custom" && matches(item.value))?.value ?? "custom";
}

type ExternalInputDraft = {
  source: SyncSource;
  includeNorthbound: boolean;
  syncMode: "incremental" | "backfill";
  syncPolicy: ExternalSyncPolicy;
  datePreset: HistoryPreset;
  dateRange: HistoryDateRange;
};

function controlledDraftKey(value: string | undefined): string | null {
  return value && /^[a-z0-9][a-z0-9:_-]{0,127}$/i.test(value) ? value : null;
}

function parseDraftDate(value: unknown): Dayjs | null | undefined {
  if (value === null) return null;
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return undefined;
  const parsed = dayjs(value);
  return parsed.isValid() && parsed.format("YYYY-MM-DD") === value ? parsed : undefined;
}

function readInputDraft(storageKey: string | undefined): ExternalInputDraft | null {
  const key = controlledDraftKey(storageKey);
  if (!key || typeof window === "undefined") return null;
  try {
    const serialized = window.sessionStorage.getItem(key);
    if (!serialized) return null;
    const value: unknown = JSON.parse(serialized);
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const draft = value as Record<string, unknown>;
    if (draft.version !== 1
      || typeof draft.source !== "string" || !["watchlist", "positions", "all"].includes(draft.source)
      || typeof draft.includeNorthbound !== "boolean"
      || typeof draft.syncMode !== "string" || !["incremental", "backfill"].includes(draft.syncMode)
      || !HISTORY_DATE_PRESETS.some((preset) => preset.value === draft.datePreset)
      || !Array.isArray(draft.dateRange) || draft.dateRange.length !== 2) return null;
    const start = parseDraftDate(draft.dateRange[0]);
    const end = parseDraftDate(draft.dateRange[1]);
    if (start === undefined || end === undefined) return null;
    const dateRange: HistoryDateRange = [start, end];
    const datePreset = draft.datePreset as HistoryPreset;
    return {
      source: draft.source as SyncSource,
      includeNorthbound: draft.includeNorthbound,
      syncMode: draft.syncMode as ExternalInputDraft["syncMode"],
      syncPolicy: ["normal", "missing", "force"].includes(String(draft.syncPolicy)) ? draft.syncPolicy as ExternalSyncPolicy : "normal",
      datePreset: start && end && datePreset !== "custom" ? matchingDatePreset(dateRange, datePreset) : datePreset,
      dateRange,
    };
  } catch {
    return null;
  }
}

const DATASET_ORDER: ExternalSyncDataset[] = [
  "fundamental",
  "financial",
  "lhb",
  "hot_rank",
  "capital_flow",
  "etf",
];

const TASK_DATASET_PREFIX = "external_sync_";
const isRunningTask = (task: { status: string }) => ["queued", "running"].includes(task.status);

function datasetFromTask(task: ExternalSyncTask): ExternalSyncDataset | null {
  if (task.result?.dataset) return DATASET_ORDER.includes(task.result.dataset) ? task.result.dataset : null;
  if (!task.task_type.startsWith(TASK_DATASET_PREFIX)) return null;
  const dataset = task.task_type.slice(TASK_DATASET_PREFIX.length) as ExternalSyncDataset;
  return DATASET_ORDER.includes(dataset) ? dataset : null;
}

export function formatDate(value: string | null, withTime = false): string {
  if (!value) return t("extNoData");
  // Backend audit timestamps are UTC even when the serialized value omits its
  // timezone. Calendar dates and explicitly zoned timestamps retain their form.
  const timestamp = withTime && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(value)
    ? `${value}Z` : value;
  const parsed = new Date(timestamp);
  if (Number.isNaN(parsed.getTime())) return value;
  return withTime ? parsed.toLocaleString() : parsed.toLocaleDateString();
}

function formatCount(value: number): string {
  return new Intl.NumberFormat().format(value);
}

function taskRange(task: ExternalSyncTask): string {
  try {
    const plan = (task.batch_recovery?.plan || task.result?.plan || {}) as Record<string, string | undefined>;
    return `${plan.requested_start_date || plan.start_date || "-"} ~ ${plan.requested_end_date || plan.end_date || "-"}`;
  } catch {
    return "- ~ -";
  }
}

function taskPolicyLabel(task: ExternalSyncTask): string {
  const policy = task.batch_recovery?.plan?.sync_policy ?? task.result?.plan?.sync_policy;
  return policy ? SYNC_POLICY_LABELS[policy] ?? "策略未记录" : "策略未记录";
}

function taskExecutionTimes(task: ExternalSyncTask): string {
  return `执行开始：${task.started_at ? formatDate(task.started_at, true) : "尚未开始"} · 执行结束：${task.finished_at ? formatDate(task.finished_at, true) : "尚未结束"}`;
}

function taskDatasetKey(task: ExternalSyncTask): string {
  return datasetFromTask(task) || task.task_type;
}

function keepLatestTaskPerDataset(tasks: ExternalSyncTask[]): ExternalSyncTask[] {
  const latest = new Map<string, ExternalSyncTask>();
  for (const task of tasks) {
    const key = taskDatasetKey(task);
    const previous = latest.get(key);
    // A stale dashboard response must not resurrect a task already observed
    // as terminal. Explicit retries are inserted after removing the old entry.
    if (previous?.id === task.id && !isRunningTask(previous) && isRunningTask(task)) continue;
    const taskTime = Date.parse(task.updated_at || task.created_at || "") || 0;
    const previousTime = previous ? Date.parse(previous.updated_at || previous.created_at || "") || 0 : -1;
    if (!previous || taskTime >= previousTime || (["queued", "running"].includes(task.status) && !["queued", "running"].includes(previous.status))) {
      latest.set(key, task);
    }
  }
  return [...latest.values()];
}

function syncResultParams(result: ExternalSyncTask["result"]): Record<string, string | number> {
  if (!result) return {};
  return {
    total: result.total,
    success: result.success,
    skipped: result.skipped,
    failed: result.failed,
  };
}

function taskHasWarnings(task: ExternalSyncTask): boolean {
  return task.failed_count > 0 || Boolean(task.result?.errors?.length) || Boolean(task.result?.warnings?.length) || task.result?.warehouse_mirror?.status === "warning" || task.result?.northbound?.status === "warning";
}

function taskCanResume(task: ExternalSyncTask): boolean {
  return ["failed", "cancelled"].includes(task.status) || (task.status === "done" && (task.failed_count > 0 || task.result?.warehouse_mirror?.status === "warning"));
}

function taskAllAttemptsFailed(task: ExternalSyncTask): boolean {
  return task.status === "done" && task.failed_count > 0 && task.ok_count === 0 && !(task.result?.success);
}

function taskProgressText(task: ExternalSyncTask): string {
  return task.total > 0 ? `${task.processed ?? 0}/${task.total}` : `已处理 ${task.processed ?? 0} · 总数未知`;
}

function statusTag(task: ExternalSyncTask | null) {
  if (!task) return <Tag>{t("extStatusNever")}</Tag>;
  if (task.cancel_requested && isRunningTask(task)) return <Tag color="warning">正在停止</Tag>;
  if (taskAllAttemptsFailed(task)) return <Tag color="error">{t("extStatusFailed")}</Tag>;
  const interrupted = task.status === "failed" && task.stage === "interrupted";
  const config = {
    queued: { color: "default", key: "extStatusQueued" },
    running: { color: "processing", key: "extStatusRunning" },
    done: { color: taskHasWarnings(task) ? "warning" : "success", key: taskHasWarnings(task) ? "extStatusPartial" : "extStatusDone" },
    failed: { color: interrupted ? "warning" : "error", key: interrupted ? "extStatusInterrupted" : "extStatusFailed" },
    cancelled: { color: "default", key: "extStatusCancelled" },
    interrupted: { color: "warning", key: "extStatusInterrupted" },
    stalled: { color: "warning", key: "extStatusStalled" },
    paused: { color: "warning", key: "extStatusStalled" },
  }[task.status as string] || { color: "default", key: "extStatusUnknown" };
  return <Tag color={config.color}>{t(config.key)}</Tag>;
}

function stageLabel(stage: string): string {
  const key = {
    queued: "extStageQueued",
    prepare: "extStagePrepare",
    northbound: "extStageNorthbound",
    fetch: "extStageFetch",
    sync: "extStageSync",
    waiting_market: "行情优先，等待中",
    mirror: "extStageSync",
    done: "extStageDone",
    failed: "extStageFailed",
    cancelled: "extStageCancelled",
  }[stage];
  return key ? t(key) : stage;
}

function readinessLabel(readiness: ExternalDataCoverage["datasets"][number]["readiness"]): string {
  const key = {
    available: "extReadinessAvailable",
    limited: "extReadinessLimited",
    event: "extReadinessEvent",
    snapshot: "extReadinessSnapshot",
    blocked: "extReadinessBlocked",
    unknown: "extReadinessUnknown",
    not_applicable: "extReadinessNotApplicable",
  }[readiness];
  return t(key);
}

function coverageReasonLabel(reason: string | null | undefined, field?: string): string {
  if (!reason) return "";
  if (reason === "field_data_unavailable" && field) {
    return template("extCoverageReasonFieldDataUnavailableDetail", { field });
  }
  const key = {
    factor_warehouse_unavailable: "extCoverageReasonFactorWarehouseUnavailable",
    no_factor_field_mapping: "extCoverageReasonNoFactorFieldMapping",
    all_fields_evaluation_ready: "extCoverageReasonAllFieldsEvaluationReady",
    event_only_data: "extCoverageReasonEventOnlyData",
    snapshot_only_data: "extCoverageReasonSnapshotOnlyData",
    coverage_or_pit_limited: "extCoverageReasonCoverageOrPitLimited",
  }[reason];
  if (key) return t(key);
  return t("extCoverageReasonUnknown");
}

function datasetCoverageReason(diagnostic: ExternalDatasetCoverage): string {
  const affected = diagnostic.fields.filter((field) => ["blocked", "unknown"].includes(field.availability));
  if (affected.length) return affected.map((field) => {
    const reason = field.reason || (field.availability === "unknown" ? "数据状态尚未确认。" : field.nonnull_rows > 0 ? "当前受评价规则限制。" : "暂无有效记录。");
    return `${field.field}：${reason}`;
  }).join("；");
  return coverageReasonLabel(diagnostic.reason);
}

function syncScopeDescription(source: SyncSource, watchlistName?: string | null, portfolioId?: number | null): string {
  if (source === "watchlist") return `同步范围：${watchlistName || "主观察池"}中的有效观察项（已排除归档和失效项）`;
  if (source === "positions") return portfolioId ? `持仓范围：当前组合（${portfolioId}）中数量大于 0 的标的` : "持仓范围：所有组合中数量大于 0 的标的";
  return "全市场范围：已同步且处于启用状态的标的";
}

function syncScopeSummary(source: SyncSource, watchlistName?: string | null, portfolioId?: number | null): string {
  if (source === "watchlist") return `${watchlistName || "主观察池"} · 有效观察项`;
  if (source === "positions") return portfolioId ? `组合 #${portfolioId} · 持仓` : "持仓标的";
  return "全市场已同步标的";
}

function scoringOutcome(task: FactorPipelineTask): { label: string; type: "success" | "warning" | "error" | "info"; detail?: string } {
  if (task.status === "cancelled") return { label: "已取消", type: "info" };
  if (task.status === "failed") return { label: "失败", type: "error" };
  if (!["done", "completed"].includes(task.status)) return { label: "处理中", type: "info" };
  const bridge = task.result?.scoring_bridge as { status?: string; materialized_count?: number; reason?: string } | undefined;
  const completion = task.result?.completion_status;
  if (completion === "TRAINING_REJECTED") return { label: "模型未通过验证", type: "warning", detail: "候选模型未通过数据或模型门槛，未生成可用模型评分。" };
  if (completion === "AWAITING_MODEL_ACTIVATION" || bridge?.status === "SKIPPED") {
    return { label: "数据已算好，还需启用评分模型", type: "warning", detail: "未生成模型评分。请到因子模型查看真实训练结果，启用有效模型后再刷新评分。" };
  }
  if (completion === "PARTIAL_SCORE_PUBLICATION" || bridge?.status === "PARTIAL") {
    if (typeof bridge?.materialized_count !== "number") {
      return { label: "评分生成受限，数量待核实", type: "warning", detail: "任务未返回评分写入数量，请检查任务结果中的输入和标的映射。" };
    }
    if (typeof bridge?.materialized_count === "number" && bridge.materialized_count <= 0) {
      return { label: "输入不足，未生成评分", type: "warning", detail: "本次未写入模型评分，请检查缺少的输入和标的映射。" };
    }
    return { label: "评分部分生成", type: "warning", detail: `已生成 ${bridge?.materialized_count ?? 0} 条评分；部分标的缺少输入或映射，请检查任务结果。` };
  }
  if (typeof bridge?.materialized_count === "number") {
    return bridge.materialized_count > 0
      ? { label: `已生成评分（${formatCount(bridge.materialized_count)} 条）`, type: "success" }
      : { label: "计算完成，未生成评分", type: "warning", detail: "本次任务没有写入模型评分，请检查模型和数据覆盖。" };
  }
  return { label: "计算完成，评分结果待核实", type: "warning", detail: "任务未返回评分写入证据，请到任务与批次查看详情。" };
}

function scoringStageLabel(stage: string): string {
  const labels: Record<string, string> = {
    queued: "等待执行", initializing: "准备输入", mirror: "写入因子仓库",
    factors: "计算因子输入", targets: "生成收益目标", train: "训练模型",
    score: "生成模型评分", materialize: "生成模型评分", done: "任务结束",
    failed: "任务失败", cancelled: "任务取消",
  };
  return labels[stage] || stage;
}

function scoringTaskUniverseDescription(task: FactorPipelineTask): string | null {
  const resultScope = task.result?.score_universe;
  let payload: Record<string, unknown> = {};
  if (typeof task.payload_json === "string") {
    try {
      const parsed: unknown = JSON.parse(task.payload_json);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) payload = parsed as Record<string, unknown>;
    } catch { /* Legacy tasks may not expose their submission metadata. */ }
  } else if (task.payload_json && typeof task.payload_json === "object") {
    payload = task.payload_json;
  }
  const universe = resultScope && typeof resultScope === "object"
    ? resultScope as Record<string, unknown>
    : { source: payload.universe_source, watchlist_id: payload.watchlist_id, portfolio_id: payload.portfolio_id, symbol_count: Array.isArray(payload.score_symbols) ? payload.score_symbols.length : null };
  const sourceLabel = universe.source === "watchlist" && typeof universe.watchlist_id === "number"
    ? `观察池 #${universe.watchlist_id}`
    : universe.source === "positions" && typeof universe.portfolio_id === "number"
      ? `组合 #${universe.portfolio_id} 持仓`
      : universe.source === "all" ? "全量已同步标的" : null;
  if (!sourceLabel) return null;
  const count = typeof universe.symbol_count === "number" ? ` · ${formatCount(universe.symbol_count)} 只股票` : " · 股票数待核实";
  return `本次评分范围：${sourceLabel}${count}；使用全市场数据作为计算参照`;
}

export type ExternalDataSyncProps = {
  showScoringRefresh?: boolean;
  /** A stable workflow-owned key enables tab-scoped form drafts only. */
  draftStorageKey?: string;
};

export default function ExternalDataSync({ showScoringRefresh = true, draftStorageKey }: ExternalDataSyncProps) {
  // The data-center can be embedded independently in tests and operational
  // tools. AppContext enriches the watchlist scope when present, but the
  // synchronization surface must remain usable without the global workbench.
  const app = useContext(AppContext);
  const [initialInputDraft] = useState(() => readInputDraft(draftStorageKey));
  const [source, setSource] = useState<SyncSource>(initialInputDraft?.source ?? "watchlist");
  const [includeNorthbound, setIncludeNorthbound] = useState(initialInputDraft?.includeNorthbound ?? true);
  const [syncMode, setSyncMode] = useState<"incremental" | "backfill">(initialInputDraft?.syncMode ?? "incremental");
  const [syncPolicy, setSyncPolicy] = useState<ExternalSyncPolicy>(initialInputDraft?.syncPolicy ?? "normal");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [coverageDetailOpen, setCoverageDetailOpen] = useState(false);
  const [syncErrorDetailOpen, setSyncErrorDetailOpen] = useState(false);
  const [datePreset, setDatePreset] = useState<HistoryPreset>(initialInputDraft?.datePreset ?? "60d");
  const [dateRange, setDateRange] = useState<HistoryDateRange>(initialInputDraft?.dateRange ?? [null, null]);
  const [capabilities, setCapabilities] = useState<ExternalDataSyncCapabilities | null>(null);
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(true);
  const [capabilitiesError, setCapabilitiesError] = useState<string | null>(null);
  const [coverage, setCoverage] = useState<ExternalDataCoverage | null>(null);
  const [coverageLoading, setCoverageLoading] = useState(true);
  const [coverageError, setCoverageError] = useState<string | null>(null);
  const [syncCoverage, setSyncCoverage] = useState<ExternalDataSyncCoverage | null>(null);
  const [syncCoverageError, setSyncCoverageError] = useState<string | null>(null);
  const [previewPlan, setPreviewPlan] = useState<ExternalDataSyncPlan | null>(null);
  const [overview, setOverview] = useState<ExternalDataOverview | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [startingDataset, setStartingDataset] = useState<ExternalSyncDataset | null>(null);
  const [syncError, setSyncError] = useState<{ dataset: ExternalSyncDataset; detail: string; range: string; retry: () => void } | null>(null);
  const [activeTask, setActiveTask] = useState<ExternalSyncTask | null>(null);
  const [activeTasks, setActiveTasks] = useState<ExternalSyncTask[]>([]);
  const [finishedTask, setFinishedTask] = useState<ExternalSyncTask | null>(null);
  const [taskActionLoading, setTaskActionLoading] = useState<"cancel" | "retry" | null>(null);
  const [repairingDataset, setRepairingDataset] = useState<"fundamental" | "financial" | "capital_flow" | null>(null);
  const [pipelineStarting, setPipelineStarting] = useState(false);
  const [pipelineTask, setPipelineTask] = useState<FactorPipelineTask | null>(null);
  const [pipelinePollError, setPipelinePollError] = useState<string | null>(null);
  const [standaloneWatchlists, setStandaloneWatchlists] = useState<SyncWatchlist[] | null>(null);
  const [watchlistsLoading, setWatchlistsLoading] = useState(false);
  const [watchlistsError, setWatchlistsError] = useState<string | null>(null);
  const watchlistsRequest = useRef(0);
  const handledTaskIds = useRef(new Set<string>());
  const taskPollRequests = useRef(new Map<string, Promise<ExternalSyncTask>>());
  const syncCoverageRequest = useRef<Promise<void> | null>(null);
  const historySpan = dateRangeSpan(dateRange);
  const historyRangeIssue = syncMode === "backfill" ? historyDateIssue(dateRange) : null;

  useEffect(() => {
    const key = controlledDraftKey(draftStorageKey);
    if (!key) return;
    try {
      window.sessionStorage.setItem(key, JSON.stringify({
        version: 1,
        source,
        includeNorthbound,
        syncMode,
        syncPolicy,
        datePreset,
        dateRange: dateRange.map((date) => date?.format("YYYY-MM-DD") ?? null),
      }));
    } catch { /* Input controls remain usable when browser storage is unavailable. */ }
  }, [draftStorageKey, source, includeNorthbound, syncMode, syncPolicy, datePreset, dateRange]);

  const changeSyncMode = (mode: "incremental" | "backfill") => {
    setSyncMode(mode);
    setPreviewPlan(null);
    if (mode === "backfill" && dateRangeSpan(dateRange) === null) {
      setDatePreset("60d");
      setDateRange(presetDateRange("60d", dateRange[1] ?? dayjs()));
    }
  };
  const changeDatePreset = (preset: HistoryPreset) => {
    setDatePreset(preset);
    setPreviewPlan(null);
    if (preset !== "custom") setDateRange(presetDateRange(preset, dateRange[1] ?? dayjs()));
  };
  const changeDateRange = (value: HistoryDateRange | null) => {
    const next = value ?? [null, null];
    setDateRange(next);
    setDatePreset(matchingDatePreset(next, datePreset));
    setPreviewPlan(null);
  };

  const contextWatchlists = app?.workbench?.watchlists;
  const contextWatchlistsAvailable = Array.isArray(contextWatchlists);
  const syncWatchlists = useMemo(
    () => contextWatchlistsAvailable ? contextWatchlists! : standaloneWatchlists ?? [],
    [contextWatchlistsAvailable, contextWatchlists, standaloneWatchlists],
  );
  const refreshStandaloneWatchlists = useCallback(async () => {
    if (contextWatchlistsAvailable) return;
    const requestId = ++watchlistsRequest.current;
    setWatchlistsLoading(true);
    setWatchlistsError(null);
    try {
      const lists = await api.getWatchlists();
      if (!Array.isArray(lists)) throw new Error("观察池列表返回无效");
      if (requestId === watchlistsRequest.current) setStandaloneWatchlists(lists);
    } catch (error: any) {
      if (requestId === watchlistsRequest.current) {
        setStandaloneWatchlists(null);
        setWatchlistsError(error?.message || String(error));
      }
    } finally {
      if (requestId === watchlistsRequest.current) setWatchlistsLoading(false);
    }
  }, [contextWatchlistsAvailable]);
  useEffect(() => {
    if (contextWatchlistsAvailable) {
      watchlistsRequest.current += 1;
      setWatchlistsLoading(false);
      setWatchlistsError(null);
      return;
    }
    void refreshStandaloneWatchlists();
    return () => { watchlistsRequest.current += 1; };
  }, [contextWatchlistsAvailable, refreshStandaloneWatchlists]);

  const primaryWatchlistId = useMemo(() => {
    const lists = syncWatchlists;
    if (app?.activeWatchlistId && lists.some((item) => item.id === app.activeWatchlistId)) {
      return app.activeWatchlistId;
    }
    return lists.find((item) => item.list_type === "watch")?.id ?? lists[0]?.id ?? null;
  }, [app?.activeWatchlistId, syncWatchlists]);
  const primaryWatchlistName = useMemo(() => {
    const lists = syncWatchlists;
    return lists.find((item) => item.id === primaryWatchlistId)?.name ?? null;
  }, [syncWatchlists, primaryWatchlistId]);
  const scoringScopeIssue = source === "watchlist" && !primaryWatchlistId
    ? !contextWatchlistsAvailable && watchlistsError
      ? `观察池列表读取失败，无法刷新评分：${watchlistsError}；请重试读取`
      : !contextWatchlistsAvailable && (watchlistsLoading || standaloneWatchlists === null)
        ? "观察池列表正在加载，暂不能刷新评分"
        : "当前观察池尚未加载或不存在，无法刷新评分；请先选择有效观察池"
    : source === "positions" && !app?.portfolioId
      ? "当前组合尚未加载或未选择，无法刷新持仓评分；请先选择组合"
      : null;

  const configs = {
      fundamental: { title: t("extSyncFundamental"), desc: t("extSyncFundamentalDesc"), icon: <FundOutlined /> },
      financial: { title: t("extSyncFinancial"), desc: t("extSyncFinancialDesc"), icon: <FileTextOutlined /> },
      lhb: { title: t("extSyncLhb"), desc: t("extSyncLhbDesc"), icon: <BankOutlined /> },
      hot_rank: { title: t("extSyncHotRank"), desc: t("extSyncHotRankDesc"), icon: <FireOutlined /> },
      tail_proxy: { title: "已停用", desc: "", icon: null },
      capital_flow: { title: t("extSyncCapitalFlow"), desc: t("extSyncCapitalFlowDesc"), icon: <RiseOutlined /> },
      etf: { title: t("extSyncEtf"), desc: t("extSyncEtfDesc"), icon: <DatabaseOutlined /> },
  };

  const refreshCoverage = useCallback(async () => {
    setCoverageLoading(true);
    try {
      setCoverage(await api.getExternalDataCoverage());
      setCoverageError(null);
    } catch (error: any) {
      setCoverageError(error?.message || String(error));
    } finally {
      setCoverageLoading(false);
    }
  }, []);

  const refreshCapabilities = useCallback(async () => {
    setCapabilitiesLoading(true);
    try {
      setCapabilities(await api.getExternalDataSyncCapabilities());
      setCapabilitiesError(null);
    } catch (error: any) {
      setCapabilitiesError(error?.message || String(error));
    } finally {
      setCapabilitiesLoading(false);
    }
  }, []);

  const refreshSyncCoverage = useCallback(() => {
    if (syncCoverageRequest.current) return syncCoverageRequest.current;
    const request = (async () => {
      try {
        setSyncCoverage(await api.getExternalDataSyncCoverage());
        setSyncCoverageError(null);
      } catch (error: any) {
        setSyncCoverageError(error?.message || String(error));
      }
    })().finally(() => { syncCoverageRequest.current = null; });
    syncCoverageRequest.current = request;
    return request;
  }, []);

  const refreshOverview = useCallback(async (silent = false) => {
    if (!silent) setOverviewLoading(true);
    try {
      const next = await api.getExternalDataOverview();
      setOverview(next);
      setOverviewError(null);
      void refreshCoverage();
      void refreshSyncCoverage();
      const running = next.datasets
        .map((item) => item.latest_task)
        .filter((task): task is ExternalSyncTask => Boolean(task && (task.status === "queued" || task.status === "running")));
      if (running.length) {
        setActiveTasks((current) => keepLatestTaskPerDataset([...current, ...running]));
        setActiveTask(running[running.length - 1]);
      }
    } catch (error: any) {
      const errorMessage = error?.message || String(error);
      setOverviewError(errorMessage);
      if (!silent) message.error(template("extOverviewFailed", { message: errorMessage }));
    } finally {
      if (!silent) setOverviewLoading(false);
    }
  }, [refreshCoverage, refreshSyncCoverage]);

  const finishTask = useCallback(async (task: ExternalSyncTask, refresh = true) => {
    if (isRunningTask(task) || handledTaskIds.current.has(task.id)) return false;
    handledTaskIds.current.add(task.id);
    setFinishedTask(task);
    if (taskAllAttemptsFailed(task)) {
      message.error("同步失败：全部尝试失败");
    } else if (task.status === "done" && !taskHasWarnings(task)) {
      message.success(task.result?.skip_reason ? task.message : t("extTaskCompleted"));
    } else if (task.status === "done") {
      message.warning(task.result?.dataset === "etf" && task.result?.warnings?.length ? task.message : t("extTaskCompletedWithErrors"));
    } else if (task.status === "cancelled") {
      message.info("同步已取消，已完成的数据保留，可继续上次更新");
    } else {
      message.error(task.message || t("extStatusFailed"));
    }
    if (task.result?.skip_reason && task.result.records === 0) {
      void refreshSyncCoverage();
      const dataset = datasetFromTask(task);
      setOverview((current) => current ? {
        ...current, datasets: current.datasets.map((item) => item.dataset === dataset
          ? { ...item, latest_task: task } : item),
      } : current);
      return false;
    }
    if (refresh) await refreshOverview(true);
    return true;
  }, [refreshOverview, refreshSyncCoverage]);

  const readPolledTask = useCallback((taskId: string) => {
    const pending = taskPollRequests.current.get(taskId);
    if (pending) return pending;
    const request = api.getExternalDataSyncTask(taskId).finally(() => {
      taskPollRequests.current.delete(taskId);
    });
    taskPollRequests.current.set(taskId, request);
    return request;
  }, []);

  useEffect(() => {
    void refreshOverview();
  }, [refreshOverview]);

  useEffect(() => {
    void refreshCapabilities();
  }, [refreshCapabilities]);

  useEffect(() => {
    const runningTasks = activeTasks.filter((task) => ["queued", "running"].includes(task.status));
    if (!runningTasks.length) return;
    let disposed = false;
    let polling = false;
    const poll = async () => {
      if (disposed || polling) return;
      polling = true;
      try {
        const results = await Promise.allSettled(runningTasks.map((task) => readPolledTask(task.id)));
        if (disposed) return;
        const nextTasks = results.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
        const failures = results.filter((result): result is PromiseRejectedResult => result.status === "rejected");
        if (failures.length) message.error(template("extSyncFailed", { message: failures[0].reason?.message || String(failures[0].reason) }));
        setActiveTasks((current) => keepLatestTaskPerDataset(current.map((task) => {
          const next = nextTasks.find((item) => item.id === task.id);
          if (!next || (!isRunningTask(task) && isRunningTask(next))) return task;
          return { ...next, cancel_requested: isRunningTask(next) && Boolean(next.cancel_requested || task.cancel_requested) };
        })));
        setActiveTask((current) => {
          const next = nextTasks.find((task) => task.id === current?.id);
          return current && next && (isRunningTask(current) || !isRunningTask(next)) ? next : current;
        });
        const completed = nextTasks.filter((next) => !isRunningTask(next));
        const refreshNeeded = await Promise.all(completed.map((next) => finishTask(next, false)));
        if (refreshNeeded.some(Boolean)) await refreshOverview(true);
      } catch (error: any) {
        if (!disposed) message.error(template("extSyncFailed", { message: error?.message || String(error) }));
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = window.setInterval(poll, 1000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [activeTasks.map((task) => `${task.id}:${task.status}`).join(","), finishTask, readPolledTask, refreshOverview]);

  const runSync = async (
    dataset: ExternalSyncDataset,
    overrides: Partial<{
      source: SyncSource;
      mode: "incremental" | "backfill";
      sync_policy: ExternalSyncPolicy;
      start_date: string;
      end_date: string;
    }> = {},
    options: { confirmed?: boolean } = {},
  ) => {
    // Inventory is advisory for starting a sync. During the initial request it
    // may still be null, but the selected scope and date parameters are valid.
    // Keep the hard stop for a known dashboard failure so an unavailable
    // inventory cannot accidentally trigger a task.
    if (dashboardUnavailable) return;
    const effectiveSource = overrides.source ?? source;
    const effectiveMode = overrides.mode ?? syncMode;
    const datasetCapability = capabilities?.[dataset];
    if (effectiveMode === "backfill" && (!capabilities || capabilitiesLoading || capabilitiesError || !datasetCapability)) {
      message.warning("历史同步能力尚未确认，请重试读取同步能力");
      return;
    }
    const snapshotFallback = effectiveMode === "backfill" && Boolean(datasetCapability) && !datasetCapability!.modes.includes("backfill");
    const confirmedOverrides = snapshotFallback
      ? { ...overrides, mode: "incremental" as const, start_date: undefined, end_date: undefined }
      : overrides;
    if (!options.confirmed) {
      const actualMode = confirmedOverrides.mode ?? syncMode;
      const selectedRange = `${overrides.start_date ?? dateRange[0]?.format("YYYY-MM-DD") ?? "未选开始日期"} 至 ${overrides.end_date ?? dateRange[1]?.format("YYYY-MM-DD") ?? "未选结束日期"}`;
      const rangeStartDay = overrides.start_date ? dayjs(overrides.start_date) : dateRange[0];
      const rangeEndDay = overrides.end_date ? dayjs(overrides.end_date) : dateRange[1];
      const plannedSpan = actualMode === "backfill" && rangeStartDay?.isValid() && rangeEndDay?.isValid()
        ? rangeEndDay.startOf("day").diff(rangeStartDay.startOf("day"), "day") + 1
        : null;
      const chunkDays = datasetCapability?.chunk_days ?? null;
      const chunkCount = chunkDays && chunkDays > 0 && plannedSpan && plannedSpan > chunkDays
        ? Math.ceil(plannedSpan / chunkDays)
        : 0;
      const partialHistory = Boolean(datasetCapability?.partial_history_fields?.length);
      const snapshotReason = dataset === "etf"
        ? "ETF 指标的溢折价、规模和份额只能可靠获取最新记录。"
        : dataset === "hot_rank"
          ? "人气榜接口只提供当前榜单快照。"
          : "该数据源只提供最近快照。";
      const snapshotTitle = dataset === "etf"
        ? "ETF 指标只同步最近数据"
        : dataset === "hot_rank"
          ? "今日人气榜只同步最近数据"
          : "该数据只同步最近数据";
      Modal.confirm({
        title: snapshotFallback ? snapshotTitle : `确认同步：${configs[dataset].title.replace(/^同步\s*/, "")}`,
        content: (
          <div style={{ lineHeight: 1.7 }}>
            <div>{syncScopeDescription(effectiveSource, primaryWatchlistName, app?.portfolioId)}</div>
            <div>实际更新范围：{actualMode === "backfill" ? selectedRange : "最近数据（今天）"}</div>
            <div>更新方式：{SYNC_POLICY_LABELS[confirmedOverrides.sync_policy ?? syncPolicy]}</div>
            {actualMode === "backfill" && chunkCount > 0 && (
              <div>分段执行：本次范围共 {plannedSpan} 天，将按每段不超过 {chunkDays} 天自动拆分，共 {chunkCount} 段。</div>
            )}
            {actualMode === "backfill" && partialHistory && (
              <div>历史回填仅补净值、收盘价、溢折价和评分；份额、规模、跟踪误差只有当天快照。</div>
            )}
            {snapshotFallback && <div style={{ marginTop: 8 }}>{snapshotReason}所选历史日期（{selectedRange}）不会生效，继续后将按最近数据同步今天的最新快照。</div>}
          </div>
        ),
        okText: snapshotFallback ? "继续同步最近数据" : "开始同步",
        cancelText: "取消",
        onOk: () => runSync(dataset, confirmedOverrides, { confirmed: true }),
      });
      return;
    }
    setStartingDataset(dataset);
    setSyncError(null);
    setFinishedTask(null);
    try {
      const effectiveStartDate = overrides.start_date
        ?? (dateRange[0] ? dateRange[0].format("YYYY-MM-DD") : undefined);
      const effectiveEndDate = overrides.end_date
        ?? (dateRange[1] ? dateRange[1].format("YYYY-MM-DD") : undefined);
      const requestedRange: HistoryDateRange = [
        effectiveStartDate ? dayjs(effectiveStartDate) : null,
        effectiveEndDate ? dayjs(effectiveEndDate) : null,
      ];
      if (effectiveMode === "backfill") {
        const issue = historyDateIssue(requestedRange);
        if (issue) { message.warning(issue); return; }
      }
      const syncPayload = {
        ...overrides,
        dataset,
        source: effectiveSource,
        watchlist_id: effectiveSource === "watchlist" ? primaryWatchlistId : undefined,
        portfolio_id: effectiveSource === "positions" ? app?.portfolioId ?? undefined : undefined,
        include_northbound: includeNorthbound && northboundReady,
        mode: effectiveMode,
        sync_policy: overrides.sync_policy ?? syncPolicy,
        start_date: effectiveMode === "backfill" ? effectiveStartDate : undefined,
        end_date: effectiveMode === "backfill" ? effectiveEndDate : undefined,
        lookback_days: effectiveMode === "backfill" ? dateRangeSpan(requestedRange)! : 1,
        limit: 20,
        ...(effectiveMode === "backfill" && dataset === "fundamental" ? { max_workers: 4 } : {}),
      };
      const plan = await api.previewExternalDataSyncPlan(syncPayload);
      setPreviewPlan(plan);
      const task = await api.startExternalDataSync(syncPayload);
      handledTaskIds.current.delete(task.id);
      setActiveTask(task);
      setActiveTasks((current) => keepLatestTaskPerDataset([...current, task]));
      if (["queued", "running"].includes(task.status)) {
        await refreshOverview(true);
      } else {
        await finishTask(task);
      }
    } catch (error: any) {
      setSyncError({
        dataset,
        detail: error?.message || String(error),
        range: `${configs[dataset].title} · ${syncScopeDescription(effectiveSource, primaryWatchlistName, app?.portfolioId)} · ${effectiveMode === "backfill" ? `${overrides.start_date ?? dateRange[0]?.format("YYYY-MM-DD") ?? "未选开始日期"} 至 ${overrides.end_date ?? dateRange[1]?.format("YYYY-MM-DD") ?? "未选结束日期"}` : "最近数据"} · ${SYNC_POLICY_LABELS[overrides.sync_policy ?? syncPolicy]}`,
        // Keep this attempt's captured form values. A later draft must not
        // silently change the source or dates when the failed attempt is retried.
        retry: () => void runSync(dataset, overrides, { confirmed: true }),
      });
      message.error(template("extSyncFailed", { message: error?.message || String(error) }));
    } finally {
      setStartingDataset(null);
    }
  };

  const cancelActiveTask = async (task: ExternalSyncTask) => {
    setTaskActionLoading("cancel");
    try {
      const cancelled = await api.cancelExternalDataSyncTask(task.id);
      const next = isRunningTask(cancelled) ? { ...cancelled, cancel_requested: true } : cancelled;
      setActiveTasks((current) => keepLatestTaskPerDataset(current.map((item) => item.id === next.id ? next : item)));
      setActiveTask(next);
      if (isRunningTask(next)) message.info("已请求取消，等待当前步骤结束");
      else await finishTask(next);
    } catch (error: any) {
      message.error(template("extSyncFailed", { message: error?.message || String(error) }));
    } finally {
      setTaskActionLoading(null);
    }
  };

  const resumeTaskFromCursor = async (task: ExternalSyncTask) => {
    setTaskActionLoading("retry");
    try {
      const resumedTask = await api.retryExternalDataSyncTask(task.id);
      handledTaskIds.current.delete(resumedTask.id);
      setFinishedTask(null);
      setActiveTask(resumedTask);
      setActiveTasks((current) => keepLatestTaskPerDataset([...current.filter((item) => item.id !== resumedTask.id), resumedTask]));
      await refreshOverview(true);
    } catch (error: any) {
      message.error(template("extSyncFailed", { message: error?.message || String(error) }));
    } finally {
      setTaskActionLoading(null);
    }
  };

  const retryTaskFromCursor = async () => {
    if (!activeTask) return;
    await resumeTaskFromCursor(activeTask);
  };

  const refreshFactorScores = async () => { /* P2-4: UX 语义别名 — 显示文案统一使用「评分/评分流水线」而不是「因子」 */
    if (!showScoringRefresh || pipelineStarting) return;
    if (historyRangeIssue) { message.warning(historyRangeIssue); return; }
    if (scoringScopeIssue) { message.warning(scoringScopeIssue); return; }
    setPipelineStarting(true);
    try {
      const start = syncMode === "backfill" && dateRange[0]
        ? dateRange[0].format("YYYY-MM-DD")
        : undefined;
      const end = syncMode === "backfill" && dateRange[1]
        ? dateRange[1].format("YYYY-MM-DD")
        : undefined;
      const task = await api.scoringCreateTask({
        start_date: start,
        end_date: end,
        full_refresh: syncMode === "backfill",
        train_model: false,
        materialize_scores: true,
        universe_source: source,
        ...(source === "watchlist" ? { watchlist_id: primaryWatchlistId! } : {}),
        ...(source === "positions" ? { portfolio_id: app!.portfolioId! } : {}),
      });
      setPipelineTask(task);
      setPipelinePollError(null);
      const taskId = typeof task === "object" && task ? (task as any).id ?? "-" : "-";
      message.success(`评分流水线任务已提交，任务号：${String(taskId)}`);
    } catch (error: any) {
      message.error(error?.message || "评分刷新启动失败，请稍后重试");
    } finally {
      setPipelineStarting(false);
    }
  };

  useEffect(() => {
    if (!showScoringRefresh || !pipelineTask || ["done", "completed", "failed", "cancelled"].includes(pipelineTask.status)) return;
    let disposed = false;
    const poll = async () => {
      try {
        const next = await api.scoringGetTask(pipelineTask.id);
        if (!disposed) { setPipelineTask(next); setPipelinePollError(null); }
      } catch (error: any) {
        if (!disposed) setPipelinePollError(error?.message || String(error));
      }
    };
    void poll();
    const timer = window.setInterval(poll, 1500);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [showScoringRefresh, pipelineTask?.id, pipelineTask?.status]);

  const repairDetectedGaps = async (dataset: "fundamental" | "financial" | "capital_flow") => {
    if (!overview) return;
    if (historyRangeIssue) { message.warning(historyRangeIssue); return; }
    setRepairingDataset(dataset);
    setFinishedTask(null);
    // 缺口修复不应受“最近快照”显示模式影响。未选择明确日期时，
    // 使用该数据集当前已知的真实覆盖范围，避免默认退回最近 60 天。
    const coverageFields = coverage?.datasets.find((item) => item.dataset === dataset)?.fields ?? [];
    const knownStarts = coverageFields
      .map((field) => field.first_date)
      .filter((value): value is string => Boolean(value));
    const knownEnds = coverageFields
      .map((field) => field.latest_date)
      .filter((value): value is string => Boolean(value));
    const end = (syncMode === "backfill" ? dateRange[1] : null)
      || (knownEnds.length ? dayjs(knownEnds.sort()[knownEnds.length - 1]) : dayjs());
    const start = (syncMode === "backfill" ? dateRange[0] : null)
      || (knownStarts.length ? dayjs(knownStarts.sort()[0]) : end.subtract(59, "day"));
    try {
      const report = await api.getExternalDataGaps(
        dataset,
        start.format("YYYY-MM-DD"),
        end.format("YYYY-MM-DD"),
      );
      if (report.gaps.length === 0) {
        message.info("所选范围没有可修复的缺口");
        return;
      }
      const task = await api.repairExternalDataGaps({
        dataset,
        start_date: report.start_date,
        end_date: report.end_date,
        gaps: report.gaps,
        sync_policy: "missing",
      });
      setActiveTask(task);
      handledTaskIds.current.delete(task.id);
      setActiveTasks((current) => keepLatestTaskPerDataset([...current, task]));
      message.success(`已创建 ${report.gaps.length}${report.truncated ? "+" : ""} 个缺口的修复任务`);
      await refreshOverview(true);
    } catch (error: any) {
      message.error(template("extSyncFailed", { message: error?.message || String(error) }));
    } finally {
      setRepairingDataset(null);
    }
  };

  const activeDataset = activeTask ? datasetFromTask(activeTask) : null;
  const taskRunning = activeTasks.some((task) => ["queued", "running"].includes(task.status));
  const anyRunning = taskRunning || Boolean(startingDataset);
  const dashboardUnavailable = !overview && overviewError !== null;
  const controlsDisabled = dashboardUnavailable;
  const taskForDataset = (dataset: ExternalSyncDataset) => activeTasks.find(
    (task) => datasetFromTask(task) === dataset && ["queued", "running"].includes(task.status),
  );
  const historyCapabilitiesReady = Boolean(capabilities && !capabilitiesLoading && !capabilitiesError);
  const northboundCapability = capabilities?.capital_flow?.northbound;
  const northboundReady = Boolean(northboundCapability?.available === true && !capabilitiesLoading && !capabilitiesError);
  const pipelineOutcome = pipelineTask ? scoringOutcome(pipelineTask) : null;
  const pipelineUniverseDescription = pipelineTask ? scoringTaskUniverseDescription(pipelineTask) : null;
  const rows = useMemo(() => {
    const byDataset = new Map((overview?.datasets || []).map((item) => [item.dataset, item]));
    return DATASET_ORDER.map(
      (dataset) =>
        byDataset.get(dataset) || {
          dataset,
          records: 0,
          symbols: 0,
          latest_date: null,
          last_updated_at: null,
          latest_task: null,
        },
    );
  }, [overview]);
  const coverageByDataset = useMemo(
    () => new Map((coverage?.datasets || []).map((item) => [item.dataset, item])),
    [coverage],
  );
  const syncCoverageByDataset = useMemo(
    () => new Map((syncCoverage?.datasets || []).map((item) => [item.dataset, item])),
    [syncCoverage],
  );
  const coverageActions = useMemo(
    () => (coverage?.datasets || []).filter((item) => DATASET_ORDER.includes(item.dataset) && ["blocked", "limited", "unknown"].includes(item.readiness)),
    [coverage],
  );

  const columns: ColumnsType<ExternalDatasetOverview> = [
    {
      title: t("extDashboardDataset"),
      dataIndex: "dataset",
      width: 330,
      render: (dataset: ExternalSyncDataset) => {
        const config = configs[dataset as keyof typeof configs];
        return (
          <Space align="start" size={10}>
            <span style={{ color: "#1677ff", fontSize: 17, lineHeight: "24px" }}>{config.icon}</span>
            <div>
              <Typography.Text strong>{config.title}</Typography.Text>
              <div style={{ color: "var(--text-muted, #667085)", fontSize: 12, marginTop: 3, lineHeight: 1.45 }}>
                {config.desc}
              </div>
            </div>
          </Space>
        );
      },
    },
    {
      title: t("extDashboardInventory"),
      width: 155,
      render: (_, row) => (
        <div>
          <Typography.Text strong>{formatCount(row.records)}</Typography.Text>
          <div style={{ color: "var(--text-muted, #667085)", fontSize: 12, marginTop: 3 }}>
            {template("extDashboardSymbols", { count: formatCount(row.symbols) })}
          </div>
        </div>
      ),
    },
    {
      title: t("extDashboardFreshness"),
      width: 180,
      render: (_, row) => (
        <div>
          <Typography.Text>{formatDate(row.latest_date)}</Typography.Text>
          <div style={{ color: "var(--text-muted, #667085)", fontSize: 12, marginTop: 3 }}>
            <Tooltip title={t("extDashboardStoredAtHint")}><span>{formatDate(row.last_updated_at, true)}</span></Tooltip>
          </div>
        </div>
      ),
    },
    {
      title: <Tooltip title="A 股日覆盖率和连续天数按交易日计算，剔除周末及交易所节假日休市，不计为缺失；估值按真实快照日期统计，评分可在 7 天内沿用已有快照。财报按实际公告日期生效，假期公告仍保留。ETF 日覆盖按最新交易日的全市场境内 ETF 统计，本次仅同步观察池时不会代表全市场已覆盖。本地数据完整情况不表示已生成模型评分。"><span>本地数据完整情况<QuestionCircleOutlined style={{ marginLeft: 4 }} /></span></Tooltip>,
      width: 230,
      render: (_, row) => {
        const diagnostic = coverageByDataset.get(row.dataset);
        if (!diagnostic) return <Typography.Text type={coverageError ? "danger" : "secondary"}>{coverageError ? "覆盖读取失败" : coverageLoading ? "读取中" : "暂无覆盖诊断"}</Typography.Text>;
        const field = diagnostic.fields.find((item) => item.nonnull_rows > 0) || diagnostic.fields[0];
        const color = {
          available: "success",
          limited: "warning",
          event: "processing",
          snapshot: "purple",
          blocked: "error",
          unknown: "default",
          not_applicable: "default",
        }[diagnostic.readiness] as "success" | "warning" | "processing" | "purple" | "error" | "default";
        if (!field) {
          return (
            <div>
              <Tag color={color}>{diagnostic.reason === "partial_fields_available" ? "部分指标可用" : readinessLabel(diagnostic.readiness)}</Tag>
              <Typography.Text type={diagnostic.readiness === "blocked" ? "danger" : "secondary"} style={{ display: "block", fontSize: 12, marginTop: 3 }}>
                {datasetCoverageReason(diagnostic)}
              </Typography.Text>
            </div>
          );
        }
        const nonnullRate = field.table_rows > 0 ? `${((field.nonnull_rows / field.table_rows) * 100).toFixed(1)}%` : "-";
        const dailyCoverage = field.latest_daily_coverage == null ? "-" : `${(field.latest_daily_coverage * 100).toFixed(1)}%`;
        return (
          <div>
            <Tag color={color}>{diagnostic.reason === "partial_fields_available" ? "部分指标可用" : readinessLabel(diagnostic.readiness)}</Tag>
            <Typography.Text style={{ fontSize: 12 }}>{field.first_date || "-"} ~ {field.latest_date || "-"}</Typography.Text>
            <div style={{ color: "var(--text-muted, #667085)", fontSize: 12, marginTop: 3 }}>
              非空 {nonnullRate} · {row.dataset === "etf" ? "全市场 ETF 日覆盖" : "日覆盖"} {dailyCoverage} · {row.dataset === "etf" ? "快照" : `连续 ${field.continuity_days} 日`}
            </div>
            {diagnostic.reason && (
              <Typography.Text type={diagnostic.readiness === "blocked" ? "danger" : "secondary"} style={{ display: "block", fontSize: 12, marginTop: 3 }}>
                {datasetCoverageReason(diagnostic)}
              </Typography.Text>
            )}
          </div>
        );
      },
    },
    {
      title: <Tooltip title="观测范围和已记录点数包含盘中或字段不全的数据；完整点数才表示已确认完成。起止日期之间仍可能存在缺口，补缺同步会检查实际缺失。"><span>已更新的标的和日期</span></Tooltip>,
      width: 230,
      render: (_, row) => {
        const item = syncCoverageByDataset.get(row.dataset);
        if (!item) return <Typography.Text type="secondary">{syncCoverageError ? "同步台账读取失败" : syncCoverage ? "暂无同步记录" : "读取中"}</Typography.Text>;
        return (
          <div style={{ fontSize: 12 }}>
            <div>观测范围：{item.earliest_date || "-"} ~ {item.latest_date || "-"}</div>
            <div>已记录 {formatCount(item.covered_points)} 个标的日期{item.completed_points != null ? ` · 完整 ${formatCount(item.completed_points)} 个` : " · 完整点数待确认"}</div>
            <div style={{ color: "var(--text-muted, #667085)", marginTop: 3 }}>首次同步：{formatDate(item.first_synced_at, true)}</div>
            <div style={{ color: "var(--text-muted, #667085)" }}>最近同步：{formatDate(item.last_synced_at, true)}</div>
          </div>
        );
      },
    },
    {
      title: t("extDashboardLastTask"),
      width: 230,
      render: (_, row) => {
        const task = row.latest_task;
        return (
          <div>
            {statusTag(task)}
            {task && (
              <>
                <Typography.Text style={{ fontSize: 12 }}>
                  {taskProgressText(task)} · {t("extDashboardSuccess")} {task.ok_count ?? 0} · {t("extDashboardFailed")} {task.failed_count ?? 0}
                  {Boolean(task.result?.skipped) && ` · 跳过 ${task.result!.skipped}`}
                </Typography.Text>
                <div style={{ color: "var(--text-muted, #667085)", fontSize: 12, marginTop: 3 }}>
                  {taskPolicyLabel(task)} · {taskRange(task)}
                </div>
                <div style={{ color: "var(--text-muted, #667085)", fontSize: 12, marginTop: 3 }}>
                  {taskExecutionTimes(task)}
                </div>
                {(task.status === "failed" || taskAllAttemptsFailed(task) || task.result?.skip_reason || task.result?.field_coverage) && task.message && (
                  <Typography.Text type={task.status === "failed" ? "danger" : "secondary"} ellipsis={{ tooltip: task.message }} style={{ display: "block", fontSize: 12, marginTop: 3 }}>
                    {task.message}
                  </Typography.Text>
                )}
              </>
            )}
          </div>
        );
      },
    },
    {
      title: t("extDashboardAction"),
      width: 220,
      fixed: "right",
      render: (_, row) => {
        const repairable = row.dataset === "fundamental" || row.dataset === "financial" || row.dataset === "capital_flow";
        const resumableTask = Boolean(row.latest_task && taskCanResume(row.latest_task) && row.latest_task.batch_recovery);
        const datasetCapability = capabilities?.[row.dataset];
        const historyCapabilityPending = syncMode === "backfill" && (!historyCapabilitiesReady || !datasetCapability);
        const snapshotOnly = syncMode === "backfill" && historyCapabilitiesReady && Boolean(datasetCapability) && !datasetCapability!.modes.includes("backfill");
        const historyLimit = datasetCapability?.history_limit_days;
        const chunkDays = datasetCapability?.chunk_days ?? null;
        const autoChunked = syncMode === "backfill" && !snapshotOnly && Boolean(chunkDays) && chunkDays! > 0;
        const spanExceedsLimit = syncMode === "backfill" && !snapshotOnly && !autoChunked && historySpan !== null && historyLimit != null && historyLimit > 0 && historySpan > historyLimit;
        let disabledReason: string | null = null;
        if (historyCapabilityPending) disabledReason = "历史同步能力尚未确认，请等待加载或重试读取同步能力";
        else if (!snapshotOnly && historyRangeIssue) disabledReason = historyRangeIssue;
        else if (spanExceedsLimit) disabledReason = `所选范围为 ${historySpan} 日，超过该数据源 ${historyLimit} 日的历史上限，请缩短日期范围`;
        else if (taskForDataset(row.dataset)) disabledReason = "该数据集正在同步，请等待当前任务完成";
        const snapshotHint = snapshotOnly ? "该数据只提供最近快照；点击同步会先确认，并按最近数据执行" : null;
        const chunkHint = autoChunked && historySpan !== null && chunkDays && historySpan > chunkDays
          ? `所选范围共 ${historySpan} 日，将按每段不超过 ${chunkDays} 日自动分段执行`
          : null;
        const partialHistoryHint = syncMode === "backfill" && Boolean(datasetCapability?.partial_history_fields?.length) && !disabledReason
          ? "历史回填只补净值、收盘价、溢折价和评分"
          : null;
        const actionHint = snapshotHint || chunkHint || partialHistoryHint;
        return (
          <Space direction="vertical" size={6}>
            <Tooltip title={disabledReason || undefined}>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <Button
                  type="primary"
                  icon={<SyncOutlined spin={startingDataset === row.dataset || Boolean(taskForDataset(row.dataset))} />}
                  loading={startingDataset === row.dataset}
                  disabled={controlsDisabled || Boolean(disabledReason)}
                  onClick={() => void runSync(row.dataset)}
                >
                  {taskForDataset(row.dataset) ? t("extSyncing") : configs[row.dataset].title}
                </Button>
                {disabledReason || actionHint ? (
                  <Tooltip title={disabledReason || actionHint}>
                    <QuestionCircleOutlined
                      aria-label={disabledReason ? "查看不可同步原因" : chunkHint ? "查看分段同步说明" : snapshotHint ? "查看快照同步说明" : "查看部分历史字段说明"}
                      style={{ color: disabledReason ? "#98a2b3" : "#d48806", cursor: "help", fontSize: 15 }}
                    />
                  </Tooltip>
                ) : null}
              </span>
            </Tooltip>
            {repairable && settingsOpen ? (
              <Button
                size="small"
                loading={repairingDataset === row.dataset}
                disabled={controlsDisabled || !overview || Boolean(taskForDataset(row.dataset)) || historyCapabilityPending || Boolean(historyRangeIssue) || spanExceedsLimit || ((syncMode !== "backfill" || !dateRange[0] || !dateRange[1]) && (coverageLoading || Boolean(coverageError) || !coverage))}
                onClick={() => void repairDetectedGaps(row.dataset as "fundamental" | "financial" | "capital_flow")}
              >
                检查缺失数据
              </Button>
            ) : null}
            {row.dataset === "fundamental" && settingsOpen ? (
              <Tooltip title={`${t("extFullValuationHistoryHint")} 此操作会重新获取全部股票的历史估值，并更新已有记录。`}>
                <Button
                  size="small"
                  disabled={controlsDisabled || !historyCapabilitiesReady || Boolean(taskForDataset("fundamental"))}
                  onClick={() => void runSync("fundamental", {
                    source: "all",
                    mode: "backfill",
                    sync_policy: "force",
                    start_date: FULL_HISTORY_START_DATE,
                    end_date: dayjs().format("YYYY-MM-DD"),
                  })}
                >
                  {t("extFullValuationHistory")}
                </Button>
              </Tooltip>
            ) : null}
            {resumableTask && row.latest_task ? (
              <Button
                size="small"
                loading={taskActionLoading === "retry"}
                disabled={controlsDisabled || taskActionLoading === "retry"}
                onClick={() => void resumeTaskFromCursor(row.latest_task!)}
              >
                {t("extResumeFromCursor")}
              </Button>
            ) : null}
          </Space>
        );
      },
    },
  ];

  const resultTask = finishedTask || (activeTask && !taskRunning ? activeTask : null);

  return (
    <section className="external-data-sync-section" style={{ width: "100%" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "flex-start", marginBottom: 18 }}>
        <div>
          <Space size={8}>
            <DatabaseOutlined style={{ color: "#1677ff" }} />
            <Typography.Title level={4} style={{ margin: 0, fontSize: 18 }}>{t("extSectionTitle")}</Typography.Title>
            <Tooltip title={t("extSectionDesc")}><QuestionCircleOutlined style={{ color: "#98a2b3" }} /></Tooltip>
          </Space>
          <Typography.Paragraph type="secondary" style={{ margin: "6px 0 0", fontSize: 13 }}>
            {t("extSectionDesc")}
          </Typography.Paragraph>
        </div>
        <Tooltip title={t("extDashboardRefresh")}>
          <Button
            aria-label={t("extDashboardRefresh")}
            icon={<ReloadOutlined spin={overviewLoading} />}
            onClick={() => void refreshOverview()}
            disabled={overviewLoading}
          />
        </Tooltip>
      </div>

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "6px 28px",
          alignItems: "baseline",
          padding: "9px 14px",
          border: "1px solid var(--border-color, #eaecf0)",
          borderRadius: 6,
          marginBottom: 14,
          background: "var(--fill-quaternary, #fafafa)",
        }}
      >
        {([
          [t("extDashboardRecords"), overview?.total_records ?? "-"],
          [t("extDashboardCoverage"), overview?.covered_symbols ?? "-"],
          [t("extDashboardAvailable"), overview ? `${overview.available_datasets}/${DATASET_ORDER.length}` : "-"],
          [t("extDashboardRunning"), overview?.running_tasks ?? "-"],
        ] as Array<[string, string | number]>).map(([label, value]) => (
          <span key={label} style={{ display: "inline-flex", alignItems: "baseline", gap: 6 }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{label}</Typography.Text>
            <Typography.Text strong style={{ fontSize: 16 }}>
              {typeof value === "number" ? formatCount(value) : value}
            </Typography.Text>
          </span>
        ))}
      </div>

      {activeTasks.length > 0 && activeTasks.map((task) => {
        const dataset = datasetFromTask(task);
        const running = ["queued", "running"].includes(task.status);
        return (
          <div key={task.id} style={{ border: "1px solid #91caff", background: "#f0f7ff", borderRadius: 6, padding: "14px 16px", marginBottom: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", marginBottom: 8 }}>
              <Space wrap>
                <Typography.Text strong>{dataset ? configs[dataset].title : task.task_type}</Typography.Text>
                {statusTag(task)}
                <Tag>{taskPolicyLabel(task)}</Tag>
                <Typography.Text type="secondary">{taskRange(task)}</Typography.Text>
                {running ? <Button size="small" danger loading={taskActionLoading === "cancel"} disabled={task.cancel_requested} onClick={() => void cancelActiveTask(task)}>{task.cancel_requested ? "正在停止" : "停止更新"}</Button> : null}
                {!running && taskCanResume(task) ? <Button size="small" type="primary" loading={taskActionLoading === "retry"} onClick={() => void resumeTaskFromCursor(task)}>继续上次更新</Button> : null}
              </Space>
              <Typography.Text type="secondary">{taskProgressText(task)}</Typography.Text>
            </div>
            <Progress
              percent={task.stage === "mirror" && running ? undefined : Math.round(task.percent)}
              status={task.status === "failed" || taskAllAttemptsFailed(task) ? "exception" : task.status === "done" ? "success" : "active"}
              format={task.stage === "mirror" && running ? () => "正在保存到本地" : undefined}
            />
            <div style={{ display: "flex", gap: 18, flexWrap: "wrap", color: "#475467", fontSize: 12, marginTop: 6 }}>
              <span>{stageLabel(task.stage)}</span><span>同步数量：{taskProgressText(task)}</span>
              <span>{t("extDashboardSuccess")}：{task.ok_count ?? 0}</span><span>{t("extDashboardFailed")}：{task.failed_count ?? 0}</span>
              {task.current_item && <span>{t("extDashboardCurrent")}：{task.current_item}</span>}
              <span>{taskExecutionTimes(task)}</span>
            </div>
          </div>
        );
      })}

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
        <Space size={6} wrap align="center">
          <Typography.Text strong>{t("extScopeLabel")}:</Typography.Text>
          <Select
            value={source}
            onChange={(value) => setSource(value)}
            style={{ width: 150 }}
            disabled={controlsDisabled}
            options={[
              { value: "watchlist", label: t("extSourceWatchlist") },
              { value: "positions", label: t("extSourcePositions") },
              { value: "all", label: t("extSourceAllMarket") },
            ]}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {syncScopeSummary(source, primaryWatchlistName, app?.portfolioId)}
          </Typography.Text>
        </Space>

        <Space size={6} wrap align="center">
          <Typography.Text strong>{t("extSyncModeLabel")}:</Typography.Text>
          <Select
            aria-label="更新日期"
            value={syncMode}
            onChange={changeSyncMode}
            disabled={controlsDisabled}
            style={{ width: 130 }}
            options={[
              { value: "incremental", label: "最近数据" },
              { value: "backfill", label: "指定历史日期" },
            ]}
          />
          {syncMode === "backfill" ? (
            <>
              <Select
                aria-label="日期跨度预设"
                value={datePreset}
                onChange={changeDatePreset}
                disabled={controlsDisabled}
                style={{ width: 120 }}
                options={HISTORY_DATE_PRESETS}
              />
              <DatePicker.RangePicker
                value={dateRange}
                disabled={controlsDisabled}
                onChange={changeDateRange}
                disabledDate={(value) => value.isAfter(dayjs(), "day")}
                allowClear
                placeholder={["开始日期", "结束日期"]}
              />
            </>
          ) : null}
        </Space>

        <Space size={6} wrap align="center">
          <Typography.Text strong>更新方式:</Typography.Text>
          <Select
            aria-label="更新方式"
            value={syncPolicy}
            onChange={(value: ExternalSyncPolicy) => { setSyncPolicy(value); setPreviewPlan(null); }}
            disabled={controlsDisabled}
            style={{ width: 235 }}
            options={Object.entries(SYNC_POLICY_LABELS).map(([value, label]) => ({ value, label }))}
          />
        </Space>

        {showScoringRefresh && <Space size={8} wrap align="center" style={{ marginLeft: "auto" }}>
          <Tooltip title={`当前评分仅支持 A 股股票。${scoringScopeIssue ?? `仅同步外部数据不会自动刷新评分；此操作会计算输入，在真实模型已启用且数据齐全时生成模型评分，并保留任务证据。${syncMode === "backfill" ? "按右侧所选日期范围重算。" : "默认重算最近约 11 天。"}为保持评分可比性，仍使用全市场数据作为计算参照，仅为所选范围生成评分。未启用模型或输入不足时会明确说明未生成或仅部分生成评分。`}`}>
            <Button
              aria-label="刷新评分"
              type="primary"
              ghost
              icon={<SyncOutlined />}
              loading={pipelineStarting}
              disabled={controlsDisabled || Boolean(historyRangeIssue) || Boolean(scoringScopeIssue) || taskRunning || Boolean(pipelineTask && ["queued", "running"].includes(pipelineTask.status))}
              onClick={() => void refreshFactorScores()}
            >
              刷新评分
            </Button>
          </Tooltip>
          <Typography.Text type={scoringScopeIssue ? "warning" : "secondary"} style={{ fontSize: 12 }}>
            {scoringScopeIssue ?? "仅 A 股股票；评分计算参照全市场数据"}
          </Typography.Text>
        </Space>}
      </div>

      <Collapse
        size="small"
        style={{ marginBottom: 12 }}
        activeKey={settingsOpen ? ["settings"] : []}
        onChange={(keys) => setSettingsOpen(keys.includes("settings"))}
        items={[{ key: "settings", label: "日期和更新设置", children: (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
              <Checkbox checked={includeNorthbound && northboundReady} disabled={controlsDisabled || !northboundReady} onChange={(event) => setIncludeNorthbound(event.target.checked)}>
                {t("extIncludeNorthbound")}
              </Checkbox>
              {!northboundReady && <Typography.Text type="warning">{capabilitiesLoading ? "北向资金能力正在确认，仅同步个股资金流" : northboundCapability?.available === false ? `北向资金不可用：${northboundCapability.reason}` : "北向资金能力未知，仅同步个股资金流；请重试读取同步能力"}</Typography.Text>}
            </div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {SYNC_POLICY_HINTS[syncPolicy]}{syncMode === "incremental" ? " 需要过去的数据时，选择“指定历史日期”。" : " ETF 指标和人气榜仅提供最近数据，选择历史日期后同步会先确认，并按最近数据执行。"}
            </Typography.Text>
            {syncMode === "backfill" && <Typography.Text type={historyRangeIssue ? "warning" : "secondary"} style={{ fontSize: 12 }}>
              {historyRangeIssue ?? `${dateRange[0]!.format("YYYY-MM-DD")} → ${dateRange[1]!.format("YYYY-MM-DD")} · ${historySpan} 个自然日（含首尾）`}
            </Typography.Text>}
          </div>
        ) }]}
      />
      {source === "watchlist" && !contextWatchlistsAvailable && watchlistsError && <Button
        size="small"
        loading={watchlistsLoading}
        onClick={() => void refreshStandaloneWatchlists()}
        style={{ marginBottom: 12 }}
      >重试读取观察池</Button>}

      {coverageError && <Alert type="error" showIcon message="数据检查结果读取失败" description={coverageError} action={<Button size="small" loading={coverageLoading} onClick={() => void refreshCoverage()}>重试读取检查结果</Button>} style={{ marginBottom: 14 }} />}
      {syncCoverageError && <Alert type="warning" showIcon message="更新记录暂时读不到，仍可更新数据，开始前会检查所选范围" action={<Button size="small" onClick={() => void refreshSyncCoverage()}>重试读取更新记录</Button>} style={{ marginBottom: 14 }} />}
      {capabilitiesError && <Alert type="warning" showIcon message="可用数据范围读取失败" description={capabilitiesError} action={<Button size="small" loading={capabilitiesLoading} onClick={() => void refreshCapabilities()}>重试读取可用范围</Button>} style={{ marginBottom: 14 }} />}
      {showScoringRefresh && pipelineTask && pipelineOutcome && <Alert
        type={pipelineOutcome.type}
        showIcon
        style={{ marginBottom: 14 }}
        message={`评分：${pipelineOutcome.label}${pipelineTask.stage ? ` · ${scoringStageLabel(pipelineTask.stage)}` : ""}${typeof pipelineTask.percent === "number" ? ` · ${Math.round(pipelineTask.percent)}%` : ""}`}
        description={<Space direction="vertical" size={4}>
          {pipelineUniverseDescription && <span>{pipelineUniverseDescription}</span>}
          {pipelineOutcome.detail && <span>{pipelineOutcome.detail}</span>}
          {pipelineTask.message && <span>{pipelineTask.message}</span>}
          {pipelinePollError && <Typography.Text type="danger">任务状态读取失败：{pipelinePollError}；正在重试</Typography.Text>}
          {(pipelineTask.result?.completion_status === "AWAITING_MODEL_ACTIVATION" || (pipelineTask.result?.scoring_bridge as { status?: string } | undefined)?.status === "SKIPPED") && <Button size="small" onClick={() => window.dispatchEvent(new CustomEvent("settings:navigate", { detail: "factor-model" }))}>设置评分模型</Button>}
        </Space>}
      />}

      {dashboardUnavailable ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 14 }}
          message={t("extOverviewUnavailable")}
          description={overviewError}
          action={<Button size="small" onClick={() => void refreshOverview()}>{t("extRetryLoad")}</Button>}
        />
      ) : null}

      {source === "all" && syncMode === "incremental" ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 14 }}
          message={t("extAllScopeSnapshotHint")}
        />
      ) : null}

      {syncMode === "backfill" ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 14 }}
          message="只保存所选日期内确实能获取的记录。只能提供最近数据的项目不能下载历史。"
        />
      ) : null}

      {coverageActions.length > 0 ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 14 }}
          message={(
            <Space size={8} wrap>
              <span>{t("extCoverageActionTitle")}</span>
              <Tag
                color={coverageActions.some((item) => item.readiness === "blocked") ? "error" : "warning"}
                style={{ marginInlineEnd: 0 }}
              >
                {coverageActions.length} 项
              </Tag>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                数据不足会降低因子覆盖率
              </Typography.Text>
            </Space>
          )}
          action={(
            <Space size={4} wrap>
              {showScoringRefresh && <Button size="small" onClick={() => window.dispatchEvent(new CustomEvent("settings:navigate", { detail: "factor-model" }))}>
                {t("extCoverageActionModel")}
              </Button>}
              <Button size="small" onClick={() => window.dispatchEvent(new CustomEvent("settings:navigate", { detail: "api-management" }))}>
                {t("extCoverageActionApi")}
              </Button>
              <Button type="link" size="small" onClick={() => setCoverageDetailOpen((open) => !open)}>
                {coverageDetailOpen ? "收起详情" : "查看详情"}
              </Button>
            </Space>
          )}
          description={coverageDetailOpen ? (
            <div>
              <div style={{ marginBottom: 8 }}>{t("extCoverageActionDescription")}</div>
              <Space wrap>
                {coverageActions.map((item) => (
                  <Tag key={item.dataset} color={item.readiness === "blocked" ? "error" : "warning"}>
                    {configs[item.dataset].title}：{datasetCoverageReason(item)}
                  </Tag>
                ))}
              </Space>
            </div>
          ) : undefined}
        />
      ) : null}

      {previewPlan ? (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 14 }}
          message={`${configs[previewPlan.dataset].title} · ${SYNC_POLICY_LABELS[previewPlan.sync_policy ?? "normal"]}: ${previewPlan.requested_start_date} ~ ${previewPlan.requested_end_date} (${previewPlan.requested_span_days} 日)`}
          description={previewPlan.provider_history_limit_days == null
            ? previewPlan.provider_reason
            : `${previewPlan.provider_reason} 历史上限: ${previewPlan.provider_history_limit_days} 日`}
        />
      ) : null}

      {syncError && <Alert
        type="error"
        showIcon
        style={{ marginBottom: 14 }}
        message={(
          <Space size={8} wrap>
            <span>这次更新遇到问题</span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{configs[syncError.dataset].title}</Typography.Text>
            {!syncErrorDetailOpen && <span
              title={syncError.detail}
              style={{
                display: "inline-block",
                maxWidth: "min(620px, 50vw)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                verticalAlign: "bottom",
              }}
            >
              {syncError.detail}
            </span>}
          </Space>
        )}
        description={syncErrorDetailOpen ? (
          <Space direction="vertical" size={4}>
            <span>{syncError.detail}</span>
            <Typography.Text type="secondary">{syncError.range}</Typography.Text>
            <span>重试会沿用这次失败时的范围和日期，当前设置保留。</span>
          </Space>
        ) : undefined}
        action={(
          <Space size={4} wrap>
            <Button loading={startingDataset === syncError.dataset} disabled={controlsDisabled || Boolean(taskForDataset(syncError.dataset))} onClick={syncError.retry}>重试这次更新</Button>
            <Button type="link" size="small" onClick={() => setSyncErrorDetailOpen((open) => !open)}>
              {syncErrorDetailOpen ? "收起详情" : "查看详情"}
            </Button>
          </Space>
        )}
      />}

      <Table<ExternalDatasetOverview>
        rowKey="dataset"
        columns={columns}
        dataSource={rows}
        loading={overviewLoading && !overview}
        pagination={false}
        size="middle"
        scroll={{ x: 1580 }}
      />

      {resultTask?.result && (
        <Alert
          style={{ marginTop: 16 }}
          type={taskHasWarnings(resultTask) ? "warning" : "success"}
          showIcon
          message={`${configs[resultTask.result.dataset].title}: ${template("extSyncResult", syncResultParams(resultTask.result))}`}
          description={
            <Space direction="vertical" style={{ width: "100%" }}>
              {resultTask.result.dataset === "etf" && resultTask.message && <Typography.Text>{resultTask.message}</Typography.Text>}
              {resultTask.result.warnings?.map((warning) => <Typography.Text type="warning" key={warning}>{warning}</Typography.Text>)}
              {resultTask.result.warehouse_mirror && <Typography.Text type={resultTask.result.warehouse_mirror.status === "warning" ? "warning" : "secondary"}>
                本地分析数据：{resultTask.result.warehouse_mirror.status === "warning" ? `失败：${resultTask.result.warehouse_mirror.error || "请继续上次更新"}` : resultTask.result.warehouse_mirror.status === "skipped" ? "未执行" : `已写入 ${formatCount(resultTask.result.warehouse_mirror.records)} 条记录`}
              </Typography.Text>}
              {resultTask.result.northbound && <Typography.Text type={resultTask.result.northbound.status === "warning" ? "warning" : "secondary"}>
                北向资金：{resultTask.result.northbound.status === "warning" ? `同步未完成：${resultTask.result.northbound.error || "数据源未返回有效记录"}` : `已写入 ${formatCount(resultTask.result.northbound.records)} 条记录`}
              </Typography.Text>}
              {resultTask.result.errors.length > 0 ? (
              <Collapse
                size="small"
                ghost
                items={[{
                  key: "errors",
                  label: `${t("extErrorsTitle")} (${resultTask.result.errors.length})`,
                  children: (
                    <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12, color: "#b42318" }}>
                      {resultTask.result.errors.map((error, index) => <li key={`${index}-${error}`}>{error}</li>)}
                    </ul>
                  ),
                }]}
              />
            ) : null}
            </Space>
          }
        />
      )}
      <Collapse
        size="small"
        style={{ marginTop: 16 }}
        items={[{ key: "calculation-checks", label: "检查数据计算过程", children: <VerifiedInputReplay /> }]}
      />
    </section>
  );
}
import VerifiedInputReplay from "./VerifiedInputReplay";
