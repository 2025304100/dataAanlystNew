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
  // 闂傚倷娴囬崑鎰偓鍏哥矙婵″爼宕ㄩ婊冩槬闂備浇宕垫慨鏉懨洪妶鍥ｅ亾濮樼厧澧寸€规洝顫夐幆鏃堝Ω閵壯屾Т闂備胶纭堕崜婵堢矙韫囨稑鐒?
  trend_score?: number;
  momentum_score?: number;
  volatility_score?: number;
  liquidity_score?: number;
  breadth_score?: number;
  event_score?: number;
  // 闂傚倷绀侀幖顐﹀疮椤愶絾娅犻幖娣妼绾惧鏌ㄥ┑鍡樺閻庢碍宀搁弻娑㈠即閵娿儰绨婚梺璇查椤兘寮诲☉銏犵闁瑰鍎愬Λ锕傛⒑闂堟稒顥滅紒澶婄秺瀵偄顓兼径濠囧敹闂佺粯鏌ㄩ幗婊堝储閹邦厾绡€闁冲皝鍋撻柛娑卞幖婢瑰绱?
  breakout_score?: number;
  pullback_score?: number;
  overheat_penalty?: number;
  // 闂傚倷娴囧銊╂嚄閼稿灚娅犳俊銈傚亾闁伙絽鐏氱粭鐔煎焵椤掆偓閻ｇ兘鏁愭径妯绘櫖濠电偛妫楀ù椋庣不濮樿埖鐓欓柤鍦瑜把囨偣娓氬﹦鎮兼俊鍙夊姇椤?-4.3闂?
  data_credibility?: number;
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
  total_capital: number;
  investable_ratio: number;
  cash_reserve_ratio: number;
  currency: string;
  is_default: number;
  created_at: string;
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
  id: number;
  status: string;
  stage: string;
  percent: number;
  message: string;
  total: number;
  processed: number;
  ok_count: number;
  failed_count: number;
  empty_count: number;
  scored_count: number;
  current_symbol: string | null;
  scan_run_id: number | null;
  executable_count: number;
  errors: string[];
  can_resume: boolean;
  created_at: string;
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

// 闂傚倷绀佺壕顓㈠磿閵堝绀夐煫鍥ㄧ☉閻ら箖鏌＄仦璇插姎闁绘挻鐩弻鐔兼嚋椤掆偓婵¤姤绻涢崨顔剧煉闁哄苯绉归幃婊兾熼崫鍕垫綂闂備礁鎼幊蹇涙偂閿熺姴鏄ラ柨鐔哄Т缁犳盯鏌曟繝蹇涙闁绘繃濞婂鍝勭暦閸パ冨闂佸憡鏌ㄧ换妯侯嚕閺屻儱钃熼柕澶堝劤閸樻悂姊虹涵鍛劷闁告柨绉撮埢宥咁煥閸喓鍘撻梺缁樺灦閿氬┑顔瑰亾闂備礁鎼幊蹇涙偂閿熺姴鏄ラ柨鐔哄Т缁犳盯鏌曟繝蹇涙闁绘繃濞婂鍝勭暦閸パ冨闂佸憡鏌ㄧ换妯侯嚕閺屻儱钃熼柕澶堝劤閸樻悂姊虹涵鍛劷闁告柨绉撮埢宥咁煥閸喓鍘撻梺缁樺灦閿氬┑顔瑰亾闂備礁鎼幊蹇涙偂閿熺姴鏄ラ柨鐔哄Т缁犳盯鏌曟繝蹇涙闁绘繃濞婂鍝勭暦閸パ冨闂佸憡鏌ㄧ换妯侯嚕閺屻儱钃熼柕澶堝劤閸樻悂姊虹涵鍛劷闁告柨绉撮埢宥咁煥閸喓鍘撻梺缁樺灦閿氬┑顔瑰亾闂備礁鎼幊蹇涙偂閿熺姴鏄ラ柨鐔哄Т缁犳盯鏌曟繝蹇涙闁绘繃濞婂鍝勭暦閸パ冨闂佸憡鏌ㄧ换妯侯嚕閺屻儱钃熼柕澶堝劤閸樻悂姊虹涵鍛劷闁告柨绉撮埢宥咁煥閸喓鍘撻梺缁樺灦閿氬┑顔瑰亾闂備礁鎼幊蹇涙偂閿熺姴鏄ラ柨鐔哄Т缁犳盯鏌曟繝蹇涙闁?
//  闂備浇宕甸崑鐐电矙閹达箑瀚夋い鎺戝绾惧潡鐓崶銊﹀皑婵℃彃鐗撻弻娑㈩敃閵堝懏鐎虹紓?(Market News / Market Events)
// 闂傚倷绀佺壕顓㈠磿閵堝绀夐煫鍥ㄧ☉閻ら箖鏌＄仦璇插姎闁绘挻鐩弻鐔兼嚋椤掆偓婵¤姤绻涢崨顔剧煉闁哄苯绉归幃婊兾熼崫鍕垫綂闂備礁鎼幊蹇涙偂閿熺姴鏄ラ柨鐔哄Т缁犳盯鏌曟繝蹇涙闁绘繃濞婂鍝勭暦閸パ冨闂佸憡鏌ㄧ换妯侯嚕閺屻儱钃熼柕澶堝劤閸樻悂姊虹涵鍛劷闁告柨绉撮埢宥咁煥閸喓鍘撻梺缁樺灦閿氬┑顔瑰亾闂備礁鎼幊蹇涙偂閿熺姴鏄ラ柨鐔哄Т缁犳盯鏌曟繝蹇涙闁绘繃濞婂鍝勭暦閸パ冨闂佸憡鏌ㄧ换妯侯嚕閺屻儱钃熼柕澶堝劤閸樻悂姊虹涵鍛劷闁告柨绉撮埢宥咁煥閸喓鍘撻梺缁樺灦閿氬┑顔瑰亾闂備礁鎼幊蹇涙偂閿熺姴鏄ラ柨鐔哄Т缁犳盯鏌曟繝蹇涙闁绘繃濞婂鍝勭暦閸パ冨闂佸憡鏌ㄧ换妯侯嚕閺屻儱钃熼柕澶堝劤閸樻悂姊虹涵鍛劷闁告柨绉撮埢宥咁煥閸喓鍘撻梺缁樺灦閿氬┑顔瑰亾闂備礁鎼幊蹇涙偂閿熺姴鏄ラ柨鐔哄Т缁犳盯鏌曟繝蹇涙闁绘繃濞婂鍝勭暦閸パ冨闂佸憡鏌ㄧ换妯侯嚕閺屻儱钃熼柕澶堝劤閸樻悂姊虹涵鍛劷闁告柨绉撮埢宥咁煥閸喓鍘撻梺缁樺灦閿氬┑顔瑰亾闂備礁鎼幊蹇涙偂閿熺姴鏄ラ柨鐔哄Т缁犳盯鏌曟繝蹇涙闁?

export interface MarketEvent {
  id: number;
  title: string;
  summary: string | null;
  impact_scope: string;       // macro_policy | sector_dynamics | international | breaking | fund_flow | sentiment | other
  importance_level: number;   // 1-5
  affected_market: string;     // A闂?| 濠电姷鏁搁崑鐐哄箹閳哄懎鍨傞柣銏㈩焾绾?| 缂傚倸鍊搁崐绋棵洪妶鍜佹僵闁挎洖鍊哥壕?
  affected_sectors: string | null;
  affected_symbols: string | null;
  sentiment: string;          // positive | negative | neutral
  source: string;             // cctv | baidu | baidu-report | manual | ...
  source_url: string | null;
  is_manual: number;          // 0=闂傚倷鑳堕崢褔銆冩惔銏㈩洸婵犲﹤瀚崣蹇涙煃閸濆嫭鍣洪柣鎾冲€块弻娑㈠Ψ椤旂厧顫梺? 1=闂傚倷绀佺紞濠傤焽瑜旈、鏍川椤旇棄寮块梺鍐叉惈鐎氥劍绂嶈ぐ鎺撶厸闁搞儮鏅涙禒婊呪偓?
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
  execution?: { entry_price_field?: "open" | "close" | string; exit_price_field?: "open" | "close" | string };
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
  quantity: number;
  exit_date?: string | null;
  exit_price?: number | null;
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
}

// 闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞?闂傚倷绀侀幖顐λ囬锕€鐒垫い鎺嗗亾鐎殿喖鐖奸、鏇熺鐎ｎ偆鍘搁悗骞垮劚濡鎮￠埀顒傜磽娴ｄ粙鍝洪柣? 闂傚倷鐒﹂幃鍫曞磿閹惰棄纾婚柣鎰暩閻牓鎮楅棃娑欏暈闁哥姴妫濋弻娑㈠即閵娿儰绨婚梺璇茬箳閸犳牠寮婚妸銉㈡婵妫欏瓭闂備浇銆€閸嬫捇鏌曡箛瀣偓鏇㈡偂濮椻偓閺屽秷顧侀柛鎾跺枛瀹曟椽鎮欓崫鍕姦濡炪倖甯掔€氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻宥堫檨闁告挾鍠栧畷娲倷閸濆嫮鍔﹀銈嗗笒鐎氼剟鎮″鈧弻?


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

export type CustomIndicatorPayload = Omit<CustomIndicator, "id" | "version" | "created_at" | "updated_at">;

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

export interface CustomIndicatorPreviewResult {
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
  label: string;
  category: "basic" | "sub_score" | "technical" | "sell";
  valueType: "number" | "string" | "boolean" | "string_list";
  operators: ConditionOperator[];
  params?: { key: string; label: string; type: "number" | "text" | "select"; default: number | string; placeholder?: string }[];
  requiresHistory: boolean;
  side?: "buy" | "sell" | "both";
}

// 闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞?闂傚倷娴囧銊╂嚄閼稿灚娅犳俊銈傚亾闁伙絽鐏氶幏鍛喆閸曨偄濡抽梻浣筋潐瀹曟﹢顢氳閺侇喖鐣烽崶鈺冿紳?闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞?

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
  status: "idle" | "running" | "completed" | "failed";
  current_table?: string;
  tables_done: number;
  tables_total: number;
  rows_migrated: number;
  error?: string;
}


export interface HistoryInitializationStage {
  key: "prepare" | "sync_bars" | "calc_scores" | "finalize" | string;
  status: "pending" | "running" | "completed" | "failed";
  percent: number;
  done: number;
  total: number;
  message?: string | null;
}

export interface HistoryInitializationSummary {
  symbols_total: number;
  sync_ok_count: number;
  sync_failed_count: number;
  empty_count: number;
  bars_rows: number;
  score_days_total: number;
  score_days_completed: number;
}

export interface HistoryInitializationTask {
  task_id?: string | null;
  status: "idle" | "running" | "completed" | "failed";
  preset: "1m" | "1q" | "1y" | "3y";
  adjust: string;
  start_date?: string | null;
  end_date?: string | null;
  progress_pct: number;
  message?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  stages: HistoryInitializationStage[];
  summary: HistoryInitializationSummary;
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
