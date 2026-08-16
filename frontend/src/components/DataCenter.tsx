import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Modal, Space, Spin, Table, Tabs, Tag, Typography } from "antd";
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
import UniverseDataPanel from "./UniverseDataPanel";

type CoverageRow = ExternalFieldCoverage & { dataset: string };

const STATUS_COLORS: Record<string, string> = {
  available: "success", limited: "warning", event: "processing",
  snapshot: "purple", blocked: "error", unknown: "default",
};

function CoverageDiagnostics() {
  const [coverage, setCoverage] = useState<ExternalDataCoverage | null>(null);
  const [snapshots, setSnapshots] = useState<Record<string, DataQualitySnapshot>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [qualityHistory, setQualityHistory] = useState<{ field: string; snapshots: DataQualitySnapshot[] } | null>(null);

  const load = async () => {
    const [next, quality] = await Promise.all([
      api.getExternalDataCoverage(), api.getExternalDataQualitySnapshots(),
    ]);
    setCoverage(next);
    setSnapshots(Object.fromEntries(quality.fields.map((item) => [item.field, item])));
  };

  useEffect(() => {
    let disposed = false;
    void load().catch((reason: unknown) => {
      if (!disposed) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (!disposed) setLoading(false);
    });
    return () => { disposed = true; };
  }, []);

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
    try {
      setQualityHistory(await api.getExternalDataQualityHistory(field));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const rows = useMemo<CoverageRow[]>(
    () => (coverage?.datasets || []).flatMap((dataset) =>
      dataset.fields.map((field) => ({ ...field, dataset: dataset.dataset })),
    ), [coverage],
  );
  const columns: ColumnsType<CoverageRow> = [
    { title: "数据集", dataIndex: "dataset", width: 130 },
    { title: "字段", dataIndex: "field", width: 170 },
    { title: "状态", dataIndex: "availability", width: 105, render: (value: string) => <Tag color={STATUS_COLORS[value] || "default"}>{value}</Tag> },
    { title: "真实日期范围", width: 210, render: (_, row) => `${row.first_date || "-"} ~ ${row.latest_date || "-"}` },
    {
      title: "覆盖", width: 205,
      render: (_, row) => {
        const nonnull = row.table_rows > 0 ? `${((row.nonnull_rows / row.table_rows) * 100).toFixed(1)}%` : "-";
        const daily = row.latest_daily_coverage == null ? "-" : `${(row.latest_daily_coverage * 100).toFixed(1)}%`;
        return `非空 ${nonnull} · 日覆盖 ${daily} · 连续 ${row.continuity_days} 日`;
      },
    },
    { title: "评价许可", width: 105, render: (_, row) => row.evaluation_enabled ? "允许" : "受限" },
    {
      title: "质量快照", width: 230,
      render: (_, row) => {
        const snapshot = snapshots[row.field];
        if (!snapshot) return "尚未采集";
        return <Space direction="vertical" size={0}>
          <span>{snapshot.captured_at ? new Date(snapshot.captured_at).toLocaleString() : "-"}</span>
          {snapshot.failure_reason && <Typography.Text type="danger" ellipsis={{ tooltip: snapshot.failure_reason }}>最近失败：{snapshot.failure_reason}</Typography.Text>}
          <Button type="link" size="small" style={{ padding: 0, textAlign: "left" }} onClick={() => void openHistory(row.field)}>查看趋势</Button>
        </Space>;
      },
    },
    { title: "原因", dataIndex: "reason", ellipsis: true },
  ];
  if (loading && !coverage) return <Spin />;
  if (error && !coverage) return <Alert type="error" showIcon message="覆盖诊断读取失败" description={error} />;
  return <div>
    {error && <Alert type="warning" showIcon message="质量快照刷新失败" description={error} style={{ marginBottom: 10 }} />}
    <Space style={{ width: "100%", justifyContent: "space-between", marginBottom: 8 }} align="start">
      <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
        覆盖诊断基于实际写入因子仓库的字段；质量快照保存历史趋势和最近一次同步失败原因。
      </Typography.Paragraph>
      <Button onClick={() => void refreshQuality()} loading={loading}>刷新质量快照</Button>
    </Space>
    <Table<CoverageRow> rowKey={(row) => `${row.dataset}:${row.field}`} columns={columns} dataSource={rows} pagination={false} size="middle" scroll={{ x: 1400 }} locale={{ emptyText: "暂无可诊断的外部因子字段" }} />
    <Modal title={`字段质量趋势：${qualityHistory?.field || ""}`} open={qualityHistory !== null} footer={null} onCancel={() => setQualityHistory(null)} width={760}>
      <Table<DataQualitySnapshot>
        rowKey="id"
        size="small"
        pagination={false}
        dataSource={qualityHistory?.snapshots || []}
        columns={[
          { title: "采集时间", dataIndex: "captured_at", render: (value) => value ? new Date(value).toLocaleString() : "-" },
          { title: "状态", dataIndex: "readiness" },
          { title: "非空率", render: (_, item) => item.row_count ? `${((item.nonnull_rows / item.row_count) * 100).toFixed(1)}%` : "-" },
          { title: "覆盖日期", render: (_, item) => `${item.first_date || "-"} ~ ${item.latest_date || "-"}` },
          { title: "最近失败", dataIndex: "failure_reason", ellipsis: true },
        ]}
      />
    </Modal>
  </div>;
}

export default function DataCenter() {
  return <section data-testid="data-center" style={{ width: "100%" }}>
    <div style={{ marginBottom: 14 }}>
      <Typography.Title level={4} style={{ margin: 0, fontSize: 18 }}>
        <DatabaseOutlined style={{ color: "#1677ff", marginRight: 8 }} />数据中心
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ margin: "6px 0 0", fontSize: 13 }}>
        行情底座保持原有同步方式；外部输入、覆盖证据和任务批次在此统一查看。
      </Typography.Paragraph>
    </div>
    <Tabs defaultActiveKey="market" items={[
      { key: "market", label: "行情底座", children: <UniverseDataPanel /> },
      { key: "inputs", label: "因子输入", children: <ExternalDataSync /> },
      { key: "coverage", label: "覆盖诊断", children: <CoverageDiagnostics /> },
      { key: "tasks", label: "任务与批次", children: <TaskCenter /> },
    ]} />
  </section>;
}
