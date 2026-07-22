// WP5.1：标的研究壳层共享类型定义
//
// 设计说明：
// - SourceContext：来源上下文（来自路由参数或显式传入），用于标的研究收口后追溯入口
// - 各子组件 Props 接口：保持最小化，避免复制 AppContext；状态留在 SymbolResearchShell
// - 复杂业务类型（TradeSetup/BacktestRun 等）沿用 src/types，不在此重复定义
//
// 关键约束（来自 spec 13.4）：
// - 不重写已稳定的业务逻辑：因子计算、回测引擎、告警规则、风控规则、模拟撮合不动
// - 只做组件职责拆分：把 JSX 块和对应 hooks 抽到独立文件
// - 保持 props 接口最小化：子组件通过 props 接收必要数据
import type React from "react";
import type {
  BacktestRun,
  FutureBuyPlan,
  FuturePlanTuning,
  ReturnScenarios,
  Symbol as SymbolInfo,
  TradeSetup,
  TradeSetupOverrides,
  TradeSetupTranche,
  WorkbenchBar,
} from "../../types";
import type { SymbolFactorExplanation } from "../../api/client";
import type {
  BOLLResult,
  CrossSignal,
  MACDResult,
  RSIExtremePoint,
} from "../../utils/indicators";

// ─── 来源上下文 ───────────────────────────────────────────

/** 标的来源类型：用于追溯标的进入研究的入口 */
export type SourceType =
  | "candidate" // 来自候选池
  | "observation" // 来自观察池
  | "portfolio_member" // 来自组合成员
  | "position" // 来自持仓
  | "alert" // 来自告警中心
  | "backtest" // 来自回测结果详情
  | "search" // 来自搜索
  | "manual" // 手动选择
  | "legacy"; // 旧入口兼容（InvestmentCenter tab）

/**
 * 来源上下文：标的研究收口后用于追溯进入入口
 * - symbol_id：进入时聚焦的标的 ID（可选，后续可由搜索替换）
 * - source_type：来源类型，用于追溯
 * - source_id：来源记录 ID（如 candidate_id / observation_id）
 * - portfolio_id：所属组合上下文（用于风控/回测）
 * - return_to：返回路径（关闭标的研究时回到的页面）
 */
export interface SourceContext {
  symbol_id?: number;
  source_type?: SourceType;
  source_id?: number;
  portfolio_id?: number;
  return_to?: string;
}

// ─── 子组件内部使用的类型别名 ─────────────────────────────

/** 标的快捷引用（仅展示所需最少字段） */
export type SymbolQuickRef = {
  symbol_id: number;
  symbol: string;
  name: string;
};

/** 交易计划草稿（编辑态） */
export type TradePlanDraft = {
  entry_min: number | null;
  entry_max: number | null;
  stop_loss: number | null;
  target_price: number | null;
  recommended_position_pct: number | null;
  recommended_position_amount: number | null;
};

/** 场景预演扩展类型：在 ReturnScenarios 基础上附加调整后的止损/目标价 */
export type ScenarioPreview = ReturnScenarios & {
  _adjStop?: number;
  _adjTarget?: number;
};

export type TrancheDraft = TradeSetupTranche;
export type FuturePlanTunings = Record<string, FuturePlanTuning>;

/** 研究情景参数（正式风控读 PortfolioRule，本组件只展示研究情景参数） */
export type RiskSettings = {
  atrMultiplier: number;
  concentrationMediumPct: number;
  concentrationHighPct: number;
  maxLossPct: number;
};

/** 告警摘要设置（正式提醒归告警中心，本组件只展示研究态预警） */
export type AlertSettings = {
  enableStopLoss: boolean;
  enableTarget: boolean;
  enableRsi: boolean;
  enableMacd: boolean;
  stopNearPct: number;
  targetNearPct: number;
  rsiOverbought: number;
  rsiOversold: number;
};

/** 价格告警条目 */
export type PriceAlert = {
  id: string;
  type: string;
  level: "warning" | "danger" | "info";
  message: string;
  detail: string;
  timestamp: number;
};

/** 风险指标计算结果 */
export type RiskMetrics = {
  rrRatio: number;
  stopDistancePct: number;
  currentStopDist: number;
  atrStopRef: number | null;
  concentrationLevel: "high" | "medium" | "low";
  maxLossAmount: number;
  maxLossPerShare: number;
  reward: number;
  riskAmount: number;
  currentPrice: number;
  stop: number | null;
  target: number | null;
  maxLossLimitAmount: number;
};

/** 图表数据（含全部技术指标） */
export interface ChartDataExt {
  bars: WorkbenchBar[];
  dates: string[];
  closes: number[];
  candlestick: number[][];
  volume: Array<{ value: number; itemStyle: { color: string } }>;
  ma10: (number | null)[];
  ma20: (number | null)[];
  macd: MACDResult | null;
  rsi: (number | null)[];
  boll: BOLLResult | null;
  atr: (number | null)[];
  macdSignals: CrossSignal[];
  rsiSignals: RSIExtremePoint[];
}

// ─── 子组件 Props 接口 ───────────────────────────────────

/** SymbolResearchShell：壳层组件，接收来源上下文参数 */
export interface SymbolResearchShellProps {
  /** 来源上下文：来自路由参数或显式传入 */
  sourceContext?: SourceContext;
  /** 初始标的 ID（覆盖 sourceContext.symbol_id） */
  initialSymbolId?: number;
  /** 打开指标详情弹窗（保留旧 InvestmentCenter 接口） */
  openMetricModal?: (type: string) => void;
  /** 允许扩展子节点 */
  children?: React.ReactNode;
}

/** SymbolSearchHeader：搜索框 + 快捷标的 + 搜索历史 */
export interface SymbolSearchHeaderProps {
  /** 移动端布局标志 */
  isMobile: boolean;
  /** 当前搜索关键词 */
  searchQuery: string;
  /** 搜索结果 */
  searchResults: SymbolInfo[];
  /** 是否正在搜索 */
  searching: boolean;
  /** 搜索历史（最近5个） */
  searchHistory: SymbolQuickRef[];
  /** 收藏集合 */
  favorites: Set<number>;
  /** 快捷标的（持仓 + 最新评分） */
  quickSymbols: SymbolQuickRef[];
  /** 当前激活的标的 ID */
  activeSymbolId: number | null;
  /** 搜索关键词变更 */
  onSearchQueryChange: (q: string) => void;
  /** 选择标的 */
  onSelectSymbol: (
    symbolId: number,
    symbolInfo?: Pick<SymbolInfo, "symbol" | "name">,
  ) => void;
  /** 切换收藏 */
  onToggleFavorite: (symbolId: number) => void;
}

/** SymbolRelationshipBar：候选/观察/组合成员/持仓/告警关联状态 */
export interface SymbolRelationshipBarProps {
  /** 标的 ID；为 null/undefined 时不发请求 */
  symbolId: number | null | undefined;
  /** 是否显示未激活状态的占位徽标 */
  showInactive?: boolean;
  /** 紧凑模式：单个徽标合并显示 */
  compact?: boolean;
  /** 自定义 className */
  className?: string;
  /** 点击徽标时回调 */
  onOpenDetail?: (symbolId: number) => void;
}

/** FactorExplanationPanel：因子解释 + 模型 ID + 因子贡献 + 数据截止时间 */
export interface FactorExplanationPanelProps {
  /** 标的 ID */
  symbolId: number | null;
  /** 因子解释数据 */
  factorExplanation: SymbolFactorExplanation | null;
  /** 是否正在加载 */
  loading: boolean;
  /** 错误信息 */
  error: string | null;
  /** 刷新回调 */
  onRefresh: () => void;
}

/** SymbolAlertSummary：告警摘要（正式提醒归告警中心） */
export interface SymbolAlertSummaryProps {
  /** 告警条目列表 */
  alerts: PriceAlert[];
  /** 告警设置 */
  alertSettings: AlertSettings;
  /** 设置面板是否展开 */
  settingsOpen: boolean;
  /** 当前标的 ID（用于创建正式告警规则时构造 config） */
  symbolId?: number | null;
  /** 切换设置面板 */
  onToggleSettings: () => void;
  /** 更新告警设置 */
  onUpdateSettings: (patch: Partial<AlertSettings>) => void;
}

/** RiskReferencePanel：研究情景参数展示 */
export interface RiskReferencePanelProps {
  /** 风险指标计算结果 */
  riskMetrics: RiskMetrics | null;
  /** 风险设置（研究情景参数） */
  riskSettings: RiskSettings;
  /** 设置面板是否展开 */
  settingsOpen: boolean;
  /** 入场价 */
  entryPrice: number;
  /** 数量 */
  quantity: number;
  /** 当前交易计划 */
  setup: TradeSetup | null;
  /** 当前组合 ID（用于跳转到组合 PortfolioRule 配置） */
  portfolioId?: number | null;
  /** 切换设置面板 */
  onToggleSettings: () => void;
  /** 更新风险设置 */
  onUpdateSettings: (patch: Partial<RiskSettings>) => void;
  /** 跳转到组合管理 PortfolioRule 配置 */
  onGoToPortfolioRule?: () => void;
}

/** TradePlanPanel：交易计划刷新 + 参数覆盖 + 情景分析 + 分批计划 + 未来买入计划 */
export interface TradePlanPanelProps {
  /** 当前交易计划 */
  setup: TradeSetup | null;
  /** 场景预演结果（含调整后的止损/目标价） */
  scenarios: ScenarioPreview | null;
  /** 入场价 */
  entryPrice: number;
  /** 数量 */
  quantity: number;
  /** 图表点击设置的入场价 */
  chartEntryPrice: number | null;
  /** 交易计划编辑态 */
  tradePlanEditing: boolean;
  /** 交易计划草稿 */
  tradePlanDraft: TradePlanDraft | null;
  /** 分批计划编辑态 */
  trancheEditing: boolean;
  /** 分批计划草稿 */
  trancheDrafts: TrancheDraft[];
  /** 刷新中标志 */
  refreshingPlan: boolean;
  /** 未来计划场景 */
  futurePlanScenario: string;
  /** 未来计划调参 */
  futurePlanTunings: FuturePlanTunings;
  /** 激活的未来买入计划 */
  activeFutureBuyPlan: FutureBuyPlan[];
  /** 编辑计划 */
  onEditPlan: () => void;
  /** 取消编辑 */
  onCancelPlanEdit: () => void;
  /** 应用参数覆盖 */
  onApplyPlanOverrides: () => Promise<void> | void;
  /** 刷新计划 */
  onRefreshPlan: () => Promise<void> | void;
  /** 更新交易计划草稿字段 */
  onUpdateTradePlanDraft: (
    field: keyof TradePlanDraft,
    value: number | null,
  ) => void;
  /** 编辑分批 */
  onEditTranches: () => void;
  /** 取消分批编辑 */
  onCancelTranches: () => void;
  /** 保存分批 */
  onSaveTranches: () => Promise<void> | void;
  /** 重置分批 */
  onResetTranches: () => Promise<void> | void;
  /** 更新分批草稿字段 */
  onUpdateTrancheDraft: (
    index: number,
    field: keyof TrancheDraft,
    value: string | number,
  ) => void;
  /** 添加分批 */
  onAddTranche: () => void;
  /** 删除分批 */
  onRemoveTranche: (index: number) => void;
  /** 更新未来计划调参 */
  onUpdateFutureTuning: (
    field: keyof FuturePlanTuning,
    value: number | null,
  ) => void;
  /** 设置未来计划场景 */
  onSetFuturePlanScenario: (scenario: string) => void;
  /** 跳转到组合交易上下文（WP5.2：模拟下单不再在研究页复制账户/订单逻辑） */
  onJumpToPortfolioTrade?: () => void;
}

/** SymbolChartPanel：K线 + MA10/MA20 + MACD + RSI + BOLL + 绘图 Hook 统一 */
export interface SymbolChartPanelProps {
  /** 图表数据 */
  chartData: ChartDataExt | null;
  /** ECharts 主图 option（已计算） */
  chartOption: Record<string, unknown> | null;
  /** ECharts MACD 副图 option */
  macdOption: Record<string, unknown> | null;
  /** ECharts RSI 副图 option */
  rsiOption: Record<string, unknown> | null;
  /** 是否显示 MACD */
  showMACD: boolean;
  /** 是否显示 RSI */
  showRSI: boolean;
  /** 图表是否展开 */
  chartExpanded: boolean;
  /** 图表周期 */
  chartTimeframe: "daily" | "weekly";
  /** 图表窗口大小 */
  chartWindowSize: number;
  /** 最后一根 K 线 */
  lastBar: WorkbenchBar | undefined;
  /** 当前交易计划（用于 Y 轴关键价） */
  setup: TradeSetup | null;
  /** 激活的未来买入计划 */
  activeFutureBuyPlan: FutureBuyPlan[];
  /** 切换 MACD 显示 */
  onToggleMACD: () => void;
  /** 切换 RSI 显示 */
  onToggleRSI: () => void;
  /** 设置图表周期 */
  onSetChartTimeframe: (tf: "daily" | "weekly") => void;
  /** 设置图表窗口大小 */
  onSetChartWindowSize: (size: number) => void;
  /** 重置图表（窗口与范围） */
  onResetChart: () => void;
  /** 图表点击事件：设置入场价 */
  onChartClick: (params: unknown) => void;
}

/** SingleSymbolBacktestPanel：事件驱动回测 + 规则模板 + 成本配置 + 结果详情 + 结果应用到组合 */
export interface SingleSymbolBacktestPanelProps {
  /** 组合 ID */
  portfolioId: number | null;
  /** 当前标的 ID */
  activeSymbolId: number | null;
  /** 来源上下文（用于结果保存来源追溯） */
  sourceContext?: SourceContext;
  /** 回测结果 */
  backtestResult: BacktestRun | null;
  /** 设置回测结果 */
  onSetBacktestResult: (result: BacktestRun | null) => void;
  /** 应用到组合后的回调 */
  onAppliedToPortfolio: () => Promise<void> | void;
}

// ─── 默认值常量（供 Shell 初始化使用） ───────────────────

export const DEFAULT_RISK_SETTINGS: RiskSettings = {
  atrMultiplier: 2,
  concentrationMediumPct: 10,
  concentrationHighPct: 20,
  maxLossPct: 2,
};

export const DEFAULT_ALERT_SETTINGS: AlertSettings = {
  enableStopLoss: true,
  enableTarget: true,
  enableRsi: true,
  enableMacd: true,
  stopNearPct: 3,
  targetNearPct: 5,
  rsiOverbought: 75,
  rsiOversold: 25,
};

export const DEFAULT_FUTURE_TUNINGS: FuturePlanTunings = {
  general: { scalePct: 100, bandPct: 1.5 },
  short: { horizonDays: 5, scalePct: 70, bandPct: 1.2 },
  mid: { horizonDays: 15, scalePct: 90, bandPct: 1.5 },
  long: { horizonDays: 30, pullbackPct: 0, bandPct: 5 },
  custom: { horizonDays: 20, pullbackPct: 3, positionPct: 5, bandPct: 1.5 },
};

// ─── 工具函数（供 Shell 与子组件共用） ───────────────────

/** 从 localStorage 读取对象，合并 fallback */
export function readStoredObject<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? { ...fallback, ...JSON.parse(raw) } : fallback;
  } catch {
    return fallback;
  }
}

/** 从 TradeSetup 构造交易计划草稿 */
export function planDraftFromSetup(setup: TradeSetup): TradePlanDraft {
  return {
    entry_min: setup.entry_min,
    entry_max: setup.entry_max,
    stop_loss: setup.stop_loss,
    target_price: setup.target_price,
    recommended_position_pct:
      Math.round((setup.recommended_position_pct ?? 0) * 10000) / 100,
    recommended_position_amount: setup.recommended_position_amount,
  };
}

/** 从 TradeSetup 构造分批计划草稿 */
export function trancheDraftFromSetup(setup: TradeSetup): TrancheDraft[] {
  // trancheLabel 在 i18n 模块，避免循环依赖，由调用方传入或就地使用
  // 这里沿用 setup 自身 label（如未设置则空字符串）
  return (setup.tranche_plan ?? []).map((item) => ({
    label: item.label || "",
    position_pct: Math.round(Number(item.position_pct || 0) * 10000) / 100,
    amount: Number(item.amount || 0),
    trigger: item.trigger || "",
  }));
}

/** 规范化分批草稿（过滤空项、转换百分比） */
export function normalizeTrancheDrafts(drafts: TrancheDraft[]): TradeSetupTranche[] {
  return drafts
    .filter(
      (item) =>
        Number(item.position_pct || 0) > 0 ||
        Number(item.amount || 0) > 0 ||
        item.trigger.trim(),
    )
    .map((item) => ({
      label: item.label.trim() || "Custom",
      position_pct: Math.max(0, Number(item.position_pct || 0)) / 100,
      amount: Math.max(0, Number(item.amount || 0)),
      trigger: item.trigger.trim(),
    }));
}

/** TradeSetupOverrides 类型再导出，便于子组件引用 */
export type { TradeSetupOverrides };
