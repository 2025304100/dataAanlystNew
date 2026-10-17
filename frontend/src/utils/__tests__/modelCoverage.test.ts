import { expect, it } from "vitest";
import { modelCoverageSummary } from "../modelCoverage";
import type { ScoringOverview } from "../../api/client";

it("uses real model rows instead of observation-only inputs or synthetic averages", () => {
  const overview = { factor_coverage: [.740159, .990791, .108342, 0, .874503].map((coverage, i) => ({
    factor_code: String(i), coverage, model_enabled: true,
  })).concat([{ factor_code: "optional", coverage: 0, model_enabled: false }]),
    active_factor_avg_coverage: .342452 } as ScoringOverview;
  const result = modelCoverageSummary(overview);
  expect(result.average).toBeCloseTo(.542759);
  expect(result.rows).toHaveLength(5);
  expect(result.lowCount).toBe(4);
});

it("preserves true zero and never turns missing inputs into zero or an invented factor count", () => {
  expect(modelCoverageSummary(undefined)).toEqual({ rows: [], average: null, lowCount: null });
  expect(modelCoverageSummary({ active_factor_avg_coverage: 0, active_factor_coverage_count: 2 }).average).toBe(0);
  expect(modelCoverageSummary({ model_factor_avg_coverage: .8, model_factor_coverage_count: 3 }))
    .toEqual({ rows: [], average: .8, lowCount: null });
  expect(modelCoverageSummary({ factor_coverage: [], model_factor_avg_coverage: .8, model_factor_coverage_count: 3 }).average).toBeNull();
  expect(modelCoverageSummary({ model_factor_coverage_count: 0, active_factor_coverage_count: 8, active_factor_avg_coverage: .8 }).average).toBeNull();
});

it.each([undefined, NaN, -1, 2])("does not inflate the average by dropping unknown/invalid coverage %s", coverage => {
  const result = modelCoverageSummary({ factor_coverage: [{ factor_code: "bad", coverage }, { factor_code: "good", coverage: 1 }] } as ScoringOverview);
  expect(result.average).toBeNull(); expect(result.lowCount).toBeNull();
});
