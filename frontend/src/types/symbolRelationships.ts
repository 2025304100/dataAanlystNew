// WP1.5/WP1.2：标的统一关联状态 TS 类型定义
// 与后端 app/schemas/symbol_relationships.py 对齐
// 五段：candidate / observation / portfolio_member / position / alert

/** 候选状态 */
export interface CandidateRelationship {
  has_candidate: boolean;
  candidate_id: number | null;
  scope: string | null;
  // stage: new/reviewed/watched/portfolio/excluded/expired
  stage: string | null;
  // action: executable/caution/observe/reject
  action: string | null;
  priority_score: number | null;
  quality_score: number | null;
  timing_score: number | null;
  data_credibility: string | null;
  // ISO 8601 datetime
  generated_at: string | null;
  scan_run_id: number | null;
  snapshot_id: number | null;
  snapshot_generated_at: string | null;
}

/** 观察项状态 */
export interface ObservationRelationship {
  has_observation: boolean;
  watchlist_id: number | null;
  watchlist_name: string | null;
  watchlist_item_id: number | null;
  // origin_type: manual/candidate/scan_result/alert/legacy_manual_unknown
  origin_type: string | null;
  // status: watching/ready/invalid/archived
  status: string | null;
  priority: number | null;
  tags: string[];
  target_portfolio_id: number | null;
  // ISO 8601 datetime
  added_at: string | null;
}

/** 组合成员状态（第一阶段只读，预留字段） */
export interface PortfolioMemberRelationship {
  has_portfolio_membership: boolean;
  portfolio_id: number | null;
  portfolio_name: string | null;
  // WP4 后填充
  member_id: number | null;
  // active/paused/archived
  member_status: string | null;
  // manual/confirm/auto
  execution_mode: string | null;
  source_type: string | null;
  effective_from: string | null;
  note: string | null;
}

/** 持仓状态 */
export interface PositionRelationship {
  has_position: boolean;
  portfolio_id: number | null;
  portfolio_name: string | null;
  position_id: number | null;
  quantity: number | null;
  cost_price: number | null;
  latest_price: number | null;
  market_value: number | null;
  // ISO 8601 datetime
  opened_at: string | null;
}

/** 告警状态 */
export interface AlertRelationship {
  has_active_alert: boolean;
  alert_rule_ids: number[];
  active_alert_events: number;
  // info/warning/error/critical
  latest_alert_severity: string | null;
  // ISO 8601 datetime
  latest_alert_at: string | null;
}

/** 标的统一关联状态（前端 useSymbolRelationships hook 消费） */
export interface SymbolRelationships {
  symbol_id: number;
  symbol: string | null;
  candidate: CandidateRelationship;
  observation: ObservationRelationship;
  portfolio_member: PortfolioMemberRelationship;
  position: PositionRelationship;
  alert: AlertRelationship;
  // ISO 8601 datetime
  fetched_at: string;
  // 接口部分失败时降级标记
  degraded: boolean;
  degraded_reason: string | null;
}
