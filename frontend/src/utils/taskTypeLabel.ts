// 任务类型（task_type）→ 用户可读名称。
//
// 为什么单独成文件：这份映射原先内联在 TaskCenter.tsx 里，只有 6 个条目，而后端
// 实际会产生 20+ 种 task_type；没命中的就 `return taskType` 原样显示，于是任务中心
// 里出现过 `external_api_probe` 这种用户读不懂的内部 code（拟真走查 §四 检出）。
// `external_sync_` 那条分支更直接：把内部后缀拼进文案。
//
// 规则：
// - 命中映射 → 中文（i18n）；
// - external_sync_ 前缀 → 按已接入数据集显示具体名称，未知数据集用"外部数据同步"；
// - 没命中 → "其他任务"，**原 code 只放进 tooltip**（运维需要时仍能看到），
//   不进入正文文案。
import { t } from "../i18n";

const TASK_TYPE_LABEL_KEYS: Record<string, string> = {
  // 行情与股票池
  market_data_sync: "taskTypeSync",
  universe_incremental_sync: "taskTypeUniverseIncremental",
  universe_sync: "taskTypeUniverseSync",
  universe_backfill: "taskTypeUniverseBackfill",
  universe_industry_backfill: "taskTypeUniverseIndustryBackfill",
  universe_range_repair: "taskTypeUniverseRangeRepair",
  universe_smart_sync: "taskTypeUniverseSmartSync",
  data_mirror: "taskTypeDataMirror",
  history_initialization: "taskTypeHistory",
  security_status_download: "taskTypeSecurityStatusDownload",
  etf_history_prepare: "taskTypeETFHistoryPrepare",
  index_daily_sync: "taskTypeIndexDailySync",
  index_prices_sync: "taskTypeIndexPricesSync",
  // 因子与挖掘
  factor_pipeline: "taskTypeFactorPipeline",
  factor_mining: "taskTypeFactorMining",
  factor_mining_validation: "taskTypeFactorMiningValidation",
  discovery_mining: "taskTypeDiscovery",
  discovery_fast_scan: "taskTypeFastScan",
  discovery: "taskTypeDiscoveryRun",
  discovery_data_prep: "taskTypeDiscoveryDataPrep",
  // 外部数据
  macro_update: "taskTypeMacro",
  financial_report_sync: "taskTypeFinancialReportSync",
  factor_production_candidate: "productionCandidate.taskType",
  factor_independent_evaluation: "productionCandidate.independentTaskType",
  hot_rank_snapshot: "taskTypeHotRankSnapshot",
  lhb_institution_sync: "taskTypeLhbInstitutionSync",
  tail_proxy_snapshot: "taskTypeTailProxySnapshot",
  external_api_probe: "taskTypeApiProbe",
  external_data_sync: "taskTypeExternalSync",
  wp5_evaluation: "taskTypeFactorEvaluation",
  universe_field_repair: "taskTypeUniverseFieldRepair",
  portfolio_data_repair: "taskTypePortfolioDataRepair",
  portfolio_reconciliation: "taskTypePortfolioReconciliation",
  // 组合交易
  portfolio_auto_trade: "taskTypePortfolioAutoTrade",
  portfolio_auto_simulation: "taskTypePortfolioAutoSimulation",
  portfolio_equity_snapshot: "taskTypePortfolioEquitySnapshot",
  // 告警
  alert_evaluation: "taskTypeAlertEvaluation",
};

const EXTERNAL_SYNC_PREFIX = "external_sync_";
const EXTERNAL_SYNC_DATASET_LABEL_KEYS: Record<string, string> = {
  fundamental: "extSyncFundamental",
  financial: "extSyncFinancial",
  lhb: "extSyncLhb",
  hot_rank: "extSyncHotRank",
  tail_proxy: "extSyncTailProxy",
  capital_flow: "extSyncCapitalFlow",
  etf: "extSyncEtf",
};

/** 供测试与守护使用：映射已覆盖的内部 code 全集。 */
export const KNOWN_TASK_TYPES: readonly string[] = Object.keys(TASK_TYPE_LABEL_KEYS);

/**
 * 任务类型的用户可见名称。永不返回内部 code。
 * @param taskType 后端 task_type 原值
 */
export function taskTypeLabel(taskType?: string | null): string {
  const value = (taskType ?? "").trim();
  if (!value) return t("taskTypeOther");
  const key = TASK_TYPE_LABEL_KEYS[value];
  if (key) return t(key);
  // fundamental 是股票估值，financial 才是财报；复用同步入口的文案，避免任务中心混淆。
  if (value.startsWith(EXTERNAL_SYNC_PREFIX)) {
    const dataset = value.slice(EXTERNAL_SYNC_PREFIX.length);
    return t(EXTERNAL_SYNC_DATASET_LABEL_KEYS[dataset] ?? "taskTypeExternalSync");
  }
  return t("taskTypeOther");
}

/**
 * 内部 code 只在 tooltip 里给运维看，不进正文。
 * 命中已知映射时返回 null（无需暴露）。
 */
export function taskTypeCodeForTooltip(taskType?: string | null): string | null {
  const value = (taskType ?? "").trim();
  if (!value) return null;
  if (TASK_TYPE_LABEL_KEYS[value]) return null;
  if (value.startsWith(EXTERNAL_SYNC_PREFIX)) return null;
  return value;
}
