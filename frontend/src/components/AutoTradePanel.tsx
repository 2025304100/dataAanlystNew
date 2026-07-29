import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Empty,
  InputNumber,
  Modal,
  Skeleton,
  Statistic,
  Switch,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { QuestionCircleOutlined, RollbackOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
import { enumLabel, sideLabel, t } from "../i18n";
import { money, formatRelativeTime } from "../utils/format";
import type { AutoTradePlanItem, AutoTradeResult } from "../types";
// WP-S-FIX.4：阻断操作就地处理（按钮替换为 CapabilityGateButton）
import { CapabilityGateButton } from "./capability/CapabilityGateButton";

/**
 * P2-3：自动交易执行面板。
 *
 * 数据来源：POST /portfolios/{id}/auto-trade/execute
 * - dry_run=True：只返回买卖计划，不实际下单（推荐首次使用）
 * - dry_run=False：按计划实际下单（含手续费/滑点/T+1校验）
 *
 * 信号源：Score.action 字段
 * - 卖出：action=exit 全卖，action=reduce 卖一半
 * - 买入：action=open/buy_dip 且 compute_position_budget.can_open=True
 *
 * 前置条件：
 * - 组合必须 account_type="simulated"
 * - 组合必须已开启 auto_trade_enabled（在组合管理 Modal 中开启）
 *
 * 交互：
 * - 切换 dry_run 开关 + 调整 buy_candidate_limit
 * - 点击"执行"按钮触发评估
 * - 结果以卖出/买入两张表展示，含执行状态与风控阻断原因
 *
 * WP6.6 增量（不修改以上原有逻辑）：
 * - 成员级执行状态展示（auto/confirm/manual、持仓、风控阻断、数据过期）
 * - dry-run 差异对比可视化（旧 vs 新来源）
 * - 双跑切换 UI（开关状态 + 回退旧来源 + 逐组合切换）
 */
// ----------------------------------------------------------------------------
// WP6.6 类型定义（与后端 app/api/routes/auto_trade.py 响应对齐）
// ----------------------------------------------------------------------------

interface MemberStatusItem {
  member_id: number;
  symbol_id: number;
  symbol: string | null;
  status: string;
  execution_mode: string;
  source_type: string;
  manual_lock: boolean;
  has_position: boolean;
  position_quantity: number;
  latest_order: {
    order_id: number;
    side: string;
    status: string;
    created_at: string | null;
    source_type: string | null;
    signal_id: number | null;
    execution_mode: string | null;
    client_order_key: string | null;
    rejection_code: string | null;
    rejection_detail: string | null;
  } | null;
  data_health: {
    healthy: boolean;
    reason: string;
    kline_latest_at: string | null;
    score_latest_at: string | null;
    rule_version_id: number | null;
  };
  risk_blocked: boolean;
  data_expired: boolean;
}

interface DryRunDiffItem {
  symbol_id: number;
  side: string;
  old_action: string | null;
  new_action: string | null;
  reason: string;
  detail: string;
}

interface MemberSourceStatus {
  portfolio_id: number;
  enabled: boolean;
  env_var_name: string;
  env_flag: string;
  whitelist_match: boolean;
  blacklist_match: boolean;
  whitelist: number[];
  blacklist: number[];
}

// 时间戳本地化（Asia/Shanghai）显示
function formatShanghai(dt: string | null): string {
  if (!dt) return "-";
  try {
    const d = new Date(dt);
    if (Number.isNaN(d.getTime())) return String(dt);
    return d.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
  } catch {
    return String(dt);
  }
}

// 执行模式本地化
function executionModeLabel(mode: string): string {
  if (mode === "manual") return t("modeManual");
  if (mode === "confirm") return t("modeConfirm");
  if (mode === "auto") return t("modeAuto");
  return mode;
}

// 成员状态本地化
function memberStatusLabel(status: string): string {
  if (status === "active") return t("statusActive");
  if (status === "paused") return t("statusPaused");
  if (status === "archived") return t("statusArchived");
  return status;
}

// 差异原因本地化
function diffReasonLabel(reason: string): string {
  switch (reason) {
    case "member_missing":
      return t("autoTradeMember.reasonCodes.memberMissing");
    case "member_paused":
      return t("autoTradeMember.reasonCodes.memberPaused");
    case "signal_diff":
      return t("autoTradeMember.reasonCodes.signalDiff");
    case "data_expired":
      return t("autoTradeMember.reasonCodes.dataExpired");
    case "risk_blocked":
      return t("autoTradeMember.reasonCodes.riskBlocked");
    default:
      return reason;
  }
}

// 差异原因颜色
function diffReasonColor(reason: string): string {
  switch (reason) {
    case "member_missing":
      return "orange";
    case "member_paused":
      return "gold";
    case "signal_diff":
      return "blue";
    case "data_expired":
      return "red";
    case "risk_blocked":
      return "volcano";
    default:
      return "default";
  }
}

export default function AutoTradePanel() {
  const ctx = useApp();
  const portfolioId = ctx.portfolioId;
  // 当前组合对象（用于判断 auto_trade_enabled）
  const currentPortfolio = ctx.portfolios.find((p) => p.id === portfolioId);
  const isSimulated = currentPortfolio?.account_type === "simulated";
  const autoTradeEnabled = Number(currentPortfolio?.auto_trade_enabled) === 1;

  const [dryRun, setDryRun] = useState(true);
  const [buyCandidateLimit, setBuyCandidateLimit] = useState(10);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AutoTradeResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // WP6.6 新增状态
  const [members, setMembers] = useState<MemberStatusItem[]>([]);
  const [membersLoading, setMembersLoading] = useState(false);
  const [membersError, setMembersError] = useState<string | null>(null);

  const [diffs, setDiffs] = useState<DryRunDiffItem[]>([]);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);

  const [sourceStatus, setSourceStatus] = useState<MemberSourceStatus | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [rollingBack, setRollingBack] = useState(false);

  const execute = useCallback(async () => {
    if (!portfolioId) return;
    // 实际下单前再次确认（dry_run=False 时）
    if (!dryRun && !window.confirm(t("autoTradeExecuteConfirm"))) return;

    setRunning(true);
    setError(null);
    try {
      const data = await api.executeAutoTrade(portfolioId, {
        dry_run: dryRun,
        buy_candidate_limit: buyCandidateLimit,
      });
      setResult(data as AutoTradeResult);
      ctx.showToast("success", t("autoTradeSuccess"));
      // 实际下单后刷新 workbench（持仓/现金变化）
      if (!dryRun) {
        await ctx.loadWorkbench();
      }
    } catch (err: any) {
      const msg = err?.message || String(err);
      setError(msg);
      ctx.showToast("error", t("autoTradeFailed") + ": " + msg);
    } finally {
      setRunning(false);
    }
  }, [portfolioId, dryRun, buyCandidateLimit, ctx]);

  // WP6.6：加载成员级状态
  const loadMembers = useCallback(async () => {
    if (!portfolioId) {
      setMembers([]);
      return;
    }
    setMembersLoading(true);
    setMembersError(null);
    try {
      const data = await api.getAutoTradeMemberStatus(portfolioId);
      setMembers(data.members ?? []);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setMembersError(msg || t("autoTradeMember.loadMembersFailed"));
    } finally {
      setMembersLoading(false);
    }
  }, [portfolioId]);

  // WP6.6：加载双跑差异
  const loadDiffs = useCallback(async () => {
    if (!portfolioId) {
      setDiffs([]);
      return;
    }
    setDiffLoading(true);
    setDiffError(null);
    try {
      const data = await api.getAutoTradeDryRunDiff(portfolioId);
      setDiffs(data.diffs ?? []);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setDiffError(msg || t("autoTradeMember.loadDiffFailed"));
    } finally {
      setDiffLoading(false);
    }
  }, [portfolioId]);

  // WP6.6：加载新来源开关状态
  const loadSourceStatus = useCallback(async () => {
    if (!portfolioId) {
      setSourceStatus(null);
      return;
    }
    setSourceLoading(true);
    setSourceError(null);
    try {
      const data = await api.getAutoTradeMemberSourceStatus(portfolioId);
      setSourceStatus(data);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setSourceError(msg || t("autoTradeMember.loadSourceStatusFailed"));
    } finally {
      setSourceLoading(false);
    }
  }, [portfolioId]);

  // WP6.6：回退到旧来源
  const handleRollback = useCallback(async () => {
    if (!portfolioId) return;
    Modal.confirm({
      title: t("autoTradeMember.rollbackToOldSource"),
      content: t("autoTradeMember.rollbackConfirmDesc"),
      okText: t("autoTradeMember.rollbackToOldSource"),
      cancelText: t("cancel"),
      okButtonProps: { danger: true },
      onOk: async () => {
        setRollingBack(true);
        try {
          await api.rollbackAutoTradeToOldSource(portfolioId);
          ctx.showToast("success", t("autoTradeMember.rollbackSuccess"));
          await loadSourceStatus();
        } catch (err: any) {
          const msg = err?.message || String(err);
          ctx.showToast("error", t("autoTradeMember.rollbackFailed") + ": " + msg);
        } finally {
          setRollingBack(false);
        }
      },
    });
  }, [portfolioId, ctx, loadSourceStatus]);

  // 组合切换时清空旧结果 + 重新加载 WP6.6 数据
  useEffect(() => {
    setResult(null);
    setError(null);
    // WP6.6：组合切换时重新加载三大分区数据
    if (portfolioId && isSimulated && autoTradeEnabled) {
      loadMembers();
      loadDiffs();
      loadSourceStatus();
    } else {
      setMembers([]);
      setDiffs([]);
      setSourceStatus(null);
    }
  }, [portfolioId, isSimulated, autoTradeEnabled, loadMembers, loadDiffs, loadSourceStatus]);

  // 渲染约束：非模拟组合或未开启自动交易时显示引导
  if (!portfolioId) {
    return null;
  }
  if (!isSimulated) {
    return (
      <section className="band auto-trade-band">
        <div className="panel">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">{t("autoTradePanelTitle")}</p>
              <h2>{t("autoTradePanelTitle")}</h2>
            </div>
          </div>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={t("autoTradeOnlySimulated")}
          />
        </div>
      </section>
    );
  }
  if (!autoTradeEnabled) {
    return (
      <section className="band auto-trade-band">
        <div className="panel">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">{t("autoTradePanelTitle")}</p>
              <h2>{t("autoTradePanelTitle")}</h2>
            </div>
          </div>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={t("autoTradeDisabled")}
          />
        </div>
      </section>
    );
  }

  const sells = result?.sells ?? [];
  const buys = result?.buys ?? [];
  const errors = result?.errors ?? [];

  // 卖出表列定义
  const sellColumns = [
    {
      title: t("symbol"),
      dataIndex: "symbol",
      key: "symbol",
      render: (v: string, record: AutoTradePlanItem) => (
        <span>
          {v}
          <span style={{ color: "#94a3b8", marginLeft: 6, fontSize: 12 }}>
            {record.name}
          </span>
        </span>
      ),
    },
    {
      title: t("action"),
      dataIndex: "action",
      key: "action",
      render: (v: string) => <Tag color={v === "exit" ? "red" : "orange"}>{v}</Tag>,
    },
    {
      title: t("quantity"),
      key: "qty",
      render: (_: any, record: AutoTradePlanItem) => (
        <span>
          {record.sell_quantity} / {record.held_quantity}
        </span>
      ),
    },
    {
      title: t("price"),
      dataIndex: "ref_price",
      key: "ref_price",
      render: (v: number) => money(v, 2),
    },
    {
      title: t("status"),
      key: "status",
      render: (_: any, record: AutoTradePlanItem) =>
        record.executed ? (
          <Tag color="green">{t("autoTradeExecuted")}</Tag>
        ) : (
          <Tag>{t("autoTradePlanned")}</Tag>
        ),
    },
    {
      title: t("fee"),
      dataIndex: "fee",
      key: "fee",
      render: (v: number | null) => (v != null ? money(v, 2) : "-"),
    },
  ];

  // 买入表列定义
  const buyColumns = [
    {
      title: t("symbol"),
      dataIndex: "symbol",
      key: "symbol",
      render: (v: string, record: AutoTradePlanItem) => (
        <span>
          {v}
          <span style={{ color: "#94a3b8", marginLeft: 6, fontSize: 12 }}>
            {record.name}
          </span>
        </span>
      ),
    },
    {
      title: t("action"),
      dataIndex: "action",
      key: "action",
      render: (v: string) => <Tag color="green">{v}</Tag>,
    },
    {
      title: t("price"),
      dataIndex: "ref_price",
      key: "ref_price",
      render: (v: number) => money(v, 2),
    },
    {
      title: t("quantity"),
      dataIndex: "buy_quantity",
      key: "buy_quantity",
      render: (v: number | null) => (v != null ? v : "-"),
    },
    {
      title: t("autoTradeBlocked"),
      key: "blocked",
      render: (_: any, record: AutoTradePlanItem) => {
        if (record.can_open === true || record.executed) {
          return record.executed ? (
            <Tag color="green">{t("autoTradeExecuted")}</Tag>
          ) : (
            <Tag>{t("autoTradePlanned")}</Tag>
          );
        }
        return (
          <Tooltip title={record.blocked_reasons?.join(", ") || record.decision || ""}>
            <Tag color="red">{t("autoTradeBlocked")}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: t("fee"),
      dataIndex: "fee",
      key: "fee",
      render: (v: number | null) => (v != null ? money(v, 2) : "-"),
    },
  ];

  // ==========================================================================
  // WP6.6：成员级执行状态表格列定义
  // ==========================================================================
  const memberColumns: ColumnsType<MemberStatusItem> = [
    {
      title: t("symbol"),
      key: "symbol",
      width: 140,
      render: (_v, record) => (
        <span>
          {record.symbol ?? `#${record.symbol_id}`}
        </span>
      ),
    },
    {
      title: t("portfolioMemberColumnStatus"),
      dataIndex: "status",
      key: "status",
      width: 90,
      render: (status: string) => {
        const color = status === "active" ? "green" : status === "paused" ? "orange" : "default";
        return <Tag color={color}>{memberStatusLabel(status)}</Tag>;
      },
    },
    {
      title: t("portfolioMemberColumnExecutionMode"),
      dataIndex: "execution_mode",
      key: "execution_mode",
      width: 90,
      render: (mode: string) => executionModeLabel(mode),
    },
    {
      title: t("autoTradeMember.hasPosition"),
      key: "has_position",
      width: 80,
      render: (_v, record) =>
        record.has_position ? (
          <Tag color="blue">{t("autoTradeMember.hasPositionYes")}</Tag>
        ) : (
          <span style={{ color: "#94a3b8", fontSize: 12 }}>-</span>
        ),
    },
    {
      title: (
        <span>
          {t("autoTradeMember.riskBlocked")}
          <Tooltip title={t("autoTradeMember.riskBlockedHint")}>
            <QuestionCircleOutlined style={{ marginLeft: 4, fontSize: 11, color: "#94a3b8" }} />
          </Tooltip>
        </span>
      ),
      key: "risk_blocked",
      width: 100,
      render: (_v, record) =>
        record.risk_blocked ? (
          <Tooltip
            title={
              record.latest_order?.rejection_detail ||
              record.latest_order?.rejection_code ||
              t("autoTradeMember.riskBlocked")
            }
          >
            <Tag color="red">{t("autoTradeMember.riskBlocked")}</Tag>
          </Tooltip>
        ) : (
          <Tag color="green">{t("autoTradeMember.normal")}</Tag>
        ),
    },
    {
      title: (
        <span>
          {t("autoTradeMember.dataExpired")}
          <Tooltip title={t("autoTradeMember.dataExpiredHint")}>
            <QuestionCircleOutlined style={{ marginLeft: 4, fontSize: 11, color: "#94a3b8" }} />
          </Tooltip>
        </span>
      ),
      key: "data_expired",
      width: 100,
      render: (_v, record) =>
        record.data_expired ? (
          <Tooltip title={record.data_health?.reason || t("autoTradeMember.dataExpired")}>
            <Tag color="red">{t("autoTradeMember.dataExpired")}</Tag>
          </Tooltip>
        ) : (
          <Tag color="green">{t("autoTradeMember.normal")}</Tag>
        ),
    },
    {
      title: t("autoTradeMember.latestOrder"),
      key: "latest_order",
      render: (_v, record) => {
        if (!record.latest_order) {
          return <span style={{ color: "#94a3b8", fontSize: 12 }}>{t("autoTradeMember.noLatestOrder")}</span>;
        }
        return (
          <Tooltip
            title={
              <div style={{ fontSize: 12 }}>
                <div>{t("autoTradeMember.orderId")}: {record.latest_order.order_id}</div>
                <div>{t("autoTradeMember.orderSide")}: {sideLabel(record.latest_order.side)}</div>
                <div>{t("autoTradeMember.orderStatus")}: {enumLabel("orderStatus", record.latest_order.status)}</div>
                <div>{t("autoTradeMember.orderSourceType")}: {enumLabel("sourceType", record.latest_order.source_type)}</div>
                <div>{t("autoTradeMember.orderSignalId")}: {record.latest_order.signal_id ?? "-"}</div>
                <div>{t("autoTradeMember.orderClientKey")}: {record.latest_order.client_order_key ?? "-"}</div>
                {record.latest_order.rejection_code && (
                  <div style={{ color: "#ffccc7" }}>
                    {t("autoTradeMember.orderRejection")}: {record.latest_order.rejection_code}
                    {record.latest_order.rejection_detail ? ` - ${record.latest_order.rejection_detail}` : ""}
                  </div>
                )}
              </div>
            }
          >
            <span style={{ fontSize: 12 }}>
              #{record.latest_order.order_id} · {record.latest_order.side} ·{" "}
              {formatShanghai(record.latest_order.created_at)}
            </span>
          </Tooltip>
        );
      },
    },
  ];

  // ==========================================================================
  // WP6.6：双跑差异表列定义
  // ==========================================================================
  const diffColumns: ColumnsType<DryRunDiffItem> = [
    {
      title: t("symbol"),
      dataIndex: "symbol_id",
      key: "symbol_id",
      width: 100,
      render: (v: number) => `#${v}`,
    },
    {
      title: t("autoTradeMember.diffSide"),
      dataIndex: "side",
      key: "side",
      width: 80,
      render: (v: string) => (
        <Tag color={v === "buy" ? "green" : "red"}>{v}</Tag>
      ),
    },
    {
      title: t("autoTradeMember.diffOldAction"),
      dataIndex: "old_action",
      key: "old_action",
      width: 110,
      render: (v: string | null) => v ?? <span style={{ color: "#94a3b8" }}>-</span>,
    },
    {
      title: t("autoTradeMember.diffNewAction"),
      dataIndex: "new_action",
      key: "new_action",
      width: 110,
      render: (v: string | null) => v ?? <span style={{ color: "#94a3b8" }}>-</span>,
    },
    {
      title: t("autoTradeMember.diffReason"),
      dataIndex: "reason",
      key: "reason",
      width: 140,
      render: (v: string) => (
        <Tooltip title={diffReasonLabel(v)}>
          <Tag color={diffReasonColor(v)}>{diffReasonLabel(v)}</Tag>
        </Tooltip>
      ),
    },
    {
      title: t("autoTradeMember.diffDetail"),
      dataIndex: "detail",
      key: "detail",
      render: (v: string) => <span style={{ fontSize: 12 }}>{v}</span>,
    },
  ];

  return (
    <section className="band auto-trade-band">
      <div className="panel">
        <div className="panel-head">
          <div>
            <p className="panel-kicker">{t("autoTradePanelTitle")}</p>
            <h2>
              {t("autoTradePanelTitle")}
              <Tooltip title={t("autoTradePanelHint")}>
                <QuestionCircleOutlined
                  style={{ marginLeft: 8, fontSize: 14, color: "#94a3b8" }}
                />
              </Tooltip>
            </h2>
          </div>
          <div
            className="panel-meta"
            style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}
          >
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <Switch
                size="small"
                checked={dryRun}
                onChange={setDryRun}
              />
              <span style={{ fontSize: 12 }}>
                {dryRun ? t("autoTradeDryRun") : t("autoTradeExecute")}
              </span>
              <Tooltip title={dryRun ? t("autoTradeDryRunHint") : t("autoTradeExecuteHint")}>
                <QuestionCircleOutlined style={{ fontSize: 11, color: "#94a3b8" }} />
              </Tooltip>
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ fontSize: 12 }}>{t("autoTradeBuyCandidateLimit")}</span>
              <InputNumber
                size="small"
                min={1}
                max={50}
                value={buyCandidateLimit}
                onChange={(v) => setBuyCandidateLimit(Number(v) || 10)}
                style={{ width: 70 }}
              />
            </span>
            <CapabilityGateButton
              capabilityKey="auto_trade"
              size="small"
              type="primary"
              onClick={execute}
              loading={running}
              danger={!dryRun}
            >
              {dryRun ? t("autoTradeDryRun") : t("autoTradeExecute")}
            </CapabilityGateButton>
          </div>
        </div>

        {/* 上次执行时间 */}
        {currentPortfolio?.auto_trade_last_run_at && (
          <p className="panel-meta" style={{ fontSize: 11, marginBottom: 8 }}>
            {t("autoTradeLastRunAt")}:{" "}
            {formatRelativeTime(currentPortfolio.auto_trade_last_run_at)}
          </p>
        )}

        {running && <Skeleton active paragraph={{ rows: 3 }} />}

        {!running && error && (
          <Alert
            type="error"
            message={t("autoTradeFailed")}
            description={error}
            showIcon
            style={{ marginBottom: 12 }}
          />
        )}

        {!running && !error && !result && (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <div>
                <p>{t("autoTradeDryRunHint")}</p>
              </div>
            }
          />
        )}

        {!running && !error && result && (
          <>
            <div style={{ marginBottom: 8, display: "flex", gap: 8, alignItems: "center" }}>
              <Tag color={result.dry_run ? "blue" : "green"}>
                {result.dry_run ? t("autoTradeDryRunBadge") : t("autoTradeExecutedBadge")}
              </Tag>
              <span style={{ fontSize: 12, color: "#94a3b8" }}>
                {new Date(result.executed_at).toLocaleString()}
              </span>
            </div>

            {/* 卖出计划 */}
            <div style={{ marginBottom: 16 }}>
              <h3 style={{ fontSize: 14, marginBottom: 8 }}>
                {t("autoTradeSells").replace("{count}", String(sells.length))}
              </h3>
              {sells.length === 0 ? (
                <div className="empty" style={{ fontSize: 12, color: "#94a3b8" }}>
                  {t("autoTradeNoSells")}
                </div>
              ) : (
                <Table
                  size="small"
                  rowKey={(record, idx) => `sell-${record.symbol_id}-${idx}`}
                  dataSource={sells}
                  columns={sellColumns}
                  pagination={false}
                />
              )}
            </div>

            {/* 买入计划 */}
            <div style={{ marginBottom: 16 }}>
              <h3 style={{ fontSize: 14, marginBottom: 8 }}>
                {t("autoTradeBuys").replace("{count}", String(buys.length))}
              </h3>
              {buys.length === 0 ? (
                <div className="empty" style={{ fontSize: 12, color: "#94a3b8" }}>
                  {t("autoTradeNoBuys")}
                </div>
              ) : (
                <Table
                  size="small"
                  rowKey={(record, idx) => `buy-${record.symbol_id}-${idx}`}
                  dataSource={buys}
                  columns={buyColumns}
                  pagination={false}
                />
              )}
            </div>

            {/* 错误列表 */}
            {errors.length > 0 && (
              <div>
                <h3 style={{ fontSize: 14, marginBottom: 8, color: "#dc2626" }}>
                  {t("autoTradeErrors").replace("{count}", String(errors.length))}
                </h3>
                <ul style={{ fontSize: 12, color: "#dc2626", paddingLeft: 20 }}>
                  {errors.map((err, idx) => (
                    <li key={`err-${idx}`}>{err}</li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}

        {/* ========================================================================
            WP6.6 新增分区：成员状态 / 双跑差异 / 开关管理
            ======================================================================== */}
        <div style={{ marginTop: 24, borderTop: "1px dashed #e5e7eb", paddingTop: 16 }}>
          <h3 style={{ fontSize: 14, marginBottom: 8, display: "flex", alignItems: "center" }}>
            {t("autoTradeMember.memberStatus")}
            <Tooltip title={t("autoTradeMember.memberStatusHint")}>
              <QuestionCircleOutlined style={{ marginLeft: 6, fontSize: 12, color: "#94a3b8" }} />
            </Tooltip>
            <Button
              size="small"
              type="link"
              onClick={loadMembers}
              loading={membersLoading}
              style={{ marginLeft: "auto", padding: 0 }}
            >
              {t("refresh")}
            </Button>
          </h3>
          {membersLoading && <Skeleton active paragraph={{ rows: 2 }} />}
          {!membersLoading && membersError && (
            <Alert
              type="error"
              message={t("autoTradeMember.loadMembersFailed")}
              description={membersError}
              showIcon
              style={{ marginBottom: 8 }}
            />
          )}
          {!membersLoading && !membersError && members.length === 0 && (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t("autoTradeMember.noMembers")}
            />
          )}
          {!membersLoading && !membersError && members.length > 0 && (
            <Table
              size="small"
              rowKey={(record) => `member-${record.member_id}`}
              dataSource={members}
              columns={memberColumns}
              pagination={false}
            />
          )}
        </div>

        {/* 双跑差异 */}
        <div style={{ marginTop: 24 }}>
          <h3 style={{ fontSize: 14, marginBottom: 8, display: "flex", alignItems: "center" }}>
            {t("autoTradeMember.dryRunDiff")}
            <Tooltip title={t("autoTradeMember.dryRunDiffHint")}>
              <QuestionCircleOutlined style={{ marginLeft: 6, fontSize: 12, color: "#94a3b8" }} />
            </Tooltip>
            <Button
              size="small"
              type="link"
              onClick={loadDiffs}
              loading={diffLoading}
              style={{ marginLeft: "auto", padding: 0 }}
            >
              {t("refresh")}
            </Button>
          </h3>
          {diffLoading && <Skeleton active paragraph={{ rows: 2 }} />}
          {!diffLoading && diffError && (
            <Alert
              type="error"
              message={t("autoTradeMember.loadDiffFailed")}
              description={diffError}
              showIcon
              style={{ marginBottom: 8 }}
            />
          )}
          {!diffLoading && !diffError && diffs.length === 0 && (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t("autoTradeMember.noDiffs")}
            />
          )}
          {!diffLoading && !diffError && diffs.length > 0 && (
            <Table
              size="small"
              rowKey={(record, idx) => `diff-${record.symbol_id}-${record.side}-${idx}`}
              dataSource={diffs}
              columns={diffColumns}
              pagination={false}
            />
          )}
        </div>

        {/* 开关管理 */}
        <div style={{ marginTop: 24 }}>
          <h3 style={{ fontSize: 14, marginBottom: 8, display: "flex", alignItems: "center" }}>
            {t("autoTradeMember.sourceSwitch")}
            <Tooltip title={t("autoTradeMember.sourceSwitchHint")}>
              <QuestionCircleOutlined style={{ marginLeft: 6, fontSize: 12, color: "#94a3b8" }} />
            </Tooltip>
            <Button
              size="small"
              type="link"
              onClick={loadSourceStatus}
              loading={sourceLoading}
              style={{ marginLeft: "auto", padding: 0 }}
            >
              {t("refresh")}
            </Button>
          </h3>
          {sourceLoading && <Skeleton active paragraph={{ rows: 2 }} />}
          {!sourceLoading && sourceError && (
            <Alert
              type="error"
              message={t("autoTradeMember.loadSourceStatusFailed")}
              description={sourceError}
              showIcon
              style={{ marginBottom: 8 }}
            />
          )}
          {!sourceLoading && !sourceError && sourceStatus && (
            <div>
              <div
                style={{
                  display: "flex",
                  gap: 24,
                  flexWrap: "wrap",
                  alignItems: "center",
                  marginBottom: 12,
                }}
              >
                <Statistic
                  title={
                    <span>
                      {t("autoTradeMember.sourceEnabled")}
                      <Tooltip title={t("autoTradeMember.sourceEnabledHint")}>
                        <QuestionCircleOutlined style={{ marginLeft: 4, fontSize: 11, color: "#94a3b8" }} />
                      </Tooltip>
                    </span>
                  }
                  valueRender={() => (
                    <Badge
                      status={sourceStatus.enabled ? "success" : "default"}
                      text={
                        sourceStatus.enabled
                          ? t("autoTradeMember.sourceEnabledOn")
                          : t("autoTradeMember.sourceEnabledOff")
                      }
                    />
                  )}
                />
                <Statistic
                  title={t("autoTradeMember.envFlag")}
                  value={sourceStatus.env_flag}
                />
                <Statistic
                  title={
                    <span>
                      {t("autoTradeMember.whitelistMatch")}
                      <Tooltip title={t("autoTradeMember.whitelistMatchHint")}>
                        <QuestionCircleOutlined style={{ marginLeft: 4, fontSize: 11, color: "#94a3b8" }} />
                      </Tooltip>
                    </span>
                  }
                  valueRender={() => (
                    <Tag color={sourceStatus.whitelist_match ? "green" : "default"}>
                      {sourceStatus.whitelist_match ? "Yes" : "No"}
                    </Tag>
                  )}
                />
                <Statistic
                  title={
                    <span>
                      {t("autoTradeMember.blacklistMatch")}
                      <Tooltip title={t("autoTradeMember.blacklistMatchHint")}>
                        <QuestionCircleOutlined style={{ marginLeft: 4, fontSize: 11, color: "#94a3b8" }} />
                      </Tooltip>
                    </span>
                  }
                  valueRender={() => (
                    <Tag color={sourceStatus.blacklist_match ? "red" : "default"}>
                      {sourceStatus.blacklist_match ? "Yes" : "No"}
                    </Tag>
                  )}
                />
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <Button
                  type="primary"
                  danger
                  icon={<RollbackOutlined />}
                  onClick={handleRollback}
                  loading={rollingBack}
                  disabled={!sourceStatus.enabled && !sourceStatus.whitelist_match}
                >
                  {t("autoTradeMember.rollbackToOldSource")}
                </Button>
                <Tooltip title={t("autoTradeMember.rollbackButtonHint")}>
                  <QuestionCircleOutlined style={{ fontSize: 12, color: "#94a3b8" }} />
                </Tooltip>
                <span style={{ fontSize: 12, color: "#94a3b8" }}>
                  {t("autoTradeMember.whitelistLabel")}: [{sourceStatus.whitelist.join(", ") || "-"}]
                </span>
                <span style={{ fontSize: 12, color: "#94a3b8" }}>
                  {t("autoTradeMember.blacklistLabel")}: [{sourceStatus.blacklist.join(", ") || "-"}]
                </span>
              </div>
            </div>
          )}
          {!sourceLoading && !sourceError && !sourceStatus && (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t("autoTradeMember.noSourceStatus")}
            />
          )}
        </div>
      </div>
    </section>
  );
}
