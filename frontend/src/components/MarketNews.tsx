import { useEffect, useState, useMemo, useCallback } from "react";
import { Alert, Button, Card, Empty, Select, Space, Tag, Typography, Tooltip } from "antd";
import { ReloadOutlined, ThunderboltOutlined, FilterOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import type { MarketEvent, MarketEventListResponse } from "../types";
import {
  t,
} from "../i18n";

const { Paragraph, Text } = Typography;

// ── 影响范围配置 ──
const SCOPE_CONFIG: Record<string, { label: string; enLabel: string; color: string; icon: string }> = {
  macro_policy:    { label: "宏观政策", enLabel: "Macro Policy",  color: "#b42318", icon: "\ud83d\udcca" },
  sector_dynamics:{ label: "行业动态", enLabel: "Sector Dynamics", color: "#d97706", icon: "\ud83d\udd25" },
  international:  { label: "国际形势", enLabel: "International",  color: "#2563eb", icon: "\ud83c\udf0d" },
  breaking:        { label: "突发事件", enLabel: "Breaking",      color: "#7c3aed", icon: "\u26a0\ufe0f" },
  fund_flow:       { label: "资金流向", enLabel: "Fund Flow",     color: "#0891b2", icon: "\ud83d\udcb0" },
  sentiment:       { label: "市场情绪", enLabel: "Sentiment",      color: "#6b7280", icon: "\ud83e\ude7a" },
  other:           { label: "其他",     enLabel: "Other",         color: "#9ca3af", icon: "\u2022" },
};

// ── 级别配置 ──
const LEVEL_CONFIG: Record<number, { label: string; enLabel: string; color: string; bgColor: string; borderStyle: string }> = {
  5: { label: "紧急", enLabel: "Urgent",     color: "#b42318", bgColor: "rgba(180,35,24,0.06)",  borderStyle: "solid 3px #b42318" },
  4: { label: "重要", enLabel: "Important",  color: "#d97706", bgColor: "rgba(217,119,6,0.06)",   borderStyle: "solid 3px #d97706" },
  3: { label: "关注", enLabel: "Watch",      color: "#2563eb", bgColor: "rgba(37,99,235,0.05)",   borderStyle: "solid 2px #2563eb" },
  2: { label: "一般", enLabel: "Normal",     color: "#6b7280", bgColor: "rgba(107,114,128,0.04)",  borderStyle: "solid 1px #d1d5db" },
  1: { label: "参考", enLabel: "Reference",  color: "#9ca3af", bgColor: "rgba(156,163,175,0.03)",  borderStyle: "solid 1px #e5e7eb" },
};

// ── 权威性评分（按来源） ──
const SOURCE_CREDIBILITY: Record<string, number> = {
  cctv: 5, "baidu-report": 4, baidu: 3, manual: 4,
};
const CREDIBILITY_LABELS: Record<number, { zh: string; en: string; stars: string }> = {
  5: { zh: "官方权威", en: "Official Authority", stars: "\u2605\u2605\u2605\u2605\u2605" },
  4: { zh: "主流媒体", en: "Mainstream Media",    stars: "\u2605\u2605\u2605\u2605\u2606" },
  3: { zh: "财经媒体", en: "Financial Media",     stars: "\u2605\u2605\u2605\u2606\u2606" },
  2: { zh: "普通来源", en: "General Source",      stars: "\u2605\u2605\u2606\u2606\u2606" },
  1: { zh: "未验证",   en: "Unverified",           stars: "\u2605\u2606\u2606\u2606\u2606" },
};

// ── 影响评价规则引擎 ──
function assessImpact(event: MarketEvent, locale: string) {
  const isZh = locale === "zh-CN";
  const level = event.importance_level;
  const sentiment = event.sentiment;
  const scope = event.impact_scope;

  // 方向
  const dirKey = sentiment === "positive" ? "impactBullish"
    : sentiment === "negative" ? "impactBearish"
    : "impactNeutral";
  const direction = t(dirKey);

  // 程度
  let degree: string;
  if (level >= 5) degree = t("impactSevere");
  else if (level >= 4) degree = t("impactSignificant");
  else if (level >= 3) degree = t("impactModerate");
  else degree = t("impactMild");

  // 持续时间
  let duration: string;
  if (scope === "breaking") duration = t("impactDurationShort");
  else if (scope === "macro_policy") duration = t("impactDurationLong");
  else if (scope === "fund_flow" || scope === "sentiment") duration = t("impactDurationShort");
  else duration = t("impactDurationMedium");

  // 操作建议（组合生成）
  const suggestions: string[] = [];
  if (sentiment === "positive") {
    if (scope === "macro_policy") suggestions.push(isZh ? "利好大盘，关注金融/地产板块" : "Bullish for broad market; watch finance & property sectors");
    else if (scope === "sector_dynamics") suggestions.push(isZh ? "相关板块有望活跃，注意追高风险" : "Relevant sectors may be active; watch for chasing risk");
    else if (scope === "fund_flow") suggestions.push(isZh ? "资金面改善，风险偏好上升" : "Fund flow improving; risk appetite rising");
    else suggestions.push(isZh ? "偏多情绪，但需结合技术面确认" : "Bullish bias but confirm with technicals");
  } else if (sentiment === "negative") {
    if (scope === "breaking") suggestions.push(isZh ? "规避系统性风险，降低仓位" : "Avoid systemic risk; reduce positions");
    else if (scope === "macro_policy") suggestions.push(isZh ? "政策收紧预期，控制杠杆" : "Tightening expected; control leverage");
    else if (scope === "international") suggestions.push(isZh ? "外部冲击，防御性板块相对安全" : "External shock; defensive sectors safer");
    else suggestions.push(isZh ? "谨慎观望，等待企稳信号" : "Cautious wait; look for stabilization signals");
  } else {
    suggestions.push(isZh ? "中性消息，维持现有策略不变" : "Neutral news; maintain current strategy");
  }

  return { direction, degree, duration, suggestion: suggestions[0] ?? "" };
}

// ── 关键词提取（简单分词） ──
function extractKeywords(title: string): string[] {
  const stopWords = new Set(["的", "了", "在", "是", "将", "与", "和", "对", "为", "从", "到", "中", "上", "下", "等", "及", "或", "但", "而", "也", "已", "这", "那", "有", "无", "不", "会", "可", "能", "把", "被", "让", "给", "向", "于", "以", "其"]);
  const candidates = title.match(/[\u4e00-\u9fa5]{2,6}/g) || [];
  return [...new Set(candidates.filter((w) => !stopWords.has(w)))].slice(0, 8);
}

// ── 格式化时间 ──
function fmtDate(d: string | null, locale: string): string {
  if (!d) return "-";
  try {
    return new Date(d).toLocaleDateString(locale === "zh-CN" ? "zh-CN" : "en-US", {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch { return d.slice(0, 16); }
}

/* ════════════════════════════════════════════════════════════
   MarketNews 主组件
   ════════════════════════════════════════════════════════════ */

export default function MarketNews() {
  const ctx = useApp();
  const [data, setData] = useState<MarketEventListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [collecting, setCollecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 筛选状态
  const [filterScope, setFilterScope] = useState<string>("");
  const [filterLevelMin, setFilterLevelMin] = useState<number>(1);
  const [filterSentiment, setFilterSentiment] = useState<string>("");
  const [sortBy, setSortBy] = useState<"published_at" | "importance_level">("published_at");
  const [showFilter, setShowFilter] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  // 概念提及统计
  const [keywordStats, setKeywordStats] = useState<Record<string, number>>({});

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getMarketEvents({
        impact_scope: filterScope || undefined,
        importance_level_min: filterLevelMin,
        sentiment: filterSentiment || undefined,
        sort_by: sortBy,
        limit: 80,
      });
      setData(result);
      // 计算关键词频率
      const kwCount: Record<string, number> = {};
      (result.events as MarketEvent[]).forEach((e: MarketEvent) => {
        extractKeywords(e.title).forEach((kw) => { kwCount[kw] = (kwCount[kw] || 0) + 1; });
      });
      setKeywordStats(kwCount);
    } catch (err: any) {
      setError(err.message || "Failed to load market news");
    } finally {
      setLoading(false);
    }
  }, [filterScope, filterLevelMin, filterSentiment, sortBy]);

  useEffect(() => { loadData(); }, [loadData]);

  const handleCollect = async () => {
    setCollecting(true);
    try {
      await api.collectMarketEvents({ days: 7 });
      loadData(); // 重新加载
    } catch (err: any) {
      setError(err.message || "Collection failed");
    } finally {
      setCollecting(false);
    }
  };

  // 维度统计数据
  const scopeStats = useMemo(() => data?.by_scope ?? {}, [data]);
  const levelStats = useMemo(() => data?.by_level ?? {}, [data]);
  const events = useMemo(() => data?.events ?? [], [data]);

  // 排序后的消息
  const sortedEvents = useMemo(() => {
    const list = [...events];
    if (sortBy === "importance_level") {
      list.sort((a, b) => b.importance_level - a.importance_level);
    } else {
      list.sort((a, b) => {
        const da = a.published_at ? new Date(a.published_at).getTime() : 0;
        const db = b.published_at ? new Date(b.published_at).getTime() : 0;
        return db - da;
      });
    }
    return list;
  }, [events, sortBy]);

  return (
    <div className="mn-page">
      {/* 头部 */}
      <section className="mn-hero">
        <div>
          <p className="panel-kicker">{t("marketNewsTitle")}</p>
          <h1>{t("marketNewsTitle")}</h1>
          <p className="mn-subtitle">{t("marketNewsSubtitle")}</p>
        </div>
        <Space wrap>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={loadData}>
            {t("marketNewsRefresh")}
          </Button>
          <Button type="primary" icon={<ThunderboltOutlined />} loading={collecting} onClick={handleCollect}>
            {collecting ? t("marketNewsCollecting") : t("marketNewsCollect")}
          </Button>
          <Button
            icon={<FilterOutlined />}
            onClick={() => setShowFilter(!showFilter)}
            className={showFilter ? "mn-filter-active" : ""}
          >
            {t("marketNewsFilter")}
          </Button>
          <Select
            value={sortBy}
            onChange={(v) => setSortBy(v as any)}
            size="middle"
            style={{ width: 140 }}
            options={[
              { value: "published_at", label: t("marketNewsSortTime") },
              { value: "importance_level", label: t("marketNewsSortLevel") },
            ]}
          />
        </Space>
      </section>

      {error && <Alert type="error" message={error} showIcon className="mn-alert" />}

      {/* 筛选面板 */}
      {showFilter && (
        <Card className="mn-filter-panel" size="small">
          <div className="mn-filter-grid">
            <div className="mn-filter-item">
              <label>{t("scopeMacroPolicy").slice(0, 2)}{ctx.locale === "en-US" ? "Scope" : "范围"}</label>
              <Select
                value={filterScope || "__all__"}
                onChange={(v) => setFilterScope(v === "__all__" ? "" : v)}
                style={{ width: "100%" }}
                options={[
                  { value: "__all__", label: t("marketNewsScopeAll") },
                  ...Object.entries(SCOPE_CONFIG).map(([k, v]) => ({ value: k, label: ctx.locale === "zh-CN" ? v.label : v.enLabel })),
                ]}
              />
            </div>
            <div className="mn-filter-item">
              <label>{t("newsLevel5").slice(0, 1)}{ctx.locale === "en-US" ? "Min Level" : "最低级别"}</label>
              <Select
                value={filterLevelMin}
                onChange={(v) => setFilterLevelMin(v)}
                style={{ width: "100%" }}
                options={[
                  { value: 1, label: `${t("marketNewsLevelAll")} (1-5)` },
                  { value: 3, label: `${t("newsLevel3")}+ (3-5)` },
                  { value: 4, label: `${t("newsLevel4")}+ (4-5)` },
                  { value: 5, label: t("newsLevel5") },
                ]}
              />
            </div>
            <div className="mn-filter-item">
              <label>{t("impactDirection").slice(0, 2)}</label>
              <Select
                value={filterSentiment || "__all__"}
                onChange={(v) => setFilterSentiment(v === "__all__" ? "" : v)}
                style={{ width: "100%" }}
                options={[
                  { value: "__all__", label: t("marketNewsSentimentAll") },
                  { value: "positive", label: t("impactBullish") },
                  { value: "negative", label: t("impactBearish") },
                  { value: "neutral", label: t("impactNeutral") },
                ]}
              />
            </div>
          </div>
        </Card>
      )}

      {!data || events.length === 0 ? (
        <Card className="mn-empty">
          <Empty description={t("marketNewsNoData")}>
            <Button type="primary" loading={collecting} onClick={handleCollect}>{t("marketNewsCollect")}</Button>
          </Empty>
        </Card>
      ) : (
        <>
          {/* 维度统计卡片 */}
          <div className="mn-scope-cards">
            {Object.entries(SCOPE_CONFIG).map(([key, cfg]) => {
              const count = scopeStats[key] || 0;
              return (
                <div key={key} className={`mn-scope-card mn-scope--${key}`} onClick={() => setFilterScope(filterScope === key ? "" : key)}>
                  <span className="mn-scope-icon">{cfg.icon}</span>
                  <div className="mn-scope-info">
                    <strong>{ctx.locale === "zh-CN" ? cfg.label : cfg.enLabel}</strong>
                    <span className="mn-scope-count">{count}</span>
                  </div>
                </div>
              );
            })}
          </div>

          {/* 消息列表 */}
          <div className="mn-news-list">
            {sortedEvents.map((event) => {
              const lvlCfg = LEVEL_CONFIG[event.importance_level] || LEVEL_CONFIG[1];
              const scopeCfg = SCOPE_CONFIG[event.impact_scope] || SCOPE_CONFIG.other;
              const assessment = assessImpact(event, ctx.locale);
              const credibility = SOURCE_CREDIBILITY[event.source] ?? 2;
              const credLbl = CREDIBILITY_LABELS[credibility] ?? CREDIBILITY_LABELS[2];
              const keywords = extractKeywords(event.title);
              const isExpanded = expandedId === event.id;

              return (
                <Card
                  key={event.id}
                  className={`mn-news-card mn-level-${event.importance_level}`}
                  style={{ borderLeft: lvlCfg.borderStyle }}
                  onClick={() => setExpandedId(isExpanded ? null : event.id)}
                >
                  <div className="mn-card-header">
                    <Space size={6}>
                      <Tag color={lvlCfg.color} style={{ fontWeight: 700, fontSize: 11 }}>
                        {ctx.locale === "zh-CN" ? lvlCfg.label : lvlCfg.enLabel}
                      </Tag>
                      <Tag color={scopeCfg.color} style={{ opacity: 0.75, fontSize: 10 }}>
                        {scopeCfg.icon} {ctx.locale === "zh-CN" ? scopeCfg.label : scopeCfg.enLabel}
                      </Tag>
                    </Space>
                    <Space size={4}>
                      <Tooltip title={`${t("credibilityLabel")}: ${ctx.locale === "zh-CN" ? credLbl.zh : credLbl.en}`}>
                        <span className="mn-credibility">{credLbl.stars}</span>
                      </Tooltip>
                      <Text type="secondary" style={{ fontSize: 11 }}>{fmtDate(event.published_at, ctx.locale)}</Text>
                    </Space>
                  </div>

                  <h3 className={`mn-title mn-title-lvl${event.importance_level}`}>{event.title}</h3>

                  {event.summary && !isExpanded && (
                    <p className="mn-summary">{event.summary.length > 120 ? event.summary.slice(0, 120) + "..." : event.summary}</p>
                  )}

                  {/* 展开详情 */}
                  {isExpanded && (
                    <div className="mn-detail-panel">
                      {event.summary && <Paragraph className="mn-detail-summary">{event.summary}</Paragraph>}

                      {/* 影响评价 */}
                      <div className="mn-assessment">
                        <div className="mn-assess-row">
                          <span className="mn-assess-label">{t("impactDirection")}</span>
                          <Tag
                            color={event.sentiment === "positive" ? "green" : event.sentiment === "negative" ? "red" : "default"}
                            style={{ fontWeight: 600 }}
                          >
                            {assessment.direction}
                          </Tag>
                          <span className="mn-assess-label">{t("impactDegree")}</span>
                          <Tag color={event.importance_level >= 4 ? "red" : event.importance_level >= 3 ? "orange" : "blue"}>
                            {assessment.degree}
                          </Tag>
                        </div>
                        <div className="mn-assess-row">
                          <span className="mn-assess-label">{t("impactDuration")}</span>
                          <Text strong>{assessment.duration}</Text>
                          <span className="mn-assess-divider" />
                          <span className="mn-assess-label">{t("impactSuggestion")}</span>
                          <Text type="secondary" style={{ fontSize: 12 }}>{assessment.suggestion}</Text>
                        </div>
                      </div>

                      {/* 关键词标签 */}
                      {keywords.length > 0 && (
                        <div className="mn-keywords">
                          <span className="mn-kw-label">{t("conceptLabel")}</span>
                          {keywords.map((kw) => (
                            <Tooltip key={kw} title={`${t("conceptMentionCount")}: ${keywordStats[kw] ?? 1}`}>
                              <Tag className="mn-kw-tag">
                                {kw}
                                <span className="mn-kw-count">{keywordStats[kw] ?? 1}</span>
                              </Tag>
                            </Tooltip>
                          ))}
                        </div>
                      )}

                      {/* 来源信息 */}
                      <div className="mn-meta-footer">
                        <Text type="secondary" style={{ fontSize: 11 }}>
                          {t(`source${event.source.charAt(0).toUpperCase() + event.source.slice(1)}` as any) || event.source}
                          {event.source_url && (
                            <a href={event.source_url} target="_blank" rel="noopener noreferrer" className="mn-source-link" onClick={(e) => e.stopPropagation()}>
                              [{ctx.locale === "zh-CN" ? "原文" : "Source"}]
                            </a>
                          )}
                        </Text>
                        {event.affected_sectors && (
                          <Tag style={{ fontSize: 10, marginLeft: 8 }}>{event.affected_sectors}</Tag>
                        )}
                      </div>
                    </div>
                  )}
                </Card>
              );
            })}
          </div>

          {/* 底部统计 */}
          <div className="mn-footer-stats">
            <Text type="secondary">
              {ctx.locale === "zh-CN"
                ? `共 ${data.total} 条消息，显示 ${sortedEvents.length} 条`
                : `${data.total} total events, showing ${sortedEvents.length}`}
            </Text>
          </div>
        </>
      )}
    </div>
  );
}
