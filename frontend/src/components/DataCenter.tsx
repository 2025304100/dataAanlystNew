import { AcceptanceButton as Button, AcceptanceNotice } from "./portfolio-trading/inspection/AcceptanceControls";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Modal, Space, Spin, Table, Tabs, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { DatabaseOutlined } from "@ant-design/icons";
import {
  api,
  type DataQualitySnapshot,
  type ExternalDataCoverage,
  type ExternalFieldCoverage,
} from "../api/client";
import ExternalDataSync from "./ExternalDataSync";
import TaskCenter from "./TaskCenter";
import SourceFieldIntegrity from "./SourceFieldIntegrity";
import UniverseDataPanel from "./UniverseDataPanel";
import DataMirrorPage from "./factors/mining/config/DataMirrorPage";
import SecurityStatusImportPanel from "./SecurityStatusImportPanel";
import SecurityStatusDownloadPanel from "./SecurityStatusDownloadPanel";
import ETFHistoryPanel from "./ETFHistoryPanel";
import ETFTrainingHistoryPanel from "./ETFTrainingHistoryPanel";
import type { BacktestRepairContext } from "../utils/backtestDraft";

type CoverageRow = ExternalFieldCoverage & { dataset: string };

const STATUS_COLORS: Record<string, string> = {
  available: "success", limited: "warning", event: "processing",
  snapshot: "purple", blocked: "error", unknown: "default",
};
const DATASET_LABELS: Record<string, string> = { fundamental: "股票估值", financial: "财报历史", lhb: "龙虎榜机构", hot_rank: "今日人气榜", tail_proxy: "候选尾盘代理", capital_flow: "资金流", etf: "ETF 指标" };
const FIELD_LABELS: Record<string, string> = { pe_ttm: "市盈率（PE-TTM）", pb: "市净率（PB）", roe_ttm: "净资产收益率（ROE-TTM）", lhb_institution_net: "龙虎榜机构净买额", hot_rank_pct: "人气榜排名百分位", proxy_score: "尾盘代理分数", main_net_inflow: "主力净流入", etf_premium_discount: "ETF 溢折价率", etf_tracking_error: "ETF 跟踪误差", etf_fund_size: "ETF 基金规模" };
const STATUS_LABELS: Record<string, string> = { available: "可用", limited: "范围有限", event: "有事件时才有记录", snapshot: "只有最近数据", blocked: "暂不能用于因子评价", unknown: "待确认" };

function CoverageDiagnostics() {
  const [coverage, setCoverage] = useState<ExternalDataCoverage | null>(null);
  const [snapshots, setSnapshots] = useState<Record<string, DataQualitySnapshot>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [qualityHistory, setQualityHistory] = useState<{ field: string; snapshots: DataQualitySnapshot[] } | null>(null);
  const [historyField, setHistoryField] = useState<string | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [failureDetail, setFailureDetail] = useState<{ title: string; capturedAt: string | null; reason: string } | null>(null);
  const historyRequest = useRef(0);

  const mounted = useRef(true);
  const load = useCallback(async () => {
    const [next, quality] = await Promise.allSettled([
      api.getExternalDataCoverage(), api.getExternalDataQualitySnapshots(),
    ]);
    if (!mounted.current) return;
    if (next.status === "fulfilled") setCoverage(next.value);
    if (quality.status === "fulfilled") setSnapshots(Object.fromEntries(quality.value.fields.map((item) => [item.field, item])));
    const failures = [next, quality].flatMap((result) => result.status === "rejected" ? [result.reason instanceof Error ? result.reason.message : String(result.reason)] : []);
    setError(failures.length ? failures.join("；") : null);
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load().catch((reason: unknown) => {
      if (mounted.current) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (mounted.current) setLoading(false);
    });
    return () => { mounted.current = false; ++historyRequest.current; };
  }, [load]);

  const retryRead = async () => {
    setLoading(true);
    try { await load(); } finally { if (mounted.current) setLoading(false); }
  };

  const refreshQuality = async () => {
    setLoading(true);
    try {
      await api.refreshExternalDataQualitySnapshots();
      await load();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  };

  const openHistory = async (field: string) => {
    const requestId = ++historyRequest.current;
    const isCurrent = () => mounted.current && requestId === historyRequest.current;
    setHistoryField(field);
    setQualityHistory(null);
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const history = await api.getExternalDataQualityHistory(field);
      if (isCurrent()) setQualityHistory(history);
    } catch (reason: unknown) {
      if (isCurrent()) setHistoryError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (isCurrent()) setHistoryLoading(false);
    }
  };

  const rows = useMemo<CoverageRow[]>(
    () => (coverage?.datasets || []).filter((dataset) => dataset.dataset !== "tail_proxy").flatMap((dataset) =>
      dataset.fields.map((field) => ({ ...field, dataset: dataset.dataset })),
    ), [coverage],
  );
  const columns: ColumnsType<CoverageRow> = [
    { title: "数据类型", dataIndex: "dataset", width: 130, render: (value: string) => DATASET_LABELS[value] || value },
    { title: "数据项目", dataIndex: "field", width: 190, render: (value: string) => FIELD_LABELS[value] || value },
    { title: "状态", dataIndex: "availability", width: 145, render: (value: string) => <Tag color={STATUS_COLORS[value] || "default"}>{STATUS_LABELS[value] || value}</Tag> },
    { title: "已有数据日期", width: 210, render: (_, row) => `${row.first_date || "-"} ~ ${row.latest_date || "-"}` },
    {
      title: "覆盖", width: 205,
      render: (_, row) => {
        const nonnull = row.table_rows > 0 ? `${((row.nonnull_rows / row.table_rows) * 100).toFixed(1)}%` : "-";
        const daily = row.latest_daily_coverage == null ? "-" : `${(row.latest_daily_coverage * 100).toFixed(1)}%`;
        return `有数值 ${nonnull} · 当天覆盖 ${daily} · 连续 ${row.continuity_days} 日`;
      },
    },
    { title: "能否用于因子评价", width: 150, render: (_, row) => row.evaluation_enabled ? "可以" : "暂不可以" },
    {
      title: "最近检查", width: 205,
      render: (_, row) => {
        const snapshot = snapshots[row.field];
        if (!snapshot) return "尚未采集";
        const reason = snapshot.failure_reason;
        return <Space direction="vertical" size={0}>
          <span>{snapshot.captured_at ? new Date(snapshot.captured_at).toLocaleString() : "-"}</span>
          <Space size={4} wrap>
            {reason && <Button type="link" size="small" danger style={{ padding: 0 }} onClick={() => setFailureDetail({ title: FIELD_LABELS[row.field] || row.field, capturedAt: snapshot.captured_at, reason })}>最近失败 · 查看原因</Button>}
            <Button type="link" size="small" style={{ padding: 0 }} onClick={() => void openHistory(row.field)}>查看趋势</Button>
          </Space>
        </Space>;
      },
    },
    { title: "原因", dataIndex: "reason", width: 220, ellipsis: { showTitle: true } },
  ];
  if (loading && !coverage) return <Spin />;
  if (error && !coverage) return <Alert type="error" showIcon message="数据检查结果读取失败" description={error} action={<Button loading={loading} onClick={() => void retryRead()}>重试读取</Button>} />;
  return <div>
    {error && <Alert type="warning" showIcon message="部分检查记录暂时读不到，下方保留已读到的数据" description={error} action={<Button size="small" loading={loading} onClick={() => void retryRead()}>重试读取</Button>} style={{ marginBottom: 10 }} />}
    <Space style={{ width: "100%", justifyContent: "space-between", marginBottom: 8 }} align="start">
      <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
        查看本地已有数据的日期、缺失情况和最近更新失败原因。“重新检查”只检查本地数据，不会重新下载。
      </Typography.Paragraph>
      <Button mutation onClick={() => void refreshQuality()} loading={loading}>重新检查本地数据</Button>
    </Space>
    <Table<CoverageRow> rowKey={(row) => `${row.dataset}:${row.field}`} columns={columns} dataSource={rows} pagination={false} size="middle" scroll={{ x: 1455 }} locale={{ emptyText: "还没有检查结果。先到“其他数据”更新需要的数据，再回这里检查。" }} />
    <Modal title={`历次检查：${FIELD_LABELS[historyField || ""] || historyField || ""}`} open={historyField !== null} footer={null} onCancel={() => { ++historyRequest.current; setHistoryField(null); setQualityHistory(null); }} width={760}>
      {historyError && <Alert type="error" message={historyError} action={<Button onClick={() => historyField && void openHistory(historyField)}>重试读取</Button>} />}
      <Table<DataQualitySnapshot>
        loading={historyLoading}
        rowKey="id"
        size="small"
        pagination={false}
        dataSource={qualityHistory?.snapshots || []}
        columns={[
          { title: "采集时间", dataIndex: "captured_at", render: (value) => value ? new Date(value).toLocaleString() : "-" },
          { title: "状态", dataIndex: "readiness", render: (value: string) => STATUS_LABELS[value] || value },
          { title: "有数值的记录占比", render: (_, item) => item.row_count ? `${((item.nonnull_rows / item.row_count) * 100).toFixed(1)}%` : "-" },
          { title: "覆盖日期", render: (_, item) => `${item.first_date || "-"} ~ ${item.latest_date || "-"}` },
          {
            title: "最近失败", dataIndex: "failure_reason", width: 140,
            render: (value: string | null, item) => value
              ? <Button type="link" size="small" danger style={{ padding: 0 }} onClick={() => setFailureDetail({ title: FIELD_LABELS[item.field] || item.field, capturedAt: item.captured_at, reason: value })}>查看原因</Button>
              : "-",
          },
        ]}
      />
    </Modal>
    <Modal title={`最近失败原因：${failureDetail?.title || ""}`} open={failureDetail !== null} footer={null} onCancel={() => setFailureDetail(null)} width={640} zIndex={1100}>
      {failureDetail && <>
        <Typography.Text type="secondary">采集时间：{failureDetail.capturedAt ? new Date(failureDetail.capturedAt).toLocaleString() : "-"}</Typography.Text>
        <Typography.Paragraph copyable={{ text: failureDetail.reason }} style={{ margin: "12px 0 0", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
          {failureDetail.reason}
        </Typography.Paragraph>
      </>}
    </Modal>
  </div>;
}

export default function DataCenter({ backtestRepair }: { backtestRepair?: BacktestRepairContext | null }) {
  const [activeTab, setActiveTab] = useState<"market" | "inputs" | "coverage" | "tasks" | "mirror">(() => {
    if (typeof window === "undefined") return "market";
    const stored = window.localStorage.getItem("settings_data_center_tab");
    return stored === "inputs" || stored === "coverage" || stored === "tasks" || stored === "mirror" ? stored : "market";
  });

  useEffect(() => {
    window.localStorage.setItem("settings_data_center_tab", activeTab);
  }, [activeTab]);

  useEffect(() => {
    const show = (event: Event) => {
      const tab = (event as CustomEvent).detail;
      if (["market", "inputs", "coverage", "tasks", "mirror"].includes(tab)) setActiveTab(tab);
    };
    window.addEventListener("data-center:navigate", show);
    return () => window.removeEventListener("data-center:navigate", show);
  }, []);

  return <section data-testid="data-center" style={{ width: "100%" }}>
    <AcceptanceNotice compact />
    <div style={{ marginBottom: 14 }}>
      <Typography.Title level={4} style={{ margin: 0, fontSize: 18 }}>
        <DatabaseOutlined style={{ color: "#1677ff", marginRight: 8 }} />数据管理
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ margin: "6px 0 0", fontSize: 13 }}>
        平时在“行情数据”更新价格；财报、资金流等资料在“其他数据”更新。遇到问题时，可查看“更新记录”。
      </Typography.Paragraph>
    </div>
    <Tabs activeKey={activeTab} onChange={(key) => setActiveTab(key as typeof activeTab)} items={[
      { key: "market", label: "行情数据", children: <><ETFTrainingHistoryPanel /><ETFHistoryPanel /><UniverseDataPanel /></> },
      { key: "inputs", label: "其他数据", children: <ExternalDataSync /> },
      { key: "coverage", label: "数据检查", children: <><SecurityStatusDownloadPanel
        portfolioId={backtestRepair?.portfolioId}
        requestedRange={backtestRepair ? { start: backtestRepair.startDate, end: backtestRepair.endDate } : undefined}
      /><SecurityStatusImportPanel /><SourceFieldIntegrity /><CoverageDiagnostics /></> },
      { key: "tasks", label: "更新记录", children: <TaskCenter /> },
      { key: "mirror", label: "本地副本", children: <DataMirrorPage /> },
    ]} />
  </section>;
}
