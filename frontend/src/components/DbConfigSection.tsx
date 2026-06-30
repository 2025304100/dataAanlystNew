import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Form, Input, InputNumber, Progress, Radio, Space, Popconfirm, Tag } from "antd";
import { ApiOutlined, CheckCircleOutlined, CloseCircleOutlined, DatabaseOutlined, SyncOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { t, template } from "../i18n";
import { dbConfigApi } from "../api/dbConfig";
import type {
  DbConfig,
  HistoryInitializationStage,
  HistoryInitializationTask,
  MigrationProgress,
  MySQLConfig,
  TestConnectionResult,
} from "../types";

function safeErrorMessage(error: any): string {
  if (!error) return "Unknown error";
  if (typeof error === "string") return error;
  const msg = error.message ?? error.detail ?? error.toString();
  return typeof msg === "string" ? msg : JSON.stringify(msg);
}

const DEFAULT_MYSQL: MySQLConfig = {
  host: "127.0.0.1",
  port: 3306,
  database: "",
  user: "",
  password: "",
};

const HISTORY_PRESET_OPTIONS: Array<{ label: string; value: HistoryInitializationTask["preset"] }> = [
  { label: "\u8fd11\u4e2a\u6708", value: "1m" },
  { label: "1\u4e2a\u5b63\u5ea6", value: "1q" },
  { label: "\u8fd11\u5e74", value: "1y" },
  { label: "3\u5e74", value: "3y" },
];

const HISTORY_STAGE_LABELS: Record<string, string> = {
  prepare: "\u51c6\u5907\u6807\u7684",
  sync_bars: "\u8865\u9f50K\u7ebf",
  calc_scores: "\u8865\u9f50\u8bc4\u5206",
  finalize: "\u6c47\u603b\u5b8c\u6210",
};

const HISTORY_STATUS_LABELS: Record<string, string> = {
  idle: "\u5f85\u6267\u884c",
  running: "\u6267\u884c\u4e2d",
  completed: "\u5df2\u5b8c\u6210",
  failed: "\u5931\u8d25",
};

export default function DbConfigSection() {
  const [form] = Form.useForm();
  const [useMysql, setUseMysql] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(null);
  const [migrating, setMigrating] = useState(false);
  const [migration, setMigration] = useState<MigrationProgress | null>(null);
  const [historyPreset, setHistoryPreset] = useState<HistoryInitializationTask["preset"]>("1y");
  const [historyTask, setHistoryTask] = useState<HistoryInitializationTask | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const migrationPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const historyPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const clearHistoryPolling = useCallback(() => {
    if (historyPollRef.current) {
      clearInterval(historyPollRef.current);
      historyPollRef.current = null;
    }
  }, []);

  const startHistoryPolling = useCallback(() => {
    clearHistoryPolling();
    historyPollRef.current = setInterval(async () => {
      try {
        const status = await api.getHistoryInitializationStatus() as HistoryInitializationTask;
        setHistoryTask(status);
        if (status.status !== "running") {
          clearHistoryPolling();
        }
      } catch {
        clearHistoryPolling();
      }
    }, 1200);
  }, [clearHistoryPolling]);

  const loadConfig = useCallback(async () => {
    setLoading(true);
    try {
      const cfg = await dbConfigApi.get();
      setUseMysql(cfg.use_mysql);
      form.setFieldsValue(cfg.mysql);
    } catch {
      // noop
    } finally {
      setLoading(false);
    }
  }, [form]);

  const loadHistoryTask = useCallback(async () => {
    try {
      const status = await api.getHistoryInitializationStatus() as HistoryInitializationTask;
      setHistoryTask(status);
      if (status.status === "running") {
        setHistoryPreset(status.preset);
        startHistoryPolling();
      }
    } catch {
      // noop
    }
  }, [startHistoryPolling]);

  useEffect(() => {
    loadConfig();
    loadHistoryTask();
  }, [loadConfig, loadHistoryTask]);

  useEffect(() => () => {
    if (migrationPollRef.current) clearInterval(migrationPollRef.current);
    clearHistoryPolling();
  }, [clearHistoryPolling]);

  const handleSave = async () => {
    try {
      const values = form.getFieldsValue() as MySQLConfig;
      const config: DbConfig = { use_mysql: useMysql, mysql: values };
      setSaving(true);
      setTestResult(null);
      await dbConfigApi.update(config);
      window.location.reload();
    } catch (error: any) {
      setTestResult({ success: false, message: safeErrorMessage(error) });
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    try {
      const values = form.getFieldsValue() as MySQLConfig;
      const config: DbConfig = { use_mysql: true, mysql: values };
      setTesting(true);
      setTestResult(null);
      const result = await dbConfigApi.test(config);
      setTestResult(result);
    } catch (error: any) {
      setTestResult({ success: false, message: safeErrorMessage(error) });
    } finally {
      setTesting(false);
    }
  };

  const handleMigrate = async () => {
    try {
      setMigrating(true);
      setMigration({ status: "running", tables_done: 0, tables_total: 0, rows_migrated: 0 });
      await dbConfigApi.migrate();
      migrationPollRef.current = setInterval(async () => {
        try {
          const status = await dbConfigApi.migrationStatus();
          setMigration(status);
          if (status.status === "completed" || status.status === "failed") {
            if (migrationPollRef.current) clearInterval(migrationPollRef.current);
            migrationPollRef.current = null;
            setMigrating(false);
          }
        } catch {
          if (migrationPollRef.current) clearInterval(migrationPollRef.current);
          migrationPollRef.current = null;
          setMigrating(false);
        }
      }, 1000);
    } catch (error: any) {
      setMigration({ status: "failed", tables_done: 0, tables_total: 0, rows_migrated: 0, error: safeErrorMessage(error) });
      setMigrating(false);
    }
  };

  const handleStartHistoryInitialization = async () => {
    try {
      setHistoryLoading(true);
      const task = await api.startHistoryInitialization({ preset: historyPreset, adjust: "qfq" }) as HistoryInitializationTask;
      setHistoryTask(task);
      startHistoryPolling();
    } catch (error: any) {
      setHistoryTask((prev) => prev ? { ...prev, status: "failed", message: safeErrorMessage(error) } : null);
    } finally {
      setHistoryLoading(false);
    }
  };

  const migrationText = () => {
    if (!migration) return "";
    if (migration.status === "running") {
      return template(t("dbMigrating"), {
        done: migration.tables_done,
        total: migration.tables_total,
        rows: migration.rows_migrated,
      });
    }
    if (migration.status === "completed") {
      return template(t("dbMigrateSuccess"), { rows: migration.rows_migrated });
    }
    if (migration.status === "failed") {
      return template(t("dbMigrateFailed"), { error: migration.error || "Unknown" });
    }
    return "";
  };

  const migrationPercent = migration && migration.tables_total > 0
    ? Math.round((migration.tables_done / migration.tables_total) * 100)
    : 0;

  const renderHistoryStage = (stage: HistoryInitializationStage) => {
    const percent = stage.total > 0 ? Math.round((stage.done / stage.total) * 100) : stage.status === "completed" ? 100 : 0;
    const progressStatus = stage.status === "failed"
      ? "exception"
      : stage.status === "completed"
        ? "success"
        : stage.status === "running"
          ? "active"
          : "normal";

    return (
      <div key={stage.key} className="history-init-stage">
        <div className="history-init-stage__head">
          <strong>{HISTORY_STAGE_LABELS[stage.key] || stage.key}</strong>
          <span>
            {stage.total > 0 ? `${stage.done}/${stage.total}` : stage.status === "completed" ? "\u5df2\u5b8c\u6210" : "\u7b49\u5f85\u4e2d"}
          </span>
        </div>
        <Progress percent={percent} size="small" status={progressStatus as any} showInfo={false} />
        {stage.message && <div className="history-init-stage__message">{stage.message}</div>}
      </div>
    );
  };

  return (
    <div className="settings-db-section">
      <div className="panel">
        <div className="panel-head">
          <div>
            <p className="panel-kicker"><DatabaseOutlined style={{ marginRight: 6 }} />{t("dbCurrentType")}</p>
            <h2>{t("dbTabTitle")}</h2>
          </div>
          <Tag color={useMysql ? "blue" : "green"} style={{ fontSize: 13, padding: "2px 10px" }}>
            {useMysql ? "MySQL" : "SQLite"}
          </Tag>
        </div>

        <Alert type="info" showIcon message={t("dbSwitchWarning")} style={{ marginBottom: 20 }} />

        <Form layout="vertical" form={form} initialValues={DEFAULT_MYSQL}>
          <Form.Item label={null} style={{ marginBottom: 16 }}>
            <Radio.Group
              value={useMysql}
              onChange={(e) => {
                setUseMysql(e.target.value);
                setTestResult(null);
              }}
              disabled={loading}
              size="large"
            >
              <Radio.Button value={false}>
                <DatabaseOutlined style={{ marginRight: 4 }} />
                {t("dbLocalMode")}
              </Radio.Button>
              <Radio.Button value={true}>
                <ApiOutlined style={{ marginRight: 4 }} />
                {t("dbRemoteMode")}
              </Radio.Button>
            </Radio.Group>
          </Form.Item>

          {useMysql && (
            <div className="db-mysql-form" style={{
              background: "rgba(0,0,0,0.02)",
              borderRadius: 8,
              padding: "16px 20px",
              marginBottom: 16,
            }}>
              <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 12 }}>
                <Form.Item label={t("dbHost")} name="host" rules={[{ required: true }]}>
                  <Input placeholder="127.0.0.1" />
                </Form.Item>
                <Form.Item label={t("dbPort")} name="port" rules={[{ required: true }]}>
                  <InputNumber min={1} max={65535} style={{ width: "100%" }} />
                </Form.Item>
              </div>

              <Form.Item label={t("dbName")} name="database" rules={[{ required: true }]}>
                <Input placeholder="quant_workbench" />
              </Form.Item>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <Form.Item label={t("dbUser")} name="user" rules={[{ required: true }]}>
                  <Input placeholder="root" />
                </Form.Item>
                <Form.Item label={t("dbPassword")} name="password">
                  <Input.Password placeholder="password" />
                </Form.Item>
              </div>

              <Space style={{ marginTop: 4 }}>
                <Button type="default" loading={testing} onClick={handleTest} icon={<SyncOutlined />}>
                  {t("dbTestConn")}
                </Button>
                {testResult && (
                  <span style={{
                    color: testResult.success ? "#0f766e" : "#b42318",
                    fontSize: 13,
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                  }}>
                    {testResult.success
                      ? <><CheckCircleOutlined /> {template(t("dbTestSuccess"), { version: testResult.server_version || "" })}</>
                      : <><CloseCircleOutlined /> {template(t("dbTestFailed"), { message: testResult.message })}</>
                    }
                  </span>
                )}
              </Space>
            </div>
          )}

          <div style={{ marginBottom: 20 }}>
            <Button
              type="primary"
              size="large"
              loading={saving}
              onClick={handleSave}
              disabled={useMysql && (!form.getFieldValue("host") || !form.getFieldValue("database") || !form.getFieldValue("user"))}
            >
              {t("dbSave")}
            </Button>
          </div>

          {useMysql && (
            <div style={{ borderTop: "1px solid rgba(0,0,0,0.06)", paddingTop: 16, marginBottom: 20 }}>
              <Popconfirm
                title={t("dbMigrateConfirm")}
                onConfirm={handleMigrate}
                okText="OK"
                cancelText="Cancel"
                disabled={migrating}
              >
                <Button
                  type="primary"
                  ghost
                  size="large"
                  loading={migrating}
                  icon={<SyncOutlined spin={migrating} />}
                  disabled={migrating}
                >
                  {t("dbMigrate")}
                </Button>
              </Popconfirm>

              {migration && migration.status !== "idle" && (
                <div style={{ marginTop: 16, maxWidth: 500 }}>
                  <Progress
                    percent={migrationPercent}
                    status={migration.status === "failed" ? "exception" : migration.status === "completed" ? "success" : "active"}
                    strokeColor={migration.status === "failed" ? "#b42318" : "#0f766e"}
                  />
                  <div style={{
                    fontSize: 13,
                    color: migration.status === "failed" ? "#b42318" : migration.status === "completed" ? "#0f766e" : "#6b7280",
                    marginTop: 6,
                  }}>
                    {migrationText()}
                    {migration.status === "running" && migration.current_table && (
                      <div style={{ fontSize: 12, color: "#9ca3af", marginTop: 2 }}>
                        {"\u5f53\u524d"}: {migration.current_table}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
        </Form>
      </div>

      <div className="panel history-init-panel">
        <div className="panel-head">
          <div>
            <p className="panel-kicker">{"\u5386\u53f2\u6570\u636e\u521d\u59cb\u5316"}</p>
            <h2>{"\u521d\u59cb\u5316\u8865\u6570"}</h2>
          </div>
          <Tag color={historyTask?.status === "completed" ? "green" : historyTask?.status === "failed" ? "red" : historyTask?.status === "running" ? "blue" : "default"}>
            {HISTORY_STATUS_LABELS[historyTask?.status || "idle"]}
          </Tag>
        </div>

        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={"\u70b9\u51fb\u521d\u59cb\u5316\u540e\uff0c\u4f1a\u5148\u8865\u9f50\u5386\u53f2K\u7ebf\uff0c\u518d\u6309\u533a\u95f4\u8865\u9f50\u5386\u53f2\u8bc4\u5206\u3002\u9ed8\u8ba4\u8fd11\u5e74\uff0c\u53ef\u9009\u8fd11\u4e2a\u6708\u30011\u4e2a\u5b63\u5ea6\u30013\u5e74\u3002"}
        />

        <div className="history-init-toolbar">
          <Radio.Group
            value={historyPreset}
            onChange={(e) => setHistoryPreset(e.target.value)}
            optionType="button"
            buttonStyle="solid"
          >
            {HISTORY_PRESET_OPTIONS.map((item) => (
              <Radio.Button key={item.value} value={item.value}>{item.label}</Radio.Button>
            ))}
          </Radio.Group>
          <Button
            type="primary"
            icon={<SyncOutlined spin={historyTask?.status === "running"} />}
            loading={historyLoading}
            disabled={historyTask?.status === "running"}
            onClick={handleStartHistoryInitialization}
          >
            {"\u521d\u59cb\u5316"}
          </Button>
        </div>

        {historyTask && historyTask.status !== "idle" && (
          <div className="history-init-progress-wrap">
            <div className="history-init-progress-head">
              <strong>{"\u603b\u4f53\u8fdb\u5ea6"}</strong>
              <span>{historyTask.progress_pct}%</span>
            </div>
            <Progress
              percent={historyTask.progress_pct}
              status={historyTask.status === "failed" ? "exception" : historyTask.status === "completed" ? "success" : "active"}
            />
            {historyTask.message && <div className="history-init-status-text">{historyTask.message}</div>}

            <div className="history-init-summary-grid">
              <div>
                <span>{"\u6807\u7684\u603b\u6570"}</span>
                <strong>{historyTask.summary.symbols_total}</strong>
              </div>
              <div>
                <span>{"K\u7ebf\u8865\u9f50"}</span>
                <strong>{historyTask.summary.sync_ok_count}</strong>
              </div>
              <div>
                <span>{"K\u7ebf\u5931\u8d25"}</span>
                <strong>{historyTask.summary.sync_failed_count}</strong>
              </div>
              <div>
                <span>{"\u8bc4\u5206\u5b8c\u6210"}</span>
                <strong>{historyTask.summary.score_days_completed}/{historyTask.summary.score_days_total}</strong>
              </div>
            </div>

            <div className="history-init-stage-list">
              {historyTask.stages.map(renderHistoryStage)}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}