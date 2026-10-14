import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  DatePicker,
  Progress,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  BankOutlined,
  ClockCircleOutlined,
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
  type ExternalDataCoverage,
  type ExternalDatasetCoverage,
  type ExternalSyncPlanDetails,
  type ExternalSyncDataset,
  type ExternalSyncTask,
  type FactorPipelineTask,
  type TailProxyImportResult,
} from "../api/client";

type SyncSource = "watchlist" | "positions" | "all";

const FULL_HISTORY_START_DATE = "1990-01-01";

const DATASET_ORDER: ExternalSyncDataset[] = [
  "fundamental",
  "financial",
  "lhb",
  "hot_rank",
  "tail_proxy",
  "capital_flow",
  "etf",
];

const TASK_DATASET_PREFIX = "external_sync_";
const isRunningTask = (task: { status: string }) => ["queued", "running"].includes(task.status);

function datasetFromTask(task: ExternalSyncTask): ExternalSyncDataset | null {
  if (task.result?.dataset) return task.result.dataset;
  if (!task.task_type.startsWith(TASK_DATASET_PREFIX)) return null;
  const dataset = task.task_type.slice(TASK_DATASET_PREFIX.length) as ExternalSyncDataset;
  return DATASET_ORDER.includes(dataset) ? dataset : null;
}

function formatDate(value: string | null, withTime = false): string {
  if (!value) return t("extNoData");
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return withTime ? parsed.toLocaleString() : parsed.toLocaleDateString();
}

function formatCount(value: number): string {
  return new Intl.NumberFormat().format(value);
}

function taskRange(task: ExternalSyncTask): string {
  try {
    const plan = (task.batch_recovery?.plan || {}) as Record<string, string | undefined>;
    return `${plan.requested_start_date || plan.start_date || "-"} ~ ${plan.requested_end_date || plan.end_date || "-"}`;
  } catch {
    return "- ~ -";
  }
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
  return task.failed_count > 0 || Boolean(task.result?.errors?.length) || task.result?.warehouse_mirror?.status === "warning" || task.result?.northbound?.status === "warning";
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
  if (task.cancel_requested && isRunningTask(task)) return <Tag color="warning">正在取消</Tag>;
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

function scoringOutcome(task: FactorPipelineTask): { label: string; type: "success" | "warning" | "error" | "info"; detail?: string } {
  if (task.status === "cancelled") return { label: "已取消", type: "info" };
  if (task.status === "failed") return { label: "失败", type: "error" };
  if (!["done", "completed"].includes(task.status)) return { label: "处理中", type: "info" };
  const bridge = task.result?.scoring_bridge as { status?: string; materialized_count?: number; reason?: string } | undefined;
  const completion = task.result?.completion_status;
  if (completion === "TRAINING_REJECTED") return { label: "模型未通过验证", type: "warning", detail: "候选模型未通过数据或模型门槛，未生成可用模型评分。" };
  if (completion === "AWAITING_MODEL_ACTIVATION" || bridge?.status === "SKIPPED") {
    return { label: "输入计算完成，等待模型激活", type: "warning", detail: "未生成模型评分。请到因子模型查看真实训练结果，激活有效模型后再刷新评分特征。" };
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

export default function ExternalDataSync() {
  // The data-center can be embedded independently in tests and operational
  // tools. AppContext enriches the watchlist scope when present, but the
  // synchronization surface must remain usable without the global workbench.
  const app = useContext(AppContext);
  const [source, setSource] = useState<SyncSource>("watchlist");
  const [includeNorthbound, setIncludeNorthbound] = useState(true);
  const [syncMode, setSyncMode] = useState<"incremental" | "backfill">("incremental");
  const [lookbackDays, setLookbackDays] = useState(60);
  const [dateRange, setDateRange] = useState<[Dayjs | null, Dayjs | null]>([null, null]);
  const [capabilities, setCapabilities] = useState<ExternalDataSyncCapabilities | null>(null);
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(true);
  const [capabilitiesError, setCapabilitiesError] = useState<string | null>(null);
  const [coverage, setCoverage] = useState<ExternalDataCoverage | null>(null);
  const [coverageLoading, setCoverageLoading] = useState(true);
  const [coverageError, setCoverageError] = useState<string | null>(null);
  const [previewPlan, setPreviewPlan] = useState<ExternalDataSyncPlan | null>(null);
  const [overview, setOverview] = useState<ExternalDataOverview | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [startingDataset, setStartingDataset] = useState<ExternalSyncDataset | null>(null);
  const [activeTask, setActiveTask] = useState<ExternalSyncTask | null>(null);
  const [activeTasks, setActiveTasks] = useState<ExternalSyncTask[]>([]);
  const [finishedTask, setFinishedTask] = useState<ExternalSyncTask | null>(null);
  const [taskActionLoading, setTaskActionLoading] = useState<"cancel" | "retry" | null>(null);
  const [partitionDetails, setPartitionDetails] = useState<ExternalSyncPlanDetails | null>(null);
  const [repairingDataset, setRepairingDataset] = useState<"fundamental" | "financial" | "capital_flow" | null>(null);
  const [tailImporting, setTailImporting] = useState(false);
  const [tailImportResult, setTailImportResult] = useState<TailProxyImportResult | null>(null);
  const [pipelineStarting, setPipelineStarting] = useState(false);
  const [pipelineTask, setPipelineTask] = useState<FactorPipelineTask | null>(null);
  const [pipelinePollError, setPipelinePollError] = useState<string | null>(null);
  const handledTaskIds = useRef(new Set<string>());

  const primaryWatchlistId = useMemo(() => {
    const lists = app?.workbench?.watchlists ?? [];
    if (app?.activeWatchlistId && lists.some((item) => item.id === app.activeWatchlistId)) {
      return app.activeWatchlistId;
    }
    return lists.find((item) => item.list_type === "watch")?.id ?? lists[0]?.id ?? null;
  }, [app?.activeWatchlistId, app?.workbench?.watchlists]);
  const primaryWatchlistName = useMemo(() => {
    const lists = app?.workbench?.watchlists ?? [];
    return lists.find((item) => item.id === primaryWatchlistId)?.name ?? null;
  }, [app?.workbench?.watchlists, primaryWatchlistId]);

  const configs = {
      fundamental: { title: t("extSyncFundamental"), desc: t("extSyncFundamentalDesc"), icon: <FundOutlined /> },
      financial: { title: t("extSyncFinancial"), desc: t("extSyncFinancialDesc"), icon: <FileTextOutlined /> },
      lhb: { title: t("extSyncLhb"), desc: t("extSyncLhbDesc"), icon: <BankOutlined /> },
      hot_rank: { title: t("extSyncHotRank"), desc: t("extSyncHotRankDesc"), icon: <FireOutlined /> },
      tail_proxy: { title: t("extSyncTailProxy"), desc: t("extSyncTailProxyDesc"), icon: <ClockCircleOutlined /> },
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

  const refreshOverview = useCallback(async (silent = false) => {
    if (!silent) setOverviewLoading(true);
    try {
      const next = await api.getExternalDataOverview();
      setOverview(next);
      setOverviewError(null);
      void refreshCoverage();
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
  }, [refreshCoverage]);

  const finishTask = useCallback(async (task: ExternalSyncTask) => {
    if (isRunningTask(task) || handledTaskIds.current.has(task.id)) return;
    handledTaskIds.current.add(task.id);
    setFinishedTask(task);
    if (taskAllAttemptsFailed(task)) {
      message.error("同步失败：全部尝试失败");
    } else if (task.status === "done" && !taskHasWarnings(task)) {
      message.success(t("extTaskCompleted"));
    } else if (task.status === "done") {
      message.warning(t("extTaskCompletedWithErrors"));
    } else if (task.status === "cancelled") {
      message.info("同步已取消，已完成的数据保留，可从断点继续");
    } else {
      message.error(task.message || t("extStatusFailed"));
    }
    await refreshOverview(true);
  }, [refreshOverview]);

  useEffect(() => {
    void refreshOverview();
  }, [refreshOverview]);

  useEffect(() => {
    void refreshCapabilities();
  }, [refreshCapabilities]);

  useEffect(() => {
    if (!activeTask) {
      setPartitionDetails(null);
      return;
    }
    void api.getExternalDataSyncTaskPartitions(activeTask.id)
      .then(setPartitionDetails)
      .catch(() => setPartitionDetails(null));
  }, [activeTask?.id, activeTask?.updated_at]);

  useEffect(() => {
    const runningTasks = activeTasks.filter((task) => ["queued", "running"].includes(task.status));
    if (!runningTasks.length) return;
    let disposed = false;
    const poll = async () => {
      try {
        const results = await Promise.allSettled(runningTasks.map((task) => api.getExternalDataSyncTask(task.id)));
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
        nextTasks.forEach((next) => {
          if (!["queued", "running"].includes(next.status)) void finishTask(next);
        });
      } catch (error: any) {
        if (!disposed) message.error(template("extSyncFailed", { message: error?.message || String(error) }));
      }
    };
    void poll();
    const timer = window.setInterval(poll, 1000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [activeTasks.map((task) => `${task.id}:${task.status}`).join(","), finishTask]);

  const runSync = async (
    dataset: ExternalSyncDataset,
    overrides: Partial<{
      source: SyncSource;
      mode: "incremental" | "backfill";
      start_date: string;
      end_date: string;
      lookback_days: number;
    }> = {},
  ) => {
    // Inventory is advisory for starting a sync. During the initial request it
    // may still be null, but the selected scope and date parameters are valid.
    // Keep the hard stop for a known dashboard failure so an unavailable
    // inventory cannot accidentally trigger a task.
    if (dashboardUnavailable) return;
    const effectiveSource = overrides.source ?? source;
    const effectiveMode = overrides.mode ?? syncMode;
    if (dataset === "tail_proxy" && effectiveSource === "all") {
      message.warning("尾盘分钟同步仅支持观察池或持仓范围，请缩小标的范围");
      return;
    }
    if (effectiveMode === "backfill" && (!capabilities || capabilitiesLoading || capabilitiesError || !capabilities[dataset]?.modes.includes("backfill"))) {
      message.warning("历史同步能力尚未确认或数据源不支持，请重试读取同步能力");
      return;
    }
    setStartingDataset(dataset);
    setFinishedTask(null);
    try {
      const effectiveStartDate = overrides.start_date
        ?? (dateRange[0] ? dateRange[0].format("YYYY-MM-DD") : undefined);
      const effectiveEndDate = overrides.end_date
        ?? (dateRange[1] ? dateRange[1].format("YYYY-MM-DD") : undefined);
      const syncPayload = {
        dataset,
        source: effectiveSource,
        watchlist_id: effectiveSource === "watchlist" ? primaryWatchlistId : undefined,
        portfolio_id: effectiveSource === "positions" ? app?.portfolioId ?? undefined : undefined,
        include_northbound: includeNorthbound && northboundReady,
        mode: effectiveMode,
        start_date: effectiveMode === "backfill" ? effectiveStartDate : undefined,
        end_date: effectiveMode === "backfill" ? effectiveEndDate : undefined,
        lookback_days: effectiveMode === "backfill" ? (overrides.lookback_days ?? lookbackDays) : 1,
        limit: 20,
        ...(effectiveMode === "backfill" && dataset === "fundamental" ? { max_workers: 4 } : {}),
      ...overrides,
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

  const refreshFactorScores = async () => { /* P2-4: UX 语义别名 — 显示文案统一使用「评分特征/评分流水线」而不是「因子」 */
    if (pipelineStarting) return;
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
      });
      setPipelineTask(task);
      setPipelinePollError(null);
      const taskId = typeof task === "object" && task ? (task as any).id ?? "-" : "-";
      message.success(`评分流水线任务已提交，任务号：${String(taskId)}`);
    } catch (error: any) {
      message.error(error?.message || "评分特征刷新启动失败，请稍后重试");
    } finally {
      setPipelineStarting(false);
    }
  };

  useEffect(() => {
    if (!pipelineTask || ["done", "completed", "failed", "cancelled"].includes(pipelineTask.status)) return;
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
  }, [pipelineTask?.id, pipelineTask?.status]);

  const repairDetectedGaps = async (dataset: "fundamental" | "financial" | "capital_flow") => {
    if (!overview) return;
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
    const end = dateRange[1]
      || (knownEnds.length ? dayjs(knownEnds.sort()[knownEnds.length - 1]) : dayjs());
    const start = dateRange[0]
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

  const importTailMinutes = async (file: File) => {
    if (!overview) {
      message.warning("同步看板尚未加载完成，请稍后再导入 CSV");
      return;
    }
    setTailImporting(true);
    setTailImportResult(null);
    try {
      const result = await api.importTailProxyMinutes({
        csv_text: await file.text(), filename: file.name,
      });
      setTailImportResult(result);
      if (result.warehouse_mirror?.status === "warning") message.warning(`原始分钟输入已保存：${result.written_sessions} 个交易日；因子仓库镜像失败，请重新导入重试`);
      else if (result.quality_snapshot?.status === "warning") message.warning("原始输入与因子仓库已写入，质量诊断刷新失败，请重试刷新诊断");
      else if (result.written_sessions > 0) message.success(`尾盘分钟导入完成：${result.written_sessions} 个有效交易日`);
      else message.warning("尾盘分钟未写入有效交易日，请检查 CSV 的交易时间和会话完整性");
      if (result.rejected_sessions > 0) {
        message.warning(`${result.rejected_sessions} 个交易日未满足完整尾盘会话校验`);
      }
      if (result.invalid_rows) message.warning(`CSV 中 ${result.invalid_rows} 行格式、数值或交易时间无效，未写入`);
      await refreshOverview(true);
    } catch (error: any) {
      message.error(template("extSyncFailed", { message: error?.message || String(error) }));
    } finally {
      setTailImporting(false);
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
  const tailImportWarning = Boolean(tailImportResult && (tailImportResult.written_sessions === 0 || tailImportResult.rejected_sessions > 0 || tailImportResult.invalid_rows || tailImportResult.warnings?.length || tailImportResult.warehouse_mirror?.status === "warning" || tailImportResult.quality_snapshot?.status === "warning"));
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
  const coverageActions = useMemo(
    () => (coverage?.datasets || []).filter((item) => ["blocked", "limited", "unknown"].includes(item.readiness)),
    [coverage],
  );

  const columns: ColumnsType<ExternalDatasetOverview> = [
    {
      title: t("extDashboardDataset"),
      dataIndex: "dataset",
      width: 330,
      render: (dataset: ExternalSyncDataset) => {
        const config = configs[dataset];
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
      title: <Tooltip title="估值按真实快照日期统计采集缺口，评分可在 7 天内沿用已有估值快照；财报按公告日期生效。仓库字段覆盖不表示已生成模型评分。"><span>仓库字段覆盖（非同步结果）<QuestionCircleOutlined style={{ marginLeft: 4 }} /></span></Tooltip>,
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
              <Tag color={color}>{readinessLabel(diagnostic.readiness)}</Tag>
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
            <Tag color={color}>{readinessLabel(diagnostic.readiness)}</Tag>
            <Typography.Text style={{ fontSize: 12 }}>{field.first_date || "-"} ~ {field.latest_date || "-"}</Typography.Text>
            <div style={{ color: "var(--text-muted, #667085)", fontSize: 12, marginTop: 3 }}>
              非空 {nonnullRate} · 日覆盖 {dailyCoverage} · 连续 {field.continuity_days} 日
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
                </Typography.Text>
                <div style={{ color: "var(--text-muted, #667085)", fontSize: 12, marginTop: 3 }}>
                  {formatDate(task.finished_at || task.updated_at, true)}
                </div>
                {(task.status === "failed" || taskAllAttemptsFailed(task)) && task.message && (
                  <Typography.Text type="danger" ellipsis={{ tooltip: task.message }} style={{ display: "block", fontSize: 12, marginTop: 3 }}>
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
        const historyUnsupported = syncMode === "backfill" && (!historyCapabilitiesReady || !capabilities?.[row.dataset]?.modes.includes("backfill"));
        const scopeUnsupported = row.dataset === "tail_proxy" && source === "all";
        let disabledReason: string | null = null;
        if (scopeUnsupported) disabledReason = "尾盘分钟同步仅支持观察池或持仓范围，请缩小标的范围";
        else if (historyUnsupported && !historyCapabilitiesReady) disabledReason = "历史同步能力尚未确认，请等待加载或重试读取同步能力";
        else if (historyUnsupported) disabledReason = row.dataset === "etf"
          ? "ETF 历史接口目前只稳定提供净值，溢折价、规模、份额等指标没有统一可靠的历史序列；当前同步按最近快照执行"
          : "当前数据源仅支持最近快照，请将同步模式切换为“最近快照”";
        else if (taskForDataset(row.dataset)) disabledReason = "该数据集正在同步，请等待当前任务完成";
        return (
          <Space direction="vertical" size={6}>
            <Tooltip title={disabledReason || undefined}>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <Button
                  type="primary"
                  icon={<SyncOutlined spin={startingDataset === row.dataset || Boolean(taskForDataset(row.dataset))} />}
                  loading={startingDataset === row.dataset}
                  disabled={controlsDisabled || Boolean(taskForDataset(row.dataset)) || historyUnsupported || scopeUnsupported}
                  onClick={() => void runSync(row.dataset)}
                >
                  {taskForDataset(row.dataset) ? t("extSyncing") : configs[row.dataset].title}
                </Button>
                {historyUnsupported || scopeUnsupported ? (
                  <Tooltip title={disabledReason}>
                    <QuestionCircleOutlined
                      aria-label="查看不可同步原因"
                      style={{ color: "#98a2b3", cursor: "help", fontSize: 15 }}
                    />
                  </Tooltip>
                ) : null}
              </span>
            </Tooltip>
            {repairable ? (
              <Button
                size="small"
                loading={repairingDataset === row.dataset}
                disabled={controlsDisabled || !overview || Boolean(taskForDataset(row.dataset)) || ((!dateRange[0] || !dateRange[1]) && (coverageLoading || Boolean(coverageError) || !coverage))}
                onClick={() => void repairDetectedGaps(row.dataset as "fundamental" | "financial" | "capital_flow")}
              >
                检查并修复缺口
              </Button>
            ) : null}
            {row.dataset === "fundamental" ? (
              <Tooltip title={t("extFullValuationHistoryHint")}>
                <Button
                  size="small"
                  disabled={controlsDisabled || !historyCapabilitiesReady || Boolean(taskForDataset("fundamental"))}
                  onClick={() => void runSync("fundamental", {
                    source: "all",
                    mode: "backfill",
                    start_date: FULL_HISTORY_START_DATE,
                    end_date: dayjs().format("YYYY-MM-DD"),
                    lookback_days: 1,
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
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
          border: "1px solid var(--border-color, #eaecf0)",
          borderRadius: 6,
          marginBottom: 18,
          overflow: "hidden",
        }}
      >
        {[
          [t("extDashboardRecords"), overview?.total_records ?? "-"],
          [t("extDashboardCoverage"), overview?.covered_symbols ?? "-"],
          [t("extDashboardAvailable"), overview ? `${overview.available_datasets}/${DATASET_ORDER.length}` : "-"],
          [t("extDashboardRunning"), overview?.running_tasks ?? "-"],
        ].map(([label, value], index) => (
          <div key={String(label)} style={{ padding: "14px 18px", borderRight: index < 3 ? "1px solid var(--border-color, #eaecf0)" : undefined }}>
            <Statistic title={label} value={value} valueStyle={{ fontSize: 22 }} />
          </div>
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
                <Typography.Text type="secondary">{taskRange(task)}</Typography.Text>
                {running ? <Button size="small" danger loading={taskActionLoading === "cancel"} disabled={task.cancel_requested} onClick={() => void cancelActiveTask(task)}>{task.cancel_requested ? "正在取消" : "取消同步"}</Button> : null}
                {!running && taskCanResume(task) ? <Button size="small" type="primary" loading={taskActionLoading === "retry"} onClick={() => void resumeTaskFromCursor(task)}>从断点继续</Button> : null}
              </Space>
              <Typography.Text type="secondary">{taskProgressText(task)}</Typography.Text>
            </div>
            <Progress
              percent={task.stage === "mirror" && running ? undefined : Math.round(task.percent)}
              status={task.status === "failed" || taskAllAttemptsFailed(task) ? "exception" : task.status === "done" ? "success" : "active"}
              format={task.stage === "mirror" && running ? () => "镜像处理中" : undefined}
            />
            <div style={{ display: "flex", gap: 18, flexWrap: "wrap", color: "#475467", fontSize: 12, marginTop: 6 }}>
              <span>{stageLabel(task.stage)}</span><span>同步数量：{taskProgressText(task)}</span>
              <span>{t("extDashboardSuccess")}：{task.ok_count ?? 0}</span><span>{t("extDashboardFailed")}：{task.failed_count ?? 0}</span>
              {task.current_item && <span>{t("extDashboardCurrent")}：{task.current_item}</span>}
            </div>
          </div>
        );
      })}

      <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap", marginBottom: 14 }}>
        <Space>
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
            {syncScopeDescription(source, primaryWatchlistName, app?.portfolioId)}
          </Typography.Text>
        </Space>
        <Checkbox checked={includeNorthbound && northboundReady} disabled={controlsDisabled || !northboundReady} onChange={(event) => setIncludeNorthbound(event.target.checked)}>
          {t("extIncludeNorthbound")}
        </Checkbox>
        {!northboundReady && <Typography.Text type="warning">{capabilitiesLoading ? "北向资金能力正在确认，仅同步个股资金流" : northboundCapability?.available === false ? `北向资金不可用：${northboundCapability.reason}` : "北向资金能力未知，仅同步个股资金流；请重试读取同步能力"}</Typography.Text>}
        <Space>
          <Typography.Text strong>{t("extSyncModeLabel")}:</Typography.Text>
          <Select
            value={syncMode}
            onChange={setSyncMode}
            disabled={controlsDisabled}
            style={{ width: 130 }}
            options={[
              { value: "incremental", label: "最近快照" },
              { value: "backfill", label: "真实历史范围" },
            ]}
          />
          {syncMode === "backfill" ? (
            <>
              <Select
                value={lookbackDays}
                onChange={setLookbackDays}
                disabled={controlsDisabled}
                style={{ width: 110 }}
                options={[30, 60, 100].map((value) => ({ value, label: `${value} 日` }))}
              />
              <DatePicker.RangePicker
                value={dateRange}
                disabled={controlsDisabled}
                onChange={(value) => setDateRange(value || [null, null])}
                allowClear
                placeholder={["开始日期", "结束日期"]}
              />
            </>
          ) : null}
        </Space>
        <Tooltip title="仅同步外部数据不会自动刷新评分特征；此操作会计算输入，在真实模型已激活且数据齐全时生成模型评分，并保留任务证据。默认重算最近约 11 天；未激活模型或输入不足时会明确说明未生成或仅部分生成评分。">
          <Button
            aria-label="刷新评分特征"
            type="primary"
            ghost
            icon={<SyncOutlined />}
            loading={pipelineStarting}
            disabled={controlsDisabled || taskRunning || Boolean(pipelineTask && ["queued", "running"].includes(pipelineTask.status))}
            onClick={() => void refreshFactorScores()}
          >
            刷新评分特征
          </Button>
        </Tooltip>
      </div>

      {coverageError && <Alert type="error" showIcon message="因子覆盖读取失败" description={coverageError} action={<Button size="small" loading={coverageLoading} onClick={() => void refreshCoverage()}>重试覆盖读取</Button>} style={{ marginBottom: 14 }} />}
      {capabilitiesError && <Alert type="warning" showIcon message="同步能力读取失败" description={capabilitiesError} action={<Button size="small" loading={capabilitiesLoading} onClick={() => void refreshCapabilities()}>重试同步能力</Button>} style={{ marginBottom: 14 }} />}
      {pipelineTask && pipelineOutcome && <Alert
        type={pipelineOutcome.type}
        showIcon
        style={{ marginBottom: 14 }}
        message={`评分特征：${pipelineOutcome.label}${pipelineTask.stage ? ` · ${scoringStageLabel(pipelineTask.stage)}` : ""}${typeof pipelineTask.percent === "number" ? ` · ${Math.round(pipelineTask.percent)}%` : ""}`}
        description={<Space direction="vertical" size={4}>
          {pipelineOutcome.detail && <span>{pipelineOutcome.detail}</span>}
          {pipelineTask.message && <span>{pipelineTask.message}</span>}
          {pipelinePollError && <Typography.Text type="danger">任务状态读取失败：{pipelinePollError}；正在重试</Typography.Text>}
          {(pipelineTask.result?.completion_status === "AWAITING_MODEL_ACTIVATION" || (pipelineTask.result?.scoring_bridge as { status?: string } | undefined)?.status === "SKIPPED") && <Button size="small" onClick={() => window.dispatchEvent(new CustomEvent("settings:navigate", { detail: "factor-model" }))}>去因子模型</Button>}
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
          message="历史范围只使用数据源真实返回的记录；不支持历史回填的数据集会保持不可执行。"
        />
      ) : null}

      {coverageActions.length > 0 ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 14 }}
          message={t("extCoverageActionTitle")}
          description={(
            <div>
              <div style={{ marginBottom: 8 }}>{t("extCoverageActionDescription")}</div>
              <Space wrap>
                {coverageActions.map((item) => (
                  <Tag key={item.dataset} color={item.readiness === "blocked" ? "error" : "warning"}>
                    {configs[item.dataset].title}：{datasetCoverageReason(item)}
                  </Tag>
                ))}
                <Button size="small" onClick={() => window.dispatchEvent(new CustomEvent("settings:navigate", { detail: "factor-model" }))}>
                  {t("extCoverageActionModel")}
                </Button>
                <Button size="small" onClick={() => window.dispatchEvent(new CustomEvent("settings:navigate", { detail: "api-management" }))}>
                  {t("extCoverageActionApi")}
                </Button>
              </Space>
            </div>
          )}
        />
      ) : null}

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 14 }}
        message="尾盘代理补充导入（自动同步优先）"
        description={<Space wrap>
          <span>二期将由系统按候选池自动采集最近真实交易日；当前 CSV 仅作为临时验收或数据源不可用时的补充入口。文件必须包含 symbol,timestamp,open,high,low,close,volume,amount；每个交易日需至少 180 根分钟线、20 根尾盘线且最后时间不早于 14:59。</span>
          <Upload
            accept=".csv,text/csv"
            showUploadList={false}
            disabled={tailImporting || controlsDisabled || !overview}
            beforeUpload={(file) => { void importTailMinutes(file); return false; }}
          >
            <Button size="small" loading={tailImporting} disabled={tailImporting || controlsDisabled || !overview}>导入 CSV</Button>
          </Upload>
        </Space>}
      />

      {tailImportResult && <Alert
        type={tailImportWarning ? "warning" : "success"}
        showIcon
        style={{ marginBottom: 14 }}
        message={tailImportResult.warehouse_mirror?.status === "warning" ? "原始分钟输入已保存，因子仓库镜像失败" : tailImportResult.quality_snapshot?.status === "warning" ? "原始输入与因子仓库已写入，质量诊断刷新失败" : `尾盘分钟输入：${tailImportResult.written_sessions} 个有效交易日，拒绝 ${tailImportResult.rejected_sessions} 个交易日`}
        description={<Space direction="vertical" size={4}>
          {Boolean(tailImportResult.invalid_rows) && <span>无效行：{tailImportResult.invalid_rows}；未写入</span>}
          {tailImportResult.warehouse_mirror && <span>因子仓库镜像：{tailImportResult.warehouse_mirror.status === "done" ? `已写入 ${formatCount(tailImportResult.warehouse_mirror.records)} 条记录` : tailImportResult.warehouse_mirror.status === "warning" ? `失败：${tailImportResult.warehouse_mirror.error || "请重新导入重试"}` : "未执行"}</span>}
          {tailImportResult.quality_snapshot && <span>质量快照：{tailImportResult.quality_snapshot.status === "done" ? "已刷新" : tailImportResult.quality_snapshot.status === "warning" ? `失败：${tailImportResult.quality_snapshot.error || "请刷新诊断重试"}` : "未刷新"}</span>}
          {tailImportResult.warnings?.map((warning, index) => <span key={`${index}-${warning}`}>{warning}</span>)}
        </Space>}
      />}

      {previewPlan ? (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 14 }}
          message={`${previewPlan.dataset}: ${previewPlan.requested_start_date} ~ ${previewPlan.requested_end_date} (${previewPlan.requested_span_days} 日)`}
          description={previewPlan.provider_history_limit_days == null
            ? previewPlan.provider_reason
            : `${previewPlan.provider_reason} 历史上限: ${previewPlan.provider_history_limit_days} 日`}
        />
      ) : null}

      <Table<ExternalDatasetOverview>
        rowKey="dataset"
        columns={columns}
        dataSource={rows}
        loading={overviewLoading && !overview}
        pagination={false}
        size="middle"
        scroll={{ x: 1050 }}
      />

      {resultTask?.result && (
        <Alert
          style={{ marginTop: 16 }}
          type={taskHasWarnings(resultTask) ? "warning" : "success"}
          showIcon
          message={`${configs[resultTask.result.dataset].title}: ${template("extSyncResult", syncResultParams(resultTask.result))}`}
          description={
            <Space direction="vertical" style={{ width: "100%" }}>
              {resultTask.result.warehouse_mirror && <Typography.Text type={resultTask.result.warehouse_mirror.status === "warning" ? "warning" : "secondary"}>
                因子仓库镜像：{resultTask.result.warehouse_mirror.status === "warning" ? `失败：${resultTask.result.warehouse_mirror.error || "请从断点重试"}` : resultTask.result.warehouse_mirror.status === "skipped" ? "未执行" : `已写入 ${formatCount(resultTask.result.warehouse_mirror.records)} 条记录`}
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
    </section>
  );
}
