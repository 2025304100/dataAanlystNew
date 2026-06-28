import ReactECharts from "echarts-for-react";
import { Alert, Card, Col, Row, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { t } from "../i18n";
import type { BacktestPricePoint, BacktestRun, BacktestTrade } from "../types";
import { money, percent, score, pnlClass } from "../utils/format";

interface BacktestResultProps {
  result: BacktestRun | null;
}

function parseEquityCurve(result: BacktestRun | null): Array<{ date: string; equity: number }> {
  if (!result?.equity_curve_json) return [];
  try {
    const parsed = JSON.parse(result.equity_curve_json);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function parseRunConfig(result: BacktestRun | null): Record<string, any> {
  if (!result?.rule_config_json) return {};
  try {
    const parsed = JSON.parse(result.rule_config_json);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function priceFieldLabel(value: string | undefined | null): string {
  return value === "open" ? t("btOpenPrice") : t("btClosePrice");
}

function reasonLabel(reason: string): string {
  const key = `btReason_${reason}`;
  const label = t(key);
  return label === key ? reason : label;
}

function exitReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "open";
  const key = `btExit_${reason}`;
  const label = t(key);
  return label === key ? reason : label;
}

export default function BacktestResult({ result }: BacktestResultProps) {
  const equityCurve = parseEquityCurve(result);
  const trades = result?.trades ?? [];
  const priceSeries: BacktestPricePoint[] = result?.price_series ?? [];
  const config = parseRunConfig(result);
  const diagnostics = result?.diagnostics ?? {};
  const execution = diagnostics.execution ?? config.execution_config ?? {};
  const entryField = execution.entry_price_field ?? "close";
  const exitField = execution.exit_price_field ?? "close";
  const skipReasons = Object.entries(diagnostics.skip_reasons ?? {}).sort((a, b) => Number(b[1]) - Number(a[1]));
  const checkedDays = diagnostics.checked_days ?? priceSeries.length;
  const buySignalDays = diagnostics.buy_signal_days ?? 0;

  const priceDates = priceSeries.map((p) => p.date);
  const priceChartOption = {
    tooltip: { trigger: "axis" },
    legend: { top: 0, data: [t("btPriceChart"), t("btBuyPoint"), t("btSellPoint")] },
    grid: { left: 52, right: 18, top: 34, bottom: 36 },
    xAxis: { type: "category", data: priceDates, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", scale: true, axisLabel: { fontSize: 10 } },
    series: [
      {
        name: t("btPriceChart"),
        type: "candlestick",
        data: priceSeries.map((p) => [p.open, p.close, p.low, p.high]),
        itemStyle: { color: "#b42318", color0: "#0f766e", borderColor: "#b42318", borderColor0: "#0f766e" },
        markPoint: {
          symbolSize: 46,
          label: { formatter: "{b}", fontSize: 10 },
          data: [
            ...trades.map((trade) => ({
              name: t("btBuyPoint"),
              coord: [trade.entry_date, trade.entry_price],
              value: `${score(trade.entry_price)} ${priceFieldLabel(entryField)}`,
              itemStyle: { color: "#0f766e" },
            })),
            ...trades
              .filter((trade) => trade.exit_date && trade.exit_price != null)
              .map((trade) => ({
                name: t("btSellPoint"),
                coord: [trade.exit_date, trade.exit_price],
                value: `${score(trade.exit_price)} ${priceFieldLabel(exitField)}`,
                itemStyle: { color: "#b42318" },
              })),
          ],
        },
      },
    ],
  };

  const equityChartOption = {
    tooltip: { trigger: "axis" },
    grid: { left: 52, right: 18, top: 24, bottom: 36 },
    xAxis: { type: "category", data: equityCurve.map((p) => p.date), axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", scale: true, axisLabel: { fontSize: 10 } },
    series: [{
      name: t("btReturn"),
      type: "line",
      smooth: true,
      showSymbol: false,
      data: equityCurve.map((p) => p.equity),
      lineStyle: { color: "#0f766e", width: 2 },
      areaStyle: { color: "rgba(15,118,110,0.10)" },
    }],
  };

  const columns: ColumnsType<BacktestTrade> = [
    { title: t("btSymbolId"), dataIndex: "symbol_id", width: 90 },
    { title: t("btEntryDate"), dataIndex: "entry_date", width: 110 },
    { title: `${t("btEntryPrice")}(${priceFieldLabel(entryField)})`, dataIndex: "entry_price", render: (v) => score(v), align: "right" },
    { title: t("btQuantity"), dataIndex: "quantity", align: "right" },
    { title: t("btExitDate"), dataIndex: "exit_date", width: 110, render: (v) => v ?? t("btNotClosed") },
    { title: `${t("btExitPrice")}(${priceFieldLabel(exitField)})`, dataIndex: "exit_price", render: (v) => score(v), align: "right" },
    { title: t("btExitReason"), dataIndex: "exit_reason", render: (v) => v ? <Tag>{exitReasonLabel(v)}</Tag> : <Tag color="blue">open</Tag> },
    { title: t("btPnl"), dataIndex: "pnl", render: (v) => <span className={pnlClass(v)}>{money(v, 2)}</span>, align: "right" },
    { title: t("btPnlPct"), dataIndex: "pnl_pct", render: (v) => <span className={pnlClass(v)}>{percent(v)}</span>, align: "right" },
    { title: t("btHoldDays"), dataIndex: "hold_days", align: "right" },
  ];

  if (!result) {
    return <div className="empty">{t("btNoData")}</div>;
  }

  return (
    <section className="backtest-result">
      <div style={{ marginBottom: 8 }}>
        <Tag color={config.version === 2 ? "purple" : "default"}>
          {config.version === 2 ? "高级模式" : "标准模式"}
        </Tag>
      </div>
      <Row gutter={[12, 12]}>
        <Col xs={12} md={6}><Card size="small"><div className="metric-label">{t("btTotalReturn")}</div><div className={pnlClass(result.total_return_pct)}>{percent(result.total_return_pct)}</div></Card></Col>
        <Col xs={12} md={6}><Card size="small"><div className="metric-label">{t("btMaxDrawdown")}</div><div>{percent(result.max_drawdown_pct)}</div></Card></Col>
        <Col xs={12} md={6}><Card size="small"><div className="metric-label">{t("btWinRate")}</div><div>{percent(result.win_rate)}</div></Card></Col>
        <Col xs={12} md={6}><Card size="small"><div className="metric-label">{t("btTradeCount")}</div><div>{result.trade_count ?? 0}</div></Card></Col>
      </Row>

      <div className="backtest-diagnostics">
        <div className="backtest-diagnostics-head">
          <strong>{t("btRuleDiagnosis")}</strong>
          <span>{t("btExecutionBasis")}: {t("btBuyPoint")}={priceFieldLabel(entryField)} / {t("btSellPoint")}={priceFieldLabel(exitField)}</span>
        </div>
        <div className="backtest-diagnostics-grid">
          <span><b>{t("btCheckedDays")}</b><strong>{checkedDays}</strong></span>
          <span><b>{t("btBuySignalDays")}</b><strong>{buySignalDays}</strong></span>
          <span><b>{t("btBlockedDays")}</b><strong>{Math.max(checkedDays - buySignalDays, 0)}</strong></span>
        </div>
        {diagnostics.fill_warning && <Alert type="warning" showIcon message={t("btFillWarning")}/>} 
        {skipReasons.length > 0 && (
          <div className="backtest-reason-list">
            {skipReasons.map(([reason, count]) => (
              <Tag key={reason}>{reasonLabel(reason)} {count}</Tag>
            ))}
          </div>
        )}
      </div>

      <div className="backtest-chart-card">
        <div className="backtest-chart-title">{t("btTradePointChart")}</div>
        {priceSeries.length > 0 ? (
          <ReactECharts option={priceChartOption} style={{ height: 320, width: "100%" }} />
        ) : (
          <div className="empty">{t("noChart")}</div>
        )}
      </div>
      <div className="backtest-chart-card">
        <div className="backtest-chart-title">{t("btEquityChart")}</div>
        <ReactECharts option={equityChartOption} style={{ height: 220, width: "100%" }} />
      </div>
      <Table rowKey="id" size="small" columns={columns} dataSource={trades} pagination={{ pageSize: 8 }} scroll={{ x: 1020 }} />
    </section>
  );
}
