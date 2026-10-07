// 对账差异类型 → 用户可读名称 / 含义 / 结论分诊 的标签层测试。
//
// 这里刻意**不 mock i18n**：拟真走查暴露的问题就是「对账守恒」把后端枚举直接拼进
// 维度列，用户读到的是「自定义维度 (DECISION_RUN_NOT_FOUND)」，顶栏还把「当天没跑
// 决策、无账可对」说成「守恒校验失败：加减和 ≠ 0」。若把 t() 换成"返回键名"的假实现，
// 就永远验不到"字典里到底有没有这句话"（返回 reconDimDecisionRunNotFound 同样算泄露）。
import { afterEach, describe, expect, it } from "vitest";

import { setLocale } from "../../i18n";
import {
  KNOWN_RECON_DIFF_KINDS,
  classifyReconVerdict,
  reconDimCodeForTooltip,
  reconDimLabel,
  reconDimWhy,
} from "../reconDimLabel";

/** 后端真实会产生 diff.kind 的取值（与 app/services/portfolio_reconciliation.py
 *  的 DiffKind 字面量对齐）。 */
const BACKEND_DIFF_KINDS = [
  "DECISION_RUN_NOT_FOUND",
  "MISSING_ORDER_PLAN",
  "UNFILLED_PLAN",
  "POSITION_MISMATCH",
  "NAV_BROKEN",
  "NOT_CHECKED",
];

afterEach(() => setLocale("zh-CN"));

describe("reconDimLabel", () => {
  it("后端每一种 diff kind 都能拿到中文名称，且不等于内部 code", () => {
    for (const kind of BACKEND_DIFF_KINDS) {
      const label = reconDimLabel(kind);
      expect(label, `diff kind=${kind} 没有可用文案`).toBeTruthy();
      expect(label).not.toBe(kind);
      // 键名回显（例如 reconDimDecisionRunNotFound）也算泄露：说明字典里没这条文案
      expect(label).not.toMatch(/^reconDim[A-Z]/);
      // 内部 code 的形态（UPPER_SNAKE）不允许出现在正文里
      expect(label).not.toMatch(/^[A-Z][A-Z0-9_]+$/);
    }
  });

  it("走查里最刺眼的那一行现在说人话了", () => {
    expect(reconDimLabel("DECISION_RUN_NOT_FOUND")).toBe("当日无决策运行");
  });

  it("未收录类型不返回枚举原值，只给通用文案；原值单独取用做 tooltip", () => {
    expect(reconDimLabel("SOME_NEW_KIND")).toBe("其他检查项");
    expect(reconDimLabel(null)).toBe("其他检查项");
    expect(reconDimLabel("")).toBe("其他检查项");
    expect(reconDimCodeForTooltip("SOME_NEW_KIND")).toBe("SOME_NEW_KIND");
    // 命中映射时不需要 tooltip 里的技术细节
    expect(reconDimCodeForTooltip("DECISION_RUN_NOT_FOUND")).toBeNull();
    expect(reconDimCodeForTooltip(null)).toBeNull();
  });

  it("每种类型都配了一句人话解释，且不泄露枚举形态", () => {
    for (const kind of BACKEND_DIFF_KINDS) {
      const why = String(reconDimWhy(kind) ?? "");
      expect(why, `diff kind=${kind} 缺少人话解释`).toBeTruthy();
      expect(why).not.toContain(kind);
      expect(why).not.toMatch(/^[A-Z][A-Z0-9_]+$/);
    }
    expect(reconDimWhy("SOME_NEW_KIND")).toBeNull();
  });

  it("已收录清单与后端枚举一致，非空且都能拿到文案", () => {
    expect([...KNOWN_RECON_DIFF_KINDS].sort()).toEqual([...BACKEND_DIFF_KINDS].sort());
    for (const kind of KNOWN_RECON_DIFF_KINDS) {
      expect(reconDimLabel(kind)).toBeTruthy();
    }
  });
});

describe("classifyReconVerdict", () => {
  it("守恒通过 → passed", () => {
    expect(classifyReconVerdict({ zero_sum_check_passed: true, items: [] })).toBe("passed");
  });

  it("只有「当日无决策」时不报成账实不符，而是 no_decision", () => {
    expect(classifyReconVerdict({
      zero_sum_check_passed: false,
      items: [{ dimension: "DECISION_RUN_NOT_FOUND" }],
    })).toBe("no_decision");
  });

  it("只有 NOT_CHECKED（数据源未接入）时不吓人，报 unavailable", () => {
    expect(classifyReconVerdict({
      zero_sum_check_passed: false,
      items: [{ dimension: "NOT_CHECKED" }],
    })).toBe("unavailable");
  });

  it("真实差异（缺单/未成交/持仓不符/现金不符）一律报 broken", () => {
    for (const kind of ["MISSING_ORDER_PLAN", "UNFILLED_PLAN", "POSITION_MISMATCH", "NAV_BROKEN"]) {
      expect(
        classifyReconVerdict({ zero_sum_check_passed: false, items: [{ dimension: kind }] }),
        `diff kind=${kind} 应判为 broken`,
      ).toBe("broken");
    }
  });

  it("真实差异与「当日无决策」同时出现时，真实差异优先（不能被缺基准掩盖）", () => {
    expect(classifyReconVerdict({
      zero_sum_check_passed: false,
      items: [{ dimension: "DECISION_RUN_NOT_FOUND" }, { dimension: "POSITION_MISMATCH" }],
    })).toBe("broken");
  });

  it("未通过但没有任何差异行 → 按 passed 兜底，不臆造问题", () => {
    expect(classifyReconVerdict({ zero_sum_check_passed: false, items: [] })).toBe("passed");
    expect(classifyReconVerdict({})).toBe("passed");
  });

  it("容忍脏数据：null 行与空维度被忽略", () => {
    expect(classifyReconVerdict({
      zero_sum_check_passed: false,
      items: [null, { dimension: "" }, { dimension: null }],
    })).toBe("passed");
  });
});
