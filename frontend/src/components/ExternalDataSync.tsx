import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import {
  api,
  type ExternalDataOverview,
  type ExternalDatasetOverview,
  type ExternalDataSyncCapabilities,
  type ExternalDataSyncPlan,
  type ExternalDataCoverage,
  type ExternalSyncPlanDetails,
  type ExternalSyncDataset,
  type ExternalSyncTask,
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

function syncResultParams(result: ExternalSyncTask["result"]): Record<string, string | number> {
  if (!result) return {};
  return {
    total: result.total,
    success: result.success,
    skipped: result.skipped,
    failed: result.failed,
  };
}

function statusTag(task: ExternalSyncTask | null) {
  if (!task) return <Tag>{t("extStatusNever")}</Tag>;
  const interrupted = task.status === "failed" && task.stage === "interrupted";
  const config = {
    queued: { color: "default", key: "extStatusQueued" },
    running: { color: "processing", key: "extStatusRunning" },
    done: { color: task.failed_count > 0 ? "warning" : "success", key: task.failed_count > 0 ? "extStatusPartial" : "extStatusDone" },
    failed: { color: interrupted ? "warning" : "error", key: interrupted ? "extStatusInterrupted" : "extStatusFailed" },
    cancelled: { color: "default", key: "extStatusCancelled" },
  }[task.status];
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

export default function ExternalDataSync() {
  const [source, setSource] = useState<SyncSource>("watchlist");
  const [includeNorthbound, setIncludeNorthbound] = useState(true);
  const [syncMode, setSyncMode] = useState<"incremental" | "backfill">("incremental");
  const [lookbackDays, setLookbackDays] = useState(60);
  const [dateRange, setDateRange] = useState<[Dayjs | null, Dayjs | null]>([null, null]);
  const [capabilities, setCapabilities] = useState<ExternalDataSyncCapabilities | null>(null);
  const [coverage, setCoverage] = useState<ExternalDataCoverage | null>(null);
  const [previewPlan, setPreviewPlan] = useState<ExternalDataSyncPlan | null>(null);
  const [overview, setOverview] = useState<ExternalDataOverview | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [startingDataset, setStartingDataset] = useState<ExternalSyncDataset | null>(null);
  const [activeTask, setActiveTask] = useState<ExternalSyncTask | null>(null);
  const [finishedTask, setFinishedTask] = useState<ExternalSyncTask | null>(null);
  const [taskActionLoading, setTaskActionLoading] = useState<"cancel" | "retry" | null>(null);
  const [partitionDetails, setPartitionDetails] = useState<ExternalSyncPlanDetails | null>(null);
  const [repairingDataset, setRepairingDataset] = useState<"fundamental" | "financial" | "capital_flow" | null>(null);
  const [tailImporting, setTailImporting] = useState(false);
  const handledTaskId = useRef<string | null>(null);

  const configs = {
      fundamental: { title: t("extSyncFundamental"), desc: t("extSyncFundamentalDesc"), icon: <FundOutlined /> },
      financial: { title: t("extSyncFinancial"), desc: t("extSyncFinancialDesc"), icon: <FileTextOutlined /> },
      lhb: { title: t("extSyncLhb"), desc: t("extSyncLhbDesc"), icon: <BankOutlined /> },
      hot_rank: { title: t("extSyncHotRank"), desc: t("extSyncHotRankDesc"), icon: <FireOutlined /> },
      tail_proxy: { title: t("extSyncTailProxy"), desc: t("extSyncTailProxyDesc"), icon: <ClockCircleOutlined /> },
      capital_flow: { title: t("extSyncCapitalFlow"), desc: t("extSyncCapitalFlowDesc"), icon: <RiseOutlined /> },
      etf: { title: t("extSyncEtf"), desc: t("extSyncEtfDesc"), icon: <DatabaseOutlined /> },
  };

  const refreshOverview = useCallback(async (silent = false) => {
    if (!silent) setOverviewLoading(true);
    try {
      const next = await api.getExternalDataOverview();
      setOverview(next);
      setOverviewError(null);
      void api.getExternalDataCoverage().then(setCoverage).catch(() => setCoverage(null));
      const running = next.datasets
        .map((item) => item.latest_task)
        .find((task): task is ExternalSyncTask => Boolean(task && (task.status === "queued" || task.status === "running")));
      if (running) {
        setActiveTask((current) =>
          current && (current.status === "queued" || current.status === "running") ? current : running,
        );
      }
    } catch (error: any) {
      const errorMessage = error?.message || String(error);
      setOverviewError(errorMessage);
      if (!silent) message.error(template("extOverviewFailed", { message: errorMessage }));
    } finally {
      if (!silent) setOverviewLoading(false);
    }
  }, []);

  const finishTask = useCallback(async (task: ExternalSyncTask) => {
    if (handledTaskId.current === task.id) return;
    handledTaskId.current = task.id;
    setFinishedTask(task);
    if (task.status === "done" && task.failed_count === 0) {
      message.success(t("extTaskCompleted"));
    } else if (task.status === "done") {
      message.warning(t("extTaskCompletedWithErrors"));
    } else {
      message.error(task.message || t("extStatusFailed"));
    }
    await refreshOverview(true);
  }, [refreshOverview]);

  useEffect(() => {
    void refreshOverview();
  }, [refreshOverview]);

  useEffect(() => {
    void api.getExternalDataSyncCapabilities().then(setCapabilities).catch(() => setCapabilities(null));
  }, []);

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
    if (!activeTask || !["queued", "running"].includes(activeTask.status)) return;
    let disposed = false;
    const poll = async () => {
      try {
        const next = await api.getExternalDataSyncTask(activeTask.id);
        if (disposed) return;
        setActiveTask(next);
        if (!["queued", "running"].includes(next.status)) await finishTask(next);
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
  }, [activeTask?.id, activeTask?.status, finishTask]);

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
    if (!overview) return;
    setStartingDataset(dataset);
    setFinishedTask(null);
    handledTaskId.current = null;
    try {
      const syncPayload = {
        dataset,
        source,
        include_northbound: includeNorthbound,
        mode: syncMode,
        start_date: syncMode === "backfill" && dateRange[0] ? dateRange[0].format("YYYY-MM-DD") : undefined,
        end_date: syncMode === "backfill" && dateRange[1] ? dateRange[1].format("YYYY-MM-DD") : undefined,
      lookback_days: syncMode === "backfill" ? lookbackDays : 1,
      limit: 20,
        ...(syncMode === "backfill" && dataset === "fundamental" ? { max_workers: 4 } : {}),
      ...overrides,
      };
      const plan = await api.previewExternalDataSyncPlan(syncPayload);
      setPreviewPlan(plan);
      const task = await api.startExternalDataSync(syncPayload);
      setActiveTask(task);
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

  const cancelActiveTask = async () => {
    if (!activeTask) return;
    setTaskActionLoading("cancel");
    try {
      const task = await api.cancelExternalDataSyncTask(activeTask.id);
      setActiveTask(task);
      await finishTask(task);
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
      handledTaskId.current = null;
      setFinishedTask(null);
      setActiveTask(resumedTask);
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

  const repairDetectedGaps = async (dataset: "fundamental" | "financial" | "capital_flow") => {
    if (!overview) return;
    setRepairingDataset(dataset);
    setFinishedTask(null);
    handledTaskId.current = null;
    const end = dateRange[1] || dayjs();
    const start = dateRange[0] || end.subtract(59, "day");
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
      message.success(`已创建 ${report.gaps.length}${report.truncated ? "+" : ""} 个缺口的修复任务`);
      await refreshOverview(true);
    } catch (error: any) {
      message.error(template("extSyncFailed", { message: error?.message || String(error) }));
    } finally {
      setRepairingDataset(null);
    }
  };

  const importTailMinutes = async (file: File) => {
    if (!overview) return;
    setTailImporting(true);
    try {
      const result = await api.importTailProxyMinutes({
        csv_text: await file.text(), filename: file.name,
      });
      message.success(`尾盘分钟导入完成：${result.written_sessions} 个有效交易日`);
      if (result.rejected_sessions > 0) {
        message.warning(`${result.rejected_sessions} 个交易日未满足完整尾盘会话校验`);
      }
      await refreshOverview(true);
    } catch (error: any) {
      message.error(template("extSyncFailed", { message: error?.message || String(error) }));
    } finally {
      setTailImporting(false);
    }
  };

  const activeDataset = activeTask ? datasetFromTask(activeTask) : null;
  const taskRunning = Boolean(activeTask && ["queued", "running"].includes(activeTask.status));
  const anyRunning = taskRunning || Boolean(startingDataset) || Boolean(overview?.running_tasks);
  const dashboardUnavailable = !overview && overviewError !== null;
  const controlsDisabled = anyRunning || dashboardUnavailable;
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
            {formatDate(row.last_updated_at, true)}
          </div>
        </div>
      ),
    },
    {
      title: "字段覆盖",
      width: 230,
      render: (_, row) => {
        const diagnostic = coverageByDataset.get(row.dataset);
        if (!diagnostic) return <Typography.Text type="secondary">读取中</Typography.Text>;
        const field = diagnostic.fields[0];
        const color = {
          available: "success",
          limited: "warning",
          event: "processing",
          snapshot: "purple",
          blocked: "error",
          unknown: "default",
          not_applicable: "default",
        }[diagnostic.readiness] as "success" | "warning" | "processing" | "purple" | "error" | "default";
        if (!field) return <Tag color={color}>不适用于因子字段</Tag>;
        const nonnullRate = field.table_rows > 0 ? `${((field.nonnull_rows / field.table_rows) * 100).toFixed(1)}%` : "-";
        const dailyCoverage = field.latest_daily_coverage == null ? "-" : `${(field.latest_daily_coverage * 100).toFixed(1)}%`;
        return (
          <div>
            <Tag color={color}>{readinessLabel(diagnostic.readiness)}</Tag>
            <Typography.Text style={{ fontSize: 12 }}>{field.first_date || "-"} ~ {field.latest_date || "-"}</Typography.Text>
            <div style={{ color: "var(--text-muted, #667085)", fontSize: 12, marginTop: 3 }}>
              非空 {nonnullRate} · 日覆盖 {dailyCoverage} · 连续 {field.continuity_days} 日
            </div>
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
                  {task.processed}/{task.total} · {t("extDashboardSuccess")} {task.ok_count} · {t("extDashboardFailed")} {task.failed_count}
                </Typography.Text>
                <div style={{ color: "var(--text-muted, #667085)", fontSize: 12, marginTop: 3 }}>
                  {formatDate(task.finished_at || task.updated_at, true)}
                </div>
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
        const resumableTask = row.latest_task?.status === "failed" && Boolean(row.latest_task.batch_recovery);
        return (
          <Space direction="vertical" size={6}>
            <Button
              type="primary"
              icon={<SyncOutlined spin={startingDataset === row.dataset || (taskRunning && activeDataset === row.dataset)} />}
              loading={startingDataset === row.dataset}
              disabled={controlsDisabled || (syncMode === "backfill" && Boolean(capabilities && !capabilities[row.dataset]?.modes.includes("backfill")))}
              onClick={() => void runSync(row.dataset)}
            >
              {taskRunning && activeDataset === row.dataset ? t("extSyncing") : configs[row.dataset].title}
            </Button>
            {repairable ? (
              <Button
                size="small"
                loading={repairingDataset === row.dataset}
                disabled={controlsDisabled}
                onClick={() => void repairDetectedGaps(row.dataset as "fundamental" | "financial" | "capital_flow")}
              >
                检查并修复缺口
              </Button>
            ) : null}
            {row.dataset === "fundamental" ? (
              <Tooltip title={t("extFullValuationHistoryHint")}>
                <Button
                  size="small"
                  disabled={controlsDisabled}
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

      {activeTask && (
        <div style={{ border: "1px solid #91caff", background: "#f0f7ff", borderRadius: 6, padding: "14px 16px", marginBottom: 18 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", marginBottom: 8 }}>
            <Space>
              <Typography.Text strong>{activeDataset ? configs[activeDataset].title : t("extSectionTitle")}</Typography.Text>
              {statusTag(activeTask)}
              {taskRunning ? (
                <Button size="small" danger loading={taskActionLoading === "cancel"} onClick={() => void cancelActiveTask()}>
                  取消同步
                </Button>
              ) : ["failed", "cancelled"].includes(activeTask.status) ? (
                <Button size="small" type="primary" loading={taskActionLoading === "retry"} onClick={() => void retryTaskFromCursor()}>
                  从断点继续
                </Button>
              ) : null}
            </Space>
            <Typography.Text type="secondary">
              {activeTask.processed}/{activeTask.total || "-"}
            </Typography.Text>
          </div>
          <Progress
            percent={Math.round(activeTask.percent)}
            status={activeTask.status === "failed" ? "exception" : activeTask.status === "done" ? "success" : "active"}
          />
          <div style={{ display: "flex", gap: 18, flexWrap: "wrap", color: "#475467", fontSize: 12, marginTop: 6 }}>
            <span>{stageLabel(activeTask.stage)}</span>
            <span>{t("extDashboardSuccess")}：{activeTask.ok_count}</span>
            <span>{t("extDashboardFailed")}：{activeTask.failed_count}</span>
            {activeTask.current_item && <span>{t("extDashboardCurrent")}：{activeTask.current_item}</span>}
          </div>
          {partitionDetails?.plan && (
            <div style={{ marginTop: 8, color: "#475467", fontSize: 12 }}>
              <span>
                分片 {partitionDetails.plan.completed_partitions + partitionDetails.plan.skipped_partitions + partitionDetails.plan.failed_partitions}/{partitionDetails.plan.total_partitions}
                {partitionDetails.plan.failed_partitions > 0 ? ` · 失败 ${partitionDetails.plan.failed_partitions}` : ""}
              </span>
              {partitionDetails.partitions.some((partition) => partition.status === "failed") ? (
                <Collapse
                  size="small"
                  ghost
                  style={{ marginTop: 4 }}
                  items={[{
                    key: "failed-partitions",
                    label: "查看失败分片",
                    children: (
                      <ul style={{ margin: 0, paddingLeft: 20 }}>
                        {partitionDetails.partitions
                          .filter((partition) => partition.status === "failed")
                          .map((partition) => (
                            <li key={partition.id}>
                              {partition.symbol || partition.partition_key}: {partition.error_message || "同步失败"}
                            </li>
                          ))}
                      </ul>
                    ),
                  }]}
                />
              ) : null}
            </div>
          )}
        </div>
      )}

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
        </Space>
        <Checkbox checked={includeNorthbound} disabled={controlsDisabled} onChange={(event) => setIncludeNorthbound(event.target.checked)}>
          {t("extIncludeNorthbound")}
        </Checkbox>
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
      </div>

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

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 14 }}
        message="尾盘分钟文件导入"
        description={<Space wrap>
          <span>CSV 必须包含 symbol,timestamp,open,high,low,close,volume,amount；每个交易日需至少 180 根分钟线、20 根尾盘线且最后时间不早于 14:59。</span>
          <Upload
            accept=".csv,text/csv"
            showUploadList={false}
            disabled={tailImporting || controlsDisabled}
            beforeUpload={(file) => { void importTailMinutes(file); return false; }}
          >
            <Button size="small" loading={tailImporting}>导入 CSV</Button>
          </Upload>
        </Space>}
      />

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
          type={resultTask.result.failed === 0 ? "success" : "warning"}
          showIcon
          message={`${configs[resultTask.result.dataset].title}: ${template("extSyncResult", syncResultParams(resultTask.result))}`}
          description={
            resultTask.result.errors.length > 0 ? (
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
            ) : null
          }
        />
      )}
    </section>
  );
}
