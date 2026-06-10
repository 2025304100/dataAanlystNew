const DOT = " | ";
const DEFAULT_CHART_WINDOW = 40;
const CHART_WINDOW_STEPS = [20, 40, 80, 120];

const I18N = {
  "zh-CN": {
    eyebrow: "个人量化工作台",
    overviewTitle: "总览",
    portfolio: "组合",
    language: "语言",
    market: "市场",
    sync: "同步",
    scan: "扫描",
    newsUpdate: "查消息面",
    refresh: "刷新",
    refreshPlan: "刷新计划",
    latestAutoScan: "最近自动扫描",
    candidates: "候选池",
    noScanYet: "暂无扫描结果",
    rank: "排名",
    symbol: "标的",
    quality: "股质",
    timing: "时点",
    stage: "阶段",
    action: "动作",
    position: "仓位",
    scoreboard: "评分看板",
    latestScores: "最新评分",
    coverage: "覆盖情况",
    watchlists: "观察池",
    recentNotes: "最近记录",
    journals: "日志",
    symbolDetail: "标的详情",
    selectSymbol: "请选择标的",
    latestAssessment: "最新判断",
    tradeSetup: "交易计划",
    scoreHistory: "评分历史",
    recentBars: "最近行情",
    close: "收盘",
    volume: "成交量",
    date: "日期",
    noCandidates: "最近一次扫描里没有可执行候选。",
    noScores: "暂无评分。",
    noWatchlists: "暂无观察池。",
    noJournals: "暂无记录。",
    noDetail: "点击候选或评分卡片查看详情。",
    trackedUniverse: "覆盖标的",
    curatedBuckets: "观察分组",
    portfolioUsage: "组合使用率",
    reserveLeft: "现金保留",
    latestScanMetric: "最近自动扫描",
    stable: "平稳",
    syncing: "同步中...",
    scanning: "扫描中...",
    planGenerating: "刷新计划中...",
    syncFailed: "同步失败",
    scanFailed: "扫描失败",
    planFailed: "计划刷新失败",
    all: "全部",
    latestScoreDate: "评分日期",
    recommendedPosition: "建议仓位",
    positionAmount: "建议金额",
    target: "目标位",
    stopLoss: "止损位",
    buyZone: "买入区间",
    riskReward: "盈亏比",
    marketLabel: "市场",
    regionLabel: "区域",
    assetLabel: "资产类型",
    latestTradeSetupEmpty: "暂无交易计划",
    cash: "现金",
    maxSingle: "单标上限",
    noPortfolio: "暂无组合",
    noActiveRule: "暂无活动规则",
    chinaMainland: "中国大陆",
    unitedStates: "美国",
    items: "项",
    unknown: "未知",
    allowAdd: "允许加仓",
    guardrails: "约束",
    reason: "依据",
    lastBar: "最新K线",
    chartWindow: "观察窗口",
    syncSummary: "已同步 {ok}/{total} 个标的，失败 {failed} 个。",
    scanSummary: "扫描完成，候选 {count} 个。",
    planSummary: "交易计划已按最新评分刷新。",
    sectorOverweight: "行业超限",
    assetOverweight: "资产超限",
    noChart: "暂无K线数据。",
    yes: "是",
    no: "否",
    stageCap: "阶段上限",
    stageRoom: "阶段余量",
    currentPosition: "当前持仓",
    riskBudget: "单笔风险预算",
    riskShare: "每股风险",
    shareCap: "风险上限股数",
    openTrigger: "开仓触发",
    addTrigger: "加仓触发",
    stopTrigger: "止损触发",
    trimTrigger: "止盈触发",
    tranchePlan: "分批执行",
    tranchePct: "仓位占比",
    trigger: "触发条件",
    notes: "执行备注",
    ma10: "MA10",
    ma20: "MA20",
    markerBuy: "买点",
    markerStop: "止损",
    markerTarget: "目标",
    simAccount: "模拟账户",
    accountSummary: "账户概览",
    accountEquity: "总权益",
    accountCash: "可用现金",
    accountMarketValue: "持仓市值",
    accountRealizedPnl: "已实现盈亏",
    accountUnrealizedPnl: "浮动盈亏",
    accountInvested: "在仓比例",
    activeTrades: "近7日交易",
    lastTrade: "最近成交",
    noTrades: "暂无模拟成交",
    recentTrades: "最近成交",
    simBuy: "模拟买入",
    simSell: "模拟卖出",
    orderQty: "数量",
    orderPrice: "价格",
    buySide: "买入",
    sellSide: "卖出",
    orderSummary: "模拟{side}成交 {symbol} {quantity} 股，成交价 {price}。",
    orderFailed: "模拟交易失败",
    holdingQty: "持仓数量",
    avgCost: "持仓均价",
    todayOpportunities: "今日机会",
    todayMethod: "自动扫描 + 评分 + 买区 + 消息面",
    todayExecutable: "可执行机会",
    todayWatch: "观察队列",
    todayMessages: "消息面",
    messageScore: "消息分",
    messagePositive: "利好",
    messageNegative: "利空",
    messageRisk: "风险",
    noNewsYet: "尚未查询消息面",
    newsUpdating: "消息查询中...",
    newsFailed: "消息查询失败",
    newsSummary: "已查询 {count} 个标的消息面",
    futureBuyPlan: "未来买入计划",
    futureScenarioGeneral: "通用",
    futureScenarioShort: "短线",
    futureScenarioMid: "中期",
    futureScenarioLong: "长线",
    futureScenarioCustom: "自定义",
    customHorizon: "观察天数",
    customPullback: "回撤幅度%",
    customPosition: "计划仓位%",
    futureZone: "关注区间",
    futureHorizon: "观察周期",
    futurePriority: "优先级",
    current_buy_zone: "当前买区",
    pullback_buy_zone: "回落低吸",
    breakout_retest: "突破回踩",
    trend_pullback: "趋势回踩",
    rebuild_after_reclaim: "收复后重建",
    long_accumulate_zone: "长线吸筹",
    custom_buy_zone: "自定义买区",
    wait_cooling: "过热等待",
    invalid_below_stop: "跌破失效",
    priority_high: "高",
    priority_normal: "中",
    priority_low: "低",
    priority_avoid: "回避",
    futureTrigger_current_buy_zone: "价格已在计划买区内，只适合试探仓，不追满仓。",
    futureTrigger_pullback_buy_zone: "等待回落进入计划买区，且不能跌破止损位。",
    futureTrigger_breakout_retest: "突破后等回踩站稳再买；如果大幅跳空高于区间则跳过。",
    futureTrigger_trend_pullback: "只在回踩 MA10 附近并重新收回时加仓。",
    futureTrigger_rebuild_after_reclaim: "只有重新收复 MA20 后才重建仓位，否则继续观察。",
    futureTrigger_long_accumulate_zone: "只在回落到中期均线/核心买区附近时分批吸筹，不追短线波动。",
    futureTrigger_custom_buy_zone: "按自定义回撤幅度等待价格进入区间，触发前不主动买入。",
    futureTrigger_wait_cooling: "避免追高，只在价格冷却到 MA20 附近且盈亏比改善后再看。",
    futureTrigger_invalid_below_stop: "任一日收盘跌破该位置，不再买入。",
  },
  "en-US": {
    eyebrow: "Personal Quant Workbench",
    overviewTitle: "Overview",
    portfolio: "Portfolio",
    language: "Language",
    market: "Market",
    sync: "Sync",
    scan: "Scan",
    newsUpdate: "News",
    refresh: "Refresh",
    refreshPlan: "Refresh Plan",
    latestAutoScan: "Latest Auto Scan",
    candidates: "Candidates",
    noScanYet: "No scan yet",
    rank: "Rank",
    symbol: "Symbol",
    quality: "Quality",
    timing: "Timing",
    stage: "Stage",
    action: "Action",
    position: "Position",
    scoreboard: "Scoreboard",
    latestScores: "Latest Scores",
    coverage: "Coverage",
    watchlists: "Watchlists",
    recentNotes: "Recent Notes",
    journals: "Journals",
    symbolDetail: "Symbol Detail",
    selectSymbol: "Select a symbol",
    latestAssessment: "Latest Assessment",
    tradeSetup: "Trade Setup",
    scoreHistory: "Score History",
    recentBars: "Recent Bars",
    close: "Close",
    volume: "Volume",
    date: "Date",
    noCandidates: "No executable candidates in the latest scan.",
    noScores: "No scores available.",
    noWatchlists: "No watchlists yet.",
    noJournals: "No journals yet.",
    noDetail: "Select a candidate or score card to inspect details.",
    trackedUniverse: "Tracked universe",
    curatedBuckets: "Curated buckets",
    portfolioUsage: "Portfolio usage",
    reserveLeft: "Cash reserve",
    latestScanMetric: "Latest auto scan",
    stable: "Stable",
    syncing: "Syncing...",
    scanning: "Scanning...",
    planGenerating: "Refreshing plan...",
    syncFailed: "Sync failed",
    scanFailed: "Scan failed",
    planFailed: "Plan refresh failed",
    all: "All",
    latestScoreDate: "Score date",
    recommendedPosition: "Suggested position",
    positionAmount: "Suggested amount",
    target: "Target",
    stopLoss: "Stop loss",
    buyZone: "Buy zone",
    riskReward: "Risk/Reward",
    marketLabel: "Market",
    regionLabel: "Region",
    assetLabel: "Asset type",
    latestTradeSetupEmpty: "No trade setup yet",
    cash: "Cash",
    maxSingle: "Max single",
    noPortfolio: "No portfolio",
    noActiveRule: "No active rule",
    chinaMainland: "China Mainland",
    unitedStates: "United States",
    items: "items",
    unknown: "Unknown",
    allowAdd: "Add allowed",
    guardrails: "Guardrails",
    reason: "Reason",
    lastBar: "Latest bar",
    chartWindow: "Window",
    syncSummary: "Synced {ok}/{total} symbols with {failed} failures.",
    scanSummary: "Scan complete with {count} candidates.",
    planSummary: "Trade plan refreshed from latest score.",
    sectorOverweight: "Sector overweight",
    assetOverweight: "Asset overweight",
    noChart: "No chart data yet.",
    yes: "Yes",
    no: "No",
    stageCap: "Stage cap",
    stageRoom: "Stage room",
    currentPosition: "Current position",
    riskBudget: "Risk budget",
    riskShare: "Risk per share",
    shareCap: "Risk-limited shares",
    openTrigger: "Open trigger",
    addTrigger: "Add trigger",
    stopTrigger: "Stop trigger",
    trimTrigger: "Trim trigger",
    tranchePlan: "Tranche plan",
    tranchePct: "Position slice",
    trigger: "Trigger",
    notes: "Execution notes",
    ma10: "MA10",
    ma20: "MA20",
    markerBuy: "Buy",
    markerStop: "Stop",
    markerTarget: "Target",
    simAccount: "Sim Account",
    accountSummary: "Account Summary",
    accountEquity: "Total equity",
    accountCash: "Available cash",
    accountMarketValue: "Market value",
    accountRealizedPnl: "Realized PnL",
    accountUnrealizedPnl: "Unrealized PnL",
    accountInvested: "Invested",
    activeTrades: "Trades (7d)",
    lastTrade: "Last trade",
    noTrades: "No simulated trades yet.",
    recentTrades: "Recent Trades",
    simBuy: "Sim Buy",
    simSell: "Sim Sell",
    orderQty: "Qty",
    orderPrice: "Price",
    buySide: "Buy",
    sellSide: "Sell",
    orderSummary: "Simulated {side} filled for {symbol} {quantity} shares at {price}.",
    orderFailed: "Simulated order failed",
    holdingQty: "Holding qty",
    avgCost: "Avg cost",
    todayOpportunities: "Today Opportunities",
    todayMethod: "Auto scan + scores + buy zones + news",
    todayExecutable: "Executable",
    todayWatch: "Watch Queue",
    todayMessages: "News",
    messageScore: "News score",
    messagePositive: "Positive",
    messageNegative: "Negative",
    messageRisk: "Risk",
    noNewsYet: "No news query yet",
    newsUpdating: "Checking news...",
    newsFailed: "News update failed",
    newsSummary: "Checked news for {count} symbols",
    futureBuyPlan: "Future Buy Plan",
    futureScenarioGeneral: "General",
    futureScenarioShort: "Short",
    futureScenarioMid: "Mid",
    futureScenarioLong: "Long",
    futureScenarioCustom: "Custom",
    customHorizon: "Horizon days",
    customPullback: "Pullback %",
    customPosition: "Position %",
    futureZone: "Watch zone",
    futureHorizon: "Horizon",
    futurePriority: "Priority",
    current_buy_zone: "Current zone",
    pullback_buy_zone: "Pullback",
    breakout_retest: "Breakout retest",
    trend_pullback: "Trend pullback",
    rebuild_after_reclaim: "Rebuild",
    long_accumulate_zone: "Long accumulate",
    custom_buy_zone: "Custom zone",
    wait_cooling: "Wait cooling",
    invalid_below_stop: "Invalid below stop",
    priority_high: "High",
    priority_normal: "Normal",
    priority_low: "Low",
    priority_avoid: "Avoid",
    futureTrigger_current_buy_zone: "Price is already inside the planned zone; starter size only.",
    futureTrigger_pullback_buy_zone: "Wait for pullback into the planned buy zone without breaking the stop.",
    futureTrigger_breakout_retest: "Buy only after breakout and retest hold; skip if it gaps far above the zone.",
    futureTrigger_trend_pullback: "Add only if price pulls back near MA10 and closes back above it.",
    futureTrigger_rebuild_after_reclaim: "Only rebuild after reclaiming MA20; otherwise keep observing.",
    futureTrigger_long_accumulate_zone: "Accumulate only near the mid-term average/core buy zone; avoid chasing short-term moves.",
    futureTrigger_custom_buy_zone: "Wait for price to enter the custom pullback zone before buying.",
    futureTrigger_wait_cooling: "Avoid chasing; revisit only after price cools near MA20 and risk/reward improves.",
    futureTrigger_invalid_below_stop: "No buy if daily close breaks this level.",
  },
};

const EXTRA_I18N = {
  "zh-CN": {
    recentViewed: "最近查看",
    zoomIn: "放大",
    zoomOut: "缩小",
    resetZoom: "重置",
    barsUnit: "根",
  },
  "en-US": {
    recentViewed: "Recent Viewed",
    zoomIn: "Zoom In",
    zoomOut: "Zoom Out",
    resetZoom: "Reset",
    barsUnit: "bars",
  },
};

Object.assign(EXTRA_I18N["zh-CN"], {
  recentViewed: "最近查看",
  zoomIn: "放大",
  zoomOut: "缩小",
  resetZoom: "重置",
  barsUnit: "根",
  scenarioPreview: "收益预演",
  expectedCase: "估准值",
  optimisticCase: "最乐观",
  pessimisticCase: "最悲观",
  estimateConfidence: "估算把握",
  estimateHorizon: "观察周期",
  referencePrice: "参考成本",
  projectedValue: "到手市值",
  projectedProfit: "预估盈亏",
  plannedOrder: "预演仓位",
  noScenarioEstimate: "暂无收益预演",
  daysUnit: "天",
});

Object.assign(EXTRA_I18N["zh-CN"], {
  expandChart: "放大视图",
  collapseChart: "收起视图",
  chartDragHint: "拖拽框选区间放大，双击恢复，滚轮微调窗口",
});

Object.assign(EXTRA_I18N["zh-CN"], {
  chartDaily: "日线",
  chartWeekly: "周线",
});

Object.assign(EXTRA_I18N["en-US"], {
  expandChart: "Expand",
  collapseChart: "Collapse",
  chartDragHint: "Drag to zoom a range, double-click to reset, use wheel for quick zoom.",
  chartDaily: "Daily",
  chartWeekly: "Weekly",
  scenarioPreview: "Return Preview",
  expectedCase: "Expected",
  optimisticCase: "Optimistic",
  pessimisticCase: "Pessimistic",
  estimateConfidence: "Confidence",
  estimateHorizon: "Horizon",
  referencePrice: "Reference",
  projectedValue: "Exit value",
  projectedProfit: "PnL",
  plannedOrder: "Preview size",
  noScenarioEstimate: "No return preview yet.",
  daysUnit: "d",
});

Object.assign(EXTRA_I18N["zh-CN"], {
  similarSignalStats: "历史相似信号",
  similarSamples: "样本",
  matchedSignals: "匹配信号",
  win5d: "5日胜率",
  win20d: "20日胜率",
  avgReturn20d: "20日均值",
  maxGain20d: "平均最大收益",
  maxDrawdown20d: "平均最大回撤",
  best20d: "最佳20日",
  worst20d: "最差20日",
  sampleInsufficient: "历史样本不足，先观察，不建议过度相信单次信号",
});

Object.assign(EXTRA_I18N["en-US"], {
  similarSignalStats: "Similar Signal History",
  similarSamples: "Samples",
  matchedSignals: "Matched",
  win5d: "5d win",
  win20d: "20d win",
  avgReturn20d: "20d avg",
  maxGain20d: "Avg max gain",
  maxDrawdown20d: "Avg max drawdown",
  best20d: "Best 20d",
  worst20d: "Worst 20d",
  sampleInsufficient: "Not enough history yet. Watch first and avoid over-trusting one signal.",
});

Object.assign(EXTRA_I18N["zh-CN"], {
  symbolCode: "代码",
  addSymbol: "添加标的",
  addingSymbol: "添加中...",
  symbolAdded: "已加入观察池：{symbol}",
  symbolAddFailed: "添加标的失败",
  symbolCodeRequired: "请输入标的代码",
  openList: "打开列表",
  listActions: "操作栏",
  viewDetail: "查看详情",
  addToWatchlist: "加入观察池",
  addedToWatchlist: "已加入观察池",
  syncSelected: "同步当前标的",
  scanSelected: "扫描当前标的",
  generatePlan: "生成交易计划",
  openWatchlist: "打开观察池",
  addActiveSymbol: "加入当前详情标的",
  noSelection: "先从左侧列表选择一项",
  symbolList: "标的列表",
  watchlistListTitle: "观察池列表",
  candidateList: "候选池列表",
  modalEmpty: "暂无可操作数据",
  ruleConfig: "规则配置",
  signalRuleKicker: "信号匹配",
  qualityTolerance: "股质容差",
  timingTolerance: "时点容差",
  minSamples: "最小样本数",
  maxSamples: "最大样本数",
  sameRegion: "同市场区域",
  sameAsset: "同资产类型",
  sameStage: "同阶段",
  sameAction: "同动作",
  saveRule: "保存规则",
  savingRule: "保存中...",
  ruleSaved: "相似信号规则已保存",
  activeSignalRule: "当前规则",
  qualityToleranceTip: "越大越容易匹配到历史样本，但相似度会变弱；越小越严格，样本可能不足。",
  timingToleranceTip: "控制时点分接近程度。调大能找到更多机会样本，调小更适合验证非常相似的买卖点。",
  minSamplesTip: "低于这个数量就提示样本不足。保守使用建议 10，快速观察可用 3-5。",
  maxSamplesTip: "最多纳入多少条历史样本。越大越平滑，但太老或不够相似的样本也会进入统计。",
  sameRegionTip: "开启后只比较同一市场区域，比如美股只和美股比；关闭会跨市场找更多样本。",
  sameAssetTip: "开启后股票只和股票比、ETF 只和 ETF 比；关闭会增加样本，但资产特征可能混杂。",
  sameStageTip: "开启后只匹配同阶段信号，比如启动只和启动比；关闭会放宽趋势状态。",
  sameActionTip: "开启后只匹配同动作，比如开仓只和开仓比；关闭能增加样本，但交易意图会更混。",
  rulePreviewIdle: "选择一个标的后，可以预览当前规则影响。",
  rulePreview: "规则体检",
  previewSamples: "有效样本",
  previewMatched: "匹配信号",
  previewWin20d: "20日胜率",
  previewAvg20d: "20日均值",
  previewDrawdown: "平均回撤",
  previewDiagnosis: "诊断",
});

Object.assign(EXTRA_I18N["en-US"], {
  symbolCode: "Code",
  addSymbol: "Add Symbol",
  addingSymbol: "Adding...",
  symbolAdded: "Added to watchlist: {symbol}",
  symbolAddFailed: "Add symbol failed",
  symbolCodeRequired: "Please enter a symbol code",
  openList: "Open List",
  listActions: "Actions",
  viewDetail: "View Detail",
  addToWatchlist: "Add to Watchlist",
  addedToWatchlist: "Added to watchlist",
  syncSelected: "Sync Selected",
  scanSelected: "Scan Selected",
  generatePlan: "Generate Plan",
  openWatchlist: "Open Watchlist",
  addActiveSymbol: "Add Active Symbol",
  noSelection: "Select an item from the list first",
  symbolList: "Symbol List",
  watchlistListTitle: "Watchlist List",
  candidateList: "Candidate List",
  modalEmpty: "No actionable data yet",
  ruleConfig: "Rule Config",
  signalRuleKicker: "Signal Matching",
  qualityTolerance: "Quality tolerance",
  timingTolerance: "Timing tolerance",
  minSamples: "Min samples",
  maxSamples: "Max samples",
  sameRegion: "Same region",
  sameAsset: "Same asset",
  sameStage: "Same stage",
  sameAction: "Same action",
  saveRule: "Save Rule",
  savingRule: "Saving...",
  ruleSaved: "Similar signal rule saved",
  activeSignalRule: "Active rule",
  qualityToleranceTip: "Higher values find more samples but weaker similarity. Lower values are stricter and may produce too few samples.",
  timingToleranceTip: "Controls how close timing scores must be. Higher values find more opportunity samples; lower values validate very similar entries.",
  minSamplesTip: "Below this count, the signal is marked as insufficient. Conservative use: 10. Quick watch: 3-5.",
  maxSamplesTip: "Maximum historical samples included. Higher values smooth results but can include older or less similar cases.",
  sameRegionTip: "When enabled, only compares the same market region. Disabling it finds more cross-market samples.",
  sameAssetTip: "When enabled, stocks compare with stocks and ETFs with ETFs. Disabling it increases samples but mixes asset behavior.",
  sameStageTip: "When enabled, only matches the same signal stage. Disabling it relaxes trend-state matching.",
  sameActionTip: "When enabled, only matches the same action. Disabling it adds samples but mixes trading intent.",
  rulePreviewIdle: "Select a symbol to preview rule impact.",
  rulePreview: "Rule Preview",
  previewSamples: "Samples",
  previewMatched: "Matched",
  previewWin20d: "20d win",
  previewAvg20d: "20d avg",
  previewDrawdown: "Avg drawdown",
  previewDiagnosis: "Diagnosis",
});

const STAGE_LABELS = {
  "zh-CN": { accel: "加速", cooldown: "冷却", overheat: "过热", start: "启动" },
  "en-US": { accel: "Accel", cooldown: "Cooldown", overheat: "Overheat", start: "Start" },
};

const ACTION_LABELS = {
  "zh-CN": { buy_dip: "低吸", exit: "退出", hold: "持有", open: "开仓", reduce: "减仓" },
  "en-US": { buy_dip: "Buy Dip", exit: "Exit", hold: "Hold", open: "Open", reduce: "Reduce" },
};

const ASSET_LABELS = {
  "zh-CN": { etf: "ETF", stock: "股票" },
  "en-US": { etf: "ETF", stock: "Stock" },
};

const WATCHLIST_TYPE_LABELS = {
  "zh-CN": { custom: "自定义", eliminate: "排除", trade: "交易", watch: "观察" },
  "en-US": { custom: "Custom", eliminate: "Eliminate", trade: "Trade", watch: "Watch" },
};

const SIGNAL_STYLE = {
  "buy-zone": { color: "#0f766e", dash: "0" },
  stop: { color: "#b42318", dash: "6 4" },
  target: { color: "#7c3aed", dash: "6 4" },
};

const FUTURE_PLAN_STYLE = {
  avoid: { color: "#b42318", fill: "rgba(180, 35, 24, 0.12)", dash: "6 4" },
  high: { color: "#0f766e", fill: "rgba(15, 118, 110, 0.16)", dash: "0" },
  low: { color: "#2563eb", fill: "rgba(37, 99, 235, 0.11)", dash: "4 4" },
  normal: { color: "#0f766e", fill: "rgba(15, 118, 110, 0.10)", dash: "4 4" },
};

const state = {
  portfolios: [],
  portfolioId: null,
  locale: "zh-CN",
  marketGroup: "all",
  activeSymbolId: null,
  workbench: null,
  detail: null,
  detailCache: {},
  detailOrder: [],
  activeWatchlistId: null,
  watchlistItems: {},
  symbolDirectory: {},
  chartTimeframe: "daily",
  chartWindowSize: DEFAULT_CHART_WINDOW,
  chartRange: null,
  chartExpanded: false,
  chartView: null,
  chartCleanup: null,
  metricModal: { type: null, items: [], selectedId: null },
  signalRulePresets: [],
  signalRule: null,
  signalRulePreviewTimer: null,
  futurePlanScenario: "general",
  futurePlanCustom: { horizonDays: 20, pullbackPct: 3, positionPct: 5 },
  newsSnapshot: null,
  status: { level: "", message: "" },
};

function t(key) {
  return I18N[state.locale]?.[key] ?? EXTRA_I18N[state.locale]?.[key] ?? EXTRA_I18N["en-US"]?.[key] ?? key;
}

function template(key, params = {}) {
  return t(key).replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? ""));
}

function percent(value) {
  if (value === null || value === undefined) return "-";
  return `${(value * 100).toFixed(1)}%`;
}

function score(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(digits);
}

function money(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const currency = state.workbench?.portfolio?.currency || "CNY";
  try {
    return new Intl.NumberFormat(state.locale, {
      style: "currency",
      currency,
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  } catch {
    return new Intl.NumberFormat(state.locale, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  }
}

function compactNumber(value) {
  if (value === null || value === undefined) return "-";
  return new Intl.NumberFormat(state.locale, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function formatDate(value) {
  if (!value) return "-";
  return new Date(value).toLocaleString(state.locale);
}

function badgeClass(value) {
  if (value === "overheat" || value === "exit" || value === "reduce") return "badge danger";
  if (value === "cooldown" || value === "hold") return "badge warn";
  return "badge";
}

function sideBadgeClass(side) {
  return side === "sell" ? "badge warn" : "badge";
}

function pnlClass(value) {
  if (value > 0) return "pnl-positive";
  if (value < 0) return "pnl-negative";
  return "";
}

function stageLabel(value) {
  return STAGE_LABELS[state.locale]?.[value] ?? value ?? "-";
}

function actionLabel(value) {
  return ACTION_LABELS[state.locale]?.[value] ?? value ?? "-";
}

function assetTypeLabel(value) {
  return ASSET_LABELS[state.locale]?.[value] ?? value ?? t("unknown");
}

function watchlistTypeLabel(value) {
  return WATCHLIST_TYPE_LABELS[state.locale]?.[value] ?? value ?? t("unknown");
}

function futureBuyLabel(value) {
  return t(value) === value ? value ?? "-" : t(value);
}

function futurePriorityLabel(value) {
  const key = `priority_${value}`;
  return t(key) === key ? value ?? "-" : t(key);
}

function futureTriggerLabel(plan) {
  const key = `futureTrigger_${plan?.label}`;
  return t(key) === key ? plan?.trigger ?? "-" : t(key);
}

function planWithRatio(plan, ratio) {
  return {
    ...plan,
    position_pct: Number((Number(plan.position_pct || 0) * ratio).toFixed(4)),
    amount: Number((Number(plan.amount || 0) * ratio).toFixed(2)),
  };
}

function invalidFuturePlan(setup) {
  return (setup?.future_buy_plan ?? []).find((plan) => plan.priority === "avoid" || plan.label === "invalid_below_stop");
}

function buildLongFuturePlan(setup) {
  if (!setup) return [];
  const ma20 = setup.moving_averages?.ma20;
  const anchor = Number(ma20 || setup.entry_min || setup.entry_max || 0);
  if (!(anchor > 0)) return setup.future_buy_plan ?? [];
  const positionPct = Math.max(Number(setup.recommended_position_pct || 0) * 0.5, 0);
  const amount = Number(setup.recommended_position_amount || 0) * 0.5;
  const plans = [
    {
      label: "long_accumulate_zone",
      horizon_days: 30,
      zone_min: Number((anchor * 0.97).toFixed(2)),
      zone_max: Number((anchor * 1.02).toFixed(2)),
      priority: "low",
      position_pct: Number(positionPct.toFixed(4)),
      amount: Number(amount.toFixed(2)),
      trigger: "",
    },
  ];
  const invalid = invalidFuturePlan(setup);
  if (invalid) plans.push(invalid);
  return plans;
}

function buildCustomFuturePlan(setup) {
  if (!setup) return [];
  const custom = state.futurePlanCustom;
  const base = Number(setup.entry_max || setup.entry_min || setup.moving_averages?.ma20 || 0);
  if (!(base > 0)) return setup.future_buy_plan ?? [];
  const pullback = Math.max(0, Number(custom.pullbackPct || 0)) / 100;
  const zoneMax = base * (1 - pullback);
  const zoneMin = zoneMax * 0.985;
  const positionPct = Math.max(0, Number(custom.positionPct || 0)) / 100;
  const amount = Number(state.workbench?.portfolio?.total_capital || 0) * Number(state.workbench?.portfolio?.investable_ratio || 1) * positionPct;
  const plans = [
    {
      label: "custom_buy_zone",
      horizon_days: Math.max(1, Number(custom.horizonDays || 20)),
      zone_min: Number(zoneMin.toFixed(2)),
      zone_max: Number(zoneMax.toFixed(2)),
      priority: "normal",
      position_pct: Number(positionPct.toFixed(4)),
      amount: Number(amount.toFixed(2)),
      trigger: "",
    },
  ];
  const invalid = invalidFuturePlan(setup);
  if (invalid) plans.push(invalid);
  return plans;
}

function getActiveFutureBuyPlan(setup) {
  const basePlan = setup?.future_buy_plan ?? [];
  if (state.futurePlanScenario === "short") {
    return basePlan
      .filter((plan) => plan.priority === "high" || plan.horizon_days <= 5 || plan.priority === "avoid")
      .map((plan) => (plan.priority === "avoid" ? plan : planWithRatio(plan, 0.7)));
  }
  if (state.futurePlanScenario === "long") {
    return buildLongFuturePlan(setup);
  }
  if (state.futurePlanScenario === "custom") {
    return buildCustomFuturePlan(setup);
  }
  if (state.futurePlanScenario === "mid") {
    const midPlans = basePlan
      .filter((plan) => plan.priority !== "high" || plan.horizon_days >= 5 || plan.priority === "avoid")
      .map((plan) => (plan.priority === "avoid" ? plan : planWithRatio(plan, 0.9)));
    return midPlans.length ? midPlans : basePlan;
  }
  return basePlan;
}

function sideLabel(value) {
  if (value === "buy") return t("buySide");
  if (value === "sell") return t("sellSide");
  return value ?? "-";
}

function regionShortLabel(value) {
  if (value === "cn") return "CN";
  if (value === "us") return "US";
  return value || "-";
}

function regionLongLabel(value) {
  if (value === "cn") return t("chinaMainland");
  if (value === "us") return t("unitedStates");
  return value || t("unknown");
}

function joinParts(parts) {
  return parts.filter(Boolean).join(DOT);
}

function signalLabel(signal) {
  if (signal.kind === "buy-zone") return t("markerBuy");
  if (signal.kind === "stop") return t("markerStop");
  if (signal.kind === "target") return t("markerTarget");
  if (signal.label === "MA10") return t("ma10");
  if (signal.label === "MA20") return t("ma20");
  return signal.label;
}

function lotSizeForDetail(detail) {
  return detail?.symbol?.region === "cn" ? 100 : 1;
}

function computeSuggestedPrice(detail) {
  const setup = detail?.latest_trade_setup;
  const lastBar = detail?.bars?.[detail.bars.length - 1];
  return setup?.entry_min ?? lastBar?.close ?? detail?.position?.latest_price ?? 0;
}

function computeSuggestedBuyQuantity(detail) {
  const price = computeSuggestedPrice(detail);
  const lotSize = lotSizeForDetail(detail);
  const budget =
    detail?.latest_trade_setup?.tranche_plan?.[0]?.amount ??
    detail?.latest_trade_setup?.remaining_stage_amount ??
    detail?.latest_trade_setup?.recommended_position_amount ??
    0;
  if (!(price > 0) || !(budget > 0)) return 0;
  return Math.floor(budget / price / lotSize) * lotSize;
}

function computeDefaultSellQuantity(detail) {
  return Math.floor(detail?.position?.quantity ?? 0);
}

function syncOrderForm(detail) {
  const qtyInput = document.getElementById("simQuantityInput");
  const priceInput = document.getElementById("simPriceInput");
  if (!detail) {
    qtyInput.value = "";
    priceInput.value = "";
    renderOrderScenarioPreview(null);
    return;
  }
  const suggestedQty = computeSuggestedBuyQuantity(detail);
  const suggestedPrice = computeSuggestedPrice(detail);
  qtyInput.value = suggestedQty > 0 ? String(suggestedQty) : "";
  priceInput.value = suggestedPrice > 0 ? score(suggestedPrice) : "";
  renderOrderScenarioPreview(detail);
}

function renderOrderScenarioPreview(detail) {
  const container = document.getElementById("orderScenarioPreview");
  const scenarios = detail?.latest_trade_setup?.return_scenarios;
  if (!detail || !scenarios) {
    setEmpty(container, t("noScenarioEstimate"));
    return;
  }

  const qtyInput = document.getElementById("simQuantityInput");
  const priceInput = document.getElementById("simPriceInput");
  const quantity =
    Number(qtyInput.value) > 0 ? Number(qtyInput.value) : Number(scenarios.planned_order?.quantity ?? 0);
  const entryPrice =
    Number(priceInput.value) > 0 ? Number(priceInput.value) : Number(scenarios.reference_price ?? computeSuggestedPrice(detail));
  const plannedAmount = quantity > 0 && entryPrice > 0 ? quantity * entryPrice : Number(scenarios.planned_order?.amount ?? 0);

  const buildScenarioBox = (labelKey, scenario) => {
    const exitPrice = Number(scenario?.exit_price ?? entryPrice);
    const returnPct = entryPrice > 0 ? (exitPrice - entryPrice) / entryPrice : 0;
    const pnlAmount = quantity > 0 ? (exitPrice - entryPrice) * quantity : 0;
    const exitValue = quantity > 0 ? exitPrice * quantity : 0;
    const pnlClass = pnlAmount > 0 ? "pnl-positive" : pnlAmount < 0 ? "pnl-negative" : "";
    return `
      <article class="scenario-box">
        <strong>${t(labelKey)}</strong>
        <div class="metric-value ${pnlClass}">${percent(returnPct)}</div>
        <div class="item-subline">${t("projectedProfit")}: <span class="${pnlClass}">${money(pnlAmount, 0)}</span></div>
        <div class="item-subline">${t("projectedValue")}: ${money(exitValue, 0)}</div>
        <div class="item-subline">${t("target")}: ${score(exitPrice)}</div>
      </article>
    `;
  };

  container.innerHTML = `
    <article class="scenario-preview-card">
      <div class="scenario-preview-head">
        <strong>${t("scenarioPreview")}</strong>
        <span class="scenario-preview-meta">${joinParts([
          `${t("plannedOrder")}: ${quantity || 0}`,
          `${t("referencePrice")}: ${score(entryPrice)}`,
          `${t("positionAmount")}: ${money(plannedAmount, 0)}`,
        ])}</span>
      </div>
      <div class="scenario-preview-meta">${joinParts([
        `${t("estimateConfidence")}: ${score(scenarios.confidence_pct, 1)}%`,
        `${t("estimateHorizon")}: ${scenarios.horizon_days}${t("daysUnit")}`,
      ])}</div>
      <div class="scenario-grid">
        ${buildScenarioBox("expectedCase", scenarios.expected)}
        ${buildScenarioBox("optimisticCase", scenarios.optimistic)}
        ${buildScenarioBox("pessimisticCase", scenarios.pessimistic)}
      </div>
    </article>
  `;
}

function formatOpenTrigger(item) {
  if (item.entry_min === null || item.entry_max === null) return "-";
  return state.locale === "zh-CN"
    ? `仅在 ${item.entry_min} - ${item.entry_max} 区间内开仓`
    : `Open only inside ${item.entry_min} - ${item.entry_max}`;
}

function formatAddTrigger(item) {
  if (!item.allow_add_position) return "-";
  const ma10 = item.moving_averages?.ma10 ?? "-";
  if (state.locale === "zh-CN") {
    return item.stage === "accel"
      ? `收盘重新站上 MA10 ${ma10}，且盈亏比仍大于 1.5`
      : "仅在趋势确认后再加仓";
  }
  return item.stage === "accel"
    ? `Add after reclaiming MA10 ${ma10} while risk/reward stays above 1.5`
    : "Add only after trend confirmation";
}

function formatStopTrigger(item) {
  if (item.stop_loss === null || item.stop_loss === undefined) return "-";
  return state.locale === "zh-CN"
    ? `任一日收盘跌破 ${item.stop_loss}`
    : `Any daily close below ${item.stop_loss}`;
}

function formatTrimTrigger(item) {
  if (item.target_price === null || item.target_price === undefined) return "-";
  const ma20 = item.moving_averages?.ma20 ?? "-";
  return state.locale === "zh-CN"
    ? `触及 ${item.target_price} 附近止盈，或价格显著高于 MA20 ${ma20}`
    : `Trim into ${item.target_price} or when price stretches too far above MA20 ${ma20}`;
}

function setEmpty(container, message) {
  container.innerHTML = `<div class="empty">${message}</div>`;
}

function renderToolbarOptions() {
  const localeSelect = document.getElementById("localeSelect");
  const marketSelect = document.getElementById("marketSelect");

  localeSelect.innerHTML = `
    <option value="zh-CN">简体中文</option>
    <option value="en-US">English</option>
  `;
  localeSelect.value = state.locale;

  marketSelect.innerHTML = `
    <option value="all">${t("all")}</option>
    <option value="cn">${t("chinaMainland")}</option>
    <option value="us">${t("unitedStates")}</option>
  `;
  marketSelect.value = state.marketGroup;
}

function renderStatus() {
  const node = document.getElementById("actionStatus");
  node.className = `action-status ${state.status.level || ""}`.trim();
  node.textContent = state.status.message;
}

function setStatus(level, message) {
  state.status = { level, message };
  renderStatus();
}

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-tip-i18n]").forEach((node) => {
    node.dataset.tip = t(node.dataset.tipI18n);
  });
  renderToolbarOptions();
  renderStatus();
  document.documentElement.lang = state.locale;
  document.title = t("eyebrow");
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || payload.message || response.statusText);
  return payload;
}

function signalRulePresetPayload(preset) {
  return {
    rule_name: preset.rule_name,
    mode: preset.mode,
    quality_tolerance: preset.quality_tolerance,
    timing_tolerance: preset.timing_tolerance,
    min_sample_count: preset.min_sample_count,
    max_samples: preset.max_samples,
    same_region: preset.same_region,
    same_asset_type: preset.same_asset_type,
    same_stage: preset.same_stage,
    same_action: preset.same_action,
  };
}

function activeSignalRuleMode() {
  return state.signalRule?.mode ?? "balanced";
}

function renderSignalRuleSummary() {
  const node = document.getElementById("signalRuleSummary");
  const rule = state.signalRule;
  if (!rule) {
    node.textContent = "-";
    return;
  }
  node.textContent = `${t("activeSignalRule")}: ${rule.rule_name}${DOT}${t("qualityTolerance")} ${rule.quality_tolerance}${DOT}${t("timingTolerance")} ${rule.timing_tolerance}${DOT}${t("minSamples")} ${rule.min_sample_count}`;
}

function setSignalRuleForm(rule) {
  document.getElementById("qualityToleranceInput").value = rule.quality_tolerance;
  document.getElementById("timingToleranceInput").value = rule.timing_tolerance;
  document.getElementById("minSampleInput").value = rule.min_sample_count;
  document.getElementById("maxSamplesInput").value = rule.max_samples;
  document.getElementById("sameRegionInput").checked = Boolean(rule.same_region);
  document.getElementById("sameAssetInput").checked = Boolean(rule.same_asset_type);
  document.getElementById("sameStageInput").checked = Boolean(rule.same_stage);
  document.getElementById("sameActionInput").checked = Boolean(rule.same_action);
}

function renderSignalPresetButtons() {
  const container = document.getElementById("signalPresetButtons");
  const mode = activeSignalRuleMode();
  container.innerHTML = state.signalRulePresets
    .map(
      (preset) => `
        <button type="button" class="preset-button ${preset.mode === mode ? "active" : ""}" data-signal-preset="${preset.mode}">
          <span class="preset-title">
            <strong>${preset.rule_name}</strong>
            <span class="tip-icon" data-tip="${preset.description}">?</span>
          </span>
          <span class="item-subline">${preset.description}</span>
        </button>
      `
    )
    .join("");
  container.querySelectorAll("[data-signal-preset]").forEach((button) => {
    button.addEventListener("click", () => applySignalPreset(button.dataset.signalPreset));
  });
}

function renderSignalRuleConfig() {
  renderSignalPresetButtons();
  if (state.signalRule) {
    setSignalRuleForm(state.signalRule);
  }
  renderSignalRuleSummary();
}

function applySignalPreset(mode) {
  const preset = state.signalRulePresets.find((item) => item.mode === mode);
  if (!preset) return;
  state.signalRule = {
    ...signalRulePresetPayload(preset),
    id: state.signalRule?.id ?? null,
    portfolio_id: state.portfolioId,
    is_active: true,
  };
  renderSignalRuleConfig();
  scheduleSignalRulePreview();
}

function collectSignalRuleForm() {
  const mode = activeSignalRuleMode();
  const preset = state.signalRulePresets.find((item) => item.mode === mode);
  const fallbackName = mode === "expert" ? t("ruleConfig") : preset?.rule_name ?? "balanced";
  return {
    rule_name: state.signalRule?.rule_name ?? fallbackName,
    mode,
    quality_tolerance: Number(document.getElementById("qualityToleranceInput").value),
    timing_tolerance: Number(document.getElementById("timingToleranceInput").value),
    min_sample_count: Number(document.getElementById("minSampleInput").value),
    max_samples: Number(document.getElementById("maxSamplesInput").value),
    same_region: document.getElementById("sameRegionInput").checked,
    same_asset_type: document.getElementById("sameAssetInput").checked,
    same_stage: document.getElementById("sameStageInput").checked,
    same_action: document.getElementById("sameActionInput").checked,
  };
}

function markSignalRuleExpert() {
  if (!state.signalRule || activeSignalRuleMode() === "expert") return;
  state.signalRule = {
    ...collectSignalRuleForm(),
    id: state.signalRule.id,
    portfolio_id: state.portfolioId,
    rule_name: "专家模式",
    mode: "expert",
    is_active: true,
  };
  renderSignalPresetButtons();
  renderSignalRuleSummary();
}

async function loadSignalRuleConfig() {
  if (!state.portfolioId) return;
  const [presets, rule] = await Promise.all([
    requestJson("/api/v1/signal-rules/presets"),
    requestJson(`/api/v1/portfolios/${state.portfolioId}/signal-rule`),
  ]);
  state.signalRulePresets = presets;
  state.signalRule = rule;
  renderSignalRuleConfig();
  scheduleSignalRulePreview(0);
}

async function saveSignalRule() {
  if (!state.portfolioId) return;
  const payload = collectSignalRuleForm();
  const saved = await requestJson(`/api/v1/portfolios/${state.portfolioId}/signal-rule`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  state.signalRule = saved;
  renderSignalRuleConfig();
  if (state.activeSymbolId) {
    await loadSymbolDetail(state.activeSymbolId);
  }
  setStatus("success", t("ruleSaved"));
}

function renderSignalRulePreview(result = null) {
  const container = document.getElementById("signalRulePreview");
  if (!state.activeSymbolId) {
    container.className = "rule-preview";
    container.innerHTML = `<div class="item-subline">${t("rulePreviewIdle")}</div>`;
    return;
  }
  if (!result) {
    container.className = "rule-preview";
    container.innerHTML = `<div class="item-subline">${t("rulePreview")}...</div>`;
    return;
  }
  const stats = result.stats;
  container.className = `rule-preview ${result.status || ""}`.trim();
  if (!stats) {
    container.innerHTML = `
      <div class="modal-action-title">
        <strong>${result.symbol ? `${result.symbol}${DOT}${result.name}` : t("rulePreview")}</strong>
        <span class="item-subline">${result.message}</span>
      </div>
    `;
    return;
  }
  const minSamples = stats.min_sample_count ?? stats.scope?.min_sample_count ?? "-";
  container.innerHTML = `
    <div class="modal-action-title">
      <strong>${t("rulePreview")}${DOT}${result.symbol}${DOT}${result.name}</strong>
      <span class="item-subline">${t("previewDiagnosis")}: ${result.message}</span>
    </div>
    <div class="rule-preview-grid">
      <div class="rule-preview-stat">
        <span class="metric-label">${t("previewSamples")}</span>
        <strong>${stats.sample_count ?? 0}/${minSamples}</strong>
      </div>
      <div class="rule-preview-stat">
        <span class="metric-label">${t("previewMatched")}</span>
        <strong>${stats.matched_count ?? 0}</strong>
      </div>
      <div class="rule-preview-stat">
        <span class="metric-label">${t("previewWin20d")}</span>
        <strong>${statPct(stats.win_rate_20d)}</strong>
      </div>
      <div class="rule-preview-stat">
        <span class="metric-label">${t("previewAvg20d")}</span>
        <strong class="${pnlClass(stats.avg_return_20d)}">${statPct(stats.avg_return_20d)}</strong>
      </div>
      <div class="rule-preview-stat">
        <span class="metric-label">${t("previewDrawdown")}</span>
        <strong class="${pnlClass(stats.avg_max_drawdown_20d)}">${statPct(stats.avg_max_drawdown_20d)}</strong>
      </div>
    </div>
  `;
}

async function loadSignalRulePreview() {
  if (!state.portfolioId || !state.signalRule) return;
  if (!state.activeSymbolId) {
    renderSignalRulePreview();
    return;
  }
  renderSignalRulePreview(null);
  const payload = {
    ...collectSignalRuleForm(),
    symbol_id: state.activeSymbolId,
  };
  const result = await requestJson(`/api/v1/portfolios/${state.portfolioId}/signal-rule/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  renderSignalRulePreview(result);
}

function scheduleSignalRulePreview(delay = 350) {
  if (state.signalRulePreviewTimer) {
    clearTimeout(state.signalRulePreviewTimer);
  }
  state.signalRulePreviewTimer = setTimeout(() => {
    loadSignalRulePreview().catch((error) => {
      document.getElementById("signalRulePreview").innerHTML = `<div class="item-subline">${error.message}</div>`;
    });
  }, delay);
}

function renderMetrics(data) {
  const grid = document.getElementById("metricGrid");
  const marketScope = data.market_scope ?? {};
  const activeRule = data.active_rule;
  const metrics = [
    {
      label: t("symbol"),
      value: marketScope.filtered_symbols ?? data.overview.symbols_count,
      note: `${t("market")}: ${state.marketGroup === "all" ? t("all") : regionLongLabel(state.marketGroup)}`,
      modalType: "symbols",
    },
    { label: t("watchlists"), value: data.overview.watchlists_count, note: t("curatedBuckets"), modalType: "watchlists" },
    { label: t("position"), value: percent(data.overview.total_position_pct), note: t("portfolioUsage") },
    { label: t("cash"), value: percent(data.overview.cash_pct), note: t("reserveLeft") },
    { label: t("candidates"), value: data.latest_scan.executable_count, note: t("latestScanMetric"), modalType: "candidates" },
    {
      label: t("maxSingle"),
      value: activeRule ? percent(activeRule.max_single_position_pct) : "-",
      note: activeRule?.rule_name ?? t("noActiveRule"),
    },
  ];

  grid.innerHTML = metrics
    .map(
      (item) => `
      <article class="metric-card ${item.modalType ? "metric-action" : ""}" ${
        item.modalType ? `role="button" tabindex="0" data-metric-type="${item.modalType}" aria-label="${item.label} ${t("openList")}"` : ""
      }>
        <div class="metric-label">${item.label}</div>
        <div class="metric-value">${item.value}</div>
        <div class="metric-note">${item.note}</div>
      </article>
    `
    )
    .join("");

  grid.querySelectorAll("[data-metric-type]").forEach((card) => {
    card.addEventListener("click", () => openMetricModal(card.dataset.metricType));
    card.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      openMetricModal(card.dataset.metricType);
    });
  });
}

function renderTodayList(items, emptyText) {
  if (!items.length) return `<div class="empty">${emptyText}</div>`;
  return items
    .map(
      (item) => `
        <button type="button" class="today-item" data-symbol-id="${item.symbol_id}">
          <span>
            <strong>${item.symbol}${DOT}${item.name}</strong>
            <span class="item-subline">${joinParts([
              regionShortLabel(item.region),
              assetTypeLabel(item.asset_type),
              stageLabel(item.stage),
              actionLabel(item.action),
            ])}</span>
          </span>
          <span class="today-score">${score(item.priority_score ?? item.timing_score ?? item.quality_score)}</span>
        </button>
      `
    )
    .join("");
}

function renderTodayNewsList(news) {
  if (!news?.symbols?.length) return `<div class="empty">${t("noNewsYet")}</div>`;
  return news.symbols
    .slice()
    .sort((left, right) => Math.abs(right.message_score) - Math.abs(left.message_score))
    .slice(0, 5)
    .map(
      (item) => `
        <button type="button" class="today-item" data-symbol-id="${item.symbol_id}">
          <span>
            <strong>${item.symbol}${DOT}${item.name}</strong>
            <span class="item-subline">${item.latest_title ?? "-"}</span>
          </span>
          <span class="today-score ${pnlClass(item.message_score)}">${score(item.message_score, 1)}</span>
          <span class="item-subline">${joinParts([
            `${t("messagePositive")} ${item.positive_count}`,
            `${t("messageNegative")} ${item.negative_count}`,
            `${t("messageRisk")} ${item.risk_count}`,
          ])}</span>
        </button>
      `
    )
    .join("");
}

function renderTodayOpportunities(data) {
  const grid = document.getElementById("todayOpportunityGrid");
  const meta = document.getElementById("todayOpportunityMeta");
  if (!grid || !meta) return;

  const candidateIds = new Set(data.candidates.map((item) => item.symbol_id));
  const executable = data.candidates.slice(0, 5);
  const watchQueue = data.latest_scores
    .filter((item) => !candidateIds.has(item.symbol_id))
    .sort((left, right) => (right.priority_score ?? 0) - (left.priority_score ?? 0))
    .slice(0, 5);
  meta.textContent = joinParts([
    `${t("candidates")}: ${data.candidates.length}`,
    state.newsSnapshot ? `${t("todayMessages")}: ${state.newsSnapshot.symbols_total}` : t("noNewsYet"),
  ]);

  grid.innerHTML = `
    <section class="today-column">
      <div class="item-topline"><strong>${t("todayExecutable")}</strong><span class="badge">${executable.length}</span></div>
      <div class="today-list">${renderTodayList(executable, t("noCandidates"))}</div>
    </section>
    <section class="today-column">
      <div class="item-topline"><strong>${t("todayWatch")}</strong><span class="badge">${watchQueue.length}</span></div>
      <div class="today-list">${renderTodayList(watchQueue, t("noScores"))}</div>
    </section>
    <section class="today-column">
      <div class="item-topline"><strong>${t("todayMessages")}</strong><span class="badge">${state.newsSnapshot?.symbols_total ?? 0}</span></div>
      <div class="today-list">${renderTodayNewsList(state.newsSnapshot)}</div>
    </section>
  `;

  grid.querySelectorAll("[data-symbol-id]").forEach((node) => {
    node.addEventListener("click", () => loadSymbolDetail(Number(node.dataset.symbolId), { focus: true }));
  });
}

function metricModalTitle(type) {
  if (type === "symbols") return t("symbolList");
  if (type === "watchlists") return t("watchlistListTitle");
  if (type === "candidates") return t("candidateList");
  return t("openList");
}

function metricModalItemId(type, item) {
  return type === "candidates" ? item.symbol_id : item.id;
}

function metricModalItemTitle(type, item) {
  if (!item) return "-";
  if (type === "watchlists") return item.name;
  return `${item.symbol}${DOT}${item.name}`;
}

function metricModalItemMeta(type, item) {
  if (!item) return "";
  if (type === "watchlists") {
    return joinParts([watchlistTypeLabel(item.list_type), `${item.item_count} ${t("items")}`]);
  }
  if (type === "candidates") {
    return joinParts([
      `${t("rank")} ${item.rank_no ?? "-"}`,
      regionShortLabel(item.region),
      assetTypeLabel(item.asset_type),
      `${t("quality")} ${score(item.quality_score)}`,
      `${t("timing")} ${score(item.timing_score)}`,
      actionLabel(item.action),
    ]);
  }
  return joinParts([regionShortLabel(item.region), item.market, assetTypeLabel(item.asset_type), item.theme]);
}

function renderMetricModalActions(type, selected) {
  const actions = document.getElementById("metricModalActions");
  if (!selected) {
    actions.innerHTML = `<div class="empty">${t("noSelection")}</div>`;
    return;
  }

  const title = metricModalItemTitle(type, selected);
  const meta = metricModalItemMeta(type, selected);
  const watchlistItems = type === "watchlists" ? state.watchlistItems[selected.id] ?? [] : [];
  const miniList =
    type === "watchlists"
      ? `
        <div class="modal-mini-list">
          ${
            watchlistItems.length
              ? watchlistItems
                  .map((watchItem) => {
                    const symbol = watchItem.symbol;
                    return `
                      <button type="button" class="symbol-chip" data-modal-watch-symbol-id="${watchItem.symbol_id}">
                        ${symbol ? `${symbol.symbol}${DOT}${symbol.name}` : `#${watchItem.symbol_id}`}
                      </button>
                    `;
                  })
                  .join("")
              : `<span class="item-subline">${t("modalEmpty")}</span>`
          }
        </div>
      `
      : "";
  const buttons =
    type === "watchlists"
      ? `
        <button type="button" class="ghost-button" data-modal-action="open-watchlist">${t("openWatchlist")}</button>
        <button type="button" class="ghost-button" data-modal-action="add-active-symbol" ${state.activeSymbolId ? "" : "disabled"}>${t("addActiveSymbol")}</button>
      `
      : `
        <button type="button" class="ghost-button" data-modal-action="view-detail">${t("viewDetail")}</button>
        <button type="button" class="ghost-button" data-modal-action="add-to-watchlist">${t("addToWatchlist")}</button>
        <button type="button" class="ghost-button" data-modal-action="sync-selected">${t("syncSelected")}</button>
        <button type="button" class="ghost-button" data-modal-action="scan-selected">${t("scanSelected")}</button>
        <button type="button" class="ghost-button" data-modal-action="generate-plan">${t("generatePlan")}</button>
      `;

  actions.innerHTML = `
    <div class="modal-action-title">
      <strong>${title}</strong>
      <span class="item-subline">${meta}</span>
    </div>
    <div class="modal-action-buttons">${buttons}</div>
    ${miniList}
  `;

  actions.querySelectorAll("[data-modal-watch-symbol-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      closeMetricModal();
      await loadSymbolDetail(Number(button.dataset.modalWatchSymbolId), { focus: true });
    });
  });
  actions.querySelectorAll("[data-modal-action]").forEach((button) => {
    button.addEventListener("click", () => handleMetricModalAction(button.dataset.modalAction));
  });
}

function renderMetricModal() {
  const { type, items, selectedId } = state.metricModal;
  const title = metricModalTitle(type);
  const selected = items.find((item) => Number(metricModalItemId(type, item)) === Number(selectedId)) ?? null;
  const list = document.getElementById("metricModalList");
  document.getElementById("metricModalKicker").textContent = t("listActions");
  document.getElementById("metricModalTitle").textContent = title;

  if (!items.length) {
    setEmpty(list, t("modalEmpty"));
    renderMetricModalActions(type, null);
    return;
  }

  list.innerHTML = items
    .map((item) => {
      const itemId = metricModalItemId(type, item);
      return `
        <button type="button" class="modal-row ${Number(itemId) === Number(selectedId) ? "active" : ""}" data-modal-item-id="${itemId}">
          <strong>${metricModalItemTitle(type, item)}</strong>
          <span class="item-subline">${metricModalItemMeta(type, item)}</span>
        </button>
      `;
    })
    .join("");
  list.querySelectorAll("[data-modal-item-id]").forEach((button) => {
    button.addEventListener("click", () => selectMetricModalItem(Number(button.dataset.modalItemId)));
  });
  renderMetricModalActions(type, selected);
}

async function loadMetricModalItems(type) {
  if (type === "symbols") return fetchVisibleSymbols();
  if (type === "watchlists") return state.workbench?.watchlists ?? [];
  if (type === "candidates") return state.workbench?.candidates ?? [];
  return [];
}

async function openMetricModal(type) {
  const backdrop = document.getElementById("metricModalBackdrop");
  backdrop.hidden = false;
  state.metricModal = { type, items: [], selectedId: null };
  renderMetricModal();
  const items = await loadMetricModalItems(type);
  state.metricModal.items = items;
  state.metricModal.selectedId = items.length ? metricModalItemId(type, items[0]) : null;
  if (type === "watchlists" && state.metricModal.selectedId) {
    await fetchWatchlistItems(state.metricModal.selectedId);
  }
  renderMetricModal();
}

function closeMetricModal() {
  document.getElementById("metricModalBackdrop").hidden = true;
}

async function selectMetricModalItem(itemId) {
  state.metricModal.selectedId = itemId;
  if (state.metricModal.type === "watchlists") {
    await fetchWatchlistItems(itemId);
  }
  renderMetricModal();
}

function getSelectedMetricModalItem() {
  const { type, items, selectedId } = state.metricModal;
  return items.find((item) => Number(metricModalItemId(type, item)) === Number(selectedId)) ?? null;
}

async function handleMetricModalAction(action) {
  const type = state.metricModal.type;
  const selected = getSelectedMetricModalItem();
  if (!selected) return;

  if (action === "view-detail") {
    closeMetricModal();
    await loadSymbolDetail(metricModalItemId(type, selected), { focus: true });
    return;
  }
  if (action === "add-to-watchlist") {
    await addSymbolToPrimaryWatchlist(metricModalItemId(type, selected));
    await loadWorkbench();
    renderMetricModal();
    setStatus("success", t("addedToWatchlist"));
    return;
  }
  if (action === "sync-selected") {
    await syncSymbols([selected]);
    await loadWorkbench();
    renderMetricModal();
    return;
  }
  if (action === "scan-selected") {
    await scanSymbols([selected]);
    await loadWorkbench();
    renderMetricModal();
    setStatus("success", template("scanSummary", { count: state.workbench?.latest_scan?.executable_count ?? 0 }));
    return;
  }
  if (action === "generate-plan") {
    closeMetricModal();
    await loadSymbolDetail(metricModalItemId(type, selected), { focus: true });
    await generateTradeSetup();
    return;
  }
  if (action === "open-watchlist") {
    closeMetricModal();
    await loadWatchlistItems(selected.id);
    document.getElementById("watchlistList")?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  if (action === "add-active-symbol" && state.activeSymbolId) {
    await addSymbolToWatchlist(selected.id, state.activeSymbolId);
    await fetchWatchlistItems(selected.id);
    await loadWorkbench();
    renderMetricModal();
    setStatus("success", t("addedToWatchlist"));
  }
}

function renderAccountSummary(data) {
  const grid = document.getElementById("accountSummaryGrid");
  const meta = document.getElementById("accountMeta");
  const summary = data.account_summary;
  if (!summary) {
    meta.textContent = "-";
    setEmpty(grid, t("noPortfolio"));
    return;
  }

  meta.textContent = joinParts([
    `${t("activeTrades")}: ${summary.trade_count_7d}`,
    `${t("lastTrade")}: ${summary.last_trade_at ? formatDate(summary.last_trade_at) : "-"}`,
  ]);

  const items = [
    { label: t("accountEquity"), value: money(summary.total_equity), note: `${t("position")}: ${summary.position_count}` },
    { label: t("accountCash"), value: money(summary.available_cash), note: percent(summary.cash_pct) },
    { label: t("accountMarketValue"), value: money(summary.market_value), note: percent(summary.invested_pct) },
    { label: t("accountInvested"), value: percent(summary.invested_pct), note: `${t("accountCash")}: ${percent(summary.cash_pct)}` },
    {
      label: t("accountRealizedPnl"),
      value: `<span class="${pnlClass(summary.realized_pnl)}">${money(summary.realized_pnl)}</span>`,
      note: t("recentTrades"),
      rich: true,
    },
    {
      label: t("accountUnrealizedPnl"),
      value: `<span class="${pnlClass(summary.unrealized_pnl)}">${money(summary.unrealized_pnl)}</span>`,
      note: t("latestAssessment"),
      rich: true,
    },
  ];

  grid.innerHTML = items
    .map(
      (item) => `
      <article class="account-item">
        <div class="metric-label">${item.label}</div>
        <div class="metric-value">${item.rich ? item.value : item.value}</div>
        <div class="metric-note">${item.note}</div>
      </article>
    `
    )
    .join("");
}

function renderCandidates(data) {
  const body = document.getElementById("candidateBody");
  const meta = document.getElementById("scanMeta");
  meta.textContent = data.latest_scan.scan_run_id
    ? `${data.latest_scan.run_name}${DOT}${formatDate(data.latest_scan.created_at)}`
    : t("noScanYet");

  if (!data.candidates.length) {
    body.innerHTML = `<tr><td colspan="7" class="empty">${t("noCandidates")}</td></tr>`;
    return;
  }

  body.innerHTML = data.candidates
    .map(
      (item) => `
      <tr class="clickable ${state.activeSymbolId === item.symbol_id ? "active" : ""}" data-symbol-id="${item.symbol_id}">
        <td>${item.rank_no ?? "-"}</td>
        <td>
          <strong>${item.symbol}</strong><br />
          <span class="item-subline">${joinParts([item.name, regionShortLabel(item.region), assetTypeLabel(item.asset_type)])}</span>
        </td>
        <td>${score(item.quality_score)}</td>
        <td>${score(item.timing_score)}</td>
        <td><span class="${badgeClass(item.stage)}">${stageLabel(item.stage)}</span></td>
        <td><span class="${badgeClass(item.action)}">${actionLabel(item.action)}</span></td>
        <td>${percent(item.recommended_position_pct)}</td>
      </tr>
    `
    )
    .join("");

  body.querySelectorAll("tr[data-symbol-id]").forEach((row) => {
    row.addEventListener("click", () => loadSymbolDetail(Number(row.dataset.symbolId), { focus: true }));
  });
}

function renderScoreList(data) {
  const list = document.getElementById("scoreList");
  if (!data.latest_scores.length) {
    setEmpty(list, t("noScores"));
    return;
  }

  list.innerHTML = data.latest_scores
    .map(
      (item) => `
      <article class="list-item clickable ${state.activeSymbolId === item.symbol_id ? "active" : ""}" data-symbol-id="${item.symbol_id}">
        <div class="item-topline">
          <strong>${item.symbol}${DOT}${item.name}</strong>
          <span class="${badgeClass(item.stage)}">${stageLabel(item.stage)}</span>
        </div>
        <div class="item-subline">
          ${joinParts([
            regionShortLabel(item.region),
            assetTypeLabel(item.asset_type),
            `${t("quality")} ${score(item.quality_score)}`,
            `${t("timing")} ${score(item.timing_score)}`,
            actionLabel(item.action),
          ])}
        </div>
      </article>
    `
    )
    .join("");

  list.querySelectorAll("[data-symbol-id]").forEach((card) => {
    card.addEventListener("click", () => loadSymbolDetail(Number(card.dataset.symbolId), { focus: true }));
  });
}

function focusDetailPanel() {
  document.getElementById("detailTitle")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function ensureSymbolDirectory() {
  if (Object.keys(state.symbolDirectory).length) return state.symbolDirectory;
  const symbols = await requestJson("/api/v1/symbols?page_size=200");
  state.symbolDirectory = Object.fromEntries(symbols.map((item) => [item.id, item]));
  return state.symbolDirectory;
}

async function fetchWatchlistItems(watchlistId) {
  if (!state.watchlistItems[watchlistId]) {
    const [items, directory] = await Promise.all([
      requestJson(`/api/v1/watchlists/${watchlistId}/items`),
      ensureSymbolDirectory(),
    ]);
    state.watchlistItems[watchlistId] = items.map((item) => ({
      ...item,
      symbol: directory[item.symbol_id] ?? null,
    }));
  }
  return state.watchlistItems[watchlistId];
}

async function loadWatchlistItems(watchlistId) {
  await fetchWatchlistItems(watchlistId);
  state.activeWatchlistId = state.activeWatchlistId === watchlistId ? null : watchlistId;
  renderWatchlists(state.workbench);
}

function renderWatchlists(data) {
  const list = document.getElementById("watchlistList");
  if (!data.watchlists.length) {
    setEmpty(list, t("noWatchlists"));
    return;
  }

  list.innerHTML = data.watchlists
    .map(
      (item) => {
        const expanded = state.activeWatchlistId === item.id;
        const items = state.watchlistItems[item.id] ?? [];
        return `
      <article class="list-item clickable ${expanded ? "active" : ""}" data-watchlist-id="${item.id}">
        <div class="item-topline">
          <strong>${item.name}</strong>
          <span class="badge">${item.item_count} ${t("items")}</span>
        </div>
        <div class="item-subline">${watchlistTypeLabel(item.list_type)}</div>
        ${
          expanded
            ? `
          <div class="watchlist-symbols">
            ${
              items.length
                ? items
                    .map((watchItem) => {
                      const symbol = watchItem.symbol;
                      return `
                        <button type="button" class="symbol-chip" data-watch-symbol-id="${watchItem.symbol_id}">
                          ${symbol ? `${symbol.symbol}${DOT}${symbol.name}` : `#${watchItem.symbol_id}`}
                        </button>
                      `;
                    })
                    .join("")
                : `<span class="item-subline">${t("noScores")}</span>`
            }
          </div>
        `
            : ""
        }
      </article>
    `;
      }
    )
    .join("");

  list.querySelectorAll("[data-watchlist-id]").forEach((card) => {
    card.addEventListener("click", async (event) => {
      if (event.target.closest("[data-watch-symbol-id]")) return;
      await loadWatchlistItems(Number(card.dataset.watchlistId));
    });
  });

  list.querySelectorAll("[data-watch-symbol-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      loadSymbolDetail(Number(button.dataset.watchSymbolId), { focus: true });
    });
  });
}

function renderJournals(data) {
  const list = document.getElementById("journalList");
  if (!data.journals.length) {
    setEmpty(list, t("noJournals"));
    return;
  }

  list.innerHTML = data.journals
    .map(
      (item) => `
      <article class="list-item">
        <div class="item-topline">
          <strong>${item.title}</strong>
          <span class="badge">${item.entry_type}</span>
        </div>
        <div class="item-subline">${t("symbol")} #${item.symbol_id}${DOT}${formatDate(item.created_at)}</div>
      </article>
    `
    )
    .join("");
}

function buildLinePath(points, mapX, mapY) {
  return points
    .map((point, index) => `${index === 0 ? "M" : "L"} ${mapX(point.index)} ${mapY(point.value)}`)
    .join(" ");
}

function parseTradeDate(value) {
  const [year, month, day] = String(value).split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day));
}

function weekKeyForDate(value) {
  const date = parseTradeDate(value);
  const day = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const weekNo = Math.ceil(((date - yearStart) / 86400000 + 1) / 7);
  return `${date.getUTCFullYear()}-${String(weekNo).padStart(2, "0")}`;
}

function aggregateWeeklyBars(bars) {
  const weeks = [];
  let current = null;

  bars.forEach((bar) => {
    const weekKey = weekKeyForDate(bar.trade_date);
    if (!current || current.week_key !== weekKey) {
      current = {
        week_key: weekKey,
        trade_date: bar.trade_date,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
        volume: bar.volume || 0,
      };
      weeks.push(current);
      return;
    }

    current.trade_date = bar.trade_date;
    current.high = Math.max(current.high, bar.high);
    current.low = Math.min(current.low, bar.low);
    current.close = bar.close;
    current.volume = (current.volume || 0) + (bar.volume || 0);
  });

  return weeks;
}

function getChartBars(detail) {
  const bars = detail?.bars ?? [];
  return state.chartTimeframe === "weekly" ? aggregateWeeklyBars(bars) : bars;
}

function resetChartInteraction() {
  state.chartRange = null;
  state.chartWindowSize = DEFAULT_CHART_WINDOW;
}

function updateChartTimeframeButtons() {
  document.getElementById("chartDailyButton")?.classList.toggle("active", state.chartTimeframe === "daily");
  document.getElementById("chartWeeklyButton")?.classList.toggle("active", state.chartTimeframe === "weekly");
}

function setChartTimeframe(timeframe) {
  if (state.chartTimeframe === timeframe) return;
  state.chartTimeframe = timeframe;
  resetChartInteraction();
  renderChart(state.detail);
}

function getAvailableChartWindows(totalBars) {
  if (!totalBars) return [];
  const windows = CHART_WINDOW_STEPS.filter((value) => value < totalBars);
  windows.push(totalBars);
  return [...new Set(windows)].sort((left, right) => left - right);
}

function getEffectiveChartWindow(totalBars) {
  if (!totalBars) return DEFAULT_CHART_WINDOW;
  const windows = getAvailableChartWindows(totalBars);
  const fallback = windows.find((value) => value >= DEFAULT_CHART_WINDOW) ?? windows[windows.length - 1];
  const requested = state.chartWindowSize || fallback;
  return windows.reduce((closest, value) => {
    if (value === requested) return value;
    if (Math.abs(value - requested) < Math.abs(closest - requested)) return value;
    return closest;
  }, fallback);
}

function getResetChartWindow(totalBars) {
  const windows = getAvailableChartWindows(totalBars || DEFAULT_CHART_WINDOW);
  return windows.find((value) => value >= DEFAULT_CHART_WINDOW) ?? windows[windows.length - 1] ?? DEFAULT_CHART_WINDOW;
}

function normalizeChartRange(totalBars) {
  if (!state.chartRange || !totalBars) return null;
  const start = Math.max(0, Math.min(totalBars - 1, Math.min(state.chartRange.start, state.chartRange.end)));
  const end = Math.max(0, Math.min(totalBars - 1, Math.max(state.chartRange.start, state.chartRange.end)));
  if (end <= start) return null;
  return { start, end };
}

function getVisibleChartSlice(bars) {
  const totalBars = bars.length;
  const normalizedRange = normalizeChartRange(totalBars);
  if (normalizedRange) {
    return {
      bars: bars.slice(normalizedRange.start, normalizedRange.end + 1),
      startIndex: normalizedRange.start,
      endIndex: normalizedRange.end,
      isCustomRange: true,
    };
  }

  const windowSize = getEffectiveChartWindow(totalBars);
  const startIndex = Math.max(0, totalBars - windowSize);
  return {
    bars: bars.slice(startIndex),
    startIndex,
    endIndex: totalBars - 1,
    isCustomRange: false,
  };
}

function updateChartControls(totalBars, visibleBars, isCustomRange) {
  const zoomInButton = document.getElementById("chartZoomInButton");
  const zoomOutButton = document.getElementById("chartZoomOutButton");
  const resetButton = document.getElementById("chartResetButton");
  const windowPill = document.getElementById("chartWindowPill");
  const chartHint = document.getElementById("chartHint");
  const expandButton = document.getElementById("chartExpandButton");
  const windows = [...new Set([...getAvailableChartWindows(totalBars), visibleBars])].sort((left, right) => left - right);
  const currentIndex = windows.findIndex((value) => value === visibleBars);
  const hasChart = Boolean(totalBars);

  zoomInButton.disabled = !hasChart || currentIndex <= 0;
  zoomOutButton.disabled = !hasChart || currentIndex === -1 || currentIndex >= windows.length - 1;
  resetButton.disabled = !hasChart || (!isCustomRange && visibleBars === getResetChartWindow(totalBars));
  const timeframeLabel = t(state.chartTimeframe === "weekly" ? "chartWeekly" : "chartDaily");
  windowPill.textContent = hasChart ? `${timeframeLabel} ${visibleBars}/${totalBars} ${t("barsUnit")}` : "-";
  chartHint.textContent = hasChart ? t("chartDragHint") : "";
  expandButton.textContent = t(state.chartExpanded ? "collapseChart" : "expandChart");
  updateChartTimeframeButtons();
}

function changeChartWindow(direction) {
  const bars = getChartBars(state.detail);
  const totalBars = bars.length;
  const slice = getVisibleChartSlice(bars);
  const windows = [...new Set([...getAvailableChartWindows(totalBars), slice.bars.length])].sort((left, right) => left - right);
  if (!windows.length) return;

  const currentIndex = Math.max(0, windows.indexOf(slice.bars.length));
  const nextIndex = Math.min(Math.max(currentIndex + direction, 0), windows.length - 1);
  if (nextIndex === currentIndex) return;

  const targetSize = windows[nextIndex];
  if (slice.isCustomRange) {
    const center = Math.round((slice.startIndex + slice.endIndex) / 2);
    let start = Math.max(0, center - Math.floor(targetSize / 2));
    let end = start + targetSize - 1;
    if (end >= totalBars) {
      end = totalBars - 1;
      start = Math.max(0, end - targetSize + 1);
    }
    state.chartRange = { start, end };
  } else {
    state.chartWindowSize = targetSize;
  }
  renderChart(state.detail);
}

function resetChartWindow() {
  const totalBars = getChartBars(state.detail).length;
  state.chartRange = null;
  state.chartWindowSize = getResetChartWindow(totalBars);
  renderChart(state.detail);
}

function updateChartExpandedState() {
  document.body.classList.toggle("chart-expanded", state.chartExpanded);
  document.getElementById("chartCard")?.classList.toggle("chart-card-expanded", state.chartExpanded);
  document.getElementById("detailChart")?.classList.toggle("expanded", state.chartExpanded);
  const expandButton = document.getElementById("chartExpandButton");
  if (expandButton) {
    expandButton.textContent = t(state.chartExpanded ? "collapseChart" : "expandChart");
  }
}

function toggleChartExpanded() {
  state.chartExpanded = !state.chartExpanded;
  updateChartExpandedState();
  renderChart(state.detail);
}

function clearChartCleanup() {
  if (typeof state.chartCleanup === "function") {
    state.chartCleanup();
    state.chartCleanup = null;
  }
}

function getChartLocalIndexFromEvent(event, svg) {
  if (!state.chartView) return null;
  const rect = svg.getBoundingClientRect();
  const scaleX = rect.width / state.chartView.width;
  const scaleY = rect.height / state.chartView.height;
  const chartX = (event.clientX - rect.left) / scaleX;
  const chartY = (event.clientY - rect.top) / scaleY;
  const interactiveBottom = state.chartView.volumeTop + state.chartView.volumeHeight;

  if (
    chartX < state.chartView.left ||
    chartX > state.chartView.left + state.chartView.plotWidth ||
    chartY < state.chartView.top ||
    chartY > interactiveBottom
  ) {
    return null;
  }

  const relativeX = Math.max(0, Math.min(state.chartView.plotWidth - 1, chartX - state.chartView.left));
  return Math.max(0, Math.min(state.chartView.visibleCount - 1, Math.floor(relativeX / state.chartView.step)));
}

function updateChartSelectionBox(wrapper, svg, startLocalIndex, endLocalIndex) {
  const box = wrapper.querySelector(".chart-select-box");
  if (!box || !state.chartView) return;

  const localStart = Math.min(startLocalIndex, endLocalIndex);
  const localEnd = Math.max(startLocalIndex, endLocalIndex);
  const svgRect = svg.getBoundingClientRect();
  const scaleX = svgRect.width / state.chartView.width;
  const scaleY = svgRect.height / state.chartView.height;
  const startX = state.chartView.left + localStart * state.chartView.step;
  const endX = state.chartView.left + (localEnd + 1) * state.chartView.step;
  const interactiveBottom = state.chartView.volumeTop + state.chartView.volumeHeight;

  box.hidden = false;
  box.style.left = `${startX * scaleX}px`;
  box.style.top = `${state.chartView.top * scaleY}px`;
  box.style.width = `${Math.max((endX - startX) * scaleX, 2)}px`;
  box.style.height = `${(interactiveBottom - state.chartView.top) * scaleY}px`;
}

function clearChartSelectionBox(wrapper) {
  const box = wrapper.querySelector(".chart-select-box");
  if (!box) return;
  box.hidden = true;
  box.style.width = "0";
  box.style.height = "0";
}

function attachChartInteractions(container, slice) {
  const wrapper = container.querySelector(".chart-svg-wrap");
  const svg = container.querySelector("svg");
  if (!wrapper || !svg) return;

  const onMouseDown = (event) => {
    if (event.button !== 0) return;
    const localIndex = getChartLocalIndexFromEvent(event, svg);
    if (localIndex === null) return;
    state.chartView.drag = { anchorIndex: localIndex, currentIndex: localIndex };
    updateChartSelectionBox(wrapper, svg, localIndex, localIndex);
    event.preventDefault();
  };

  const onMouseMove = (event) => {
    if (!state.chartView?.drag) return;
    const localIndex = getChartLocalIndexFromEvent(event, svg);
    if (localIndex === null) return;
    state.chartView.drag.currentIndex = localIndex;
    updateChartSelectionBox(wrapper, svg, state.chartView.drag.anchorIndex, localIndex);
  };

  const onMouseUp = () => {
    if (!state.chartView?.drag) return;
    const { anchorIndex, currentIndex } = state.chartView.drag;
    state.chartView.drag = null;
    clearChartSelectionBox(wrapper);

    if (currentIndex === null || currentIndex === undefined) return;
    if (Math.abs(currentIndex - anchorIndex) < 2) return;

    state.chartRange = {
      start: slice.startIndex + Math.min(anchorIndex, currentIndex),
      end: slice.startIndex + Math.max(anchorIndex, currentIndex),
    };
    renderChart(state.detail);
  };

  const onDoubleClick = () => {
    if (state.chartRange) {
      resetChartWindow();
      return;
    }
    toggleChartExpanded();
  };

  svg.addEventListener("mousedown", onMouseDown);
  window.addEventListener("mousemove", onMouseMove);
  window.addEventListener("mouseup", onMouseUp);
  svg.addEventListener("dblclick", onDoubleClick);

  state.chartCleanup = () => {
    svg.removeEventListener("mousedown", onMouseDown);
    window.removeEventListener("mousemove", onMouseMove);
    window.removeEventListener("mouseup", onMouseUp);
    svg.removeEventListener("dblclick", onDoubleClick);
  };
}

function computeChartPriceDomain(windowBars, setup, ma10Points, ma20Points) {
  const candlePrices = windowBars.flatMap((item) => [item.low, item.high, item.open, item.close]);
  const movingAveragePrices = [...ma10Points, ...ma20Points].map((item) => item.value);
  const signalPrices = (setup?.chart_signals ?? [])
    .map((item) => item.price)
    .filter((value) => value !== null && value !== undefined && Number.isFinite(Number(value)));
  const futurePlanPrices = getActiveFutureBuyPlan(setup)
    .flatMap((item) => [item.zone_min, item.zone_max])
    .filter((value) => value !== null && value !== undefined && Number.isFinite(Number(value)));
  const allPrices = [...candlePrices, ...movingAveragePrices, ...signalPrices, ...futurePlanPrices].filter((value) => Number.isFinite(Number(value)));
  const visiblePrices = candlePrices.filter((value) => Number.isFinite(Number(value)));
  const latestClose = windowBars[windowBars.length - 1]?.close ?? allPrices[allPrices.length - 1] ?? 1;
  const rawMin = Math.min(...allPrices);
  const rawMax = Math.max(...allPrices);
  const visibleMin = Math.min(...visiblePrices);
  const visibleMax = Math.max(...visiblePrices);
  const visibleSpan = Math.max(visibleMax - visibleMin, Math.abs(latestClose) * 0.006, 0.01);
  const rawSpan = Math.max(rawMax - rawMin, visibleSpan);
  const padding = Math.max(rawSpan * 0.06, visibleSpan * 0.18, Math.abs(latestClose) * 0.002, 0.01);
  const priceMin = Math.max(0, rawMin - padding);
  const priceMax = rawMax + padding;
  return {
    priceMin,
    priceMax,
    priceSpan: Math.max(priceMax - priceMin, Math.abs(latestClose) * 0.008, 0.01),
  };
}

function buildFuturePlanOverlay(setup, latestClose, dimensions, priceY) {
  const plans = getActiveFutureBuyPlan(setup);
  if (!plans.length || !dimensions.futureWidth) return "";

  const { futureLeft, futureWidth, futureRight, top, priceHeight } = dimensions;
  const usablePlans = plans.filter((plan) => plan.zone_min !== null && plan.zone_max !== null);
  if (!usablePlans.length) return "";

  const bands = usablePlans
    .map((plan, index) => {
      const style = FUTURE_PLAN_STYLE[plan.priority] ?? FUTURE_PLAN_STYLE.normal;
      const y1 = priceY(Math.max(plan.zone_min, plan.zone_max));
      const y2 = priceY(Math.min(plan.zone_min, plan.zone_max));
      const bandY = Math.min(y1, y2);
      const bandHeight = Math.max(Math.abs(y2 - y1), 8);
      const labelY = Math.max(top + 12, Math.min(top + priceHeight - 6, bandY + bandHeight / 2 + 4));
      return `
        <rect x="${futureLeft}" y="${bandY}" width="${futureWidth}" height="${bandHeight}" fill="${style.fill}" stroke="${style.color}" stroke-width="1" stroke-dasharray="${style.dash}" rx="6" />
        <text x="${futureRight - 8}" y="${labelY}" text-anchor="end" font-size="11" fill="${style.color}">${futureBuyLabel(plan.label)}</text>
        <text x="${futureLeft + 8}" y="${labelY}" font-size="10" fill="${style.color}">${score(plan.zone_min)}-${score(plan.zone_max)}</text>
      `;
    })
    .join("");

  const curvePoints = usablePlans
    .filter((plan) => plan.priority !== "avoid")
    .slice(0, 3)
    .map((plan, index, list) => {
      const mid = (Number(plan.zone_min) + Number(plan.zone_max)) / 2;
      const progress = list.length === 1 ? 0.68 : 0.28 + (index / Math.max(list.length - 1, 1)) * 0.58;
      return {
        x: futureLeft + futureWidth * progress,
        y: priceY(mid),
      };
    });
  const latestPoint = {
    x: futureLeft - 12,
    y: priceY(latestClose),
  };
  const curvePath = curvePoints.length
    ? `M ${latestPoint.x} ${latestPoint.y} ${curvePoints.map((point) => `L ${point.x} ${point.y}`).join(" ")}`
    : "";
  const curve = curvePath
    ? `
      <path d="${curvePath}" fill="none" stroke="#111827" stroke-width="1.4" stroke-dasharray="5 5" opacity="0.75" />
      ${curvePoints.map((point) => `<circle cx="${point.x}" cy="${point.y}" r="3.5" fill="#111827" />`).join("")}
    `
    : "";

  return `
    <rect x="${futureLeft}" y="${top}" width="${futureWidth}" height="${priceHeight}" fill="rgba(31,41,51,0.035)" stroke="rgba(31,41,51,0.10)" stroke-dasharray="4 4" rx="8" />
    <text x="${futureLeft + 8}" y="${top + 16}" font-size="11" fill="#6b7280">${t("futureBuyPlan")}</text>
    ${bands}
    ${curve}
  `;
}

function renderChart(detail) {
  const container = document.getElementById("detailChart");
  const meta = document.getElementById("chartMeta");
  clearChartCleanup();
  updateChartExpandedState();
  const bars = getChartBars(detail);
  const setup = detail?.latest_trade_setup;

  if (!bars.length) {
    state.chartView = null;
    meta.textContent = "-";
    setEmpty(container, t("noChart"));
    updateChartControls(0, 0, false);
    return;
  }

  const slice = getVisibleChartSlice(bars);
  const windowBars = slice.bars;
  updateChartControls(bars.length, windowBars.length, slice.isCustomRange);
  const width = state.chartExpanded ? 1360 : 1040;
  const height = state.chartExpanded ? 620 : 420;
  const left = 56;
  const right = 28;
  const top = 20;
  const priceHeight = state.chartExpanded ? 360 : 240;
  const volumeTop = state.chartExpanded ? 420 : 286;
  const volumeHeight = state.chartExpanded ? 110 : 82;
  const futurePlans = getActiveFutureBuyPlan(setup);
  const futureWidth = futurePlans.length ? (state.chartExpanded ? 210 : 168) : 0;
  const futureGap = futurePlans.length ? 16 : 0;
  const totalPlotWidth = width - left - right;
  const plotWidth = totalPlotWidth - futureWidth - futureGap;
  const futureLeft = left + plotWidth + futureGap;
  const futureRight = futureLeft + futureWidth;
  const step = plotWidth / windowBars.length;
  const candleWidth = Math.max(5, Math.min(state.chartExpanded ? 34 : 28, step * 0.56));
  state.chartView = {
    width,
    height,
    left,
    right,
    top,
    plotWidth,
    totalPlotWidth,
    futureWidth,
    priceHeight,
    volumeTop,
    volumeHeight,
    step,
    visibleCount: windowBars.length,
    startIndex: slice.startIndex,
    endIndex: slice.endIndex,
    drag: null,
  };

  const ma10Points = [];
  const ma20Points = [];
  windowBars.forEach((_, index) => {
    const globalIndex = bars.length - windowBars.length + index;
    const tenSlice = bars.slice(Math.max(0, globalIndex - 9), globalIndex + 1);
    const twentySlice = bars.slice(Math.max(0, globalIndex - 19), globalIndex + 1);
    if (tenSlice.length >= 5) {
      ma10Points.push({ index, value: tenSlice.reduce((sum, item) => sum + item.close, 0) / tenSlice.length });
    }
    if (twentySlice.length >= 10) {
      ma20Points.push({ index, value: twentySlice.reduce((sum, item) => sum + item.close, 0) / twentySlice.length });
    }
  });

  const { priceMin, priceMax, priceSpan } = computeChartPriceDomain(windowBars, setup, ma10Points, ma20Points);
  const volumeMax = Math.max(...windowBars.map((item) => item.volume || 0), 1);
  const latest = windowBars[windowBars.length - 1];

  const mapX = (index) => left + step * index + step / 2;
  const priceY = (value) => top + ((priceMax - value) / priceSpan) * priceHeight;
  const volumeY = (value) => volumeTop + volumeHeight - ((value || 0) / volumeMax) * volumeHeight;
  const futurePlanOverlay = buildFuturePlanOverlay(
    setup,
    latest.close,
    { futureLeft, futureWidth, futureRight, top, priceHeight },
    priceY
  );

  meta.textContent = `${t("lastBar")}: ${latest.trade_date}${DOT}${t("close")} ${score(latest.close)}`;

  const gridLines = Array.from({ length: 4 }, (_, index) => {
    const ratio = index / 3;
    const price = priceMax - priceSpan * ratio;
    const y = top + priceHeight * ratio;
    return `
      <line x1="${left}" y1="${y}" x2="${width - right}" y2="${y}" stroke="rgba(31,41,51,0.12)" stroke-dasharray="4 4" />
      <text x="${left - 8}" y="${y + 4}" text-anchor="end" font-size="11" fill="#6b7280">${score(price)}</text>
    `;
  }).join("");

  const candles = windowBars.map((item, index) => {
    const xCenter = mapX(index);
    const gain = item.close >= item.open;
    const color = gain ? "#0f766e" : "#b42318";
    const openY = priceY(item.open);
    const closeY = priceY(item.close);
    const highY = priceY(item.high);
    const lowY = priceY(item.low);
    const bodyTop = Math.min(openY, closeY);
    const bodyHeight = Math.max(Math.abs(closeY - openY), 2);
    const volTop = volumeY(item.volume);
    const volHeight = volumeTop + volumeHeight - volTop;
    return `
      <line x1="${xCenter}" y1="${highY}" x2="${xCenter}" y2="${lowY}" stroke="${color}" stroke-width="1.5" />
      <rect x="${xCenter - candleWidth / 2}" y="${bodyTop}" width="${candleWidth}" height="${bodyHeight}" fill="${color}" rx="1" />
      <rect x="${xCenter - candleWidth / 2}" y="${volTop}" width="${candleWidth}" height="${volHeight}" fill="${color}" opacity="0.28" rx="1" />
    `;
  }).join("");

  const signalLines = (setup?.chart_signals ?? [])
    .map((signal) => {
      const style = SIGNAL_STYLE[signal.kind];
      if (!style) return "";
      const y = priceY(signal.price);
      return `
        <line x1="${left}" y1="${y}" x2="${futurePlans.length ? futureLeft - 8 : width - right}" y2="${y}" stroke="${style.color}" stroke-width="1.2" stroke-dasharray="${style.dash}" opacity="0.85" />
        <rect x="${(futurePlans.length ? futureLeft - 92 : width - right - 86)}" y="${y - 10}" width="80" height="18" rx="9" fill="white" opacity="0.92" />
        <text x="${(futurePlans.length ? futureLeft - 52 : width - right - 46)}" y="${y + 3}" text-anchor="middle" font-size="11" fill="${style.color}">${signalLabel(signal)}</text>
      `;
    })
    .join("");

  const ma10Path = ma10Points.length ? buildLinePath(ma10Points, mapX, priceY) : "";
  const ma20Path = ma20Points.length ? buildLinePath(ma20Points, mapX, priceY) : "";
  const latestMa10 = ma10Points.length ? ma10Points[ma10Points.length - 1].value : null;
  const latestMa20 = ma20Points.length ? ma20Points[ma20Points.length - 1].value : null;
  const actionMarker = `
    <circle cx="${mapX(windowBars.length - 1)}" cy="${priceY(latest.close)}" r="4.5" fill="#111827" />
    <rect x="${mapX(windowBars.length - 1) - 34}" y="${priceY(latest.close) - 28}" width="68" height="18" rx="9" fill="#111827" />
    <text x="${mapX(windowBars.length - 1)}" y="${priceY(latest.close) - 15}" text-anchor="middle" font-size="11" fill="white">${actionLabel(detail.latest_score?.action)}</text>
  `;

  const xTicks = [0, Math.floor(windowBars.length / 2), windowBars.length - 1]
    .map((index) => `<text x="${mapX(index)}" y="${height - 8}" text-anchor="middle" font-size="11" fill="#6b7280">${windowBars[index].trade_date.slice(5)}</text>`)
    .join("");

  container.innerHTML = `
    <div class="chart-svg-wrap" style="height:${height}px">
      <div class="chart-select-box" hidden></div>
      <svg viewBox="0 0 ${width} ${height}" aria-label="candlestick chart">
        <rect x="${left}" y="${top}" width="${plotWidth}" height="${priceHeight}" fill="rgba(255,255,255,0.82)" rx="8" />
        <rect x="${left}" y="${volumeTop}" width="${plotWidth}" height="${volumeHeight}" fill="rgba(15,118,110,0.04)" rx="8" />
        ${gridLines}
        ${signalLines}
        ${futurePlanOverlay}
        ${ma10Path ? `<path d="${ma10Path}" fill="none" stroke="#2563eb" stroke-width="2" />` : ""}
        ${ma20Path ? `<path d="${ma20Path}" fill="none" stroke="#f59e0b" stroke-width="2" />` : ""}
        ${candles}
        ${actionMarker}
        ${xTicks}
        <text x="${left}" y="${volumeTop - 8}" font-size="11" fill="#6b7280">${t("chartWindow")}: ${windowBars[0].trade_date} - ${latest.trade_date}</text>
        <text x="${width - right}" y="${volumeTop - 8}" text-anchor="end" font-size="11" fill="#6b7280">${t("volume")}: ${compactNumber(latest.volume)}</text>
      </svg>
    </div>
    <div class="chart-legend">
      <span>${t("lastBar")}: ${latest.trade_date}</span>
      <span>O ${score(latest.open)}</span>
      <span>H ${score(latest.high)}</span>
      <span>L ${score(latest.low)}</span>
      <span>C ${score(latest.close)}</span>
      <span>${t("ma10")} ${score(latestMa10)}</span>
      <span>${t("ma20")} ${score(latestMa20)}</span>
    </div>
  `;
  attachChartInteractions(container, slice);
}

function renderRecentTradeCards(container, trades) {
  if (!trades?.length) {
    setEmpty(container, t("noTrades"));
    return;
  }

  container.innerHTML = trades
    .map(
      (item) => `
      <article class="list-item">
        <div class="item-topline">
          <strong>${item.symbol}${DOT}${item.name}</strong>
          <span class="${sideBadgeClass(item.side)}">${sideLabel(item.side)}</span>
        </div>
        <div class="item-subline">${joinParts([
          `${t("orderQty")}: ${item.quantity}`,
          `${t("orderPrice")}: ${score(item.price)}`,
          `${t("positionAmount")}: ${money(item.amount)}`,
        ])}</div>
        <div class="item-subline ${pnlClass(item.realized_pnl)}">${joinParts([
          `${t("accountRealizedPnl")}: ${money(item.realized_pnl)}`,
          formatDate(item.created_at),
        ])}</div>
      </article>
    `
    )
    .join("");
}

function rememberDetail(detail) {
  if (!detail?.symbol?.id) return;
  const symbolId = detail.symbol.id;
  state.detailCache[symbolId] = detail;
  state.detailOrder = [symbolId, ...state.detailOrder.filter((item) => item !== symbolId)].slice(0, 4);
}

function removeDetailFromDock(symbolId) {
  delete state.detailCache[symbolId];
  state.detailOrder = state.detailOrder.filter((item) => item !== symbolId);
  if (state.activeSymbolId === symbolId) {
    state.activeSymbolId = state.detailOrder[0] ?? null;
    const nextDetail = state.activeSymbolId ? state.detailCache[state.activeSymbolId] : null;
    renderDetail(nextDetail ?? null);
    return;
  }
  renderDetailDock();
}

function renderDetailDock() {
  const rail = document.getElementById("detailQuickRail");
  if (!state.detailOrder.length) {
    setEmpty(rail, t("noDetail"));
    return;
  }

  rail.innerHTML = state.detailOrder
    .map((symbolId) => {
      const item = state.detailCache[symbolId];
      if (!item) return "";
      const lastBar = item.bars?.[item.bars.length - 1];
      return `
        <article class="detail-chip ${state.activeSymbolId === symbolId ? "active" : ""}" data-detail-id="${symbolId}">
          <div class="detail-chip-top">
            <div>
              <div class="detail-chip-title">${item.symbol.symbol}</div>
              <div class="detail-chip-meta">${item.symbol.name}</div>
            </div>
            <button type="button" class="detail-chip-close" data-close-detail="${symbolId}">x</button>
          </div>
          <div class="detail-chip-meta">${joinParts([
            regionShortLabel(item.symbol.region),
            assetTypeLabel(item.symbol.asset_type),
            stageLabel(item.latest_score?.stage),
            actionLabel(item.latest_score?.action),
          ])}</div>
          <div class="detail-chip-note">${joinParts([
            `${t("quality")} ${score(item.latest_score?.quality_score)}`,
            `${t("timing")} ${score(item.latest_score?.timing_score)}`,
            `${t("close")} ${score(lastBar?.close)}`,
          ])}</div>
        </article>
      `;
    })
    .join("");

  rail.querySelectorAll("[data-detail-id]").forEach((node) => {
    node.addEventListener("click", (event) => {
      if (event.target.closest("[data-close-detail]")) return;
      const symbolId = Number(node.dataset.detailId);
      state.activeSymbolId = symbolId;
      renderDetail(state.detailCache[symbolId] ?? null);
    });
  });

  rail.querySelectorAll("[data-close-detail]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      removeDetailFromDock(Number(button.dataset.closeDetail));
    });
  });
}

function statPct(value) {
  return value === null || value === undefined ? "-" : percent(value);
}

function renderSignalStatsCard(stats) {
  if (!stats) return "";
  const sampleCount = stats.sample_count ?? 0;
  const minSamples = stats.min_sample_count ?? stats.scope?.min_sample_count ?? 3;
  const enoughSamples = sampleCount >= minSamples;
  const avgReturnClass = pnlClass(stats.avg_return_20d);
  const drawdownClass = pnlClass(stats.avg_max_drawdown_20d);

  return `
    <article class="list-item">
      <div class="item-topline">
        <strong>${t("similarSignalStats")}</strong>
        <span class="badge ${enoughSamples ? "" : "warn"}">${t("similarSamples")} ${sampleCount}/${minSamples}</span>
      </div>
      <div class="item-subline">${joinParts([
        `${t("matchedSignals")}: ${stats.matched_count ?? 0}`,
        `${t("win5d")}: ${statPct(stats.win_rate_5d)}`,
        `${t("win20d")}: ${statPct(stats.win_rate_20d)}`,
      ])}</div>
      <div class="item-subline">${joinParts([
        `${t("avgReturn20d")}: ${statPct(stats.avg_return_20d)}`,
        `${t("maxGain20d")}: ${statPct(stats.avg_max_gain_20d)}`,
        `${t("maxDrawdown20d")}: ${statPct(stats.avg_max_drawdown_20d)}`,
      ])}</div>
      <div class="item-subline">${joinParts([
        `${t("best20d")}: ${statPct(stats.best_return_20d)}`,
        `${t("worst20d")}: ${statPct(stats.worst_return_20d)}`,
      ])}</div>
      ${enoughSamples ? "" : `<div class="item-subline ${drawdownClass || avgReturnClass}">${t("sampleInsufficient")}</div>`}
    </article>
  `;
}

function bindFuturePlanControls() {
  document.querySelectorAll("[data-future-scenario]").forEach((button) => {
    button.addEventListener("click", () => {
      state.futurePlanScenario = button.dataset.futureScenario;
      renderDetail(state.detail);
      renderChart(state.detail);
    });
  });

  const customInputs = [
    ["futureCustomHorizon", "horizonDays"],
    ["futureCustomPullback", "pullbackPct"],
    ["futureCustomPosition", "positionPct"],
  ];
  customInputs.forEach(([id, key]) => {
    const input = document.getElementById(id);
    if (!input) return;
    input.addEventListener("input", () => {
      state.futurePlanCustom[key] = Number(input.value);
      renderDetail(state.detail);
      renderChart(state.detail);
    });
  });
}

function renderDetail(detail) {
  const title = document.getElementById("detailTitle");
  const meta = document.getElementById("detailMeta");
  const summary = document.getElementById("detailSummary");
  const setup = document.getElementById("detailSetup");
  const history = document.getElementById("detailHistory");
  const journals = document.getElementById("detailJournals");
  const trades = document.getElementById("detailTrades");
  const setupButton = document.getElementById("setupButton");
  const simBuyButton = document.getElementById("simBuyButton");
  const simSellButton = document.getElementById("simSellButton");

  state.detail = detail;
  if (detail?.symbol?.id) {
    rememberDetail(detail);
  }
  renderDetailDock();
  if (state.workbench) {
    renderCandidates(state.workbench);
    renderScoreList(state.workbench);
  }

  if (!detail) {
    title.textContent = t("selectSymbol");
    meta.textContent = "-";
    setEmpty(summary, t("noDetail"));
    setEmpty(setup, t("latestTradeSetupEmpty"));
    history.innerHTML = `<tr><td colspan="5" class="empty">${t("noDetail")}</td></tr>`;
    renderChart(null);
    setEmpty(trades, t("noTrades"));
    setEmpty(journals, t("noJournals"));
    setupButton.disabled = true;
    simBuyButton.disabled = true;
    simSellButton.disabled = true;
    syncOrderForm(null);
    return;
  }

  setupButton.disabled = !detail.latest_score;
  simBuyButton.disabled = false;
  simSellButton.disabled = !(detail.position?.quantity > 0);
  title.textContent = `${detail.symbol.symbol}${DOT}${detail.symbol.name}`;
  meta.textContent = joinParts([
    `${t("marketLabel")}: ${String(detail.symbol.market || "-").toUpperCase()}`,
    `${t("regionLabel")}: ${regionLongLabel(detail.symbol.region)}`,
    `${t("assetLabel")}: ${assetTypeLabel(detail.symbol.asset_type)}`,
  ]);

  const summaryRows = [];
  if (detail.latest_score) {
    summaryRows.push(`
      <article class="list-item">
        <div class="item-topline"><strong>${t("latestScoreDate")}</strong><span>${detail.latest_score.trade_date}</span></div>
        <div class="item-subline">${joinParts([
          `${t("quality")} ${score(detail.latest_score.quality_score)} (${detail.latest_score.quality_grade})`,
          `${t("timing")} ${score(detail.latest_score.timing_score)}`,
          stageLabel(detail.latest_score.stage),
          actionLabel(detail.latest_score.action),
        ])}</div>
      </article>
    `);
  }
  if (detail.signal_stats) {
    summaryRows.push(renderSignalStatsCard(detail.signal_stats));
  }
  if (detail.position) {
    summaryRows.push(`
      <article class="list-item">
        <div class="item-topline"><strong>${t("currentPosition")}</strong><span>${percent(detail.position.position_pct)}</span></div>
        <div class="item-subline">${joinParts([
          `${t("holdingQty")}: ${detail.position.quantity}`,
          `${t("avgCost")}: ${score(detail.position.avg_cost)}`,
          `${t("close")}: ${score(detail.position.latest_price)}`,
        ])}</div>
        <div class="item-subline">${joinParts([
          `${t("accountMarketValue")}: ${money(detail.position.market_value)}`,
          `${t("assetLabel")}: ${assetTypeLabel(detail.position.asset_type)}`,
        ])}</div>
      </article>
    `);
  }
  summary.innerHTML = summaryRows.length ? summaryRows.join("") : `<div class="empty">${t("noScores")}</div>`;

  if (detail.latest_trade_setup) {
    const item = detail.latest_trade_setup;
    const guardrails = [];
    if (item.is_sector_overweight) guardrails.push(t("sectorOverweight"));
    if (item.is_asset_overweight) guardrails.push(t("assetOverweight"));
    const tranches = item.tranche_plan ?? [];
    const futureBuyPlan = getActiveFutureBuyPlan(item);

    setup.innerHTML = `
      <article class="list-item">
        <div class="item-topline">
          <strong>${t("buyZone")}</strong>
          <span>${item.entry_min ?? "-"} - ${item.entry_max ?? "-"}</span>
        </div>
        <div class="item-subline">${joinParts([
          `${t("stopLoss")}: ${item.stop_loss ?? "-"}`,
          `${t("target")}: ${item.target_price ?? "-"}`,
          `${t("riskReward")}: ${item.risk_reward_ratio ?? "-"}`,
        ])}</div>
        <div class="item-subline">${joinParts([
          `${t("recommendedPosition")}: ${percent(item.recommended_position_pct)}`,
          `${t("positionAmount")}: ${money(item.recommended_position_amount)}`,
          `${t("allowAdd")}: ${item.allow_add_position ? t("yes") : t("no")}`,
        ])}</div>
        <div class="item-subline">${joinParts([
          `${t("stageCap")}: ${percent(item.stage_cap_pct)} / ${money(item.stage_cap_amount)}`,
          `${t("stageRoom")}: ${percent(item.remaining_stage_pct)} / ${money(item.remaining_stage_amount)}`,
        ])}</div>
        <div class="item-subline">${joinParts([
          `${t("currentPosition")}: ${percent(item.current_position_pct)} / ${money(item.current_position_amount)}`,
          `${t("riskBudget")}: ${money(item.risk_budget_amount)}`,
          `${t("riskShare")}: ${score(item.risk_per_share)}`,
          `${t("shareCap")}: ${item.risk_capped_shares ?? "-"}`,
        ])}</div>
        <div class="item-subline">${joinParts([
          stageLabel(item.stage),
          actionLabel(item.action),
          guardrails.length ? `${t("guardrails")}: ${guardrails.join(", ")}` : t("stable"),
        ])}</div>
        <div class="item-subline">${t("openTrigger")}: ${formatOpenTrigger(item)}</div>
        <div class="item-subline">${t("addTrigger")}: ${formatAddTrigger(item)}</div>
        <div class="item-subline">${t("stopTrigger")}: ${formatStopTrigger(item)}</div>
        <div class="item-subline">${t("trimTrigger")}: ${formatTrimTrigger(item)}</div>
        <div class="item-subline">${t("reason")}: ${item.setup_reason ?? "-"}</div>
        ${tranches.length
          ? `
          <div class="item-subline"><strong>${t("tranchePlan")}</strong></div>
          ${tranches
            .map(
              (tranche) => `
              <div class="item-subline">
                ${joinParts([
                  tranche.label,
                  `${t("tranchePct")}: ${percent(tranche.position_pct)}`,
                  `${t("positionAmount")}: ${money(tranche.amount)}`,
                  `${t("trigger")}: ${tranche.trigger}`,
                ])}
              </div>
            `
            )
            .join("")}
        `
          : ""}
        ${futureBuyPlan.length
          ? `
          <div class="future-plan-head">
            <strong>${t("futureBuyPlan")}</strong>
            <div class="future-scenario-tabs">
              ${["general", "short", "mid", "long", "custom"]
                .map(
                  (scenario) => `
                    <button type="button" class="ghost-button detail-action ${state.futurePlanScenario === scenario ? "active" : ""}" data-future-scenario="${scenario}">
                      ${t(`futureScenario${scenario.charAt(0).toUpperCase()}${scenario.slice(1)}`)}
                    </button>
                  `
                )
                .join("")}
            </div>
          </div>
          ${
            state.futurePlanScenario === "custom"
              ? `
                <div class="future-custom-grid">
                  <label><span>${t("customHorizon")}</span><input id="futureCustomHorizon" type="number" min="1" max="120" step="1" value="${state.futurePlanCustom.horizonDays}" /></label>
                  <label><span>${t("customPullback")}</span><input id="futureCustomPullback" type="number" min="0" max="30" step="0.5" value="${state.futurePlanCustom.pullbackPct}" /></label>
                  <label><span>${t("customPosition")}</span><input id="futureCustomPosition" type="number" min="0" max="100" step="0.5" value="${state.futurePlanCustom.positionPct}" /></label>
                </div>
              `
              : ""
          }
          ${futureBuyPlan
            .map(
              (plan) => `
              <div class="item-subline">
                ${joinParts([
                  futureBuyLabel(plan.label),
                  `${t("futureHorizon")}: ${plan.horizon_days}${t("daysUnit")}`,
                  `${t("futureZone")}: ${plan.zone_min ?? "-"} - ${plan.zone_max ?? "-"}`,
                  `${t("futurePriority")}: ${futurePriorityLabel(plan.priority)}`,
                  `${t("tranchePct")}: ${percent(plan.position_pct)}`,
                  `${t("positionAmount")}: ${money(plan.amount)}`,
                ])}
              </div>
              <div class="item-subline">${t("trigger")}: ${futureTriggerLabel(plan)}</div>
            `
            )
            .join("")}
        `
          : ""}
      </article>
    `;
    bindFuturePlanControls();
  } else {
    setEmpty(setup, t("latestTradeSetupEmpty"));
  }

  history.innerHTML = detail.score_history.length
    ? detail.score_history
        .map(
          (item) => `
            <tr>
              <td>${item.trade_date}</td>
              <td>${score(item.quality_score)}</td>
              <td>${score(item.timing_score)}</td>
              <td>${stageLabel(item.stage)}</td>
              <td>${actionLabel(item.action)}</td>
            </tr>
          `
        )
        .join("")
    : `<tr><td colspan="5" class="empty">${t("noScores")}</td></tr>`;

  renderChart(detail);
  renderRecentTradeCards(trades, detail.recent_trades);

  if (!detail.journals.length) {
    setEmpty(journals, t("noJournals"));
  } else {
    journals.innerHTML = detail.journals
      .map(
        (item) => `
        <article class="list-item">
          <div class="item-topline">
            <strong>${item.title}</strong>
            <span class="badge">${item.entry_type}</span>
          </div>
          <div class="item-subline">${formatDate(item.created_at)}</div>
        </article>
      `
      )
      .join("");
  }

  syncOrderForm(detail);
}

async function loadSymbolDetail(symbolId, options = {}) {
  state.activeSymbolId = symbolId;
  resetChartInteraction();
  const detail = await requestJson(`/api/v1/dashboard/symbol-detail?portfolio_id=${state.portfolioId}&symbol_id=${symbolId}`);
  renderDetail(detail);
  scheduleSignalRulePreview();
  if (options.focus) {
    focusDetailPanel();
  }
}

async function generateTradeSetup() {
  if (!state.activeSymbolId || !state.detail?.latest_score) return;
  await requestJson("/api/v1/trade-setups/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      portfolio_id: state.portfolioId,
      symbol_id: state.activeSymbolId,
      score_id: state.detail.latest_score.id,
    }),
  });
  await loadSymbolDetail(state.activeSymbolId);
  setStatus("success", template("planSummary"));
}

async function fetchVisibleSymbols() {
  const response = await requestJson("/api/v1/symbols?page_size=200");
  return state.marketGroup === "all" ? response : response.filter((item) => item.region === state.marketGroup);
}

function symbolIdFromItem(item) {
  return item.symbol_id ?? item.id;
}

function inferSymbolPayload(rawCode) {
  const symbol = rawCode.trim().toUpperCase();
  if (!symbol) {
    throw new Error(t("symbolCodeRequired"));
  }

  if (/^\d{6}$/.test(symbol)) {
    const isEtf = symbol.startsWith("5") || symbol.startsWith("15") || symbol.startsWith("16") || symbol.startsWith("18");
    const market = symbol.startsWith("6") || symbol.startsWith("5") ? "sh" : "sz";
    return {
      symbol,
      name: symbol,
      asset_type: isEtf ? "etf" : "stock",
      market,
      board: "main",
      theme: isEtf ? "custom-etf" : "custom-stock",
    };
  }

  if (/^[A-Z.]{1,10}$/.test(symbol)) {
    const usEtfs = new Set(["DIA", "GLD", "IWM", "QQQ", "SLV", "SPY", "TLT", "VTI", "VOO", "XLK", "XLF", "XLE"]);
    return {
      symbol,
      name: symbol,
      asset_type: usEtfs.has(symbol) ? "etf" : "stock",
      market: "us",
      board: "custom",
      theme: usEtfs.has(symbol) ? "custom-etf" : "custom-stock",
    };
  }

  throw new Error(t("symbolCodeRequired"));
}

async function findExistingSymbol(symbolCode) {
  const symbols = await requestJson(`/api/v1/symbols?keyword=${encodeURIComponent(symbolCode)}&page_size=200`);
  return symbols.find((item) => item.symbol.toUpperCase() === symbolCode.toUpperCase()) ?? null;
}

async function addSymbolToWatchlist(watchlistId, symbolId) {
  try {
    await requestJson(`/api/v1/watchlists/${watchlistId}/items`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol_id: symbolId }),
    });
  } catch (error) {
    if (!String(error.message).includes("already")) {
      throw error;
    }
  }
  delete state.watchlistItems[watchlistId];
  state.activeWatchlistId = watchlistId;
}

async function addSymbolToPrimaryWatchlist(symbolId) {
  const watchlist = state.workbench?.watchlists?.[0];
  if (!watchlist) return;
  await addSymbolToWatchlist(watchlist.id, symbolId);
}

async function addSymbolFromInput() {
  const input = document.getElementById("symbolCodeInput");
  const payload = inferSymbolPayload(input.value);
  let symbol = await findExistingSymbol(payload.symbol);
  if (!symbol) {
    symbol = await requestJson("/api/v1/symbols", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  await addSymbolToPrimaryWatchlist(symbol.id);
  if (state.marketGroup !== "all" && symbol.region && state.marketGroup !== symbol.region) {
    state.marketGroup = symbol.region;
    document.getElementById("marketSelect").value = symbol.region;
  }
  state.activeSymbolId = symbol.id;
  state.symbolDirectory = {};
  input.value = "";
  await loadWorkbench();
  await loadSymbolDetail(symbol.id, { focus: true });
  setStatus("success", template("symbolAdded", { symbol: symbol.symbol }));
}

async function loadLatestNewsSnapshot(data) {
  const symbolIds = [
    ...new Set([
      ...data.candidates.map((item) => item.symbol_id),
      ...data.latest_scores.map((item) => item.symbol_id),
    ]),
  ].slice(0, 20);
  if (!symbolIds.length) {
    state.newsSnapshot = null;
    return;
  }

  const params = new URLSearchParams({
    portfolio_id: String(state.portfolioId),
    days: "7",
    limit: "20",
  });
  symbolIds.forEach((symbolId) => params.append("symbol_ids", String(symbolId)));

  try {
    const response = await requestJson(`/api/v1/news/latest?${params.toString()}`);
    state.newsSnapshot = response.symbols_total || response.macro ? response : null;
  } catch (error) {
    console.warn("Failed to load latest news snapshot", error);
    state.newsSnapshot = null;
  }
}

async function loadWorkbench() {
  if (!state.portfolioId) return;
  const data = await requestJson(`/api/v1/dashboard/workbench?portfolio_id=${state.portfolioId}&market_group=${state.marketGroup}`);
  state.workbench = data;
  await loadLatestNewsSnapshot(data);
  renderTodayOpportunities(data);
  renderMetrics(data);
  renderAccountSummary(data);
  renderCandidates(data);
  renderScoreList(data);
  renderWatchlists(data);
  renderJournals(data);

  const visibleSymbolIds = new Set([
    ...data.candidates.map((item) => item.symbol_id),
    ...data.latest_scores.map((item) => item.symbol_id),
  ]);
  if (state.activeSymbolId && visibleSymbolIds.has(state.activeSymbolId)) {
    await loadSymbolDetail(state.activeSymbolId);
  } else if (data.latest_scores.length) {
    await loadSymbolDetail(data.latest_scores[0].symbol_id);
  } else if (data.candidates.length) {
    await loadSymbolDetail(data.candidates[0].symbol_id);
  } else {
    state.activeSymbolId = null;
    renderDetail(null);
  }
}

async function loadPortfolios() {
  const select = document.getElementById("portfolioSelect");
  const portfolios = await requestJson("/api/v1/portfolios");
  state.portfolios = portfolios;
  if (!portfolios.length) {
    select.innerHTML = `<option value="">${t("noPortfolio")}</option>`;
    state.portfolioId = null;
    return;
  }
  select.innerHTML = portfolios.map((item) => `<option value="${item.id}">${item.name}</option>`).join("");
  const defaultPortfolio = portfolios.find((item) => Number(item.is_default) === 1) ?? portfolios[0];
  state.portfolioId = defaultPortfolio.id;
  select.value = String(state.portfolioId);
}

async function setButtonBusy(button, busyTextKey, handler) {
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = t(busyTextKey);
  try {
    await handler();
  } finally {
    button.disabled = false;
    button.textContent = previous;
  }
}

async function syncSymbols(symbols) {
  const symbolIds = symbols.map(symbolIdFromItem);
  if (!symbolIds.length) {
    setStatus("error", t("noDetail"));
    return;
  }

  const response = await requestJson("/api/v1/market-data/update", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      scope: "symbols",
      symbol_ids: symbolIds,
      asset_types: [...new Set(symbols.map((item) => item.asset_type))],
      adjust: "qfq",
      auto_scan: true,
      portfolio_id: state.portfolioId,
      portfolio_rule_id: state.workbench?.active_rule?.id ?? null,
    }),
  });
  setStatus("success", template("syncSummary", { ok: response.data.ok_count, total: response.data.symbols_total, failed: response.data.failed_count }));
}

async function runSync() {
  const symbols = await fetchVisibleSymbols();
  await syncSymbols(symbols);
  await loadWorkbench();
}

async function scanSymbols(symbols) {
  const symbolIds = symbols.map(symbolIdFromItem);
  if (!symbolIds.length) {
    setStatus("error", t("noDetail"));
    return;
  }

  return requestJson("/api/v1/scans/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      portfolio_id: state.portfolioId,
      portfolio_rule_id: state.workbench?.active_rule?.id ?? null,
      run_name: "manual-workbench-scan",
      scope_snapshot: {
        symbol_ids: symbolIds,
        asset_types: [...new Set(symbols.map((item) => item.asset_type))],
        markets: [...new Set(symbols.map((item) => item.market))],
      },
    }),
  });
}

async function runScan() {
  const symbols = await fetchVisibleSymbols();
  await scanSymbols(symbols);
  await loadWorkbench();
  setStatus("success", template("scanSummary", { count: state.workbench?.latest_scan?.executable_count ?? 0 }));
}

async function runNewsUpdate() {
  const candidateIds = state.workbench?.candidates?.map((item) => item.symbol_id) ?? [];
  let symbolIds = candidateIds;
  if (!symbolIds.length) {
    const symbols = await fetchVisibleSymbols();
    symbolIds = symbols.map((item) => item.id);
  }
  if (!symbolIds.length) {
    setStatus("error", t("noDetail"));
    return;
  }

  const response = await requestJson("/api/v1/news/update", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      portfolio_id: state.portfolioId,
      scope: "symbols",
      symbol_ids: symbolIds,
      days: 7,
      include_macro: true,
      include_sector: true,
      include_symbol: true,
    }),
  });
  state.newsSnapshot = response;
  if (state.workbench) {
    renderTodayOpportunities(state.workbench);
  }
  setStatus("success", template("newsSummary", { count: response.symbols_total }));
}

async function submitSimOrder(side) {
  if (!state.portfolioId || !state.activeSymbolId || !state.detail) return;

  const qtyInput = document.getElementById("simQuantityInput");
  const priceInput = document.getElementById("simPriceInput");
  const symbolCode = state.detail.symbol.symbol;
  let quantity = Number(qtyInput.value);
  let price = Number(priceInput.value);

  if (!(quantity > 0)) {
    quantity = side === "sell" ? computeDefaultSellQuantity(state.detail) : computeSuggestedBuyQuantity(state.detail);
  }
  if (!(price > 0)) {
    price = computeSuggestedPrice(state.detail);
  }
  if (!(quantity > 0) || !(price > 0)) {
    throw new Error(state.locale === "zh-CN" ? "请先提供有效数量和价格" : "Please provide a valid quantity and price");
  }

  const result = await requestJson(`/api/v1/portfolios/${state.portfolioId}/sim-orders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol_id: state.activeSymbolId,
      side,
      quantity,
      price,
      order_type: "market",
    }),
  });

  await loadWorkbench();
  setStatus(
    "success",
    template("orderSummary", {
      side: sideLabel(side),
      symbol: symbolCode,
      quantity: result.trade.quantity,
      price: score(result.trade.price),
    })
  );
}

document.getElementById("portfolioSelect").addEventListener("change", async (event) => {
  state.portfolioId = Number(event.target.value);
  await loadSignalRuleConfig();
  await loadWorkbench();
});

document.getElementById("localeSelect").addEventListener("change", async (event) => {
  state.locale = event.target.value;
  applyI18n();
  if (state.workbench) {
    renderMetrics(state.workbench);
    renderTodayOpportunities(state.workbench);
    renderAccountSummary(state.workbench);
    renderCandidates(state.workbench);
    renderScoreList(state.workbench);
    renderWatchlists(state.workbench);
    renderJournals(state.workbench);
  }
  renderDetail(state.detail);
  renderSignalRuleConfig();
  scheduleSignalRulePreview(0);
});

document.getElementById("marketSelect").addEventListener("change", async (event) => {
  state.marketGroup = event.target.value;
  await loadWorkbench();
});

document.getElementById("refreshButton").addEventListener("click", async () => {
  await loadWorkbench();
  setStatus("", "");
});

document.getElementById("ruleConfigButton").addEventListener("click", () => {
  document.getElementById("ruleConfigSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
});

document.getElementById("signalRuleForm").addEventListener("input", () => {
  markSignalRuleExpert();
  scheduleSignalRulePreview();
});

document.getElementById("signalRuleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await setButtonBusy(document.getElementById("saveSignalRuleButton"), "savingRule", saveSignalRule);
  } catch (error) {
    setStatus("error", error.message);
  }
});

document.getElementById("addSymbolButton").addEventListener("click", async (event) => {
  try {
    await setButtonBusy(event.currentTarget, "addingSymbol", addSymbolFromInput);
  } catch (error) {
    setStatus("error", `${t("symbolAddFailed")}: ${error.message}`);
  }
});

document.getElementById("symbolCodeInput").addEventListener("keydown", async (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  document.getElementById("addSymbolButton").click();
});

document.getElementById("syncButton").addEventListener("click", async (event) => {
  try {
    await setButtonBusy(event.currentTarget, "syncing", runSync);
  } catch (error) {
    setStatus("error", `${t("syncFailed")}: ${error.message}`);
  }
});

document.getElementById("scanButton").addEventListener("click", async (event) => {
  try {
    await setButtonBusy(event.currentTarget, "scanning", runScan);
  } catch (error) {
    setStatus("error", `${t("scanFailed")}: ${error.message}`);
  }
});

document.getElementById("newsButton").addEventListener("click", async (event) => {
  try {
    await setButtonBusy(event.currentTarget, "newsUpdating", runNewsUpdate);
  } catch (error) {
    setStatus("error", `${t("newsFailed")}: ${error.message}`);
  }
});

document.getElementById("setupButton").addEventListener("click", async (event) => {
  try {
    await setButtonBusy(event.currentTarget, "planGenerating", generateTradeSetup);
  } catch (error) {
    setStatus("error", `${t("planFailed")}: ${error.message}`);
  }
});

document.getElementById("simBuyButton").addEventListener("click", async (event) => {
  try {
    await setButtonBusy(event.currentTarget, "simBuy", () => submitSimOrder("buy"));
  } catch (error) {
    setStatus("error", `${t("orderFailed")}: ${error.message}`);
  }
});

document.getElementById("simSellButton").addEventListener("click", async (event) => {
  const qtyInput = document.getElementById("simQuantityInput");
  if (!Number(qtyInput.value) && state.detail?.position?.quantity) {
    qtyInput.value = String(computeDefaultSellQuantity(state.detail));
  }
  try {
    await setButtonBusy(event.currentTarget, "simSell", () => submitSimOrder("sell"));
  } catch (error) {
    setStatus("error", `${t("orderFailed")}: ${error.message}`);
  }
});

document.getElementById("simQuantityInput").addEventListener("input", () => {
  renderOrderScenarioPreview(state.detail);
});

document.getElementById("simPriceInput").addEventListener("input", () => {
  renderOrderScenarioPreview(state.detail);
});

document.getElementById("chartDailyButton").addEventListener("click", () => {
  setChartTimeframe("daily");
});

document.getElementById("chartWeeklyButton").addEventListener("click", () => {
  setChartTimeframe("weekly");
});

document.getElementById("chartZoomInButton").addEventListener("click", () => {
  changeChartWindow(-1);
});

document.getElementById("chartZoomOutButton").addEventListener("click", () => {
  changeChartWindow(1);
});

document.getElementById("chartResetButton").addEventListener("click", () => {
  resetChartWindow();
});

document.getElementById("chartExpandButton").addEventListener("click", () => {
  toggleChartExpanded();
});

document.getElementById("metricModalClose").addEventListener("click", () => {
  closeMetricModal();
});

document.getElementById("metricModalBackdrop").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) {
    closeMetricModal();
  }
});

document.getElementById("detailChart").addEventListener(
  "wheel",
  (event) => {
    if (!getChartBars(state.detail).length) return;
    event.preventDefault();
    changeChartWindow(event.deltaY < 0 ? -1 : 1);
  },
  { passive: false }
);

window.addEventListener("keydown", (event) => {
  const modalOpen = !document.getElementById("metricModalBackdrop").hidden;
  if (event.key === "Escape" && modalOpen) {
    closeMetricModal();
    return;
  }
  if (event.key === "Escape" && state.chartExpanded) {
    toggleChartExpanded();
  }
});

async function bootstrap() {
  applyI18n();
  await loadPortfolios();
  await loadSignalRuleConfig();
  await loadWorkbench();
}

bootstrap().catch((error) => {
  setStatus("error", error.message);
});
