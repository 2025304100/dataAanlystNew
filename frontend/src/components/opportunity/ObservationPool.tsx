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
// - 接口失败降级不抛异常，按 WP-S.6 统一错误协议展示 error_code/user_message/impact/next_actions
// - 不再只显示"重试"按钮，而是给出具体原因 + 下一步动作（前往基础数据 / 运行增量同步 / 重试 等）
// - 错误消息不暴露敏感信息，技术详情折叠
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
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  ReloadOutlined,
  InboxOutlined,
  EyeOutlined,
  ArrowRightOutlined,
  DatabaseOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { useApp } from "../../context/AppContext";
import { ApiError, requestJson } from "../../api/client";
import { enumLabel, t, template } from "../../i18n";
import { OpportunityStatusBadges } from "./OpportunityStatusBadges";
import { navigateToResearch } from "../../utils/sourceContext";

const { Text, Paragraph } = Typography;

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

// WP-S.6 统一错误协议展示模型
interface ObservationPoolErrorInfo {
  error_code: string;
  user_message: string;
  impact: string;
  retryable: boolean;
  status_code?: number;
  correlation_id?: string;
  // 已映射为本地图例的下一步动作
  next_actions: Array<{
    label: string;
    action_type: "retry" | "redirect" | "configure" | "dismiss" | "view_details" | "sync";
    target?: string | null;
    reason?: string | null;
  }>;
  // 原始技术详情（折叠展示，不暴露给普通用户）
  technical_details?:
    | {
        exception_type?: string | null;
        error_message?: string | null;
        stack_summary?: string | null;
      }
    | string
    | null;
}

// 状态/来源筛选可选项
const STATUS_VALUES = ["watching", "ready", "invalid", "archived"];
const ORIGIN_VALUES = ["manual", "candidate", "scan_result", "alert", "legacy_manual_unknown"];
const statusLabel = (value: string) => enumLabel("observationPoolStatus", value);
const originLabel = (value: string) => enumLabel("observationPoolOrigin", value);

// 优先级可选值
const PRIORITY_OPTIONS = [
  { value: 0, label: "0" },
  { value: 25, label: "25" },
  { value: 50, label: "50" },
  { value: 75, label: "75" },
  { value: 100, label: "100" },
];

/**
 * 将任意异常映射为 ObservationPoolErrorInfo（WP-S.6 统一错误协议）。
 *
 * 优先复用 ApiError 携带的 error_code/user_message/next_actions；
 * 对于非 ApiError（如原生 TypeError 网络错误、超时）按场景映射中文文案 + 下一步动作。
 */
function toErrorInfo(err: unknown): ObservationPoolErrorInfo {
  if (err instanceof ApiError) {
    const code = err.error_code ?? "UNKNOWN_ERROR";
    const userMessage = err.user_message ?? err.message ?? t("observationPoolErrorUnknownMessage");
    const impact = err.impact ?? "";
    const retryable = err.retryable ?? false;
    const nextActions = err.next_actions && err.next_actions.length > 0
      ? err.next_actions
      : retryable
        ? [{ label: t("observationPoolErrorActionRetry"), action_type: "retry" as const }]
        : [];
    return {
      error_code: code,
      user_message: userMessage,
      impact,
      retryable,
      status_code: err.status_code,
      correlation_id: err.correlation_id,
      next_actions: nextActions,
      technical_details: err.detail as ObservationPoolErrorInfo["technical_details"],
    };
  }
  // 非 ApiError：兜底为 UNKNOWN_ERROR
  const fallbackMsg = err instanceof Error ? err.message : String(err);
  return {
    error_code: "UNKNOWN_ERROR",
    user_message: t("observationPoolErrorUnknownMessage"),
    impact: "",
    retryable: true,
    next_actions: [{ label: t("observationPoolErrorActionRetry"), action_type: "retry" }],
    technical_details: fallbackMsg,
  };
}

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
  const [errorInfo, setErrorInfo] = useState<ObservationPoolErrorInfo | null>(null);

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

  // 技术详情展开（错误展示用）
  const [showTechnical, setShowTechnical] = useState(false);

  // 实际查询的 watchlistId（支持多名单筛选）
  const effectiveWatchlistId = watchlistFilter ?? watchlistId;

  const fetchObservations = useCallback(async () => {
    if (!effectiveWatchlistId) {
      setObservations([]);
      setErrorInfo(null);
      return;
    }
    setLoading(true);
    setErrorInfo(null);
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
      // WP-S.6 统一错误协议：解析 error_code/user_message/impact/next_actions
      setErrorInfo(toErrorInfo(err));
    } finally {
      setLoading(false);
    }
  }, [effectiveWatchlistId, statusFilter, originFilter, tagFilter]);

  useEffect(() => {
    fetchObservations();
  }, [fetchObservations]);

  // 错误展示中的"下一步动作"统一执行入口
  const handleNextAction = useCallback(
    (actionType: string, target?: string | null) => {
      switch (actionType) {
        case "retry":
          fetchObservations();
          break;
        case "redirect":
          if (target === "/market-data" || target === "macro") {
            ctx.setActiveTab("macro");
          } else if (target === "/settings") {
            ctx.setActiveTab("settings");
          } else {
            // 默认跳转到基础数据 tab
            ctx.setActiveTab("macro");
          }
          break;
        case "configure":
          ctx.setActiveTab("settings");
          break;
        case "dismiss":
          setErrorInfo(null);
          break;
        case "view_details":
          setShowTechnical(true);
          break;
        default:
          // 兜底：重试
          fetchObservations();
      }
    },
    [fetchObservations, ctx],
  );

  // 单条错误 toast 的统一封装：将 ApiError 转中文 user_message
  const showActionError = useCallback((err: unknown, fallbackKey: string) => {
    if (err instanceof ApiError && err.user_message) {
      message.error(err.user_message);
    } else if (err instanceof Error && err.message) {
      message.error(err.message);
    } else {
      message.error(t(fallbackKey));
    }
  }, []);

  // 跳转到基础数据 tab（用于"数据未准备好"场景的下一步动作）
  const handleGoToMarketData = useCallback(() => {
    ctx.setActiveTab("macro");
  }, [ctx]);

  // 触发增量同步（用于"数据未准备好"场景的下一步动作）
  const handleRunSync = useCallback(() => {
    ctx.runSync();
  }, [ctx]);

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
        } catch (err) {
          failCount += 1;
          // 第一条失败立即提示，但继续处理后续项
          if (failCount === 1) {
            showActionError(err, "observationPoolBatchArchiveFailed");
          }
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
  }, [selectedRowKeys, effectiveWatchlistId, fetchObservations, showActionError]);

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
        } catch (err) {
          failCount += 1;
          if (failCount === 1) {
            showActionError(err, "observationPoolBatchRestoreFailed");
          }
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
  }, [selectedRowKeys, effectiveWatchlistId, fetchObservations, showActionError]);

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
        } catch (err) {
          failCount += 1;
          if (failCount === 1) {
            showActionError(err, "observationPoolBatchUpdateFailed");
          }
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
  }, [selectedRowKeys, effectiveWatchlistId, batchPriority, fetchObservations, showActionError]);

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
        } catch (err) {
          failCount += 1;
          if (failCount === 1) {
            showActionError(err, "observationPoolBatchUpdateFailed");
          }
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
  }, [selectedRowKeys, effectiveWatchlistId, batchTagInput, fetchObservations, showActionError]);

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
        render: (v: string) => <Tag>{originLabel(v)}</Tag>,
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
        render: (v: string) => <Tag color={v === "archived" ? "default" : "blue"}>{statusLabel(v)}</Tag>,
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
                  } catch (err) {
                    showActionError(err, "observationPoolArchiveFailed");
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
                  } catch (err) {
                    showActionError(err, "observationPoolRestoreFailed");
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
    [fetchObservations, ctx, statusFilter, originFilter, tagFilter, watchlistFilter, showActionError],
  );

  // 渲染统一错误协议展示（WP-S.6）
  const renderError = () => {
    if (!errorInfo) return null;
    const info = errorInfo;
    // 标题：优先使用 user_message，否则使用通用标题
    const title = info.user_message || t("observationPoolErrorTitle");
    // 错误码标签：以稳定的 error_code 显示，便于排查
    const codeTag = (
      <Space size={4} wrap>
        <Tag color="error">{info.error_code}</Tag>
        {info.status_code != null && info.status_code > 0 && (
          <Tag color="warning">HTTP {info.status_code}</Tag>
        )}
        {info.retryable && (
          <Tag color="processing">{t("observationPoolErrorRetryableYes")}</Tag>
        )}
      </Space>
    );
    // 下一步动作按钮：根据 action_type 渲染对应的入口
    // 检查 next_actions 是否已包含 redirect / sync 动作（避免与下方兜底按钮重复）
    const hasRedirectAction = info.next_actions.some(
      (a) => a.action_type === "redirect",
    );
    const hasSyncAction = info.next_actions.some(
      (a) => a.action_type === "sync",
    );
    const actionButtons = (
      <Space size="small" wrap>
        {info.next_actions.map((action, idx) => {
          const icon =
            action.action_type === "retry"
              ? <ReloadOutlined />
              : action.action_type === "redirect" && (action.target === "/market-data" || action.target === "macro")
                ? <DatabaseOutlined />
                : action.action_type === "configure"
                  ? <DatabaseOutlined />
                  : <SyncOutlined />;
          return (
            <Button
              key={`${action.action_type}-${idx}`}
              size="small"
              type={action.action_type === "retry" ? "primary" : "default"}
              icon={icon}
              onClick={() => handleNextAction(action.action_type, action.target)}
            >
              {action.label}
            </Button>
          );
        })}
        {/* 数据未准备好场景：额外暴露"前往基础数据"和"运行增量同步"入口
            （后端通过 STALE_DATA / DATA_SYNC_TIMEOUT / DATA_NOT_READY 等错误码触发，
             若 next_actions 已包含同类型动作则不重复渲染，避免按钮重复） */}
        {(info.error_code === "STALE_DATA" ||
          info.error_code === "DATA_SYNC_TIMEOUT" ||
          info.error_code === "DATA_NOT_READY") && (
          <>
            {!hasRedirectAction && (
              <Button
                size="small"
                icon={<DatabaseOutlined />}
                onClick={handleGoToMarketData}
              >
                {t("observationPoolErrorActionGoMarketData")}
              </Button>
            )}
            {!hasSyncAction && (
              <Button
                size="small"
                icon={<SyncOutlined />}
                onClick={handleRunSync}
              >
                {t("observationPoolErrorActionRunSync")}
              </Button>
            )}
          </>
        )}
      </Space>
    );

    return (
      <Alert
        type="error"
        showIcon
        message={
          <div>
            <div style={{ marginBottom: 4 }}>
              <Text strong>{title}</Text>
            </div>
            <div style={{ marginBottom: 4 }}>{codeTag}</div>
            {info.impact && (
              <div style={{ marginBottom: 4, color: "var(--muted)", fontSize: 12 }}>
                <Text type="secondary">
                  {t("observationPoolErrorImpact")}: {info.impact}
                </Text>
              </div>
            )}
            {info.correlation_id && (
              <div style={{ marginBottom: 4, color: "var(--muted)", fontSize: 12 }}>
                <Text type="secondary" code>
                  {t("observationPoolErrorCorrelation")}: {info.correlation_id.slice(0, 8)}
                </Text>
              </div>
            )}
          </div>
        }
        description={
          <div>
            <div style={{ marginBottom: 8 }}>
              <Text type="secondary" strong>
                {t("observationPoolErrorActions")}
              </Text>
            </div>
            {actionButtons}
            {/* 技术详情折叠区域：默认隐藏，避免暴露 SQL/堆栈给普通用户 */}
            {info.technical_details && (
              <div style={{ marginTop: 8 }}>
                <Button
                  size="small"
                  type="link"
                  onClick={() => setShowTechnical((v) => !v)}
                >
                  {t("observationPoolErrorToggleTechnical")}
                </Button>
                {showTechnical && (
                  <pre
                    style={{
                      margin: "4px 0",
                      padding: 8,
                      background: "#f5f5f5",
                      maxHeight: 200,
                      overflow: "auto",
                      fontSize: 12,
                    }}
                  >
                    {typeof info.technical_details === "string"
                      ? info.technical_details
                      : JSON.stringify(info.technical_details, null, 2)}
                  </pre>
                )}
              </div>
            )}
          </div>
        }
        action={
          // Alert 右侧主操作：可重试时显示"重试加载"
          info.retryable ? (
            <Button size="small" type="primary" onClick={() => fetchObservations()}>
              {t("observationPoolErrorActionRetry")}
            </Button>
          ) : undefined
        }
      />
    );
  };

  // 空/错/加载状态
  const renderBody = () => {
    if (!workbench) {
      return (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin tip={t("opportunityObservationLoading")}>
            <div style={{ minHeight: 48 }} />
          </Spin>
        </div>
      );
    }
    if (!primaryWatchlist && !watchlistFilter) {
      return <Empty description={t("opportunityObservationNoWatchlist")} />;
    }
    if (loading && observations.length === 0 && !errorInfo) {
      return (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin tip={t("opportunityObservationLoading")}>
            <div style={{ minHeight: 48 }} />
          </Spin>
        </div>
      );
    }
    if (errorInfo && observations.length === 0) {
      return renderError();
    }
    if (observations.length === 0 && !errorInfo) {
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
          <strong>{t("observationPoolDetailOrigin")}:</strong> <Tag>{originLabel(item.origin_type)}</Tag>
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
              {template("observationPoolDetailBars", { count: item.bar_count })}
            </span>
          )}
          {item.degraded && (
            <Tag color="warning" style={{ marginLeft: 8 }}>
              {item.degraded_reason ?? t("observationPoolDetailDegraded")}
            </Tag>
          )}
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>{t("observationPoolDetailPortfolioRelation")}:</strong>{" "}
          {item.target_portfolio_name ?? "-"}
          {item.has_position && (
            <Tag color="green" style={{ marginLeft: 8 }}>
              {item.position_portfolio_name ?? t("observationPoolDetailPosition")}
            </Tag>
          )}
        </div>
        {item.note && (
          <div style={{ marginBottom: 8 }}>
            <strong>{t("observationPoolDetailNote")}:</strong> {item.note}
          </div>
        )}
        {item.tags.length > 0 && (
          <div style={{ marginBottom: 8 }}>
            <strong>{t("observationPoolDetailTags")}:</strong>{" "}
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
            options={STATUS_VALUES.map((value) => ({ value, label: statusLabel(value) }))}
          />
          <Select
            allowClear
            placeholder={t("observationPoolFilterOrigin")}
            style={{ minWidth: 160 }}
            value={originFilter}
            onChange={(v) => setOriginFilter(v)}
            options={ORIGIN_VALUES.map((value) => ({ value, label: originLabel(value) }))}
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
