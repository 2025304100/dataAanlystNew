// UAT-PAGES.2：机会中心-已排除候选池
//
// 数据来源：
// - GET /api/v1/discovery/excluded（返回已排除候选列表，含排除事件信息）
// - POST /api/v1/discovery/candidates/{id}/restore（恢复到候选池或观察池）
//
// 关键约束：
// - 使用真实后端数据，不伪造
// - 支持筛选（按标的代码/名称/排除时间区间/原因类型）
// - 支持详情查看（点击查看完整排除事件信息）
// - 支持恢复操作（写 opportunity_transition_events 审计事件）
// - 错误处理：使用统一错误协议（中文文案 + 重试按钮）
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
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
  EyeOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import { type Dayjs } from "dayjs";
import { useApp } from "../../context/AppContext";
import { api } from "../../api/client";
import { actionLabel, assetTypeLabel, enumLabel, stageLabel, t } from "../../i18n";

// 已排除候选项（对齐后端 list_excluded_candidates 返回字段）
interface ExcludedItem {
  candidate_id: number | null;
  symbol: string | null;
  name: string | null;
  asset_type: string | null;
  scan_run_id: number | null;
  quality_score: number | null;
  timing_score: number | null;
  priority_score: number | null;
  stage: string | null;
  action: string | null;
  created_at: string | null;
  // 排除事件字段
  event_id: number | null;
  excluded_at: string | null;
  actor_type: string;
  exclude_reason: Record<string, unknown> | string | null;
  from_status: string | null;
  to_status: string;
  idempotency_key: string;
}

const ACTOR_VALUES = ["user", "system", "migration"];
const actorLabel = (value: string) => enumLabel("actorType", value);

export interface ExcludedPoolProps {
  className?: string;
}

export default function ExcludedPool({ className }: ExcludedPoolProps) {
  const ctx = useApp();
  const { workbench } = ctx;

  const [items, setItems] = useState<ExcludedItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 筛选条件
  const [symbolFilter, setSymbolFilter] = useState<string>("");
  const [reasonFilter, setReasonFilter] = useState<string>("");
  const [excludeDateFrom, setExcludeDateFrom] = useState<Dayjs | null>(null);
  const [excludeDateTo, setExcludeDateTo] = useState<Dayjs | null>(null);

  // 详情弹窗
  const [detailItem, setDetailItem] = useState<ExcludedItem | null>(null);

  // 恢复目标选择弹窗
  const [restoreWatchlistId, setRestoreWatchlistId] = useState<number | undefined>(undefined);
  const [restoring, setRestoring] = useState(false);

  // 当前恢复操作的候选
  const [restoreCandidate, setRestoreCandidate] = useState<ExcludedItem | null>(null);

  // 候选的主观察池（用于恢复到观察池默认选项）
  const primaryWatchlist = useMemo(() => {
    const lists = workbench?.watchlists ?? [];
    return lists.find((w) => w.list_type === "watch") ?? lists[0] ?? null;
  }, [workbench?.watchlists]);

  const fetchExcluded = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listExcludedCandidates({
        symbol: symbolFilter.trim() || undefined,
        reasonType: reasonFilter.trim() || undefined,
        excludeDateFrom: excludeDateFrom ? excludeDateFrom.format("YYYY-MM-DD") : undefined,
        excludeDateTo: excludeDateTo ? excludeDateTo.format("YYYY-MM-DD") : undefined,
        limit: 200,
      });
      setItems(data ?? []);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("excludedPoolError"));
    } finally {
      setLoading(false);
    }
  }, [symbolFilter, reasonFilter, excludeDateFrom, excludeDateTo]);

  useEffect(() => {
    fetchExcluded();
  }, [fetchExcluded]);

  // 恢复候选
  const handleRestore = useCallback(
    async (item: ExcludedItem, target: "candidate" | "observation", watchlistId?: number) => {
      if (item.candidate_id == null) {
        message.warning(t("excludedPoolRestoreFailed"));
        return;
      }
      setRestoring(true);
      try {
        const result = await api.restoreDiscoveryCandidate(item.candidate_id, {
          target,
          watchlistId: target === "observation" ? watchlistId : undefined,
        });
        if (result.already_restored) {
          message.info(t("excludedPoolRestoreIdempotent"));
        } else if (target === "observation") {
          message.success(t("excludedPoolRestoreToObservationDone"));
        } else {
          message.success(t("excludedPoolRestoreDone"));
        }
        setRestoreCandidate(null);
        fetchExcluded();
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        message.error(`${t("excludedPoolRestoreFailed")}: ${msg}`);
      } finally {
        setRestoring(false);
      }
    },
    [fetchExcluded],
  );

  // 渲染排除原因（统一为字符串）
  const renderReason = useCallback((reason: ExcludedItem["exclude_reason"]) => {
    if (reason == null) return "-";
    if (typeof reason === "string") return reason || "-";
    if (typeof reason === "object") {
      const r = reason as Record<string, unknown>;
      const reasonText = r.reason;
      if (typeof reasonText === "string" && reasonText) return reasonText;
      try {
        return JSON.stringify(reason);
      } catch {
        return "-";
      }
    }
    return String(reason);
  }, []);

  const columns: ColumnsType<ExcludedItem> = useMemo(
    () => [
      {
        title: t("excludedPoolColumnSymbol"),
        key: "symbol",
        width: 110,
        render: (_v: unknown, row: ExcludedItem) => row.symbol ?? "-",
      },
      {
        title: t("excludedPoolColumnName"),
        key: "name",
        width: 160,
        render: (_v: unknown, row: ExcludedItem) => row.name ?? "-",
      },
      {
        title: t("excludedPoolColumnScores"),
        key: "scores",
        width: 160,
        render: (_v: unknown, row: ExcludedItem) => (
          <Space size={4} wrap>
            <Tag color="blue">Q{row.quality_score != null ? row.quality_score.toFixed(1) : "-"}</Tag>
            <Tag color="green">T{row.timing_score != null ? row.timing_score.toFixed(1) : "-"}</Tag>
            <Tag color="orange">P{row.priority_score != null ? row.priority_score.toFixed(1) : "-"}</Tag>
          </Space>
        ),
      },
      {
        title: t("excludedPoolColumnExcludedAt"),
        dataIndex: "excluded_at",
        key: "excluded_at",
        width: 170,
        render: (v: string | null) => v ?? "-",
      },
      {
        title: t("excludedPoolColumnReason"),
        key: "reason",
        width: 200,
        ellipsis: true,
        render: (_v: unknown, row: ExcludedItem) => renderReason(row.exclude_reason),
      },
      {
        title: t("excludedPoolColumnActor"),
        dataIndex: "actor_type",
        key: "actor_type",
        width: 100,
        render: (v: string) => <Tag>{actorLabel(v)}</Tag>,
      },
      {
        title: t("excludedPoolColumnScanRun"),
        dataIndex: "scan_run_id",
        key: "scan_run_id",
        width: 100,
        render: (v: number | null) => (v != null ? `#${v}` : "-"),
      },
      {
        title: t("excludedPoolColumnActions"),
        key: "actions",
        width: 200,
        render: (_v: unknown, row: ExcludedItem) => (
          <Space size="small">
            <Button
              size="small"
              type="link"
              icon={<EyeOutlined />}
              onClick={() => setDetailItem(row)}
            >
              {t("excludedPoolDetailView")}
            </Button>
            <Button
              size="small"
              type="link"
              icon={<UndoOutlined />}
              onClick={() => {
                setRestoreCandidate(row);
                setRestoreWatchlistId(primaryWatchlist?.id);
              }}
            >
              {t("excludedPoolRestore")}
            </Button>
          </Space>
        ),
      },
    ],
    [renderReason, primaryWatchlist?.id],
  );

  // 空/错/加载状态
  const renderBody = () => {
    if (loading && items.length === 0) {
      return (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin tip={t("excludedPoolError")} />
        </div>
      );
    }
    if (error && items.length === 0) {
      return (
        <Alert
          type="error"
          showIcon
          message={t("excludedPoolError")}
          description={error}
          action={
            <Button size="small" onClick={fetchExcluded}>
              {t("excludedPoolRetry")}
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
              <div>{t("excludedPoolEmpty")}</div>
              <div style={{ color: "var(--muted)", fontSize: 12, marginTop: 4 }}>
                {t("excludedPoolEmptyHint")}
              </div>
            </div>
          }
        />
      );
    }
    return (
      <Table<ExcludedItem>
        rowKey={(row) => `${row.event_id ?? "ev"}-${row.candidate_id ?? "no"}`}
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
        title={`${t("excludedPoolDetailView")} · ${item.symbol ?? ""}`}
        footer={[
          <Button key="close" onClick={() => setDetailItem(null)}>
            {t("excludedPoolDetailClose")}
          </Button>,
        ]}
        onCancel={() => setDetailItem(null)}
      >
        <div style={{ marginBottom: 8 }}>
          <strong>{t("excludedPoolDetailCandidate")}:</strong>
          <div style={{ marginTop: 4, marginLeft: 8, fontSize: 13 }}>
            <div>{t("symbol")}: {item.symbol ?? "-"} | {t("excludedPoolDetailName")}: {item.name ?? "-"}</div>
            <div>{t("excludedPoolDetailAssetType")}: {assetTypeLabel(item.asset_type)} | {t("excludedPoolColumnScanRun")}: {item.scan_run_id ?? "-"}</div>
            <div>
              {t("quality")}: {item.quality_score != null ? item.quality_score.toFixed(1) : "-"} |{" "}
              {t("timing")}: {item.timing_score != null ? item.timing_score.toFixed(1) : "-"} |{" "}
              {t("priority")}: {item.priority_score != null ? item.priority_score.toFixed(1) : "-"}
            </div>
            <div>{t("stage")}: {stageLabel(item.stage)} | {t("action")}: {actionLabel(item.action)}</div>
            <div>{t("excludedPoolDetailCreatedAt")}: {item.created_at ?? "-"}</div>
          </div>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("excludedPoolDetailEvent")}:</strong>
          <div style={{ marginTop: 4, marginLeft: 8, fontSize: 13 }}>
            <div>{t("excludedPoolDetailEventId")}: {item.event_id ?? "-"} | {t("excludedPoolDetailActorType")}: {actorLabel(item.actor_type)}</div>
            <div>{t("excludedPoolDetailExcludedAt")}: {item.excluded_at ?? "-"}</div>
            <div>{t("excludedPoolDetailFromStatus")}: {enumLabel("opportunityStatus", item.from_status)} → {t("excludedPoolDetailToStatus")}: {enumLabel("opportunityStatus", item.to_status)}</div>
            <div style={{ marginTop: 4 }}>
              <span>{t("excludedPoolColumnReason")}: </span>
              <span>{renderReason(item.exclude_reason)}</span>
            </div>
            <div style={{ marginTop: 4 }}>
              <span>{t("excludedPoolDetailIdempotencyKey")}: </span>
              <code style={{ fontSize: 11 }}>{item.idempotency_key}</code>
            </div>
          </div>
        </div>
      </Modal>
    );
  };

  // 渲染恢复弹窗
  const renderRestoreModal = () => {
    if (!restoreCandidate) return null;
    const item = restoreCandidate;
    return (
      <Modal
        open={!!restoreCandidate}
        title={`${t("excludedPoolRestore")} · ${item.symbol ?? ""}`}
        onCancel={() => setRestoreCandidate(null)}
        footer={[
          <Button key="cancel" onClick={() => setRestoreCandidate(null)}>
            {t("excludedPoolDetailClose")}
          </Button>,
          <Popconfirm
            key="restore-candidate"
            title={t("excludedPoolRestoreConfirm")}
            onConfirm={() => handleRestore(item, "candidate")}
            disabled={restoring}
          >
            <Button loading={restoring} icon={<UndoOutlined />}>
              {t("excludedPoolRestoreToCandidate")}
            </Button>
          </Popconfirm>,
          <Popconfirm
            key="restore-observation"
            title={t("excludedPoolRestoreConfirm")}
            onConfirm={() => handleRestore(item, "observation", restoreWatchlistId ?? primaryWatchlist?.id)}
            disabled={restoring || (!restoreWatchlistId && !primaryWatchlist)}
          >
            <Button
              type="primary"
              loading={restoring}
              icon={<UndoOutlined />}
            >
              {t("excludedPoolRestoreToObservation")}
            </Button>
          </Popconfirm>,
        ]}
      >
        <div style={{ marginBottom: 8 }}>
          <span>{t("excludedPoolRestoreToObservation")}:</span>
          <Select
            allowClear
            style={{ minWidth: 200, marginLeft: 8 }}
            placeholder={t("excludedPoolRestoreToObservation")}
            value={restoreWatchlistId ?? primaryWatchlist?.id}
            onChange={(v) => setRestoreWatchlistId(v)}
            options={(workbench?.watchlists ?? []).map((w) => ({
              value: w.id,
              label: w.name,
            }))}
          />
        </div>
      </Modal>
    );
  };

  return (
    <div
      className={`opportunity-excluded-pool ${className ?? ""}`}
      data-opportunity-tab="excluded"
    >
      <div style={{ marginBottom: 8, display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <Space size="small" wrap>
          <Input
            allowClear
            placeholder={t("excludedPoolFilterSymbol")}
            style={{ width: 180 }}
            value={symbolFilter}
            onChange={(e) => setSymbolFilter(e.target.value)}
          />
          <Input
            allowClear
            placeholder={t("excludedPoolFilterReason")}
            style={{ width: 180 }}
            value={reasonFilter}
            onChange={(e) => setReasonFilter(e.target.value)}
          />
          <DatePicker
            placeholder={t("excludedPoolFilterDateFrom")}
            value={excludeDateFrom}
            onChange={(d) => setExcludeDateFrom(d)}
          />
          <DatePicker
            placeholder={t("excludedPoolFilterDateTo")}
            value={excludeDateTo}
            onChange={(d) => setExcludeDateTo(d)}
          />
          <Select
            allowClear
            placeholder={t("excludedPoolColumnActor")}
            style={{ minWidth: 120 }}
            options={ACTOR_VALUES.map((value) => ({ value, label: actorLabel(value) }))}
          />
          <Button icon={<ReloadOutlined />} onClick={fetchExcluded} loading={loading}>
            {t("excludedPoolRetry")}
          </Button>
        </Space>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>
          {t("excludedPoolCount")}: {items.length}
        </span>
      </div>

      {renderBody()}
      {renderDetail()}
      {renderRestoreModal()}

      {/* 标记最新加载时间（仅用于调试/可观测性） */}
      <span style={{ display: "none" }} data-excluded-count={items.length} />
    </div>
  );
}
