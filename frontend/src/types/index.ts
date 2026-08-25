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
  // FR-P1-2 可靠性扩展（async 来源填充；discovery 保持 undefined）
  heartbeat_at?: string | null;
  stage_budget_seconds?: number | null;
  last_progress_at?: string | null;
  last_progress_percent?: number | null;
  suggested_action?: string | null;
  cancel_requested?: boolean;
  correlation_id?: string | null;
  idempotency_key?: string | null;
  is_terminal_locked?: boolean;
  cancelled_timeout_at?: string | null;
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
  // FR-P0-10：9 状态 PortfolioStatus（后端可选返回；null/undefined = 旧版本后端未提供，前端降级隐藏）
  portfolio_status?: PortfolioStatus | string | null;
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
  // Request parameters echoed from the persisted run.
  benchmark?: string | null;
  benchmark_code?: string | null;
  commission_rate?: number | null;
  stamp_tax_rate?: number | null;
  slippage_bps?: number | null;
  price_type?: "NEXT_OPEN" | "T_CLOSE" | string | null;
  volume_limit_pct?: number | null;
  rebalance_frequency?: "daily" | "weekly" | "monthly" | "on_signal" | string | null;
  pit_mode?: "legacy_research" | "research_pit" | "production_pit" | string | null;
  cost_config?: Record<string, unknown> | null;
  // Immutable strategy/data snapshot references.
  strategy_snapshot_id?: string | null;
  snapshot_no?: number | null;
  snapshot_hash?: string | null;
  factor_model_run_id?: string | null;
  factor_set_id?: string | null;
  factor_data_cutoff_at?: string | null;
  data_cutoff_at?: string | null;
  data_snapshot?: Record<string, unknown> | null;
  member_snapshot_json?: string | null;
  excluded_members_json?: string | null;
  // Benchmark health and gate state are persisted/returned, never inferred in UI.
  benchmark_equity?: BacktestEquityPoint[];
  benchmark_equity_json?: string | null;
  benchmark_status?: "FULL" | "BENCHMARK_INCOMPLETE" | "SOURCE_MISSING" | string | null;
  benchmark_gap_days?: number | null;
  gate_policy_version?: string | null;
  gate_result?: string | null;
  gate_result_json?: Record<string, unknown> | null;
  blocking_status?: string | null;
  blocking_reasons?: unknown;
  is_result_production_eligible?: boolean;
  total_return?: number | null;
  total_return_pct?: number | null;
  max_drawdown?: number | null;
  max_drawdown_pct?: number | null;
  sharpe_ratio?: number | null;
  win_rate?: number | null;
  profit_factor?: number | null;
  trade_count?: number | null;
  avg_holding_days?: number | null;
  equity_curve?: BacktestEquityPoint[];
  metrics?: Record<string, unknown>;
  diagnostics?: Record<string, unknown>;
  warnings?: Array<string | Record<string, unknown>>;
  errors?: string[];
  decision_run_ids?: string[];
  evidence_summary?: Record<string, unknown>;
  rejected_count?: number;
  decision_snapshot?: Record<string, unknown> | null;
}

/** Normalized equity point returned by a portfolio backtest response. */
export interface BacktestEquityPoint {
  date: string;
  equity?: number | null;
  cash?: number | null;
  position_value?: number | null;
  benchmark?: number | null;
  [key: string]: unknown;
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
  decision_evidence_id?: string | null;
  exit_evidence_id?: string | null;
  /** Exact DecisionRun derived by the ledger API from the entry evidence. */
  entry_decision_run_id?: string | null;
  /** Exact DecisionRun derived by the ledger API from the exit evidence. */
  exit_decision_run_id?: string | null;
  intended_entry_price?: number | null;
  intended_exit_price?: number | null;
  slippage_bps?: number | null;
  entry_rejection_reason?: string | null;
  entry_requested_quantity?: number | null;
  entry_filled_quantity?: number | null;
  entry_remaining_quantity?: number | null;
  entry_order_plan_status?: string | null;
  entry_unfilled_reason?: string | null;
  exit_requested_quantity?: number | null;
  exit_filled_quantity?: number | null;
  exit_remaining_quantity?: number | null;
  exit_order_plan_status?: string | null;
  exit_unfilled_reason?: string | null;
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
  cost_config?: Record<string, unknown> | null;
  score_weight_mode?: string | null;
  factor_model_run_id?: string | null;
  factor_set_id?: string | null;
  strategy_snapshot_id?: string | null;
  factor_data_cutoff_at?: string | null;
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
  reproducibility_status?: "reproducible" | "legacy/non_reproducible" | string | null;
  reproducibility_reason?: string | null;
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
  pit_mode?: string | null;
  match_mode?: "NEXT_OPEN" | "T_CLOSE" | string | null;
  benchmark?: string | null;
  benchmark_code?: string | null;
  commission_rate?: number | null;
  stamp_tax_rate?: number | null;
  slippage_bps?: number | null;
  price_type?: "NEXT_OPEN" | "T_CLOSE" | string | null;
  volume_limit_pct?: number | null;
  rebalance_frequency?: string | null;
  benchmark_equity_json?: string | null;
  benchmark_status?: string | null;
  benchmark_gap_days?: number | null;
  snapshot_no?: number | null;
  snapshot_hash?: string | null;
  gate_policy_version?: string | null;
  gate_result?: string | null;
  gate_result_json?: Record<string, unknown> | null;
  blocking_status?: string | null;
  blocking_reasons?: unknown;
  is_result_production_eligible?: boolean;
  decision_run_ids?: string[];
  evidence_summary?: Record<string, unknown>;
  rejected_count?: number;
  decision_snapshot?: Record<string, unknown> | null;
  benchmark_equity?: BacktestEquityPoint[];
  data_snapshot?: Record<string, unknown> | null;
}

/**
 * Daily per-symbol position ledger returned by GET /backtest/runs/{run_id}/positions.
 *
 * Each row is the portfolio state transition for one symbol on one trade date,
 * rather than a projection of an individual BacktestTrade.
 */
export interface BacktestPosition {
  run_id: number;
  symbol_id: number;
  trade_date: string;
  opening_quantity: number;
  buy_quantity: number;
  sell_quantity: number;
  closing_quantity: number;
  status: "OPEN" | "CLOSED";
  as_of_date: string;
  mark_price: number | null;
  market_value: number | null;
  portfolio_equity: number | null;
  weight: number | null;
  buy_evidence_ids: string[];
  sell_evidence_ids: string[];
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
// ⚠️ 契约对齐：app/schemas/async_task.py::AsyncTaskRead，字段一一对应
export interface AsyncTaskRead {
  id: string;
  task_type: string;
  status: string;
  stage: string;
  percent: number;
  message: string;
  total: number;
  processed: number;
  ok_count: number;
  failed_count: number;
  current_item?: string | null;
  result?: Record<string, unknown> | null;
  errors: Array<Record<string, unknown>>;
  // WPD-05: 顶层 error_code，从 errors_json[0].error_code 提取
  error_code?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at?: string | null;
  // WP-S.5 任务防卡死状态机扩展字段
  heartbeat_at?: string | null;
  stage_budget_seconds?: number | null;
  stage_started_at?: string | null;
  last_progress_at?: string | null;
  last_progress_percent?: number | null;
  current_step_description?: string | null;
  suggested_action?: string | null;
  batch_recovery?: Array<Record<string, unknown>> | Record<string, unknown> | null;
  last_patrol_at?: string | null;
  cancel_requested: boolean;
  payload_json?: string | null;
  fingerprint?: string | null;
  // FR-P1-2 可靠性扩展字段
  correlation_id?: string | null;
  idempotency_key?: string | null;
  is_terminal_locked: boolean;
  cancelled_timeout_at?: string | null;
}

// FR-P1-2/AC-10: POST /system/tasks/{task_id}/cancel 契约
export interface AsyncTaskCancelRequest {
  timeout_seconds?: number;  // 1..3600，默认 60
}
export interface AsyncTaskCancelResponse {
  task_id: string;
  status: string;
  stage: string;
  cancel_requested: boolean;
  cancelled_timeout_at?: string | null;
  correlation_id?: string | null;
  idempotency_key?: string | null;
  is_terminal_locked: boolean;
  error_code?: string | null;
}

// FR-P1-3: POST /portfolios/{pid}/auto-simulation/preflight 契约
export type AutoSimulationSkipReason =
  | "PORTFOLIO_NOT_READY"
  | "SCHEDULE_TOO_EARLY"
  | "DUAL_EXECUTION_RISK_PROHIBITED"
  | "RECONCILIATION_GAP_WARNING"
  | null;

export interface AutoSimulationPreflightRequest {
  trade_date: string;           // ISO date, 一般 today
  decision_at?: string | null;  // ISO datetime, 不传=now()
}
export interface AutoSimulationPreflightResponse {
  portfolio_id: number;
  trade_date: string;  // ISO date
  proceed: boolean;
  skip_reason: AutoSimulationSkipReason;
  skip_detail?: string | null;
  from_state?: string | null;
  to_state?: string | null;
  transition_trace?: Record<string, unknown> | null;
  warnings: string[];
}

// FR-P1-2: Portfolio Resume 契约（POST /portfolios/{pid}/resume/plan & /resume）
export interface PortfolioResumePlanRequest {
  stop_before_trade_date?: string | null;  // ISO date，若空则自动补到 today-1
  max_trade_days?: number;                 // 单日批次，默认 5
  include_held_liquidate?: boolean;        // 补当前持仓缺失交易日的撮合/清算
}
export interface PortfolioResumePlanResponse {
  portfolio_id: number;
  gaps: Array<{
    kind: string;
    from_trade_date: string;
    to_trade_date: string;
    days_count: number;
  }>;
  scope: {
    decision_dates: string[];          // ISO date[]
    total_decisions: number;
    has_current_holdings: boolean;
    held_liquidation_needed: boolean;
  };
  validation: {
    ok: boolean;
    blockers: string[];
    warnings: string[];
    last_decision_trade_date?: string | null;
    last_reconciled_trade_date?: string | null;
  };
  tasks: Array<{
    resume_id: string;
    resume_kind: string;
    trade_date: string;
    idempotency_key: string;
    estimated_seconds?: number | null;
  }>;
}
export interface PortfolioResumeExecuteRequest {
  resume_ids: string[];  // 为空=执行 plan 里所有任务
  run_async?: boolean;   // 默认 true；false=同步等待（仅测试用）
}
export interface PortfolioResumeExecuteResponse {
  created_tasks: Array<{
    resume_id: string;
    resume_kind: string;
    trade_date: string;
    async_task_id: string;
    task_type: string;
    idempotency_key: string;
    correlation_id?: string | null;
  }>;
  skipped: Array<{
    resume_id: string;
    resume_kind: string;
    trade_date: string;
    reason_code: string;
    reason_human?: string | null;
  }>;
  async_id?: string | null;  // 若 run_async=true，返回批量 async task_id（可选）
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

// ============================================================================
// FR-P0-10 / FR-P1-8a 组合治理契约（对齐 9 状态 PortfolioStatus 枚举 + 4 对账治理 API + 审计事件）
// ============================================================================

/**
 * PortfolioStatus 9 状态枚举（与代码 PortfolioStatus 严格对齐）
 * 值名必须与 spec.md FR-P0-10 / tasks.md WP1-12 / checklist.md G4-51 一致。
 * grep 第 10 个值不在下列枚举内 → DB CHECK IntegrityError。
 */
export type PortfolioStatus =
  | "PENDING_INITIAL_REVIEW"   // 新组合初始审查前态
  | "READY"                    // 唯一可通过 HG1 的生产就绪态
  | "RUNNING_AUTO_SIMULATION"  // 20:30 自动推演运行中（租约 auto_sim:{pid}）
  | "RUNNING_BACKTEST"         // 后台回测运行中（租约 bt:{pid}:{btid}，与 AUTO 独立）
  | "DATA_INCOMPLETE_PAUSED"   // 数据缺失/HEAVY，自动暂停新买单
  | "RECONCILIATION_BLOCKED"   // 对账差异≠0，仅人工恢复
  | "MODEL_INACTIVE"           // 绑定模型已退役/未激活
  | "SCORE_STALE"              // Score 覆盖率/新鲜度未通过门禁
  | "INTERRUPTED"              // 任务心跳超时/异常中断，等待恢复扫描器
  | "ADMIN_PAUSED";            // 管理员一键刹车（唯一出边→显式转 READY，禁止自动恢复）

/** 9 状态×4 允许矩阵当前态对应的四列（前端表格用 boolean 直接渲染 ✅❌） */
export interface PortfolioStatePermissions {
  allow_new_buys: boolean;                 // 新买单（手动 BUY / auto-simulation BUY）
  allow_risk_exits: boolean;               // 风险退出（强制止损 / 清仓 SELL）
  allow_auto_recovery: boolean;            // 是否允许恢复扫描器自动改回 READY
  requires_manual_ack: boolean;            // 是否需要人工确认才能继续一切动作
}

/** GET /portfolios/{pid}/status → PortfolioStatusResponse 6 字段契约（FR-P1-8a） */
export interface PortfolioStatusResponse {
  portfolio_id: number;
  current_state: PortfolioStatus | string;           // 字符串兜底（未来扩展值也能显示）
  last_decision_trade_date?: string | null;          // ISO date，可 NULL 但 JSON 键必须存在
  last_reconciled_trade_date?: string | null;        // ISO date，可 NULL 但 JSON 键必须存在
  allowed_transitions: string[];                     // 直接来自 _allowed_transitions_from(current)；空数组=全部按钮 disabled
  is_auto_simulation_eligible: boolean;              // HG1 综合结果；false=预检按钮灰
  // FR-P1-8a 扩展：若后端未返回，前端按 ALLOWANCE_MATRIX 静态表 fail-closed 派生
  permissions?: PortfolioStatePermissions;
}

/** 9×4 允许矩阵静态常量（FR-P0-10 / FR-P1-8a 契约）。
 *  与后端 allowance 矩阵严格对齐，当 API 未返回 permissions 字段时作为 fallback。 */
export const PORTFOLIO_STATE_ALLOWANCE_MATRIX: Record<PortfolioStatus, PortfolioStatePermissions> = {
  PENDING_INITIAL_REVIEW: { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
  READY: { allow_new_buys: true, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false },
  RUNNING_AUTO_SIMULATION: { allow_new_buys: true, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false },
  RUNNING_BACKTEST: { allow_new_buys: true, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false },
  DATA_INCOMPLETE_PAUSED: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: true, requires_manual_ack: false },
  RECONCILIATION_BLOCKED: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: true },
  MODEL_INACTIVE: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false },
  SCORE_STALE: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: true, requires_manual_ack: false },
  INTERRUPTED: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: true, requires_manual_ack: false },
  ADMIN_PAUSED: { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
};

/** POST /portfolios/{pid}/reconcile → 对账守恒 10 字段明细（FR-P1-8a） */
export interface ReconciliationDiffItem {
  dimension:
    | "order_count" | "order_amount"
    | "trade_count" | "trade_amount"
    | "position_count" | "position_market_value"
    | "cash_balance"
    | "evidence_items" | "evidence_hash"
    | string;            // 兼容后端新增维度（前端直接渲染字符串）
  expected_value: number | string | null;
  actual_value: number | string | null;
  diff_value: number | string | null;               // ≠ 0 → 该行标红
  explain_note?: string | null;
}
export interface ReconciliationResponse {
  portfolio_id: number;
  trade_date: string;                                      // ISO date
  as_of_at: string;                                        // ISO datetime
  differences_found: boolean;                              // true=至少有一项 diff 非零
  zero_sum_check_passed: boolean;                          // 证据守恒总开关（true=10项加减和=0）
  last_reconciled_trade_date?: string | null;
  items: ReconciliationDiffItem[];                         // 至少 10 项
  correlation_id?: string | null;
}

/** POST /portfolios/{pid}/confirm-reconciliation → 单人确认 3 字段（无 second_reviewer/dual_approval） */
export interface ConfirmReconciliationRequest {
  ack: boolean;                                   // 必须=true（否则 400）
  force_skip?: boolean;                           // 高危：差异未清零就强行转 READY；true=二次确认红底
  operator_id?: number | null;                    // 为空后端自动取当前登录用户；前端默认隐藏（单人确认）
  review_note: string;                            // 非空，<10字时 422
  force_rerun_before?: boolean;                   // 是否先重新跑一次 reconcile 再确认（默认 true）
}
export interface ConfirmReconciliationResponse {
  portfolio_id: number;
  trade_date: string;
  operator_id: number;
  from_state?: string | null;                             // 一般 RECONCILIATION_BLOCKED
  to_state?: string | null;                               // READY=成功；未通过则不变
  last_reconciled_trade_date?: string | null;
  acknowledged_diffs_cleared: boolean;                    // true=差异 0
  portfolio_now_ready: boolean;                           // 最终 portfolio_status ∈ {READY}
  correlation_id: string;
}

/** POST /portfolios/{pid}/transition-state → ANY → ADMIN_PAUSED 刹车 / ADMIN → READY 解除 （FR-P0-10） */
export interface StateTransitionRequest {
  target_state: PortfolioStatus | string;           // 只允许 allowed_transitions 数组内的目标
  trigger_reason: string;                               // 非空；如"管理员紧急刹车（线上问题处理）"
  operated_by?: number | null;                          // 单人确认；为空后端自动取 current user
  review_note?: string | null;                          // ADMIN_PAUSED→READY 或 RECON→READY 时非空
  noop_if_already?: boolean;                            // 幂等；目标=当前 → 200 NOOP 不报错
}
export interface StateTransitionResponse {
  portfolio_id: number;
  from_state: string | null;
  to_state: string | null;
  transition_applied: boolean;                          // NOOP 时 false 不报错
  noop_detected?: boolean;
  operated_by?: number | null;
  reviewed_at?: string | null;                          // ISO datetime
  review_note?: string | null;
  trigger_reason: string;
  illegal_transition_rejected?: boolean;                // 非法跳转=STATE_TRANSITION_FORBIDDEN
  audit_event_id?: number | null;
  correlation_id: string;
}

/**
 * FinalStatus 7 值撮合枚举（对齐 SimulationMatchStatus 7 值）
 * grep 第 8 值不在下列 7 项内 → DB CHECK 报错。
 */
export type SimulationFinalStatus =
  | "FILLED" | "PARTIAL_FILL"
  | "REJECTED_TRADE_HALTED" | "REJECTED_BELOW_LOT" | "REJECTED_NO_QUANTITY"
  | "SIGNAL_EXPIRED" | "PENDING_RETRY";

/**
 * DataSource 5 值基准数据源枚举（对齐 BenchmarkDataSource 5 值）
 * BOTH_FAILED ⇄ overall_status=UNAVAILABLE 联动约束。
 */
export type BenchmarkDataSource =
  | "INDEX_PRICE_TABLE"     // CS_INDEX_BENCHMARK：组合基准表内建基准
  | "AKSHARE_PRIMARY"       // AKSHARE / WSD_PRIVATE_DB / RQDATA 主
  | "BAOSTOCK_FALLBACK"     // 备
  | "FALLBACK_MIXED"        // 主+备部分混用
  | "BOTH_FAILED";          // 主备都失败；overall_status 必须=UNAVAILABLE

/** ============= 审计事件（G4 治理事件分页过滤契约 FR-P1-8a #5） ============= */
export type AuditEventType =
  | "STATE_TRANSITION" | "ILLEGAL_TRANSITION_ATTEMPT"
  | "RECONCILIATION_RUN" | "RECONCILIATION_RECONFIRMED_PASS" | "RECONCILIATION_BLOCK"
  | "TERMINAL_LOCK_ENFORCED" | "TERMINAL_LOCK_INTERCEPTED"
  | "HEARTBEAT_TIMEOUT" | "RECOVERY_SCAN_RUN"
  | "STRICT_AUTH_HARD_DENIED" | "STRICT_AUTH_SOFT_WARNED"
  | "SCORE_STALE_GATE" | "MODEL_INACTIVE"
  | "ADMIN_PAUSED_APPLIED" | "ADMIN_PAUSED_RELEASED"
  | "G5_DUAL_RUN_LAUNCHED" | "G5_DUAL_RUN_COMPLETED"
  | "PREFLIGHT_BLOCKED" | "PREFLIGHT_PASSED" | string;

export type AuditEventSeverity =
  | "INFO" | "WARNING" | "L1" | "L2" | "L3" | string;

export interface AuditEvent {
  id: number;
  portfolio_id?: number | null;
  event_type: AuditEventType;
  severity: AuditEventSeverity;
  trigger_reason?: string | null;
  operated_by?: number | null;                 // 单人确认；不设 second_reviewer
  reviewed_at?: string | null;                 // ISO datetime
  review_note?: string | null;
  from_state?: string | null;
  to_state?: string | null;
  correlation_id?: string | null;
  attributes_json?: string | null;             // JSON，额外上下文（可前端展开）
  attributes?: Record<string, unknown> | null; // 反序列化后
  created_at: string;                          // ISO datetime
}

export interface AuditEventPageResponse {
  items: AuditEvent[];
  total: number;
  page: number;
  page_size: number;
  page_count: number;
  has_more: boolean;
  event_types_in_page?: string[] | null;
}

/** 审计事件查询过滤器（前端 filter 栏 4 项） */
export interface AuditEventFilter {
  event_type?: AuditEventType | null;
  severity?: AuditEventSeverity | null;
  start_date?: string | null;      // ISO date
  end_date?: string | null;        // ISO date
  query?: string | null;           // 模糊匹配 trigger_reason / review_note / correlation_id
}

// ============================================================================
// G5 双跑对账框架契约（对齐 g5_dual_run_replay.G5ReplaySummary）
// ============================================================================

/** G5 业务口径 6 动作名（6×6 混淆矩阵行列用这些字符串，不输出代码值） */
export type G5BusinessAction =
  | "BUY" | "SELL_LIMITED_RISK" | "HOLDS"
  | "REJECTED_OR_BLOCKED" | "RISK_CLOSED" | "NO_ACTION_OR_ARCHIVED";

/** G5 业务口径 8 归因类目 + 3 类 P0 阻断不入 8 类统计（checklist G5-13~15） */
export type G5AttributionCategory =
  // 以下 8 类（业务口径可归因可解释，不触发 P0 阻断）：
  | "PARAMETER_CONFIG_DIFF"
  | "RISK_CONFIG_DIFF"
  | "SCORING_VERSION_MISMATCH"
  | "INPUT_DATA_ROUNDING"
  | "TRADE_TIMING_MISMATCH"
  | "ENGINE_FALLBACK_CODEPATH"
  | "EXECUTION_SIM_MODEL_DIFF"
  | "UNKNOWN_BUT_EXPLAINABLE"
  // 以下 3 类 P0 阻断（不入 8 类统计；出现=日失败，必须有 change_note）：
  | "UNKNOWN_ENGINE_DIFF"
  | "DUPLICATE_REPLAY_SIDE_EFFECT"
  | "CORRUPTED_SNAPSHOT_OR_EVIDENCE";

/** G5 归因条目（单证券单天最多 1 条，禁止双重计数；优先级 P0 > P1 参数/配置 > P2 版本/数据 > P3 执行/模拟 ）*/
export interface G5AttributionItem {
  symbol_id?: number | null;
  symbol?: string | null;
  category: G5AttributionCategory;
  severity: "P0" | "P1" | "P2" | "P3";
  summary: string;
  change_note?: string | null;          // UNKNOWN_ENGINE_DIFF 必须非空，否则判定为 P0 未通过
  evidence_snippet?: Record<string, unknown> | null;
}

/** 单日双跑对账报告（DailyDualRunReport） */
export interface DailyDualRunReport {
  trade_date: string;                                        // ISO date
  actions_match_rate?: number | null;                        // 0.0~1.0
  universe_jaccard_index?: number | null;
  p0_unexplained_count: number;                              // UNKNOWN_ENGINE_DIFF 且 change_note 空
  p1_hold_noaction_flip: number;                             // HOLD/NO_ACTION 口径混淆导致的"差异"
  attributions: G5AttributionItem[];                         // 单归因输出；不允许同证券同日归因双重计数
  /** 6×6 混淆矩阵：matrix[old_business_action][new_business_action] = securities_count */
  action_confusion_matrix?: Record<G5BusinessAction, Partial<Record<G5BusinessAction, number>>> | null;
}

/** G5 汇总（G5ReplaySummary dataclass 契约） */
export interface G5DualRunSummaryResponse {
  portfolio_id: number;
  start_date: string;          // ISO date
  end_date: string;            // ISO date
  total_days: number;          // 预期交易日数量
  days_replayed: number;       // 实际已重放（= total_days 时才可能 eligible）
  skipped_days: string[];      // ISO date[]
  daily_reports: DailyDualRunReport[];
  avg_action_match_rate: number;          // 0.0~1.0；≥0.95 通过 P1 动作口径
  avg_universe_jaccard: number;
  total_p0_unexplained: number;            // 必须=0 才满足 eligible
  total_p1_hold_noaction_flip: number;     // 可 >0（差异口径允许）
  failing_days_p0: string[];               // ISO date[]
  failing_days_p1: string[];
  g5_eligible_for_g6: boolean;             // P0+P1 双通过 → true（G6 灰度入场券）
  correlation_id?: string | null;
  // ── 前端轮询优化：后端可选地回传内嵌异步任务元信息 ──
  is_terminal_locked?: boolean;
  terminal_lock_reason?: string | null;
  status?: "SUBMITTED" | "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED" | "TIMEOUT" | string;
}

export interface G5DualRunLaunchRequest {
  start_date: string;                                      // ISO date
  end_date: string;                                        // ISO date（与 start_date 连续 10 个真实交易日）
  old_engine_version: string;
  new_engine_version: string;
  run_async?: boolean;                                     // 默认 true
  strict_match_p0_threshold?: number | null;               // 默认 0.0（允许 0 条 P0 未解释）
}
export interface G5DualRunLaunchResponse {
  replay_id: string;
  portfolio_id: number;
  start_date: string;
  end_date: string;
  expected_trade_days: number;
  async_task_id?: string | null;
  correlation_id: string;
  // ── 前端轮询优化：后端可选地回传内嵌异步任务元信息，省去一次单独 getAsyncTask 往返 ──
  is_terminal_locked?: boolean;
  terminal_lock_reason?: string | null;
  status?: "SUBMITTED" | "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED" | "TIMEOUT" | string;
}

/** G7 只读运维状态（扩大范围、停新买、待处理订单和告警） */
export interface G7OperationalStatus {
  portfolio_id: number;
  current_state: string;
  g6_eligible: boolean;
  g5_report_id?: number | null;
  can_stop_new_buys: boolean;
  new_buys_stopped: boolean;
  pending_order_count: number;
  pending_orders_by_status: Record<string, number>;
  active_alert_count: number;
  ready_for_expansion: boolean;
  can_resume: boolean;
  operational_blockers: string[];
  resume_blockers: string[];
}

export interface G6RolloutResult {
  portfolio_id: number;
  status: "STARTED" | "ROLLED_BACK" | "NOOP" | string;
  from_source_mode: string;
  to_source_mode: string;
  operator_id: string;
  correlation_id?: string | null;
  g5_report_id?: number | null;
  audit_event_id?: number | null;
  reason?: string | null;
}

export interface DecisionOrderPlanRead {
  order_plan_id: string;
  decision_run_id: string;
  evidence_id: string;
  symbol_id: number;
  action: string;
  signal_date: string;
  execution_date: string;
  target_quantity: number;
  direction: "BUY" | "SELL" | null;
  intended_price: number | null;
  reason_code: string | null;
  rejection_trace: Array<Record<string, unknown>>;
}
