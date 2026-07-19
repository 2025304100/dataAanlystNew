import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  InputNumber,
  Skeleton,
  Switch,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
import { t } from "../i18n";
import { money, formatRelativeTime } from "../utils/format";
import type { AutoTradePlanItem, AutoTradeResult } from "../types";

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
 */
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

  // 组合切换时清空旧结果
  useEffect(() => {
    setResult(null);
    setError(null);
  }, [portfolioId]);

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
            <Button
              size="small"
              type="primary"
              onClick={execute}
              loading={running}
              danger={!dryRun}
            >
              {dryRun ? t("autoTradeDryRun") : t("autoTradeExecute")}
            </Button>
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
      </div>
    </section>
  );
}
