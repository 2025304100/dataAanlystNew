import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Col, Empty, Modal, Progress, Row, Select, Space, Statistic, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import ReactECharts from "echarts-for-react";
import { ReloadOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import type { MacroIndicator, MacroOverview } from "../types";

const { Paragraph, Text } = Typography;

const LABELS = {
  "zh-CN": {
    title: "宏观数据",
    subtitle: "把 CPI、PPI、PMI、融资融券和信用数据汇总成市场环境分。",
    update: "更新宏观数据",
    updating: "更新中...",
    region: "区域",
    all: "综合",
    cn: "中国大陆",
    us: "美国",
    marketScore: "市场环境分",
    stance: "环境判断",
    indicators: "指标数",
    failed: "失败源",
    brief: "数据分析简报",
    radar: "五维评分",
    table: "指标明细",
    empty: "暂无宏观快照，点击更新宏观数据。",
    value: "最新值",
    previous: "前值",
    delta: "变化",
    period: "周期",
    score: "指标分",
    status: "状态",
    category: "维度",
    source: "来源",
    clickHint: "点击指标行查看历史公布数据",
    historyTitle: "历史公布数据",
    historyChart: "趋势图",
    noHistory: "暂无历史数据，请先更新宏观数据。",
    positive: "偏利好",
    neutral: "中性",
    negative: "偏压力",
    risk_on: "偏积极",
    cautious: "偏谨慎",
    defensive: "防守",
    growth: "增长",
    inflation: "通胀",
    liquidity: "流动性",
    credit: "信用/杠杆",
    risk: "风险",
    updated: "更新时间",
  },
  "en-US": {
    title: "Macro Data",
    subtitle: "CPI, PPI, PMI, margin financing and credit data summarized into one market regime score.",
    update: "Update Macro",
    updating: "Updating...",
    region: "Region",
    all: "Global",
    cn: "China Mainland",
    us: "United States",
    marketScore: "Market Score",
    stance: "Regime",
    indicators: "Indicators",
    failed: "Failed Sources",
    brief: "Analysis Brief",
    radar: "Factor Scores",
    table: "Indicator Details",
    empty: "No macro snapshot yet. Click update to fetch data.",
    value: "Latest",
    previous: "Previous",
    delta: "Delta",
    period: "Period",
    score: "Score",
    status: "Status",
    category: "Factor",
    source: "Source",
    clickHint: "Click a row to view historical releases",
    historyTitle: "Release History",
    historyChart: "Trend",
    noHistory: "No history yet. Update macro data first.",
    positive: "Positive",
    neutral: "Neutral",
    negative: "Pressure",
    risk_on: "Risk-on",
    cautious: "Cautious",
    defensive: "Defensive",
    growth: "Growth",
    inflation: "Inflation",
    liquidity: "Liquidity",
    credit: "Credit/Leverage",
    risk: "Risk",
    updated: "Updated",
  },
} as const;

const INDICATOR_LABELS: Record<string, { zh: string; en: string }> = {
  cn_cpi_yoy: { zh: "\u4e2d\u56fd CPI \u540c\u6bd4", en: "China CPI YoY" },
  cn_ppi_yoy: { zh: "\u4e2d\u56fd PPI \u540c\u6bd4", en: "China PPI YoY" },
  cn_pmi: { zh: "\u4e2d\u56fd\u5236\u9020\u4e1a PMI", en: "China Manufacturing PMI" },
  cn_m2_yoy: { zh: "\u4e2d\u56fd M2 \u540c\u6bd4", en: "China M2 YoY" },
  cn_new_credit: { zh: "\u4e2d\u56fd\u65b0\u589e\u4fe1\u8d37", en: "China New Credit" },
  cn_social_financing: { zh: "\u4e2d\u56fd\u793e\u878d\u89c4\u6a21", en: "China Social Financing" },
  cn_10y_yield: { zh: "\u4e2d\u56fd10\u5e74\u56fd\u503a\u6536\u76ca\u7387", en: "China 10Y Government Bond Yield" },
  cn_lpr_1y: { zh: "\u4e2d\u56fd LPR 1\u5e74", en: "China LPR 1Y" },
  cn_lpr_5y: { zh: "\u4e2d\u56fd LPR 5\u5e74", en: "China LPR 5Y" },
  cn_margin_sh: { zh: "\u4e0a\u4ea4\u6240\u878d\u8d44\u878d\u5238\u4f59\u989d", en: "SSE Margin Balance" },
  cn_margin_sz: { zh: "\u6df1\u4ea4\u6240\u878d\u8d44\u878d\u5238\u4f59\u989d", en: "SZSE Margin Balance" },
  us_cpi_yoy: { zh: "\u7f8e\u56fd CPI \u540c\u6bd4", en: "US CPI YoY" },
  us_10y_yield: { zh: "\u7f8e\u56fd10\u5e74\u56fd\u503a\u6536\u76ca\u7387", en: "US 10Y Treasury Yield" },
  us_core_cpi_mom: { zh: "\u7f8e\u56fd\u6838\u5fc3 CPI \u73af\u6bd4", en: "US Core CPI MoM" },
  us_ppi: { zh: "\u7f8e\u56fd PPI \u540c\u6bd4", en: "US PPI YoY" },
  us_industrial_production: { zh: "\u7f8e\u56fd\u5de5\u4e1a\u4ea7\u51fa\u540c\u6bd4", en: "US Industrial Production YoY" },
  us_non_farm: { zh: "\u7f8e\u56fd\u975e\u519c\u5c31\u4e1a", en: "US Nonfarm Payrolls" },
  us_unemployment: { zh: "\u7f8e\u56fd\u5931\u4e1a\u7387", en: "US Unemployment Rate" },
};

function indicatorName(row: MacroIndicator, locale: string) {
  const item = INDICATOR_LABELS[row.indicator_key];
  if (!item) return row.name;
  return locale === "en-US" ? item.en : item.zh;
}

function fmt(value: number | null | undefined, unit?: string | null, locale = "zh-CN") {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  const abs = Math.abs(value);
  if (unit === "CNY") return locale === "en-US" ? "CNY " + (value / 100000000).toFixed(2) + "00M" : (value / 100000000).toFixed(2) + "\u4ebf\u5143";
  if (unit === "CNY 100M") return locale === "en-US" ? "CNY " + value.toFixed(0) + "00M" : value.toFixed(0) + "\u4ebf\u5143";
  if (unit === "10k people") return locale === "en-US" ? value.toFixed(1) + "0k" : value.toFixed(1) + "\u4e07\u4eba";
  const rendered = abs >= 10000 ? (value / 10000).toFixed(2) + "\u4e07" : value.toFixed(abs >= 100 ? 0 : 2);
  return unit ? rendered + unit : rendered;
}

function scoreColor(score: number) {
  if (score >= 68) return "#0f766e";
  if (score <= 42) return "#b42318";
  return "#d97706";
}

function statusColor(status: string) {
  if (status === "positive") return "success";
  if (status === "negative") return "error";
  return "default";
}

export default function MacroData() {
  const ctx = useApp();
  const labels = LABELS[ctx.locale as "zh-CN" | "en-US"] ?? LABELS["zh-CN"];
  const [region, setRegion] = useState<"all" | "cn" | "us">("all");
  const [overview, setOverview] = useState<MacroOverview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedIndicator, setSelectedIndicator] = useState<MacroIndicator | null>(null);
  const [history, setHistory] = useState<MacroIndicator[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  const load = async (nextRegion = region) => {
    setError(null);
    try {
      const data = await api.getMacroOverview(nextRegion);
      setOverview(data);
    } catch (err: any) {
      setError(err.message);
    }
  };

  useEffect(() => {
    load(region);
  }, [region]);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.updateMacroData({ region });
      setOverview(data);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const openHistory = async (row: MacroIndicator) => {
    setSelectedIndicator(row);
    setHistoryLoading(true);
    try {
      const rows = await api.getMacroIndicatorHistory(row.region, row.indicator_key, 80);
      setHistory(rows);
    } catch (err: any) {
      setError(err.message);
      setHistory([]);
    } finally {
      setHistoryLoading(false);
    }
  };
  const snapshot = overview?.snapshot ?? null;
  const dimensionRows = useMemo(() => {
    if (!snapshot) return [];
    return [
      { key: "growth", value: snapshot.growth_score },
      { key: "inflation", value: snapshot.inflation_score },
      { key: "liquidity", value: snapshot.liquidity_score },
      { key: "credit", value: snapshot.credit_score },
      { key: "risk", value: snapshot.risk_score },
    ];
  }, [snapshot]);

  const radarOption = useMemo(() => {
    const values = dimensionRows.map((row) => Math.max(0, Math.min(100, 50 + row.value * 2.5)));
    return {
      tooltip: {},
      radar: {
        radius: "68%",
        indicator: dimensionRows.map((row) => ({ name: labels[row.key as keyof typeof labels], max: 100 })),
        splitNumber: 4,
      },
      series: [
        {
          type: "radar",
          data: [{ value: values, name: labels.marketScore, areaStyle: { opacity: 0.18 } }],
          lineStyle: { color: "#0f766e", width: 2 },
          itemStyle: { color: "#0f766e" },
        },
      ],
    };
  }, [dimensionRows, labels]);

  const historyOption = useMemo(() => {
    const data = history.filter((row) => row.value !== null && row.value !== undefined);
    return {
      tooltip: { trigger: "axis" },
      grid: { left: 48, right: 18, top: 24, bottom: 44 },
      xAxis: { type: "category", data: data.map((row) => row.period) },
      yAxis: { type: "value", scale: true },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18 }],
      series: [{ type: "line", smooth: true, showSymbol: data.length <= 24, data: data.map((row) => row.value), lineStyle: { color: "#0f766e", width: 2 }, itemStyle: { color: "#0f766e" }, areaStyle: { color: "rgba(15, 118, 110, 0.10)" } }],
    };
  }, [history]);

  const historyColumns: ColumnsType<MacroIndicator> = [
    { title: labels.period, dataIndex: "period", width: 140 },
    { title: labels.value, dataIndex: "value", render: (_, row) => fmt(row.value, row.unit, ctx.locale), align: "right" },
    { title: labels.previous, dataIndex: "previous_value", render: (_, row) => fmt(row.previous_value, row.unit, ctx.locale), align: "right" },
    { title: labels.delta, dataIndex: "delta", render: (value) => fmt(value, null, ctx.locale), align: "right" },
    { title: labels.score, dataIndex: "score", render: (value: number) => value.toFixed(1), align: "right" },
  ];
  const columns: ColumnsType<MacroIndicator> = [
    {
      title: labels.category,
      dataIndex: "category",
      render: (value) => labels[value as keyof typeof labels] ?? value,
      width: 110,
    },
    { title: labels.title, dataIndex: "name", width: 220, render: (_, row) => <Button type="link" className="macro-link-button">{indicatorName(row, ctx.locale)}</Button> },
    { title: labels.period, dataIndex: "period", width: 120 },
    {
      title: labels.value,
      dataIndex: "value",
      render: (_, row) => fmt(row.value, row.unit, ctx.locale),
      align: "right",
      width: 120,
    },
    {
      title: labels.previous,
      dataIndex: "previous_value",
      render: (_, row) => fmt(row.previous_value, row.unit, ctx.locale),
      align: "right",
      width: 120,
    },
    {
      title: labels.delta,
      dataIndex: "delta",
      render: (value) => fmt(value, null, ctx.locale),
      align: "right",
      width: 90,
    },
    {
      title: labels.score,
      dataIndex: "score",
      render: (value: number) => <Text strong style={{ color: value >= 0 ? "#0f766e" : "#b42318" }}>{value.toFixed(1)}</Text>,
      align: "right",
      width: 90,
      sorter: (a, b) => a.score - b.score,
      defaultSortOrder: "descend",
    },
    {
      title: labels.status,
      dataIndex: "status",
      render: (value: string) => <Tag color={statusColor(value)}>{labels[value as keyof typeof labels] ?? value}</Tag>,
      width: 100,
    },
  ];

  return (
    <div className="macro-page">
      <section className="macro-hero">
        <div>
          <p className="panel-kicker">{labels.title}</p>
          <h1>{labels.title}</h1>
          <p>{labels.subtitle}</p>
        </div>
        <Space wrap>
          <Select
            value={region}
            onChange={(value) => setRegion(value)}
            style={{ width: 140 }}
            options={[
              { value: "all", label: labels.all },
              { value: "cn", label: labels.cn },
              { value: "us", label: labels.us },
            ]}
          />
          <Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={refresh}>
            {loading ? labels.updating : labels.update}
          </Button>
        </Space>
      </section>

      {error && <Alert type="error" message={error} showIcon className="macro-alert" />}

      {!snapshot ? (
        <Card className="macro-empty">
          <Empty description={labels.empty}>
            <Button type="primary" loading={loading} onClick={refresh}>{labels.update}</Button>
          </Empty>
        </Card>
      ) : (
        <>
          <Row gutter={[12, 12]} className="macro-score-row">
            <Col xs={24} md={8}>
              <Card>
                <Statistic
                  title={labels.marketScore}
                  value={snapshot.market_score}
                  precision={1}
                  valueStyle={{ color: scoreColor(snapshot.market_score) }}
                />
                <Progress percent={Math.round(snapshot.market_score)} strokeColor={scoreColor(snapshot.market_score)} showInfo={false} />
              </Card>
            </Col>
            <Col xs={12} md={4}>
              <Card><Statistic title={labels.stance} value={labels[snapshot.stance as keyof typeof labels] ?? snapshot.stance} /></Card>
            </Col>
            <Col xs={12} md={4}>
              <Card><Statistic title={labels.indicators} value={snapshot.indicators_total} /></Card>
            </Col>
            <Col xs={12} md={4}>
              <Card><Statistic title={labels.failed} value={snapshot.failed_total} /></Card>
            </Col>
            <Col xs={12} md={4}>
              <Card><Statistic title={labels.updated} value={snapshot.created_at ? new Date(snapshot.created_at).toLocaleDateString() : "-"} /></Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="macro-main-row">
            <Col xs={24} lg={10}>
              <Card title={labels.radar} className="macro-card">
                <ReactECharts option={radarOption} style={{ height: 320 }} />
              </Card>
            </Col>
            <Col xs={24} lg={14}>
              <Card title={labels.brief} className="macro-card">
                <div className="macro-brief">
                  {(overview?.brief ?? []).map((line, index) => (
                    <Paragraph key={index}>{line}</Paragraph>
                  ))}
                </div>
                {overview?.failed?.length ? (
                  <Alert
                    type="warning"
                    showIcon
                    message={`${labels.failed}: ${overview.failed.length}`}
                    description={overview.failed.slice(0, 3).map((item) => item.name || item.indicator_key).join(" / ")}
                  />
                ) : null}
              </Card>
            </Col>
          </Row>

          <Card title={labels.table} extra={<Text type="secondary">{labels.clickHint}</Text>} className="macro-card">
            <Table
              rowKey={(row) => `${row.region}-${row.indicator_key}`}
              columns={columns}
              dataSource={overview?.indicators ?? []}
              pagination={false}
              scroll={{ x: 960 }}
              size="middle"
              onRow={(record) => ({ onClick: () => openHistory(record) })}
              rowClassName="macro-clickable-row"
            />
          </Card>
        </>
      )}
      <Modal
        open={!!selectedIndicator}
        title={selectedIndicator ? indicatorName(selectedIndicator, ctx.locale) : labels.historyTitle}
        onCancel={() => setSelectedIndicator(null)}
        footer={null}
        width={920}
        destroyOnClose
      >
        {history.length === 0 && !historyLoading ? (
          <Empty description={labels.noHistory} />
        ) : (
          <>
            <Card title={labels.historyChart} size="small" className="macro-history-card">
              <ReactECharts option={historyOption} showLoading={historyLoading} style={{ height: 300 }} />
            </Card>
            <Table
              rowKey={(row) => row.region + "-" + row.indicator_key + "-" + row.period}
              columns={historyColumns}
              dataSource={[...history].reverse()}
              pagination={{ pageSize: 8, size: "small" }}
              size="small"
              scroll={{ x: 720 }}
            />
          </>
        )}
      </Modal>
    </div>
  );
}
