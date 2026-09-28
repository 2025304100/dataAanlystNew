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

describe("回测自动下单资格文案（PT-DEF-20 口径 A）", () => {
  // excluded_members_json 存的是“不具备自动下单资格”的审计名单，这些成员
  // 仍参与回测。文案一旦写回“已排除/excluded”，用户就会把审计信息读成过滤结果，
  // 所以把口径钉在用例里，而不是靠人记代码注释。
  const WORDING_KEYS = [
    "portfolioBacktest.excludedMembers",
    "portfolioBacktest.excludedMembersSummary",
    "portfolioBacktest.manualMembersWarning",
  ] as const;
  const CLARIFY_KEYS = [
    "portfolioBacktest.excludedMembersSummary",
    "portfolioBacktest.manualMembersWarning",
  ] as const;

  it("中文文案不得声称已排除，关键处要说明仍参与回测", () => {
    for (const key of WORDING_KEYS) {
      expect(String(zhCN[key])).not.toMatch(/排除/);
    }
    for (const key of CLARIFY_KEYS) {
      expect(String(zhCN[key])).toMatch(/仍参与/);
    }
  });

  it("英文文案同样不得声称 excluded", () => {
    for (const key of WORDING_KEYS) {
      expect(String(enUS[key]).toLowerCase()).not.toMatch(/exclud/);
    }
    for (const key of CLARIFY_KEYS) {
      expect(String(enUS[key]).toLowerCase()).toMatch(/still/);
    }
  });

  it("C-03 已删除的 only_auto 死键不得残留在一份字典里", () => {
    for (const dict of [zhCN, enUS]) {
      expect(dict).not.toHaveProperty("portfolioBacktest.onlyAuto");
      expect(dict).not.toHaveProperty("portfolioBacktest.onlyAutoHint");
    }
  });
});
