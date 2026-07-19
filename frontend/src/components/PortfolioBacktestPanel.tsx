import { useCallback, useState } from "react";
import { Alert, Button, DatePicker, Input, Space, Tag } from "antd";
import dayjs from "dayjs";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
import { t } from "../i18n";
import { money } from "../utils/format";
import type { BacktestRun, PortfolioBacktestResult } from "../types";
import BacktestResult from "./BacktestResult";

/**
 * P2-2：组合整体回测面板。
 *
 * 数据来源：POST /backtest/portfolio/run
 * - 后端自动从组合持仓 + 最新 scan executable 候选推导 symbol_ids
 * - 后端自动构造与 auto_trade 信号逻辑一致的 rule_config（基于 Score.action）
 * - 复用 portfolio 的 active rule 限制仓位与持仓数
 *
 * 前置条件：
 * - 组合必须 account_type="simulated"
 * - 组合必须已开启 auto_trade_enabled
 *
 * 交互：
 * - 选择日期区间 + 可选 run_name
 * - 点击"运行组合回测"按钮
 * - 成功后自动拉取完整 run 详情并复用 BacktestResult 组件展示
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

  const canRun = !!portfolioId && isSimulated && autoTradeEnabled && !!range?.[0] && !!range?.[1];

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

        <Space className="backtest-actions" style={{ marginTop: 12 }}>
          <Button
            type="primary"
            loading={running}
            disabled={!canRun}
            onClick={run}
          >
            {running ? t("portBtRunning") : t("portBtRun")}
          </Button>
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
              </Space>
            </div>
          </div>
        )}

        {detail && (
          <div style={{ marginTop: 16 }}>
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
