import { afterEach, describe, expect, it } from "vitest";
import { setLocale } from "../../../i18n";
import { formatMiningDateTime, miningExperienceStatusLabel, miningFrequencyLabel, miningSourceLabel } from "./miningDisplay";

afterEach(() => setLocale("zh-CN"));

describe("mining display localization", () => {
  it("translates known values and preserves unknown server values", () => {
    setLocale("zh-CN");
    expect(miningFrequencyLabel("weekly")).toBe("周频");
    expect(miningSourceLabel("manual")).toBe("人工录入");
    expect(miningExperienceStatusLabel("normal")).toBe("正常");
    setLocale("en-US");
    expect(miningFrequencyLabel("weekly")).toBe("Weekly");
    expect(miningSourceLabel("mutation")).toBe("Mutation");
    expect(miningExperienceStatusLabel("normal")).toBe("Normal");
    expect(miningExperienceStatusLabel("custom_status")).toBe("custom_status");
    expect(miningSourceLabel(null)).toBe("-");
  });

  it("formats date-only boundaries in the selected locale without shifting their day", () => {
    setLocale("zh-CN");
    expect(formatMiningDateTime("2026-09-20")).toBe("2026/09/20");
    setLocale("en-US");
    expect(formatMiningDateTime("2026-09-20")).toBe("09/20/2026");
  });

  it("interprets legacy UTC timestamps consistently and handles absent or invalid dates", () => {
    expect(formatMiningDateTime("2026-09-20T15:55:35")).toBe(formatMiningDateTime("2026-09-20T15:55:35Z"));
    expect(formatMiningDateTime("2026-09-20 15:55:35")).toBe(formatMiningDateTime("2026-09-20T15:55:35Z"));
    expect(formatMiningDateTime("2026-09-20T23:55:35+08:00")).toBe(formatMiningDateTime("2026-09-20T15:55:35Z"));
    expect(formatMiningDateTime("2026-09-20T15:55:35Z")).not.toContain("T");
    expect(formatMiningDateTime(undefined)).toBe("-");
    expect(formatMiningDateTime("bad timestamp")).toBe("-");
  });
});
