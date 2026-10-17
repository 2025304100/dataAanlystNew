import { describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/trustedResearchCandidate.json";
import { hydrateResearchModelPresentation, isTrustedResearchDataset, researchValidationPassed } from "../factorModelResearch";
import { getLocale, setLocale, t } from "../../i18n";

describe("real saved research candidate presentation evidence", () => {
  it("explains the independent-validation block in both supported languages", () => {
    const previous = getLocale();
    try {
      for (const locale of ["zh-CN", "en-US"] as const) {
        setLocale(locale);
        expect(t("factorModelResearchValidatedPending")).not.toBe("factorModelResearchValidatedPending");
        expect(t("factorModelResearchNoProduction")).not.toBe("factorModelResearchNoProduction");
        expect(t("factorModelResearchIndependentRequired")).toContain(locale === "zh-CN" ? "没有新的独立盲测" : "no new independent blind test");
      }
    } finally { setLocale(previous); }
  });

  it("recognizes the real model and the separate real task model/identity locations", () => {
    expect(fixture.model.id).toBe("ridge-e561cd9a4b126e0ef589f0d2");
    expect(fixture.task.id).toBe("d5aefcd9a1904156874b067c50e402b5");
    expect(researchValidationPassed(fixture.model)).toBe(true);
    expect(researchValidationPassed(fixture.task.result.model, fixture.task.result.trusted_history_dataset)).toBe(true);
    expect(researchValidationPassed(fixture.task.result.model)).toBe(false);
    expect(fixture.model.status).toBe("rejected");
    expect(fixture.model.production_eligible).toBe(false);
  });

  it("hydrates the actual scoring brief with display evidence while preserving status and eligibility", async () => {
    const before = JSON.stringify(fixture.scoring_brief);
    const read = vi.fn(async () => fixture.model);
    expect(researchValidationPassed(fixture.scoring_brief)).toBe(false);
    const [shown] = await hydrateResearchModelPresentation([fixture.scoring_brief], read);
    expect(read).toHaveBeenCalledWith(fixture.model.id);
    expect(researchValidationPassed(shown)).toBe(true);
    expect(shown.status).toBe("rejected");
    expect(shown.production_eligible).toBe(false);
    expect(shown).toHaveProperty('feature_versions', fixture.model.feature_versions);
    expect(JSON.stringify(fixture.scoring_brief)).toBe(before);
  });

  it.each(["read-failed", "wrong-id", "missing-identity", "numeric-failure", "eligible-conflict"])("keeps an actual brief unpromoted when detail evidence is %s", async (failure) => {
    const read = vi.fn(async () => {
      if (failure === "read-failed") throw new Error("Unavailable");
      return { ...fixture.model,
        ...(failure === "wrong-id" ? { id: "another-model" } : {}),
        ...(failure === "missing-identity" ? { hyperparameters: {} } : {}),
        ...(failure === "numeric-failure" ? { metrics: { research_model_gate_status: "rejected" } } : {}),
        ...(failure === "eligible-conflict" ? { production_eligible: true } : {}),
      };
    });
    const [shown] = await hydrateResearchModelPresentation([fixture.scoring_brief], read);
    expect(shown).toBe(fixture.scoring_brief);
    expect(researchValidationPassed(shown)).toBe(false);
  });

  it("does not fetch details for ordinary rejected rows or trust an IC value as research success", async () => {
    const brief = { ...fixture.scoring_brief, readiness_code: "MODEL_NOT_VALIDATED" };
    const read = vi.fn(async () => fixture.model);
    const [shown] = await hydrateResearchModelPresentation([brief], read);
    expect(shown).toBe(brief);
    expect(read).not.toHaveBeenCalled();
  });

  it.each([undefined, {}, { research_only: true }, { ...fixture.model.hyperparameters.trusted_history_dataset, dataset_id: " " }, { ...fixture.model.hyperparameters.trusted_history_dataset, research_only: false }])("does not infer research success from an absent or untrusted identity: %j", (identity) => {
    expect(researchValidationPassed({ ...fixture.model, hyperparameters: { ...fixture.model.hyperparameters, trusted_history_dataset: identity } })).toBe(false);
  });

  it("preserves real numerical failures, missing gates and inconsistent recorded gates", () => {
    expect(researchValidationPassed({ ...fixture.model, metrics: { research_model_gate_status: "rejected" } })).toBe(false);
    expect(researchValidationPassed({ ...fixture.model, hyperparameters: {}, metrics: {} })).toBe(false);
    expect(researchValidationPassed({ ...fixture.model, status: "failed" })).toBe(false);
    expect(researchValidationPassed({ status: "rejected", metrics: { validation_ic: .5 } })).toBe(false);
    expect(isTrustedResearchDataset([])).toBe(false);
  });
});
