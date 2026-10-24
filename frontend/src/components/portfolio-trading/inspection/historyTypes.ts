export type HistoryReviewDecision = "pending" | "approved" | "rejected" | "revoked" | "unknown" | "not_applicable";
export type HistoryExecutionResult = "not_executed" | "running" | "success" | "partial_failure" | "failed" | "dependency_blocked" | "unknown" | "not_applicable";

export interface HistoryReference {
  kind: string;
  ref: string;
  version?: string | null;
  retention_status: string;
  source_event_id: number;
  read_only: true;
}

export interface HistoryObject {
  object_id: string;
  security_id?: string | null;
  symbol_id?: number | null;
  market?: string | null;
  code?: string | null;
  name?: string | null;
  name_source: "audit" | "current_catalogue" | "unknown";
  trade_date?: string | null;
  trade_date_to?: string | null;
  security_index_status: "known" | "unknown" | "not_applicable";
  trade_date_index_status: "known" | "unknown" | "not_applicable";
  result: HistoryExecutionResult;
  result_basis?: "reinspection" | "execution" | "unknown";
  source_event_ids: number[];
  references: HistoryReference[];
}

export interface HistoryBatch {
  batch_id: string;
  portfolio_id: number;
  title: string;
  occurred_at: string;
  occurred_from: string;
  display_timezone: string;
  trade_date_index_status?: "known" | "unknown" | "not_applicable";
  operators: string[];
  actions: string[];
  review_decision: HistoryReviewDecision;
  execution_result: HistoryExecutionResult;
  event_count: number;
  total_object_count: number;
  matched_object_count: number;
  matched_security_count: number;
  objects: HistoryObject[];
  matched_objects: HistoryObject[];
  references: HistoryReference[];
  index_warnings: string[];
  read_only: true;
}

export interface HistoryEvent {
  id: number;
  action: string;
  source_action?: string | null;
  action_label: string;
  occurred_at: string;
  source_occurred_at?: string | null;
  operator_id: string;
  correlation_id?: string | null;
  business_key?: string | null;
  before: unknown;
  after: unknown;
  attributes: unknown;
  note?: string | null;
  review_decision: HistoryReviewDecision;
  execution_result: HistoryExecutionResult;
  references: HistoryReference[];
  material_warnings: string[];
}

export interface HistoryDetail extends HistoryBatch { events: HistoryEvent[] }

export interface HistoryQuery {
  occurred_from?: string;
  occurred_to?: string;
  trade_date_from?: string;
  trade_date_to?: string;
  security_id?: string[];
  action?: string[];
  review_decision?: HistoryReviewDecision;
  execution_result?: HistoryExecutionResult;
  operator?: string;
  actor_type?: string;
  failure_code?: string;
  ai_assisted?: boolean;
  since_last_reconciliation?: boolean;
  q?: string;
  issue_id?: string;
  correlation_id?: string;
  report_ref?: string;
  report_version?: string;
  page?: number;
  page_size?: number;
  snapshot_event_id?: number;
}

export interface HistoryPage {
  items: HistoryBatch[];
  total: number;
  page: number;
  page_size: number;
  page_count: number;
  has_more: boolean;
  snapshot_event_id: number;
  filters: Record<string, unknown>;
  relation_status: "not_requested" | "matched" | "no_related_history" | "filtered_out";
  empty_reason?: string | null;
  read_only: true;
}

export interface HistorySecurity {
  security_id: string;
  market?: string | null;
  code?: string | null;
  name?: string | null;
}
export interface HistorySecurityPage { items: HistorySecurity[]; total: number }

export interface InspectionHistoryApi {
  getPortfolioInspectionHistory(portfolioId: number, query: HistoryQuery): Promise<HistoryPage>;
  getPortfolioInspectionHistorySecurities(portfolioId: number, q?: string): Promise<HistorySecurityPage>;
  getPortfolioInspectionHistoryDetail(portfolioId: number, batchId: string, query?: HistoryQuery): Promise<HistoryDetail>;
  getPortfolioInspectionHistoryMaterial?(portfolioId: number, reference: HistoryReference, snapshotEventId?: number): Promise<HistoryMaterial>;
  exportPortfolioInspectionHistory?(portfolioId: number, query: HistoryQuery): Promise<HistoryEvidencePackage>;
}

export interface HistoryMaterial {
  portfolio_id: number;
  reference: HistoryReference;
  status: "available" | "partial" | "archived" | "missing" | "inaccessible" | "unsupported";
  material_kind: "immutable_report" | "immutable_baseline" | "audit_snapshot" | "retained_source" | "reference_only";
  title: string;
  explanation: string;
  data: unknown;
  missing_sources: string[];
  content_hash?: string | null;
  original_audit: unknown;
  read_only: true;
}
export interface HistoryEvidencePackage {
  manifest: { exported_at: string; content_sha256: string; hash_scope: string; read_only: true };
  payload: { portfolio_id: number; snapshot_event_id: number; batch_count: number; [key: string]: unknown };
}

export interface InspectionHistoryContext {
  issue_id?: string;
  correlation_id?: string;
  report_ref?: string;
  report_version?: string;
  security_ids?: string[];
  trade_date?: string;
  label?: string;
}
