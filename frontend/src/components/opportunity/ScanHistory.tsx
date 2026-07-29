// UAT-PAGES.2：机会中心-扫描记录
//
// 数据来源：
// - GET /api/v1/discovery/scan-runs（返回扫描记录列表，含快照/阶段耗时/参数/缓存命中/差异摘要）
// - GET /api/v1/discovery/scan-runs/{id}（返回单条扫描记录详情）
//
// 展示内容：
// - 扫描快照（snapshot_id/scope/trade_date/status/generated_at）
// - 阶段耗时（stage_durations，来自 DiscoveryTaskRecord.stage_durations_json）
// - 参数（min_score/cache_key/portfolio_id/portfolio_rule_id）
// - 缓存命中（cache_hit）
// - 差异摘要（dirty_symbol_count/reused_score_count/rescored_count）
// - 错误详情（snapshot_error_summary，来自 DiscoveryScoreSnapshot.error_summary_json）
//
// 关键约束：
// - 使用真实后端数据，不伪造
// - 支持筛选（按 scope/trade_date/status）
// - 支持详情查看（点击查看完整扫描参数和结果）
// - 错误处理：使用统一错误协议（中文文案 + 重试按钮）
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
  Empty,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  ReloadOutlined,
  EyeOutlined,
} from "@ant-design/icons";
import { type Dayjs } from "dayjs";
import { api } from "../../api/client";
import { t } from "../../i18n";

// 扫描记录项（对齐后端 list_scan_runs 返回字段）
interface ScanRunItem {
  id: number;
  run_name: string;
  scope_snapshot: string;
  filters_snapshot: string | null;
  portfolio_id: number | null;
  portfolio_rule_id: number | null;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  // ScanRun 缓存与统计
  snapshot_id: number | null;
  cache_key: string | null;
  cache_hit: number | null;
  total_in_snapshot: number | null;
  coarse_match_count: number | null;
  advanced_match_count: number | null;
  result_rows_written: number | null;
  degraded_reason: string | null;
  // DiscoveryTaskRecord 字段
  task_id: string | null;
  scope: string | null;
  min_score: number | null;
  stage_durations: Array<Record<string, unknown>> | null;
  dirty_symbol_count: number | null;
  reused_score_count: number | null;
  rescored_count: number | null;
  snapshot_hit: number | null;
  task_degraded_reason: string | null;
  // DiscoveryScoreSnapshot 字段
  snapshot_scope: string | null;
  snapshot_trade_date: string | null;
  snapshot_status: string | null;
  snapshot_generated_at: string | null;
  snapshot_symbol_count: number | null;
  snapshot_coverage_pct: number | null;
  snapshot_dirty_symbol_count: number | null;
  snapshot_build_duration_seconds: number | null;
  snapshot_error_summary: Record<string, unknown> | null;
  scoring_config_id: number | null;
  scoring_config_version: number | null;
  weight_mode: string | null;
  factor_model_run_id: string | null;
  snapshot_data_cutoff_at: string | null;
  // 详情接口附加字段
  task_record?: Record<string, unknown> | null;
  snapshot_record?: Record<string, unknown> | null;
}

const SCOPE_OPTIONS = [
  { value: "cn-stock", label: "cn-stock" },
  { value: "cn-etf", label: "cn-etf" },
  { value: "us-stock", label: "us-stock" },
  { value: "us-etf", label: "us-etf" },
];

const STATUS_OPTIONS = [
  { value: "done", label: "done" },
  { value: "success", label: "success" },
  { value: "completed", label: "completed" },
  { value: "running", label: "running" },
  { value: "failed", label: "failed" },
  { value: "cancelled", label: "cancelled" },
];

export interface ScanHistoryProps {
  className?: string;
}

export default function ScanHistory({ className }: ScanHistoryProps) {
  const [items, setItems] = useState<ScanRunItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 筛选条件
  const [scopeFilter, setScopeFilter] = useState<string | undefined>(undefined);
  const [tradeDateFilter, setTradeDateFilter] = useState<Dayjs | null>(null);
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);

  // 详情弹窗
  const [detailItem, setDetailItem] = useState<ScanRunItem | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const fetchScanRuns = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listScanRuns({
        scope: scopeFilter,
        tradeDate: tradeDateFilter ? tradeDateFilter.format("YYYY-MM-DD") : undefined,
        status: statusFilter,
        limit: 100,
      });
      setItems(data ?? []);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("scanHistoryError"));
    } finally {
      setLoading(false);
    }
  }, [scopeFilter, tradeDateFilter, statusFilter]);

  useEffect(() => {
    fetchScanRuns();
  }, [fetchScanRuns]);

  // 详情查看：先显示列表数据，再请求详情接口补充 task_record/snapshot_record
  const handleViewDetail = useCallback(async (item: ScanRunItem) => {
    setDetailItem(item);
    setDetailLoading(true);
    try {
      const detail = await api.getScanRunDetail(item.id);
      if (detail) {
        setDetailItem(detail as ScanRunItem);
      }
    } catch {
      // 详情接口失败时保留列表数据，不阻塞查看
    } finally {
      setDetailLoading(false);
    }
  }, []);

  // 渲染阶段耗时摘要
  const renderStageDurations = useCallback((durations: ScanRunItem["stage_durations"]) => {
    if (!durations || durations.length === 0) return "-";
    const summary = durations
      .map((d) => {
        const stage = String(d.stage ?? d.name ?? "?");
        const ms = d.duration_ms ?? d.durationMs;
        const numMs = typeof ms === "number" ? ms : null;
        if (numMs != null) {
          return `${stage}:${t("scanHistoryDurationMs").replace("{ms}", String(numMs))}`;
        }
        return stage;
      })
      .join(" ");
    return (
      <Tooltip title={summary}>
        <span>{summary}</span>
      </Tooltip>
    );
  }, []);

  // 渲染差异摘要
  const renderDiffSummary = useCallback((row: ScanRunItem) => {
    const parts: string[] = [];
    if (row.dirty_symbol_count != null) {
      parts.push(t("scanHistoryDirtySymbols").replace("{count}", String(row.dirty_symbol_count)));
    }
    if (row.reused_score_count != null) {
      parts.push(t("scanHistoryReusedScores").replace("{count}", String(row.reused_score_count)));
    }
    if (row.rescored_count != null) {
      parts.push(t("scanHistoryRescored").replace("{count}", String(row.rescored_count)));
    }
    if (parts.length === 0) return "-";
    return (
      <Space size={4} wrap>
        {parts.map((p, i) => (
          <Tag key={i}>{p}</Tag>
        ))}
      </Space>
    );
  }, []);

  const columns: ColumnsType<ScanRunItem> = useMemo(
    () => [
      {
        title: t("scanHistoryColumnId"),
        dataIndex: "id",
        key: "id",
        width: 70,
        render: (v: number) => `#${v}`,
      },
      {
        title: t("scanHistoryColumnRunName"),
        dataIndex: "run_name",
        key: "run_name",
        width: 160,
        ellipsis: true,
      },
      {
        title: t("scanHistoryColumnScope"),
        key: "scope",
        width: 100,
        render: (_v: unknown, row: ScanRunItem) => row.scope ?? row.snapshot_scope ?? "-",
      },
      {
        title: t("scanHistoryColumnTradeDate"),
        key: "trade_date",
        width: 120,
        render: (_v: unknown, row: ScanRunItem) =>
          row.snapshot_trade_date ? row.snapshot_trade_date.slice(0, 10) : "-",
      },
      {
        title: t("scanHistoryColumnStatus"),
        dataIndex: "status",
        key: "status",
        width: 100,
        render: (v: string) => {
          const color =
            v === "done" || v === "success" || v === "completed"
              ? "success"
              : v === "running"
                ? "processing"
                : v === "failed"
                  ? "error"
                  : "default";
          return <Tag color={color}>{v}</Tag>;
        },
      },
      {
        title: t("scanHistoryColumnGeneratedAt"),
        key: "generated_at",
        width: 170,
        render: (_v: unknown, row: ScanRunItem) => row.snapshot_generated_at ?? row.created_at ?? "-",
      },
      {
        title: t("scanHistoryColumnStageDurations"),
        key: "stage_durations",
        width: 220,
        ellipsis: true,
        render: (_v: unknown, row: ScanRunItem) => renderStageDurations(row.stage_durations),
      },
      {
        title: t("scanHistoryColumnCacheHit"),
        key: "cache_hit",
        width: 100,
        render: (_v: unknown, row: ScanRunItem) => {
          if (row.cache_hit == null) return "-";
          return row.cache_hit === 1 ? (
            <Tag color="green">{t("scanHistoryCacheHit")}</Tag>
          ) : (
            <Tag>{t("scanHistoryCacheMiss")}</Tag>
          );
        },
      },
      {
        title: t("scanHistoryColumnDiffSummary"),
        key: "diff_summary",
        width: 260,
        render: (_v: unknown, row: ScanRunItem) => renderDiffSummary(row),
      },
      {
        title: t("scanHistoryColumnParams"),
        key: "params",
        width: 200,
        render: (_v: unknown, row: ScanRunItem) => (
          <Space size={4} wrap>
            {row.min_score != null && <Tag>min:{row.min_score}</Tag>}
            {row.portfolio_id != null && <Tag>pf:{row.portfolio_id}</Tag>}
            {row.portfolio_rule_id != null && <Tag>pr:{row.portfolio_rule_id}</Tag>}
          </Space>
        ),
      },
      {
        title: t("scanHistoryColumnErrors"),
        key: "errors",
        width: 120,
        render: (_v: unknown, row: ScanRunItem) => {
          if (row.snapshot_error_summary == null) return "-";
          return <Tag color="warning">{t("scanHistoryDetailView")}</Tag>;
        },
      },
      {
        title: t("scanHistoryColumnActions"),
        key: "actions",
        width: 120,
        render: (_v: unknown, row: ScanRunItem) => (
          <Button
            size="small"
            type="link"
            icon={<EyeOutlined />}
            onClick={() => handleViewDetail(row)}
          >
            {t("scanHistoryDetailView")}
          </Button>
        ),
      },
    ],
    [renderStageDurations, renderDiffSummary, handleViewDetail],
  );

  // 空/错/加载状态
  const renderBody = () => {
    if (loading && items.length === 0) {
      return (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin tip={t("scanHistoryError")} />
        </div>
      );
    }
    if (error && items.length === 0) {
      return (
        <Alert
          type="error"
          showIcon
          message={t("scanHistoryError")}
          description={error}
          action={
            <Button size="small" onClick={fetchScanRuns}>
              {t("scanHistoryRetry")}
            </Button>
          }
        />
      );
    }
    if (items.length === 0) {
      return (
        <Empty
          description={
            <div>
              <div>{t("scanHistoryEmpty")}</div>
              <div style={{ color: "var(--muted)", fontSize: 12, marginTop: 4 }}>
                {t("scanHistoryEmptyHint")}
              </div>
            </div>
          }
        />
      );
    }
    return (
      <Table<ScanRunItem>
        rowKey={(row) => String(row.id)}
        dataSource={items}
        size="small"
        pagination={{ pageSize: 20, showSizeChanger: false }}
        columns={columns}
        scroll={{ x: "max-content" }}
        loading={loading}
      />
    );
  };

  // 渲染详情弹窗
  const renderDetail = () => {
    if (!detailItem) return null;
    const item = detailItem;
    return (
      <Modal
        open={!!detailItem}
        title={`${t("scanHistoryDetailView")} · #${item.id}`}
        width={720}
        footer={[
          <Button key="close" onClick={() => setDetailItem(null)}>
            {t("scanHistoryDetailClose")}
          </Button>,
        ]}
        onCancel={() => setDetailItem(null)}
      >
        {detailLoading && (
          <div style={{ textAlign: "center", padding: 12 }}>
            <Spin />
          </div>
        )}
        <div style={{ marginBottom: 8 }}>
          <strong>{t("scanHistoryDetailSnapshot")}:</strong>
          <pre style={{ margin: "4px 0", padding: 8, background: "#f5f5f5", maxHeight: 200, overflow: "auto", fontSize: 12 }}>
            {JSON.stringify(
              {
                snapshot_id: item.snapshot_id,
                scope: item.snapshot_scope ?? item.scope,
                trade_date: item.snapshot_trade_date,
                status: item.snapshot_status,
                generated_at: item.snapshot_generated_at,
                symbol_count: item.snapshot_symbol_count,
                coverage_pct: item.snapshot_coverage_pct,
                dirty_symbol_count: item.snapshot_dirty_symbol_count,
                build_duration_seconds: item.snapshot_build_duration_seconds,
                scoring_config_id: item.scoring_config_id,
                scoring_config_version: item.scoring_config_version,
                weight_mode: item.weight_mode,
                factor_model_run_id: item.factor_model_run_id,
                data_cutoff_at: item.snapshot_data_cutoff_at,
              },
              null,
              2,
            )}
          </pre>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("scanHistoryDetailTask")}:</strong>
          <pre style={{ margin: "4px 0", padding: 8, background: "#f5f5f5", maxHeight: 200, overflow: "auto", fontSize: 12 }}>
            {JSON.stringify(
              {
                task_id: item.task_id,
                scope: item.scope,
                min_score: item.min_score,
                status: item.status,
                started_at: item.started_at,
                finished_at: item.finished_at,
                created_at: item.created_at,
                portfolio_id: item.portfolio_id,
                portfolio_rule_id: item.portfolio_rule_id,
                degraded_reason: item.degraded_reason ?? item.task_degraded_reason,
              },
              null,
              2,
            )}
          </pre>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("scanHistoryDetailParams")}:</strong>
          <pre style={{ margin: "4px 0", padding: 8, background: "#f5f5f5", maxHeight: 200, overflow: "auto", fontSize: 12 }}>
            {JSON.stringify(
              {
                cache_key: item.cache_key,
                cache_hit: item.cache_hit,
                snapshot_hit: item.snapshot_hit,
                total_in_snapshot: item.total_in_snapshot,
                coarse_match_count: item.coarse_match_count,
                advanced_match_count: item.advanced_match_count,
                result_rows_written: item.result_rows_written,
                filters_snapshot: item.filters_snapshot,
              },
              null,
              2,
            )}
          </pre>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("scanHistoryDetailStageDurations")}:</strong>
          <pre style={{ margin: "4px 0", padding: 8, background: "#f5f5f5", maxHeight: 200, overflow: "auto", fontSize: 12 }}>
            {item.stage_durations ? JSON.stringify(item.stage_durations, null, 2) : "-"}
          </pre>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("scanHistoryColumnDiffSummary")}:</strong>
          <div style={{ marginTop: 4, marginLeft: 8, fontSize: 13 }}>
            <div>{t("scanHistoryDirtySymbols").replace("{count}", String(item.dirty_symbol_count ?? "-"))}</div>
            <div>{t("scanHistoryReusedScores").replace("{count}", String(item.reused_score_count ?? "-"))}</div>
            <div>{t("scanHistoryRescored").replace("{count}", String(item.rescored_count ?? "-"))}</div>
          </div>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("scanHistoryDetailErrors")}:</strong>
          <pre style={{ margin: "4px 0", padding: 8, background: "#f5f5f5", maxHeight: 200, overflow: "auto", fontSize: 12 }}>
            {item.snapshot_error_summary ? JSON.stringify(item.snapshot_error_summary, null, 2) : "-"}
          </pre>
        </div>
      </Modal>
    );
  };

  return (
    <div
      className={`opportunity-scan-history ${className ?? ""}`}
      data-opportunity-tab="scan-history"
    >
      <div style={{ marginBottom: 8, display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <Space size="small" wrap>
          <Select
            allowClear
            placeholder={t("scanHistoryFilterScope")}
            style={{ minWidth: 140 }}
            value={scopeFilter}
            onChange={(v) => setScopeFilter(v)}
            options={SCOPE_OPTIONS}
          />
          <DatePicker
            placeholder={t("scanHistoryFilterTradeDate")}
            value={tradeDateFilter}
            onChange={(d) => setTradeDateFilter(d)}
          />
          <Select
            allowClear
            placeholder={t("scanHistoryFilterStatus")}
            style={{ minWidth: 140 }}
            value={statusFilter}
            onChange={(v) => setStatusFilter(v)}
            options={STATUS_OPTIONS}
          />
          <Button icon={<ReloadOutlined />} onClick={fetchScanRuns} loading={loading}>
            {t("scanHistoryRetry")}
          </Button>
        </Space>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>
          {t("scanHistoryCount")}: {items.length}
        </span>
      </div>

      {renderBody()}
      {renderDetail()}

      {/* 标记最新加载（仅用于调试/可观测性） */}
      <span style={{ display: "none" }} data-scan-runs-count={items.length} />
    </div>
  );
}
