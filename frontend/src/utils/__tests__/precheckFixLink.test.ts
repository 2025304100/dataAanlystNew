// 预检阻断项"去处"文案映射的单元测试（体检报告 §三十四）。
// 这里用真实 i18n（不 mock），所以顺带验到"字典里确实有这条文案"——
// 只 mock t 的话，登记漏了文案也会一路绿。
import { describe, expect, it } from "vitest";

import { describeFixTab, KNOWN_FIX_TABS } from "../precheckFixLink";

describe("describeFixTab", () => {
  it("已登记的 tab 给出可读名称，而不是把常量原样回显", () => {
    expect(KNOWN_FIX_TABS.length).toBeGreaterThan(0);
    for (const tab of KNOWN_FIX_TABS) {
      const label = describeFixTab(tab);
      expect(label).toBeTruthy();
      expect(label).not.toContain(tab);
      // 字典缺文案时会退化成 key 回显，那也是泄漏
      expect(label).not.toMatch(/^precheckFixTab\./);
    }
  });

  it("未知 tab 一律返回 null —— 绝不把后端常量交给界面", () => {
    for (const tab of ["members_only", "legacy_scan", "settings-data-center-x", "unknown_tab"]) {
      expect(describeFixTab(tab)).toBeNull();
    }
  });

  it("空值与空白都安全返回 null", () => {
    expect(describeFixTab(undefined)).toBeNull();
    expect(describeFixTab(null)).toBeNull();
    expect(describeFixTab("   ")).toBeNull();
  });

  it("覆盖后端当前实际会发出的全部 fix_link.tab 值", () => {
    // 来源：app/services/bfg_precheck_service.py 与 factors/wp5_eval_task.py 的 fix_link
    expect([...KNOWN_FIX_TABS].sort()).toEqual(
      ["factors", "portfolio-backtest", "settings", "settings-data-center"].sort(),
    );
  });

  it("带空格的脏输入仍能命中（不因后端拼接差异泄漏常量）", () => {
    expect(describeFixTab("  portfolio-backtest  ")).toBe(describeFixTab("portfolio-backtest"));
  });
});
