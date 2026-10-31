export type InspectionEvidenceStatus = "PASSED" | "MISSING" | "STALE" | "READ_ERROR" | "VERSION_MISMATCH" | "FUTURE_DATA" | "NOT_DUE" | "CALENDAR_UNKNOWN" | "UNKNOWN" | "NOT_APPLICABLE";

export interface InspectionEvidence {
  evidence_id: string;
  admission_relevant?: boolean;
  symbol_id: number | null;
  symbol?: string | null;
  check_code: string;
  label: string;
  status: InspectionEvidenceStatus;
  reason: string;
  value?: unknown;
  expected?: unknown;
  actual_trade_date?: string | null;
  expected_trade_date?: string | null;
  trading_day_lag?: number | null;
  available_at?: string | null;
  availability_basis?: string | null;
  observed_at: string;
  source_code: string;
  source_label: string;
  source_record_ref?: string | null;
  model_run_id?: string | null;
  config_id?: number | null;
  config_version?: number | null;
  affected_capabilities: string[];
  depends_on: string[];
  metadata: Record<string, unknown>;
}

export interface InspectionSymbol {
  symbol_id: number;
  symbol: string;
  name: string;
  market: string;
  asset_type: string;
  scopes: string[];
  entry_authorized: boolean;
  expected_trade_date: string | null;
  actual_bar_trade_date: string | null;
  actual_score_trade_date: string | null;
  scoring: Record<string, unknown>;
  evidence: InspectionEvidence[];
}

export interface InspectionRepairOperation {
  operation_id: string;
  kind: "sync_daily_bars" | "recalculate_rule_score" | "recalculate_model_score" | "review_evidence";
  label: string;
  symbol_id: number | null;
  symbol: string | null;
  trade_date: string | null;
  start_date?: string | null;
  end_date?: string | null;
  config_id: number | null;
  config_version: number | null;
  enabled: boolean;
  disabled_reason: string | null;
  disabled_code?: string | null;
  target: string;
  depends_on: string[];
  resolves_evidence_ids: string[];
  expected_effect: string;
}

export interface InspectionRepairPlan {
  plan_id: string;
  snapshot_id: string;
  portfolio_id: number;
  version: number;
  generated_at: string;
  expires_at: string;
  executable_count: number;
  disabled_count: number;
  operations: InspectionRepairOperation[];
  confirmation_required: boolean;
  completion_criteria: string[];
  limitations: string[];
}

export interface PortfolioInspectionDiagnostics {
  portfolio_id: number;
  snapshot_id: string;
  version: number;
  generated_at: string;
  policy_version: string;
  read_only: boolean;
  usable_for_admission: boolean;
  summary: Record<string, number | string | boolean | null>;
  binding: Record<string, unknown>;
  calendars: Record<string, unknown>[];
  symbols: InspectionSymbol[];
  evidence: InspectionEvidence[];
  repair_plan: InspectionRepairPlan;
  limitations: string[];
}

export interface InspectionRepairSubmit {
  plan_id: string;
  snapshot_id: string;
  generated_at: string;
  operation_ids: string[];
  confirmed: true;
  idempotency_key: string;
}

export interface InspectionRepairTask {
  task_id: string;
  portfolio_id: number;
  status: string;
  stage: string;
  percent: number;
  total: number;
  processed: number;
  ok_count: number;
  failed_count: number;
  message: string;
  objects: Record<string, unknown>[];
  retryable_operation_ids: string[];
  parent_task_id: string | null;
  reinspection: Record<string, unknown> | null;
  audit_refs: string[];
  replayed: boolean;
  correlation_id?: string | null;
  issue_ids?: string[];
  report_refs?: string[];
  created_by?: string;
  created_at?: string | null;
  updated_at?: string | null;
  can_cancel?: boolean;
  cancel_requested?: boolean;
  recovery?: { status: string; reason: string; error_codes?: string[] };
}

export interface InspectionRuntime {
  portfolio_id: number;
  generated_at: string;
  current_state: string;
  permissions: {
    allow_new_buys: boolean | null;
    allow_risk_exits: boolean | null;
    allow_auto_recovery: boolean | null;
    requires_manual_ack: boolean | null;
  };
  capability_reasons: Record<string, string[]>;
  recovery_summary: {
    can_confirm: boolean;
    confirmation_action?: string | null;
    target_state?: string;
    blockers: string[];
    checks: { key: string; passed: boolean | null; reason: string; not_applicable?: boolean }[];
    review_required: boolean;
    phase?: string;
    recommended_action?: string;
    waiting_reason?: string | null;
    next_check_at?: string | null;
  };
  dimensions: Record<string, string | null>;
  guide?: InspectionGuide;
}

export type GuideStatus = "INCOMPLETE" | "RUNNING" | "NEEDS_RECHECK" | "PASSED" | "NOT_APPLICABLE" | "UNAVAILABLE" | "WAITING";
export interface GuideStep { key: string; label: string; status: GuideStatus; reason: string; proof_hash: string;
  proof: Record<string, unknown>; tasks: Array<{task_id: string; task_type: string; status: string}>; next_action: string; }
export interface InspectionGuide { schema_version: string; snapshot_id: string; can_confirm: boolean; steps: GuideStep[];
  completion_proofs: Array<{key: string; status: string; proof_hash: string; reason: string; proof: Record<string, unknown>}>;
  effects: { target_state: string; current_permissions: Record<string, boolean | null>; historical_unknown_retained: boolean; force_recovery_enabled: boolean }; }

export interface EvidenceCandidate { candidate_id: string; source_hash: string; order_id: number; evidence_id: string;
  run_id: string; symbol_id: number; symbol: string; name: string; market: string; trade_date: string; execution_date: string; order_status: string; quantity: number;
  filled_quantity: number; fill_count: number; ledger_count: number; match_reasons: string[]; effect: string; }
export interface EvidenceSupplement { id: string; candidate_id: string; source_hash: string; order_id: number;
  created_by: string; created_at: string; note: string; decision: string | null; reviewed_by: string | null;
  review_note: string | null; status: string; usable: boolean; }
export interface EvidenceSupplementPage { portfolio_id: number; trade_date: string; baseline_id: string | null;
  generation: number | null; candidates: EvidenceCandidate[]; candidate_count: number; has_more: boolean;
  requests: EvidenceSupplement[]; limitations: string[]; matched_source_count?:number; }

export type InspectionMaterialKind = "execution" | "schedule" | "positions";
export type MaterialConclusion = "FOUND_PENDING_VALIDATION" | "NOT_FOUND" | "UNRECOVERABLE";
export interface MaterialInvestigationRequest {
  trade_date: string; material: InspectionMaterialKind; report_ref: string; report_version: string;
  conclusion: MaterialConclusion; attempted_sources: string[]; evidence_refs: string[]; note: string; request_key: string;
}
export interface MaterialInvestigation extends Omit<MaterialInvestigationRequest, "request_key"> {
  id: number; created_at: string; created_by: string; material_label: string; conclusion_label: string;
  proves_execution: false; reconciliation_passed: false; trading_resumed: false; baseline_accepted: false;
}
export interface MaterialInvestigationPage {
  portfolio_id: number; trade_date: string; material: InspectionMaterialKind;
  items: MaterialInvestigation[]; has_more: boolean; read_only: true;
}

export interface InspectionReconciliationDay {
  history_only?: boolean;
  trade_date: string;
  status: string;
  reason: string;
  report_id?: string | null;
  report_version?: string | null;
  /** 作业侧痕迹：队列接口会带出逐日工作状态与下次检查时刻（旧后端可能缺省） */
  work_state?: string;
  next_check_at?: string | null;
  persistent_report_id?: string | null;
  persistent_report_version?: string | number | null;
}

export interface InspectionReconciliationQueue {
  portfolio_id: number;
  coverage_start: string | null;
  verified_through: string | null;
  target_trade_date: string | null;
  calendar_status: string;
  coverage_source?: string;
  days: InspectionReconciliationDay[];
  limitations: string[];
}

export interface InspectionJob {
  portfolio_id: number;
  enabled: boolean;
  status: string;
  last_run_at: string | null;
  next_run_at: string | null;
  task_id: string | null;
  reason: string;
  check_summary?: {passed_count: number; waiting_count: number; difference_count: number; error_count: number};
  queue: InspectionReconciliationQueue | null;
  work_items?: Array<{trade_date: string; work_state: string; verdict?: string; task_id?: string | null; requested_evidence_version: number; processed_evidence_version: number; attempt_count: number; last_error_code?: string | null; next_check_at?: string | null; retry_exhausted?: boolean}>;
  source_events?: {count: number; status: string; waiting_for_coverage?:boolean; events: Array<{id: string; source_table: string; source_key: string | null; operation: string; status: string; created_at: string}>};
}

export interface CoverageActivationReview {
  portfolio_id: number;
  can_confirm: boolean;
  baseline_id: string | null;
  generation: number | null;
  cursor_row_version: number | null;
  evidence_fingerprint: string | null;
  coverage_start_trade_date: string | null;
  already_activated: boolean;
  blockers: Array<{ code: string; message: string }>;
  checks: Array<{ key: string; passed: boolean | null; reason: string }>;
}
export interface CoverageActivationRequest {
  baseline_id: string;
  generation: number;
  expected_cursor_row_version: number;
  expected_evidence_fingerprint: string;
  activation_key: string;
  acknowledge_new_scope: true;
  review_note: string;
}
