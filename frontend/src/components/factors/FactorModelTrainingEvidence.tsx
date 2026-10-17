import { Alert, Descriptions, Tooltip } from "antd";
import { InfoCircleOutlined } from "@ant-design/icons";
import type { FactorModelRun } from "../../api/client";
import { t } from "../../i18n";
import { isTrustedResearchDataset } from "../../utils/factorModelResearch";

export function isObservedResearchComparison(hyperparameters?: Record<string, unknown> | null) {
  return hyperparameters?.research_evaluation_status === "previously_observed_development_comparison";
}

export function FactorModelResearchWarning({ hyperparameters }: { hyperparameters?: Record<string, unknown> | null }) {
  const observed = isObservedResearchComparison(hyperparameters);
  const trustedResearch = isTrustedResearchDataset(hyperparameters?.trusted_history_dataset);
  const quality = hyperparameters?.research_data_quality_limitations;
  const hasSourceConflict = typeof quality === "string" && quality.trim().length > 0;
  if (!observed && !hasSourceConflict) return null;
  return <>
    {observed && <Alert type="warning" showIcon message={t("factorModelObservedResearchTitle")}
      description={t(trustedResearch ? "factorModelResearchIndependentRequired" : "factorModelObservedResearchWarning")} style={{ marginBottom: 12 }} data-testid="model-research-warning" />}
    {hasSourceConflict && <Alert type="warning" showIcon message={t("factorModelSourceConflictTitle")}
      description={t("factorModelSourceConflictWarning")} style={{ marginBottom: 12 }} data-testid="model-data-quality-warning" />}
  </>;
}

/** Read only the exact model's recorded training evidence; never infer counts. */
export default function FactorModelTrainingEvidence({ model }: { model?: FactorModelRun | null }) {
  const metrics = model?.metrics;
  const hp = model?.hyperparameters;
  const preGatePreventedFit = model?.status === "rejected"
    && typeof model.rejection_reason === "string" && model.rejection_reason.includes("[P2-G Pre]")
    && Array.isArray(model.weights) && model.weights.length === 0
    && (model.artifact_path === null || model.artifact_path === "");
  const count = (key: string) => {
    const value = metrics?.[key];
    return typeof value === "number" && Number.isInteger(value) && value >= 0
      ? value.toLocaleString() : t("factorModelEvidenceNotRecorded");
  };
  const label = (key: string, help: string) => <Tooltip title={t(help)}><span>{t(key)} <InfoCircleOutlined /></span></Tooltip>;
  const protocol = typeof hp?.protocol_version === "string" && hp.protocol_version.trim() ? hp.protocol_version : null;
  const knownProtocol = protocol === "ridge-temporal-holdout-v2" || protocol === "ridge-temporal-cross-sectional-rank-v1";
  const research = isObservedResearchComparison(hp);
  const rankFit = hp?.fit_policy === "cross_sectional_rank_v1";
  const targetPolicy = hp?.fit_target_policy && typeof hp.fit_target_policy === "object" && !Array.isArray(hp.fit_target_policy)
    ? hp.fit_target_policy as Record<string, unknown> : null;
  const recordedRankTarget = targetPolicy?.target === "within_trade_date_average_rank_divided_by_n_plus_one_minus_half" && targetPolicy?.ties === "average";
  const recordedDateWeights = targetPolicy?.sample_weight === "n_rows / (n_dates * date_row_count)" && targetPolicy?.weight_normalization === "mean_one";
  const stage = hp?.coefficient_stage;
  const icir = metrics?.validation_icir;
  const pooledIc = metrics?.validation_ic_method === "pooled_spearman";
  const dailyIcIr = metrics?.validation_icir_method === "daily_spearman_mean_over_sample_std_ddof1";
  return <><FactorModelResearchWarning hyperparameters={hp} /><Descriptions size="small" column={{ xs: 1, sm: 1, md: 1, lg: 2 }} bordered style={{ marginBottom: 12, overflowWrap: "anywhere", wordBreak: "break-word" }} data-testid="model-training-evidence">
    {rankFit && <>
      <Descriptions.Item label={label("factorModelFitTarget", "factorModelRankFitTargetHelp")}>{recordedRankTarget ? t("factorModelRankFitTarget") : t("factorModelEvidenceNotRecorded")}</Descriptions.Item>
      <Descriptions.Item label={label("factorModelFitWeights", "factorModelEqualDateWeightsHelp")}>{recordedDateWeights ? t("factorModelEqualDateWeights") : t("factorModelEvidenceNotRecorded")}</Descriptions.Item>
    </>}
    <Descriptions.Item label={label(preGatePreventedFit ? "factorModelTrainingAdmissionSamples" : "factorModelActualFitSamples", preGatePreventedFit ? "factorModelTrainingAdmissionSamplesHelp" : "factorModelActualFitSamplesHelp")}>{count("training_sample_count")}</Descriptions.Item>
    <Descriptions.Item label={label("factorModelValidationSamples", "factorModelValidationSamplesHelp")}>{count("validation_sample_count")}</Descriptions.Item>
    <Descriptions.Item label={label("factorModelValidationDates", "factorModelValidationDatesHelp")}>{count("validation_date_count")}</Descriptions.Item>
    <Descriptions.Item label={label(preGatePreventedFit ? "factorModelInnerTrainingSegmentSamples" : "factorModelInnerFitSamples", preGatePreventedFit ? "factorModelFitSkippedPreGateHelp" : "factorModelInnerFitSamplesHelp")}>{count("inner_training_sample_count")}</Descriptions.Item>
    <Descriptions.Item label={label(preGatePreventedFit ? "factorModelTuningSegmentSamples" : "factorModelTuningSamples", preGatePreventedFit ? "factorModelFitSkippedPreGateHelp" : "factorModelTuningSamplesHelp")}>{count("tuning_sample_count")}</Descriptions.Item>
    <Descriptions.Item label={label("factorModelProtocol", research ? "factorModelObservedResearchWarning" : knownProtocol ? "factorModelProtocolV2Help" : "factorModelProtocolUnknownHelp")}>{protocol || t("factorModelEvidenceNotRecorded")}</Descriptions.Item>
    <Descriptions.Item label={label("factorModelCoefficientStage", preGatePreventedFit ? "factorModelFitSkippedPreGateHelp" : stage === "outer_training_purged" && knownProtocol ? research ? "factorModelResearchCoefficientHelp" : "factorModelCoefficientOuterHelp" : "factorModelProtocolUnknownHelp")}>{preGatePreventedFit ? t("factorModelFitSkippedPreGate") : stage === "outer_training_purged" && knownProtocol ? t("factorModelCoefficientOuter") : typeof stage === "string" && stage.trim() ? stage : t("factorModelEvidenceNotRecorded")}</Descriptions.Item>
    <Descriptions.Item label={label("factorModelValidationIcMethod", "factorModelPooledIcHelp")}>{pooledIc ? t("factorModelPooledIc") : t("factorModelEvidenceNotRecorded")}</Descriptions.Item>
    <Descriptions.Item label={label("factorModelValidationIcir", dailyIcIr ? "factorModelDailyIcirHelp" : "factorModelMetricMethodUnknownHelp")}>{typeof icir === "number" && Number.isFinite(icir) ? icir.toFixed(4) : t("factorModelEvidenceNotRecorded")}</Descriptions.Item>
    <Descriptions.Item label={label("factorModelValidationIcirMethod", "factorModelDailyIcirHelp")}>{dailyIcIr ? t("factorModelDailyIcir") : t("factorModelEvidenceNotRecorded")}</Descriptions.Item>
  </Descriptions></>;
}
