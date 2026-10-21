export interface ProductionAdmissionReport {
  model_id: string; model_label: string; parent_fingerprint: string; identity_sha256: string;
  can_freeze: boolean; sample_precheck_ready: boolean; production_eligible: boolean; planning_only: boolean;
  read_only: boolean; training_not_submitted: boolean; model_not_activated: boolean;
  independent_after: string; potential_mature_signal_days_upper_bound: number; independent_signal_days_upper_bound: number;
  bundle: {bundle_id:string; dataset_id:string; first_date:string; last_date:string; raw_trade_dates:number; bar_rows:number; symbols:string[]};
  identity: {factor_set_id:string; window_days:number; validation_days:number; observed_through:string; minimum_independent_signal_dates:number;
    symbols:string[]; feature_versions:Record<string,number>; fit_policy:string; alphas:number[]; manual_activation_confirmation_required:boolean};
  cohort_change: {parent_count:number; planned_count:number; added:string[]; removed:string[]};
  current_release_readiness: {ready:boolean; code:string; message:string};
  gates: Array<{code:string;stage:string;passed:boolean|null;actual?:number;required?:number;message:string}>;
  frozen_plan: {plan_id:string; frozen_at:string; is_training_or_release_certificate:boolean; identity:ProductionAdmissionReport['identity']}|null;
  frozen_plan_sha256:string|null;
}

export interface ProductionExecutionReview {
  sha256:string;locked:boolean;current_cost_differs:boolean;
  contract:{plan_id:string;training_cutoff:string;cost:{portfolio_id:number;currency:string;evaluation_capital:number;
    fees:Record<string,number>;provenance:Record<string,string>;source_snapshot_id:string|null}};
}

export interface ProductionPredictionStatus {
  plan_id:string;candidate_model_id:string;independent_after:string;
  journal_dates:string[];journal_count:number;required_independent_dates:number;
  independent_validation_complete:boolean;cost_evaluation_complete:boolean;
  qualification_issued:boolean;model_activated:boolean;read_only:boolean;can_record_today:boolean;
}

export interface ProductionReleaseReview {
  plan_id:string; candidate_model_id:string; ready:boolean; sha256:string;
  blockers:string[]; qualification_issued:boolean; model_activated:boolean; message:string;
  independent_signal_days?:number; mature_signal_days?:number;
  result?:{mean_daily_ic:number;net_return:number;evidence_pass:boolean}|null;
}

export interface ProductionIndependentReview {
  ready:boolean; sha256:string; blockers:string[]; mature_signal_count:number;
  missing_journal_dates:string[];
}
