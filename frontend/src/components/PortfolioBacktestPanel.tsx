import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  DatePicker,
  Input,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
} from "antd";
import dayjs from "dayjs";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
import { enumLabel, t, template } from "../i18n";
import { money } from "../utils/format";
import type { BacktestRun, PortfolioBacktestResult } from "../types";
import BacktestResult from "./BacktestResult";
// WP-AI.7：让 AI 解释按钮
import ExplainButton from "./ai/ExplainButton";

/**
 * P2-2 + WP7.4：组合整体回测面板。
 *
 * 数据来源：POST /backtest/portfolio/run
 * - 后端自动从组合持仓 + 最新 scan executable 候选推导 symbol_ids
 * - 后端自动构造与 auto_trade 信号逻辑一致的 rule_config（基于 Score.action）
 * - 复用 portfolio 的 active rule 限制仓位与持仓数
 *
 * WP7.4 新增四个分区：
 * 1. 来源说明：显示当前来源标签（legacy_scan / member / 未知）、数据截止、引擎
 * 2. 成员资格校验：only_auto 复选框 + 排除成员表 + manual/confirm 警告
 * 3. 组合快照折叠面板：标的列表 / 成员快照 / 评分模式 / 规则版本
 * 4. 新旧引擎对比按钮：调用 /portfolios/{id}/backtest/compare 并展示差异
 *
 * 前置条件：
 * - 组合必须 account_type="simulated"
 * - 组合必须已开启 auto_trade_enabled
 */
export default function PortfolioBacktestPanel() {
  const ctx = useApp();
  const portfolioId = ctx.portfolioId;
  const currentPortfolio = ctx.portfolios.find((p) => p.id === portfolioId);
  const isSimulated = currentPortfolio?.account_type === "simulated";
  const autoTradeEnabled = Number(currentPortfolio?.auto_trade_enabled) === 1;

  const [range, setRange] = useState<[dayjs.Dayjs, dayjs.Dayjs]>([
    dayjs().subtract(1, "year"),
    dayjs(),
  ]);
  const [runName, setRunName] = useState("");
  const [running, setRunning] = useState(false);
  const [summary, setSummary] = useState<PortfolioBacktestResult | null>(null);
  const [detail, setDetail] = useState<BacktestRun | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 来源开关状态（全局 PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED）
  const [sourceStatus, setSourceStatus] = useState<{
    enabled: boolean;
    env_flag: string;
    source_label: string;
  } | null>(null);
  const [sourceStatusLoading, setSourceStatusLoading] = useState(false);

  // 新旧引擎对比
  const [comparing, setComparing] = useState(false);
  const [compareResult, setCompareResult] = useState<{
    old: {
      run_id: number;
      symbol_ids: number[];
      source_type: string;
      metrics: {
        total_return: number | null;
        max_drawdown: number | null;
        sharpe_ratio: number | null;
        trade_count: number;
      };
    };
    new: {
      run_id: number;
      symbol_ids: number[];
      source_type: string;
      metrics: {
        total_return: number | null;
        max_drawdown: number | null;
        sharpe_ratio: number | null;
        trade_count: number;
      };
    };
    diff: {
      symbol_ids_added: number[];
      symbol_ids_removed: number[];
      metrics_diff: Record<string, { old: number | null; new: number | null; delta: number | null }>;
      explanation: string;
    };
  } | null>(null);
  const [compareError, setCompareError] = useState<string | null>(null);

  const canRun = !!portfolioId && isSimulated && autoTradeEnabled && !!range?.[0] && !!range?.[1];
  const canCompare = canRun && !comparing;

  // 拉取来源开关状态（仅模拟 + 自动交易组合才有意义）
  const loadSourceStatus = useCallback(async () => {
    if (!portfolioId || !isSimulated || !autoTradeEnabled) {
      setSourceStatus(null);
      return;
    }
    setSourceStatusLoading(true);
    try {
      const data = await api.getPortfolioBacktestSourceStatus(portfolioId);
      setSourceStatus(data);
    } catch {
      // 来源状态拉取失败不阻塞主流程
      setSourceStatus(null);
    } finally {
      setSourceStatusLoading(false);
    }
  }, [portfolioId, isSimulated, autoTradeEnabled]);

  useEffect(() => {
    loadSourceStatus();
  }, [loadSourceStatus]);

  const run = useCallback(async () => {
    if (!portfolioId || !canRun) return;
    setRunning(true);
    setError(null);
    setSummary(null);
    setDetail(null);
    try {
      const data = await api.runPortfolioBacktest(portfolioId, {
        start_date: range[0].format("YYYY-MM-DD"),
        end_date: range[1].format("YYYY-MM-DD"),
        run_name: runName.trim() || undefined,
      });
      setSummary(data as PortfolioBacktestResult);
      ctx.showToast("success", t("portBtSuccess"));
      // 拉取完整 run 详情用于展示图表与交易记录
      try {
        const full = await api.getBacktestRun((data as PortfolioBacktestResult).run_id);
        setDetail(full as BacktestRun);
      } catch {
        // 详情拉取失败不影响主流程，summary 已有核心信息
      }
    } catch (err: any) {
      const msg = err?.message || String(err);
      setError(msg);
      ctx.showToast("error", t("portBtFailed") + ": " + msg);
    } finally {
      setRunning(false);
    }
  }, [portfolioId, canRun, range, runName, ctx]);

  const runCompare = useCallback(async () => {
    if (!portfolioId || !canCompare) return;
    setComparing(true);
    setCompareError(null);
    setCompareResult(null);
    try {
      const data = await api.comparePortfolioBacktestEngines(portfolioId, {
        start_date: range[0].format("YYYY-MM-DD"),
        end_date: range[1].format("YYYY-MM-DD"),
      });
      setCompareResult(data);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setCompareError(msg);
      ctx.showToast("error", t("portfolioBacktest.compareFailed") + ": " + msg);
    } finally {
      setComparing(false);
    }
  }, [portfolioId, canCompare, range]);

  // ----------------------------------------------------------------------------
  // WP7.4 分区 1：来源说明（基于 detail.source_type / summary.source_type）
  // ----------------------------------------------------------------------------
  const sourceType = detail?.source_type ?? summary?.source_type ?? null;
  const sourceLabelKey =
    sourceType === "legacy_scan"
      ? "portfolioBacktest.legacySource"
      : sourceType === "member"
        ? "portfolioBacktest.memberSource"
        : "portfolioBacktest.unknownSource";
  const sourceTagColor =
    sourceType === "legacy_scan"
      ? "orange"
      : sourceType === "member"
        ? "green"
        : "default";

  // ----------------------------------------------------------------------------
  // WP7.4 分区 2：成员资格校验（excluded_members_json 解析）
  // ----------------------------------------------------------------------------
  const excludedMembers = useMemo(() => {
    if (!detail?.excluded_members_json) return [];
    try {
      const parsed = JSON.parse(detail.excluded_members_json);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }, [detail?.excluded_members_json]);

  // 排除原因中包含 manual/confirm 时触发警告
  const hasManualConfirmMembers = excludedMembers.some(
    (m: any) => typeof m?.reason === "string" && /manual|confirm/.test(m.reason),
  );

  // ----------------------------------------------------------------------------
  // WP7.4 分区 3：组合快照展示
  // ----------------------------------------------------------------------------
  const snapshotSymbolIds = useMemo(() => {
    const raw = detail?.symbol_ids_json;
    if (!raw) return [];
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }, [detail?.symbol_ids_json]);

  const memberSnapshot = useMemo(() => {
    if (!detail?.member_snapshot_json) return [];
    try {
      const parsed = JSON.parse(detail.member_snapshot_json);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }, [detail?.member_snapshot_json]);

  const hasSnapshot =
    !!detail &&
    (!!detail.symbol_ids_json ||
      !!detail.member_snapshot_json ||
      !!detail.score_mode ||
      !!detail.portfolio_rule_version_id);

  // ----------------------------------------------------------------------------
  // 渲染
  // ----------------------------------------------------------------------------
  return (
    <section className="band">
      <div className="panel">
        <div className="panel-head">
          <div>
            <p className="panel-kicker">{t("portBtKicker")}</p>
            <h2>{t("portBtTitle")}</h2>
          </div>
        </div>

        <p className="panel-meta">{t("portBtHelp")}</p>

        {!isSimulated && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t("portBtOnlySimulated")}
          />
        )}
        {isSimulated && !autoTradeEnabled && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={t("portBtAutoTradeRequired")}
          />
        )}

        {/* WP7.4 分区 1：来源说明（全局开关状态 + 最近一次回测来源） */}
        {isSimulated && autoTradeEnabled && (
          <div style={{ marginBottom: 12 }}>
            <Space wrap>
              <Tooltip title={t("portfolioBacktest.sourceStatusLoading")}>
                <Tag color="blue">
                  {t("portfolioBacktest.sourceLabel")}:{" "}
                  {sourceStatusLoading
                    ? "..."
                    : sourceStatus
                      ? sourceStatus.source_label === "members"
                        ? t("portfolioBacktest.memberSource")
                        : t("portfolioBacktest.legacySource")
                      : "-"}
                </Tag>
              </Tooltip>
              {sourceStatus && (
                <Tag color={sourceStatus.enabled ? "green" : "orange"}>
                  {sourceStatus.env_flag}={sourceStatus.enabled ? "true" : "false"}
                </Tag>
              )}
              {detail?.data_cutoff_at && (
                <Tag>
                  {t("portfolioBacktest.dataCutoffAt")}: {detail.data_cutoff_at}
                </Tag>
              )}
              {detail?.engine_name && detail?.engine_version && (
                <Tag>
                  {t("portfolioBacktest.engineInfo")}: {detail.engine_name} v{detail.engine_version}
                </Tag>
              )}
            </Space>
          </div>
        )}

        <div className="backtest-grid">
          <label>
            <span>{t("portBtDateRange")}</span>
            <DatePicker.RangePicker
              value={range}
              onChange={(value) => value?.[0] && value?.[1] && setRange([value[0], value[1]])}
              allowClear={false}
            />
          </label>
          <label>
            <span>{t("portBtRunName")}</span>
            <Input
              value={runName}
              onChange={(e) => setRunName(e.target.value)}
              placeholder={t("portBtRunNamePlaceholder")}
            />
          </label>
        </div>

        {/* 成员资格由执行快照统一决定，前端不再提供 only_auto 覆盖开关。 */}
        <div style={{ marginTop: 12 }}>
          {hasManualConfirmMembers && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 8 }}
              message={t("portfolioBacktest.manualMembersWarning")}
            />
          )}
        </div>

        <Space className="backtest-actions" style={{ marginTop: 12 }}>
          <Button
            type="primary"
            loading={running}
            disabled={!canRun}
            onClick={run}
          >
            {running ? t("portBtRunning") : t("portBtRun")}
          </Button>
          {/* WP7.4 分区 4：新旧引擎对比按钮 */}
          <Tooltip title={t("portfolioBacktest.compareEngines")}>
            <Button
              loading={comparing}
              disabled={!canCompare}
              onClick={runCompare}
              data-testid="compare-engines-button"
            >
              {comparing
                ? t("portfolioBacktest.comparing")
                : t("portfolioBacktest.compareEngines")}
            </Button>
          </Tooltip>
        </Space>

        {error && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 12 }}
            message={t("portBtFailed")}
            description={error}
          />
        )}

        {summary && (
          <div style={{ marginTop: 16 }}>
            <div className="panel-head" style={{ marginBottom: 8 }}>
              <div>
                <p className="panel-kicker">{t("portBtResultTitle")}</p>
                <h3>{summary.run_name}</h3>
              </div>
              <Space wrap>
                <Tag color="blue">{t("portBtRunId")}: {summary.run_id}</Tag>
                <Tag color="geekblue">{t("portBtSymbolCount")}: {summary.symbol_count}</Tag>
                <Tag color="green">{t("portBtStatus")}: {t("portBtStatusCompleted")}</Tag>
                <Tag color="orange">{t("portBtInitialCapital")}: {money(summary.initial_capital)}</Tag>
                <Tag>{t("portBtDateRangeLabel")}: {summary.start_date} ~ {summary.end_date}</Tag>
                {/* WP7.4：来源标签 */}
                <Tag color={sourceTagColor} data-testid="source-label-tag">
                  {t("portfolioBacktest.sourceLabel")}: {t(sourceLabelKey)}
                </Tag>
                {summary.excluded_member_count != null && summary.excluded_member_count > 0 && (
                  <Tag color="red">
                    {t("portfolioBacktest.excludedMembers")}: {summary.excluded_member_count}
                  </Tag>
                )}
              </Space>
            </div>
          </div>
        )}

        {/* WP7.4 分区 2 续：排除成员表 */}
        {detail && excludedMembers.length > 0 && (
          <div style={{ marginTop: 12 }} data-testid="excluded-members-section">
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 8 }}
              message={template("portfolioBacktest.excludedMembersSummary", {
                count: excludedMembers.length,
              })}
            />
            <Table
              size="small"
              rowKey={(r: any) => `${r.member_id}-${r.symbol_id}`}
              dataSource={excludedMembers}
              columns={[
                { title: "member_id", dataIndex: "member_id", key: "member_id" },
                { title: "symbol_id", dataIndex: "symbol_id", key: "symbol_id" },
                { title: t("portfolioBacktest.excludedMembers"), dataIndex: "reason", key: "reason" },
              ]}
              pagination={false}
            />
          </div>
        )}

        {/* WP7.4 分区 3：组合快照折叠面板（默认展开，便于审计历史可复现性） */}
        {detail && hasSnapshot && (
          <div style={{ marginTop: 12 }} data-testid="snapshot-section">
            <Collapse
              defaultActiveKey={["snapshot"]}
              items={[
                {
                  key: "snapshot",
                  label: t("portfolioBacktest.snapshot"),
                  forceRender: true,
                  children: (
                    <div>
                      {snapshotSymbolIds.length > 0 && (
                        <div style={{ marginBottom: 8 }} data-testid="snapshot-symbol-list">
                          <strong>{t("portfolioBacktest.symbolList")}:</strong>{" "}
                          {snapshotSymbolIds.join(", ")}
                        </div>
                      )}
                      {memberSnapshot.length > 0 && (
                        <div style={{ marginBottom: 8 }}>
                          <div style={{ marginBottom: 4 }}>
                            <strong>{t("portfolioBacktest.memberSnapshotTable")}</strong>
                          </div>
                          <Table
                            size="small"
                            rowKey={(r: any) => `${r.member_id}-${r.symbol_id}`}
                            dataSource={memberSnapshot}
                            columns={[
                              { title: t("portfolioBacktest.memberId"), dataIndex: "member_id", key: "member_id" },
                              { title: t("portfolioBacktest.symbolId"), dataIndex: "symbol_id", key: "symbol_id" },
                              { title: t("portfolioBacktest.effectiveFrom"), dataIndex: "effective_from", key: "effective_from" },
                              { title: t("portfolioBacktest.effectiveTo"), dataIndex: "effective_to", key: "effective_to" },
                              { title: t("portfolioBacktest.executionMode"), dataIndex: "execution_mode", key: "execution_mode", render: (value: string) => enumLabel("executionMode", value) },
                              {
                                title: t("portfolioBacktest.ruleVersion"),
                                dataIndex: "entry_rule_version_id",
                                key: "entry_rule_version_id",
                              },
                            ]}
                            pagination={false}
                          />
                        </div>
                      )}
                      {detail.score_mode && (
                        <div style={{ marginBottom: 4 }} data-testid="snapshot-score-mode">
                          <strong>{t("portfolioBacktest.scoreMode")}:</strong> {enumLabel("factorMode", detail.score_mode)}
                        </div>
                      )}
                      {detail.portfolio_rule_version_id != null && (
                        <div data-testid="snapshot-rule-version">
                          <strong>{t("portfolioBacktest.ruleVersion")}:</strong>{" "}
                          {detail.portfolio_rule_version_id}
                        </div>
                      )}
                    </div>
                  ),
                },
              ]}
            />
          </div>
        )}

        {/* WP7.4 分区 4：对比结果展示 */}
        {comparing && (
          <div style={{ marginTop: 12 }} data-testid="compare-loading">
            <Spin tip={t("portfolioBacktest.comparing")} />
          </div>
        )}
        {compareError && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 12 }}
            message={t("portfolioBacktest.compareFailed")}
            description={compareError}
          />
        )}
        {compareResult && (
          <div style={{ marginTop: 12 }} data-testid="compare-result-section">
            <Collapse
              defaultActiveKey={["compare"]}
              items={[
                {
                  key: "compare",
                  label: t("portfolioBacktest.compareResult"),
                  children: (
                    <div>
                      <Space wrap style={{ marginBottom: 8 }}>
                        <Tag color="orange">
                          {t("portfolioBacktest.symbolsOnlyInLegacy")}:{" "}
                          {compareResult.diff.symbol_ids_removed.length}
                        </Tag>
                        <Tag color="green">
                          {t("portfolioBacktest.symbolsOnlyInMember")}:{" "}
                          {compareResult.diff.symbol_ids_added.length}
                        </Tag>
                      </Space>
                      <Table
                        size="small"
                        rowKey={(r: any) => r.key}
                        pagination={false}
                        style={{ marginBottom: 8 }}
                        dataSource={[
                          {
                            key: "total_return",
                            metric: t("portfolioBacktest.totalReturn"),
                            old: compareResult.old.metrics.total_return,
                            new: compareResult.new.metrics.total_return,
                            delta: compareResult.diff.metrics_diff.total_return?.delta ?? null,
                          },
                          {
                            key: "max_drawdown",
                            metric: t("portfolioBacktest.maxDrawdown"),
                            old: compareResult.old.metrics.max_drawdown,
                            new: compareResult.new.metrics.max_drawdown,
                            delta: compareResult.diff.metrics_diff.max_drawdown?.delta ?? null,
                          },
                          {
                            key: "sharpe_ratio",
                            metric: t("portfolioBacktest.sharpe"),
                            old: compareResult.old.metrics.sharpe_ratio,
                            new: compareResult.new.metrics.sharpe_ratio,
                            delta: compareResult.diff.metrics_diff.sharpe_ratio?.delta ?? null,
                          },
                        ]}
                        columns={[
                          { title: t("portfolioBacktest.metricsDiff"), dataIndex: "metric", key: "metric" },
                          {
                            title: t("portfolioBacktest.symbolsOnlyInLegacy"),
                            dataIndex: "old",
                            key: "old",
                            render: (v: number | null) => (v == null ? "-" : v.toFixed(4)),
                          },
                          {
                            title: t("portfolioBacktest.symbolsOnlyInMember"),
                            dataIndex: "new",
                            key: "new",
                            render: (v: number | null) => (v == null ? "-" : v.toFixed(4)),
                          },
                          {
                            title: "Δ",
                            dataIndex: "delta",
                            key: "delta",
                            render: (v: number | null) => (v == null ? "-" : v.toFixed(4)),
                          },
                        ]}
                      />
                      <Alert
                        type="info"
                        showIcon
                        message={t("portfolioBacktest.explanation")}
                        description={compareResult.diff.explanation}
                      />
                    </div>
                  ),
                },
              ]}
            />
          </div>
        )}

        {detail && (
          <div style={{ marginTop: 16 }}>
            <div style={{ marginBottom: 8, display: "flex", justifyContent: "flex-end" }}>
              {/* WP-AI.7：让 AI 解释（携带 backtest_run_id + portfolio_id） */}
              <ExplainButton
                sourcePage="portfolio_backtest"
                references={{
                  backtest_run_id: detail.id,
                  portfolio_id: portfolioId ?? 0,
                }}
              />
            </div>
            <BacktestResult
              result={detail}
              portfolioId={portfolioId ?? undefined}
              onAppliedToPortfolio={() => ctx.loadWorkbench()}
            />
          </div>
        )}
      </div>
    </section>
  );
}
