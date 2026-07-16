import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Col, Empty, Modal, Progress, Row, Select, Space, Statistic, Table, Tag, Typography, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import ReactECharts from "echarts-for-react";
import { ReloadOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import type { MacroIndicator, MacroOverview } from "../types";
import { t, template } from "../i18n";

const { Paragraph, Text } = Typography;

function indicatorName(row: MacroIndicator) {
  const key = "macro_indicator_" + row.indicator_key;
  const translated = t(key);
  return translated === key ? row.name : translated;
}

function indicatorTip(key: string): { text: string; bench: string } {
  const tipKey = "macro_tip_" + key;
  const benchKey = "macro_bench_" + key;
  const text = t(tipKey);
  const bench = t(benchKey);
  return {
    text: text === tipKey ? "" : text,
    bench: bench === benchKey ? "" : bench,
  };
}

// 趋势箭头渲染
function trendArrow(delta: number | null | undefined) {
  if (delta == null || Number.isNaN(delta)) return null;
  const abs = Math.abs(delta);
  let display: string;
  if (abs >= 100000000) {
    display = (delta / 100000000).toFixed(2) + t("macro_unit_yi");
  } else if (abs >= 10000) {
    display = (delta / 10000).toFixed(2) + t("macro_unit_wan");
  } else {
    display = delta.toFixed(2);
  }
  if (delta > 0) return <span style={{ color: "#b42318", marginLeft: 4 }}>&#8593;{display}</span>;
  if (delta < 0) return <span style={{ color: "#0f766e", marginLeft: 4 }}>&#8595;{display}</span>;
  return <span style={{ color: "#6b7280", marginLeft: 4 }}>=0</span>;
}

function fmt(value: number | null | undefined, unit?: string | null) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  const abs = Math.abs(value);
  if (unit === "CNY") return (value / 100000000).toFixed(2) + t("macro_unit_yi_yuan");
  if (unit === "CNY 100M") return value.toFixed(0) + t("macro_unit_yi_yuan");
  if (unit === "10k people") return value.toFixed(1) + t("macro_unit_wan_ren");
  const rendered = abs >= 10000 ? (value / 10000).toFixed(2) + t("macro_unit_wan") : value.toFixed(abs >= 100 ? 0 : 2);
  return unit ? rendered + unit : rendered;
}

function scoreColor(score: number) {
  if (score >= 68) return "#0f766e";
  if (score <= 42) return "#b42318";
  return "#d97706";
}

function statusColor(status: string) {
  if (status === "positive") return "success";
  if (status === "negative") return "error";
  return "default";
}

function localizeIndicatorTaskName(value: string | null | undefined): string {
  if (!value) return "";
  const key = "macro_indicator_" + value;
  const translated = t(key);
  return translated === key ? value : translated;
}

function formatTaskProgressTitle(
  task: { processed?: number | null; total?: number | null },
): string {
  const processed = task.processed ?? 0;
  const total = task.total ?? 0;
  if (total <= 0) return t("macro_syncing");
  return template("macro_task_progress_title", { processed, total });
}

function formatTaskProgressSummary(
  task: { processed?: number | null; total?: number | null; ok_count?: number | null; failed_count?: number | null; current_item?: string | null },
): string {
  const processed = task.processed ?? 0;
  const total = task.total ?? 0;
  const okCount = task.ok_count ?? 0;
  const failedCount = task.failed_count ?? 0;
  const currentItem = localizeIndicatorTaskName(task.current_item);
  const params = { processed, total, okCount, failedCount, currentItem };
  if (!currentItem) return template("macro_task_progress_summary", params);
  return template("macro_task_progress_summary_with_current", params);
}

type FailureDetail = {
  key: string;
  name: string;
  scope: string;
  detail: string;
};

function toDisplayText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function normalizeFailureDetails(items: unknown, fallbackScope = "macro", fallbackMessage?: string): FailureDetail[] {
  if (!Array.isArray(items) || items.length === 0) {
    if (!fallbackMessage) return [];
    return [{
      key: `${fallbackScope}-fallback`,
      name: fallbackScope,
      scope: fallbackScope,
      detail: fallbackMessage,
    }];
  }
  return items.map((item, index) => {
    if (item && typeof item === "object") {
      const row = item as Record<string, unknown>;
      const name =
        toDisplayText(row.name) ||
        toDisplayText(row.indicator_key) ||
        toDisplayText(row.scope) ||
        toDisplayText(row.source) ||
        `${fallbackScope}-${index + 1}`;
      const scope =
        toDisplayText(row.scope) ||
        toDisplayText(row.source) ||
        toDisplayText(row.region) ||
        fallbackScope;
      const detail =
        toDisplayText(row.error) ||
        toDisplayText(row.message) ||
        toDisplayText(row.reason) ||
        toDisplayText(row.detail) ||
        toDisplayText(row.status) ||
        fallbackMessage ||
        "-";
      const key =
        toDisplayText(row.indicator_key) ||
        toDisplayText(row.key) ||
        toDisplayText(row.name) ||
        `${fallbackScope}-${index}`;
      return { key: `${key}-${index}`, name, scope, detail };
    }
    const text = toDisplayText(item) || fallbackMessage || "-";
    return { key: `${fallbackScope}-${index}`, name: text, scope: fallbackScope, detail: text };
  });
}

function failurePreview(items: FailureDetail[]) {
  return items.slice(0, 3).map((item) => (
    item.detail && item.detail !== item.name ? `${item.name}: ${item.detail}` : item.name
  )).join(" / ");
}

export default function MacroData() {
  const ctx = useApp();
  const [region, setRegion] = useState<"all" | "cn" | "us">("all");
  const [overview, setOverview] = useState<MacroOverview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [updateTask, setUpdateTask] = useState<any | null>(null);
  const pollRef = useRef<ReturnType<typeof window.setInterval> | null>(null);
  const [selectedIndicator, setSelectedIndicator] = useState<MacroIndicator | null>(null);
  const [history, setHistory] = useState<MacroIndicator[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [taskNotice, setTaskNotice] = useState<string | null>(null);
  const [taskNoticeType, setTaskNoticeType] = useState<"success" | "info" | "warning" | "error" | null>(null);
  const [taskFailureDetails, setTaskFailureDetails] = useState<FailureDetail[]>([]);
  const [failureDetailOpen, setFailureDetailOpen] = useState(false);
  const terminalNoticeRef = useRef<string | null>(null);

  const stopPolling = () => {
    if (pollRef.current) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const notifyTaskOutcome = (taskId: string, status: string, type: "success" | "info" | "warning" | "error", text: string) => {
    const key = `${taskId}:${status}:${text}`;
    if (!text || terminalNoticeRef.current === key) return;
    terminalNoticeRef.current = key;
    if (type === "success") ctx.showToast("success", text);
    else if (type === "error") ctx.showToast("error", text);
    else ctx.showToast("info", text);
  };

  const load = async (nextRegion = region) => {
    setError(null);
    try {
      const data = await api.getMacroOverview(nextRegion);
      setOverview(data);
    } catch (err: any) {
      setError(err.message);
    }
  };

  useEffect(() => {
    load(region);
  }, [region]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const task = await api.getLatestMacroUpdateTask();
        if (!alive || !task || !["queued", "running"].includes(task.status)) return;
        setUpdateTask(task);
        setLoading(true);
      } catch {
        // ignore resume failure
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (!updateTask || !["queued", "running"].includes(updateTask.status)) {
      stopPolling();
      return;
    }
    pollRef.current = window.setInterval(async () => {
      try {
        const next = await api.getMacroUpdateTask(updateTask.id);
        setUpdateTask(next);
        if (!["queued", "running"].includes(next.status)) {
          stopPolling();
        }
        if (next.status === "done") {
          const failures = normalizeFailureDetails(next.result?.failed, next.result?.region ?? "macro");
          const notice = failures.length ? `${t("macro_task_partial")} (${failures.length})` : t("macro_task_done");
          setTaskFailureDetails(failures);
          setTaskNotice(notice);
          setTaskNoticeType(failures.length ? "warning" : "success");
          setError(null);
          notifyTaskOutcome(next.id, next.status, failures.length ? "warning" : "success", notice);
          await load(region);
          setLoading(false);
        } else if (next.status === "failed") {
          const failures = normalizeFailureDetails(next.errors, "macro", next.message || t("macro_task_failed"));
          const notice = next.message || t("macro_task_failed");
          setTaskFailureDetails(failures);
          setTaskNotice(notice);
          setTaskNoticeType("error");
          setError(null);
          notifyTaskOutcome(next.id, next.status, "error", notice);
          setLoading(false);
        } else if (next.status === "cancelled") {
          setTaskFailureDetails([]);
          setTaskNotice(t("macro_task_cancelled"));
          setTaskNoticeType("info");
          setError(null);
          notifyTaskOutcome(next.id, next.status, "info", t("macro_task_cancelled"));
          setLoading(false);
        }
      } catch (err: any) {
        stopPolling();
        setError(err.message);
        setLoading(false);
      }
    }, 2500);
    return () => {
      stopPolling();
    };
  }, [ctx.locale, region, updateTask?.id, updateTask?.status]);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    setTaskNotice(null);
    setTaskNoticeType(null);
    setTaskFailureDetails([]);
    setFailureDetailOpen(false);
    terminalNoticeRef.current = null;
    try {
      const task = await api.startMacroUpdateTask({ region });
      setUpdateTask(task);
      if (task.status === "done") {
        const failures = normalizeFailureDetails(task.result?.failed, task.result?.region ?? "macro");
        const notice = failures.length ? `${t("macro_task_partial")} (${failures.length})` : t("macro_task_done");
        setTaskFailureDetails(failures);
        setTaskNotice(notice);
        setTaskNoticeType(failures.length ? "warning" : "success");
        notifyTaskOutcome(task.id, task.status, failures.length ? "warning" : "success", notice);
        await load(region);
        setLoading(false);
      }
    } catch (err: any) {
      setError(err.message);
      setLoading(false);
    }
  };

  const cancelRefresh = async () => {
    if (!updateTask?.id) return;
    try {
      const task = await api.cancelMacroUpdateTask(updateTask.id);
      setUpdateTask(task);
      stopPolling();
      setTaskFailureDetails([]);
      setTaskNotice(t("macro_task_cancelled"));
      setTaskNoticeType("info");
      setError(null);
      notifyTaskOutcome(task.id, task.status, "info", t("macro_task_cancelled"));
      setLoading(false);
    } catch (err: any) {
      setError(err.message);
    }
  };

  const openHistory = async (row: MacroIndicator) => {
    setSelectedIndicator(row);
    setHistoryLoading(true);
    try {
      const rows = await api.getMacroIndicatorHistory(row.region, row.indicator_key, 80);
      setHistory(rows);
    } catch (err: any) {
      setError(err.message);
      setHistory([]);
    } finally {
      setHistoryLoading(false);
    }
  };
  const snapshot = overview?.snapshot ?? null;
  const overviewFailureDetails = useMemo(
    () => normalizeFailureDetails(overview?.failed, region),
    [overview?.failed, region],
  );
  const activeFailureDetails = taskFailureDetails.length ? taskFailureDetails : overviewFailureDetails;
  const dimensionRows = useMemo(() => {
    if (!snapshot) return [];
    return [
      { key: "growth", value: snapshot.growth_score },
      { key: "inflation", value: snapshot.inflation_score },
      { key: "liquidity", value: snapshot.liquidity_score },
      { key: "credit", value: snapshot.credit_score },
      { key: "risk", value: snapshot.risk_score },
    ];
  }, [snapshot]);

  const radarOption = useMemo(() => {
    const values = dimensionRows.map((row) => Math.max(0, Math.min(100, 50 + row.value * 2.5)));
    return {
      tooltip: {},
      radar: {
        radius: "68%",
        indicator: dimensionRows.map((row) => ({ name: t("macro_" + row.key), max: 100 })),
        splitNumber: 4,
      },
      series: [
        {
          type: "radar",
          data: [{ value: values, name: t("macro_market_score"), areaStyle: { opacity: 0.18 } }],
          lineStyle: { color: "#0f766e", width: 2 },
          itemStyle: { color: "#0f766e" },
        },
      ],
    };
  }, [dimensionRows, ctx.locale]);

  const historyOption = useMemo(() => {
    const data = history.filter((row) => row.value !== null && row.value !== undefined);
    return {
      tooltip: { trigger: "axis" },
      grid: { left: 48, right: 18, top: 24, bottom: 44 },
      xAxis: { type: "category", data: data.map((row) => row.period) },
      yAxis: { type: "value", scale: true },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18 }],
      series: [{ type: "line", smooth: true, showSymbol: data.length <= 24, data: data.map((row) => row.value), lineStyle: { color: "#0f766e", width: 2 }, itemStyle: { color: "#0f766e" }, areaStyle: { color: "rgba(15, 118, 110, 0.10)" } }],
    };
  }, [history]);

  const historyColumns: ColumnsType<MacroIndicator> = [
    { title: t("macro_period"), dataIndex: "period", width: 140 },
    { title: t("macro_value"), dataIndex: "value", render: (_, row) => fmt(row.value, row.unit), align: "right" },
    { title: t("macro_previous"), dataIndex: "previous_value", render: (_, row) => fmt(row.previous_value, row.unit), align: "right" },
    { title: t("macro_delta"), dataIndex: "delta", render: (value) => fmt(value, null), align: "right" },
    { title: t("macro_score"), dataIndex: "score", render: (value: number) => value.toFixed(1), align: "right" },
  ];
  const failureColumns: ColumnsType<FailureDetail> = [
    { title: t("macro_failure_item"), dataIndex: "name", width: 220 },
    { title: t("macro_failure_scope"), dataIndex: "scope", width: 140 },
    {
      title: t("macro_failure_detail"),
      dataIndex: "detail",
      render: (value: string) => <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{value || "-"}</span>,
    },
  ];
  const columns: ColumnsType<MacroIndicator> = [
    {
      title: t("macro_category"),
      dataIndex: "category",
      render: (value) => {
        const catColor: Record<string, string> = {
          growth: "#0f766e", inflation: "#b45309", liquidity: "#2563eb",
          credit: "#7c3aed", risk: "#dc2626",
        };
        return <Tag color={catColor[value] ?? "default"} style={{ fontWeight: 600, fontSize: 11 }}>{t("macro_" + value)}</Tag>;
      },
      width: 110,
    },
    {
      title: t("macro_title"),
      dataIndex: "name",
      width: 240,
      render: (_, row) => {
        const { text: tipText, bench } = indicatorTip(row.indicator_key);
        const name = indicatorName(row);
        if (tipText) {
          return (
            <Tooltip
              title={<div className="macro__tip-content">
                <div className="macro__tip-title">{name}</div>
                <div className="macro__tip-body">{tipText}</div>
                {bench && (
                  <div className="macro__tip-bench">
                    <span className="macro__bench-label">
                      {t("macro_benchmark")}
                    </span>
                    <span className="macro__bench-value">{bench}</span>
                  </div>
                )}
              </div>}
              classNames={{ root: "macro-tip-overlay" }}
              placement="topLeft"
              arrow={{ pointAtCenter: true }}
            >
              <span className="macro__indicator-name">
                <span className="macro__info-icon">i</span>
                {name}
              </span>
            </Tooltip>
          );
        }
        return <span>{name}</span>;
      },
    },
    { title: t("macro_period"), dataIndex: "period", width: 120 },
    {
      title: t("macro_value"),
      dataIndex: "value",
      render: (_, row) => (
        <span>
          {fmt(row.value, row.unit)}
          {trendArrow(row.delta)}
        </span>
      ),
      align: "right",
      width: 130,
    },
    {
      title: t("macro_previous"),
      dataIndex: "previous_value",
      render: (_, row) => fmt(row.previous_value, row.unit),
      align: "right",
      width: 120,
    },
    {
      title: t("macro_delta"),
      dataIndex: "delta",
      render: (value, _row) => (
        <span>
          {fmt(value, _row?.unit ?? null)}
          {trendArrow(value)}
        </span>
      ),
      align: "right",
      width: 100,
    },
    {
      title: t("macro_score"),
      dataIndex: "score",
      render: (value: number) => <Text strong style={{ color: value >= 0 ? "#0f766e" : "#b42318" }}>{value.toFixed(1)}</Text>,
      align: "right",
      width: 90,
      sorter: (a, b) => a.score - b.score,
      defaultSortOrder: "descend",
    },
    {
      title: t("macro_status"),
      dataIndex: "status",
      render: (value: string) => <Tag color={statusColor(value)}>{t("macro_" + value)}</Tag>,
      width: 100,
    },
  ];

  return (
    <div className="macro-page">
      <section className="macro-hero">
        <div>
          <p className="panel-kicker">{t("macro_title")}</p>
          <h1>{t("macro_title")}</h1>
          <p>{t("macro_subtitle")}</p>
        </div>
        <Space wrap>
          <Select
            value={region}
            onChange={(value) => setRegion(value)}
            style={{ width: 140 }}
            options={[
              { value: "all", label: t("macro_all") },
              { value: "cn", label: t("macro_cn") },
              { value: "us", label: t("macro_us") },
            ]}
          />
          <Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={refresh}>
            {loading ? t("macro_syncing") : t("macro_update")}
          </Button>
          {updateTask && ["queued", "running"].includes(updateTask.status) ? (
            <Button onClick={cancelRefresh}>{t("macro_task_cancel")}</Button>
          ) : null}
        </Space>
      </section>

      {updateTask && ["queued", "running"].includes(updateTask.status) ? (
        <Alert
          type="info"
          showIcon
          className="macro-alert"
          message={t("macro_task_running")}
          description={
            <div>
              <div style={{ marginBottom: 8 }}>{formatTaskProgressTitle(updateTask)}</div>
              <Progress percent={Math.round(updateTask.percent ?? 0)} status="active" />
              <div style={{ marginTop: 8, fontSize: 12, color: "var(--text-muted, #888)" }}>
                {formatTaskProgressSummary(updateTask)}
              </div>
            </div>
          }
        />
      ) : null}

      {taskNotice && taskNoticeType ? (
        <Alert
          type={taskNoticeType}
          showIcon
          className="macro-alert"
          message={taskNotice}
          description={
            taskFailureDetails.length ? (
              <Space direction="vertical" size={8}>
                <span>{failurePreview(taskFailureDetails)}</span>
                <Button size="small" onClick={() => setFailureDetailOpen(true)}>
                  {t("macro_view_failures")}
                </Button>
              </Space>
            ) : undefined
          }
        />
      ) : null}

      {error && <Alert type="error" message={error} showIcon className="macro-alert" />}

      {!snapshot ? (
        <Card className="macro-empty">
          <Empty description={t("macro_empty")}>
            <Button type="primary" loading={loading} onClick={refresh}>{t("macro_update")}</Button>
          </Empty>
        </Card>
      ) : (
        <>
          <Row gutter={[12, 12]} className="macro-score-row">
            <Col xs={24} md={8}>
              <Card>
                <Statistic
                  title={t("macro_market_score")}
                  value={snapshot.market_score}
                  precision={1}
                  valueStyle={{ color: scoreColor(snapshot.market_score) }}
                />
                <Progress percent={Math.round(snapshot.market_score)} strokeColor={scoreColor(snapshot.market_score)} showInfo={false} />
              </Card>
            </Col>
            <Col xs={12} md={4}>
              <Card><Statistic title={t("macro_stance")} value={t("macro_" + snapshot.stance)} /></Card>
            </Col>
            <Col xs={12} md={4}>
              <Card><Statistic title={t("macro_indicators")} value={snapshot.indicators_total} /></Card>
            </Col>
            <Col xs={12} md={4}>
              <Card><Statistic title={t("macro_failed")} value={snapshot.failed_total} /></Card>
            </Col>
            <Col xs={12} md={4}>
              <Card><Statistic title={t("macro_updated")} value={snapshot.created_at ? new Date(snapshot.created_at).toLocaleDateString() : "-"} /></Card>
            </Col>
          </Row>

          {/* 市场脉搏摘要 */}
          <Card className="macro-pulse-card" title={t("macro_market_pulse")}>
            <div className="macro__pulse-grid">
              {dimensionRows.map((dim) => {
                const dimIcon: Record<string, string> = {
                  growth: "\ud83d\udcca", inflation: "\ud83d\udd25", liquidity: "\ud83d\udcb0",
                  credit: "\ud83d\udcc8", risk: "\u26a0\ufe0f",
                };
                const val = Math.max(0, Math.min(100, 50 + dim.value * 2.5));
                const level = val >= 68 ? "good" : val <= 42 ? "bad" : "neutral";
                return (
                  <div key={dim.key} className={`macro__pulse-item macro__pulse--${level}`}>
                    <span className="macro__pulse-icon">{dimIcon[dim.key] ?? "\u2022"}</span>
                    <div className="macro__pulse-info">
                      <strong>{t("macro_" + dim.key)}</strong>
                      <div className="macro__pulse-bar">
                        <div className="macro__pulse-fill" style={{ width: `${val}%` }} />
                      </div>
                      <span className="macro__pulse-score">{val.toFixed(0)}</span>
                    </div>
                  </div>
                );
              })}
            </div>
            {snapshot.stance && (
              <div className={`macro__stance-banner macro__stance--${snapshot.stance}`}>
                <span className="macro__stance-label">
                  {t("macro_market_mode_" + snapshot.stance)}
                </span>
                <span className="macro__stance-hint">
                  {t("macro_stance_hint_" + snapshot.stance)}
                </span>
              </div>
            )}
          </Card>

          <Row gutter={[12, 12]} className="macro-main-row">
            <Col xs={24} lg={10}>
              <Card title={t("macro_radar")} className="macro-card">
                <ReactECharts option={radarOption} style={{ height: 320 }} />
              </Card>
            </Col>
            <Col xs={24} lg={14}>
              <Card title={t("macro_brief")} className="macro-card">
                <div className="macro-brief">
                  {(overview?.brief ?? []).map((line, index) => (
                    <Paragraph key={index}>{line}</Paragraph>
                  ))}
                </div>
                {activeFailureDetails.length ? (
                  <Alert
                    type="warning"
                    showIcon
                    message={`${t("macro_failed")}: ${activeFailureDetails.length}`}
                    description={
                      <Space direction="vertical" size={8}>
                        <span>{failurePreview(activeFailureDetails)}</span>
                        <Button size="small" onClick={() => setFailureDetailOpen(true)}>
                          {t("macro_view_failures")}
                        </Button>
                      </Space>
                    }
                  />
                ) : null}
              </Card>
            </Col>
          </Row>

          <Card title={t("macro_table")} extra={<Text type="secondary">{t("macro_click_hint")}</Text>} className="macro-card">
            <Table
              rowKey={(row) => `${row.region}-${row.indicator_key}`}
              columns={columns}
              dataSource={overview?.indicators ?? []}
              pagination={false}
              scroll={{ x: 960 }}
              size="middle"
              onRow={(record) => ({ onClick: () => openHistory(record) })}
              rowClassName="macro-clickable-row"
            />
          </Card>
        </>
      )}
      <Modal
        open={failureDetailOpen}
        title={t("macro_failure_details")}
        onCancel={() => setFailureDetailOpen(false)}
        footer={null}
        width={860}
        destroyOnHidden
      >
        {activeFailureDetails.length ? (
          <Table
            rowKey="key"
            columns={failureColumns}
            dataSource={activeFailureDetails}
            pagination={{ pageSize: 8, size: "small" }}
            scroll={{ x: 720 }}
            size="small"
          />
        ) : (
          <Empty description={t("macro_failure_empty")} />
        )}
      </Modal>
      <Modal
        open={!!selectedIndicator}
        title={selectedIndicator ? indicatorName(selectedIndicator) : t("macro_history_title")}
        onCancel={() => setSelectedIndicator(null)}
        footer={null}
        width={920}
        destroyOnHidden
      >
        {history.length === 0 && !historyLoading ? (
          <Empty description={t("macro_no_history")} />
        ) : (
          <>
            <Card title={t("macro_history_chart")} size="small" className="macro-history-card">
              <ReactECharts option={historyOption} showLoading={historyLoading} style={{ height: 300 }} />
            </Card>
            <Table
              rowKey={(row) => row.region + "-" + row.indicator_key + "-" + row.period}
              columns={historyColumns}
              dataSource={[...history].reverse()}
              pagination={{ pageSize: 8, size: "small" }}
              size="small"
              scroll={{ x: 720 }}
            />
          </>
        )}
      </Modal>
    </div>
  );
}
