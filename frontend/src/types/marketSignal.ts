export interface SignalRuleDraft {
  name: string; target_type: "symbol" | "sector"; target_key: string;
  quote_key: string | null; quote_type: "index" | "symbol";
  min_score: number; window_days: 1 | 3 | 5; min_move_pct: number;
  adaptive_threshold: boolean; volatility_percentile: number;
  max_age_hours: number; cooldown_hours: number; max_daily: number;
  delivery_mode: "instant" | "digest"; digest_time: string;
  mode: "shadow" | "trial"; enabled: boolean; channel_ids: number[];
}
export interface SignalRule extends SignalRuleDraft {
  id: number; revision: number; target_name: string; created_at: string; updated_at: string;
}
export interface SignalSettings {
  enabled: boolean; ai_profile_id: number | null; global_daily_limit: number;
  max_analysis_per_run: number; auto_analyze: boolean; collect_on_monitor?: boolean;
}
export interface SignalStats {
  total: number; matured: number; hits: number; misses: number; pending: number; unverifiable: number;
  precision: number | null; event_clusters: number; validation_status: string;
  historical_baseline_rate: number | null; uplift?: number | null;
  conservative_lower?: number | null; observation_days?: number; validation_note?: string;
}
export interface SignalDashboard {
  target_precision: number; live: SignalStats; shadow: SignalStats; queued: number; suppressed: number;
  rules: { rule_id: number; name: string; revision: number; live: SignalStats; shadow: SignalStats; history_matured: number }[];
}
export interface SignalMetadata {
  symbols: { key: string; name: string; asset_type: string }[];
  sectors: { key: string; name: string }[]; indices: string[];
  ai_profiles: { id: number; name: string; model: string; enabled: boolean; daily_request_limit: number; daily_request_count: number }[];
  channels: { id: number; name: string; channel_type: string }[];
  readiness: { rule_id: number; ready: boolean; reason: string | null }[];
  schedules: { id: number; enabled: boolean; last_status: string | null; next_run_at: string | null }[];
}
export interface SignalEvidence {
  id: number; title: string; text: string; source: string; source_url: string | null;
  published_at: string | null; time_quality: string; kind: string; first_seen_at: string;
  metadata: Record<string, unknown>;
}
export interface SignalAssessment {
  id: number; evidence_id: number; rule_id: number; rule_revision: number; status: string; score: number | null;
  decision_reason: string | null; calibrated_probability: number | null;
  analysis: Record<string, unknown>; input: Record<string, unknown>; model_name: string | null; total_tokens: number; created_at: string;
}
export interface SignalPrediction {
  id: number; rule_id: number; rule_revision: number; target_type: string; target_key: string;
  delivery_status: string; outcome_status: string; sent_at: string | null; due_at: string;
  snapshot: { rule: SignalRuleDraft & { target_name?: string }; score: number; threshold_pct: number;
    reference_close: number; reference_date: string; observation_dates: string[]; evidence: SignalEvidence;
    analysis: Record<string, unknown>; [key: string]: unknown };
  outcome: { reason?: string; max_move_pct?: number; first_hit_date?: string; [key: string]: unknown };
}
export interface SignalPage<T> { total: number; items: T[] }
export interface SignalJob { id: string; status: string; percent: number; message?: string; result?: unknown; errors?: unknown[] }
