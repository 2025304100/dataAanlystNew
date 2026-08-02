import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  InputNumber,
  Progress,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { ReloadOutlined, StopOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { useApp } from "../../context/AppContext";
import { t } from "../../i18n";
import {
  api,
  type EvaluationRunRead,
  type EvaluationTaskCreatePayload,
  type EvaluationTaskRead,
  type ParameterPerturbationResult,
  type StressTestSummary,
} from "../../api/client";

const TERMINAL_TASK_STATES = new Set(["done", "completed", "failed", "cancelled"]);
const POLL_INTERVAL_MS = 2000;
const STALL_THRESHOLD_SECONDS = 30;

/** 终态门禁颜色映射。 */
function gateColor(gate: string | null | undefined): string {
  if (!gate) return "default";
  const map: Record<string, string> = {
    passed: "green",
    rejected: "red",
    warn: "orange",
  };
  return map[gate] || "default";
}

function gateLabel(gate: string | null | undefined): string {
  if (!gate) return t("evalLabGatePending");
  const key = `evalLabGate_${gate}`;
  const translated = t(key);
  return translated === key ? gate : translated;
}

function taskStatusLabel(status: string): string {
  const map: Record<string, string> = {
    running: "evalLabTaskRunning",
    queued: "evalLabTaskQueued",
    done: "evalLabTaskDone",
    completed: "evalLabTaskDone",
    failed: "evalLabTaskFailed",
    cancelled: "evalLabTaskCancelled",
  };
  const key = map[status] || "evalLabTaskUnknown";
  const translated = t(key);
  return translated === key ? status : translated;
}

function taskStatusColor(status: string): string {
  const map: Record<string, string> = {
    running: "processing",
    queued: "default",
    done: "success",
    completed: "success",
    failed: "error",
    cancelled: "default",
  };
  return map[status] || "default";
}

function stageLabel(stage: string | null | undefined): string {
  if (!stage) return "-";
  const key = `evalLabStage_${stage}`;
  const translated = t(key);
  return translated === key ? stage : translated;
}

function parseServerDateTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const hasTimeZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  const parsed = new Date(hasTimeZone ? value : `${value}Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatDateTime(value: string | null | undefined): string {
  const parsed = parseServerDateTime(value);
  if (!parsed) return "-";
  const pad = (item: number) => String(item).padStart(2, "0");
  return [
    `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`,
    `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`,
  ].join(" ");
}

function shortId(value: string | null | undefined): string {
  if (!value) return "-";
  return value.length > 22 ? `${value.slice(0, 12)}...${value.slice(-6)}` : value;
}

function formatNumber(value: unknown, digits = 4): string {
  if (value == null || value === "") return "-";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "-";
  if (Math.abs(num) >= 1000) return num.toFixed(2);
  return num.toFixed(digits);
}

function formatPercent(value: unknown, digits = 2): string {
  if (value == null || value === "") return "-";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "-";
  return `${(num * 100).toFixed(digits)}%`;
}

function verdictLabel(verdict: string | null | undefined): string {
  if (!verdict) return "-";
  if (verdict === "stable") return t("evalLabStressVerdictStable");
  if (verdict === "unstable") return t("evalLabStressVerdictUnstable");
  if (verdict === "cliff_drop") return t("evalLabStressVerdictCliffDrop");
  return verdict;
}

function verdictColor(verdict: string | null | undefined): string {
  if (!verdict) return "default";
  if (verdict === "stable") return "green";
  if (verdict === "unstable") return "orange";
  if (verdict === "cliff_drop") return "red";
  return "default";
}

interface EvalLabState {
  tasks: EvaluationTaskRead[];
  runs: EvaluationRunRead[];
  activeTask: EvaluationTaskRead | null;
  selectedRun: EvaluationRunRead | null;
  gateFilter: string;
  loadingTasks: boolean;
  loadingRuns: boolean;
  submitting: boolean;
  cancelling: boolean;
  error: string | null;
}

const initialState: EvalLabState = {
  tasks: [],
  runs: [],
  activeTask: null,
  selectedRun: null,
  gateFilter: "",
  loadingTasks: false,
  loadingRuns: false,
  submitting: false,
  cancelling: false,
  error: null,
};

export default function FactorEvaluationLab() {
  const ctx = useApp();
  const { message } = App.useApp();
  void ctx; // 保留 context 用于未来 locale 切换/校验

  const [state, setState] = useState<EvalLabState>(initialState);
  const lastProgressRef = useRef<{ signature: string; time: number } | null>(null);
  const [stalled, setStalled] = useState(false);
  const [elapsed, setElapsed] = useState(0);

  // 表单字段
  const [factorCode, setFactorCode] = useState("");
  const [factorKind, setFactorKind] = useState<"continuous" | "event" | "regime">("continuous");
  const [targetHorizon, setTargetHorizon] = useState(5);
  const [nGroups, setNGroups] = useState(5);
  const [costRate, setCostRate] = useState(0.001);

  const update = useCallback((patch: Partial<EvalLabState>) => {
    setState((prev) => ({ ...prev, ...patch }));
  }, []);

  const loadTasks = useCallback(async (showLoading = true) => {
    if (showLoading) update({ loadingTasks: true });
    try {
      const tasks = await api.listEvaluationTasks(50);
      const activeFromList =
        tasks.find((t) => !TERMINAL_TASK_STATES.has(t.status)) ?? null;
      update({ tasks, activeTask: activeFromList ?? tasks[0] ?? null, error: null });
    } catch (err: any) {
      update({ error: err?.message || t("evalLabLoadFailed") });
    } finally {
      if (showLoading) update({ loadingTasks: false });
    }
  }, [update]);

  const loadRuns = useCallback(async (showLoading = true) => {
    if (showLoading) update({ loadingRuns: true });
    try {
      const runs = await api.listEvaluationRuns({
        gateResult: state.gateFilter || undefined,
        limit: 50,
      });
      update({ runs, error: null });
    } catch (err: any) {
      update({ error: err?.message || t("evalLabRunLoadFailed") });
    } finally {
      if (showLoading) update({ loadingRuns: false });
    }
  }, [state.gateFilter, update]);

  const loadAll = useCallback(async (showLoading = true) => {
    await Promise.all([loadTasks(showLoading), loadRuns(showLoading)]);
  }, [loadTasks, loadRuns]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // 运行中任务轮询
  useEffect(() => {
    if (!state.activeTask || TERMINAL_TASK_STATES.has(state.activeTask.status)) return;
    const timer = window.setInterval(async () => {
      try {
        const task = await api.getEvaluationTask(state.activeTask!.id);
        update({ activeTask: task });
        if (TERMINAL_TASK_STATES.has(task.status)) {
          await loadAll(false);
        }
      } catch (err: any) {
        update({ error: err?.message || t("evalLabLoadFailed") });
      }
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [state.activeTask?.id, state.activeTask?.status, loadAll, update]);

  // 计算已运行时长与停滞检测
  useEffect(() => {
    const activeTask = state.activeTask;
    if (!activeTask || TERMINAL_TASK_STATES.has(activeTask.status)) {
      setElapsed(0);
      setStalled(false);
      lastProgressRef.current = null;
      return;
    }
    const startTimeStr = activeTask.started_at || activeTask.created_at;
    const startTime = parseServerDateTime(startTimeStr)?.getTime() ?? Date.now();
    const signature = [
      activeTask.percent,
      activeTask.processed,
      activeTask.message,
      activeTask.updated_at,
    ].join("|");
    if (!lastProgressRef.current || lastProgressRef.current.signature !== signature) {
      lastProgressRef.current = { signature, time: Date.now() };
      setStalled(false);
    }
    const tickTimer = window.setInterval(() => {
      const now = Date.now();
      setElapsed(Math.max(0, Math.floor((now - startTime) / 1000)));
      if (lastProgressRef.current) {
        const stallSeconds = (now - lastProgressRef.current.time) / 1000;
        setStalled(stallSeconds >= STALL_THRESHOLD_SECONDS);
      }
    }, 1000);
    return () => window.clearInterval(tickTimer);
  }, [
    state.activeTask?.id,
    state.activeTask?.status,
    state.activeTask?.percent,
    state.activeTask?.processed,
    state.activeTask?.message,
    state.activeTask?.updated_at,
    state.activeTask?.started_at,
    state.activeTask?.created_at,
  ]);

  // 当任务变为 done 时，从 result.run_id 自动选中对应运行
  useEffect(() => {
    const activeTask = state.activeTask;
    if (!activeTask || activeTask.status !== "done") return;
    const runId = (activeTask.result as Record<string, unknown> | null)?.run_id;
    if (typeof runId === "string" && runId && !state.selectedRun) {
      api.getEvaluationRun(runId)
        .then((run) => update({ selectedRun: run }))
        .catch(() => { /* 运行记录可能尚未写入，忽略 */ });
    }
  }, [state.activeTask, state.selectedRun, update]);

  const handleSubmit = async () => {
    const code = factorCode.trim();
    if (!code) {
      message.warning(t("evalLabCodeRequired"));
      return;
    }
    update({ submitting: true, error: null });
    try {
      const payload: EvaluationTaskCreatePayload = {
        factor_code: code,
        factor_kind: factorKind,
        target_horizon: targetHorizon,
        n_groups: nGroups,
        cost_rate: costRate,
      };
      const task = await api.createEvaluationTask(payload);
      message.success(t("evalLabCreateSuccess"));
      setFactorCode("");
      update({ activeTask: task, selectedRun: null });
      await loadAll(false);
    } catch (err: any) {
      const msg = err?.user_message || err?.message || t("evalLabCreateFailed");
      message.error(msg);
      update({ error: msg });
    } finally {
      update({ submitting: false });
    }
  };

  const handleCancel = async () => {
    const activeTask = state.activeTask;
    if (!activeTask) return;
    update({ cancelling: true });
    try {
      const task = await api.cancelEvaluationTask(activeTask.id);
      update({ activeTask: task });
      message.success(t("evalLabCancelSuccess"));
    } catch (err: any) {
      const msg = err?.user_message || err?.message || t("evalLabCancelFailed");
      message.error(msg);
    } finally {
      update({ cancelling: false });
    }
  };

  const handleViewRun = useCallback(async (runId: string) => {
    try {
      const run = await api.getEvaluationRun(runId);
      update({ selectedRun: run });
    } catch (err: any) {
      message.error(err?.message || t("evalLabRunLoadFailed"));
    }
  }, [update, message]);

  const handleViewTaskRun = useCallback(async (taskId: string) => {
    // 从任务 result.run_id 跳转到运行详情（保留为公共方法，供任务列表/进度卡片复用）
    try {
      const task = await api.getEvaluationTask(taskId);
      update({ activeTask: task });
      const runId = (task.result as Record<string, unknown> | null)?.run_id;
      if (typeof runId === "string" && runId) {
        await handleViewRun(runId);
      }
    } catch (err: any) {
      message.error(err?.message || t("evalLabLoadFailed"));
    }
  }, [update, message, handleViewRun]);
  void handleViewTaskRun; // 预留接口，未来可由行点击触发

  const taskRunning = !!state.activeTask && !TERMINAL_TASK_STATES.has(state.activeTask.status);

  // —— 任务列表列定义 ——
  const taskColumns = useMemo(
    () => [
      {
        title: t("evalLabColFactorCode"),
        dataIndex: ["result", "factor_code"],
        key: "factor_code",
        width: 140,
        render: (value: unknown, record: EvaluationTaskRead) => {
          // 优先从 result 取，回退到 current_item
          const fromResult = (record.result as Record<string, unknown> | null)?.factor_code;
          const v = (fromResult as string) || record.current_item || "-";
          return <span>{String(v)}</span>;
        },
      },
      {
        title: t("evalLabColStatus"),
        dataIndex: "status",
        key: "status",
        width: 100,
        render: (status: string) => <Tag color={taskStatusColor(status)}>{taskStatusLabel(status)}</Tag>,
      },
      {
        title: t("evalLabColStage"),
        dataIndex: "stage",
        key: "stage",
        width: 120,
        render: (stage: string) => stageLabel(stage),
      },
      {
        title: t("evalLabColProgress"),
        dataIndex: "percent",
        key: "percent",
        width: 100,
        render: (percent: number) => `${(percent || 0).toFixed(1)}%`,
      },
      {
        title: t("evalLabColStartedAt"),
        dataIndex: "started_at",
        key: "started_at",
        width: 160,
        render: (value: string | null) => formatDateTime(value),
      },
      {
        title: t("evalLabColFinishedAt"),
        dataIndex: "finished_at",
        key: "finished_at",
        width: 160,
        render: (value: string | null) => formatDateTime(value),
      },
      {
        title: t("evalLabColActions"),
        key: "actions",
        width: 120,
        render: (_: unknown, record: EvaluationTaskRead) => {
          const runId = (record.result as Record<string, unknown> | null)?.run_id;
          return (
            <Button
              size="small"
              type="link"
              disabled={typeof runId !== "string" || !runId}
              onClick={() => typeof runId === "string" && runId && handleViewRun(runId)}
            >
              {t("evalLabViewRun")}
            </Button>
          );
        },
      },
    ],
    [handleViewRun],
  );

  // —— 运行历史列定义 ——
  const runColumns = useMemo(
    () => [
      {
        title: t("evalLabColRunId"),
        dataIndex: "id",
        key: "id",
        width: 200,
        render: (id: string) => (
          <Tooltip title={id}>
            <span>{shortId(id)}</span>
          </Tooltip>
        ),
      },
      {
        title: t("evalLabColVersion"),
        dataIndex: "factor_version_id",
        key: "factor_version_id",
        width: 80,
      },
      {
        title: t("evalLabColGate"),
        dataIndex: "gate_result",
        key: "gate_result",
        width: 100,
        render: (gate: string | null) => <Tag color={gateColor(gate)}>{gateLabel(gate)}</Tag>,
      },
      {
        title: t("evalLabColCutoff"),
        dataIndex: "data_cutoff_at",
        key: "data_cutoff_at",
        width: 160,
        render: (value: string | null) => formatDateTime(value),
      },
      {
        title: t("evalLabColCreatedAt"),
        dataIndex: "created_at",
        key: "created_at",
        width: 160,
        render: (value: string | null) => formatDateTime(value),
      },
      {
        title: t("evalLabColActions"),
        key: "actions",
        width: 120,
        render: (_: unknown, record: EvaluationRunRead) => (
          <Button
            size="small"
            type="link"
            onClick={() => handleViewRun(record.id)}
          >
            {t("evalLabViewRun")}
          </Button>
        ),
      },
    ],
    [handleViewRun],
  );

  const activeTask = state.activeTask;
  const selectedRun = state.selectedRun;
  const metrics = selectedRun?.metrics;
  const stress = metrics?.stress_test;
  const heartbeatStr = activeTask?.heartbeat_at ? formatDateTime(activeTask.heartbeat_at) : "-";

  return (
    <div className="factor-eval-lab">
      <div className="factor-eval-lab-header">
        <div>
          <h2>{t("evalLabTitle")}</h2>
          <p className="factor-eval-lab-subtitle">{t("evalLabSubtitle")}</p>
        </div>
        <Space>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => loadAll(true)}
            loading={state.loadingTasks || state.loadingRuns}
          >
            {t("evalLabRefresh")}
          </Button>
        </Space>
      </div>

      {state.error ? (
        <Alert
          type="error"
          message={state.error}
          closable
          onClose={() => update({ error: null })}
          style={{ marginBottom: 12 }}
        />
      ) : null}

      <div className="factor-eval-lab-layout">
        {/* 左栏：创建表单 + 活动任务 */}
        <div className="factor-eval-lab-main">
          <Card
            title={
              <Space>
                <ThunderboltOutlined />
                {t("evalLabNewTask")}
              </Space>
            }
            size="small"
            style={{ marginBottom: 12 }}
          >
            <div className="factor-eval-lab-grid">
              <div className="factor-eval-lab-field">
                <label>{t("evalLabFactorCode")}</label>
                <Input
                  value={factorCode}
                  onChange={(e) => setFactorCode(e.target.value)}
                  placeholder={t("evalLabFactorCodePlaceholder")}
                  disabled={taskRunning || state.submitting}
                  onPressEnter={handleSubmit}
                />
              </div>
              <div className="factor-eval-lab-field">
                <label>{t("evalLabFactorKind")}</label>
                <Select
                  value={factorKind}
                  onChange={(v) => setFactorKind(v)}
                  disabled={taskRunning || state.submitting}
                  style={{ width: "100%" }}
                  options={[
                    { value: "continuous", label: t("factorKind_continuous") },
                    { value: "event", label: t("factorKind_event") },
                    { value: "regime", label: t("factorKind_regime") },
                  ]}
                />
              </div>
              <div className="factor-eval-lab-field">
                <label>{t("evalLabTargetHorizon")}</label>
                <InputNumber
                  value={targetHorizon}
                  onChange={(v) => setTargetHorizon(Number(v) || 5)}
                  min={1}
                  max={60}
                  disabled={taskRunning || state.submitting}
                  style={{ width: "100%" }}
                />
              </div>
              <div className="factor-eval-lab-field">
                <label>{t("evalLabNGroups")}</label>
                <InputNumber
                  value={nGroups}
                  onChange={(v) => setNGroups(Number(v) || 5)}
                  min={2}
                  max={10}
                  disabled={taskRunning || state.submitting}
                  style={{ width: "100%" }}
                />
              </div>
              <div className="factor-eval-lab-field">
                <label>{t("evalLabCostRate")}</label>
                <InputNumber
                  value={costRate}
                  onChange={(v) => setCostRate(Number(v) || 0)}
                  min={0}
                  max={0.01}
                  step={0.0005}
                  disabled={taskRunning || state.submitting}
                  style={{ width: "100%" }}
                />
              </div>
              <div className="factor-eval-lab-field factor-eval-lab-submit">
                <Button
                  type="primary"
                  onClick={handleSubmit}
                  loading={state.submitting}
                  disabled={taskRunning}
                  block
                >
                  {state.submitting ? t("evalLabSubmitting") : t("evalLabSubmit")}
                </Button>
              </div>
            </div>
          </Card>

          {/* 实时进度卡片 */}
          {activeTask ? (
            <Card
              title={t("evalLabSectionProgress")}
              size="small"
              style={{ marginBottom: 12 }}
              extra={
                <Space size="small">
                  <Tooltip title={`${t("evalLabTaskHeartbeat")}: ${heartbeatStr}`}>
                    <Tag color={taskRunning ? "processing" : "default"}>
                      {taskStatusLabel(activeTask.status)}
                    </Tag>
                  </Tooltip>
                  {taskRunning ? (
                    <Button
                      size="small"
                      danger
                      icon={<StopOutlined />}
                      onClick={handleCancel}
                      loading={state.cancelling}
                    >
                      {t("evalLabCancel")}
                    </Button>
                  ) : null}
                </Space>
              }
            >
              <Descriptions column={1} size="small" bordered>
                <Descriptions.Item label={t("evalLabTaskIdLabel")}>
                  <Tooltip title={activeTask.id}>
                    <span>{shortId(activeTask.id)}</span>
                  </Tooltip>
                </Descriptions.Item>
                <Descriptions.Item label={t("evalLabColStage")}>
                  {stageLabel(activeTask.stage)}
                </Descriptions.Item>
                <Descriptions.Item label={t("evalLabColMessage")}>
                  {activeTask.message || "-"}
                </Descriptions.Item>
                {taskRunning ? (
                  <Descriptions.Item label={t("evalLabTaskElapsed")}>
                    {elapsed} {t("evalLabSeconds")}
                  </Descriptions.Item>
                ) : null}
                {taskRunning && activeTask.heartbeat_at ? (
                  <Descriptions.Item label={t("evalLabTaskHeartbeat")}>
                    {heartbeatStr}
                  </Descriptions.Item>
                ) : null}
              </Descriptions>
              <div style={{ marginTop: 8 }}>
                <Progress percent={Math.round(activeTask.percent || 0)} status={taskRunning ? "active" : "normal"} />
              </div>
              {stalled && taskRunning ? (
                <Alert
                  type="warning"
                  message={t("evalLabTaskStalled")}
                  showIcon
                  style={{ marginTop: 8 }}
                />
              ) : null}
              {activeTask.error_code ? (
                <Alert
                  type="error"
                  message={activeTask.error_code}
                  description={activeTask.message}
                  showIcon
                  style={{ marginTop: 8 }}
                />
              ) : null}
            </Card>
          ) : null}

          {/* 任务列表 */}
          <Card
            title={t("evalLabTaskList")}
            size="small"
            style={{ marginBottom: 12 }}
          >
            <Spin spinning={state.loadingTasks}>
              {state.tasks.length === 0 ? (
                <Empty description={t("evalLabEmptyTasks")} />
              ) : (
                <Table
                  rowKey="id"
                  dataSource={state.tasks}
                  columns={taskColumns}
                  size="small"
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  scroll={{ x: 900 }}
                />
              )}
            </Spin>
          </Card>
        </div>

        {/* 右栏：运行历史 + 运行报告 */}
        <div className="factor-eval-lab-side">
          <Card
            title={t("evalLabRunHistory")}
            size="small"
            style={{ marginBottom: 12 }}
            extra={
              <Select
                size="small"
                value={state.gateFilter || ""}
                onChange={(v) => update({ gateFilter: v || "" })}
                style={{ width: 120 }}
                options={[
                  { value: "", label: t("evalLabFilterAll") },
                  { value: "passed", label: t("evalLabGate_passed") },
                  { value: "rejected", label: t("evalLabGate_rejected") },
                  { value: "warn", label: t("evalLabGate_warn") },
                ]}
              />
            }
          >
            <Spin spinning={state.loadingRuns}>
              {state.runs.length === 0 ? (
                <Empty description={t("evalLabEmptyRuns")} />
              ) : (
                <Table
                  rowKey="id"
                  dataSource={state.runs}
                  columns={runColumns}
                  size="small"
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  scroll={{ x: 800 }}
                />
              )}
            </Spin>
          </Card>

          {/* 运行报告 */}
          {selectedRun ? (
            <EvaluationRunReport run={selectedRun} metrics={metrics} stress={stress} />
          ) : (
            <Card size="small">
              <Empty description={t("evalLabNoMetrics")} />
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

// ══════════════════════════════════════════════════════════
// 评估运行报告子组件
// ══════════════════════════════════════════════════════════

interface EvaluationRunReportProps {
  run: EvaluationRunRead;
  metrics: EvaluationRunRead["metrics"] | undefined;
  stress: StressTestSummary | undefined;
}

function EvaluationRunReport({ run, metrics, stress }: EvaluationRunReportProps) {
  const m = metrics || ({} as EvaluationRunRead["metrics"]);
  const hasMetrics = !!metrics && Object.keys(metrics).length > 0;
  const quantileReturns = Array.isArray(m.quantile_returns) ? m.quantile_returns : [];

  return (
    <Card
      title={
        <Space>
          <span>{t("evalLabSectionReport")}</span>
          <Tag color={gateColor(run.gate_result)}>{gateLabel(run.gate_result)}</Tag>
        </Space>
      }
      size="small"
      style={{ marginBottom: 12 }}
      extra={
        <Tooltip title={run.id}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {shortId(run.id)}
          </Typography.Text>
        </Tooltip>
      }
    >
      <Alert
        type="info"
        message={t("evalLabRunImmutable")}
        showIcon
        style={{ marginBottom: 12 }}
      />

      {/* 门禁结论 + 拒绝原因 */}
      <Card
        type="inner"
        title={t("evalLabSectionGate")}
        size="small"
        style={{ marginBottom: 12 }}
      >
        <Descriptions column={2} size="small">
          <Descriptions.Item label={t("evalLabColGate")}>
            <Tag color={gateColor(run.gate_result)}>{gateLabel(run.gate_result)}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabRejectionReasons")}>
            {run.rejection_reasons && run.rejection_reasons.length > 0 ? (
              <Space direction="vertical" size={2}>
                {run.rejection_reasons.map((reason, idx) => (
                  <Tag key={idx} color="red">
                    {reason}
                  </Tag>
                ))}
              </Space>
            ) : (
              <Typography.Text type="secondary">{t("evalLabNoRejectionReasons")}</Typography.Text>
            )}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {/* 基础指标 */}
      <Card
        type="inner"
        title={t("evalLabSectionMetrics")}
        size="small"
        style={{ marginBottom: 12 }}
      >
        {!hasMetrics ? (
          <Empty description={t("evalLabNoMetrics")} />
        ) : (
          <>
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label={t("evalLabMetricRankIc")}>
                {formatNumber(m.rank_ic_mean)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricRankIcMedian")}>
                {formatNumber(m.rank_ic_median)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricRankIcStd")}>
                {formatNumber(m.rank_ic_std)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricIcir")}>
                {formatNumber(m.icir)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricPositiveRatio")}>
                {formatPercent(m.positive_ic_ratio)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricCoverage")}>
                {formatPercent(m.coverage)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricSamples")}>
                {formatNumber(m.n_samples, 0)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricMonotonicity")}>
                {formatNumber(m.monotonicity_score)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricLongShort")}>
                {formatNumber(m.long_short_return)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricTurnover")}>
                {formatNumber(m.turnover)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricCostAdjusted")}>
                {formatNumber(m.cost_adjusted_return)}
              </Descriptions.Item>
            </Descriptions>
            {quantileReturns.length > 0 ? (
              <div style={{ marginTop: 8 }}>
                <Typography.Text strong style={{ fontSize: 13 }}>
                  {t("evalLabMetricQuantileReturns")}：
                </Typography.Text>
                <Space size={4} wrap style={{ marginTop: 4 }}>
                  {quantileReturns.map((ret, idx) => (
                    <Tag key={idx} color="blue">
                      Q{idx + 1}: {formatNumber(ret)}
                    </Tag>
                  ))}
                </Space>
              </div>
            ) : null}
            <Descriptions column={2} size="small" style={{ marginTop: 8 }}>
              <Descriptions.Item label={t("evalLabMetricTrainRange")}>
                {formatDateRange(m.train_start, m.train_end)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricValidationRange")}>
                {formatDateRange(m.validation_start, m.validation_end)}
              </Descriptions.Item>
            </Descriptions>
          </>
        )}
      </Card>

      {/* 压力测试 */}
      <Card
        type="inner"
        title={t("evalLabSectionStress")}
        size="small"
        style={{ marginBottom: 12 }}
      >
        {!stress ? (
          <Empty description={t("evalLabNoStress")} />
        ) : (
          <>
            <Descriptions column={1} size="small" bordered style={{ marginBottom: 8 }}>
              <Descriptions.Item label={t("evalLabStressOverall")}>
                <Tag color={verdictColor(stress.overall_verdict)}>
                  {verdictLabel(stress.overall_verdict)}
                </Tag>
              </Descriptions.Item>
              {stress.failure_reasons && stress.failure_reasons.length > 0 ? (
                <Descriptions.Item label={t("evalLabStressFailureReasons")}>
                  <Space direction="vertical" size={2}>
                    {stress.failure_reasons.map((reason, idx) => (
                      <Tag key={idx} color="red">
                        {reason}
                      </Tag>
                    ))}
                  </Space>
                </Descriptions.Item>
              ) : null}
            </Descriptions>

            {/* 参数扰动 */}
            <Typography.Text strong style={{ display: "block", margin: "8px 0 4px" }}>
              {t("evalLabStressParameter")}
            </Typography.Text>
            {stress.parameter_results && stress.parameter_results.length > 0 ? (
              <Table
                rowKey="param_name"
                dataSource={stress.parameter_results}
                size="small"
                pagination={false}
                scroll={{ x: 600 }}
                columns={[
                  {
                    title: t("evalLabStressColParam"),
                    dataIndex: "param_name",
                    key: "param_name",
                  },
                  {
                    title: t("evalLabStressColBaseline"),
                    dataIndex: "baseline_value",
                    key: "baseline_value",
                    render: (v: number) => formatNumber(v, 2),
                  },
                  {
                    title: t("evalLabStressColVerdict"),
                    dataIndex: "verdict",
                    key: "verdict",
                    render: (v: string) => <Tag color={verdictColor(v)}>{verdictLabel(v)}</Tag>,
                  },
                  {
                    title: t("evalLabStressColSignRatio"),
                    dataIndex: "sign_consistency_ratio",
                    key: "sign_consistency_ratio",
                    render: (v: number) => formatPercent(v),
                  },
                  {
                    title: t("evalLabStressColMedianRatio"),
                    dataIndex: "median_ic_ratio",
                    key: "median_ic_ratio",
                    render: (v: number) => formatNumber(v),
                  },
                  {
                    title: t("evalLabStressColPassing"),
                    dataIndex: "passing_neighbor_count",
                    key: "passing_neighbor_count",
                  },
                  {
                    title: t("evalLabStressColCliff"),
                    dataIndex: "has_cliff_drop",
                    key: "has_cliff_drop",
                    render: (v: boolean) => (v ? t("evalLabStressYes") : t("evalLabStressNo")),
                  },
                ]}
                expandable={{
                  expandedRowRender: (record: ParameterPerturbationResult) => (
                    <Table
                      rowKey="label"
                      dataSource={record.points || []}
                      size="small"
                      pagination={false}
                      columns={[
                        { title: t("evalLabStressColLabel"), dataIndex: "label", key: "label" },
                        {
                          title: t("evalLabStressColValue"),
                          dataIndex: "param_value",
                          key: "param_value",
                          render: (v: number) => formatNumber(v, 2),
                        },
                        {
                          title: t("evalLabStressColIcMean"),
                          dataIndex: "ic_mean",
                          key: "ic_mean",
                          render: (v: number) => formatNumber(v),
                        },
                        {
                          title: t("evalLabStressColIcir"),
                          dataIndex: "icir",
                          key: "icir",
                          render: (v: number) => formatNumber(v),
                        },
                        {
                          title: t("evalLabStressColPassed"),
                          dataIndex: "passed_min_gate",
                          key: "passed_min_gate",
                          render: (v: boolean) =>
                            v ? (
                              <Tag color="green">{t("evalLabStressYes")}</Tag>
                            ) : (
                              <Tag color="red">{t("evalLabStressNo")}</Tag>
                            ),
                        },
                      ]}
                    />
                  ),
                }}
              />
            ) : (
              <Empty description={t("evalLabNoParameterResults")} />
            )}

            {/* 时间段稳定性 */}
            {stress.time_result ? (
              <>
                <Typography.Text strong style={{ display: "block", margin: "12px 0 4px" }}>
                  {t("evalLabStressTime")}
                </Typography.Text>
                <Descriptions column={2} size="small" bordered>
                  <Descriptions.Item label={t("evalLabStressColVerdict")}>
                    <Tag color={verdictColor(stress.time_result.verdict)}>
                      {verdictLabel(stress.time_result.verdict)}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="IC Stability">
                    {formatNumber(stress.time_result.ic_stability)}
                  </Descriptions.Item>
                </Descriptions>
                {stress.time_result.segments && stress.time_result.segments.length > 0 ? (
                  <Table
                    rowKey="segment_label"
                    dataSource={stress.time_result.segments}
                    size="small"
                    pagination={false}
                    style={{ marginTop: 8 }}
                    columns={[
                      {
                        title: t("evalLabStressColSegment"),
                        dataIndex: "segment_label",
                        key: "segment_label",
                      },
                      {
                        title: t("evalLabStressColIcMean"),
                        dataIndex: "ic_mean",
                        key: "ic_mean",
                        render: (v: number) => formatNumber(v),
                      },
                      {
                        title: t("evalLabStressColIcir"),
                        dataIndex: "icir",
                        key: "icir",
                        render: (v: number) => formatNumber(v),
                      },
                    ]}
                  />
                ) : null}
              </>
            ) : null}

            {/* 缺失敏感度 */}
            {stress.missing_result ? (
              <>
                <Typography.Text strong style={{ display: "block", margin: "12px 0 4px" }}>
                  {t("evalLabStressMissing")}
                </Typography.Text>
                <Descriptions column={2} size="small" bordered>
                  <Descriptions.Item label={t("evalLabStressColVerdict")}>
                    <Tag color={verdictColor(stress.missing_result.verdict)}>
                      {verdictLabel(stress.missing_result.verdict)}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="IC Decay Ratio">
                    {formatNumber(stress.missing_result.ic_decay_ratio)}
                  </Descriptions.Item>
                </Descriptions>
              </>
            ) : null}
          </>
        )}
      </Card>

      {/* 完整交易日证据 */}
      <Card
        type="inner"
        title={t("evalLabSectionEvidence")}
        size="small"
        style={{ marginBottom: 12 }}
      >
        <Descriptions column={2} size="small" bordered>
          <Descriptions.Item label={t("evalLabEvidenceSelectedDate")}>
            {run.selected_trade_date || "-"}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabEvidenceObserved")}>
            {run.observed_symbols ?? "-"}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabEvidenceExpected")}>
            {run.expected_symbols ?? "-"}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabEvidenceRatio")}>
            {run.completeness_ratio != null
              ? formatPercent(run.completeness_ratio)
              : "-"}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabEvidenceFallback")} span={2}>
            {run.fallback_reason || t("evalLabNoRejectionReasons")}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {/* 评估配置 */}
      <Card
        type="inner"
        title={t("evalLabSectionConfig")}
        size="small"
      >
        <Descriptions column={2} size="small" bordered>
          <Descriptions.Item label={t("evalLabColVersion")}>
            {run.factor_version_id}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabTargetCode")}>
            {run.target_code}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabConfigFactorKind")}>
            {String(run.config?.factor_kind ?? "-")}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabConfigTargetHorizon")}>
            {String(run.config?.target_horizon ?? "-")}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabConfigNGroups")}>
            {String(run.config?.n_groups ?? "-")}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabConfigCostRate")}>
            {formatNumber(run.config?.cost_rate, 6)}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabConfigEvaluatorVersion")}>
            {String(run.config?.evaluator_version ?? "-")}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabColCutoff")}>
            {formatDateTime(run.data_cutoff_at)}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabMetricTrainRange")}>
            {formatDateRange(run.train_start_date, run.train_end_date)}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabMetricValidationRange")}>
            {formatDateRange(run.validation_start_date, run.validation_end_date)}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabTaskIdLabel")}>
            {run.task_id ? (
              <Tooltip title={run.task_id}>
                <span>{shortId(run.task_id)}</span>
              </Tooltip>
            ) : (
              "-"
            )}
          </Descriptions.Item>
        </Descriptions>
      </Card>
    </Card>
  );
}

function formatDateRange(start: string | null | undefined, end: string | null | undefined): string {
  if (!start && !end) return "-";
  const s = start ? start.slice(0, 10) : "?";
  const e = end ? end.slice(0, 10) : "?";
  return `${s} ~ ${e}`;
}
