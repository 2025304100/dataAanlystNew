import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Col, Empty, Modal, Progress, Row, Select, Space, Statistic, Table, Tag, Typography, Tooltip } from "antd";
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

// ── 指标详解字典：含义 + 对股市的影响 ──
const INDICATOR_TIPS: Record<string, { zh: string; en: string }> = {
  cn_cpi_yoy: {
    zh: "\u3010\u6d88\u8d39\u8005\u7269\u4ef7\u6307\u6570\u540c\u6bd4\u3011\u8861\u91cf\u5c45\u6c11\u6d88\u8d39\u54c1\u4ef7\u683c\u53d8\u52a8\u3002CPI\u6e29\u548c\u4e0a\u6da8(2-3%)\u5229\u4e8e\u6d88\u8d39\u548c\u76c8\u5229\u589e\u957f\uff1b\u8fc7\u9ad8\u5f15\u53d1\u7d27\u7f29\u9884\u671f\u538b\u5236\u4f30\u503c\uff1b\u8fc7\u4f4e\u751a\u81f3\u901a\u7f29\u5219\u53cd\u6620\u9700\u6c42\u76ae\u8f6f\u3002",
    en: "Consumer Price Index YoY \u2014 measures consumer goods price changes. Moderate CPI (2-3%) supports earnings growth; high CPI triggers tightening expectations that compress valuations; deflation signals weak demand.",
  },
  cn_ppi_yoy: {
    zh: "\u3010\u751f\u4ea7\u8005\u4ef7\u683c\u6307\u6570\u540c\u6bd4\u3011\u8861\u91cf\u51fa\u5382\u4ef7\u683c\u53d8\u52a8\uff0c\u9886\u5148CPI\u7ea61-2\u4e2a\u5b63\u5ea6\u3002PPI\u4e0a\u884c\u9884\u793a\u4f01\u4e1a\u6210\u672c\u538b\u529b\u589e\u5927\uff0c\u4e2d\u4e0b\u6e38\u5229\u6da6\u627f\u538b\uff1bPPI\u4e0b\u884c\u5219\u5229\u597d\u5236\u9020\u4e1a\u6bdb\u5229\u4fee\u590d\u3002",
    en: "Producer Price Index YoY \u2014 factory-gate prices, leads CPI by 1-2 quarters. Rising PPI foreshadows cost pressure on downstream margins; falling PPI supports margin recovery for manufacturers.",
  },
  cn_pmi: {
    zh: "\u3010\u5236\u9020\u4e1a\u91c7\u8d2d\u7ecf\u7406\u6307\u6570\u301150\u4e3a\u8363\u67af\u7ebf\u3002PMI>50\u8868\u793a\u6269\u5f20\uff0c<50\u6536\u7f29\u3002PMI\u6301\u7eed\u8d70\u5f3a\u901a\u5e38\u5bf9\u5e94\u4f01\u4e1a\u76c8\u5229\u6539\u5584\u548c\u5468\u671f\u80a1\u673a\u4f1a\uff1b\u8dcc\u783450\u5219\u9700\u8b66\u60d5\u7ecf\u6d4e\u653e\u6162\u98ce\u9669\u3002",
    en: "Manufacturing PMI \u2014 50 is the boom/bust line. Above 50 = expansion, below 50 = contraction. Sustained strength correlates with earnings improvement and cyclical opportunities; below 50 warns of slowdown risk.",
  },
  cn_m2_yoy: {
    zh: "\u3010\u5e7f\u4e49\u8d27\u5e01\u4f9b\u5e94\u91cf\u540c\u6bd4\u3011\u53cd\u6620\u5e02\u573a\u6d41\u52a8\u6027\u5145\u88d5\u7a0b\u5ea6\u3002M2\u589e\u901f\u9ad8\u4e8e\u540d\u4e49GDP\u589e\u901f\u610f\u5473\u7740\u5bbd\u677e\u73af\u5883\uff0c\u5229\u597d\u98ce\u9669\u8d44\u4ea7\uff1bM2\u6301\u7eed\u4e0b\u884c\u5219\u6697\u793a\u6d41\u52a8\u6027\u6536\u7d27\uff0c\u5bf9\u9ad8\u4f30\u503c\u677f\u5757\u5f62\u6210\u538b\u529b\u3002",
    en: "Broad Money Supply YoY \u2014 measures liquidity abundance. M2 growth above nominal GDP implies easing conditions favorable to risk assets; sustained M2 decline suggests tightening pressure on high-valuation sectors.",
  },
  cn_new_credit: {
    zh: "\u3010\u65b0\u589e\u4eba\u6c11\u5e01\u8d37\u6b3e\u3011\u53cd\u6620\u94f6\u884c\u4f53\u7cfb\u5411\u5b9e\u4f53\u6ce8\u5165\u7684\u4fe1\u7528\u89c4\u6a21\u3002\u793e\u878e\u653e\u91cf+\u4fe1\u8d37\u6269\u5f20=\u5bbd\u4fe1\u7528\u5468\u671f\uff0c\u5229\u597d\u91d1\u878e\u3001\u5730\u4ea7\u7b49\u5229\u7387\u654f\u611f\u578b\u884c\u4e1a\uff1b\u4fe1\u8d39\u840f\u7f29\u5219\u4fe1\u53f7\u504f\u7a7a\u3002",
    en: "New RMB Loans \u2014 credit injected into the real economy via banks. Expanding credit = easing cycle, beneficial to rate-sensitive sectors like finance and property; contracting credit is a bearish signal.",
  },
  cn_social_financing: {
    zh: "\u3010\u793e\u4f1a\u878d\u8d44\u89c4\u6a21\u5b58\u91cf\u3011\u7efc\u5408\u53cd\u6620\u5b9e\u4f53\u7ecf\u6d4e\u4ece\u91d1\u878d\u4f53\u7cfb\u83b7\u5f97\u7684\u5168\u90e8\u8d44\u91d1\u3002\u793e\u878e\u589e\u901f\u56de\u5347\u662f\u7ecf\u6d4e\u4f01\u7a33\u7684\u5148\u884c\u6307\u6807\uff1b\u589e\u901f\u4e0b\u6ed1\u5219\u9884\u793a\u4fe1\u7528\u5468\u671f\u8f6c\u5f31\u3002",
    en: "Total Social Financing Stock \u2014 all funding the real economy receives from the financial system. Rising TSF growth is a leading indicator of economic stabilization; declining growth signals a weakening credit cycle.",
  },
  cn_10y_yield: {
    zh: "\u301010\u5e74\u671f\u56fd\u503a\u6536\u76ca\u7387\u3011\u65e0\u98ce\u9669\u5229\u7387\u951a\u5b9a\u3002\u6536\u76ca\u7387\u4e0b\u884c=\u503a\u725b+\u5229\u597d\u6210\u957f\u80a1DCF\u4f30\u503c\uff1b\u6536\u76ca\u7387\u5feb\u901f\u4e0a\u884c\u5219\u538b\u5236\u9ad8\u4f30\u503c\u677f\u5757\uff0c\u4e14\u53ef\u80fd\u89e6\u53d1\u5916\u8d44\u6d41\u51fa\u65b0\u5174\u5e02\u573a\u3002",
    en: "10Y Government Bond Yield \u2014 risk-free rate anchor. Falling yields = bond bull + DCF valuation boost for growth stocks; rapidly rising yields compresses high-valuation sectors and may trigger capital outflow from EM.",
  },
  cn_lpr_1y: {
    zh: "\u30101\u5e74\u671fLPR\u62a5\u4ef7\u3011\u77ed\u671f\u8d37\u6b3e\u57fa\u51c6\u5229\u7387\u3002LPR\u4e0b\u8c03\u76f4\u63a5\u964d\u4f4e\u4f01\u4e1a\u878d\u8d44\u6210\u672c\uff0c\u523a\u6fc0\u6295\u8d44\u548c\u6d88\u8d39\uff1b\u4e0a\u8c03\u5219\u6536\u7d27\u6d41\u52a8\u6027\u9884\u671f\u3002",
    en: "1Y LPR \u2014 short-term lending benchmark rate. LPR cuts directly reduce corporate financing costs, stimulating investment and consumption; hikes tighten liquidity expectations.",
  },
  cn_lpr_5y: {
    zh: "\u30105\u5e74\u671fLPR\u62a5\u4ef7\u3011\u623f\u8d37\u548c\u4e2d\u957f\u671f\u8d37\u6b3e\u57fa\u51c6\u30025\u5e74\u671fLPR\u4e0b\u8c03\u6700\u76f4\u63a5\u5f71\u54cd\u5730\u4ea7\u94fe\u548c\u57fa\u5efa\u677f\u5757\uff1b\u662f\u89c2\u5bdf\u8d27\u5e01\u653f\u7b56\u677e\u7d27\u7684\u6838\u5fc3\u7a97\u53e3\u3002",
    en: "5Y LPR \u2014 mortgage and long-term loan benchmark. 5Y LPR cuts directly impact property and infrastructure sectors; key window for observing monetary policy stance.",
  },
  cn_margin_sh: {
    zh: "\u3010\u4e0a\u4ea4\u6240\u878d\u8d44\u4f59\u989d\u3011\u53cd\u6628A\u80a1\u6746\u6746\u8d44\u91d1\u60c5\u7eea\u3002\u878d\u8d44\u4f59\u989d\u8fde\u7eed\u6500\u5347=\u5e02\u573a\u98ce\u9669\u504f\u597d\u4e0a\u5347\uff0c\u5f80\u5f80\u4f34\u968f\u91cf\u4ef7\u9f50\u5347\uff1b\u5feb\u901f\u56de\u843d\u5219\u9884\u8b66\u56de\u8c03\u6216\u8e29\u8e04\u98ce\u9669\u3002",
    en: "SSE Margin Balance \u2014 A-share leverage sentiment. Rising margin balance = rising risk appetite, often accompanied by volume-price gains; rapid decline warns of pullback or stampede risk.",
  },
  cn_margin_sz: {
    zh: "\u3010\u6df1\u4ea4\u6240\u878d\u8d44\u4f59\u989d\u3011\u540c\u4e0a\u4ea4\u6240\uff0c\u4fa7\u91cd\u4e2d\u5c0f\u76d8/\u79d1\u6280\u6210\u957f\u80a1\u7684\u6746\u6746\u60c5\u7eea\u3002\u6df1\u5e02\u878d\u8d44\u53d8\u5316\u5bf9\u5214\u4e1a\u677f\u6307\u6709\u8f83\u5f3a\u9886\u5148\u6027\u3002",
    en: "SZSE Margin Balance \u2014 same as SSE but focused on mid/small-cap / tech growth leverage sentiment. SZSE margin changes have strong leading power for ChiNext index.",
  },
  us_cpi_yoy: {
    zh: "\u3010\u7f8e\u56fdCPI\u540c\u6bd4\u3011\u5168\u7403\u901a\u80c0\u98ce\u5411\u6807\u3002\u7f8e\u56fdCPI\u8d85\u9884\u671f\u2192\u7f8e\u8054\u50a8\u9e70\u6d3e\u52a0\u606f\u2192\u7f8e\u5143\u8d70\u5f3a\u2192\u65b0\u5174\u5e02\u573a\u8d44\u672c\u5916\u6d41+\u7f8e\u503a\u6536\u76ca\u7387\u4e0a\u884c\u2192\u5168\u7403\u98ce\u9669\u8d44\u4ea7\u627f\u538b\u3002",
    en: "US CPI YoY \u2014 global inflation barometer. US CPI beat \u2192 Fed hawkish hike \u2192 strong dollar \u2192 EM capital outflow + Treasury yield rise \u2192 global risk assets under pressure.",
  },
  us_10y_yield: {
    zh: "\u3010\u7f8e\u56fd10\u5e74\u671f\u56fd\u503a\u6536\u76ca\u7387\u3011\u5168\u7403\u8d44\u4ea7\u5b9a\u4ef7\u4e4b\u951a\u3002\u7f8e\u503a\u6536\u76ca\u7387\u4e0a\u884c\u538b\u5236\u5168\u7403\u6210\u957f\u80a1\u4f30\u503c\uff0c\u5c24\u5176\u5bf9\u7eb3\u65af\u8fbe\u514b\u548c\u6e2f\u80a1\u79d1\u6280\u80a1\u51b2\u51fb\u660e\u663e\uff1b\u4e0b\u884c\u5219\u91ca\u653e\u4f30\u503c\u7a7a\u95f4\u3002",
    en: "US 10Y Treasury Yield \u2014 global asset pricing anchor. Rising yields suppress global growth stock valuations, especially Nasdaq and HK tech stocks; falling yields release valuation room.",
  },
  us_core_cpi_mom: {
    zh: "\u3010\u7f8e\u56fd\u6838\u5fc3CPI\u73af\u6bd4\u3011\u5254\u9664\u98df\u54c1\u80fd\u6e90\u540e\u7684\u901a\u80c0\u6838\u5fc3\u9879\u3002\u6838\u5fc3CPI\u662f\u7f8e\u8054\u50a8\u51b3\u7b56\u7684\u6700\u91cd\u8981\u53c2\u8003\u4e4b\u4e00\uff0c\u73af\u6bd4\u8d85\u9884\u671f\u4f1a\u5f3a\u5316\u52a0\u606f\u8def\u5f84\u9884\u671f\u3002",
    en: "US Core CPI MoM \u2014 inflation excluding food & energy. Core CPI is one of the Fed's most important decision references; MoM beats strengthen rate-hike path expectations.",
  },
  us_ppi: {
    zh: "\u3010\u7f8e\u56fdPPI\u540c\u6bd4\u3011\u751f\u4ea7\u7aef\u901a\u80c0\u6307\u6807\u3002PPI\u5411CPI\u4f20\u5bfc\u7ea63-6\u4e2a\u6708\uff0c\u53ef\u9884\u5224\u672a\u6765\u901a\u80c0\u8d70\u52bf\u3002PPI\u8d85\u9884\u671f\u4e0a\u884c\u589e\u52a0\u201c\u6ede\u80c0\u201d\u62c5\u5fe7\u3002",
    en: "US PPI YoY \u2014 production-side inflation metric. PPI transmits to CPI in ~3-6 months, useful for forecasting future inflation trajectory. Surging PPI raises stagflation concerns.",
  },
  us_industrial_production: {
    zh: "\u3010\u5de5\u4e1a\u4ea7\u51fa\u540c\u6bd4\u3011\u8861\u91cf\u7f8e\u56fd\u5236\u9020\u4e1a\u666f\u6c14\u5ea6\u3002\u4ea7\u51fa\u5f3a\u52b2=\u7ecf\u6d4e\u57fa\u672c\u9762\u5065\u5eb7\uff0c\u652f\u6491\u7f8e\u80a1\u76c8\u5229\u9884\u671f\uff1b\u5927\u5e45\u4e0b\u6ed1\u5219\u9884\u793a\u8870\u9000\u98ce\u9669\uff0c\u5227\u597d\u9632\u5fa1\u6027\u677f\u5757\u3002",
    en: "Industrial Production YoY \u2014 US manufacturing health gauge. Strong output = healthy fundamentals supporting S&P earnings expectations; sharp decline signals recession risk, favors defensive sectors.",
  },
  us_non_farm: {
    zh: "\u3010\u975e\u519c\u5c31\u4e1a\u4eba\u6570\u3011\u6bcf\u6708\u6700\u91cd\u8981\u7ecf\u6d4e\u6570\u636e\u4e4b\u4e00\u3002\u975e\u519c\u8d85\u9884\u671f\u2192\u5c31\u4e1a\u5e02\u573a\u706b\u70ed\u2192\u5de5\u8d44\u4e0a\u6da8\u2192\u901a\u80c0\u7c98\u6027\u2192 Fed\u7ef4\u6301\u9ad8\u5229\u7387\u66f4\u4e45 (Higher for Longer)\u3002",
    en: "Nonfarm Payrolls \u2014 among the most important monthly data points. NFP beat \u2192 hot labor market \u2192 wage inflation \u2192 sticky inflation \u2192 Fed keeps rates higher for longer.",
  },
  us_unemployment: {
    zh: "\u3010\u5931\u4e1a\u7387\u3011\u53cd\u5411\u6307\u6807\uff1a\u5931\u4e1a\u7387\u4f4e=\u52b3\u52a8\u529b\u5e02\u573a\u7d27\u5f20=\u5de5\u8d44\u901a\u80c0\u538b\u529b\u5927\u3002\u5931\u4e1a\u7387\u4f4e\u4e8e4%\u65f6Fed\u96be\u4ee5\u964d\u606f\uff1b\u7a81\u78344.5%\u4ee5\u4e0a\u5219\u8870\u9000\u6982\u7387\u663e\u8457\u5347\u9ad8\u3002",
    en: "Unemployment Rate \u2014 inverse signal: low UR = tight labor market = wage-inflation pressure. Below 4% makes it hard for Fed to cut; above 4.5% significantly raises recession probability.",
  },
};

// ── 基准值区间字典 ──
const INDICATOR_BENCHMARKS: Record<string, { zh: string; en: string }> = {
  cn_cpi_yoy:   { zh: "\u76ee\u6807\u533a\u95f4 2%~3%", en: "Target range 2%–3%" },
  cn_ppi_yoy:   { zh: "\u6b63\u5e38\u533a\u95f4 0%~3%", en: "Normal range 0%–3%" },
  cn_pmi:       { zh: "\u8367\u67af\u7ebf 50\uff08>52\u5f3a\u52b2\uff0c<48\u5f31\u52bf\uff09", en: "Boom/bust at 50 (>52 strong, <48 weak)" },
  cn_m2_yoy:    { zh: "\u5408\u7406\u533a\u95f4 8%~11%", en: "Healthy range 8%–11%" },
  cn_new_credit:{ zh: "\u5e73\u5747\u6bcf\u6708 ~1\u4e07\u4ebf", en: "Avg monthly ~1T CNY" },
  cn_social_financing: { zh: "\u589e\u901f 9%~11% \u4e3a\u5065\u5eb7", en: "Growth 9%–11% healthy" },
  cn_10y_yield: { zh: "\u5386\u53f2\u533a\u95f4 2.5%~3.5%", en: "Historical range 2.5%–3.5%" },
  cn_lpr_1y:    { zh: "\u5386\u53f2\u533a\u95f4 3.0%~3.5%", en: "Historical range 3.0%–3.5%" },
  cn_lpr_5y:    { zh: "\u5386\u53f2\u533a\u95f4 3.5%~4.5%", en: "Historical range 3.5%–4.5%" },
  cn_margin_sh:  { zh: "\u8d8b\u52bf\u91cd\u4e8e\u7edd\u5bf9\u503c\uff0c15000\u4ebf\u4e3a\u5206\u6c34\u5cad", en: "Trend > absolute; 1500B key level" },
  cn_margin_sz:  { zh: "\u8d8b\u52bf\u91cd\u4e8e\u7edd\u5bf9\u503c\uff0c10000\u4ebf\u4e3a\u5206\u6c34\u5cdb", en: "Trend > absolute; 1000B key level" },
  us_cpi_yoy:   { zh: "Fed\u76ee\u6807 2%\uff08>3% \u62c5\u5fe7\uff0c<1% \u901a\u7f29\u98ce\u9669\uff09", en: "Fed target 2% (>3% concern, <1% deflation)" },
  us_10y_yield:  { zh: "\u4e2d\u6027\u533a\u95f4 3.5%~4.5%", en: "Neutral zone 3.5%–4.5%" },
  us_core_cpi_mom:{ zh: "\u6b63\u5e38\u6708\u73af\u6bd4 0.2%~0.3%\uff08>0.4% \u8fc7\u70ed\uff09", en: "Normal MoM 0.2%–0.3% (>0.4% hot)" },
  us_ppi:       { zh: "\u6b63\u5e38\u533a\u95f4 0%~3%", en: "Normal range 0%–3%" },
  us_industrial_production: { zh: "\u6b63\u5e38\u589e\u957f 0%~3%", en: "Normal growth 0%–3%" },
  us_non_farm:  { zh: "\u5065\u5eb7\u589e\u4f9d 15\u4e07~25\u4e07/\u6708", en: "Healthy addition 150k–250k/mo" },
  us_unemployment:{ zh: "\u81ea\u7136\u5931\u4e1a\u7387 ~4%\uff08NAIRU\uff09", en: "Natural rate ~4% (NAIRU)" },
};

function indicatorName(row: MacroIndicator, locale: string) {
  const item = INDICATOR_LABELS[row.indicator_key];
  if (!item) return row.name;
  return locale === "en-US" ? item.en : item.zh;
}

function indicatorTip(key: string, locale: string): { text: string; bench: string } {
  const tip = INDICATOR_TIPS[key];
  const bench = INDICATOR_BENCHMARKS[key];
  return {
    text: tip ? (locale === "en-US" ? tip.en : tip.zh) : "",
    bench: bench ? (locale === "en-US" ? bench.en : bench.zh) : "",
  };
}

// 趋势箭头渲染
function trendArrow(delta: number | null | undefined) {
  if (delta == null || Number.isNaN(delta)) return null;
  const abs = Math.abs(delta);
  let display: string;
  if (abs >= 100000000) {
    display = (delta / 100000000).toFixed(2) + "\u4ebf";
  } else if (abs >= 10000) {
    display = (delta / 10000).toFixed(2) + "\u4e07";
  } else {
    display = delta.toFixed(2);
  }
  if (delta > 0) return <span style={{ color: "#b42318", marginLeft: 4 }}>&#8593;{display}</span>;
  if (delta < 0) return <span style={{ color: "#0f766e", marginLeft: 4 }}>&#8595;{display}</span>;
  return <span style={{ color: "#6b7280", marginLeft: 4 }}>=0</span>;
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
      render: (value) => {
        const catColor: Record<string, string> = {
          growth: "#0f766e", inflation: "#b45309", liquidity: "#2563eb",
          credit: "#7c3aed", risk: "#dc2626",
        };
        return <Tag color={catColor[value] ?? "default"} style={{ fontWeight: 600, fontSize: 11 }}>{labels[value as keyof typeof labels] ?? value}</Tag>;
      },
      width: 110,
    },
    {
      title: labels.title,
      dataIndex: "name",
      width: 240,
      render: (_, row) => {
        const { text: tipText, bench } = indicatorTip(row.indicator_key, ctx.locale);
        const name = indicatorName(row, ctx.locale);
        if (tipText) {
          return (
            <Tooltip
              title={<div className="macro__tip-content">
                <div className="macro__tip-title">{name}</div>
                <div className="macro__tip-body">{tipText}</div>
                {bench && (
                  <div className="macro__tip-bench">
                    <span className="macro__bench-label">
                      {ctx.locale === "en-US" ? "Benchmark" : "\u57fa\u51c6\u503c"}
                    </span>
                    <span className="macro__bench-value">{bench}</span>
                  </div>
                )}
              </div>}
              classNames={{ root: "macro-tip-overlay" }}
              placement="topLeft"
              arrow={{ pointAtCenter: true }}
            >
              <span className="macro__indicator-name">
                <span className="macro__info-icon">i</span>
                {name}
              </span>
            </Tooltip>
          );
        }
        return <span>{name}</span>;
      },
    },
    { title: labels.period, dataIndex: "period", width: 120 },
    {
      title: labels.value,
      dataIndex: "value",
      render: (_, row) => (
        <span>
          {fmt(row.value, row.unit, ctx.locale)}
          {trendArrow(row.delta)}
        </span>
      ),
      align: "right",
      width: 130,
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
      render: (value, _row) => (
        <span>
          {fmt(value, _row?.unit ?? null, ctx.locale)}
          {trendArrow(value)}
        </span>
      ),
      align: "right",
      width: 100,
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

          {/* 市场脉搏摘要 */}
          <Card className="macro-pulse-card" title={ctx.locale === "en-US" ? "Market Pulse" : "\u5e02\u573a\u8109\u640f"}>
            <div className="macro__pulse-grid">
              {dimensionRows.map((dim) => {
                const dimIcon: Record<string, string> = {
                  growth: "\ud83d\udcca", inflation: "\ud83d\udd25", liquidity: "\ud83d\udcb0",
                  credit: "\ud83d\udcc8", risk: "\u26a0\ufe0f",
                };
                const val = Math.max(0, Math.min(100, 50 + dim.value * 2.5));
                const level = val >= 68 ? "good" : val <= 42 ? "bad" : "neutral";
                return (
                  <div key={dim.key} className={`macro__pulse-item macro__pulse--${level}`}>
                    <span className="macro__pulse-icon">{dimIcon[dim.key] ?? "\u2022"}</span>
                    <div className="macro__pulse-info">
                      <strong>{labels[dim.key as keyof typeof labels] ?? dim.key}</strong>
                      <div className="macro__pulse-bar">
                        <div className="macro__pulse-fill" style={{ width: `${val}%` }} />
                      </div>
                      <span className="macro__pulse-score">{val.toFixed(0)}</span>
                    </div>
                  </div>
                );
              })}
            </div>
            {snapshot.stance && (
              <div className={`macro__stance-banner macro__stance--${snapshot.stance}`}>
                <span className="macro__stance-label">
                  {snapshot.stance === "risk_on" ? (ctx.locale === "en-US" ? "Market Mode: Risk-On" : "\u5e02\u573a\u6a21\u5f0f\uff1a\u79ef\u6781")
                   : snapshot.stance === "cautious" ? (ctx.locale === "en-US" ? "Market Mode: Cautious" : "\u5e02\u573a\u6a21\u5f0f\uff1a\u8c28\u614e")
                   : snapshot.stance === "defensive" ? (ctx.locale === "en-US" ? "Market Mode: Defensive" : "\u5e02\u573a\u6a21\u5f0f\uff1a\u9632\u5b88")
                   : snapshot.stance}
                </span>
                <span className="macro__stance-hint">
                  {snapshot.stance === "risk_on"
                    ? (ctx.locale === "en-US" ? "Favor cyclical / growth sectors; watch for overheating signals." : "\u504f\u597d\u5468\u671f/\u6210\u957f\u677f\u5757\uff1b\u6ce8\u610f\u8fc7\u70ed\u4fe1\u53f7\u3002")
                    : snapshot.stance === "cautious"
                      ? (ctx.locale === "en-US" ? "Reduce leverage; focus on quality & cash flow." : "\u964d\u4f4e\u6746\u6746\uff1c\u805a\u7126\u54c1\u8d28\u73b0\u91d1\u6d41\u3002")
                      : snapshot.stance === "defensive"
                        ? (ctx.locale === "en-US" ? "Prioritize defensive sectors: utilities, staples, healthcare." : "\u4f18\u5148\u9632\u5fa1\u6027\u677f\u5757\uff1a\u516c\u7528\u4e8b\u4e1a\u3001\u5fc5\u9801\u6d88\u8d39\u3001\u533b\u7597\u3002")
                        : ""}
                </span>
              </div>
            )}
          </Card>

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
        destroyOnHidden
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
