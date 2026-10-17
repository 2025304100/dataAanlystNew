import { getLocale, t } from "../../../i18n";

function label(value: string | null | undefined, keys: Record<string, string>): string {
  if (!value) return "-";
  const key = keys[value.toLowerCase()];
  return key ? t(key) : value;
}

export function miningFrequencyLabel(value: string | null | undefined): string {
  return label(value, { daily: "miningFreqDaily", weekly: "miningFreqWeekly", monthly: "miningFreqMonthly" });
}

export function miningSourceLabel(value: string | null | undefined): string {
  return label(value, {
    ai: "miningOriginAi", ai_generated: "miningOriginAi",
    enumerated: "miningOriginEnumerated", template: "miningOriginEnumerated",
    random: "miningOriginRandom", random_injection: "miningOriginRandom",
    manual: "miningOriginManual", mutation: "miningOperationMutation",
    crossover: "miningOperationCrossover", elite: "miningOperationElite",
    elitism: "miningOperationElite", reproduction: "miningOperationReproduction",
    initial: "miningOperationInitial", seed: "miningOperationInitial",
  });
}

export function miningExperienceStatusLabel(value: string | null | undefined): string {
  return label(value, {
    normal: "miningExperienceStatusNormal", premium: "miningExperienceStatusPremium",
    archived: "miningExperienceStatusArchived", need_field: "miningExperienceStatusNeedField",
    unavailable: "miningExperienceStatusUnavailable",
  });
}

export function miningStageLabel(value: string | null | undefined): string {
  return label(value, {
    queued: "miningRunStatusQueued", running: "miningRunStatusRunning",
    validating: "miningRunStatusValidating", paused: "miningRunStatusPaused",
    draft: "miningTaskStatusDraft", converged: "miningRunStatusConverged",
    discarded: "miningRunStatusDiscarded", cancel_requested: "miningRunStatusCancelRequested",
    invalidated: "miningRunStatusInvalidated",
    done: "miningRunStatusDone", succeeded: "miningRunStatusSucceeded",
    failed: "miningRunStatusFailed", cancelled: "miningRunStatusCancelled",
    prepare: "taskStage_prepare", mirror: "taskStage_mirror",
    targets: "taskStage_targets", factors: "taskStage_factors",
    interrupted: "taskStage_interrupted", stalled: "taskStage_stalled",
  });
}

/** Backend event timestamps are UTC, including legacy ISO strings without an offset. */
export function formatMiningDateTime(value: string | null | undefined): string {
  if (!value?.trim()) return "-";
  const input = value.trim();
  // Date-only business boundaries must retain their calendar day in every timezone.
  if (/^\d{4}-\d{2}-\d{2}$/.test(input)) {
    const date = new Date(`${input}T00:00:00Z`);
    return Number.isNaN(date.getTime()) ? "-" : new Intl.DateTimeFormat(getLocale(), {
      year: "numeric", month: "2-digit", day: "2-digit", timeZone: "UTC",
    }).format(date);
  }
  const iso = input.replace(" ", "T");
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : `${iso}Z`;
  const date = new Date(zoned);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat(getLocale(), {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(date);
}
