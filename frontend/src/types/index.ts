// Core types matching backend schemas

export interface Symbol {
  id: number;
  symbol: string;
  name: string;
  asset_type: string;
  market: string;
  region?: string;
  board?: string;
  industry?: string;
  theme?: string;
  listed_at?: string | null;
  created_at?: string;
}

export interface Score {
  id: number;
  symbol_id: number;
  trade_date: string;
  quality_score: number;
  quality_grade: string;
  timing_score: number;
  stage: string;
  action: string;
  priority_score: number;
  // Detailed sub-scores used by the scoring pipeline.
  trend_score?: number;
  momentum_score?: number;
  volatility_score?: number;
  liquidity_score?: number;
  breadth_score?: number;
  event_score?: number;
  // Entry/exit structure details derived from breakout and pullback logic.
  breakout_score?: number;
  pullback_score?: number;
  overheat_penalty?: number;
  // Data credibility indicator. Lower values imply weaker underlying coverage.
  data_credibility?: number;
  // 评分配置快照（P0：轻量自定义评分配置）
  scoring_asset_type?: string;
  scoring_config_id?: number;
  scoring_preset_key?: string;
  scoring_preset_name?: string;
  scoring_config_version?: number;
  scoring_config_snapshot_json?: string;
  dimension_scores_json?: string;
  factor_scores_json?: string;
  weight_mode?: 'manual' | 'shadow' | 'ridge';
  factor_model_run_id?: string;
  factor_data_cutoff_at?: string;
  factor_quality_score?: number;
  factor_timing_score?: number;
  model_alpha_score?: number;
  macro_regime?: 'risk_on' | 'neutral' | 'cautious' | 'defensive';
  macro_position_multiplier?: number;
  created_at?: string;
}

export interface PositionConstraint {
  key: string;
  limit_pct: number;
  used_pct: number;
  remaining_pct: number;
  amount?: number;
}

export interface AllocationSnapshot {
  total_capital?: number;
  investable_capital?: number;
  used_amount?: number;
  cash_amount?: number;
  investable_remaining_amount?: number;
  total_position_pct?: number;
  stock_position_pct?: number;
  etf_position_pct?: number;
  cash_pct?: number;
  investable_remaining_pct?: number;
  remaining_stock_pct?: number;
  remaining_etf_pct?: number;
  position_count?: number;
  sector_exposure?: Record<string, number>;
  sector_amount?: Record<string, number>;
  stock_amount?: number;
  etf_amount?: number;
}

export interface TradeSetupOverrides {
  entry_min?: number | null;
  entry_max?: number | null;
  stop_loss?: number | null;
  target_price?: number | null;
  recommended_position_pct?: number | null;
  recommended_position_amount?: number | null;
}

export interface TradeSetupTranche {
  label: string;
  position_pct: number;
  amount: number;
  trigger: string;
}

export interface FuturePlanTuning {
  horizonDays?: number;
  pullbackPct?: number;
  positionPct?: number;
  bandPct?: number;
  scalePct?: number;
}

export interface TradeSetup {
  id: number;
  portfolio_id: number;
  symbol_id: number;
  score_id: number | null;
  scan_run_id: number | null;
  stage: string;
  action: string;
  entry_min: number | null;
  entry_max: number | null;
  stop_loss: number | null;
  target_price: number | null;
  recommended_position_pct: number;
  recommended_position_amount: number;
  suggested_buy_pct?: number;
  suggested_buy_amount?: number;
  risk_reward_ratio: number | null;
  allow_add_position: boolean;
  is_sector_overweight: boolean;
  is_asset_overweight: boolean;
  can_open?: boolean;
  decision?: string;
  blocked_reasons?: string[];
  position_constraints?: PositionConstraint[];
  open_slots_remaining?: number | null;
  allocation_snapshot?: AllocationSnapshot | null;
  setup_reason: string | null;
  manual_overrides_json?: string | null;
  field_sources_json?: string | null;
  manual_overrides?: Record<string, number | null>;
  field_sources?: Record<string, "system" | "manual" | string>;
  manual_tranche_plan_json?: string | null;
  created_at: string;
  moving_averages?: { ma10?: number; ma20?: number };
  chart_signals?: Array<{ kind: string; label: string; price: number }>;
  tranche_plan?: TradeSetupTranche[];
  future_buy_plan?: FutureBuyPlan[];
  stage_cap_pct?: number;
  stage_cap_amount?: number;
  remaining_stage_pct?: number;
  remaining_stage_amount?: number;
  current_position_pct?: number;
  current_position_amount?: number;
  risk_budget_amount?: number;
  risk_per_share?: number;
  risk_capped_shares?: number | null;
  risk_capped_amount?: number | null;
  return_scenarios?: ReturnScenarios;
}

export interface FutureBuyPlan {
  label: string;
  horizon_days: number;
  zone_min: number | null;
  zone_max: number | null;
  priority: string;
  position_pct: number;
  amount: number;
  trigger: string;
}

export interface ReturnScenarios {
  confidence_pct: number;
  horizon_days: number;
  reference_price: number;
  planned_order?: { quantity: number; amount: number };
  expected: { exit_price: number };
  optimistic: { exit_price: number };
  pessimistic: { exit_price: number };
}

export interface SignalStats {
  sample_count: number;
  min_sample_count: number;
  matched_count: number;
  win_rate_5d: number | null;
  win_rate_20d: number | null;
  avg_return_20d: number | null;
  avg_max_gain_20d: number | null;
  avg_max_drawdown_20d: number | null;
  best_return_20d: number | null;
  worst_return_20d: number | null;
  scope?: { max_samples: number; min_sample_count: number };
  win_rate_3d?: number | null;
  win_rate_10d?: number | null;
  avg_return_3d?: number | null;
  avg_return_10d?: number | null;
  avg_max_gain_3d?: number | null;
  avg_max_drawdown_3d?: number | null;
  avg_max_gain_10d?: number | null;
  avg_max_drawdown_10d?: number | null;
}

export interface Position {
  symbol_id: number;
  symbol: string;
  name: string;
  quantity: number;
  avg_cost: number;
  latest_price: number;
  market_value: number;
  position_pct: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  asset_type?: string;
  theme?: string;
}

export interface WorkbenchBar {
  trade_date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface JournalEntry {
  id: number;
  title: string;
  entry_type: string;
  symbol_id: number;
  created_at: string;
  trade_setup_id?: number;
  content?: string;
  subjective_view?: string;
  follow_system?: number;
  outcome?: string;
  review_note?: string;
  updated_at?: string;
  score_id?: number;
  stage?: string;
  action?: string;
  actual_action?: string;
  review_tags?: string;
}

export interface UnifiedTask {
  id: string;
  source: "async" | "discovery";
  task_type: string;
  status: string;
  stage: string;
  percent: number;
  message: string;
  total: number;
  processed: number;
  ok_count: number;
  failed_count: number;
  current_item?: string;
  payload: Record<string, any>;
  result: Record<string, any>;
  errors: any[];
  // WPD-05: 顶层 error_code，从 errors_json[0].error_code 提取（可能为 null/undefined）
  error_code?: string | null;
  duration_sec?: number;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  updated_at: string;
}

export interface AlertRule {
  id: number;
  name: string;
  alert_type: string;
  enabled: number;
  severity: string;
  config_json: string | null;
  last_triggered_at?: string;
  cooldown_minutes: number;
  created_at: string;
  updated_at: string;
}

export interface AlertEvent {
  id: number;
  rule_id: number;
  alert_type: string;
  severity: string;
  title: string;
  message: string;
  symbol_id?: number;
  symbol?: { id: number; symbol: string; name: string } | null;
  data?: Record<string, any>;
  acknowledged: number;
  resolved?: boolean;
  resolved_at?: string | null;
  technical_details?: string | null;
  created_at: string;
}

export interface TradeRecord {
  id: number;
  symbol_id: number;
  symbol: string;
  name: string;
  side: string;
  quantity: number;
  price: number;
  amount: number;
  fee: number;
  realized_pnl: number;
  created_at: string;
}

export interface WorkbenchCandidate {
  symbol_id: number;
  symbol: string;
  name: string;
  region: string;
  asset_type: string;
  market: string;
  rank_no?: number;
  quality_score: number;
  timing_score: number;
  priority_score: number;
  trend_score?: number;
  momentum_score?: number;
  volatility_score?: number;
  liquidity_score?: number;
  breadth_score?: number;
  event_score?: number;
  data_credibility?: number | null;
  stage: string;
  action: string;
  recommended_position_pct: number;
  is_sector_overweight?: boolean;
  is_asset_overweight?: boolean;
  reason_tags?: string[];
  scan_result_id?: number;
  id?: number;
  warning_days?: number;
  valid_days?: number;
  is_frozen?: boolean;
  created_at?: string;
  base_opportunity_score?: number;
  final_opportunity_score?: number;
  news_message_score?: number;
  news_confidence?: number;
  news_multiplier?: number;
  news_adjustment_pct?: number;
  // P1：评分配置快照（用于按维度排序和"为什么入选"展示）
  scoring_preset_key?: string;
  scoring_preset_name?: string;
  scoring_config_version?: number;
  dimension_scores_json?: string;
  scoring_config_snapshot_json?: string;
  weight_mode?: 'manual' | 'shadow' | 'ridge';
  factor_model_run_id?: string;
  factor_data_cutoff_at?: string;
  factor_quality_score?: number;
  factor_timing_score?: number;
  model_alpha_score?: number;
  macro_regime?: 'risk_on' | 'neutral' | 'cautious' | 'defensive';
  macro_position_multiplier?: number;
  // P1：挖掘候选手动晋升状态（discovery_candidates 表）
  candidate_id?: number | null;
  is_promoted?: number | null; // 0=未晋升 1=已加入候选池 null=历史数据无状态
  is_legacy?: boolean;
}

export interface WorkbenchScore extends WorkbenchCandidate {
  trade_date?: string;
}

export interface WorkbenchWatchlist {
  id: number;
  name: string;
  list_type: string;
  item_count: number;
}

export interface WorkbenchLatestScan {
  scan_run_id: number | null;
  run_name: string;
  created_at: string;
  executable_count: number;
  total_results: number;
  auto_scan: boolean;
}

export interface WorkbenchActiveRule {
  id: number;
  rule_name: string;
  max_single_position_pct: number;
  max_sector_position_pct: number;
  max_stock_position_pct: number;
  max_etf_position_pct: number;
  max_loss_per_trade_pct?: number;
  max_open_positions: number;
}

export interface WorkbenchMarketScope {
  selected_group: string;
  available_groups: string[];
  total_symbols: number;
  filtered_symbols: number;
  region_counts: Record<string, number>;
}

export interface AccountSummary {
  cash_balance: number;
  available_cash: number;
  market_value: number;
  total_equity: number;
  realized_pnl: number;
  unrealized_pnl: number;
  cash_pct: number;
  invested_pct: number;
  position_count: number;
  trade_count_7d: number;
  last_trade_at: string | null;
}

export interface DashboardOverview {
  symbols_count: number;
  watchlists_count: number;
  total_position_pct: number;
  cash_pct: number;
  top_candidates: Array<Record<string, unknown>>;
  risk_flags: string[];
}

export interface DashboardWorkbench {
  portfolio: {
    id: number;
    name: string;
    currency: string;
    total_capital: number;
    investable_ratio: number;
    cash_reserve_ratio: number;
  };
  active_rule: WorkbenchActiveRule | null;
  market_scope: WorkbenchMarketScope | null;
  overview: DashboardOverview;
  account_summary: AccountSummary | null;
  latest_scan: WorkbenchLatestScan;
  candidates: WorkbenchCandidate[];
  latest_scores: WorkbenchScore[];
  positions: Position[];
  watchlists: WorkbenchWatchlist[];
  journals: JournalEntry[];
  recent_trades: TradeRecord[];
}

export interface SymbolDetail {
  symbol: Symbol;
  latest_score: Score | null;
  latest_trade_setup: TradeSetup | null;
  signal_stats: SignalStats | null;
  position: Position | null;
  score_history: Score[];
  bars: WorkbenchBar[];
  journals: JournalEntry[];
  recent_trades: TradeRecord[];
}

export interface Portfolio {
  id: number;
  name: string;
  account_type: string;
  asset_scope: "stock" | "etf" | "mixed";
  total_capital: number;
  investable_ratio: number;
  cash_reserve_ratio: number;
  currency: string;
  is_default: number;
  auto_trade_enabled: number;
  auto_trade_last_run_at: string | null;
  created_at: string;
  updated_at?: string;
}

// P0-6：组合 CRUD 新增类型
export interface PortfolioCreatePayload {
  name: string;
  account_type: string;  // "simulated" | "manual"
  asset_scope?: "stock" | "etf" | "mixed";
  total_capital: number;
  investable_ratio: number;
  cash_reserve_ratio: number;
  currency?: string;
  is_default?: boolean;
  auto_trade_enabled?: boolean;
}

export interface PortfolioUpdatePayload {
  name?: string;
  asset_scope?: "stock" | "etf" | "mixed";
  total_capital?: number;
  investable_ratio?: number;
  cash_reserve_ratio?: number;
  currency?: string;
  is_default?: boolean;
  auto_trade_enabled?: boolean;
}

// P2-3：自动交易执行结果
export interface AutoTradePlanItem {
  symbol_id: number;
  symbol: string;
  name: string;
  action: string;
  stage?: string | null;
  ref_price: number;
  executed: boolean;
  order_id?: number | null;
  filled_price?: number | null;
  fee?: number | null;
  // 卖出特有
  held_quantity?: number | null;
  sell_quantity?: number | null;
  reason?: string | null;
  // 买入特有
  can_open?: boolean | null;
  decision?: string | null;
  blocked_reasons?: string[];
  recommended_amount?: number | null;
  buy_quantity?: number | null;
}

// P0-AutoTrade：就绪检查 issue（blocker/warning 通用结构）
export interface ReadinessIssue {
  code: string;
  message: string;
  detail?: unknown;
}

// P0-AutoTrade：自动交易就绪状态（对齐 /auto-trade/readiness 响应）
export interface AutoTradeReadiness {
  portfolio_id: number;
  for_schedule?: boolean;
  ready: boolean;
  enabled: boolean;
  account_ready: boolean;
  data_ready: boolean;
  source_ready: boolean;
  schedule_ready: boolean;
  blockers: ReadinessIssue[];
  warnings: ReadinessIssue[];
  source_mode?: string;
  executed_at?: string | null;
}

// P0-AutoTrade：dry-run diff（与后端 auto_trade_dual_run diffs 对齐）
export interface AutoTradeDryRunDiff {
  symbol_id: number;
  side: "buy" | "sell" | string;
  old_action: string | null;
  new_action: string | null;
  reason: string;
  detail: string;
}

export interface AutoTradeResult {
  portfolio_id: number;
  dry_run: boolean;
  sells: AutoTradePlanItem[];
  buys: AutoTradePlanItem[];
  errors: string[];
  executed_at: string;
  // P0-AutoTrade：新增诊断字段
  executed_source?: "old" | "new" | string | null;
  member_source_enabled?: boolean;
  source_mode?: string | null;
  readiness?: Omit<AutoTradeReadiness, "portfolio_id"> | null;
  blockers?: ReadinessIssue[];
  warnings?: ReadinessIssue[];
  diffs?: AutoTradeDryRunDiff[];
}

// P2-2: 组合整体回测结果
// WP7.1/WP7.3: 新增 symbol_source / source_type / excluded_member_count
export interface PortfolioBacktestResult {
  run_id: number;
  portfolio_id: number;
  symbol_ids: number[];
  symbol_count: number;
  start_date: string;
  end_date: string;
  initial_capital: number;
  status: string;
  run_name: string;
  symbol_source?: string | null;
  source_type?: string | null;
  excluded_member_count?: number;
}

export interface SignalRulePreset {
  mode: string;
  rule_name: string;
  description: string;
  quality_tolerance: number;
  timing_tolerance: number;
  min_sample_count: number;
  max_samples: number;
  same_region: boolean;
  same_asset_type: boolean;
  same_stage: boolean;
  same_action: boolean;
}

export interface SignalRule {
  id: number | null;
  portfolio_id: number;
  rule_name: string;
  mode: string;
  quality_tolerance: number;
  timing_tolerance: number;
  min_sample_count: number;
  max_samples: number;
  same_region: boolean;
  same_asset_type: boolean;
  same_stage: boolean;
  same_action: boolean;
  is_active: boolean;
}

export interface SignalRulePreviewResult {
  symbol_id: number;
  symbol: string;
  name: string;
  status: string;
  message: string;
  stats: SignalStats | null;
}

export interface DiscoveryTask {
  id: string | number;
  status: string;
  stage: string;
  percent: number;
  message: string;
  scope: string;
  total: number;
  processed: number;
  ok_count: number;
  failed_count: number;
  empty_count: number;
  scored_count: number;
  current_symbol: string | null;
  scan_run_id: number | null;
  executable_count: number;
  cleanup_count?: number;
  errors: Array<Record<string, unknown>>;
  can_resume: boolean;
  can_retry?: boolean;
  created_at: string;
  started_at?: string | null;
  paused_at?: string | null;
  cancelled_at?: string | null;
  finished_at?: string | null;
  updated_at: string;
}

export interface DiscoveryScopeStats {
  scope: string;
  total_symbols: number;
  cached_symbols: number;
  active_symbols: number;
}


export interface DiscoveryIndicatorEvaluation {
  scan_result_id: number;
  symbol_id: number;
  values: Record<string, boolean | number | null>;
}

export interface NewsEvent {
  symbol_id: number;
  symbol: string;
  title: string;
  source: string;
  url: string;
  event_type: string;
  sentiment: string;
  strength: number;
  effective_score: number;
  risk_level: string;
  published_at: string;
  expires_at: string | null;
}

export interface NewsSymbolSummary {
  symbol_id: number;
  symbol: string;
  name: string;
  message_score: number;
  sentiment: string;
  risk_level: string;
  confidence: number;
  positive_count: number;
  negative_count: number;
  risk_count: number;
  latest_title: string;
  events: NewsEvent[];
}

export interface NewsMacroSummary {
  message_score: number;
  sentiment: string;
  risk_level: string;
  summary: string;
  events: NewsEvent[];
}

export interface NewsSnapshot {
  symbols_total: number;
  macro: NewsMacroSummary | null;
  symbols: NewsSymbolSummary[];
}

export interface MacroIndicator {
  id?: number | null;
  region: string;
  category: string;
  indicator_key: string;
  name: string;
  period: string;
  value: number | null;
  previous_value: number | null;
  delta: number | null;
  unit: string | null;
  frequency: string;
  source: string;
  score: number;
  status: string;
  updated_at?: string | null;
}

export interface MacroSnapshotSummary {
  id?: number | null;
  region: string;
  market_score: number;
  stance: string;
  summary: string;
  growth_score: number;
  inflation_score: number;
  liquidity_score: number;
  credit_score: number;
  risk_score: number;
  indicators_total: number;
  failed_total: number;
  created_at?: string | null;
}

export interface MacroOverview {
  region: string;
  snapshot: MacroSnapshotSummary | null;
  indicators: MacroIndicator[];
  brief: string[];
  failed: Array<Record<string, unknown>>;
}

export interface WatchlistItem {
  id: number;
  watchlist_id: number;
  symbol_id: number;
  added_at: string;
  symbol: Symbol | null;
}

export interface SimOrderResult {
  order: Record<string, unknown>;
  trade: TradeRecord;
  summary: AccountSummary;
}

export interface MarketDataUpdateResponse {
  data: {
    ok_count: number;
    symbols_total: number;
    failed_count: number;
  };
}

// Market events and news items surfaced in the workbench.
// Scope, impact, and source metadata are used for filtering and display.

export interface MarketEvent {
  id: number;
  title: string;
  summary: string | null;
  impact_scope: string;       // macro_policy | sector_dynamics | international | breaking | fund_flow | sentiment | other
  importance_level: number;   // 1-5
  affected_market: string;     // A-share | Hong Kong | US
  affected_sectors: string | null;
  affected_symbols: string | null;
  sentiment: string;          // positive | negative | neutral
  source: string;             // cctv | baidu | baidu-report | manual | ...
  source_url: string | null;
  is_manual: number;          // 0 = synced from source, 1 = manually entered
  published_at: string | null;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface MarketEventListResponse {
  events: MarketEvent[];
  total: number;
  by_scope: Record<string, number>;
  by_level: Record<string, number>;
}

export interface MarketEventCollectRequest {
  days?: number;
  sources?: string[];
}

export interface MarketEventCollectResponse {
  collected: number;
  skipped_duplicate: number;
  errors: string[];
}

export interface DataHealthIssue {
  level: "ok" | "warn" | "error" | string;
  message: string;
}

export interface DataHealthBarIssue {
  symbol_id: number;
  symbol: string;
  name: string;
  asset_type: string;
  market: string;
  theme: string | null;
  latest_trade_date: string | null;
  latest_age_days: number | null;
  reason: "missing_bars" | "stale_bars" | string;
}

export interface DataHealth {
  status: "ok" | "warn" | "error" | string;
  score: number;
  updated_at: string;
  issues: DataHealthIssue[];
  symbols: {
    total: number;
    by_region: Record<string, number>;
    by_asset_type: Record<string, number>;
  };
  bars: {
    total: number;
    covered_symbols: number;
    coverage_pct: number;
    latest_trade_date: string | null;
    latest_age_days: number | null;
    missing_symbols: number;
    outdated_symbols: number;
    stale_symbols: number;
    stale_pct: number;
    stale_cutoff: string | null;
    repair_hint: string;
    missing_samples: DataHealthBarIssue[];
    stale_samples: DataHealthBarIssue[];
  };
  scores: {
    scored_symbols: number;
    latest_trade_date: string | null;
    latest_age_days: number | null;
  };
  macro: {
    indicators_total: number;
    latest_updated_at: string | null;
    latest_age_days: number | null;
    market_score: number | null;
    failed_total: number;
  };
  market_events: {
    latest_at: string | null;
    latest_age_days: number | null;
    events_7d: number;
    important_events_7d: number;
  };
  discovery: {
    latest_task: null | {
      id: string;
      status: string;
      stage: string;
      percent: number;
      total: number;
      processed: number;
      updated_at: string;
    };
    warning_results: number;
    expired_results: number;
    frozen_results: number;
  };
}

export interface BacktestSummary {
  total_return_pct?: number | null;
  max_drawdown_pct?: number | null;
  sharpe_ratio?: number | null;
  win_rate?: number | null;
  profit_factor?: number | null;
  trade_count?: number | null;
  avg_holding_days?: number | null;
}

export interface BacktestPricePoint {
  symbol_id: number;
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface BacktestDiagnostics {
  checked_days?: number;
  buy_signal_days?: number;
  trade_count?: number;
  skip_reasons?: Record<string, number>;
  sample_misses?: Array<Record<string, unknown>>;
  fill_warning?: string;
  execution?: { entry_timing?: string; exit_timing?: string; entry_price_field?: "open" | "close" | string; exit_price_field?: "open" | "close" | string };
  buy_conditions?: Record<string, unknown>;
  sell_conditions?: Record<string, unknown>;
  position_config?: Record<string, unknown>;
}

export interface BacktestConditionTrace {
  field: string;
  operator: string;
  expected?: unknown;
  actual?: unknown;
  matched: boolean;
  indicator_key?: string | null;
  indicator_name?: string | null;
  value_type?: string | null;
}

export interface BacktestTrade {
  id: number;
  run_id: number;
  symbol_id: number;
  entry_date: string;
  entry_price: number;
  entry_signal_date?: string | null;
  entry_signal_price?: number | null;
  entry_signal_price_field?: string | null;
  entry_execution_timing?: string | null;
  quantity: number;
  exit_date?: string | null;
  exit_price?: number | null;
  exit_signal_date?: string | null;
  exit_signal_price?: number | null;
  exit_signal_price_field?: string | null;
  exit_execution_timing?: string | null;
  exit_reason?: string | null;
  pnl?: number | null;
  pnl_pct?: number | null;
  hold_days?: number | null;
  entry_cost: number;
  exit_cost?: number | null;
  entry_traces?: BacktestConditionTrace[];
  exit_traces?: BacktestConditionTrace[];
}

export interface BacktestRun {
  id: number;
  portfolio_id: number;
  run_name: string;
  symbols_json: string;
  rule_config_json: string;
  cost_config_json?: string | null;
  start_date: string;
  end_date: string;
  initial_capital: number;
  total_return?: number | null;
  total_return_pct?: number | null;
  max_drawdown?: number | null;
  max_drawdown_pct?: number | null;
  sharpe_ratio?: number | null;
  win_rate?: number | null;
  profit_factor?: number | null;
  trade_count?: number | null;
  avg_holding_days?: number | null;
  equity_curve_json?: string | null;
  status: string;
  error_message?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  trades?: BacktestTrade[];
  price_series?: BacktestPricePoint[];
  diagnostics?: BacktestDiagnostics;
  summary?: BacktestSummary;
  // WP7.2 回测快照字段（旧回测这些字段为 null/undefined）
  member_snapshot_json?: string | null;
  symbol_ids_json?: string | null;
  excluded_members_json?: string | null;
  portfolio_rule_version_id?: number | null;
  score_mode?: string | null;
  data_cutoff_at?: string | null;
  engine_name?: string | null;
  engine_version?: string | null;
  source_type?: string | null;
}

// Custom indicator definitions and preview payloads.


export interface CustomIndicatorParamDef {
  key: string;
  label: string;
  type: "number" | "text" | "select";
  default: number | string;
}

export interface CustomIndicator {
  id: number;
  name: string;
  key: string;
  description: string;
  category: string;
  formula: string;
  value_type: "boolean" | "number";
  params: CustomIndicatorParamDef[];
  scope: string[];
  enabled: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

/** WP4-01: 数值指标提升为因子草稿的响应。 */
export interface CustomIndicatorPromoteResponse {
  success: boolean;
  factor_id: number;
  factor_code: string;
  factor_version_id: number;
  factor_version: number;
  lifecycle_status: string;
  origin: string;
  source_mapping: {
    source_type?: string;
    indicator_id?: number;
    indicator_key?: string;
    indicator_version?: number;
    promoted_at?: string;
    [key: string]: unknown;
  };
  message?: string;
  request_id?: string;
}

export type CustomIndicatorPayload = Omit<CustomIndicator, "id" | "version" | "created_at" | "updated_at"> & {
  change_note?: string;
};

export interface CustomIndicatorVersion {
  id: number;
  indicator_id: number;
  version: number;
  formula: string;
  params_json: string;
  value_type: "boolean" | "number";
  change_note: string;
  created_at: string;
}

export interface CustomIndicatorPreviewBar {
  trade_date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
}

export interface CustomIndicatorPreviewScore {
  quality_score?: number | null;
  timing_score?: number | null;
  trend_score?: number | null;
  momentum_score?: number | null;
}

export interface CustomIndicatorPreviewSeriesItem {
  trade_date: string;
  value_type: "boolean" | "number";
  result_boolean?: boolean | null;
  result_number?: number | null;
  display_value: string;
  latest_bar: CustomIndicatorPreviewBar;
  score_snapshot?: CustomIndicatorPreviewScore | null;
}

export interface CustomIndicatorPreviewRead {
  ok: boolean;
  message: string;
  symbol_id: number;
  symbol: string;
  name: string;
  trade_date: string;
  value_type: "boolean" | "number";
  result_boolean?: boolean | null;
  result_number?: number | null;
  display_value: string;
  latest_bar: CustomIndicatorPreviewBar;
  score_snapshot?: CustomIndicatorPreviewScore | null;
  recent_results?: CustomIndicatorPreviewSeriesItem[];
}

export interface DiscoveryPlanFilter {
  id: string;
  indicator_key?: string;
  operator: "gt" | "gte" | "lt" | "lte" | "eq" | "neq";
  number_value: number;
  boolean_value: boolean;
}

export interface DiscoveryPlan {
  id: number;
  name: string;
  logic: "AND" | "OR";
  pool_tab: "all" | "highQuality" | "highTiming" | "actionable" | "overheatRisk" | "lowCredibility";
  filters: DiscoveryPlanFilter[];
  created_at: string;
  updated_at: string;
}

export type DiscoveryPlanPayload = Omit<DiscoveryPlan, "id" | "created_at" | "updated_at">;
export type ConditionOperator = "gt" | "gte" | "lt" | "lte" | "eq" | "neq" | "in" | "not_in";
export type LogicOperator = "AND" | "OR";

export interface ConditionLeaf {
  field: string;
  operator: ConditionOperator;
  value: number | string | boolean | (string | number)[];
  params?: Record<string, number | string>;
}

export interface ConditionGroup {
  logic: LogicOperator;
  conditions: (ConditionLeaf | ConditionGroup)[];
}

export function isConditionGroup(node: ConditionLeaf | ConditionGroup): node is ConditionGroup {
  return "logic" in node;
}

export interface BacktestRuleConfigV2 {
  version: 2;
  buy_conditions: ConditionGroup;
  sell_conditions: ConditionGroup;
  position_config: Record<string, unknown>;
  execution_config?: Record<string, unknown>;
}

export interface RuleTemplate {
  id: number;
  name: string;
  description: string;
  rule_config: BacktestRuleConfigV2 | Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ConditionFieldDef {
  key: string;
  labelKey: string;
  category: "basic" | "sub_score" | "technical" | "sell";
  valueType: "number" | "string" | "boolean" | "string_list";
  operators: ConditionOperator[];
  params?: { key: string; labelKey: string; type: "number" | "text" | "select"; default: number | string; placeholder?: string }[];
  requiresHistory: boolean;
  side?: "buy" | "sell" | "both";
}

// Database configuration for optional MySQL connectivity.

export interface MySQLConfig {
  host: string;
  port: number;
  database: string;
  user: string;
  password: string;
}

export interface DbConfig {
  use_mysql: boolean;
  mysql: MySQLConfig;
}

export interface TestConnectionResult {
  success: boolean;
  message: string;
  server_version?: string;
}

export interface MigrationProgress {
  status: "idle" | "running" | "completed" | "failed" | "cancelled";
  current_table?: string;
  tables_done: number;
  tables_total: number;
  rows_migrated: number;
  error?: string;
}


export interface HistoryInitializationStage {
  key: "prepare" | "sync_bars" | "calc_scores" | "scan" | "finalize" | string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  percent: number;
  done: number;
  total: number;
  message?: string | null;
}

export type HistoryInitializationSymbolSource =
  | "all" | "watchlist" | "positions" | "scored" | "candidates" | "cn-stock" | "cn-etf";

export interface HistoryInitializationSummary {
  symbols_total: number;
  sync_ok_count: number;
  sync_failed_count: number;
  empty_count: number;
  bars_rows: number;
  score_days_total: number;
  score_days_completed: number;
  scan_run_id?: number | null;
  scan_executable_count?: number;
}

export interface HistoryInitializationFailureItem {
  symbol_id: number;
  symbol: string;
  name?: string | null;
  asset_type?: string | null;
  stage: string;
  message: string;
  failed_days: number;
  last_trade_date?: string | null;
}

export interface HistoryInitializationRunRecord {
  task_id?: string | null;
  status: "idle" | "running" | "completed" | "failed" | "cancelled";
  preset: "1m" | "1q" | "1y" | "3y";
  adjust: string;
  repair_mode?: "both" | "bars" | "scores";
  symbol_source?: HistoryInitializationSymbolSource;
  auto_scan?: boolean;
  portfolio_id?: number | null;
  watchlist_id?: number | null;
  asset_types?: string[] | null;
  symbol_ids?: number[];
  start_date?: string | null;
  end_date?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
  message?: string | null;
  stages: HistoryInitializationStage[];
  failed_items: HistoryInitializationFailureItem[];
  summary: HistoryInitializationSummary;
}

export interface HistoryInitializationTask {
  task_id?: string | null;
  status: "idle" | "running" | "completed" | "failed" | "cancelled";
  preset: "1m" | "1q" | "1y" | "3y";
  adjust: string;
  repair_mode?: "both" | "bars" | "scores";
  symbol_source?: HistoryInitializationSymbolSource;
  auto_scan?: boolean;
  portfolio_id?: number | null;
  watchlist_id?: number | null;
  asset_types?: string[] | null;
  symbol_ids?: number[];
  start_date?: string | null;
  end_date?: string | null;
  progress_pct: number;
  message?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
  stages: HistoryInitializationStage[];
  failed_items: HistoryInitializationFailureItem[];
  summary: HistoryInitializationSummary;
  recent_runs?: HistoryInitializationRunRecord[];
}

export interface BacktestCoverageIssue {
  symbol_id: number;
  symbol?: string;
  name?: string;
  bar_days: number;
  score_days: number;
  coverage_pct: number;
  missing_days: number;
  bar_start?: string | null;
  bar_end?: string | null;
  score_start?: string | null;
  score_end?: string | null;
}

export interface BacktestCoverageWarning {
  code: "BACKTEST_SCORE_COVERAGE_INSUFFICIENT" | string;
  message: string;
  summary: {
    symbols_total: number;
    symbols_ready: number;
    symbols_missing: number;
    recommended_preset: "1m" | "1q" | "1y" | "3y";
    min_coverage_pct?: number;
  };
  issues: BacktestCoverageIssue[];
}

// ----------------------------------------------------------------------------
// WP8.3：绩效归因、复盘与基准对比类型
// ----------------------------------------------------------------------------

/**
 * 单个归因条目。各维度通用结构：
 * - label: 分组标签（成员名 / 执行模式 / 来源 / 规则版本等）
 * - contribution_pct: 贡献占比
 * - pnl: 盈亏金额
 * - trade_count: 交易笔数
 * - detail / extra: 维度专属补充字段
 */
export interface AttributionItem {
  label: string;
  contribution_pct: number | null;
  pnl: number | null;
  trade_count: number | null;
  detail?: string | null;
  extra?: Record<string, string | number | null>;
}

/**
 * 归因维度结果。
 * - items: 分组条目
 * - sample_warning: 样本不足提示（null 表示样本充足）
 * - sample_size: 采样交易笔数（可选，后端未返回时前端用 items 推断）
 * - group_count: 分组数（可选）
 */
export interface AttributionDimension {
  items: AttributionItem[];
  sample_warning: string | null;
  sample_size?: number | null;
  group_count?: number | null;
}

/** 回测 vs 模拟 偏差。字段为后端返回的指标差异，保留扩展。 */
export interface BacktestVsSimDiff {
  total_return_pct?: number | null;
  max_drawdown_pct?: number | null;
  sharpe_ratio?: number | null;
  win_rate?: number | null;
  trade_count?: number | null;
  [key: string]: unknown;
}

export interface BacktestVsSim {
  diff: BacktestVsSimDiff;
  explanation: string;
}

/** 成本影响汇总。 */
export interface CostImpact {
  total_cost: number;
  slippage_cost: number;
  rejected_count: number;
  risk_blocked_count: number;
  impact_pct: number;
}

/** 基准对比（沪深 300 / 中证 500 等）。后端未配置时为 null。 */
export interface BenchmarkComparison {
  name: string;
  excess_return: number | null;
  tracking_error: number | null;
  information_ratio: number | null;
}

/** 归因报告整体结构，对应 GET /portfolios/{id}/attribution 返回。 */
export interface AttributionReport {
  by_member: AttributionDimension;
  by_execution_mode: AttributionDimension;
  by_source: AttributionDimension;
  by_rule_signal: AttributionDimension;
  backtest_vs_sim: BacktestVsSim;
  cost_impact: CostImpact;
  summary: string;
  benchmark?: BenchmarkComparison | null;
}

/** 复盘记录。 */
export interface Review {
  id: number;
  portfolio_id: number;
  note: string;
  attribution_snapshot?: string | null;
  created_at: string;
  created_by?: string | null;
}

// ----------------------------------------------------------------------------
// WP-AI.7：AI 助手前端类型定义
// ----------------------------------------------------------------------------

/** AI 会话（对应后端 AISession 模型）。 */
export interface AISession {
  id: number;
  title: string;
  source_page: string | null;
  provider: string | null;
  model: string | null;
  profile_id: string | null;
  status: string;
  context_summary: string | null;
  total_tokens: number;
  total_cost: number | null;
  created_at: string | null;
  updated_at: string | null;
  deleted_at: string | null;
}

/** AI 会话列表响应。 */
export interface AISessionListResponse {
  items: AISession[];
  limit: number;
  offset: number;
  include_archived: boolean;
}

/** AI 消息（对应后端 AIMessage 模型）。 */
export interface AIMessage {
  id: number;
  session_id: number;
  role: string;
  content: string;
  context_summary: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  latency_ms: number | null;
  model_used: string | null;
  provider_used: string | null;
  metadata_json: string | null;
  created_at: string | null;
}

/** AI 消息列表响应。 */
export interface AIMessageListResponse {
  items: AIMessage[];
  session_id: number;
  limit: number;
  offset: number;
}

/** AI 动作审计（对应后端 AIActionAudit 模型）。 */
export interface AIActionAudit {
  id: number;
  message_id: number;
  action_type: string;
  suggested_payload: string;
  preview_result: string | null;
  user_confirmed: boolean;
  confirmed_at: string | null;
  final_result: string | null;
  rejected_reason: string | null;
  created_at: string | null;
}

/** AI Profile（对应后端 AIProfile 模型）。 */
export interface AIProfile {
  id: number;
  name: string;
  provider: string;
  base_url: string | null;
  model: string;
  auth_type: string | null;
  secret_key_ref: string | null;
  timeout_seconds: number;
  max_tokens: number;
  max_context_tokens: number;
  daily_request_limit: number;
  max_concurrent: number;
  purpose: string;
  priority: number;
  is_enabled: boolean;
  is_fallback: boolean;
  health_status: string;
  last_health_check: string | null;
  daily_request_count: number;
  daily_request_reset_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** AI Profile 测试连接结果。 */
export interface AIProfileTestResult {
  success: boolean;
  latency_ms: number;
  model_info: Record<string, unknown> | null;
  error: string | null;
}

/** AI Profile 用量。 */
export interface AIProfileUsage {
  profile_id: number;
  daily_request_count: number;
  daily_request_limit: number;
  remaining: number;
  daily_request_reset_at: string | null;
}

/** AI 健康状态条目。 */
export interface AIHealth {
  id: number;
  name: string;
  provider: string;
  model: string;
  priority: number;
  is_enabled: boolean;
  is_fallback: boolean;
  health_status: string;
  last_health_check: string | null;
  daily_request_count: number;
  daily_request_limit: number;
}

/** AI 统一响应结构（对应后端 AIResponse）。 */
export interface AIResponse {
  answer: string;
  evidence: Array<{
    type: string;
    source: string;
    content: string;
    confidence: number | null;
  }>;
  warnings: string[];
  suggested_actions: Array<{
    action_type: string;
    description: string;
    draft_id?: string;
  }>;
  draft: Record<string, unknown> | null;
  metadata: {
    data_as_of?: string | null;
    model_version?: string | null;
    rule_version?: string | null;
    provider_used?: string | null;
    latency_ms?: number | null;
    tokens?: number | null;
    [key: string]: unknown;
  };
}

/** AI 助手上下文（页面来源 + 引用 ID）。 */
export interface AIAssistantContext {
  source_page: string;
  references: Record<string, number | string>;
  initial_question?: string;
}

/** WP4-05: AI 草案审计详情（含原始建议与当前 payload 用于差异对比）。 */
export interface AIDraftDetail {
  audit_id: number;
  message_id: number;
  action_type: string;
  original_suggested_payload: Record<string, unknown>;
  current_payload: Record<string, unknown>;
  was_modified: boolean;
  user_confirmed: boolean;
  confirmed_at: string | null;
  final_result: Record<string, unknown> | null;
  rejected_reason: string | null;
  created_at: string;
}

/** WP4-05: AI 草案预览结果（dry-run 校验）。 */
export interface AIDraftPreviewResult {
  is_valid: boolean;
  errors: string[];
  changes: Array<{
    field: string;
    old_value: unknown;
    new_value: unknown;
    description: string;
  }>;
  extra?: Record<string, unknown>;
}

/** WP4-05: AI 草案执行结果。 */
export interface AIDraftExecuteResult {
  success: boolean;
  factor_id?: number;
  factor_code?: string;
  factor_version_id?: number;
  factor_version?: number;
  lifecycle_status?: string;
  message?: string;
  error?: string;
  idempotent?: boolean;
}

// WP-S-FIX.1: 能力门禁类型（对应后端 app/schemas/capability.py）
export interface CapabilityPrerequisite {
  key: string;
  label: string;
  satisfied: boolean;
  detail: string | null;
}

export interface CapabilityAction {
  label: string;
  action_type: "redirect" | "configure" | "sync" | "retry" | "dismiss";
  target: string | null;
  reason: string | null;
}

export interface CapabilityItem {
  key: string;
  label: string;
  status: "ready" | "degraded" | "blocked";
  reason_code: string | null;
  user_message: string;
  prerequisites: CapabilityPrerequisite[];
  recommended_actions: CapabilityAction[];
  data_cutoff_at: string | null;
  last_checked_at: string;
}

export interface CapabilitiesResponse {
  overall_status: "ready" | "degraded" | "blocked";
  capabilities: CapabilityItem[];
  checked_at: string;
}

// WP-P-FIX.1: 数据准备任务与快照状态类型
export interface AsyncTaskRead {
  id: string;
  task_type: string;
  status: string;
  payload: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  progress: number | null;
  message: string | null;
  error: string | null;
  result_json: Record<string, unknown> | null;
  // WPD-05: 顶层 error_code，从 errors_json[0].error_code 提取
  error_code?: string | null;
}

export interface SnapshotStatusRead {
  scope: string;
  has_ready_snapshot: boolean;
  ready_snapshot_id: number | null;
  ready_snapshot_generated_at: string | null;
  ready_snapshot_trade_date: string | null;
  ready_snapshot_symbol_count: number | null;
  ready_snapshot_dirty_symbol_count: number | null;
  has_building_snapshot: boolean;
  building_snapshot_id: number | null;
  building_snapshot_created_at: string | null;
  last_data_prep_task_id: string | null;
  last_data_prep_status: string | null;
  recommended_action: string | null;
  last_fast_scan_timings: Record<string, unknown> | null;
  last_fast_scan_status: string | null;
}
