import { afterEach, describe, expect, it } from "vitest";
import enUS from "../en-US";
import { enumLabel, setLocale, t } from "../index";
import zhCN from "../zh-CN";

afterEach(() => setLocale("zh-CN"));

describe("i18n dictionaries", () => {
  it("keeps Chinese and English key sets aligned", () => {
    expect(Object.keys(zhCN).sort()).toEqual(Object.keys(enUS).sort());
  });

  it("renders common enums in Chinese", () => {
    setLocale("zh-CN");
    expect(t("all")).toBe("全部");
    expect(enumLabel("taskStatus", "running")).toBe("运行中");
    expect(enumLabel("taskStage", "calc_scores")).toBe("计算评分");
    expect(enumLabel("aiEvidenceType", "score")).toBe("评分");
    expect(enumLabel("aiEvidenceSource", "discovery")).toBe("机会挖掘");
    expect(enumLabel("observationPoolStatus", "archived")).toBe("已归档");
    expect(enumLabel("factorMode", "manual")).toBe("手工权重");
  });

  it("renders the same enums in English after switching locale", () => {
    setLocale("en-US");
    expect(t("all")).toBe("All");
    expect(enumLabel("taskStage", "calc_scores")).toBe("Calculating Scores");
    expect(enumLabel("aiEvidenceSource", "discovery")).toBe("Discovery");
    expect(enumLabel("observationPoolOrigin", "candidate")).toBe("Candidate");
  });
});
