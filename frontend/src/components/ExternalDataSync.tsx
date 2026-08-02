import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  Progress,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
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
import { t, template } from "../i18n";
import {
  api,
  type ExternalDataOverview,
  type ExternalDatasetOverview,
  type ExternalSyncDataset,
  type ExternalSyncTask,
} from "../api/client";

type SyncSource = "watchlist" | "positions" | "all";

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
  const config = {
    queued: { color: "default", key: "extStatusQueued" },
    running: { color: "processing", key: "extStatusRunning" },
    done: { color: task.failed_count > 0 ? "warning" : "success", key: task.failed_count > 0 ? "extStatusPartial" : "extStatusDone" },
    failed: { color: "error", key: "extStatusFailed" },
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
    done: "extStageDone",
    failed: "extStageFailed",
    cancelled: "extStageCancelled",
  }[stage];
  return key ? t(key) : stage;
}

export default function ExternalDataSync() {
  const [source, setSource] = useState<SyncSource>("watchlist");
  const [includeNorthbound, setIncludeNorthbound] = useState(true);
  const [overview, setOverview] = useState<ExternalDataOverview | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [startingDataset, setStartingDataset] = useState<ExternalSyncDataset | null>(null);
  const [activeTask, setActiveTask] = useState<ExternalSyncTask | null>(null);
  const [finishedTask, setFinishedTask] = useState<ExternalSyncTask | null>(null);
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
      const running = next.datasets
        .map((item) => item.latest_task)
        .find((task): task is ExternalSyncTask => Boolean(task && (task.status === "queued" || task.status === "running")));
      if (running) {
        setActiveTask((current) =>
          current && (current.status === "queued" || current.status === "running") ? current : running,
        );
      }
    } catch (error: any) {
      if (!silent) message.error(template("extOverviewFailed", { message: error?.message || String(error) }));
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

  const runSync = async (dataset: ExternalSyncDataset) => {
    setStartingDataset(dataset);
    setFinishedTask(null);
    handledTaskId.current = null;
    try {
      const task = await api.startExternalDataSync({
        dataset,
        source,
        include_northbound: includeNorthbound,
        lookback_days: 30,
        limit: 20,
      });
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

  const activeDataset = activeTask ? datasetFromTask(activeTask) : null;
  const taskRunning = Boolean(activeTask && ["queued", "running"].includes(activeTask.status));
  const anyRunning = taskRunning || Boolean(startingDataset) || Boolean(overview?.running_tasks);
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
      width: 155,
      fixed: "right",
      render: (_, row) => (
        <Button
          type="primary"
          icon={<SyncOutlined spin={startingDataset === row.dataset || (taskRunning && activeDataset === row.dataset)} />}
          loading={startingDataset === row.dataset}
          disabled={anyRunning}
          onClick={() => void runSync(row.dataset)}
        >
          {taskRunning && activeDataset === row.dataset ? t("extSyncing") : configs[row.dataset].title}
        </Button>
      ),
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
          [t("extDashboardRecords"), overview?.total_records || 0],
          [t("extDashboardCoverage"), overview?.covered_symbols || 0],
          [t("extDashboardAvailable"), `${overview?.available_datasets || 0}/${DATASET_ORDER.length}`],
          [t("extDashboardRunning"), overview?.running_tasks || 0],
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
        </div>
      )}

      <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap", marginBottom: 14 }}>
        <Space>
          <Typography.Text strong>{t("extSourceLabel")}:</Typography.Text>
          <Select
            value={source}
            onChange={(value) => setSource(value)}
            style={{ width: 150 }}
            disabled={anyRunning}
            options={[
              { value: "watchlist", label: t("extSourceWatchlist") },
              { value: "positions", label: t("extSourcePositions") },
              { value: "all", label: t("extSourceAll") },
            ]}
          />
        </Space>
        <Checkbox checked={includeNorthbound} disabled={anyRunning} onChange={(event) => setIncludeNorthbound(event.target.checked)}>
          {t("extIncludeNorthbound")}
        </Checkbox>
      </div>

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
