// WP2.4：机会中心-观察池正式页面
// 在 WP1 只读版本基础上升级为富读模型 + 筛选 + 详情 + 批量操作 + 空/错/加载状态
//
// 数据来源：
// - GET /api/v1/watchlists/{id}/observations 富读列表（支持 status/origin_type/tag 筛选）
// - PATCH /api/v1/watchlists/{id}/observations/{observation_id} 更新
// - POST /api/v1/watchlists/{id}/observations/{observation_id}/archive 归档
// - POST /api/v1/watchlists/{id}/observations/{observation_id}/restore 恢复
// - POST /api/v1/watchlists/{id}/observations/batch-import 批量幂等写入
//
// 关键约束：
// - 后端存储，清空浏览器缓存后数据仍存在
// - 接口失败降级不抛异常，显示错误信息 + 重试按钮
// - 错误消息不暴露敏感信息
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  ReloadOutlined,
  InboxOutlined,
  EyeOutlined,
  ArrowRightOutlined,
} from "@ant-design/icons";
import { useApp } from "../../context/AppContext";
import { requestJson } from "../../api/client";
import { t } from "../../i18n";
import { OpportunityStatusBadges } from "./OpportunityStatusBadges";
import { navigateToResearch } from "../../utils/sourceContext";

// 观察项富读模型（对齐 app.schemas.watchlist.ObservationRead）
interface ObservationItem {
  watchlist_item_id: number;
  watchlist_id: number;
  watchlist_name: string | null;
  symbol_id: number;
  symbol: string | null;
  added_at: string | null;
  updated_at: string | null;
  archived_at: string | null;
  origin_type: string;
  origin_id: number | null;
  reason: Record<string, unknown> | null;
  score_snapshot: Record<string, unknown> | null;
  status: string;
  priority: number;
  tags: string[];
  note: string | null;
  target_portfolio_id: number | null;
  target_portfolio_name: string | null;
  latest_price: number | null;
  latest_price_date: string | null;
  price_change_pct: number | null;
  latest_total_score: number | null;
  latest_quality_score: number | null;
  latest_timing_score: number | null;
  latest_score_date: string | null;
  data_credibility: string | null;
  bar_count: number | null;
  has_position: boolean;
  position_portfolio_name: string | null;
  degraded: boolean;
  degraded_reason: string | null;
}

// 状态/来源筛选可选项
const STATUS_OPTIONS = [
  { value: "watching", label: "watching" },
  { value: "ready", label: "ready" },
  { value: "invalid", label: "invalid" },
  { value: "archived", label: "archived" },
];

const ORIGIN_OPTIONS = [
  { value: "manual", label: "manual" },
  { value: "candidate", label: "candidate" },
  { value: "scan_result", label: "scan_result" },
  { value: "alert", label: "alert" },
  { value: "legacy_manual_unknown", label: "legacy_manual_unknown" },
];

// 优先级可选值
const PRIORITY_OPTIONS = [
  { value: 0, label: "0" },
  { value: 25, label: "25" },
  { value: 50, label: "50" },
  { value: 75, label: "75" },
  { value: 100, label: "100" },
];

export interface ObservationPoolProps {
  className?: string;
}

export default function ObservationPool({ className }: ObservationPoolProps) {
  const ctx = useApp();
  const { workbench, activeWatchlistId } = ctx;

  // 找到主观察池：优先 activeWatchlistId，其次 list_type="watch"，否则取第一个
  const primaryWatchlist = useMemo(() => {
    const lists = workbench?.watchlists ?? [];
    if (activeWatchlistId) {
      const found = lists.find((item) => item.id === activeWatchlistId);
      if (found) return found;
    }
    return lists.find((item) => item.list_type === "watch") ?? lists[0] ?? null;
  }, [workbench?.watchlists, activeWatchlistId]);

  const watchlistId = primaryWatchlist?.id ?? null;

  const [observations, setObservations] = useState<ObservationItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 筛选条件
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [originFilter, setOriginFilter] = useState<string | undefined>(undefined);
  const [tagFilter, setTagFilter] = useState<string>("");
  const [watchlistFilter, setWatchlistFilter] = useState<number | undefined>(undefined);

  // 多选
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([]);
  const [batchUpdating, setBatchUpdating] = useState(false);

  // 详情弹窗
  const [detailItem, setDetailItem] = useState<ObservationItem | null>(null);

  // 批量更新优先级
  const [batchPriority, setBatchPriority] = useState<number | undefined>(undefined);
  const [batchTagInput, setBatchTagInput] = useState<string>("");

  // 实际查询的 watchlistId（支持多名单筛选）
  const effectiveWatchlistId = watchlistFilter ?? watchlistId;

  const fetchObservations = useCallback(async () => {
    if (!effectiveWatchlistId) {
      setObservations([]);
      setError(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      params.set("limit", "200");
      if (statusFilter) params.set("status", statusFilter);
      if (originFilter) params.set("origin_type", originFilter);
      if (tagFilter.trim()) params.set("tag", tagFilter.trim());
      const url = `/api/v1/watchlists/${effectiveWatchlistId}/observations?${params.toString()}`;
      const data = await requestJson<ObservationItem[]>(url);
      setObservations(data ?? []);
    } catch (err) {
      // 错误消息不暴露敏感信息，仅展示通用错误
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("observationPoolError"));
    } finally {
      setLoading(false);
    }
  }, [effectiveWatchlistId, statusFilter, originFilter, tagFilter]);

  useEffect(() => {
    fetchObservations();
  }, [fetchObservations]);

  // 批量归档
  const handleBatchArchive = useCallback(async () => {
    if (selectedRowKeys.length === 0 || !effectiveWatchlistId) return;
    setBatchUpdating(true);
    try {
      let okCount = 0;
      let failCount = 0;
      for (const id of selectedRowKeys) {
        try {
          await requestJson<ObservationItem>(
            `/api/v1/watchlists/${effectiveWatchlistId}/observations/${id}/archive`,
            { method: "POST" },
          );
          okCount += 1;
        } catch {
          failCount += 1;
        }
      }
      if (okCount > 0) {
        message.success(t("observationPoolBatchArchiveDone").replace("{ok}", String(okCount)));
      }
      if (failCount > 0) {
        message.warning(t("observationPoolBatchArchiveFailed").replace("{fail}", String(failCount)));
      }
      setSelectedRowKeys([]);
      fetchObservations();
    } finally {
      setBatchUpdating(false);
    }
  }, [selectedRowKeys, effectiveWatchlistId, fetchObservations]);

  // 批量恢复
  const handleBatchRestore = useCallback(async () => {
    if (selectedRowKeys.length === 0 || !effectiveWatchlistId) return;
    setBatchUpdating(true);
    try {
      let okCount = 0;
      let failCount = 0;
      for (const id of selectedRowKeys) {
        try {
          await requestJson<ObservationItem>(
            `/api/v1/watchlists/${effectiveWatchlistId}/observations/${id}/restore`,
            { method: "POST" },
          );
          okCount += 1;
        } catch {
          failCount += 1;
        }
      }
      if (okCount > 0) {
        message.success(t("observationPoolBatchRestoreDone").replace("{ok}", String(okCount)));
      }
      if (failCount > 0) {
        message.warning(t("observationPoolBatchRestoreFailed").replace("{fail}", String(failCount)));
      }
      setSelectedRowKeys([]);
      fetchObservations();
    } finally {
      setBatchUpdating(false);
    }
  }, [selectedRowKeys, effectiveWatchlistId, fetchObservations]);

  // 批量更新优先级
  const handleBatchUpdatePriority = useCallback(async () => {
    if (selectedRowKeys.length === 0 || !effectiveWatchlistId) return;
    if (batchPriority === undefined) {
      message.warning(t("observationPoolBatchSelectPriority"));
      return;
    }
    setBatchUpdating(true);
    try {
      let okCount = 0;
      let failCount = 0;
      for (const id of selectedRowKeys) {
        try {
          await requestJson<ObservationItem>(
            `/api/v1/watchlists/${effectiveWatchlistId}/observations/${id}`,
            {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ priority: batchPriority }),
            },
          );
          okCount += 1;
        } catch {
          failCount += 1;
        }
      }
      if (okCount > 0) {
        message.success(t("observationPoolBatchUpdateDone").replace("{ok}", String(okCount)));
      }
      if (failCount > 0) {
        message.warning(t("observationPoolBatchUpdateFailed").replace("{fail}", String(failCount)));
      }
      setSelectedRowKeys([]);
      setBatchPriority(undefined);
      fetchObservations();
    } finally {
      setBatchUpdating(false);
    }
  }, [selectedRowKeys, effectiveWatchlistId, batchPriority, fetchObservations]);

  // 批量更新标签
  const handleBatchUpdateTags = useCallback(async () => {
    if (selectedRowKeys.length === 0 || !effectiveWatchlistId) return;
    const tags = batchTagInput
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    if (tags.length === 0) {
      message.warning(t("observationPoolBatchInputTags"));
      return;
    }
    setBatchUpdating(true);
    try {
      let okCount = 0;
      let failCount = 0;
      for (const id of selectedRowKeys) {
        try {
          await requestJson<ObservationItem>(
            `/api/v1/watchlists/${effectiveWatchlistId}/observations/${id}`,
            {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ tags }),
            },
          );
          okCount += 1;
        } catch {
          failCount += 1;
        }
      }
      if (okCount > 0) {
        message.success(t("observationPoolBatchUpdateDone").replace("{ok}", String(okCount)));
      }
      if (failCount > 0) {
        message.warning(t("observationPoolBatchUpdateFailed").replace("{fail}", String(failCount)));
      }
      setSelectedRowKeys([]);
      setBatchTagInput("");
      fetchObservations();
    } finally {
      setBatchUpdating(false);
    }
  }, [selectedRowKeys, effectiveWatchlistId, batchTagInput, fetchObservations]);

  const columns: ColumnsType<ObservationItem> = useMemo(
    () => [
      {
        title: t("observationPoolColumnSymbol"),
        key: "symbol",
        width: 110,
        render: (_v: unknown, row: ObservationItem) => row.symbol ?? "-",
      },
      {
        title: t("observationPoolColumnOrigin"),
        dataIndex: "origin_type",
        key: "origin_type",
        width: 130,
        render: (v: string) => <Tag>{v}</Tag>,
      },
      {
        title: t("observationPoolColumnAddedAt"),
        dataIndex: "added_at",
        key: "added_at",
        width: 160,
        render: (v: string | null) => v ?? "-",
      },
      {
        title: t("observationPoolColumnPriority"),
        dataIndex: "priority",
        key: "priority",
        width: 80,
        render: (v: number) => v ?? 0,
      },
      {
        title: t("observationPoolColumnStatus"),
        dataIndex: "status",
        key: "status",
        width: 110,
        render: (v: string) => <Tag color={v === "archived" ? "default" : "blue"}>{v}</Tag>,
      },
      {
        title: t("observationPoolColumnLatestPrice"),
        key: "latest_price",
        width: 110,
        render: (_v: unknown, row: ObservationItem) =>
          row.latest_price != null ? row.latest_price.toFixed(2) : "-",
      },
      {
        title: t("observationPoolColumnLatestScore"),
        key: "latest_total_score",
        width: 100,
        render: (_v: unknown, row: ObservationItem) =>
          row.latest_total_score != null ? row.latest_total_score.toFixed(1) : "-",
      },
      {
        title: t("observationPoolColumnTargetPortfolio"),
        key: "target_portfolio_name",
        width: 140,
        render: (_v: unknown, row: ObservationItem) => row.target_portfolio_name ?? "-",
      },
      {
        title: t("opportunityObservationColStatus"),
        key: "badges",
        width: 280,
        render: (_v: unknown, row: ObservationItem) => (
          <OpportunityStatusBadges symbolId={row.symbol_id} />
        ),
      },
      {
        title: t("observationPoolColumnActions"),
        key: "actions",
        width: 200,
        render: (_v: unknown, row: ObservationItem) => (
          <Space size="small">
            <Button
              size="small"
              type="link"
              icon={<ArrowRightOutlined />}
              onClick={() => {
                // WP5.3：观察池入口跳转，携带 portfolio_id（target_portfolio_id）
                navigateToResearch(
                  ctx,
                  {
                    symbol_id: row.symbol_id,
                    source_type: "observation",
                    source_id: row.watchlist_item_id,
                    portfolio_id: row.target_portfolio_id ?? undefined,
                    return_to: "observation",
                  },
                  {
                    returnState: {
                      statusFilter,
                      originFilter,
                      tagFilter,
                      watchlistFilter,
                    },
                  },
                );
              }}
            >
              {t("observationPoolEnterResearch")}
            </Button>
            <Button
              size="small"
              type="link"
              icon={<EyeOutlined />}
              onClick={() => setDetailItem(row)}
            >
              {t("observationPoolDetailView")}
            </Button>
            {row.status !== "archived" ? (
              <Popconfirm
                title={t("observationPoolArchiveConfirm")}
                onConfirm={async () => {
                  try {
                    await requestJson<ObservationItem>(
                      `/api/v1/watchlists/${row.watchlist_id}/observations/${row.watchlist_item_id}/archive`,
                      { method: "POST" },
                    );
                    message.success(t("observationPoolArchiveDone"));
                    fetchObservations();
                  } catch {
                    message.error(t("observationPoolArchiveFailed"));
                  }
                }}
              >
                <Button size="small" type="link" icon={<InboxOutlined />}>
                  {t("observationPoolArchive")}
                </Button>
              </Popconfirm>
            ) : (
              <Button
                size="small"
                type="link"
                onClick={async () => {
                  try {
                    await requestJson<ObservationItem>(
                      `/api/v1/watchlists/${row.watchlist_id}/observations/${row.watchlist_item_id}/restore`,
                      { method: "POST" },
                    );
                    message.success(t("observationPoolRestoreDone"));
                    fetchObservations();
                  } catch {
                    message.error(t("observationPoolRestoreFailed"));
                  }
                }}
              >
                {t("observationPoolRestore")}
              </Button>
            )}
          </Space>
        ),
      },
    ],
    // WP5.3：补全依赖项，避免闭包过期导致 returnState 携带旧筛选值
    [fetchObservations, ctx, statusFilter, originFilter, tagFilter, watchlistFilter],
  );

  // 空/错/加载状态
  const renderBody = () => {
    if (!workbench) {
      return (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin tip={t("opportunityObservationLoading")} />
        </div>
      );
    }
    if (!primaryWatchlist && !watchlistFilter) {
      return <Empty description={t("opportunityObservationNoWatchlist")} />;
    }
    if (loading && observations.length === 0) {
      return (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin tip={t("opportunityObservationLoading")} />
        </div>
      );
    }
    if (error && observations.length === 0) {
      return (
        <Alert
          type="error"
          showIcon
          message={t("observationPoolError")}
          description={error}
          action={
            <Button size="small" onClick={fetchObservations}>
              {t("observationPoolRetry")}
            </Button>
          }
        />
      );
    }
    if (observations.length === 0) {
      return (
        <Empty
          description={
            <div>
              <div>{t("observationPoolEmpty")}</div>
              <div style={{ color: "var(--muted)", fontSize: 12, marginTop: 4 }}>
                {t("observationPoolEmptyHint")}
              </div>
            </div>
          }
        />
      );
    }
    return (
      <Table<ObservationItem>
        rowKey={(row) => row.watchlist_item_id}
        dataSource={observations}
        size="small"
        pagination={{ pageSize: 20, showSizeChanger: false }}
        columns={columns}
        scroll={{ x: "max-content" }}
        rowSelection={{
          selectedRowKeys,
          onChange: (keys) => setSelectedRowKeys(keys as number[]),
        }}
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
        title={`${t("observationPoolDetailView")} · ${item.symbol ?? ""}`}
        footer={[
          <Button key="close" onClick={() => setDetailItem(null)}>
            {t("observationPoolDetailClose")}
          </Button>,
        ]}
        onCancel={() => setDetailItem(null)}
      >
        <div style={{ marginBottom: 8 }}>
          <strong>{t("observationPoolDetailOrigin")}:</strong> <Tag>{item.origin_type}</Tag>
          {item.origin_id != null && <span style={{ marginLeft: 8 }}>#{item.origin_id}</span>}
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("observationPoolDetailReason")}:</strong>
          <pre style={{ margin: "4px 0", padding: 8, background: "#f5f5f5", maxHeight: 200, overflow: "auto" }}>
            {item.reason ? JSON.stringify(item.reason, null, 2) : "-"}
          </pre>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("observationPoolDetailScoreSnapshot")}:</strong>
          <pre style={{ margin: "4px 0", padding: 8, background: "#f5f5f5", maxHeight: 200, overflow: "auto" }}>
            {item.score_snapshot ? JSON.stringify(item.score_snapshot, null, 2) : "-"}
          </pre>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("observationPoolDetailCurrentScore")}:</strong>{" "}
          {item.latest_total_score != null ? item.latest_total_score.toFixed(1) : "-"}
          {item.latest_score_date && (
            <span style={{ marginLeft: 8, color: "var(--muted)", fontSize: 12 }}>
              {item.latest_score_date}
            </span>
          )}
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("observationPoolDetailDataHealth")}:</strong>{" "}
          {item.data_credibility ?? "-"}
          {item.bar_count != null && (
            <span style={{ marginLeft: 8, color: "var(--muted)", fontSize: 12 }}>
              bars={item.bar_count}
            </span>
          )}
          {item.degraded && (
            <Tag color="warning" style={{ marginLeft: 8 }}>
              {item.degraded_reason ?? "degraded"}
            </Tag>
          )}
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("observationPoolDetailPortfolioRelation")}:</strong>{" "}
          {item.target_portfolio_name ?? "-"}
          {item.has_position && (
            <Tag color="green" style={{ marginLeft: 8 }}>
              {item.position_portfolio_name ?? "position"}
            </Tag>
          )}
        </div>
        {item.note && (
          <div style={{ marginBottom: 8 }}>
            <strong>note:</strong> {item.note}
          </div>
        )}
        {item.tags.length > 0 && (
          <div style={{ marginBottom: 8 }}>
            <strong>tags:</strong>{" "}
            {item.tags.map((tag) => (
              <Tag key={tag}>{tag}</Tag>
            ))}
          </div>
        )}
      </Modal>
    );
  };

  return (
    <div
      className={`opportunity-observation-pool ${className ?? ""}`}
      data-opportunity-tab="observation"
    >
      <div style={{ marginBottom: 8, display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <Space size="small" wrap>
          <Select
            allowClear
            placeholder={t("observationPoolFilterStatus")}
            style={{ minWidth: 140 }}
            value={statusFilter}
            onChange={(v) => setStatusFilter(v)}
            options={STATUS_OPTIONS}
          />
          <Select
            allowClear
            placeholder={t("observationPoolFilterOrigin")}
            style={{ minWidth: 160 }}
            value={originFilter}
            onChange={(v) => setOriginFilter(v)}
            options={ORIGIN_OPTIONS}
          />
          <Input
            allowClear
            placeholder={t("observationPoolFilterTag")}
            style={{ width: 160 }}
            value={tagFilter}
            onChange={(e) => setTagFilter(e.target.value)}
          />
          {workbench?.watchlists && workbench.watchlists.length > 1 && (
            <Select
              allowClear
              placeholder={t("observationPoolFilterWatchlist")}
              style={{ minWidth: 160 }}
              value={watchlistFilter}
              onChange={(v) => setWatchlistFilter(v)}
              options={workbench.watchlists.map((w) => ({
                value: w.id,
                label: w.name,
              }))}
            />
          )}
          <Button icon={<ReloadOutlined />} onClick={fetchObservations} loading={loading}>
            {t("observationPoolRetry")}
          </Button>
        </Space>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>
          {t("opportunityObservationCount")}: {observations.length}
        </span>
      </div>

      {selectedRowKeys.length > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message={`${t("observationPoolBatchSelected")}: ${selectedRowKeys.length}`}
          action={
            <Space size="small" wrap>
              <Popconfirm
                title={t("observationPoolBatchArchiveConfirm")}
                onConfirm={handleBatchArchive}
                disabled={batchUpdating}
              >
                <Button size="small" icon={<InboxOutlined />} loading={batchUpdating}>
                  {t("observationPoolBatchArchive")}
                </Button>
              </Popconfirm>
              <Popconfirm
                title={t("observationPoolBatchRestoreConfirm")}
                onConfirm={handleBatchRestore}
                disabled={batchUpdating}
              >
                <Button size="small" loading={batchUpdating}>
                  {t("observationPoolBatchRestore")}
                </Button>
              </Popconfirm>
              <Select
                size="small"
                placeholder={t("observationPoolBatchUpdatePriority")}
                style={{ width: 120 }}
                value={batchPriority}
                onChange={(v) => setBatchPriority(v)}
                options={PRIORITY_OPTIONS}
              />
              <Button size="small" onClick={handleBatchUpdatePriority} loading={batchUpdating}>
                {t("observationPoolBatchApply")}
              </Button>
              <Input
                size="small"
                placeholder={t("observationPoolBatchTagPlaceholder")}
                style={{ width: 180 }}
                value={batchTagInput}
                onChange={(e) => setBatchTagInput(e.target.value)}
                onPressEnter={handleBatchUpdateTags}
              />
              <Button size="small" onClick={handleBatchUpdateTags} loading={batchUpdating}>
                {t("observationPoolBatchUpdateTags")}
              </Button>
            </Space>
          }
        />
      )}

      {renderBody()}
      {renderDetail()}

      {/* 标记当前活动 watchlist id（仅用于调试/可观测性，不影响业务） */}
      <span style={{ display: "none" }} data-active-watchlist-id={activeWatchlistId ?? ""} />
    </div>
  );
}
