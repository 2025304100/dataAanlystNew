/**
 * 因子挖掘域 TS 契约（设计文档 §8.8）。
 *
 * ⚠️ 本文件的类型名 / 字段名在 M1a 冻结后只能**加可选字段**（设计文档 §4.5）。
 * ⚠️ 风格：与现有 `api/client.ts` 的因子类型保持一致——后端 snake_case 在客户端
 *    统一转 camelCase。转换在 `api/factorMining.ts` 内完成，组件只见 camelCase。
 */

// ── 枚举 ──────────────────────────────────────────────

export type MiningRunStatus =
  | "draft" | "queued" | "running" | "paused" | "cancel_requested"
  | "validating" | "succeeded" | "failed" | "cancelled" | "converged" | "invalidated";

export type RebalanceFrequency = "daily" | "weekly" | "monthly";

export type FactorCategory =
  | "trend" | "reversal" | "volatility" | "valuation" | "quality" | "volume_price";

export type OperationType =
  | "elite" | "mutation" | "crossover" | "random" | "ai_generated" | "enumerated";

export type QualityGrade = "S" | "A" | "B" | "C" | "D";

export type LogicSource = "ai" | "template" | "manual";

export type ValidationStatus = "queued" | "running" | "passed" | "blocked" | "warning" | "failed";

export type PoolSourceType = "import" | "filter";

export type PoolAnalysisStatus = "not_analyzed" | "analyzing" | "analyzed" | "reset";

// ── 锁 ────────────────────────────────────────────────

export interface LockHolder {
  busy: boolean;
  taskId?: string;
  runId?: string;
  acquiredAt?: string;
  heartbeatAt?: string;
  /** 排队中的 task_id 列表（仅 duckdbWrite） */
  queue?: string[];
}

export interface LockStatus {
  miningDomain: LockHolder;
  duckdbWrite: LockHolder;
}

// ── 切分预算（★ M10 核心，Step2 展示 + 门禁） ────────────

export interface SplitBudget {
  frequency: RebalanceFrequency;
  totalPoints: number;
  trainPoints: number;
  valPoints: number;
  testPoints: number;
  /** 归一化后的**调仓点数** */
  purgePoints: number;
  embargoPoints: number;
  /** 折算的**交易日数**——必须与 purgePoints 同时展示，避免误读 */
  purgeTradingDays: number;
  embargoTradingDays: number;
  tailLoss: number;
  frequencyFloor: number;
  meetsFloor: boolean;
  /** 月频 = true：不做 Bootstrap/置换，最高评 B 级 */
  statisticallyDegraded: boolean;
}

// ── 批次 ──────────────────────────────────────────────

export interface MiningRun {
  id: string;
  status: MiningRunStatus;
  candidatePoolSnapshotId: string;
  rebalanceFrequency: RebalanceFrequency;
  targetHorizon: number;
  dataCutoffAt: string;
  startDate: string;
  endDate: string;
  currentGeneration: number;
  maxGeneration: number;
  converged: boolean;
  totalTrials: number;
  purgePoints: number;
  embargoPoints: number;
  createdAt: string;
  errorCode?: string;
}

export interface MiningRunCreated {
  runId: string;
  taskId: string;
  /** 0 = 立即开始；>0 = 排队位次 */
  queuePosition: number;
  etaSeconds?: number;
  splitBudget?: SplitBudget;
}

export interface MiningRunDetail extends MiningRun {
  /** 仅排队时有值 */
  queueInfo?: { position: number; etaSeconds?: number };
  splitBudget?: SplitBudget;
}

// ── 代际与个体 ────────────────────────────────────────

export interface GenerationStat {
  generation: number;
  populationSize: number;
  bestIcir: number;
  avgIcir: number;
  medianIcir: number;
  diversityHealth: number;
  categoryEvenness: number;
  paretoFrontCount: number;
  stallCount: number;
  actualMutationRate: number;
  actualCrossoverRate: number;
  actualRandomRate: number;
  adaptiveState?: string;
  eliteCount: number;
  mutationCount: number;
  crossoverCount: number;
  randomCount: number;
  eliminatedCount: number;
  evaluationDurationMs: number;
  /** G2 缓存命中率 */
  probeG2HitRate: number;
  probeSubexprTotal: number;
  probeSubexprUnique: number;
  probeDataLoadMs: number;
  probeAstEvalMs: number;
  probeSubexprComputeMs: number;
  probeFactorAssembleMs: number;
  probeMetricCalcMs: number;
  probeDbWriteMs: number;
  /** null = 该代未触发抽样校验；0 = 未通过（前端需标红该代） */
  cacheValidationPassed: 0 | 1 | null;
  cacheValidationMaxDiff?: number;
}

export interface CandidateRead {
  id: string;
  formulaExpr: string;
  canonicalFormula: string;
  category: FactorCategory | null;
  generation: number;
  operation: OperationType;
  icir: number;
  /** 校正后 ICIR（M2，Deflated Sharpe） */
  correctedIcir?: number;
  coverage: number;
  turnover: number;
  complexity: number;
  rank?: number;
  crowdingDistance?: number;
  grade?: QualityGrade;
  /** test_ICIR / train_ICIR；<0.5 标"疑似过拟合"且不得 B 级以上 */
  decayRatio?: number;
  economicLogic?: string;
  expectedDirection?: "positive" | "negative";
  interpretabilityScore?: number;
  logicSource?: LogicSource;
  eliminationStatus?: string;
  eliminationReason?: string;
  createdAt: string;
}

export interface LineageNode {
  candidateId: string;
  generation: number;
  operation: OperationType;
  parentIds: string[];
  formulaExpr: string;
  category?: FactorCategory;
}

// ── 统计与分级（M2） ──────────────────────────────────

export interface WalkForwardResult {
  windows: number;
  sameDirection: number;
  perWindowIcir: number[];
}

export interface StatResult {
  pValue: number;
  pAdjBonferroni: number;
  qValueFdr: number;
  ciLower: number;
  ciUpper: number;
  /** 月频时不计算 */
  permPValue?: number;
  totalTrials: number;
  dsrIcir: number;
  decayRatio: number;
  walkForward: WalkForwardResult;
  /** true = 月频降级，Bootstrap/置换未计算 */
  degraded: boolean;
}

export interface GradeDimension {
  key: string;
  label: string;
  value: number;
  threshold: number;
  passed: boolean;
  gap: number;
}

export interface GradeEvidence {
  grade: QualityGrade;
  reason: string;
  dimensions: GradeDimension[];
  stats: StatResult;
  lineage: LineageNode[];
  thresholdsSource: "default" | "custom";
  /** 人工调整后被锁定，季度重评不覆盖 */
  manualAdjusted?: boolean;
  gradeHistory?: {
    grade: QualityGrade;
    previousGrade?: QualityGrade;
    trigger: "initial" | "quarterly" | "manual" | "downgrade_auto";
    reason: string;
    createdAt: string;
  }[];
}

// ── 候选池 ────────────────────────────────────────────

export interface PoolPreviewResult {
  hitCount: number;
  excludedByCategory: Record<string, number>;
  sampleSymbols: string[];
  /** 因缺失/PIT 门禁/数据版本不可用而未参与筛选的数量 */
  unavailableByField: Record<string, number>;
}

export interface PoolSnapshotRef {
  snapshotId: string;
  poolId: string;
  ruleHash: string;
  dataCutoffAt: string;
  memberCount: number;
  analysisJson?: PoolAnalysis;
}

export interface PoolAnalysis {
  memberCount: number;
  avgMarketCap?: number;
  marketCapTier?: string;
  dataCompleteness?: number;
  marketCapDistribution?: { tier: string; count: number; ratio: number }[];
  industryDistribution?: { industry: string; ratio: number }[];
  styleExposure?: { style: string; value: number }[];
  marketRegime?: { stage: string; volatility: string; trendStrength: string };
  factorTypeSuggestion?: { category: FactorCategory; stars: number }[];
  fieldCoverage?: { field: string; coverage: number }[];
  validTradeDays?: number;
  avgDailySymbols?: number;
  /** 年份 → 标的数（低覆盖年份需显式标注） */
  yearlySymbolCounts?: Record<string, number>;
  lowCoverageYears?: number[];
}

// ── 校验 ──────────────────────────────────────────────

export interface FieldValidationReport {
  validationId: string;
  status: ValidationStatus;
  configHash: string;
  expiresAt?: string;
  totalShards: number;
  doneShards: number;
  blockers: {
    fieldCode: string;
    fieldLabel: string;
    problemType: "coverage_low" | "pit_violation" | "no_data" | "lookback_exceeded";
    currentValue: string;
    requiredValue: string;
    reason: string;
    missingSummary?: Record<string, number>;
  }[];
  warnings: { fieldCode: string; message: string }[];
  perField: Record<
    string,
    { coverage: number; missingStreak: number; pitOk: boolean; firstDate?: string; lastDate?: string }
  >;
}

// ── 错误扩展（对应后端 FactorSevenError.extras） ────────

export interface MiningFixAction {
  action: "reselect_fields" | "go_to_data_repair" | "adjust_time_range" | "adjust_universe";
  targetStep: number;
  message: string;
}

export interface MiningErrorExtras {
  fixAction?: MiningFixAction;
  budget?: SplitBudget;
  ownerTaskId?: string;
  ownerRunId?: string;
}
