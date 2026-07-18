import ReactECharts from "echarts-for-react";
import { Alert, Card, Col, Row, Space, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { t } from "../i18n";
import { CONDITION_FIELDS } from "../constants/conditionFields";
import type { BacktestConditionTrace, BacktestPricePoint, BacktestRun, BacktestTrade } from "../types";
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

function priceFieldCode(value: string | undefined | null): string {
  return value === "open" ? "O" : "C";
}

function resolveExecutionTiming(execution: Record<string, any>, side: "entry" | "exit"): string {
  const explicit = execution?.[`${side}_timing`];
  if (explicit === "signal_close" || explicit === "next_open" || explicit === "signal_open") return explicit;
  const field = execution?.[`${side}_price_field`];
  return field === "open" ? "signal_open" : "signal_close";
}

function executionTimingLabel(value: string | null | undefined): string {
  if (value === "next_open") return t("btTimingNextOpen");
  if (value === "signal_open") return t("btTimingSignalOpen");
  return t("btTimingSignalClose");
}

function executionTimingShortLabel(value: string | null | undefined): string {
  if (value === "next_open") return t("btTimingNextOpenShort");
  if (value === "signal_open") return t("btTimingSignalOpenShort");
  return t("btTimingSignalCloseShort");
}

const CONDITION_LABELS = new Map(CONDITION_FIELDS.map((field) => [field.key, field.labelKey]));

function conditionFieldLabel(field: string, trace?: BacktestConditionTrace): string {
  if (field === "custom_indicator") {
    return trace?.indicator_name || t("btCustomIndicator");
  }
  const labelKey = CONDITION_LABELS.get(field);
  return labelKey ? t(labelKey) : field;
}

function reasonLabel(reason: string): string {
  const key = `btReason_${reason}`;
  const label = t(key);
  if (label !== key) return label;
  const labelKey = CONDITION_LABELS.get(reason);
  return labelKey ? t(labelKey) : reason;
}

function exitReasonLabel(reason: string | null | undefined): string {
  if (!reason) return t("btNotClosed");
  const key = `btExit_${reason}`;
  const label = t(key);
  return label === key ? reasonLabel(reason) : label;
}

function formatTraceValue(value: unknown): string {
  if (value == null) return "-";
  if (typeof value === "boolean") return value ? t("yes") : t("no");
  if (typeof value === "number") return score(value);
  if (Array.isArray(value)) return value.map((item) => formatTraceValue(item)).join(" / ");
  return String(value);
}

function traceHoverLines(traces?: BacktestConditionTrace[]): string[] {
  if (!traces?.length) return [];
  return traces.map((trace) => {
    const name = conditionFieldLabel(trace.field, trace);
    return `<div>${name}: ${formatTraceValue(trace.actual)} / ${trace.operator} ${formatTraceValue(trace.expected)}</div>`;
  });
}

function traceSummaryText(traces?: BacktestConditionTrace[], onlyFailed = false): string {
  if (!traces?.length) return "-";
  const list = onlyFailed ? traces.filter((item) => !item.matched) : traces;
  if (!list.length) return onlyFailed ? t("btNoFailedConditions") : "-";
  const summary = list.slice(0, 2).map((trace) => {
    const name = conditionFieldLabel(trace.field, trace);
    return `${name}: ${formatTraceValue(trace.actual)} / ${trace.operator} ${formatTraceValue(trace.expected)}`;
  });
  return list.length > 2 ? `${summary.join(" ; ")} +${list.length - 2}` : summary.join(" ; ");
}

function renderTraceList(title: string, traces: BacktestConditionTrace[] | undefined, priceLabel: string, priceValue: number | null | undefined, extraTags: string[]) {
  const traceRows = traces ?? [];
  return (
    <div style={{ border: "1px solid rgba(31,41,55,0.08)", borderRadius: 8, padding: 12, background: "#fff" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        <strong>{title}</strong>
        <Space size={6} wrap>
          <Tag>{priceLabel}</Tag>
          {priceValue != null && <Tag color="blue">{score(priceValue)}</Tag>}
          {extraTags.map((item) => <Tag key={item}>{item}</Tag>)}
        </Space>
      </div>
      {traceRows.length ? (
        <div style={{ display: "grid", gap: 8 }}>
          {traceRows.map((trace, index) => (
            <div
              key={`${trace.field}_${index}`}
              style={{
                display: "grid",
                gap: 4,
                padding: "8px 10px",
                borderRadius: 8,
                background: trace.matched ? "rgba(15,118,110,0.08)" : "rgba(180,35,24,0.08)",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <Tag color={trace.matched ? "green" : "red"}>{trace.matched ? t("btConditionPassed") : t("btConditionFailed")}</Tag>
                <strong>{conditionFieldLabel(trace.field, trace)}</strong>
                <span style={{ color: "#64748b", fontSize: 12 }}>{trace.operator}</span>
                {trace.field === "custom_indicator" && trace.value_type && <Tag>{trace.value_type}</Tag>}
              </div>
              <div style={{ color: "#475569", fontSize: 12 }}>
                {t("btActualValue")}: {formatTraceValue(trace.actual)}
                <span style={{ margin: "0 8px" }}>|</span>
                {t("btExpectedValue")}: {formatTraceValue(trace.expected)}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="item-subline">{t("btNoTraceData")}</div>
      )}
    </div>
  );
}

export default function BacktestResult({ result }: BacktestResultProps) {
  const equityCurve = parseEquityCurve(result);
  const trades = result?.trades ?? [];
  const priceSeries: BacktestPricePoint[] = result?.price_series ?? [];
  const config = parseRunConfig(result);
  const diagnostics = result?.diagnostics ?? {};
  const execution = diagnostics.execution ?? config.execution_config ?? {};
  const entryTiming = resolveExecutionTiming(execution, "entry");
  const exitTiming = resolveExecutionTiming(execution, "exit");
  const entryField = execution.entry_price_field ?? (entryTiming === "signal_close" ? "close" : "open");
  const exitField = execution.exit_price_field ?? (exitTiming === "signal_close" ? "close" : "open");
  const skipReasons = Object.entries(diagnostics.skip_reasons ?? {}).sort((a, b) => Number(b[1]) - Number(a[1]));
  const checkedDays = diagnostics.checked_days ?? priceSeries.length;
  const buySignalDays = diagnostics.buy_signal_days ?? 0;
  const blockedDays = Math.max(checkedDays - buySignalDays, 0);
  const closedTrades = trades.filter((trade) => trade.exit_date && trade.exit_price != null);
  const openTrades = trades.filter((trade) => !trade.exit_date || trade.exit_price == null);
  const winners = closedTrades.filter((trade) => Number(trade.pnl ?? 0) > 0);
  const losers = closedTrades.filter((trade) => Number(trade.pnl ?? 0) < 0);
  const avgWinPct = winners.length ? winners.reduce((sum, trade) => sum + Number(trade.pnl_pct ?? 0), 0) / winners.length : null;
  const avgLossPct = losers.length ? losers.reduce((sum, trade) => sum + Number(trade.pnl_pct ?? 0), 0) / losers.length : null;
  const signalConversion = buySignalDays > 0 ? (trades.length / buySignalDays) : null;
  const sampleMisses = (diagnostics.sample_misses ?? []) as Array<Record<string, any>>;

  const priceRows = priceSeries.map((point) => ({
    date: point.date,
    open: Number(point.open ?? 0),
    high: Number(point.high ?? 0),
    low: Number(point.low ?? 0),
    close: Number(point.close ?? 0),
  }));
  const priceDates = priceRows.map((point) => point.date);

  const buyMarkers = trades.map((trade) => {
    const tradeTiming = trade.entry_execution_timing ?? entryTiming;
    const signalPriceField = trade.entry_signal_price_field ?? (tradeTiming === "signal_open" ? "open" : "close");
    return {
      value: [trade.entry_date, trade.entry_price],
      markerLabel: `${t("btBuyPoint")}-${executionTimingShortLabel(tradeTiming)}` ,
      tooltipLabel: `${t("btBuyPoint")}: ${score(trade.entry_price)} (${executionTimingLabel(tradeTiming)})`,
      hoverLines: [
        `<div><strong>${t("btBuyPoint")}</strong></div>`,
        `<div>${t("btEntryDate")}: ${trade.entry_date}</div>`,
        `<div>${t("btEntryPrice")}: ${score(trade.entry_price)} (${executionTimingLabel(tradeTiming)})</div>`,
        ...(trade.entry_signal_date ? [`<div>${t("btSignalDate")}: ${trade.entry_signal_date}</div>`] : []),
        ...(trade.entry_signal_price != null ? [`<div>${t("btSignalPrice")}: ${score(trade.entry_signal_price)} (${priceFieldLabel(signalPriceField)})</div>`] : []),
        `<div>${t("btQuantity")}: ${score(trade.quantity)}</div>`,
        ...traceHoverLines(trade.entry_traces),
      ],
      trade,
    };
  });
  const sellMarkers = trades
    .filter((trade) => trade.exit_date && trade.exit_price != null)
    .map((trade) => {
      const tradeTiming = trade.exit_execution_timing ?? exitTiming;
      const signalPriceField = trade.exit_signal_price_field ?? (tradeTiming === "signal_open" ? "open" : "close");
      return {
        value: [trade.exit_date as string, trade.exit_price as number],
        markerLabel: `${t("btSellPoint")}-${executionTimingShortLabel(tradeTiming)}` ,
        tooltipLabel: `${t("btSellPoint")}: ${score(trade.exit_price)} (${executionTimingLabel(tradeTiming)})`,
        hoverLines: [
          `<div><strong>${t("btSellPoint")}</strong></div>`,
          `<div>${t("btExitDate")}: ${trade.exit_date as string}</div>`,
          `<div>${t("btExitPrice")}: ${score(trade.exit_price)} (${executionTimingLabel(tradeTiming)})</div>`,
          ...(trade.exit_signal_date ? [`<div>${t("btSignalDate")}: ${trade.exit_signal_date}</div>`] : []),
          ...(trade.exit_signal_price != null ? [`<div>${t("btSignalPrice")}: ${score(trade.exit_signal_price)} (${priceFieldLabel(signalPriceField)})</div>`] : []),
          `<div>${t("btExitReason")}: ${exitReasonLabel(trade.exit_reason)}</div>`,
          ...traceHoverLines(trade.exit_traces),
        ],
        trade,
      };
    });

  const tradeEventsByDate = new Map<string, string[]>();
  buyMarkers.forEach((marker) => {
    const trade = marker.trade;
    const items = tradeEventsByDate.get(trade.entry_date) ?? [];
    items.push(`${marker.tooltipLabel} / ${t("btQuantity")}: ${score(trade.quantity)}`);
    tradeEventsByDate.set(trade.entry_date, items);
  });
  sellMarkers.forEach((marker) => {
    const trade = marker.trade;
    const exitDate = trade.exit_date as string;
    const items = tradeEventsByDate.get(exitDate) ?? [];
    items.push(`${marker.tooltipLabel} / ${t("btExitReason")}: ${exitReasonLabel(trade.exit_reason)}`);
    tradeEventsByDate.set(exitDate, items);
  });

  const priceChartOption = {
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      formatter: (params: any) => {
        const rows = Array.isArray(params) ? params : [params];
        const axisDate = rows[0]?.axisValue;
        const candle = priceRows.find((row) => row.date === axisDate);
        if (!candle) return axisDate ?? "";
        const lines = [
          `<div><strong>${candle.date}</strong></div>`,
          `<div>${t("btOpenPrice")}: ${score(candle.open)}</div>`,
          `<div>${t("btClosePrice")}: ${score(candle.close)}</div>`,
          `<div>${t("chartHigh")}: ${score(candle.high)}</div>`,
          `<div>${t("chartLow")}: ${score(candle.low)}</div>`,
        ];
        const tradeEvents = tradeEventsByDate.get(candle.date) ?? [];
        if (tradeEvents.length > 0) {
          lines.push(`<div style="margin-top:6px">${tradeEvents.join("<br/>")}</div>`);
        }
        return lines.join("");
      },
    },
    legend: { top: 0, data: [t("btPriceChart"), t("btBuyPoint"), t("btSellPoint")] },
    grid: { left: 52, right: 18, top: 34, bottom: 36 },
    xAxis: { type: "category", data: priceDates, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", scale: true, axisLabel: { fontSize: 10 } },
    series: [
      {
        name: t("btPriceChart"),
        type: "candlestick",
        data: priceRows.map((point) => [point.open, point.close, point.low, point.high]),
        itemStyle: { color: "#b42318", color0: "#0f766e", borderColor: "#b42318", borderColor0: "#0f766e" },
      },
      {
        name: t("btBuyPoint"),
        type: "scatter",
        symbol: "triangle",
        symbolSize: 14,
        itemStyle: { color: "#0f766e" },
        tooltip: {
          trigger: "item",
          formatter: (params: any) => params.data.hoverLines?.join("") ?? params.data.tooltipLabel,
        },
        label: {
          show: false,
          position: "top",
          fontSize: 10,
          formatter: (params: any) => params.data.markerLabel,
        },
        emphasis: {
          scale: 1.1,
          label: {
            show: true,
            position: "top",
            fontSize: 10,
            formatter: (params: any) => params.data.markerLabel,
          },
        },
        data: buyMarkers,
      },
      {
        name: t("btSellPoint"),
        type: "scatter",
        symbol: "triangle",
        symbolRotate: 180,
        symbolSize: 14,
        itemStyle: { color: "#b42318" },
        tooltip: {
          trigger: "item",
          formatter: (params: any) => params.data.hoverLines?.join("") ?? params.data.tooltipLabel,
        },
        label: {
          show: false,
          position: "bottom",
          fontSize: 10,
          formatter: (params: any) => params.data.markerLabel,
        },
        emphasis: {
          scale: 1.1,
          label: {
            show: true,
            position: "bottom",
            fontSize: 10,
            formatter: (params: any) => params.data.markerLabel,
          },
        },
        data: sellMarkers,
      },
    ],
  };

  const equityChartOption = {
    tooltip: { trigger: "axis" },
    grid: { left: 52, right: 18, top: 24, bottom: 36 },
    xAxis: { type: "category", data: equityCurve.map((point) => point.date), axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", scale: true, axisLabel: { fontSize: 10 } },
    series: [{
      name: t("btReturn"),
      type: "line",
      smooth: true,
      showSymbol: false,
      data: equityCurve.map((point) => point.equity),
      lineStyle: { color: "#0f766e", width: 2 },
      areaStyle: { color: "rgba(15,118,110,0.10)" },
    }],
  };

  const metrics = [
    { label: t("btTotalReturn"), value: percent(result?.total_return_pct), className: pnlClass(result?.total_return_pct) },
    { label: t("btMaxDrawdown"), value: percent(result?.max_drawdown_pct), className: "" },
    { label: t("btWinRate"), value: percent(result?.win_rate), className: pnlClass(result?.win_rate != null ? result.win_rate - 0.5 : null) },
    { label: t("btTradeCount"), value: String(result?.trade_count ?? 0), className: "" },
    { label: t("btProfitFactor"), value: result?.profit_factor != null ? score(result.profit_factor) : "-", className: "" },
    { label: t("btAvgHoldDays"), value: result?.avg_holding_days != null ? score(result.avg_holding_days) : "-", className: "" },
  ];

  const columns: ColumnsType<BacktestTrade> = [
    { title: t("btSymbolId"), dataIndex: "symbol_id", width: 90 },
    { title: t("btEntryDate"), dataIndex: "entry_date", width: 124, render: (value, trade) => <div><div>{value}</div>{trade.entry_signal_date && trade.entry_signal_date !== value ? <div className="item-subline">{t("btSignalDate")}: {trade.entry_signal_date}</div> : null}</div> },
    { title: `${t("btEntryPrice")}(${priceFieldLabel(entryField)})`, dataIndex: "entry_price", render: (value, trade) => <div style={{ textAlign: "right" }}><div>{score(value)}</div>{trade.entry_signal_price != null ? <div className="item-subline">{t("btSignalPrice")}: {score(trade.entry_signal_price)}</div> : null}</div>, align: "right" },
    { title: t("btEntryMode"), width: 124, render: (_, trade) => <Tag color="green">{executionTimingLabel(trade.entry_execution_timing ?? entryTiming)}</Tag> },
    { title: t("btQuantity"), dataIndex: "quantity", render: (value) => score(value), align: "right" },
    { title: t("btExitDate"), dataIndex: "exit_date", width: 124, render: (value, trade) => value ? <div><div>{value}</div>{trade.exit_signal_date && trade.exit_signal_date !== value ? <div className="item-subline">{t("btSignalDate")}: {trade.exit_signal_date}</div> : null}</div> : t("btNotClosed") },
    { title: `${t("btExitPrice")}(${priceFieldLabel(exitField)})`, dataIndex: "exit_price", render: (value, trade) => value == null ? "-" : <div style={{ textAlign: "right" }}><div>{score(value)}</div>{trade.exit_signal_price != null ? <div className="item-subline">{t("btSignalPrice")}: {score(trade.exit_signal_price)}</div> : null}</div>, align: "right" },
    { title: t("btExitMode"), width: 124, render: (_, trade) => trade.exit_date ? <Tag color="red">{executionTimingLabel(trade.exit_execution_timing ?? exitTiming)}</Tag> : <Tag>{t("btNotClosed")}</Tag> },
    {
      title: t("btExitReason"),
      dataIndex: "exit_reason",
      render: (value) => value ? <Tag>{exitReasonLabel(value)}</Tag> : <Tag color="blue">{t("btNotClosed")}</Tag>,
    },
    { title: t("btPnl"), dataIndex: "pnl", render: (value) => <span className={pnlClass(value)}>{money(value, 2)}</span>, align: "right" },
    { title: t("btPnlPct"), dataIndex: "pnl_pct", render: (value) => <span className={pnlClass(value)}>{percent(value)}</span>, align: "right" },
    { title: t("btHoldDays"), dataIndex: "hold_days", align: "right" },
  ];

  const missColumns: ColumnsType<Record<string, any>> = [
    { title: t("btMissDate"), dataIndex: "date", width: 110 },
    { title: t("btMissOpen"), dataIndex: "open", width: 90, align: "right", render: (value) => value == null ? "-" : score(value) },
    { title: t("btMissClose"), dataIndex: "close", width: 90, align: "right", render: (value) => value == null ? "-" : score(value) },
    { title: t("btMissReason"), dataIndex: "reason", width: 120, render: (value) => <Tag color="orange">{reasonLabel(String(value))}</Tag> },
    {
      title: t("btMissScore"),
      width: 140,
      render: (_, row) => `${t("quality")}:${row.quality_score != null ? score(row.quality_score) : "-"} / ${t("timing")}:${row.timing_score != null ? score(row.timing_score) : "-"}`,
    },
    {
      title: t("btMissState"),
      width: 140,
      render: (_, row) => `${t(`stage_${row.stage ?? "start"}`)} / ${t(`action_${row.action ?? "open"}`)}`,
    },
    {
      title: t("btMissFailedConditions"),
      render: (_, row) => traceSummaryText((row.failed_traces ?? []) as BacktestConditionTrace[], true),
    },
  ];

  if (!result) {
    return <div className="empty">{t("btNoData")}</div>;
  }

  return (
    <section className="backtest-result">
      <div style={{ marginBottom: 8 }}>
        <Tag color={config.version === 2 ? "purple" : "default"}>
          {config.version === 2 ? t("btModeAdvanced") : t("btModeStandard")}
        </Tag>
      </div>

      <Row gutter={[12, 12]}>
        {metrics.map((item) => (
          <Col key={item.label} xs={12} md={8} xl={4}>
            <Card size="small">
              <div className="metric-label">{item.label}</div>
              <div className={item.className}>{item.value}</div>
            </Card>
          </Col>
        ))}
      </Row>

      <div className="backtest-diagnostics">
        <div className="backtest-diagnostics-head">
          <strong>{t("btRuleDiagnosis")}</strong>
          <span>{t("btExecutionBasis")}: {t("btBuyPoint")}={executionTimingLabel(entryTiming)} / {t("btSellPoint")}={executionTimingLabel(exitTiming)}</span>
        </div>
        <div className="backtest-diagnostics-grid">
          <span><b>{t("btCheckedDays")}</b><strong>{checkedDays}</strong></span>
          <span><b>{t("btBuySignalDays")}</b><strong>{buySignalDays}</strong></span>
          <span><b>{t("btBlockedDays")}</b><strong>{blockedDays}</strong></span>
          <span><b>{t("btSignalConversion")}</b><strong>{signalConversion == null ? "-" : percent(signalConversion)}</strong></span>
          <span><b>{t("btClosedTrades")}</b><strong>{closedTrades.length}</strong></span>
          <span><b>{t("btOpenTrades")}</b><strong>{openTrades.length}</strong></span>
          <span><b>{t("btAvgWin")}</b><strong className={pnlClass(avgWinPct)}>{avgWinPct == null ? "-" : percent(avgWinPct)}</strong></span>
          <span><b>{t("btAvgLoss")}</b><strong className={pnlClass(avgLossPct)}>{avgLossPct == null ? "-" : percent(avgLossPct)}</strong></span>
        </div>
        {diagnostics.fill_warning && <Alert type="warning" showIcon message={t("btFillWarning")} />}
        {skipReasons.length > 0 && (
          <div>
            <div className="panel-meta" style={{ marginBottom: 6 }}>{t("btBlockedReasonBreakdown")}</div>
            <div className="backtest-reason-list">
              {skipReasons.map(([reason, count]) => (
                <Tag key={reason}>{reasonLabel(reason)} {count}</Tag>
              ))}
            </div>
          </div>
        )}
        {closedTrades.length > 0 && (
          <div>
            <div className="panel-meta" style={{ marginBottom: 6 }}>{t("btExitReasonBreakdown")}</div>
            <div className="backtest-reason-list">
              {Array.from(closedTrades.reduce((acc, trade) => {
                const key = trade.exit_reason || "open";
                acc.set(key, (acc.get(key) ?? 0) + 1);
                return acc;
              }, new Map<string, number>()).entries()).map(([reason, count]) => (
                <Tag key={reason} color="blue">{exitReasonLabel(reason)} {count}</Tag>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="backtest-chart-card">
        <div className="backtest-chart-title">{t("btTradePointChart")}</div>
        <div className="panel-meta" style={{ marginBottom: 10 }}>
          <Space wrap>
            <Tag color="green">{t("btBuyPoint")}: {executionTimingLabel(entryTiming)}</Tag>
            <Tag color="red">{t("btSellPoint")}: {executionTimingLabel(exitTiming)}</Tag>
            <Tag>{t("btBuyPoint")} {buyMarkers.length}</Tag>
            <Tag>{t("btSellPoint")} {sellMarkers.length}</Tag>
          </Space>
        </div>
        {priceSeries.length > 0 ? (
          <ReactECharts option={priceChartOption} style={{ height: 360, width: "100%" }} />
        ) : (
          <div className="empty">{t("noChart")}</div>
        )}
      </div>

      <div className="backtest-chart-card">
        <div className="backtest-chart-title">{t("btEquityChart")}</div>
        <ReactECharts option={equityChartOption} style={{ height: 220, width: "100%" }} />
      </div>

      {sampleMisses.length > 0 && (
        <div className="backtest-chart-card">
          <div className="backtest-chart-title">{t("btBlockedSampleTitle")}</div>
          <Table
            rowKey={(row) => `${row.date}_${row.symbol_id}_${row.reason}`}
            size="small"
            columns={missColumns}
            dataSource={sampleMisses}
            pagination={false}
            scroll={{ x: 980 }}
          />
        </div>
      )}

      <div className="backtest-chart-card">
        <div className="backtest-chart-title">{t("btTradeTableTitle")}</div>
        <Table
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={trades}
          pagination={{ pageSize: 8 }}
          scroll={{ x: 1320 }}
          expandable={{
            expandedRowRender: (trade) => (
              <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
                {renderTraceList(
                  t("btEntryTriggerTitle"),
                  trade.entry_traces,
                  `${t("btEntryPrice")} (${priceFieldLabel(entryField)})`,
                  trade.entry_price,
                  [
                    `${t("btQuantity")} ${score(trade.quantity)}`,
                    `${t("btEntryCost")} ${money(trade.entry_cost, 2)}`,
                  ],
                )}
                {renderTraceList(
                  t("btExitTriggerTitle"),
                  trade.exit_traces,
                  `${t("btExitPrice")} (${priceFieldLabel(exitField)})`,
                  trade.exit_price,
                  [
                    `${t("btExitReason")} ${exitReasonLabel(trade.exit_reason)}`,
                    `${t("btExitCost")} ${trade.exit_cost != null ? money(trade.exit_cost, 2) : "-"}`,
                  ],
                )}
              </div>
            ),
            rowExpandable: (trade) => Boolean((trade.entry_traces?.length ?? 0) || (trade.exit_traces?.length ?? 0)),
          }}
        />
      </div>
    </section>
  );
}
