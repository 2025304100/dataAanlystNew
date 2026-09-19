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
import { enumLabel, t, template } from "../i18n";

const ACTIVE_STATES = new Set(["queued", "running", "dispatching"]);

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
      setError(err.message || t("scheduleLoadFailed"));
    } finally {
      if (showLoading) setLoading(false);
    }
  }, []);

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
      setError(t("scheduleInvalidJson"));
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
      ctx.showToast("success", t("scheduleSaved"));
      await load(false);
    } catch (err: any) {
      setError(err.message || t("scheduleSaveFailed"));
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
      setError(err.message || t("scheduleSaveFailed"));
    } finally {
      setTogglingId(null);
    }
  };

  const runNow = async (item: ScheduledTask) => {
    setRunningId(item.id);
    setError(null);
    try {
      await api.runScheduledTask(item.id);
      ctx.showToast("success", t("scheduleStarted"));
      await load(false);
    } catch (err: any) {
      setError(err.message || t("scheduleRunFailed"));
    } finally {
      setRunningId(null);
    }
  };

  const remove = (item: ScheduledTask) => {
    Modal.confirm({
      title: t("scheduleDeleteTitle"),
      content: t("scheduleDeleteConfirm"),
      okText: t("scheduleDelete"),
      okButtonProps: { danger: true },
      cancelText: t("scheduleCancel"),
      onOk: async () => {
        try {
          await api.deleteScheduledTask(item.id);
          ctx.showToast("success", t("scheduleDeleted"));
          await load(false);
        } catch (err: any) {
          setError(err.message || t("scheduleSaveFailed"));
        }
      },
    });
  };

  const scheduleText = (item: ScheduledTask) => {
    if (item.frequency === "interval") {
      return template("scheduleIntervalText", { label: t("scheduleInterval"), minutes: String(item.interval_minutes ?? "-") });
    }
    if (item.frequency === "weekly") {
      const days = item.weekdays
        .map((value) => t("scheduleWeekday" + value))
        .join(" / ");
      return days + " " + item.time_of_day;
    }
    return template("scheduleDailyText", { label: t("scheduleDaily"), time: item.time_of_day ?? "-" });
  };

  const columns = [
    {
      title: t("scheduleName"),
      dataIndex: "name",
      key: "name",
      render: (value: string, item: ScheduledTask) => (
        <div className="schedule-name-cell">
          <strong>{value}</strong>
          <span>{definitionMap.get(item.task_type)?.name ?? item.task_type}</span>
        </div>
      ),
    },
    { title: t("scheduleSchedule"), key: "schedule", render: (_: unknown, item: ScheduledTask) => scheduleText(item) },
    { title: t("scheduleNextRun"), dataIndex: "next_run_at", key: "next_run_at", render: (value: string | null) => formatUtc(value) },
    {
      title: t("scheduleLastRun"),
      key: "last_run",
      render: (_: unknown, item: ScheduledTask) => (
        <div className="schedule-last-cell">
          <span>{formatUtc(item.last_run_at)}</span>
          {item.last_task_status && <Tag color={statusColor(item.last_task_status)}>{enumLabel("taskStatus", item.last_task_status)}</Tag>}
        </div>
      ),
    },
    {
      title: t("scheduleEnabled"),
      key: "enabled",
      render: (_: unknown, item: ScheduledTask) => (
        <Switch checked={item.enabled} loading={togglingId === item.id} onChange={(value) => toggle(item, value)} />
      ),
    },
    {
      title: t("scheduleActions"),
      key: "actions",
      render: (_: unknown, item: ScheduledTask) => (
        <Space size={4} wrap>
          <Button size="small" type="primary" icon={<PlayCircleOutlined />} loading={runningId === item.id} onClick={() => runNow(item)}>
            {t("scheduleRunNow")}
          </Button>
          <Tooltip title={t("scheduleEdit")}>
            <Button size="small" aria-label={t("scheduleEdit")} icon={<EditOutlined />} onClick={() => openEdit(item)} />
          </Tooltip>
          <Tooltip title={t("scheduleDelete")}>
            <Button size="small" danger aria-label={t("scheduleDelete")} icon={<DeleteOutlined />} onClick={() => remove(item)} />
          </Tooltip>
        </Space>
      ),
    },
  ];

  const runColumns = [
    { title: t("scheduleName"), dataIndex: "schedule_name", key: "schedule_name" },
    { title: t("scheduleTrigger"), dataIndex: "trigger_source", key: "trigger_source", render: (value: string) => value === "manual" ? t("scheduleManual") : t("scheduleScheduled") },
    { title: t("scheduleStatus"), dataIndex: "status", key: "status", render: (value: string) => <Tag color={statusColor(value)}>{enumLabel("taskStatus", value)}</Tag> },
    { title: t("scheduleTaskId"), dataIndex: "task_id", key: "task_id", render: (value: string | null) => value ? <Tooltip title={value}>{value.slice(0, 16)}</Tooltip> : "-" },
    { title: t("scheduleMessage"), dataIndex: "message", key: "message", ellipsis: true },
    { title: t("scheduleCreatedAt"), dataIndex: "created_at", key: "created_at", render: (value: string) => formatUtc(value) },
  ];

  return (
    <div className="scheduled-task-manager">
      <div className="schedule-manager-head">
        <div>
          <p className="panel-kicker">{t("scheduleTitle")}</p>
          <div className="schedule-summary">
            <span>{t("scheduleTotal")}: <strong>{schedules.length}</strong></span>
            <span>{t("scheduleEnabledCount")}: <strong>{schedules.filter((item) => item.enabled).length}</strong></span>
            {hasActiveRun && <Tag color="blue">{t("scheduleActive")}</Tag>}
            <Tag icon={<CalendarOutlined />}>{t("scheduleBackendRequired")}</Tag>
          </div>
        </div>
        <Space>
          <Tooltip title={t("scheduleRefresh")}>
            <Button aria-label={t("scheduleRefresh")} icon={<ReloadOutlined />} loading={loading} onClick={() => load()} />
          </Tooltip>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>{t("scheduleAdd")}</Button>
        </Space>
      </div>

      {error && <Alert type="error" showIcon closable message={error} onClose={() => setError(null)} />}

      <Table<ScheduledTask>
        rowKey="id" size="small" loading={loading} dataSource={schedules} columns={columns}
        pagination={false} scroll={{ x: 960 }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("scheduleNoSchedules")} /> }}
      />

      <section className="schedule-run-history">
        <div className="factor-section-title"><h3>{t("scheduleRunHistory")}</h3><Tag>{runs.length}</Tag></div>
        <Table<ScheduledTaskRun>
          rowKey="id" size="small" dataSource={runs} columns={runColumns}
          pagination={{ pageSize: 10, hideOnSinglePage: true }} scroll={{ x: 820 }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("scheduleNoRuns")} /> }}
        />
      </section>

      <Modal
        open={modalOpen} title={editing ? t("scheduleEdit") : t("scheduleAdd")}
        okText={t("scheduleSave")} cancelText={t("scheduleCancel")} confirmLoading={saving}
        onOk={save} onCancel={() => setModalOpen(false)} width={680}
      >
        <div className="schedule-form">
          <label><span>{t("scheduleName")}</span><Input value={draft.name} onChange={(event) => setDraft((value) => ({ ...value, name: event.target.value }))} /></label>
          <label>
            <span>{t("scheduleTaskType")}</span>
            <Select value={draft.task_type || undefined} options={definitions.map((item) => ({ value: item.task_type, label: item.name }))} onChange={changeTaskType} />
          </label>
          <label>
            <span>{t("scheduleFrequency")}</span>
            <Select
              value={draft.frequency}
              options={[
                { value: "daily", label: t("scheduleDaily") },
                { value: "weekly", label: t("scheduleWeekly") },
                { value: "interval", label: t("scheduleInterval") },
              ]}
              onChange={(value) => setDraft((current) => ({ ...current, frequency: value }))}
            />
          </label>
          {draft.frequency !== "interval" && (
            <label><span>{t("scheduleTime")}</span><Input type="time" value={draft.time_of_day} onChange={(event) => setDraft((value) => ({ ...value, time_of_day: event.target.value }))} /></label>
          )}
          {draft.frequency === "weekly" && (
            <label className="schedule-form-wide">
              <span>{t("scheduleWeekdays")}</span>
              <Checkbox.Group
                value={draft.weekdays}
                options={[0, 1, 2, 3, 4, 5, 6].map((v) => ({ value: v, label: t("scheduleWeekday" + v) }))}
                onChange={(values) => setDraft((current) => ({ ...current, weekdays: values.map(Number) }))}
              />
            </label>
          )}
          {draft.frequency === "interval" && (
            <label><span>{t("scheduleIntervalMinutes")}</span><InputNumber min={1} max={525600} value={draft.interval_minutes} onChange={(value) => setDraft((current) => ({ ...current, interval_minutes: Number(value ?? 60) }))} /></label>
          )}
          <label>
            <span>{t("scheduleTimezone")}</span>
            <Select
              value={draft.timezone}
              options={[{ value: "Asia/Shanghai", label: "Asia/Shanghai" }, { value: "UTC", label: "UTC" }]}
              onChange={(value) => setDraft((current) => ({ ...current, timezone: value }))}
            />
          </label>
          <label className="schedule-form-switch"><span>{t("scheduleEnabled")}</span><Switch checked={draft.enabled} onChange={(value) => setDraft((current) => ({ ...current, enabled: value }))} /></label>
          <label className="schedule-form-wide">
            <span>{t("schedulePayload")}</span>
            <Input.TextArea rows={9} value={draft.payloadText} onChange={(event) => setDraft((value) => ({ ...value, payloadText: event.target.value }))} />
          </label>
        </div>
      </Modal>
    </div>
  );
}
