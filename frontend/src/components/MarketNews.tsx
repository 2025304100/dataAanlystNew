import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Empty, Input, Pagination, Select, Space, Tag, Tooltip, Typography } from "antd";
import { CalendarOutlined, DownloadOutlined, FilterOutlined, ReloadOutlined, StarFilled, StarOutlined, TagsOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import { t, template } from "../i18n";
import type { MarketEvent, MarketEventListResponse, MarketEventHotspotsResponse, MarketEventCollectRun, MarketEventQuery } from "../types";
import { aggregateMarketSectors, CONCEPT_DAYS, fetchCompleteMarketEvents, marketEventsCsv, marketTime, storedSectorIds } from "../utils/marketNews";
import ThemeManagementModal from "./ThemeManagementModal";
import { signalLabels } from "../i18n/marketSignals";

const MarketSignalCenter = lazy(() => import("./MarketSignalCenter"));

const { Paragraph, Text } = Typography;

// P0-2：全部已实现采集源（与后端 ALL_COLLECT_SOURCES 保持一致）
const ALL_COLLECT_SOURCES = ["eastmoney-global", "caixin", "cctv", "baidu", "baidu-report", "futures-shmet"];
// P0-3：样本不足阈值——板块方向计数低于该值时不具统计意义，显示"样本不足"而非满格误导
const MIN_SAMPLE_COUNT = 3;
// P1-6：收藏标记起步用 localStorage（不加表）；id 集合序列化存储
const FAVORITES_KEY = "mn_favorites";
const PAGE_SIZE = 50;

function loadFavorites(): Set<number> {
  try {
    const raw = JSON.parse(localStorage.getItem(FAVORITES_KEY) || "[]");
    return new Set(Array.isArray(raw) ? raw.filter((v) => typeof v === "number") : []);
  } catch {
    return new Set();
  }
}

// ── 影响范围配置（8类）──
const SCOPE_CONFIG: Record<string, { color: string; icon: string }> = {
  macro_policy:     { color: "#b42318", icon: "📊" },
  commodity_futures:{ color: "#059669", icon: "🧪" },
  sector_dynamics:  { color: "#d97706", icon: "🔥" },
  international:    { color: "#2563eb", icon: "🌍" },
  breaking:         { color: "#7c3aed", icon: "⚠️" },
  fund_flow:        { color: "#0891b2", icon: "💰" },
  sentiment:        { color: "#6b7280", icon: "🧠" },
  other:            { color: "#64748b", icon: "•" },
};
const SCOPE_KEYS = Object.keys(SCOPE_CONFIG);

// ── 级别配置（5级）──
const LEVEL_CONFIG: Record<number, { color: string; bgColor: string; borderStyle: string }> = {
  5: { color: "#b42318", bgColor: "rgba(180,35,24,0.06)",  borderStyle: "solid 3px #b42318" },
  4: { color: "#d97706", bgColor: "rgba(217,119,6,0.06)",   borderStyle: "solid 3px #d97706" },
  3: { color: "#2563eb", bgColor: "rgba(37,99,235,0.05)",   borderStyle: "solid 2px #2563eb" },
  2: { color: "#6b7280", bgColor: "rgba(107,114,128,0.04)",  borderStyle: "solid 1px #d1d5db" },
  1: { color: "#9ca3af", bgColor: "rgba(156,163,175,0.03)",  borderStyle: "solid 1px #e5e7eb" },
};

// ── 权威性评分 ──
const SOURCE_CREDIBILITY: Record<string, number> = {
  cctv: 5, "baidu-report": 4, baidu: 3,
  "eastmoney-global": 4, caixin: 4, "futures-shmet": 3, manual: 4,
};
const CREDIBILITY_LABELS: Record<number, { stars: string }> = {
  5: { stars: "\u2605\u2605\u2605\u2605\u2605" },
  4: { stars: "\u2605\u2605\u2605\u2605\u2606" },
  3: { stars: "\u2605\u2605\u2605\u2606\u2606" },
  2: { stars: "\u2605\u2605\u2606\u2606\u2606" },
  1: { stars: "\u2605\u2606\u2606\u2606\u2606" },
};

/* ════════════════════════════════════════════════════════════
   ★ 核心改进：行业板块概念知识库
   用结构化知识库替代机械分词，将新闻映射到有投资价值的板块

   每个板块包含：
   - id: 唯一标识
   - keywords: 匹配关键词列表（标题中包含即命中）
   - color: 展示颜色
   - icon: emoji 图标
   ════════════════════════════════════════════════════════════ */
interface SectorConcept {
  id: string;
  keywords: string[];
  color: string;
  icon: string;
}

const SECTOR_CONCEPTS: SectorConcept[] = [
  // ===== 科技/AI =====
  { id: "ai_compute",       keywords: ["人工智能","AI","大模型","GPT","ChatGPT","GPU","算力","数据中心","云计算","英伟达","NVIDIA","芯片","半导体","台积电","英特尔","AMD","微软","OpenAI","深度学习","机器学习","推理","训练","H100","A100","服务器","超算"], color: "#7c3aed", icon: "🤖" },
  { id: "semiconductor",    keywords: ["半导体","芯片","晶圆","光刻","存储器","DRAM","NAND","闪存","集成电路","IC设计","封测","中芯","华虹","三星电子","SK海力士","美光","高通","博通","联发科"], color: "#2563eb", icon: "💾" },
  { id: "software_saas",    keywords: ["软件","SaaS","云服务","办公软件","操作系统","数据库","ERP","CRM","Oracle","Salesforce","Adobe","用友","金山","金蝶","微软","谷歌"], color: "#0891b2", icon: "💿" },
  { id: "consumer_electronics", keywords: ["消费电子","手机","iPhone","华为","小米","苹果","平板","可穿戴","AR","VR","元宇宙","Meta","耳机","智能家居","IoT"], color: "#db2777", icon: "📱" },
  { id: "space_satellite",  keywords: ["航天","卫星","SpaceX","火箭","太空","NASA","星链","北斗","GPS","马斯克","贝索斯","商业航天","低轨卫星","发射","轨道"], color: "#4338ca", icon: "🚀" },

  // ===== 新能源 =====
  { id: "new_energy_vehicle",keywords: ["新能源车","电动汽车","EV","特斯拉","Tesla","比亚迪","蔚来","理想","小鹏","问界","宁德时代","CATL","动力电池","锂电","充电桩","自动驾驶","智驾","固态电池","4680","刀片电池"], color: "#16a34a", icon: "🚗" },
  { id: "solar_pv",         keywords: ["光伏","太阳能","硅料","硅片","组件","逆变器","隆基绿能","通威股份","阳光电源","天合光能","晶科能源","HJT","TOPCon","钙钛矿"], color: "#eab308", icon: "☀️" },
  { id: "wind_power",       keywords: ["风电","风力发电","风机","叶片","海上风电","陆上风电","金风科技","明阳智能","运达股份","三峡能源"], color: "#0ea5e9", icon: "💨" },
  { id: "energy_storage",   keywords: ["储能","钠离子","液流电池","压缩空气","抽水蓄能","电网侧储能","户用储能","派能科技","鹏辉能源"], color: "#f59e0b", icon: "🔋" },

  // ===== 贵金属/大宗商品 =====
  { id: "precious_metals",  keywords: ["黄金","白银","金价","银价","避险","COMEX","伦敦金","现货黄金","ETF黄金","央行购金","珠宝首饰","铂金","钯金","紫金矿业","山东黄金"], color: "#d97706", icon: "🥇" },
  { id: "crude_oil_energy", keywords: ["原油","石油","OPEC","EIA","油价","WTI","布伦特","天然气","页岩油","沙特","俄罗斯石油","炼油","成品油","汽油","柴油","中石油","中石化","中海油","埃克森美孚","雪佛龙"], color: "#dc2626", icon: "🛢️" },
  { id: "steel_metals",     keywords: ["钢铁","铁矿石","螺纹钢","热卷板","铜","铝","锌","镍","锡","铅","LME","SHMET","上海金属","宝钢","河钢","五矿","江西铜业","紫金矿业","洛阳钼业"], color: "#78716c", icon: "⛏️" },
  { id: "agriculture",      keywords: ["农产品","大豆","玉米","小麦","棉花","糖","橡胶","棕榈油","豆粕","生猪","猪肉","粮食","化肥","农药","种业","北大荒","中粮"], color: "#65a30d", icon: "🌾" },

  // ===== 金融地产 =====
  { id: "real_estate",      keywords: ["房地产","楼市","房价","恒大","碧桂园","万科","保利","龙湖","融创","房贷","LPR","土地出让","二手房","新房销售","物业","保障房","城中村"], color: "#b45309", icon: "🏠" },
  { id: "banking_finance",  keywords: ["银行","利率","降息","加息","存款","贷款","信贷","M2","MLF","SLF","逆回购","LPR","央行","美联储","欧央行","日央行","四大行","招行","平安银行","券商","保险"], color: "#0369a1", icon: "🏦" },
  { id: "insurance",        keywords: ["保险","寿险","财险","中国人寿","中国平安","太保","新华保险","保费","赔付率","偿付能力","养老险","健康险"], color: "#059669", icon: "🛡️" },

  // ===== 医药生物 =====
  { id: "pharma_biotech",   keywords: ["医药","制药","创新药","仿制药","CRO","CDMO","疫苗","中药","恒瑞医药","药明康德","迈瑞医疗","爱尔眼科","通策医疗","CXO","ADC","GLP","减肥药"], color: "#be185d", icon: "💊" },
  { id: "medical_devices",  keywords: ["医疗器械","影像设备","高值耗材","IVD","体外诊断","联影医疗","迈瑞","乐普医疗","微创医疗","骨科植入","心血管支架"], color: "#9333ea", icon: "🩺" },

  // ===== 消费 =====
  { id: "food_beverage",    keywords: ["白酒","啤酒","乳制品","调味品","预制菜","零食","茅台","五粮液","伊利","海天味业","涪陵榨菜","安井食品","餐饮","海底捞","星巴克","可口可乐","百事"], color: "#c2410c", icon: "🍷" },
  { id: "retail_ecommerce", keywords: ["电商","零售","直播带货","拼多多","京东","阿里巴巴","淘宝","天猫","亚马逊","抖音电商","美团","免税","中国中免","永辉超市","苏宁"], color: "#ea580c", icon: "🛒" },
  { id: "tourism_hotel",    keywords: ["旅游","酒店","航空","机场","景区","免税店","携程","同程","中国中免","锦江酒店","首旅酒店","迪士尼","主题公园","邮轮","出境游"], color: "#0891b2", icon: "✈️" },
  { id: "auto_chain",       keywords: ["汽车","整车","零部件","特斯拉供应链","比亚迪产业链","造车新势力","自动驾驶","激光雷达","毫米波雷达","线控底盘","一体化压铸","拓普集团","德赛西威","华域汽车"], color: "#4f46e5", icon: "🚙" },

  // ===== 军工/高端制造 =====
  { id: "defense_military", keywords: ["军工","国防","导弹","战斗机","航母","无人机","军机","舰船","潜艇","中航沈飞","航发动力","中国船舶","洪都航空","北方导航","信息化装备","C919","大飞机"], color: "#1e3a5f", icon: "✈️" },
  { id: "robotics",         keywords: ["机器人","人形机器人","工业机器人","协作机器人","伺服电机","减速器","传感器","特斯拉Optimus","优必选","埃斯顿","汇川技术","具身智能"], color: "#6366f1", icon: "🦾" },
  { id: "advanced_mfg",     keywords: ["数控机床","工业母机","激光设备","3D打印","增材制造","工程机械","三一重工","中联重科","徐工机械","格力电器","美的集团","海尔智家"], color: "#374151", icon: "🏭" },

  // ===== 宏观/政策 =====
  { id: "macro_policy_cn",  keywords: ["国务院","发改委","财政部","央行","证监会","政治局","两会","GDP目标","财政政策","货币政策","减税降费","专项债","赤字率","社融","M2","PMI","CPI","PPI","稳增长","扩内需","促消费"], color: "#b91c1c", icon: "🏛️" },
  { id: "macro_intl_geo",   keywords: ["地缘政治","贸易战","关税","制裁","美联储","欧洲央行","日本央行","G7","G20","IMF","世界银行","WTO","RCEP","一带一路","中美关系","中俄关系","中欧关系","俄乌冲突","巴以冲突","红海危机"], color: "#991b1b", icon: "🌐" },
  { id: "crypto_web3",      keywords: ["比特币","以太坊","BTC","ETH","区块链","Web3","DeFi","NFT","稳定币","SEC","币安","Coinbase","挖矿","矿机","ETF","现货比特币","监管"], color: "#f7931a", icon: "₿" },

  // ===== 其他 =====
  { id: "telecom_5g",       keywords: ["5G","6G","通信","运营商","中国移动","中国联通","中国电信","光纤","基站","光模块","中兴通讯","烽火通信","华为","网络建设","算力网络"], color: "#0284c7", icon: "📡" },
  { id: "media_entertainment", keywords: ["影视","游戏","短视频","抖音","快手","腾讯游戏","网易游戏","电影票房","院线","芒果TV","爱奇艺","腾讯视频","字节跳动","广告营销"], color: "#e11d48", icon: "🎬" },
  { id: "environment_carbon", keywords: ["碳中和","碳交易","环保","ESG","绿色金融","碳达峰","新能源配储","CCUS","碳排放权","清洁能源转型","光伏治沙","林业碳汇"], color: "#059669", icon: "🌿" },
];

// 预构建关键词→板块的倒排索引（性能优化）
const KEYWORD_TO_SECTOR: Map<string, SectorConcept> = new Map();
SECTOR_CONCEPTS.forEach((sector) => {
  sector.keywords.forEach((kw) => {
    if (!KEYWORD_TO_SECTOR.has(kw)) KEYWORD_TO_SECTOR.set(kw, sector);
  });
});

/** 将新闻标题+摘要映射到相关板块（最多返回3个） */
function mapToSectors(title: string, summary?: string | null): SectorConcept[] {
  const text = `${title} ${summary ?? ""}`;
  const matched = new Map<string, SectorConcept>();
  // 遍历所有关键词做匹配
  for (const [kw, sector] of KEYWORD_TO_SECTOR) {
    if (text.includes(kw) && !matched.has(sector.id)) {
      matched.set(sector.id, sector);
      if (matched.size >= 3) break;
    }
  }
  return Array.from(matched.values());
}

/** 获取板块名称 */
function sectorName(s: SectorConcept): string {
  const key = "market_news_sector_" + s.id;
  const label = t(key);
  return label === key ? s.id : label;
}

function sectorMeta(id: string): SectorConcept {
  return SECTOR_CONCEPTS.find((sector) => sector.id === id) ?? { id, keywords: [], color: "#64748b", icon: "🏷️" };
}
function eventSectorIds(event: MarketEvent): string[] {
  return storedSectorIds(event, (legacy) => mapToSectors(legacy.title, legacy.summary).map((sector) => sector.id));
}

// ── 影响评价规则引擎 ──
function assessImpact(event: MarketEvent) {
  const lv = event.importance_level;
  const st = event.sentiment;
  const sc = event.impact_scope;

  const direction = st === "positive"
    ? t("market_news_sentiment_positive")
    : st === "negative"
      ? t("market_news_sentiment_negative")
      : t("market_news_sentiment_neutral");

  let degree: string;
  if (lv >= 5) degree = t("market_news_degree_severe");
  else if (lv >= 4) degree = t("market_news_degree_significant");
  else if (lv >= 3) degree = t("market_news_degree_moderate");
  else degree = t("market_news_degree_mild");

  let duration: string;
  if (sc === "breaking") duration = t("market_news_duration_short");
  else if (sc === "macro_policy") duration = t("market_news_duration_long");
  else if (sc === "fund_flow" || sc === "sentiment") duration = t("market_news_duration_short");
  else duration = t("market_news_duration_medium");

  let suggestion: string;
  if (lv >= 5) {
    suggestion = t("market_news_suggestion_urgent");
  } else if (st === "positive") {
    if (sc === "macro_policy") suggestion = t("market_news_suggestion_positive_macro");
    else if (sc === "sector_dynamics") suggestion = t("market_news_suggestion_positive_sector");
    else if (sc === "fund_flow") suggestion = t("market_news_suggestion_positive_fund");
    else suggestion = t("market_news_suggestion_positive_other");
  } else if (st === "negative") {
    if (sc === "breaking") suggestion = t("market_news_suggestion_negative_breaking");
    else if (sc === "macro_policy") suggestion = t("market_news_suggestion_negative_macro");
    else if (sc === "international") suggestion = t("market_news_suggestion_negative_intl");
    else suggestion = t("market_news_suggestion_negative_other");
  } else {
    suggestion = t("market_news_suggestion_neutral");
  }

  return { direction, degree, duration, suggestion };
}

function fmtDate(d: string | null, locale: string): string {
  if (!d) return "-";
  try {
    const timestamp = marketTime(d);
    if (!Number.isFinite(timestamp)) return d.slice(0, 16);
    return new Date(timestamp).toLocaleDateString(locale === "zh-CN" ? "zh-CN" : "en-US",
      { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch { return d.slice(0, 16); }
}

function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

/* ════════════════════════════════════════════════════════════
   MarketNews 主组件
   ════════════════════════════════════════════════════════════ */
export default function MarketNews() {
  const ctx = useApp();
  const locale = ctx.locale as "zh-CN" | "en-US";

  // ── 核心状态 ──
  const [data, setData] = useState<MarketEventListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [collecting, setCollecting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [themeManagerOpen, setThemeManagerOpen] = useState(false);
  const [signalsOpen, setSignalsOpen] = useState(() => new URLSearchParams(window.location.search).get("marketSignals") === "1");
  useEffect(() => {
    const show = () => setSignalsOpen(true);
    window.addEventListener("market-signals:open", show);
    return () => window.removeEventListener("market-signals:open", show);
  }, []);
  const closeSignals = () => {
    setSignalsOpen(false);
    const url = new URL(window.location.href);
    url.searchParams.delete("marketSignals");
    window.history.replaceState(null, "", url);
  };
  const [error, setError] = useState<string | null>(null);

  // 筛选状态
  const [filterScope, setFilterScope] = useState("");
  const [filterLevelMin, setFilterLevelMin] = useState(1);
  const [filterSentiment, setFilterSentiment] = useState("");
  // P1-6：关键词检索与"仅收藏"本地筛选
  const [keyword, setKeyword] = useState("");
  const [onlyFavorites, setOnlyFavorites] = useState(false);
  const [favorites, setFavorites] = useState<Set<number>>(() => loadFavorites());
  // P1-7：最近一次采集运行（可观测）
  const [lastRun, setLastRun] = useState<MarketEventCollectRun | null>(null);
  const [sortBy, setSortBy] = useState<"published_at" | "importance_level">("published_at");
  const [showFilter, setShowFilter] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  // 全局时间范围（控制新闻加载）
  const [timeRange, setTimeRange] = useState<number>(30);

  const favoriteIds = [...favorites].filter((id) => Number.isSafeInteger(id) && id > 0).sort((a, b) => a - b);
  const favoriteKey = favoriteIds.join(",");
  const query = useMemo<MarketEventQuery>(() => ({
    keyword: keyword.trim() || undefined, impact_scope: filterScope || undefined,
    importance_level_min: filterLevelMin, sentiment: filterSentiment || undefined,
    sort_by: sortBy, date_from: daysAgo(timeRange), include_expired: false,
  }), [keyword, filterScope, filterLevelMin, filterSentiment, sortBy, timeRange]);
  const queryKey = JSON.stringify([query, onlyFavorites ? favoriteKey : null]);
  const currentQueryKey = useRef(queryKey);
  currentQueryKey.current = queryKey;
  const [pagination, setPagination] = useState({ key: "", page: 1 });
  const page = pagination.key === queryKey ? pagination.page : 1;
  const listSnapshot = useRef<{ key: string; as_of?: string; snapshot_max_id?: number } | null>(null);

  // 概念面板的时间Tab和方向Tab
  const [conceptTimeTab, setConceptTimeTab] = useState<"7d"|"30d"|"90d"|"365d">("30d");
  const [conceptDirTab, setConceptDirTab] = useState<"bull"|"bear">("bull");

  // P0-3：概念热点后端聚合数据；接口异常时降级为本地统计（hotspotFallback=true）
  const [hotspotData, setHotspotData] = useState<MarketEventHotspotsResponse | null>(null);
  const [hotspotFallback, setHotspotFallback] = useState(false);
  const [hotspotLoading, setHotspotLoading] = useState(false);
  const listRequestId = useRef(0);
  const hotspotRequestId = useRef(0);
  const mounted = useRef(false);
  const currentConceptTab = useRef(conceptTimeTab);
  currentConceptTab.current = conceptTimeTab;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      listRequestId.current++;
      hotspotRequestId.current++;
    };
  }, []);

  // P1-5：人工修正消息标记（分类/等级/方向）——关键词规则误判时的运营兜底
  const [fixingId, setFixingId] = useState<number | null>(null);
  const [fixDraft, setFixDraft] = useState<{ scope: string; level: number; sentiment: string }>({ scope: "other", level: 2, sentiment: "neutral" });

  // 过期清理计数
  const [expiredCount, setExpiredCount] = useState(0);

  // ── 加载新闻数据 ──
  const loadData = useCallback(async () => {
    if (!mounted.current) return;
    const requestId = ++listRequestId.current;
    setLoading(true);
    setError(null);
    try {
      const snapshot: { as_of?: string; snapshot_max_id?: number } = listSnapshot.current?.key === queryKey ? listSnapshot.current : {};
      const params = { ...query, as_of: snapshot.as_of, snapshot_max_id: snapshot.snapshot_max_id };
      const [result, runsResult] = await Promise.all([
        onlyFavorites
          ? fetchCompleteMarketEvents(api.getMarketEvents, params, favoriteIds,
              () => mounted.current && requestId === listRequestId.current)
          : api.getMarketEvents({ ...params, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE }),
        api.getMarketEventCollectRuns(1).catch(() => null),
      ]);
      if (requestId !== listRequestId.current) return;
      setLastRun(Array.isArray(runsResult) && runsResult.length > 0 ? runsResult[0] : null);
      listSnapshot.current = { key: queryKey, as_of: result.as_of ?? undefined, snapshot_max_id: result.snapshot_max_id ?? undefined };
      const lastPage = Math.max(1, Math.ceil(result.total / PAGE_SIZE));
      if (page > lastPage) { setPagination({ key: queryKey, page: lastPage }); return; }
      setExpiredCount(result.expired_total ?? 0);
      setData({ ...result, events: onlyFavorites ? result.events.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE) : result.events });
    } catch (err: any) {
      if (requestId === listRequestId.current) setError(t(err.message || "market_news_load_failed"));
    } finally {
      if (requestId === listRequestId.current) setLoading(false);
    }
  }, [queryKey, page, locale]);

  useEffect(() => {
    void loadData();
    return () => { listRequestId.current++; };
  }, [loadData]);

  // P0-3：概念热点改调后端聚合接口——统计窗口是真实回溯（此前"近一季/近一年"
  // 只是对同一批 30 天数据再过滤的假切换），且口径不再受列表筛选影响。
  // data 变化（采集/刷新后）时联动重拉，保证热点与列表同步更新。
  const loadHotspots = useCallback(async (tab: "7d"|"30d"|"90d"|"365d") => {
    if (!mounted.current || tab !== currentConceptTab.current) return;
    const requestId = ++hotspotRequestId.current;
    setHotspotLoading(true);
    setHotspotFallback(false);
    try {
      const result = await api.getMarketEventHotspots(CONCEPT_DAYS[tab]);
      if (requestId !== hotspotRequestId.current || tab !== currentConceptTab.current) return;
      if (result.days !== CONCEPT_DAYS[tab]) throw new Error("Hotspot window mismatch");
      setHotspotData(result);
      setHotspotFallback(false);
    } catch {
      if (requestId !== hotspotRequestId.current || tab !== currentConceptTab.current) return;
      // 降级：后端接口不可用时沿用本地统计，页面保持可用（口径退回当次加载数据）
      setHotspotFallback(true);
    } finally {
      if (requestId === hotspotRequestId.current && tab === currentConceptTab.current) setHotspotLoading(false);
    }
  }, []);
  useEffect(() => {
    void loadHotspots(conceptTimeTab);
    return () => { hotspotRequestId.current++; };
  }, [loadHotspots, conceptTimeTab, data]);

  // 采集消息
  const handleCollect = async () => {
    setCollecting(true);
    setError(null);
    try {
      // P0-2：默认采集全部 6 个已实现源（此前硬编码只采东财全球+财新两源）
      const result = await api.collectMarketEvents({ days: 30, sources: ALL_COLLECT_SOURCES });
      if (!mounted.current) return;
      listSnapshot.current = null;
      await loadData();
      if (!mounted.current) return;
      if (result.status === "failed") {
        const message = `${t("market_news_collect_failed")}: ${(result.errors ?? []).join("; ")}`;
        setError(message);
        ctx.showToast("error", message);
      } else if (result.errors?.length) {
        ctx.showToast("info", `${template("market_news_collect_partial", { n: result.collected })}; ${result.errors.join("; ")}`);
      } else if (result && typeof result.collected === "number" && result.collected > 0) {
        ctx.showToast("success", template("market_news_collect_success", { n: result.collected }));
      } else {
        ctx.showToast("info", t("market_news_collect_nothing"));
      }
    } catch (err: any) {
      if (!mounted.current) return;
      setError(err.message || t("market_news_collect_failed"));
      ctx.showToast("error", err.message || t("market_news_collect_failed"));
    } finally {
      if (mounted.current) setCollecting(false);
    }
  };

  // P1-6：收藏切换（localStorage 持久化）
  const toggleFavorite = (id: number) => {
    setFavorites((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      localStorage.setItem(FAVORITES_KEY, JSON.stringify([...next]));
      return next;
    });
  };

  // Export every row matching the captured filters, across all pages/favorite batches.
  const exportCsv = async () => {
    setExporting(true);
    const capturedKey = queryKey;
    try {
      const result = await fetchCompleteMarketEvents(api.getMarketEvents, {
        ...query, as_of: data?.as_of ?? undefined, snapshot_max_id: data?.snapshot_max_id ?? undefined,
      }, onlyFavorites ? favoriteIds : undefined,
      () => mounted.current && currentQueryKey.current === capturedKey);
      const blob = new Blob([marketEventsCsv(result.events)], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      try {
        const a = document.createElement("a");
        a.href = url;
        a.download = `market-news-${new Date().toISOString().slice(0, 10)}.csv`;
        a.click();
      } finally { URL.revokeObjectURL(url); }
      ctx.showToast("success", template("market_news_export_done", { n: result.total }));
    } catch (err: any) {
      if (mounted.current) ctx.showToast("error", t(err?.message || "market_news_export_failed"));
    } finally {
      if (mounted.current) setExporting(false);
    }
  };

  // P1-5：保存人工修正（写回后端，标题未变只更新标记字段）
  const startFix = (event: MarketEvent) => {
    setFixingId(event.id);
    setFixDraft({ scope: event.impact_scope, level: event.importance_level, sentiment: event.sentiment });
  };
  const saveFix = async (eventId: number) => {
    try {
      await api.updateMarketEvent(eventId, {
        impact_scope: fixDraft.scope,
        importance_level: fixDraft.level,
        sentiment: fixDraft.sentiment,
      });
      ctx.showToast("success", t("market_news_fix_saved"));
      setFixingId(null);
      await loadData();
      void loadHotspots(conceptTimeTab);
    } catch (err: any) {
      ctx.showToast("error", err?.message || String(err));
    }
  };

  // ── 派生数据 ──
  const events = useMemo(() => data?.events ?? [], [data]);
  const sortedEvents = events; // Preserve server/complete-query ordering, including tie breakers.
  const scopeStats = useMemo(() => data?.by_scope ?? {}, [data]);

  // 辅助函数
  const scopeLabel = (key: string) => t("market_news_scope_" + key);
  const levelLabel = (lv: number) => t("market_news_level_" + lv);
  const srcName = (src: string) => {
    const key = "market_news_source_" + src;
    const label = t(key);
    return label === key ? src : label;
  };

  // ── 概念面板：行数据源 ──
  // P0-3：优先后端聚合（独立时间窗、不受列表筛选污染）；接口异常时降级为
  // 本地统计（旧口径：仅当次加载的数据 + 前端按时间Tab过滤）。
  interface ConceptRow { sector: SectorConcept; bull: number; bear: number; neutral: number; maxLevel: number; }
  const conceptRows = useMemo<ConceptRow[]>(() => {
    if (!hotspotFallback) {
      if (!hotspotData || hotspotData.days !== CONCEPT_DAYS[conceptTimeTab]) return [];
      return hotspotData.sectors
        .map((row) => {
          return { sector: sectorMeta(row.sector_id), bull: row.bull, bear: row.bear, neutral: row.neutral, maxLevel: row.max_level };
        })
        .filter((row): row is ConceptRow => row !== null);
    }
    return aggregateMarketSectors(events, CONCEPT_DAYS[conceptTimeTab], eventSectorIds)
      .map(({ id, ...counts }) => ({ sector: sectorMeta(id), ...counts }));
  }, [hotspotData, hotspotFallback, conceptTimeTab, events]);

  // 利多板块排行 / 利空板块排行
  const bullSectors = useMemo(() =>
    conceptRows.filter((s) => s.bull > 0).sort((a, b) => b.bull - a.bull),
    [conceptRows]
  );
  const bearSectors = useMemo(() =>
    conceptRows.filter((s) => s.bear > 0).sort((a, b) => b.bear - a.bear),
    [conceptRows]
  );

  // 当前显示的列表（根据方向Tab）
  const displayedSectors = conceptDirTab === "bull" ? bullSectors : bearSectors;
  const maxDisplayCount = displayedSectors.length > 0
    ? displayedSectors[0][conceptDirTab === "bull" ? "bull" : "bear"]
    : 1;

  /* ═══════════════════════════════ 渲染 ═════════════════════════════ */
  return (
    <div className="mn-page">
      {/* ── 头部工具栏 ── */}
      <section className="mn-hero">
        <div>
          <p className="panel-kicker">{t("market_news_title")}</p>
          <h1>{t("market_news_title")}</h1>
          <p className="mn-subtitle">{t("market_news_subtitle")}</p>
        </div>
        <Space wrap>
          <Button aria-label={signalLabels(locale).title} icon={<ThunderboltOutlined />} onClick={() => setSignalsOpen(true)}>{signalLabels(locale).title}</Button>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => { listSnapshot.current = null; void loadData(); }}>{t("market_news_refresh")}</Button>
          <Button icon={<DownloadOutlined />} loading={exporting} disabled={loading || !data?.total} onClick={exportCsv}>{t("market_news_export")}</Button>
          <Button type="primary" icon={<ThunderboltOutlined />} loading={collecting} onClick={handleCollect}>
            {collecting ? t("market_news_collecting") : t("market_news_collect")}
          </Button>
          <Button icon={<TagsOutlined />} onClick={() => setThemeManagerOpen(true)}>主题管理</Button>
          <Button icon={<FilterOutlined />} onClick={() => setShowFilter(!showFilter)}
            className={showFilter ? "mn-filter-active" : ""}>{t("market_news_filter")}</Button>

          <Select value={timeRange} onChange={setTimeRange} size="middle" style={{ width: 120 }}
            options={[
              { value: 7, label: t("market_news_last_7d") }, { value: 30, label: t("market_news_last_30d") },
              { value: 90, label: t("market_news_last_90d") }, { value: 365, label: t("market_news_last_365d") },
            ]}
          />

          <Select value={sortBy} onChange={(v) => setSortBy(v as any)} size="middle" style={{ width: 140 }}
            options={[{ value: "published_at", label: t("market_news_sort_time") }, { value: "importance_level", label: t("market_news_sort_level") }]}
          />
        </Space>
      </section>

      {/* 错误 + 过期提示 */}
      {error && <Alert type="error" message={error} showIcon className="mn-alert" />}
      {expiredCount > 0 && <Alert type="info" message={`${t("market_news_expired_cleaned")}: ${expiredCount}`} showIcon closable className="mn-alert" />}

      {/* ── 筛选面板 ── */}
      {showFilter && (
        <Card className="mn-filter-panel" size="small">
          <div className="mn-filter-grid">
            <label className="mn-filter-item"><span>{t("market_news_scope")}</span><Select value={filterScope || "__all__"} onChange={(v) => setFilterScope(v === "__all__" ? "" : v)} style={{ width: "100%" }}
              options={[{ value: "__all__", label: t("market_news_all_scopes") }, ...SCOPE_KEYS.map((k) => ({ value: k, label: scopeLabel(k) }))]} /></label>
            <label className="mn-filter-item"><span>{t("market_news_min_level")}</span><Select value={filterLevelMin} onChange={setFilterLevelMin} style={{ width: "100%" }}
              options={[{ value: 1, label: t("market_news_all_levels") }, { value: 3, label: `${levelLabel(3)}+ (3-5)` }, { value: 4, label: `${levelLabel(4)}+ (4-5)` }, { value: 5, label: levelLabel(5) }]} /></label>
            <label className="mn-filter-item"><span>{t("market_news_direction")}</span><Select value={filterSentiment || "__all__"} onChange={(v) => setFilterSentiment(v === "__all__" ? "" : v)} style={{ width: "100%" }}
              options={[{ value: "__all__", label: t("market_news_all_sentiments") }, { value: "positive", label: t("market_news_sentiment_positive") }, { value: "negative", label: t("market_news_sentiment_negative") }, { value: "neutral", label: t("market_news_sentiment_neutral") }]} /></label>
            <label className="mn-filter-item"><span>{t("market_news_keyword_search")}</span>
              <Input.Search value={keyword} onChange={(e) => setKeyword(e.target.value)} onSearch={() => loadData()} allowClear
                placeholder={t("market_news_keyword_placeholder")} />
            </label>
            <label className="mn-filter-item"><span>{t("market_news_favorite_filter")}</span>
              <Select value={onlyFavorites ? "only" : "__all__"} onChange={(v) => setOnlyFavorites(v === "only")} style={{ width: "100%" }}
                options={[{ value: "__all__", label: t("market_news_all_items") }, { value: "only", label: `⭐ ${t("market_news_favorite_only")}` }]} />
            </label>
          </div>
        </Card>
      )}

        <>
          {/* ── Scope 维度卡片 ── */}
          <div className="mn-scope-cards">
            {SCOPE_KEYS.map((key) => {
              const cfg = SCOPE_CONFIG[key]; const count = scopeStats[key] || 0; const active = filterScope === key;
              return (<button key={key} className={`mn-scope-card mn-scope--${key}${active ? " active" : ""}`}
                onClick={() => setFilterScope(filterScope === key ? "" : key)} title={`${scopeLabel(key)}: ${count}`}>
                <span className="mn-scope-icon">{cfg.icon}</span>
                <div className="mn-scope-info"><strong>{scopeLabel(key)}</strong><span className="mn-scope-count">{count}</span></div>
              </button>);
            })}
          </div>

          {/* ═══════════════════════════════════════════════════
             ★ 概念热点汇总面板（全新设计）
             时间Tab + 利多/利空Tab + 板块级展示
             ═══════════════════════════════════════════════════ */}
            <div className="mn-concept-panel" aria-busy={hotspotLoading}>
              {/* 头部 */}
              <div className="mn-concept-head">
                <div className="mn-concept-title-row">
                  <span className="mn-concept-icon">🎯</span>
                  <strong>{t("market_news_concept_panel_title")}</strong>
                  <span className="mn-concept-sub-text">{t("market_news_concept_panel_sub")}</span>
                </div>
                <span className="mn-concept-badge-total">{conceptRows.length} {t("market_news_active_concepts")}</span>
              </div>

              {/* ====== 第一行：Tab 切换栏 ====== */}
              <div className="mn-tab-bar">
                {/* 左侧：时间区间 Tab */}
                <div className="mn-tab-group mn-tab-group--time">
                  <span className="mn-tab-label">{t("market_news_time_tab_label")}:</span>
                  <button className={`mn-tab-btn${conceptTimeTab === "7d" ? " active" : ""}`} onClick={() => setConceptTimeTab("7d")}>{t("market_news_last_7d")}</button>
                  <button className={`mn-tab-btn${conceptTimeTab === "30d" ? " active" : ""}`} onClick={() => setConceptTimeTab("30d")}>{t("market_news_last_30d")}</button>
                  <button className={`mn-tab-btn${conceptTimeTab === "90d" ? " active" : ""}`} onClick={() => setConceptTimeTab("90d")}>{t("market_news_last_90d")}</button>
                  <button className={`mn-tab-btn${conceptTimeTab === "365d" ? " active" : ""}`} onClick={() => setConceptTimeTab("365d")}>{t("market_news_last_365d")}</button>
                </div>

                {/* 右侧：利多/利空 Tab */}
                <div className="mn-tab-group mn-tab-group--dir">
                  <button className={`mn-tab-btn mn-tab-btn--bull${conceptDirTab === "bull" ? " active" : ""}`} onClick={() => setConceptDirTab("bull")}>
                    🟢 {t("market_news_bull_tab")}
                  </button>
                  <button className={`mn-tab-btn mn-tab-btn--bear${conceptDirTab === "bear" ? " active" : ""}`} onClick={() => setConceptDirTab("bear")}>
                    🔴 {t("market_news_bear_tab")}
                  </button>
                </div>
              </div>

              {/* ====== 第二行：板块排行榜 ====== */}
              <div className="mn-sector-list">
                {displayedSectors.length > 0 ? displayedSectors.map((stat, idx) => {
                  const countKey = conceptDirTab === "bull" ? "bull" : "bear";
                  const count = stat[countKey] as number;
                  const pct = maxDisplayCount > 0 ? (count / maxDisplayCount) * 100 : 0;
                  const isTop3 = idx < 3;
                  // P0-3：稀疏样本下进度条会误导（1 条即满格），低于阈值挂"样本不足"标记
                  const lowSample = count < MIN_SAMPLE_COUNT;

                  return (
                    <Tooltip key={stat.sector.id} title={
                      `${sectorName(stat.sector)}: ${count}${t("market_news_count_unit")} ${conceptDirTab === "bull" ? t("market_news_sentiment_positive") : t("market_news_sentiment_negative")}` +
                      ` | ${t("market_news_sentiment_positive")}${stat.bull} ${t("market_news_sentiment_negative")}${stat.bear}` +
                      (stat.maxLevel >= 4 ? ` | ${levelLabel(stat.maxLevel)}` : "")
                    }>
                      <div className={`mn-sector-row${isTop3 ? ` mn-sector--top${idx}` : ""}${lowSample ? " mn-sector--low-sample" : ""}`}>
                        <span className="mn-sector-rank">{idx + 1}</span>
                        <span className="mn-sector-icon">{stat.sector.icon}</span>
                        <span className="mn-sector-name">{sectorName(stat.sector)}</span>
                        <div className="mn-sector-bar-track">
                          <div className={`mn-sector-bar-fill${conceptDirTab === "bull" ? " mn-bar--bull" : " mn-bar--bear"}`}
                            style={{ width: `${Math.max(pct, 8)}%` }} />
                        </div>
                        <span className={`mn-sector-count${conceptDirTab === "bull" ? " count-bull" : " count-bear"}`}>
                          {count}{t("market_news_count_unit")}
                        </span>
                        {lowSample && <span className="mn-sample-hint">{t("market_news_sample_insufficient")}</span>}
                      </div>
                    </Tooltip>
                  );
                }) : (
                  <div className="mn-empty-hint">
                    {hotspotLoading ? t("market_news_loading") : conceptDirTab === "bull" ? t("market_news_no_bull_data") : t("market_news_no_bear_data")}
                  </div>
                )}
              </div>

              {/* 底部提示 */}
              <div className="mn-panel-footer-hint">
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {hotspotFallback
                    ? <><span>{t("market_news_hotspot_fallback_hint")}</span>{" "}<span>{template("market_news_hotspot_loaded_hint", { n: events.length, days: timeRange })}</span></>
                    : template("market_news_hotspot_server_hint", { days: CONCEPT_DAYS[conceptTimeTab], sectors: conceptRows.length })}
                </Text>
              </div>
            </div>

          {/* ── 消息卡片列表 ── */}
          {!data || events.length === 0 ? (
            <Card className="mn-empty"><Empty description={t("market_news_empty")}><Button type="primary" loading={collecting} onClick={handleCollect}>{t("market_news_collect")}</Button></Empty></Card>
          ) : <div className="mn-news-list">
            {sortedEvents.map((event) => {
              const lvlCfg = LEVEL_CONFIG[event.importance_level] || LEVEL_CONFIG[1];
              const scopeCfg = SCOPE_CONFIG[event.impact_scope] || SCOPE_CONFIG.other;
              const assessment = assessImpact(event);
              const credibility = SOURCE_CREDIBILITY[event.source] ?? 2;
              const credLbl = CREDIBILITY_LABELS[credibility] ?? CREDIBILITY_LABELS[2];
              const mappedSectors = eventSectorIds(event).map(sectorMeta);
              const isExpanded = expandedId === event.id;

              return (
                <Card key={event.id} className={`mn-news-card mn-level-${event.importance_level}`}
                  onClick={() => setExpandedId(isExpanded ? null : event.id)}>
                  <div className="mn-card-header">
                    <Space size={6} wrap>
                      <Tag color={lvlCfg.color}>{levelLabel(event.importance_level)}</Tag>
                      <Tag style={{ backgroundColor: scopeCfg.color, borderColor: scopeCfg.color, color: "#fff", fontWeight: 600 }}>{scopeLabel(event.impact_scope)}</Tag>
                      <Tag color={event.sentiment === "positive" ? "green" : event.sentiment === "negative" ? "red" : "default"}>
                        {event.sentiment === "positive" ? t("market_news_sentiment_positive") : event.sentiment === "negative" ? t("market_news_sentiment_negative") : t("market_news_sentiment_neutral")}
                      </Tag>
                    </Space>
                    <span className="mn-fav-toggle" title={favorites.has(event.id) ? t("market_news_unfavorite") : t("market_news_favorite")}
                      onClick={(e) => { e.stopPropagation(); toggleFavorite(event.id); }}>
                      {favorites.has(event.id) ? <StarFilled style={{ color: "#f59e0b" }} /> : <StarOutlined style={{ color: "#94a3b8" }} />}
                    </span>
                    <Text type="secondary">{fmtDate(event.published_at, locale)}</Text>
                  </div>
                  <h3 className={`mn-title${event.importance_level >= 4 ? ` mn-title-lvl${event.importance_level}` : ""}`}>{event.title}</h3>
                  {event.summary && !isExpanded && (
                    <p className="mn-summary">{event.summary.length > 160 ? event.summary.slice(0, 160) + "..." : event.summary}</p>
                  )}
                  {isExpanded && (
                    <div className="mn-detail-panel">
                      {event.summary && <Paragraph className="mn-detail-summary">{event.summary}</Paragraph>}
                      {event.classification_analysis && (() => {
                        const analysis = event.classification_analysis;
                        const words = (values: string[] | undefined) => values?.length ? values.join("、") : t("market_news_analysis_none");
                        return <div className="mn-classification-analysis" role="note" style={{ padding: 12, marginBottom: 12, background: "#f8fafc", border: "1px solid #e2e8f0", borderRadius: 8 }}>
                          <Text strong>{t("market_news_analysis_title")}</Text>
                          <Paragraph type="secondary" style={{ marginBottom: 6 }}>{t("market_news_analysis_current_notice")}</Paragraph>
                          <div>{template("market_news_analysis_suggestion", { level: levelLabel(analysis.current_level), scope: scopeLabel(analysis.current_scope), direction: t(`market_news_sentiment_${analysis.current_sentiment}`) })}</div>
                          <div>{t("market_news_analysis_scope_hits")}: {words(analysis.scope_hits[analysis.current_scope])}</div>
                          <div>{t("market_news_analysis_level_hits")}: {analysis.level_hits[String(analysis.current_level)]?.length ? words(analysis.level_hits[String(analysis.current_level)]) : t("market_news_analysis_default_level")}</div>
                          <div>{t("market_news_analysis_positive_hits")}: {words(analysis.positive_hits)}</div>
                          <div>{t("market_news_analysis_negative_hits")}: {words(analysis.negative_hits)}</div>
                          {analysis.scope_ties.length > 1 && <div>{template("market_news_analysis_tie", { scopes: analysis.scope_ties.map(scopeLabel).join("、") })}</div>}
                          {analysis.negated_phrases.length > 0 && <div>{t("market_news_analysis_negated")}: {analysis.negated_phrases.map(phrase => <Tag key={phrase}>{phrase}</Tag>)}</div>}
                          <Text type={analysis.matches_saved ? "secondary" : "warning"}>{t(analysis.matches_saved ? "market_news_analysis_matches" : "market_news_analysis_mismatch")}</Text>
                          <div><Text type="secondary">{t("market_news_analysis_sector_limit")}</Text></div>
                        </div>;
                      })()}
                      <div className="mn-assessment">
                        <div className="mn-assessment-row"><span className="mn-assessment-label">{t("market_news_impact_dir")}</span><Text strong style={{ color: event.sentiment === "positive" ? "#16a34a" : event.sentiment === "negative" ? "#dc2626" : "#6b7280" }}>{assessment.direction}</Text></div>
                        <div className="mn-assessment-row"><span className="mn-assessment-label">{t("market_news_impact_deg")}</span><Text>{assessment.degree}</Text></div>
                        <div className="mn-assessment-row"><span className="mn-assessment-label">{t("market_news_impact_dur")}</span><Text>{assessment.duration}</Text></div>
                        <div className="mn-assessment-divider" />
                        <div className="mn-assessment-row"><span className="mn-assessment-label">{t("market_news_suggestion")}</span><Text>{assessment.suggestion}</Text></div>
                      </div>

                      {/* ★ 相关板块标签（替代原来的关键词标签） */}
                      {mappedSectors.length > 0 && (
                        <div className="mn-keywords">
                          <Text type="secondary" className="mn-kw-label">{t("market_news_related_sectors")}</Text>
                          {mappedSectors.map((s) => (
                            <Tag key={s.id} className="mn-kw-tag" color={s.color}>
                              {s.icon} {sectorName(s)}
                            </Tag>
                          ))}
                        </div>
                      )}
                      <div onClick={(e) => e.stopPropagation()}>
                        <Text type="secondary">{t(`market_news_tags_${event.sector_tag_source ?? "legacy"}`)}</Text>
                        {event.sector_tag_source !== "auto" && <Button type="link" size="small" onClick={async () => {
                          try { await api.updateMarketEvent(event.id, { sector_tag_source: "auto" }); await loadData(); }
                          catch (err: any) { ctx.showToast("error", err.message); }
                        }}>{t("market_news_tags_reset_auto")}</Button>}
                      </div>

                      <div className="mn-meta-footer">
                        <Tooltip title={`${t("market_news_source")}: ${srcName(event.source)}`}><Text type="secondary">{srcName(event.source)}</Text></Tooltip>
                        <span className="mn-credibility" title={t("market_news_credibility_" + credibility)}>{credLbl.stars}</span>
                        {event.source_url && (<a href={event.source_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} className="mn-source-link">{t("market_news_source")}</a>)}
                      </div>

                      {/* P1-5：人工修正标记入口（关键词规则误判的运营兜底） */}
                      <div className="mn-fix-row" onClick={(e) => e.stopPropagation()}>
                        {fixingId === event.id ? (
                          <Space size={6} wrap>
                            <Select size="small" value={fixDraft.scope} onChange={(v) => setFixDraft({ ...fixDraft, scope: v })} style={{ width: 132 }}
                              options={SCOPE_KEYS.map((k) => ({ value: k, label: scopeLabel(k) }))} />
                            <Select size="small" value={fixDraft.level} onChange={(v) => setFixDraft({ ...fixDraft, level: v })} style={{ width: 96 }}
                              options={[1, 2, 3, 4, 5].map((l) => ({ value: l, label: levelLabel(l) }))} />
                            <Select size="small" value={fixDraft.sentiment} onChange={(v) => setFixDraft({ ...fixDraft, sentiment: v })} style={{ width: 96 }}
                              options={[
                                { value: "positive", label: t("market_news_sentiment_positive") },
                                { value: "negative", label: t("market_news_sentiment_negative") },
                                { value: "neutral", label: t("market_news_sentiment_neutral") },
                              ]} />
                            <Button size="small" type="primary" onClick={() => void saveFix(event.id)}>{t("market_news_fix_save")}</Button>
                            <Button size="small" onClick={() => setFixingId(null)}>{t("market_news_fix_cancel")}</Button>
                          </Space>
                        ) : (
                          <Button size="small" type="link" onClick={() => startFix(event)}>{t("market_news_fix_toggle")}</Button>
                        )}
                      </div>
                    </div>
                  )}
                </Card>
              );
            })}
          </div>}
          <Pagination current={page} pageSize={PAGE_SIZE} total={data?.total ?? 0} showSizeChanger={false}
            onChange={(next) => setPagination({ key: queryKey, page: next })}
            showTotal={(total) => template("market_news_page_total", { n: total })} />

          {/* 底部统计 + P1-7 最近采集 */}
          <div className="mn-footer-stats">
            <Text type="secondary">{t("market_news_total_prefix")} <strong>{data?.total ?? 0}</strong> {t("market_news_total_suffix")}</Text>
            {lastRun && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                {template("market_news_last_collect_line", {
                  time: fmtDate(lastRun.finished_at ?? lastRun.started_at, locale),
                  source: lastRun.trigger_source === "scheduled" ? t("market_news_collect_scheduled") : t("market_news_collect_manual"),
                  collected: lastRun.collected,
                  skipped: lastRun.skipped_duplicate,
                  errors: lastRun.errors.length > 0 ? ` · ${template("market_news_last_collect_errors", { n: lastRun.errors.length })}` : "",
                })}
              </Text>
            )}
          </div>
        </>
      <ThemeManagementModal open={themeManagerOpen} onClose={() => setThemeManagerOpen(false)} />
      {signalsOpen && <Suspense fallback={null}><MarketSignalCenter open onClose={closeSignals} /></Suspense>}
    </div>
  );
}
