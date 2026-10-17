export interface VerifiedInputBundle {
  bundle_id: string; label: string; dataset_id: string; manifest_sha256: string;
  dataset_manifest_sha256: string; suggested_model_id?: string;
  financial_rows: number; financial_symbols: number; flow_rows: number; flow_symbols: number;
}
export interface InputBusinessStatus {
  bundle_id: string; bundle_manifest_sha256: string; first_date: string; last_date: string;
  fixed_symbols: number; calculation_date: string | null; calc_batch_id: string | null;
  business_counts: Record<string, number>; warehouse_counts: Record<string, number>;
  market_coverage: Array<{factor_code: string; universe_symbols: number; eligible_symbols: number; coverage: number; latest_trade_date: string | null}>;
  fixed_pool_coverage: Array<{code: string; eligible: number; denominator: number}>;
  read_only: boolean; historical_asof_certified: boolean;
}
export interface InputReplayTask {
  id: string; status: string; stage: string; percent: number; message?: string;
}
export interface InputReplayRequest {
  bundle_id: string; manifest_sha256: string; model_id: string; request_id: string;
}
export interface ReplayRow {
  symbol: string; score: number | null; linear_signal: number | null; missing_features: string[];
  features: Record<string, {version: number; raw_value: number | null; normalized_value: number | null; coefficient: number; contribution: number | null}>;
  factor_observations: Array<{code: string; version: number; raw_value: number | null; normalized_value: number | null; eligible: boolean}>;
}
export interface InputReplayReport {
  replay_id: string; published_at: string; research_only: boolean; production_scores_published: number;
  bundle_id: string; bundle_manifest_sha256: string; dataset_id: string; dataset_manifest_sha256: string;
  first_date: string; last_date: string; calendar_dates: number; frozen_symbol_count: number; scored_symbols: number;
  model: {id: string; label: string; factor_set_id: string; intercept: number; readiness_code: string; feature_versions: Record<string, number>};
  input_counts: Record<string, number>;
  coverage: Array<{code: string; version: number; eligible: number; denominator: number; model_feature: boolean}>;
  rows: ReplayRow[]; gaps: {no_flow_symbols: string[]; no_financial_symbols: string[]};
}
