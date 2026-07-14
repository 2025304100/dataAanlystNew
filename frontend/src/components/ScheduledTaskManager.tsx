import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
} from "antd";
import {
  CalendarOutlined,
  DeleteOutlined,
  EditOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import {
  api,
  type ScheduledTask,
  type ScheduledTaskDefinition,
  type ScheduledTaskFrequency,
  type ScheduledTaskPayload,
  type ScheduledTaskRun,
} from "../api/client";
import { useApp } from "../context/AppContext";

const ACTIVE_STATES = new Set(["queued", "running", "dispatching"]);
const WEEKDAYS = [
  { value: 0, zh: "周一", en: "Mon" },
  { value: 1, zh: "周二", en: "Tue" },
  { value: 2, zh: "周三", en: "Wed" },
  { value: 3, zh: "周四", en: "Thu" },
  { value: 4, zh: "周五", en: "Fri" },
  { value: 5, zh: "周六", en: "Sat" },
  { value: 6, zh: "周日", en: "Sun" },
];

const LABELS = {
  "zh-CN": {
    title: "定时任务管理", add: "新增计划", refresh: "刷新", name: "计划名称",
    taskType: "任务类型", schedule: "执行计划", nextRun: "下次执行", lastRun: "上次执行",
    status: "状态", enabled: "启用", actions: "操作", runNow: "立即执行", edit: "编辑",
    delete: "删除", deleteTitle: "删除定时任务",
    deleteConfirm: "确认删除该计划及其调度记录？已生成的业务任务不会被删除。",
    daily: "每天", weekly: "每周", interval: "间隔", frequency: "频率", time: "执行时间",
    weekdays: "执行星期", intervalMinutes: "间隔分钟", timezone: "时区",
    payload: "任务参数 JSON", cancel: "取消", save: "保存", runHistory: "调度执行记录",
    trigger: "触发方式", manual: "手动", scheduled: "自动", taskId: "业务任务 ID",
    message: "信息", createdAt: "触发时间", noSchedules: "暂无定时任务",
    noRuns: "暂无调度执行记录", loadFailed: "加载定时任务失败", saveFailed: "保存定时任务失败",
    runFailed: "执行任务失败", invalidJson: "任务参数必须是 JSON 对象",
    saved: "定时任务已保存", started: "任务已提交执行", deleted: "定时任务已删除",
    active: "执行中", total: "计划总数", enabledCount: "已启用",
    backendRequired: "调度器随后台服务运行",
  },
  "en-US": {
    title: "Scheduled Tasks", add: "New Schedule", refresh: "Refresh", name: "Schedule Name",
    taskType: "Task Type", schedule: "Schedule", nextRun: "Next Run", lastRun: "Last Run",
    status: "Status", enabled: "Enabled", actions: "Actions", runNow: "Run Now", edit: "Edit",
    delete: "Delete", deleteTitle: "Delete Scheduled Task",
    deleteConfirm: "Delete this schedule and its dispatch records? Existing business tasks are kept.",
    daily: "Daily", weekly: "Weekly", interval: "Interval", frequency: "Frequency", time: "Run Time",
    weekdays: "Weekdays", intervalMinutes: "Interval Minutes", timezone: "Timezone",
    payload: "Task Payload JSON", cancel: "Cancel", save: "Save", runHistory: "Dispatch History",
    trigger: "Trigger", manual: "Manual", scheduled: "Scheduled", taskId: "Business Task ID",
    message: "Message", createdAt: "Triggered At", noSchedules: "No scheduled tasks",
    noRuns: "No dispatch history", loadFailed: "Failed to load scheduled tasks",
    saveFailed: "Failed to save scheduled task", runFailed: "Failed to run task",
    invalidJson: "Task payload must be a JSON object", saved: "Scheduled task saved",
    started: "Task dispatched", deleted: "Scheduled task deleted", active: "Running",
    total: "Schedules", enabledCount: "Enabled", backendRequired: "Scheduler runs with the backend service",
  },
};

type Draft = {
  name: string;
  task_type: string;
  frequency: ScheduledTaskFrequency;
  time_of_day: string;
  weekdays: number[];
  interval_minutes: number;
  timezone: string;
  payloadText: string;
  enabled: boolean;
};

function emptyDraft(definition?: ScheduledTaskDefinition): Draft {
  return {
    name: definition?.name ?? "",
    task_type: definition?.task_type ?? "",
    frequency: "daily",
    time_of_day: "18:00",
    weekdays: [0, 1, 2, 3, 4],
    interval_minutes: 60,
    timezone: "Asia/Shanghai",
    payloadText: JSON.stringify(definition?.default_payload ?? {}, null, 2),
    enabled: true,
  };
}

function formatUtc(value: string | null | undefined) {
  if (!value) return "-";
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value + "Z";
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function statusColor(status: string | null | undefined) {
  if (status === "done") return "green";
  if (status === "failed") return "red";
  if (status === "cancelled") return "default";
  if (ACTIVE_STATES.has(status ?? "")) return "blue";
  return "default";
}

export default function ScheduledTaskManager() {
  const ctx = useApp();
  const labels = LABELS[ctx.locale as keyof typeof LABELS] ?? LABELS["zh-CN"];
  const english = ctx.locale === "en-US";
  const [definitions, setDefinitions] = useState<ScheduledTaskDefinition[]>([]);
  const [schedules, setSchedules] = useState<ScheduledTask[]>([]);
  const [runs, setRuns] = useState<ScheduledTaskRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<ScheduledTask | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [draft, setDraft] = useState<Draft>(() => emptyDraft());
  const [saving, setSaving] = useState(false);
  const [runningId, setRunningId] = useState<number | null>(null);
  const [togglingId, setTogglingId] = useState<number | null>(null);

  const load = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    setError(null);
    try {
      const [definitionRows, scheduleRows, runRows] = await Promise.all([
        api.getScheduledTaskDefinitions(),
        api.getScheduledTasks(),
        api.getScheduledTaskRuns(100),
      ]);
      setDefinitions(definitionRows);
      setSchedules(scheduleRows);
      setRuns(runRows);
    } catch (err: any) {
      setError(err.message || labels.loadFailed);
    } finally {
      if (showLoading) setLoading(false);
    }
  }, [labels.loadFailed]);

  useEffect(() => { load(); }, [load]);

  const hasActiveRun = useMemo(
    () => runs.some((item) => ACTIVE_STATES.has(item.status)),
    [runs],
  );

  useEffect(() => {
    if (!hasActiveRun) return;
    const timer = window.setInterval(() => load(false), 5000);
    return () => window.clearInterval(timer);
  }, [hasActiveRun, load]);

  const definitionMap = useMemo(
    () => new Map(definitions.map((item) => [item.task_type, item])),
    [definitions],
  );

  const openCreate = () => {
    setEditing(null);
    setDraft(emptyDraft(definitions[0]));
    setModalOpen(true);
  };

  const openEdit = (item: ScheduledTask) => {
    setEditing(item);
    setDraft({
      name: item.name,
      task_type: item.task_type,
      frequency: item.frequency,
      time_of_day: item.time_of_day ?? "18:00",
      weekdays: item.weekdays,
      interval_minutes: item.interval_minutes ?? 60,
      timezone: item.timezone,
      payloadText: JSON.stringify(item.payload ?? {}, null, 2),
      enabled: item.enabled,
    });
    setModalOpen(true);
  };

  const changeTaskType = (taskType: string) => {
    const definition = definitionMap.get(taskType);
    setDraft((current) => ({
      ...current,
      task_type: taskType,
      name: editing ? current.name : definition?.name ?? current.name,
      payloadText: JSON.stringify(definition?.default_payload ?? {}, null, 2),
    }));
  };

  const save = async () => {
    let payload: Record<string, unknown>;
    try {
      const parsed = JSON.parse(draft.payloadText || "{}");
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error();
      payload = parsed;
    } catch {
      setError(labels.invalidJson);
      return;
    }
    const body: ScheduledTaskPayload = {
      name: draft.name.trim(),
      task_type: draft.task_type,
      frequency: draft.frequency,
      time_of_day: draft.frequency === "interval" ? null : draft.time_of_day,
      weekdays: draft.frequency === "weekly" ? draft.weekdays : [],
      interval_minutes: draft.frequency === "interval" ? draft.interval_minutes : null,
      timezone: draft.timezone,
      payload,
      enabled: draft.enabled,
    };
    setSaving(true);
    setError(null);
    try {
      if (editing) await api.updateScheduledTask(editing.id, body);
      else await api.createScheduledTask(body);
      setModalOpen(false);
      ctx.showToast("success", labels.saved);
      await load(false);
    } catch (err: any) {
      setError(err.message || labels.saveFailed);
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (item: ScheduledTask, enabled: boolean) => {
    setTogglingId(item.id);
    try {
      await api.updateScheduledTask(item.id, { enabled });
      await load(false);
    } catch (err: any) {
      setError(err.message || labels.saveFailed);
    } finally {
      setTogglingId(null);
    }
  };

  const runNow = async (item: ScheduledTask) => {
    setRunningId(item.id);
    setError(null);
    try {
      await api.runScheduledTask(item.id);
      ctx.showToast("success", labels.started);
      await load(false);
    } catch (err: any) {
      setError(err.message || labels.runFailed);
    } finally {
      setRunningId(null);
    }
  };

  const remove = (item: ScheduledTask) => {
    Modal.confirm({
      title: labels.deleteTitle,
      content: labels.deleteConfirm,
      okText: labels.delete,
      okButtonProps: { danger: true },
      cancelText: labels.cancel,
      onOk: async () => {
        try {
          await api.deleteScheduledTask(item.id);
          ctx.showToast("success", labels.deleted);
          await load(false);
        } catch (err: any) {
          setError(err.message || labels.saveFailed);
        }
      },
    });
  };

  const scheduleText = (item: ScheduledTask) => {
    if (item.frequency === "interval") {
      return labels.interval + " " + (item.interval_minutes ?? "-") + " min";
    }
    if (item.frequency === "weekly") {
      const days = item.weekdays
        .map((value) => WEEKDAYS.find((day) => day.value === value))
        .filter(Boolean)
        .map((day) => english ? day!.en : day!.zh)
        .join(" / ");
      return days + " " + item.time_of_day;
    }
    return labels.daily + " " + item.time_of_day;
  };

  const columns = [
    {
      title: labels.name,
      dataIndex: "name",
      key: "name",
      render: (value: string, item: ScheduledTask) => (
        <div className="schedule-name-cell">
          <strong>{value}</strong>
          <span>{definitionMap.get(item.task_type)?.name ?? item.task_type}</span>
        </div>
      ),
    },
    { title: labels.schedule, key: "schedule", render: (_: unknown, item: ScheduledTask) => scheduleText(item) },
    { title: labels.nextRun, dataIndex: "next_run_at", key: "next_run_at", render: (value: string | null) => formatUtc(value) },
    {
      title: labels.lastRun,
      key: "last_run",
      render: (_: unknown, item: ScheduledTask) => (
        <div className="schedule-last-cell">
          <span>{formatUtc(item.last_run_at)}</span>
          {item.last_task_status && <Tag color={statusColor(item.last_task_status)}>{item.last_task_status}</Tag>}
        </div>
      ),
    },
    {
      title: labels.enabled,
      key: "enabled",
      render: (_: unknown, item: ScheduledTask) => (
        <Switch checked={item.enabled} loading={togglingId === item.id} onChange={(value) => toggle(item, value)} />
      ),
    },
    {
      title: labels.actions,
      key: "actions",
      render: (_: unknown, item: ScheduledTask) => (
        <Space size={4} wrap>
          <Button size="small" type="primary" icon={<PlayCircleOutlined />} loading={runningId === item.id} onClick={() => runNow(item)}>
            {labels.runNow}
          </Button>
          <Tooltip title={labels.edit}>
            <Button size="small" aria-label={labels.edit} icon={<EditOutlined />} onClick={() => openEdit(item)} />
          </Tooltip>
          <Tooltip title={labels.delete}>
            <Button size="small" danger aria-label={labels.delete} icon={<DeleteOutlined />} onClick={() => remove(item)} />
          </Tooltip>
        </Space>
      ),
    },
  ];

  const runColumns = [
    { title: labels.name, dataIndex: "schedule_name", key: "schedule_name" },
    { title: labels.trigger, dataIndex: "trigger_source", key: "trigger_source", render: (value: string) => value === "manual" ? labels.manual : labels.scheduled },
    { title: labels.status, dataIndex: "status", key: "status", render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> },
    { title: labels.taskId, dataIndex: "task_id", key: "task_id", render: (value: string | null) => value ? <Tooltip title={value}>{value.slice(0, 16)}</Tooltip> : "-" },
    { title: labels.message, dataIndex: "message", key: "message", ellipsis: true },
    { title: labels.createdAt, dataIndex: "created_at", key: "created_at", render: (value: string) => formatUtc(value) },
  ];

  return (
    <div className="scheduled-task-manager">
      <div className="schedule-manager-head">
        <div>
          <p className="panel-kicker">{labels.title}</p>
          <div className="schedule-summary">
            <span>{labels.total}: <strong>{schedules.length}</strong></span>
            <span>{labels.enabledCount}: <strong>{schedules.filter((item) => item.enabled).length}</strong></span>
            {hasActiveRun && <Tag color="blue">{labels.active}</Tag>}
            <Tag icon={<CalendarOutlined />}>{labels.backendRequired}</Tag>
          </div>
        </div>
        <Space>
          <Tooltip title={labels.refresh}>
            <Button aria-label={labels.refresh} icon={<ReloadOutlined />} loading={loading} onClick={() => load()} />
          </Tooltip>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>{labels.add}</Button>
        </Space>
      </div>

      {error && <Alert type="error" showIcon closable message={error} onClose={() => setError(null)} />}

      <Table<ScheduledTask>
        rowKey="id" size="small" loading={loading} dataSource={schedules} columns={columns}
        pagination={false} scroll={{ x: 960 }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={labels.noSchedules} /> }}
      />

      <section className="schedule-run-history">
        <div className="factor-section-title"><h3>{labels.runHistory}</h3><Tag>{runs.length}</Tag></div>
        <Table<ScheduledTaskRun>
          rowKey="id" size="small" dataSource={runs} columns={runColumns}
          pagination={{ pageSize: 10, hideOnSinglePage: true }} scroll={{ x: 820 }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={labels.noRuns} /> }}
        />
      </section>

      <Modal
        open={modalOpen} title={editing ? labels.edit : labels.add}
        okText={labels.save} cancelText={labels.cancel} confirmLoading={saving}
        onOk={save} onCancel={() => setModalOpen(false)} width={680}
      >
        <div className="schedule-form">
          <label><span>{labels.name}</span><Input value={draft.name} onChange={(event) => setDraft((value) => ({ ...value, name: event.target.value }))} /></label>
          <label>
            <span>{labels.taskType}</span>
            <Select value={draft.task_type || undefined} options={definitions.map((item) => ({ value: item.task_type, label: item.name }))} onChange={changeTaskType} />
          </label>
          <label>
            <span>{labels.frequency}</span>
            <Select
              value={draft.frequency}
              options={[
                { value: "daily", label: labels.daily },
                { value: "weekly", label: labels.weekly },
                { value: "interval", label: labels.interval },
              ]}
              onChange={(value) => setDraft((current) => ({ ...current, frequency: value }))}
            />
          </label>
          {draft.frequency !== "interval" && (
            <label><span>{labels.time}</span><Input type="time" value={draft.time_of_day} onChange={(event) => setDraft((value) => ({ ...value, time_of_day: event.target.value }))} /></label>
          )}
          {draft.frequency === "weekly" && (
            <label className="schedule-form-wide">
              <span>{labels.weekdays}</span>
              <Checkbox.Group
                value={draft.weekdays}
                options={WEEKDAYS.map((day) => ({ value: day.value, label: english ? day.en : day.zh }))}
                onChange={(values) => setDraft((current) => ({ ...current, weekdays: values.map(Number) }))}
              />
            </label>
          )}
          {draft.frequency === "interval" && (
            <label><span>{labels.intervalMinutes}</span><InputNumber min={1} max={525600} value={draft.interval_minutes} onChange={(value) => setDraft((current) => ({ ...current, interval_minutes: Number(value ?? 60) }))} /></label>
          )}
          <label>
            <span>{labels.timezone}</span>
            <Select
              value={draft.timezone}
              options={[{ value: "Asia/Shanghai", label: "Asia/Shanghai" }, { value: "UTC", label: "UTC" }]}
              onChange={(value) => setDraft((current) => ({ ...current, timezone: value }))}
            />
          </label>
          <label className="schedule-form-switch"><span>{labels.enabled}</span><Switch checked={draft.enabled} onChange={(value) => setDraft((current) => ({ ...current, enabled: value }))} /></label>
          <label className="schedule-form-wide">
            <span>{labels.payload}</span>
            <Input.TextArea rows={9} value={draft.payloadText} onChange={(event) => setDraft((value) => ({ ...value, payloadText: event.target.value }))} />
          </label>
        </div>
      </Modal>
    </div>
  );
}
