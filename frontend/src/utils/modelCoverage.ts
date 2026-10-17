import type { FactorCoverage, ScoringOverview } from "../api/client";

/** Real rows are authoritative. An aggregate never invents symbol/factor counts. */
export function modelCoverageSummary(overview: Partial<Pick<ScoringOverview,
  "factor_coverage" | "model_factor_avg_coverage" | "model_factor_coverage_count" |
  "active_factor_avg_coverage" | "active_factor_coverage_count">> | null | undefined) {
  const valid = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
  if (Array.isArray(overview?.factor_coverage)) {
    const rows: FactorCoverage[] = overview.factor_coverage.filter(row => row.model_enabled !== false);
    const known = rows.length > 0 && rows.every(row => valid(row.coverage));
    return { rows, average: known ? rows.reduce((sum, row) => sum + row.coverage, 0) / rows.length : null,
      lowCount: known ? rows.filter(row => row.coverage < .95).length : null };
  }
  const modelMean = overview?.model_factor_avg_coverage;
  const legacyMean = overview?.active_factor_avg_coverage;
  const average = (overview?.model_factor_coverage_count ?? 0) > 0 && valid(modelMean) ? modelMean
    : overview?.model_factor_coverage_count === 0 ? null
    : (overview?.active_factor_coverage_count ?? 0) > 0 && valid(legacyMean) ? legacyMean : null;
  return { rows: [] as FactorCoverage[], average, lowCount: null };
}
