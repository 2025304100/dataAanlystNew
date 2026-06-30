import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Form, Input, InputNumber, Popconfirm, Progress, Radio, Space, Tag } from "antd";
import { ApiOutlined, CheckCircleOutlined, CloseCircleOutlined, DatabaseOutlined, SyncOutlined } from "@ant-design/icons";
import { t, template } from "../i18n";
import { dbConfigApi } from "../api/dbConfig";
import type { DbConfig, MigrationProgress, MySQLConfig, TestConnectionResult } from "../types";

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

export default function DbConfigSection() {
  const [form] = Form.useForm();
  const [useMysql, setUseMysql] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(null);
  const [migrating, setMigrating] = useState(false);
  const [migration, setMigration] = useState<MigrationProgress | null>(null);
  const migrationPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

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

  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  useEffect(() => () => {
    if (migrationPollRef.current) {
      clearInterval(migrationPollRef.current);
    }
  }, []);

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
            if (migrationPollRef.current) {
              clearInterval(migrationPollRef.current);
            }
            migrationPollRef.current = null;
            setMigrating(false);
          }
        } catch {
          if (migrationPollRef.current) {
            clearInterval(migrationPollRef.current);
          }
          migrationPollRef.current = null;
          setMigrating(false);
        }
      }, 1000);
    } catch (error: any) {
      setMigration({ status: "failed", tables_done: 0, tables_total: 0, rows_migrated: 0, error: safeErrorMessage(error) });
      setMigrating(false);
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
            <div
              className="db-mysql-form"
              style={{
                background: "rgba(0,0,0,0.02)",
                borderRadius: 8,
                padding: "16px 20px",
                marginBottom: 16,
              }}
            >
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
                  <span
                    style={{
                      color: testResult.success ? "#0f766e" : "#b42318",
                      fontSize: 13,
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                    }}
                  >
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
                  <div
                    style={{
                      fontSize: 13,
                      color: migration.status === "failed" ? "#b42318" : migration.status === "completed" ? "#0f766e" : "#6b7280",
                      marginTop: 6,
                    }}
                  >
                    {migrationText()}
                    {migration.status === "running" && migration.current_table && (
                      <div style={{ fontSize: 12, color: "#9ca3af", marginTop: 2 }}>
                        {t("histDbCurrent")}: {migration.current_table}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
        </Form>
      </div>
    </div>
  );
}
