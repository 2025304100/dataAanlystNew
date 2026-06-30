import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Empty, Select, Space, Tag, Tooltip, Typography } from "antd";
import { CalendarOutlined, FilterOutlined, ReloadOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import type { MarketEvent, MarketEventListResponse } from "../types";

const { Paragraph, Text } = Typography;

// ════════════════════════════════════════════════
//  双语标签字典
// ════════════════════════════════════════════════
const L: Record<string, Record<string, any>> = {
  "zh-CN": {
    title: "行情消息", subtitle: "汇总影响大盘、期货和商品行情的重大消息，并给出影响评估。",
    refresh: "刷新", collect: "采集消息", collecting: "采集中...",
    filter: "筛选", empty: "暂无行情消息，点击采集按钮获取最新重大消息。",
    collectSuccess: (n: number) => `采集完成，新增 ${n} 条行情消息`,
    collectNothing: "本次采集没有新消息（可能全部重复或来源暂不可用）",
    allScopes: "全部范围", allSentiments: "全部方向",
    minLevel: "最低级别", scope: "范围", direction: "方向",
    sortTime: "按时间", sortLevel: "按重要级别",
    source: "来源", suggestion: "应对建议", keywords: "关键词",
    totalPrefix: "共", totalSuffix: "条消息",
    positive: "利多", negative: "利空", neutral: "中性",
    macro_policy: "宏观政策", commodity_futures: "期货商品",
    sector_dynamics: "行业动态", international: "国际形势",
    breaking: "突发事件", fund_flow: "资金流向",
    sentiment: "市场情绪", other: "其他",
    lvl5: "紧急", lvl4: "重要", lvl3: "关注", lvl2: "一般", lvl1: "参考",
    impactDir: "影响方向", impactDeg: "影响程度", impactDur: "预计持续",
    degSevere: "重大冲击", degSignificant: "显著影响", degModerate: "值得关注", degMild: "轻微波动",
    durShort: "1-3天", durMedium: "1-2周", durLong: "1-4周",
    cred5: "官方权威", cred4: "主流媒体", cred3: "财经媒体", cred2: "普通来源", cred1: "未验证",
    kwLabel: "关键词", kwCount: "提及次数",
    timeRange: "时间跨度", last7d: "近7天", last30d: "近一月", last90d: "近一季", last365d: "近一年",
    expiredCleaned: "已清理过期消息",
    // 概念热点汇总（新版）
    conceptPanelTitle: "概念热点汇总", conceptPanelSub: "根据新闻内容智能匹配相关板块/领域",
    timeTabLabel: "时间区间", bullTab: "利多板块", bearTab: "利空板块",
    hotSectorRank: "板块热度排行", relatedNews: "关联消息",
    noBullData: "暂无利多信号", noBearData: "暂无利空信号",
    activeConcepts: "个活跃板块",
  },
  "en-US": {
    title: "Market News", subtitle: "Major news affecting broad market, futures & commodities, with impact assessment.",
    refresh: "Refresh", collect: "Collect News", collecting: "Collecting...",
    filter: "Filter", empty: "No market news yet. Click Collect to fetch latest major events.",
    collectSuccess: (n: number) => `Collection done, ${n} new events added`,
    collectNothing: "No new events found (duplicates or sources temporarily unavailable)",
    allScopes: "All Scopes", allSentiments: "All Directions",
    minLevel: "Min Level", scope: "Scope", direction: "Direction",
    sortTime: "By Time", sortLevel: "By Importance",
    source: "Source", suggestion: "Suggestion", keywords: "Keywords",
    totalPrefix: "Total", totalSuffix: "events",
    positive: "Bullish", negative: "Bearish", neutral: "Neutral",
    macro_policy: "Macro Policy", commodity_futures: "Futures & Commodities",
    sector_dynamics: "Sector Dynamics", international: "International",
    breaking: "Breaking", fund_flow: "Fund Flow",
    sentiment: "Sentiment", other: "Other",
    lvl5: "Urgent", lvl4: "Important", lvl3: "Watch", lvl2: "Normal", lvl1: "Reference",
    impactDir: "Direction", impactDeg: "Degree", impactDur: "Duration",
    degSevere: "Major Shock", degSignificant: "Significant", degModerate: "Notable", degMild: "Mild",
    durShort: "1-3 days", durMedium: "1-2 weeks", durLong: "1-4 weeks",
    cred5: "Official Authority", cred4: "Mainstream Media", cred3: "Financial Media", cred2: "General Source", cred1: "Unverified",
    kwLabel: "Keywords", kwCount: "Mentions",
    timeRange: "Time Range", last7d: "Last 7 Days", last30d: "Last Month", last90d: "Last Quarter", last365d: "Last Year",
    expiredCleaned: "Expired items cleaned",
    conceptPanelTitle: "Concept Hotspot Summary", conceptPanelSub: "Intelligently match sectors/themes from news content",
    timeTabLabel: "Time Range", bullTab: "Bullish Sectors", bearTab: "Bearish Sectors",
    hotSectorRank: "Sector Heat Ranking", relatedNews: "Related News",
    noBullData: "No bullish signals", noBearData: "No bearish signals",
    activeConcepts: "active sectors",
  },
};

// ── 影响范围配置（8类）──
const SCOPE_CONFIG: Record<string, { label: string; enLabel: string; color: string; icon: string }> = {
  macro_policy:     { label: "宏观政策", enLabel: "Macro Policy",   color: "#b42318", icon: "📊" },
  commodity_futures:{ label: "期货商品", enLabel: "Futures",        color: "#059669", icon: "🧪" },
  sector_dynamics:  { label: "行业动态", enLabel: "Sector Dynamics",color: "#d97706", icon: "🔥" },
  international:    { label: "国际形势", enLabel: "International",  color: "#2563eb", icon: "🌍" },
  breaking:         { label: "突发事件", enLabel: "Breaking",       color: "#7c3aed", icon: "⚠️" },
  fund_flow:        { label: "资金流向", enLabel: "Fund Flow",      color: "#0891b2", icon: "💰" },
  sentiment:   { label: "市场情绪", enLabel: "Sentiment",      color: "#6b7280", icon: "🧠" },
  other:            { label: "其他",     enLabel: "Other",          color: "#64748b", icon: "•" },
};
const SCOPE_KEYS = Object.keys(SCOPE_CONFIG);

// ── 级别配置（5级）──
const LEVEL_CONFIG: Record<number, { label: string; enLabel: string; color: string; bgColor: string; borderStyle: string }> = {
  5: { label: "紧急", enLabel: "Urgent",     color: "#b42318", bgColor: "rgba(180,35,24,0.06)",  borderStyle: "solid 3px #b42318" },
  4: { label: "重要", enLabel: "Important",  color: "#d97706", bgColor: "rgba(217,119,6,0.06)",   borderStyle: "solid 3px #d97706" },
  3: { label: "关注", enLabel: "Watch",      color: "#2563eb", bgColor: "rgba(37,99,235,0.05)",   borderStyle: "solid 2px #2563eb" },
  2: { label: "一般", enLabel: "Normal",     color: "#6b7280", bgColor: "rgba(107,114,128,0.04)",  borderStyle: "solid 1px #d1d5db" },
  1: { label: "参考", enLabel: "Reference",  color: "#9ca3af", bgColor: "rgba(156,163,175,0.03)",  borderStyle: "solid 1px #e5e7eb" },
};

// ── 权威性评分 ──
const SOURCE_CREDIBILITY: Record<string, number> = {
  cctv: 5, "baidu-report": 4, baidu: 3,
  "eastmoney-global": 4, caixin: 4, "futures-shmet": 3, manual: 4,
};
const CREDIBILITY_LABELS: Record<number, { zh: string; en: string; stars: string }> = {
  5: { zh: "官方权威", en: "Official Authority", stars: "\u2605\u2605\u2605\u2605\u2605" },
  4: { zh: "主流媒体", en: "Mainstream Media",    stars: "\u2605\u2605\u2605\u2605\u2606" },
  3: { zh: "财经媒体", en: "Financial Media",     stars: "\u2605\u2605\u2605\u2606\u2606" },
  2: { zh: "普通来源", en: "General Source",      stars: "\u2605\u2605\u2606\u2606\u2606" },
  1: { zh: "未验证",   en: "Unverified",           stars: "\u2605\u2606\u2606\u2606\u2606" },
};
const SOURCE_NAMES: Record<string, { zh: string; en: string }> = {
  cctv: { zh: "央视新闻", en: "CCTV" }, baidu: { zh: "百度财经", en: "Baidu Finance" },
  "baidu-report": { zh: "百度报道", en: "Baidu Report" },
  "eastmoney-global": { zh: "东方财富全球", en: "Eastmoney Global" },
  caixin: { zh: "财新", en: "Caixin" }, "futures-shmet": { zh: "上海金属网", en: "SHMET Futures" },
  manual: { zh: "手动录入", en: "Manual" },
};

/* ════════════════════════════════════════════════════════════
   ★ 核心改进：行业板块概念知识库
   用结构化知识库替代机械分词，将新闻映射到有投资价值的板块
   
   每个板块包含：
   - id: 唯一标识
   - name / enName: 中文名/英文名
   - keywords: 匹配关键词列表（标题中包含即命中）
   - color: 展示颜色
   - icon: emoji 图标
   ════════════════════════════════════════════════════════════ */
interface SectorConcept {
  id: string;
  name: string;
  enName: string;
  keywords: string[];
  color: string;
  icon: string;
}

const SECTOR_CONCEPTS: SectorConcept[] = [
  // ===== 科技/AI =====
  { id: "ai_compute",       name: "AI算力",        enName: "AI & Compute",     keywords: ["人工智能","AI","大模型","GPT","ChatGPT","GPU","算力","数据中心","云计算","英伟达","NVIDIA","芯片","半导体","台积电","英特尔","AMD","微软","OpenAI","深度学习","机器学习","推理","训练","H100","A100","服务器","超算"], color: "#7c3aed", icon: "🤖" },
  { id: "semiconductor",    name: "半导体芯片",     enName: "Semiconductors",    keywords: ["半导体","芯片","晶圆","光刻","存储器","DRAM","NAND","闪存","集成电路","IC设计","封测","中芯","华虹","三星电子","SK海力士","美光","高通","博通","联发科"], color: "#2563eb", icon: "💾" },
  { id: "software_saas",    name: "软件/SaaS",      enName: "Software/SaaS",     keywords: ["软件","SaaS","云服务","办公软件","操作系统","数据库","ERP","CRM","Oracle","Salesforce","Adobe","用友","金山","金蝶","微软","谷歌"], color: "#0891b2", icon: "💿" },
  { id: "consumer_electronics", name: "消费电子", enName: "Consumer Electronics", keywords: ["消费电子","手机","iPhone","华为","小米","苹果","平板","可穿戴","AR","VR","元宇宙","Meta","耳机","智能家居","IoT"], color: "#db2777", icon: "📱" },
  { id: "space_satellite",  name: "航天卫星",      enName: "Space & Satellite", keywords: ["航天","卫星","SpaceX","火箭","太空","NASA","星链","北斗","GPS","马斯克","贝索斯","商业航天","低轨卫星","发射","轨道"], color: "#4338ca", icon: "🚀" },

  // ===== 新能源 =====
  { id: "new_energy_vehicle",name: "新能源汽车",   enName: "New Energy Vehicle",keywords: ["新能源车","电动汽车","EV","特斯拉","Tesla","比亚迪","蔚来","理想","小鹏","问界","宁德时代","CATL","动力电池","锂电","充电桩","自动驾驶","智驾","固态电池","4680","刀片电池"], color: "#16a34a", icon: "🚗" },
  { id: "solar_pv",         name: "光伏",          enName: "Solar/PV",           keywords: ["光伏","太阳能","硅料","硅片","组件","逆变器","隆基绿能","通威股份","阳光电源","天合光能","晶科能源","HJT","TOPCon","钙钛矿"], color: "#eab308", icon: "☀️" },
  { id: "wind_power",       name: "风电",          enName: "Wind Power",        keywords: ["风电","风力发电","风机","叶片","海上风电","陆上风电","金风科技","明阳智能","运达股份","三峡能源"], color: "#0ea5e9", icon: "💨" },
  { id: "energy_storage",   name: "储能",          enName: "Energy Storage",     keywords: ["储能","钠离子","液流电池","压缩空气","抽水蓄能","电网侧储能","户用储能","派能科技","鹏辉能源"], color: "#f59e0b", icon: "🔋" },

  // ===== 贵金属/大宗商品 =====
  { id: "precious_metals",  name: "贵金属",        enName: "Precious Metals",   keywords: ["黄金","白银","金价","银价","避险","COMEX","伦敦金","现货黄金","ETF黄金","央行购金","珠宝首饰","铂金","钯金","紫金矿业","山东黄金"], color: "#d97706", icon: "🥇" },
  { id: "crude_oil_energy", name: "原油能源",      enName: "Crude Oil/Energy",  keywords: ["原油","石油","OPEC","EIA","油价","WTI","布伦特","天然气","页岩油","沙特","俄罗斯石油","炼油","成品油","汽油","柴油","中石油","中石化","中海油","埃克森美孚","雪佛龙"], color: "#dc2626", icon: "🛢️" },
  { id: "steel_metals",     name: "钢铁有色",      enName: "Steel & Metals",    keywords: ["钢铁","铁矿石","螺纹钢","热卷板","铜","铝","锌","镍","锡","铅","LME","SHMET","上海金属","宝钢","河钢","五矿","江西铜业","紫金矿业","洛阳钼业"], color: "#78716c", icon: "⛏️" },
  { id: "agriculture",      name: "农业农产品",    enName: "Agriculture",        keywords: ["农产品","大豆","玉米","小麦","棉花","糖","橡胶","棕榈油","豆粕","生猪","猪肉","粮食","化肥","农药","种业","北大荒","中粮"], color: "#65a30d", icon: "🌾" },

  // ===== 金融地产 =====
  { id: "real_estate",      name: "房地产",        enName: "Real Estate",       keywords: ["房地产","楼市","房价","恒大","碧桂园","万科","保利","龙湖","融创","房贷","LPR","土地出让","二手房","新房销售","物业","保障房","城中村"], color: "#b45309", icon: "🏠" },
  { id: "banking_finance",  name: "银行金融",      enName: "Banking/Finance",    keywords: ["银行","利率","降息","加息","存款","贷款","信贷","M2","MLF","SLF","逆回购","LPR","央行","美联储","欧央行","日央行","四大行","招行","平安银行","券商","保险"], color: "#0369a1", icon: "🏦" },
  { id: "insurance",        name: "保险",          enName: "Insurance",          keywords: ["保险","寿险","财险","中国人寿","中国平安","太保","新华保险","保费","赔付率","偿付能力","养老险","健康险"], color: "#059669", icon: "🛡️" },

  // ===== 医药生物 =====
  { id: "pharma_biotech",   name: "医药生物",      enName: "Pharma/Biotech",     keywords: ["医药","制药","创新药","仿制药","CRO","CDMO","疫苗","中药","恒瑞医药","药明康德","迈瑞医疗","爱尔眼科","通策医疗","CXO","ADC","GLP","减肥药"], color: "#be185d", icon: "💊" },
  { id: "medical_devices",  name: "医疗器械",      enName: "Medical Devices",    keywords: ["医疗器械","影像设备","高值耗材","IVD","体外诊断","联影医疗","迈瑞","乐普医疗","微创医疗","骨科植入","心血管支架"], color: "#9333ea", icon: "🩺" },

  // ===== 消费 =====
  { id: "food_beverage",    name: "食品饮料",      enName: "Food & Beverage",    keywords: ["白酒","啤酒","乳制品","调味品","预制菜","零食","茅台","五粮液","伊利","海天味业","涪陵榨菜","安井食品","餐饮","海底捞","星巴克","可口可乐","百事"], color: "#c2410c", icon: "🍷" },
  { id: "retail_ecommerce", name: "零售电商",      enName: "Retail/E-commerce",  keywords: ["电商","零售","直播带货","拼多多","京东","阿里巴巴","淘宝","天猫","亚马逊","抖音电商","美团","免税","中国中免","永辉超市","苏宁"], color: "#ea580c", icon: "🛒" },
  { id: "tourism_hotel",    name: "旅游酒店",      enName: "Tourism & Hotel",    keywords: ["旅游","酒店","航空","机场","景区","免税店","携程","同程","中国中免","锦江酒店","首旅酒店","迪士尼","主题公园","邮轮","出境游"], color: "#0891b2", icon: "✈️" },
  { id: "auto_chain",       name: "汽车产业链",    enName: "Auto Supply Chain",  keywords: ["汽车","整车","零部件","特斯拉供应链","比亚迪产业链","造车新势力","自动驾驶","激光雷达","毫米波雷达","线控底盘","一体化压铸","拓普集团","德赛西威","华域汽车"], color: "#4f46e5", icon: "🚙" },

  // ===== 军工/高端制造 =====
  { id: "defense_military", name: "军工国防",      enName: "Defense/Military",   keywords: ["军工","国防","导弹","战斗机","航母","无人机","军机","舰船","潜艇","中航沈飞","航发动力","中国船舶","洪都航空","北方导航","信息化装备","C919","大飞机"], color: "#1e3a5f", icon: "✈️" },
  { id: "robotics",         name: "机器人",        enName: "Robotics",           keywords: ["机器人","人形机器人","工业机器人","协作机器人","伺服电机","减速器","传感器","特斯拉Optimus","优必选","埃斯顿","汇川技术","具身智能"], color: "#6366f1", icon: "🦾" },
  { id: "advanced_mfg",     name: "高端制造",      enName: "Advanced Mfg",       keywords: ["数控机床","工业母机","激光设备","3D打印","增材制造","工程机械","三一重工","中联重科","徐工机械","格力电器","美的集团","海尔智家"], color: "#374151", icon: "🏭" },

  // ===== 宏观/政策 =====
  { id: "macro_policy_cn",  name: "国内宏观政策",  enName: "China Macro Policy", keywords: ["国务院","发改委","财政部","央行","证监会","政治局","两会","GDP目标","财政政策","货币政策","减税降费","专项债","赤字率","社融","M2","PMI","CPI","PPI","稳增长","扩内需","促消费"], color: "#b91c1c", icon: "🏛️" },
  { id: "macro_intl_geo",   name: "国际地缘政治",  enName: "Geopolitics",        keywords: ["地缘政治","贸易战","关税","制裁","美联储","欧洲央行","日本央行","G7","G20","IMF","世界银行","WTO","RCEP","一带一路","中美关系","中俄关系","中欧关系","俄乌冲突","巴以冲突","红海危机"], color: "#991b1b", icon: "🌐" },
  { id: "crypto_web3",      name: "加密/Web3",     enName: "Crypto/Web3",        keywords: ["比特币","以太坊","BTC","ETH","区块链","Web3","DeFi","NFT","稳定币","SEC","币安","Coinbase","挖矿","矿机","ETF","现货比特币","监管"], color: "#f7931a", icon: "₿" },

  // ===== 其他 =====
  { id: "telecom_5g",       name: "通信/5G",       enName: "Telecom/5G",         keywords: ["5G","6G","通信","运营商","中国移动","中国联通","中国电信","光纤","基站","光模块","中兴通讯","烽火通信","华为","网络建设","算力网络"], color: "#0284c7", icon: "📡" },
  { id: "media_entertainment", name: "传媒娱乐",   enName: "Media/Entertainment", keywords: ["影视","游戏","短视频","抖音","快手","腾讯游戏","网易游戏","电影票房","院线","芒果TV","爱奇艺","腾讯视频","字节跳动","广告营销"], color: "#e11d48", icon: "🎬" },
  { id: "environment_carbon", name: "环保/碳中和", enName: "ESG/Carbon Neutral", keywords: ["碳中和","碳交易","环保","ESG","绿色金融","碳达峰","新能源配储","CCUS","碳排放权","清洁能源转型","光伏治沙","林业碳汇"], color: "#059669", icon: "🌿" },
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

/** 获取板块名称（双语） */
function sectorName(s: SectorConcept, locale: string): string {
  return locale === "zh-CN" ? s.name : s.enName;
}

// ── 影响评价规则引擎 ──
function assessImpact(event: MarketEvent, locale: string) {
  const zh = locale === "zh-CN";
  const lv = event.importance_level;
  const st = event.sentiment;
  const sc = event.impact_scope;

  const direction = st === "positive"
    ? (zh ? "利多" : "Bullish")
    : st === "negative"
      ? (zh ? "利空" : "Bearish")
      : (zh ? "中性" : "Neutral");

  let degree: string;
  if (lv >= 5) degree = zh ? "重大冲击" : "Major Shock";
  else if (lv >= 4) degree = zh ? "显著影响" : "Significant";
  else if (lv >= 3) degree = zh ? "值得关注" : "Notable";
  else degree = zh ? "轻微波动" : "Mild";

  let duration: string;
  if (sc === "breaking") duration = zh ? "1-3天" : "1-3 days";
  else if (sc === "macro_policy") duration = zh ? "1-4周" : "1-4 weeks";
  else if (sc === "fund_flow" || sc === "sentiment") duration = zh ? "1-3天" : "1-3 days";
  else duration = zh ? "1-2周" : "1-2 weeks";

  let suggestion: string;
  if (lv >= 5) {
    suggestion = zh ? "先降低激进仓位，等待消息落地和价格反应。" : "Reduce aggressive exposure first.";
  } else if (st === "positive") {
    if (sc === "macro_policy") suggestion = zh ? "利好大盘，关注金融/地产板块。" : "Bullish broad market; watch finance & property.";
    else if (sc === "sector_dynamics") suggestion = zh ? "相关板块有望活跃，注意追高风险。" : "Sector may be active; watch chasing risk.";
    else if (sc === "fund_flow") suggestion = zh ? "资金面改善，风险偏好上升。" : "Fund flow improving; risk appetite rising.";
    else suggestion = zh ? "偏多情绪，需结合技术面确认。" : "Bullish bias but confirm with technicals.";
  } else if (st === "negative") {
    if (sc === "breaking") suggestion = zh ? "规避系统性风险，降低仓位。" : "Avoid systemic risk; reduce positions.";
    else if (sc === "macro_policy") suggestion = zh ? "政策收紧预期，控制杠杆。" : "Tightening expected; control leverage.";
    else if (sc === "international") suggestion = zh ? "外部冲击，防御性板块相对安全。" : "External shock; defensive safer.";
    else suggestion = zh ? "谨慎观望，等待企稳信号。" : "Cautious wait; look for stabilization.";
  } else {
    suggestion = zh ? "中性消息，维持现有策略不变。" : "Neutral; maintain current strategy.";
  }

  return { direction, degree, duration, suggestion };
}

function fmtDate(d: string | null, locale: string): string {
  if (!d) return "-";
  try {
    return new Date(d).toLocaleDateString(locale === "zh-CN" ? "zh-CN" : "en-US",
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
  const lb = L[locale] ?? L["zh-CN"];

  // ── 核心状态 ──
  const [data, setData] = useState<MarketEventListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [collecting, setCollecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 筛选状态
  const [filterScope, setFilterScope] = useState("");
  const [filterLevelMin, setFilterLevelMin] = useState(1);
  const [filterSentiment, setFilterSentiment] = useState("");
  const [sortBy, setSortBy] = useState<"published_at" | "importance_level">("published_at");
  const [showFilter, setShowFilter] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  // 全局时间范围（控制新闻加载）
  const [timeRange, setTimeRange] = useState<number>(30);

  // 概念热点面板专用状态
  interface SectorStat { sector: SectorConcept; bull: number; bear: number; neutral: number; maxLevel: number; events: number[]; }
  const [sectorStats, setSectorStats] = useState<SectorStat[]>([]);

  // 概念面板的时间Tab和方向Tab
  const [conceptTimeTab, setConceptTimeTab] = useState<"30d"|"90d"|"365d">("30d");
  const [conceptDirTab, setConceptDirTab] = useState<"bull"|"bear">("bull");

  // 过期清理计数
  const [expiredCount, setExpiredCount] = useState(0);

  // 关键词统计（用于卡片展开详情）
  const [keywordStats, setKeywordStats] = useState<Record<string, number>>({});

  // ── 加载新闻数据 ──
  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getMarketEvents({
        impact_scope: filterScope || undefined,
        importance_level_min: filterLevelMin,
        sentiment: filterSentiment || undefined,
        sort_by: sortBy,
        limit: 200,
        date_from: daysAgo(timeRange),
      });

      // 过期清理
      let cleaned = result.events as MarketEvent[];
      let expired = 0;
      const now = new Date();
      cleaned = cleaned.filter((e) => {
        if (e.expires_at && new Date(e.expires_at) < now) { expired++; return false; }
        return true;
      });
      if (expired > 0) setExpiredCount(expired);

      setData({ ...result, events: cleaned, total: cleaned.length });

      // 计算板块统计 + 关键词统计
      buildSectorStats(cleaned);
    } catch (err: any) {
      setError(err.message || (locale === "zh-CN" ? "加载失败" : "Failed to load"));
    } finally {
      setLoading(false);
    }
  }, [filterScope, filterLevelMin, filterSentiment, sortBy, timeRange, locale]);

  useEffect(() => { loadData(); }, [loadData]);

  /** 构建板块统计数据（核心函数） */
  function buildSectorStats(events: MarketEvent[]) {
    // 板块聚合
    const sectorMap = new Map<string, { bull: number; bear: number; neutral: number; maxLevel: number; events: number[] }>();

    // 关键词频率（保留用于展开详情）
    const kwCount: Record<string, number> = {};

    events.forEach((e) => {
      // 映射到板块
      const sectors = mapToSectors(e.title, e.summary);
      sectors.forEach((s) => {
        if (!sectorMap.has(s.id)) sectorMap.set(s.id, { bull: 0, bear: 0, neutral: 0, maxLevel: 0, events: [] });
        const st = sectorMap.get(s.id)!;
        if (e.sentiment === "positive") st.bull++;
        else if (e.sentiment === "negative") st.bear++;
        else st.neutral++;
        st.maxLevel = Math.max(st.maxLevel, e.importance_level);
        st.events.push(e.id);
      });

      // 同时提取原始关键词（用于详情展示）
      e.title.match(/[\u4e00-\u9fa5]{3,8}/g)?.forEach((w) => {
        kwCount[w] = (kwCount[w] || 0) + 1;
      });
    });

    setKeywordStats(kwCount);

    // 转换为排序后的数组
    const stats: SectorStat[] = Array.from(sectorMap.entries())
      .map(([id, data]) => ({
        sector: SECTOR_CONCEPTS.find((s) => s.id === id)!,
        ...data,
      }))
      .sort((a, b) => {
        // 按总提及量排序
        const aTotal = a.bull + a.bear + a.neutral;
        const bTotal = b.bull + b.bear + b.neutral;
        return bTotal !== aTotal ? bTotal - aTotal : b.maxLevel - a.maxLevel;
      });

    setSectorStats(stats);
  }

  // 采集消息
  const handleCollect = async () => {
    setCollecting(true);
    setError(null);
    try {
      const result = await api.collectMarketEvents({ days: 30, sources: ["eastmoney-global", "caixin"] });
      await loadData();
      if (result && typeof result.collected === "number" && result.collected > 0) {
        ctx.showToast("success", lb.collectSuccess(result.collected));
      } else {
        ctx.showToast("info", lb.collectNothing);
      }
    } catch (err: any) {
      setError(err.message || (locale === "zh-CN" ? "采集失败" : "Collection failed"));
      ctx.showToast("error", err.message || (locale === "zh-CN" ? "采集失败" : "Collection failed"));
    } finally {
      setCollecting(false);
    }
  };

  // ── 派生数据 ──
  const events = useMemo(() => data?.events ?? [], [data]);
  const scopeStats = useMemo(() => data?.by_scope ?? {}, [data]);

  const sortedEvents = useMemo(() => {
    const list = [...events];
    if (sortBy === "importance_level") list.sort((a, b) => b.importance_level - a.importance_level);
    else list.sort((a, b) => {
      const da = a.published_at ? new Date(a.published_at).getTime() : 0;
      const db = b.published_at ? new Date(b.published_at).getTime() : 0;
      return db - da;
    });
    return list;
  }, [events, sortBy]);

  // 辅助函数
  const scopeLabel = (key: string) =>
    (SCOPE_CONFIG[key]?.[locale === "zh-CN" ? "label" : "enLabel"]) ?? key;
  const levelLabel = (lv: number) => {
    const cfg = LEVEL_CONFIG[lv];
    return cfg ? (locale === "zh-CN" ? cfg.label : cfg.enLabel) : (locale === "zh-CN" ? "参考" : "Reference");
  };
  const srcName = (src: string) => {
    const n = SOURCE_NAMES[src];
    return n ? (locale === "zh-CN" ? n.zh : n.en) : src;
  };

  // ── 概念面板：根据时间Tab筛选显示的数据 ──
  const filteredSectorStats = useMemo(() => {
    if (conceptTimeTab === "30d") return sectorStats; // 已是当前加载的数据
    // 对于更长的时间范围，我们基于已加载的事件按日期过滤
    const cutoffDays = conceptTimeTab === "90d" ? 90 : 365;
    const cutoff = new Date(); cutoff.setDate(cutoff.getDate() - cutoffDays);
    const filtered = sectorStats.filter((s) =>
      s.events.some((eid) => {
        const ev = events.find((e) => e.id === eid);
        return ev?.published_at && new Date(ev.published_at) >= cutoff;
      })
    );
    return filtered;
  }, [sectorStats, conceptTimeTab, events]);

  // 利多板块排行 / 利空板块排行
  const bullSectors = useMemo(() =>
    filteredSectorStats.filter((s) => s.bull > 0).sort((a, b) => b.bull - a.bull),
    [filteredSectorStats]
  );
  const bearSectors = useMemo(() =>
    filteredSectorStats.filter((s) => s.bear > 0).sort((a, b) => b.bear - a.bear),
    [filteredSectorStats]
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
          <p className="panel-kicker">{lb.title}</p>
          <h1>{lb.title}</h1>
          <p className="mn-subtitle">{lb.subtitle}</p>
        </div>
        <Space wrap>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={loadData}>{lb.refresh}</Button>
          <Button type="primary" icon={<ThunderboltOutlined />} loading={collecting} onClick={handleCollect}>
            {collecting ? lb.collecting : lb.collect}
          </Button>
          <Button icon={<FilterOutlined />} onClick={() => setShowFilter(!showFilter)}
            className={showFilter ? "mn-filter-active" : ""}>{lb.filter}</Button>

          <Select value={timeRange} onChange={setTimeRange} size="middle" style={{ width: 120 }}
            options={[
              { value: 7, label: lb.last7d }, { value: 30, label: lb.last30d },
              { value: 90, label: lb.last90d }, { value: 365, label: lb.last365d },
            ]}
          />

          <Select value={sortBy} onChange={(v) => setSortBy(v as any)} size="middle" style={{ width: 140 }}
            options={[{ value: "published_at", label: lb.sortTime }, { value: "importance_level", label: lb.sortLevel }]}
          />
        </Space>
      </section>

      {/* 错误 + 过期提示 */}
      {error && <Alert type="error" message={error} showIcon className="mn-alert" />}
      {expiredCount > 0 && <Alert type="info" message={`${lb.expiredCleaned}: ${expiredCount}`} showIcon closable className="mn-alert" />}

      {/* ── 筛选面板 ── */}
      {showFilter && (
        <Card className="mn-filter-panel" size="small">
          <div className="mn-filter-grid">
            <label className="mn-filter-item"><span>{lb.scope}</span><Select value={filterScope || "__all__"} onChange={(v) => setFilterScope(v === "__all__" ? "" : v)} style={{ width: "100%" }}
              options={[{ value: "__all__", label: lb.allScopes }, ...SCOPE_KEYS.map((k) => ({ value: k, label: scopeLabel(k) }))]} /></label>
            <label className="mn-filter-item"><span>{lb.minLevel}</span><Select value={filterLevelMin} onChange={setFilterLevelMin} style={{ width: "100%" }}
              options={[{ value: 1, label: locale === "zh-CN" ? "全部分级 (1-5)" : "All Levels (1-5)" }, { value: 3, label: `${levelLabel(3)}+ (3-5)` }, { value: 4, label: `${levelLabel(4)}+ (4-5)` }, { value: 5, label: levelLabel(5) }]} /></label>
            <label className="mn-filter-item"><span>{lb.direction}</span><Select value={filterSentiment || "__all__"} onChange={(v) => setFilterSentiment(v === "__all__" ? "" : v)} style={{ width: "100%" }}
              options={[{ value: "__all__", label: lb.allSentiments }, { value: "positive", label: lb.positive }, { value: "negative", label: lb.negative }, { value: "neutral", label: lb.neutral }]} /></label>
          </div>
        </Card>
      )}

      {/* ── 空状态 ── */}
      {!data || events.length === 0 ? (
        <Card className="mn-empty"><Empty description={lb.empty}><Button type="primary" loading={collecting} onClick={handleCollect}>{lb.collect}</Button></Empty></Card>
      ) : (
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
          {sectorStats.length > 0 && (
            <div className="mn-concept-panel">
              {/* 头部 */}
              <div className="mn-concept-head">
                <div className="mn-concept-title-row">
                  <span className="mn-concept-icon">🎯</span>
                  <strong>{lb.conceptPanelTitle}</strong>
                  <span className="mn-concept-sub-text">{lb.conceptPanelSub}</span>
                </div>
                <span className="mn-concept-badge-total">{sectorStats.length} {lb.activeConcepts}</span>
              </div>

              {/* ====== 第一行：Tab 切换栏 ====== */}
              <div className="mn-tab-bar">
                {/* 左侧：时间区间 Tab */}
                <div className="mn-tab-group mn-tab-group--time">
                  <span className="mn-tab-label">{lb.timeTabLabel}:</span>
                  <button className={`mn-tab-btn${conceptTimeTab === "30d" ? " active" : ""}`} onClick={() => setConceptTimeTab("30d")}>{locale === "zh-CN" ? "近一月" : "Last Month"}</button>
                  <button className={`mn-tab-btn${conceptTimeTab === "90d" ? " active" : ""}`} onClick={() => setConceptTimeTab("90d")}>{locale === "zh-CN" ? "近一季" : "Last Quarter"}</button>
                  <button className={`mn-tab-btn${conceptTimeTab === "365d" ? " active" : ""}`} onClick={() => setConceptTimeTab("365d")}>{locale === "zh-CN" ? "近一年" : "Last Year"}</button>
                </div>

                {/* 右侧：利多/利空 Tab */}
                <div className="mn-tab-group mn-tab-group--dir">
                  <button className={`mn-tab-btn mn-tab-btn--bull${conceptDirTab === "bull" ? " active" : ""}`} onClick={() => setConceptDirTab("bull")}>
                    🟢 {lb.bullTab}
                  </button>
                  <button className={`mn-tab-btn mn-tab-btn--bear${conceptDirTab === "bear" ? " active" : ""}`} onClick={() => setConceptDirTab("bear")}>
                    🔴 {lb.bearTab}
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

                  return (
                    <Tooltip key={stat.sector.id} title={
                      `${sectorName(stat.sector, locale)}: ${count}${locale === "zh-CN" ? "条" : ""} ${conceptDirTab === "bull" ? lb.positive : lb.negative}` +
                      ` | ${lb.positive}${stat.bull} ${lb.negative}${stat.bear}` +
                      (stat.maxLevel >= 4 ? ` | ${levelLabel(stat.maxLevel)}` : "")
                    }>
                      <div className={`mn-sector-row${isTop3 ? ` mn-sector--top${idx}` : ""}`}>
                        <span className="mn-sector-rank">{idx + 1}</span>
                        <span className="mn-sector-icon">{stat.sector.icon}</span>
                        <span className="mn-sector-name">{sectorName(stat.sector, locale)}</span>
                        <div className="mn-sector-bar-track">
                          <div className={`mn-sector-bar-fill${conceptDirTab === "bull" ? " mn-bar--bull" : " mn-bar--bear"}`}
                            style={{ width: `${Math.max(pct, 8)}%` }} />
                        </div>
                        <span className={`mn-sector-count${conceptDirTab === "bull" ? " count-bull" : " count-bear"}`}>
                          {count}{locale === "zh-CN" ? "条" : ""}
                        </span>
                      </div>
                    </Tooltip>
                  );
                }) : (
                  <div className="mn-empty-hint">
                    {conceptDirTab === "bull" ? lb.noBullData : lb.noBearData}
                  </div>
                )}
              </div>

              {/* 底部提示 */}
              <div className="mn-panel-footer-hint">
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {locale === "zh-CN"
                    ? `基于 ${events.length} 条新闻自动匹配 · 共识别 ${sectorStats.length} 个活跃板块`
                    : `Auto-matched from ${events.length} news items · ${sectorStats.length} active sectors identified`
                  }
                </Text>
              </div>
            </div>
          )}

          {/* ── 消息卡片列表 ── */}
          <div className="mn-news-list">
            {sortedEvents.map((event) => {
              const lvlCfg = LEVEL_CONFIG[event.importance_level] || LEVEL_CONFIG[1];
              const scopeCfg = SCOPE_CONFIG[event.impact_scope] || SCOPE_CONFIG.other;
              const assessment = assessImpact(event, locale);
              const credibility = SOURCE_CREDIBILITY[event.source] ?? 2;
              const credLbl = CREDIBILITY_LABELS[credibility] ?? CREDIBILITY_LABELS[2];
              const mappedSectors = mapToSectors(event.title, event.summary);
              const isExpanded = expandedId === event.id;

              return (
                <Card key={event.id} className={`mn-news-card mn-level-${event.importance_level}`}
                  onClick={() => setExpandedId(isExpanded ? null : event.id)}>
                  <div className="mn-card-header">
                    <Space size={6} wrap>
                      <Tag color={lvlCfg.color}>{levelLabel(event.importance_level)}</Tag>
                      <Tag style={{ backgroundColor: scopeCfg.color, borderColor: scopeCfg.color, color: "#fff", fontWeight: 600 }}>{scopeLabel(event.impact_scope)}</Tag>
                      <Tag color={event.sentiment === "positive" ? "green" : event.sentiment === "negative" ? "red" : "default"}>
                        {event.sentiment === "positive" ? lb.positive : event.sentiment === "negative" ? lb.negative : lb.neutral}
                      </Tag>
                    </Space>
                    <Text type="secondary">{fmtDate(event.published_at, locale)}</Text>
                  </div>
                  <h3 className={`mn-title${event.importance_level >= 4 ? ` mn-title-lvl${event.importance_level}` : ""}`}>{event.title}</h3>
                  {event.summary && !isExpanded && (
                    <p className="mn-summary">{event.summary.length > 160 ? event.summary.slice(0, 160) + "..." : event.summary}</p>
                  )}
                  {isExpanded && (
                    <div className="mn-detail-panel">
                      {event.summary && <Paragraph className="mn-detail-summary">{event.summary}</Paragraph>}
                      <div className="mn-assessment">
                        <div className="mn-assessment-row"><span className="mn-assessment-label">{lb.impactDir}</span><Text strong style={{ color: event.sentiment === "positive" ? "#16a34a" : event.sentiment === "negative" ? "#dc2626" : "#6b7280" }}>{assessment.direction}</Text></div>
                        <div className="mn-assessment-row"><span className="mn-assessment-label">{lb.impactDeg}</span><Text>{assessment.degree}</Text></div>
                        <div className="mn-assessment-row"><span className="mn-assessment-label">{lb.impactDur}</span><Text>{assessment.duration}</Text></div>
                        <div className="mn-assessment-divider" />
                        <div className="mn-assessment-row"><span className="mn-assessment-label">{lb.suggestion}</span><Text>{assessment.suggestion}</Text></div>
                      </div>

                      {/* ★ 相关板块标签（替代原来的关键词标签） */}
                      {mappedSectors.length > 0 && (
                        <div className="mn-keywords">
                          <Text type="secondary" className="mn-kw-label">{locale === "zh-CN" ? "相关板块" : "Related Sectors"}</Text>
                          {mappedSectors.map((s) => (
                            <Tag key={s.id} className="mn-kw-tag" color={s.color.replace("#", "")}>
                              {s.icon} {sectorName(s, locale)}
                            </Tag>
                          ))}
                        </div>
                      )}

                      <div className="mn-meta-footer">
                        <Tooltip title={`${lb.source}: ${srcName(event.source)}`}><Text type="secondary">{srcName(event.source)}</Text></Tooltip>
                        <span className="mn-credibility" title={credLbl[locale === "zh-CN" ? "zh" : "en"]}>{credLbl.stars}</span>
                        {event.source_url && (<a href={event.source_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} className="mn-source-link">{locale === "zh-CN" ? "来源" : "Source"}</a>)}
                      </div>
                    </div>
                  )}
                </Card>
              );
            })}
          </div>

          {/* 底部统计 */}
          <div className="mn-footer-stats">
            <Text type="secondary">{lb.totalPrefix} <strong>{data.total}</strong> {lb.totalSuffix}</Text>
          </div>
        </>
      )}
    </div>
  );
}
