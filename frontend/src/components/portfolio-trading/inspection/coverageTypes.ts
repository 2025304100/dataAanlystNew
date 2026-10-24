import type { HistoryEvidencePackage, HistoryQuery } from "./historyTypes";

export interface CoverageGap {
  trade_date_from: string;
  trade_date_to: string;
  reason: string;
  attempted_sources: string[];
  evidence_refs: string[];
  verdict: "HISTORICAL_EVIDENCE_UNKNOWN";
  closure_type: "ARCHIVED_UNVERIFIABLE";
}
export interface CoverageProposal { coverage_start_trade_date?: string; history_gaps: CoverageGap[] }
export interface CoverageSnapshot {
  currency?: string;
  cash?: string | null;
  independent_expected_cash?: string | null;
  positions?: Array<{ symbol_id: number; security_id?: string; name?: string; quantity: string; avg_cost: string; source_ref?: string }>;
  unresolved_obligation_count?: number | null;
  [key: string]: unknown;
}
export interface CoveragePreview {
  portfolio_id: number;
  as_of_at: string;
  can_accept: boolean;
  coverage_start_trade_date: string | null;
  evidence_fingerprint: string;
  cursor_row_version: number;
  current_generation: number;
  snapshot: CoverageSnapshot;
  source_refs: Array<{ source: string; available: boolean; row_count?: number; row_ids?: string[]; content_hash?: string; missing_columns?: string[] }>;
  history_gaps: CoverageGap[];
  blockers: Array<{ code: string; message: string; details?: unknown }>;
  calendar_version: string | null;
  policy_version: string;
  runtime_enabled: false;
  acceptance_does_not_resume_trading: true;
}
export interface CoverageAcceptRequest extends CoverageProposal {
  coverage_start_trade_date: string;
  expected_evidence_fingerprint: string;
  expected_cursor_row_version: number;
  acceptance_key: string;
  review_note: string;
  acknowledge_history_unknown: true;
}
export interface CoverageAccepted {
  portfolio_id: number;
  baseline_id: string;
  cursor_id: string;
  generation: number;
  coverage_start_trade_date: string;
  verified_through_trade_date: null;
  audit_event_id: number;
  content_hash: string;
  history_gaps: CoverageGap[];
  idempotent: boolean;
  runtime_enabled: false;
  trading_resumed: false;
}
export interface CoverageState {
  portfolio_id: number;
  cursor: null | { id: string; generation: number; baseline_id: string; coverage_start_trade_date: string; verified_through_trade_date: string | null; row_version: number };
  baselines: Array<{ id: string; kind: string; as_of_at: string; coverage_start_trade_date: string; content_hash: string; accepted_audit_id: number; history_gaps: CoverageGap[] }>;
  historical_coverage_unknown: boolean;
}
export interface InspectionCoverageApi {
  getPortfolioInspectionCoverage(portfolioId: number): Promise<CoverageState>;
  previewPortfolioInspectionCoverage(portfolioId: number, payload: CoverageProposal): Promise<CoveragePreview>;
  acceptPortfolioInspectionCoverage(portfolioId: number, payload: CoverageAcceptRequest): Promise<CoverageAccepted>;
  exportPortfolioInspectionHistory?(portfolioId: number, query: HistoryQuery): Promise<HistoryEvidencePackage>;
}

