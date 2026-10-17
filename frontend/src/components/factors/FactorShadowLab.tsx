import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Descriptions, Empty, Progress, Select, Space, Spin, Table, Tag, Tooltip, Typography } from "antd";
import { CheckCircleOutlined, CloseCircleOutlined, ExclamationCircleOutlined, InfoCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import ReactECharts from "echarts-for-react";
import { t } from "../../i18n";
import { api, type ShadowObservationRead, type ShadowObservationSummary, type ShadowHealthReport } from "../../api/client";
import { useUnsupportedFeatures } from "../settings/UnsupportedFeaturesToggle";
import { useFactorLabSelection, type FactorLabSelectionProps } from "./useFactorLabSelection";
import "./FactorShadowLab.css";

const { Text } = Typography;
function healthColor(status: string | null | undefined) {
  return ({ healthy: "green", degraded: "orange", blocked: "red" } as Record<string, string>)[status || ""] || "default";
}
function healthLabel(status: string | null | undefined) {
  if (!status) return "-";
  const key = `shadowLabHealth_${status}`;
  return t(key) === key ? status : t(key);
}
function formatNumber(value: unknown, percent = false) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return percent ? `${(number * 100).toFixed(2)}%` : number.toFixed(4);
}
function MetricLabel({ label, hint }: { label: string; hint: string }) {
  return <Space size={4}><span>{label}</span><Tooltip title={t(hint)}><InfoCircleOutlined aria-label={t("factorLabMetricHelp")} /></Tooltip></Space>;
}

export default function FactorShadowLab(props: FactorLabSelectionProps = {}) {
  const selection = useFactorLabSelection(props);
  const showUnsupported = useUnsupportedFeatures();
  const versionId = selection.version?.id;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [observations, setObservations] = useState<ShadowObservationRead[]>([]);
  const [summary, setSummary] = useState<ShadowObservationSummary | null>(null);
  const [health, setHealth] = useState<ShadowHealthReport | null>(null);
  const [capabilityError, setCapabilityError] = useState(false);
  const [capabilityReload, setCapabilityReload] = useState(0);
  const requestSequence = useRef(0);
  const currentVersion = useRef(versionId);
  currentVersion.current = versionId;

  useEffect(() => {
    let cancelled = false;
    setCapabilityError(false);
    api.getShadowCapabilities().catch(() => { if (!cancelled) setCapabilityError(true); });
    return () => { cancelled = true; };
  }, [capabilityReload]);

  const loadAll = useCallback(async (id: number) => {
    const request = ++requestSequence.current;
    setLoading(true);
    setError(null);
    setObservations([]);
    setSummary(null);
    setHealth(null);
    try {
      const [rows, aggregate, report] = await Promise.all([
        api.listShadowObservations(id, { limit: 200 }), api.getShadowObservationSummary(id), api.getShadowHealth(id),
      ]);
      if (requestSequence.current !== request || currentVersion.current !== id) return;
      setObservations(rows);
      setSummary(aggregate);
      setHealth(report);
    } catch (err: unknown) {
      if (requestSequence.current !== request || currentVersion.current !== id) return;
      setError(err instanceof Error ? err.message : t("shadowLabLoadFailed"));
    } finally {
      if (requestSequence.current === request && currentVersion.current === id) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (versionId != null) void loadAll(versionId);
    else {
      requestSequence.current += 1;
      setLoading(false); setObservations([]); setSummary(null); setHealth(null); setError(null);
    }
    return () => { requestSequence.current += 1; };
  }, [versionId, loadAll]);

  const orderedObservations = useMemo(() => [...observations].sort((a, b) => a.trade_date.localeCompare(b.trade_date)), [observations]);
  const icChartOption = useMemo(() => ({
    tooltip: { trigger: "axis" }, legend: { data: ["IC", t("shadowLabCoverage")] },
    grid: { left: 50, right: 45, top: 40, bottom: 35 },
    xAxis: { type: "category", data: orderedObservations.map(row => row.trade_date), axisLabel: { rotate: 30 } },
    yAxis: [{ type: "value", name: "IC", position: "left" }, { type: "value", name: t("shadowLabCoverage"), position: "right", min: 0, max: 1 }],
    series: [
      { name: "IC", type: "line", data: orderedObservations.map(row => row.ic_value), connectNulls: false, itemStyle: { color: "#1b7f79" } },
      { name: t("shadowLabCoverage"), type: "line", yAxisIndex: 1, data: orderedObservations.map(row => row.coverage), connectNulls: false, itemStyle: { color: "#597eb5" } },
    ],
  }), [orderedObservations]);
  const observationColumns = useMemo(() => [
    { title: t("shadowLabTradeDate"), dataIndex: "trade_date", key: "date", width: 110 },
    { title: <MetricLabel label={t("shadowLabValidDay")} hint="factorLabShadowValidHint" />, dataIndex: "is_valid_day", key: "valid", width: 110,
      render: (valid: boolean, row: ShadowObservationRead) => <Tooltip title={row.invalid_reason || undefined}><Tag color={valid ? "green" : "orange"}>{t(valid ? "shadowLabYes" : "shadowLabNo")}</Tag></Tooltip> },
    { title: <MetricLabel label="IC" hint="factorLabShadowIcHint" />, dataIndex: "ic_value", key: "ic", width: 100, render: (value: number | null) => formatNumber(value) },
    { title: <MetricLabel label={t("shadowLabCoverage")} hint="factorLabShadowCoverageHint" />, dataIndex: "coverage", key: "coverage", width: 110, render: (value: number | null) => formatNumber(value, true) },
    { title: <MetricLabel label={t("shadowLabTurnover")} hint="factorLabShadowTurnoverHint" />, dataIndex: "turnover", key: "turnover", width: 110, render: (value: number | null) => formatNumber(value, true) },
    { title: <MetricLabel label={t("shadowLabCompleteness")} hint="factorLabShadowCompletenessHint" />, dataIndex: "completeness_ratio", key: "complete", width: 120, render: (value: number | null) => formatNumber(value, true) },
    { title: t("shadowLabHealthStatus"), dataIndex: "health_status", key: "health", width: 90, render: (value: string) => <Tag color={healthColor(value)}>{healthLabel(value)}</Tag> },
  ], []);
  const validDays = summary?.valid_days ?? 0;
  const minRequired = summary?.min_required_days ?? 20;
  const progress = minRequired > 0 ? Math.max(0, Math.min(100, Math.round(validDays / minRequired * 100))) : 0;

  return <div className="factor-shadow-lab factor-shadow-lab-guided">
    <div><h3>{t("shadowLabTitle")}</h3><Text type="secondary">{t("factorLabShadowSubtitle")}</Text></div>
    <Alert showIcon type="info" message={t("factorLabShadowReadOnly")} description={t("factorLabShadowReadOnlyDetail")} />
    {capabilityError ? <Alert showIcon type="warning" message={t("factorLabCapabilityFailed")}
      action={<Button size="small" onClick={() => setCapabilityReload(value => value + 1)}>{t("factorLabRetryCheck")}</Button>} /> : null}
    <Card size="small" title={t("factorLabShadowSelect") }>
      <div className="shadow-selection-grid">
        <div><label>{t("evalWorkbenchFactor")}</label><Select showSearch allowClear aria-label={t("evalWorkbenchFactor")}
          value={selection.factorCode || undefined} options={selection.factorOptions} onChange={selection.selectFactor}
          loading={selection.factorLoading} placeholder={t("evalLabFactorCodePlaceholder")}
          optionFilterProp="label" style={{ width: "100%" }} /></div>
        <div><label>{t("factorLabVersion")}</label><Select aria-label={t("factorLabVersion")}
          value={versionId} options={selection.versionOptions} onChange={selection.selectVersion}
          loading={selection.versionLoading} disabled={!selection.factorCode} placeholder={t("factorLabSelectVersion")}
          notFoundContent={selection.versionLoading ? <Spin size="small" /> : t("factorLabNoVersions")} style={{ width: "100%" }} /></div>
        <Button icon={<ReloadOutlined />} loading={loading} disabled={!versionId}
          onClick={() => versionId && void loadAll(versionId)}>{t("shadowLabRefresh")}</Button>
      </div>
      {selection.error ? <Alert type="error" showIcon message={selection.error} style={{ marginTop: 12 }}
        action={<Button size="small" onClick={selection.retry}>{t("factorLabRetryCheck")}</Button>} /> : null}
    </Card>
    {showUnsupported ? <Card size="small" title={t("shadowLabApproval")}>
      <Alert showIcon type="warning" message={t("factorLabShadowWritesUnavailable")} style={{ marginBottom: 12 }} />
      <Space wrap>{["shadowLabRequestActivation", "shadowLabApprove", "shadowLabReject", "shadowLabQuarantine"].map(key => <Button key={key} disabled>{t(key)}</Button>)}</Space>
    </Card> : null}
    {error ? <Alert type="error" showIcon message={t("shadowLabLoadFailed")} description={error}
      action={<Button onClick={() => versionId && void loadAll(versionId)}>{t("factorLabRetryCheck")}</Button>} /> : null}
    <Spin spinning={loading}>
      {!versionId ? <Empty description={t("shadowLabInputHint")} /> : !loading && !error && summary ? <>
        <Card size="small" title={t("shadowLabObservationSummary")} extra={<Tag color={summary.is_complete ? "green" : "orange"}
          icon={summary.is_complete ? <CheckCircleOutlined /> : <ExclamationCircleOutlined />}>{t(summary.is_complete ? "factorLabObservationComplete" : "shadowLabGateIncomplete")}</Tag>}>
          <Descriptions size="small" column={{ xs: 1, sm: 3 }}>
            <Descriptions.Item label={<MetricLabel label={t("shadowLabValidDays")} hint="factorLabShadowValidHint" />}><Text strong>{validDays}</Text> / {minRequired}</Descriptions.Item>
            <Descriptions.Item label={t("shadowLabHealthStatus")}><Tag color={healthColor(health?.health_status)}>{healthLabel(health?.health_status)}</Tag></Descriptions.Item>
            <Descriptions.Item label={t("shadowLabTotalDays")}>{health?.n_total_days ?? "-"}</Descriptions.Item>
          </Descriptions>
          <Progress percent={progress} status={summary.is_complete ? "success" : "normal"} />
          {summary.reason ? <Text type="secondary">{summary.reason}</Text> : null}
        </Card>
        {health?.alerts.length ? <Card size="small" title={t("shadowLabAlerts")}><Space direction="vertical" style={{ width: "100%" }}>
          {health.alerts.map((alert, index) => <Alert key={index} showIcon type={alert.severity === "critical" ? "error" : "warning"}
            icon={alert.severity === "critical" ? <CloseCircleOutlined /> : <ExclamationCircleOutlined />} message={alert.message} />)}
          {health.should_quarantine ? <Alert showIcon type="error" message={t("factorLabShadowRiskHint")} /> : null}
        </Space></Card> : null}
        {observations.length ? <Card size="small" title={t("shadowLabIcCurve")}><ReactECharts option={icChartOption} style={{ height: 260 }} /></Card> : null}
        <Card size="small" title={t("shadowLabObservations")}><Table<ShadowObservationRead> dataSource={[...orderedObservations].reverse()}
          columns={observationColumns} rowKey="id" size="small" pagination={{ pageSize: 10, showSizeChanger: false }} scroll={{ x: 760 }}
          locale={{ emptyText: t("factorLabShadowNoHistory") }} />
          <Text type="secondary">{t("factorLabShadowHistoryLimit")}</Text>
        </Card>
      </> : null}
    </Spin>
  </div>;
}
