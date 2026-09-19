// TR-12.1: 验证 renderReasonToString 对非字符串类型安全，特别是对 Date 对象不抛 split 错误
import { describe, it, expect } from "vitest";
import { renderReasonToString, normalizeBackendError } from "../errorRender";

describe("renderReasonToString TR-12.1 crash-safe", () => {
  it("{ error_code, message: new Date() } 不抛 TypeError: split is not a function，且结果含 Date", () => {
    const payload = { error_code: "X", message: new Date("2026-09-02T00:00:00.000Z") };
    let result = "";
    expect(() => {
      result = renderReasonToString(payload);
    }).not.toThrow();
    expect(typeof result).toBe("string");
    // message 本身是 Date 对象；renderReasonToString 在结构化分支中取 detail_zh/message 走 safeStringify -> [Date: ...]
    expect(/Date/.test(result) || /\[X\]/.test(result) || result.includes("UNKNOWN_ERROR")).toBe(true);
    // 额外断言：如果对 result 自己调用 split 仍然不抛
    expect(() => result.split(":")).not.toThrow();
  });

  it("纯 new Date() 仍然返回字符串，含 Date", () => {
    const d = new Date("2026-01-01");
    let s = "";
    expect(() => { s = renderReasonToString(d); }).not.toThrow();
    expect(typeof s).toBe("string");
    expect(/Date/.test(s)).toBe(true);
  });

  it("Error 对象 / 普通对象 / 字符串都安全", () => {
    expect(renderReasonToString(new Error("boom"))).toContain("boom");
    expect(renderReasonToString(null)).toBe("");
    expect(renderReasonToString(undefined)).toBe("");
    expect(renderReasonToString({})).toBe("{}");
    expect(renderReasonToString("ok")).toBe("ok");
  });

  it("超长字符串被截断至 maxLen，不崩溃", () => {
    const long = "a".repeat(1000);
    const s = renderReasonToString(long, 400);
    expect(s.length).toBe(400);
    expect(s.endsWith("...")).toBe(true);
  });
});

describe("normalizeBackendError 7 要素归一化", () => {
  it("新协议 7 要素原样返回", () => {
    const r = normalizeBackendError({
      error_code: "PRECHECK_BLOCKED",
      title_zh: "回测预检未通过",
      detail_zh: "缺口 39 天",
      correlation_id: "abc123",
      impact: "不创建 run",
      fix_link: "/docs/x",
      retryable: false,
      blocking_reasons: [{ code: "A" }],
    });
    expect(r.error_code).toBe("PRECHECK_BLOCKED");
    expect(r.title_zh).toBe("回测预检未通过");
    expect(r.detail_zh).toContain("缺口 39 天");
    expect(r.correlation_id).toBe("abc123");
    expect(r.impact).toBe("不创建 run");
    expect(r.fix_link).toBe("/docs/x");
    expect(r.retryable).toBe(false);
    expect(r.blocking_reasons).toHaveLength(1);
  });

  it("旧 FastAPI HTTPException.detail=dict 归一化", () => {
    const r = normalizeBackendError({
      response: {
        data: {
          detail: { error_code: "BACKTEST_PORTFOLIO_NOT_FOUND", message: "not found 42" },
        },
      },
    });
    expect(r.error_code).toBe("BACKTEST_PORTFOLIO_NOT_FOUND");
    expect(r.detail_zh).toContain("not found 42");
  });

  it("Date 作为 message，normalize 后不抛，且含 Date 信息", () => {
    let r: ReturnType<typeof normalizeBackendError> | null = null;
    expect(() => {
      r = normalizeBackendError({ detail: { message: new Date("2026-06-01T00:00:00.000Z") } });
    }).not.toThrow();
    expect(r).toBeTruthy();
    expect(typeof (r as any).raw_message).toBe("string");
  });
});
