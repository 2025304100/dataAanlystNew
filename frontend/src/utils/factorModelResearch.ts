type ResearchEvidence = {
  id?: unknown;
  status?: unknown;
  hyperparameters?: Record<string, unknown> | null;
  metrics?: Record<string, unknown> | null;
  research_model_gate_status?: unknown;
  readiness_code?: unknown;
  production_eligible?: unknown;
  feature_versions?: Record<string, number>;
};

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

export function isTrustedResearchDataset(value: unknown): boolean {
  const identity = record(value);
  return identity?.research_only === true && typeof identity.dataset_id === "string"
    && /^[a-z0-9][a-z0-9_-]{0,79}$/.test(identity.dataset_id) && typeof identity.manifest_sha256 === "string"
    && /^[a-f0-9]{64}$/.test(identity.manifest_sha256);
}

/** Display evidence only. This must never be used as an activation/scoring gate. */
export function researchValidationPassed(model?: ResearchEvidence | null, taskDatasetIdentity?: unknown): boolean {
  if (model?.status !== "rejected") return false;
  const identity = model.hyperparameters?.trusted_history_dataset ?? taskDatasetIdentity;
  if (!isTrustedResearchDataset(identity)) return false;
  const gates = [model.hyperparameters?.research_model_gate_status,
    model.research_model_gate_status, model.metrics?.research_model_gate_status]
    .filter(value => value !== undefined && value !== null);
  return gates.length > 0 && gates.every(value => value === "validated");
}

/** Scoring briefs omit research identity/gates. Read exact details only for known research rows.
 * Enrich display metadata; retain the brief's authoritative status and eligibility unchanged.
 */
export async function hydrateResearchModelPresentation<T extends ResearchEvidence>(
  models: T[], readModel: (id: string) => Promise<ResearchEvidence>,
): Promise<T[]> {
  return Promise.all(models.map(async model => {
    if (model.status !== "rejected" || model.readiness_code !== "MODEL_RESEARCH_DATASET_ONLY"
      || typeof model.id !== "string" || researchValidationPassed(model)) return model;
    try {
      const detail = await readModel(model.id);
      if (detail.id !== model.id || detail.production_eligible !== false || !researchValidationPassed(detail)) return model;
      return { ...model, feature_versions: detail.feature_versions ?? model.feature_versions, hyperparameters: { ...model.hyperparameters,
        trusted_history_dataset: detail.hyperparameters?.trusted_history_dataset,
        research_evaluation_status: detail.hyperparameters?.research_evaluation_status,
        research_model_gate_status: detail.hyperparameters?.research_model_gate_status },
        metrics: { ...model.metrics, research_model_gate_status: detail.metrics?.research_model_gate_status } };
    } catch {
      return model;
    }
  }));
}
