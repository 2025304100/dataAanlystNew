import ReactECharts from "echarts-for-react";
import { Card, Col, Row, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { BacktestRun, BacktestTrade } from "../types";
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

export default function BacktestResult({ result }: BacktestResultProps) {
  const equityCurve = parseEquityCurve(result);
  const trades = result?.trades ?? [];

  const chartOption = {
    tooltip: { trigger: "axis" },
    grid: { left: 52, right: 18, top: 24, bottom: 36 },
    xAxis: { type: "category", data: equityCurve.map((p) => p.date), axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", scale: true, axisLabel: { fontSize: 10 } },
    series: [{
      name: "??",
      type: "line",
      smooth: true,
      showSymbol: false,
      data: equityCurve.map((p) => p.equity),
      lineStyle: { color: "#0f766e", width: 2 },
      areaStyle: { color: "rgba(15,118,110,0.10)" },
    }],
  };

  const columns: ColumnsType<BacktestTrade> = [
    { title: "??ID", dataIndex: "symbol_id", width: 90 },
    { title: "???", dataIndex: "entry_date", width: 110 },
    { title: "???", dataIndex: "entry_price", render: (v) => score(v), align: "right" },
    { title: "??", dataIndex: "quantity", align: "right" },
    { title: "???", dataIndex: "exit_date", width: 110, render: (v) => v ?? "???" },
    { title: "???", dataIndex: "exit_price", render: (v) => score(v), align: "right" },
    { title: "??", dataIndex: "exit_reason", render: (v) => v ? <Tag>{v}</Tag> : <Tag color="blue">open</Tag> },
    { title: "??", dataIndex: "pnl", render: (v) => <span className={pnlClass(v)}>{money(v, 2)}</span>, align: "right" },
    { title: "???", dataIndex: "pnl_pct", render: (v) => <span className={pnlClass(v)}>{percent(v)}</span>, align: "right" },
    { title: "????", dataIndex: "hold_days", align: "right" },
  ];

  if (!result) {
    return <div className="empty">????????????????????????????</div>;
  }

  return (
    <section className="backtest-result">
      <Row gutter={[12, 12]}>
        <Col xs={12} md={6}><Card size="small"><div className="metric-label">????</div><div className={pnlClass(result.total_return_pct)}>{percent(result.total_return_pct)}</div></Card></Col>
        <Col xs={12} md={6}><Card size="small"><div className="metric-label">????</div><div>{percent(result.max_drawdown_pct)}</div></Card></Col>
        <Col xs={12} md={6}><Card size="small"><div className="metric-label">??</div><div>{percent(result.win_rate)}</div></Card></Col>
        <Col xs={12} md={6}><Card size="small"><div className="metric-label">????</div><div>{result.trade_count ?? 0}</div></Card></Col>
      </Row>
      <div className="backtest-chart-card">
        <ReactECharts option={chartOption} style={{ height: 260, width: "100%" }} />
      </div>
      <Table rowKey="id" size="small" columns={columns} dataSource={trades} pagination={{ pageSize: 8 }} scroll={{ x: 920 }} />
    </section>
  );
}
