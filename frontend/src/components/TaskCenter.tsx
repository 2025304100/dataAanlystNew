import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Collapse, Empty, Modal, Progress, Select, Space, Spin, Tag, Timeline, Typography } from "antd";
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  ExceptionOutlined,
  LoadingOutlined,
  PauseCircleOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import { enumLabel, t } from "../i18n";
import type { UnifiedTask } from "../types";
// WPD-05：任务错误消息显示工具（优先 error_code → i18n，兜底乱码检测）
import { getTaskErrorMessage } from "../utils/taskErrorDisplay";
// WP-AI.7：让 AI 解释按钮
import ExplainButton from "./ai/ExplainButton";

const STATUS_COLORS: Record<string, string> = {
  done: "#0f766e",
  failed: "#b42318",
  cancelled: "#94a3b8",
  expired: "#94a3b8",
  running: "#1570ef",
  queued: "#d97706",
  paused: "#d97706",
};

const STATUS_ICONS: Record<string, React.ReactNode> = {
  done: <CheckCircleOutlined />,
  failed: <CloseCircleOutlined />,
  cancelled: <CloseCircleOutlined />,
  expired: <ExceptionOutlined />,
  running: <LoadingOutlined />,
  queued: <ClockCircleOutlined />,
  paused: <PauseCircleOutlined />,
};

const TASK_TYPE_LABELS: Record<string, string> = {
  market_data_sync: "taskTypeSync",
  history_initialization: "taskTypeHistory",
  discovery_mining: "taskTypeDiscovery",
  universe_incremental_sync: "taskTypeUniverseIncremental",
  macro_update: "taskTypeMacro",
  factor_pipeline: "taskTypeFactorPipeline",
};

function isExternalSyncTask(taskType: string): boolean {
  return taskType.startsWith("external_sync_");
}

function formatDuration(sec?: number): string {
  if (sec == null || sec < 0) return "-";
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function formatTime(iso?: string): string {
  if (!iso) return "-";
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

export default function TaskCenter() {
  const { showToast } = useApp();
  const [tasks, setTasks] = useState<UnifiedTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<string>("all");
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [abortingId, setAbortingId] = useState<string | null>(null);
  const [observability, setObservability] = useState<{
    domains: Record<string, { slot_limit: number; running: number; queued: number; waiting: number; available_slots: number }>;
  } | null>(null);
  const [batchDetail, setBatchDetail] = useState<any | null>(null);
  const [batchLoadingId, setBatchLoadingId] = useState<string | null>(null);

  const loadTasks = useCallback(async () => {
    setLoading(true);
    try {
      const [data, observation] = await Promise.all([
        api.getTaskHistory(undefined, 50),
        api.getTaskObservability(),
      ]);
      setTasks(data.tasks);
      setObservability(observation);
    } catch (error: unknown) {
      // 收窄 unknown 类型，安全提取错误消息
      const msg = error instanceof Error ? error.message : "";
      showToast("error", msg || t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    loadTasks();
  }, [loadTasks]);

  // 判断任务是否可中止:运行中/排队中/已暂停(discovery)均可中止
  const canAbort = useCallback((task: UnifiedTask): boolean => {
    if (["running", "queued"].includes(task.status)) return true;
    // discovery 任务的 paused 状态也允许中止(等同于 cancel)
    if (task.source === "discovery" && task.status === "paused") return true;
    return false;
  }, []);

  const handleAbort = useCallback(
    (task: UnifiedTask) => {
      Modal.confirm({
        title: t("taskAbort"),
        content: t("taskAbortConfirm"),
        okText: t("taskAbort"),
        okButtonProps: { danger: true },
        cancelText: t("cancel"),
        onOk: async () => {
          setAbortingId(task.id);
          try {
            if (task.source === "async") {
              if (task.task_type === "market_data_sync") {
                await api.cancelMarketDataSyncTask(task.id);
              } else if (task.task_type === "history_initialization") {
                await api.cancelHistoryInitialization();
              } else if (task.task_type === "universe_incremental_sync") {
                await api.cancelUniverseIncrementalSync();
              } else if (task.task_type === "macro_update") {
                await api.cancelMacroUpdateTask(task.id);
              } else if (task.task_type === "factor_pipeline") {
                await api.cancelFactorPipelineTask(task.id);
              } else if (isExternalSyncTask(task.task_type)) {
                await api.cancelExternalDataSyncTask(task.id);
              } else {
                throw new Error(`Unsupported async task type: ${task.task_type}`);
              }
            } else if (task.source === "discovery") {
              // discovery task id 是 uuid hex 字符串,client 签名历史遗留为 number,运行时拼接到 URL 无影响
              await api.sendDiscoveryCommand(task.id as unknown as number, "cancel");
            } else {
              throw new Error(`Unsupported task source: ${task.source}`);
            }
            showToast("success", t("taskAbortSuccess"));
            await loadTasks();
          } catch (error: unknown) {
            const msg = error instanceof Error ? error.message : "";
            showToast("error", msg || t("taskAbortFailed"));
          } finally {
            setAbortingId(null);
          }
        },
      });
    },
    [showToast, loadTasks]
  );

  const openBatchDetail = useCallback(async (taskId: string) => {
    setBatchLoadingId(taskId);
    try {
      setBatchDetail(await api.getTaskBatch(taskId));
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : "";
      showToast("error", msg || t("loadFailed"));
    } finally {
      setBatchLoadingId(null);
    }
  }, [showToast]);

  const filteredTasks = useMemo(() => {
    if (filter === "all") return tasks;
    if (filter === "active") return tasks.filter((task) => ["running", "queued"].includes(task.status));
    if (filter === "failed") return tasks.filter((task) => task.status === "failed");
    if (filter === "external") return tasks.filter((task) => isExternalSyncTask(task.task_type));
    return tasks.filter((task) => task.task_type === filter);
  }, [tasks, filter]);

  const activeCount = tasks.filter((task) => ["running", "queued"].includes(task.status)).length;

  const taskTypeLabel = (taskType: string): string => {
    if (isExternalSyncTask(taskType)) return `外部数据 · ${taskType.slice("external_sync_".length)}`;
    const key = TASK_TYPE_LABELS[taskType];
    return key ? t(key) : taskType;
  };

  const statusTag = (status: string) => {
    const color = STATUS_COLORS[status] || "#94a3b8";
    const icon = STATUS_ICONS[status] || null;
    return (
      <Tag color={color} icon={icon}>
        {t(`taskStatus_${status}`) || status}
      </Tag>
    );
  };

  const renderTaskDetail = (task: UnifiedTask) => {
    const progressPct = Math.round(task.percent);
    const hasErrors = task.errors && task.errors.length > 0;
    const duration = task.duration_sec;
    const abortable = canAbort(task);
    const isAborting = abortingId === task.id;
    // WPD-05：优先用 error_code → i18n 翻译，兜底历史乱码 message
    const displayMessage = getTaskErrorMessage(task, t);

    return (
      <div className="task-detail-panel">
        {/* Actions */}
        <div className="task-detail-section task-detail-actions">
          <Space size="small">
            {abortable && (
              <Button
                danger
                icon={<PoweroffOutlined />}
                loading={isAborting}
                onClick={() => handleAbort(task)}
              >
                {isAborting ? t("taskAbortRunning") : t("taskAbort")}
              </Button>
            )}
            {isExternalSyncTask(task.task_type) && (
              <Button
                loading={batchLoadingId === task.id}
                onClick={() => void openBatchDetail(task.id)}
              >
                查看批次
              </Button>
            )}
            {/* WP-AI.7：让 AI 解释（携带 task_id） */}
            <ExplainButton
              sourcePage="task_center"
              references={{ task_id: task.id }}
            />
          </Space>
        </div>
        {/* Progress */}
        <div className="task-detail-section">
          <div className="task-detail-row">
            <span className="task-detail-label">{t("taskProgress")}</span>
            <Progress percent={progressPct} size="small" style={{ flex: 1, marginLeft: 12 }} status={task.status === "failed" ? "exception" : task.status === "done" ? "success" : "active"} />
          </div>
          <div className="task-detail-row">
            <span className="task-detail-label">{t("taskProcessed")}</span>
            <span>{task.processed} / {task.total}</span>
          </div>
          <div className="task-detail-row">
            <span className="task-detail-label">{t("taskResult")}</span>
            <Space>
              <Tag color="green">{t("taskOk")}: {task.ok_count}</Tag>
              <Tag color="red">{t("taskFailed")}: {task.failed_count}</Tag>
            </Space>
          </div>
          {duration != null && (
            <div className="task-detail-row">
              <span className="task-detail-label">{t("taskDuration")}</span>
              <span>{formatDuration(duration)}</span>
            </div>
          )}
        </div>

        {/* Timeline */}
        <div className="task-detail-section">
          <span className="task-detail-label">{t("taskTimeline")}</span>
          <Timeline
            style={{ marginTop: 8 }}
            items={[
              { children: <span>{t("taskCreatedAt")}: {formatTime(task.created_at)}</span> },
              ...(task.started_at ? [{ children: <span>{t("taskStartedAt")}: {formatTime(task.started_at)}</span> }] : []),
              ...(task.finished_at ? [{ children: <span>{t("taskFinishedAt")}: {formatTime(task.finished_at)}</span> }] : []),
            ]}
          />
        </div>

        {/* Current item */}
        {task.current_item && (
          <div className="task-detail-section">
            <span className="task-detail-label">{t("taskCurrentItem")}</span>
            <span style={{ marginLeft: 8 }}>{task.current_item}</span>
          </div>
        )}

        {/* Payload */}
        {Object.keys(task.payload).length > 0 && (
          <div className="task-detail-section">
            <span className="task-detail-label">{t("taskPayload")}</span>
            <pre className="task-detail-json">{JSON.stringify(task.payload, null, 2)}</pre>
          </div>
        )}

        {/* Result */}
        {Object.keys(task.result).length > 0 && (
          <div className="task-detail-section">
            <span className="task-detail-label">{t("taskResultDetail")}</span>
            <pre className="task-detail-json">{JSON.stringify(task.result, null, 2)}</pre>
          </div>
        )}

        {/* Errors */}
        {hasErrors && (
          <div className="task-detail-section">
            <span className="task-detail-label" style={{ color: "#b42318" }}>{t("taskErrors")} ({task.errors.length})</span>
            <div className="task-error-list">
              {task.errors.map((err: unknown, idx: number) => (
                <div key={idx} className="task-error-item">
                  {typeof err === "string" ? err : JSON.stringify(err)}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Message (WPD-05: 优先 error_code → i18n 翻译，兜底乱码检测) */}
        {displayMessage && (
          <div className="task-detail-section">
            <span className="task-detail-label">{t("taskMessage")}</span>
            <span style={{ marginLeft: 8 }}>{displayMessage}</span>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="task-center-container">
      {/* Header */}
      <div className="task-center-header">
        <div className="task-center-header-left">
          <SyncOutlined style={{ fontSize: 18, color: "#0f766e" }} />
          <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>{t("taskCenter")}</h3>
          {activeCount > 0 && (
            <Tag color="blue" icon={<LoadingOutlined />}>{activeCount} {t("taskActive")}</Tag>
          )}
          {observability && Object.entries(observability.domains).map(([domain, slot]) => (
            <Tag key={domain} color={slot.waiting ? "warning" : "default"}>
              {domain} {slot.running}/{slot.slot_limit}{slot.waiting ? ` · waiting ${slot.waiting}` : ""}
            </Tag>
          ))}
        </div>
        <Space>
          <Select
            value={filter}
            onChange={setFilter}
            style={{ width: 160 }}
            options={[
              { value: "all", label: t("taskFilterAll") },
              { value: "active", label: t("taskFilterActive") },
              { value: "failed", label: t("taskFilterFailed") },
              { value: "market_data_sync", label: t("taskTypeSync") },
              { value: "history_initialization", label: t("taskTypeHistory") },
              { value: "discovery_mining", label: t("taskTypeDiscovery") },
              { value: "universe_incremental_sync", label: t("taskTypeUniverseIncremental") },
              { value: "macro_update", label: t("taskTypeMacro") },
              { value: "factor_pipeline", label: t("taskTypeFactorPipeline") },
              { value: "external", label: "外部数据同步" },
            ]}
          />
          <Button icon={<ReloadOutlined />} onClick={loadTasks} loading={loading}>
            {t("refresh")}
          </Button>
        </Space>
      </div>

      {/* Task list */}
      {loading && tasks.length === 0 ? (
        <div style={{ textAlign: "center", padding: 40 }}><Spin /></div>
      ) : filteredTasks.length === 0 ? (
        <Empty description={t("taskNoTasks")} style={{ padding: 40 }} />
      ) : (
        <Collapse
          accordion
          expandIconPosition="end"
          activeKey={expandedKeys}
          onChange={(keys) => setExpandedKeys(keys as string[])}
          items={filteredTasks.map((task) => ({
            key: task.id,
            label: (
              <div className="task-list-item">
                <div className="task-list-item-main">
                  {statusTag(task.status)}
                  <Tag>{taskTypeLabel(task.task_type)}</Tag>
                  <span className="task-list-item-stage">{enumLabel("taskStage", task.stage)}</span>
                </div>
                <div className="task-list-item-meta">
                  <span>{task.processed}/{task.total}</span>
                  <span className="task-list-item-time">{formatTime(task.created_at)}</span>
                </div>
              </div>
            ),
            children: renderTaskDetail(task),
          }))}
        />
      )}
      <Modal
        title="同步计划与分片"
        open={batchDetail !== null}
        footer={null}
        width={780}
        onCancel={() => setBatchDetail(null)}
      >
        {batchDetail?.plan && <>
          <pre className="task-detail-json">{JSON.stringify(batchDetail.plan, null, 2)}</pre>
          <TableLikePartitions partitions={batchDetail.partitions || []} />
        </>}
      </Modal>
    </div>
  );
}

function TableLikePartitions({ partitions }: { partitions: any[] }) {
  if (!partitions.length) return <Empty description="暂无分片" />;
  return <div className="task-error-list" style={{ maxHeight: 360, overflow: "auto" }}>
    {partitions.map((partition) => (
      <div className="task-detail-row" key={partition.id}>
        <span>{partition.symbol || partition.key}</span>
        <Space size="small">
          <Tag>{partition.status}</Tag>
          <span>{partition.rows_written} rows</span>
          {partition.error_message && <Typography.Text type="danger" ellipsis={{ tooltip: partition.error_message }}>失败：{partition.error_message}</Typography.Text>}
        </Space>
      </div>
    ))}
  </div>;
}
