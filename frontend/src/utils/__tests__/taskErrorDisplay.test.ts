/**
 * WPD-05: taskErrorDisplay 工具单元测试。
 */
import { describe, expect, it } from "vitest";

import { getTaskErrorMessage, isLikelyMojibake } from "../taskErrorDisplay";

/** 模拟 i18n t()：有翻译返回翻译，无翻译返回 key 本身（与真实 t() 行为一致）。 */
function makeT(dict: Record<string, string>) {
  return (key: string): string => dict[key] ?? key;
}

describe("isLikelyMojibake", () => {
  it("对 null/undefined 返回 false", () => {
    expect(isLikelyMojibake(null)).toBe(false);
    expect(isLikelyMojibake(undefined)).toBe(false);
  });

  it("对空字符串返回 false", () => {
    expect(isLikelyMojibake("")).toBe(false);
  });

  it("对正常中文返回 false", () => {
    expect(isLikelyMojibake("因子仓库被占用或繁忙，请稍后重试")).toBe(false);
    expect(isLikelyMojibake("因子流水线运行失败，请稍后重试")).toBe(false);
  });

  it("对纯 ASCII 返回 false", () => {
    expect(isLikelyMojibake("Factor pipeline failed")).toBe(false);
    expect(isLikelyMojibake("Task created")).toBe(false);
  });

  it("对含 U+FFFD 替换符返回 true", () => {
    expect(isLikelyMojibake("�数�仓库被占用")).toBe(true);
    expect(isLikelyMojibake("因子\uFFFD仓库")).toBe(true);
  });

  it("对连续控制字符返回 true", () => {
    // 4 个连续 NUL 字符
    expect(isLikelyMojibake("abc\x00\x00\x00\x00def")).toBe(true);
  });

  it("对少量控制字符返回 false（不足 4 个连续）", () => {
    expect(isLikelyMojibake("abc\x00def\x01ghi")).toBe(false);
  });
});

describe("getTaskErrorMessage", () => {
  const t = makeT({
    error_code_DB_LOCK_TIMEOUT: "因子仓库被占用或繁忙，请稍后重试",
    error_code_UNKNOWN_ERROR: "因子流水线运行失败，请稍后重试",
    error_code_VALIDATION_ERROR: "因子流水线启动失败：参数校验未通过",
  });

  it("error_code 存在且有翻译 → 返回翻译", () => {
    const task = {
      message: "some raw message",
      error_code: "DB_LOCK_TIMEOUT",
    };
    expect(getTaskErrorMessage(task, t)).toBe("因子仓库被占用或繁忙，请稍后重试");
  });

  it("error_code 存在但无翻译 → 返回 message（兜底）", () => {
    const task = {
      message: "历史中文 message",
      error_code: "UNTRANSLATED_CODE",
    };
    expect(getTaskErrorMessage(task, t)).toBe("历史中文 message");
  });

  it("error_code 存在但无翻译且 message 为空 → 返回 UNKNOWN_ERROR 翻译", () => {
    const task = {
      message: "",
      error_code: "UNTRANSLATED_CODE",
    };
    expect(getTaskErrorMessage(task, t)).toBe("因子流水线运行失败，请稍后重试");
  });

  it("无 error_code 但 message 有效 → 返回 message", () => {
    const task = {
      message: "Task completed successfully",
      error_code: null,
    };
    expect(getTaskErrorMessage(task, t)).toBe("Task completed successfully");
  });

  it("无 error_code 且 message 含 U+FFFD → 返回 UNKNOWN_ERROR 翻译", () => {
    const task = {
      message: "�子仓库被占用\uFFFD",
      error_code: null,
    };
    expect(getTaskErrorMessage(task, t)).toBe("因子流水线运行失败，请稍后重试");
  });

  it("无 error_code 且 message 为空 → 返回空字符串", () => {
    const task = {
      message: "",
      error_code: null,
    };
    expect(getTaskErrorMessage(task, t)).toBe("");
  });

  it("task 为 null → 返回空字符串", () => {
    expect(getTaskErrorMessage(null, t)).toBe("");
  });

  it("task 为 undefined → 返回空字符串", () => {
    expect(getTaskErrorMessage(undefined, t)).toBe("");
  });

  it("errors[0].error_code 存在但顶层 error_code 不存在 → 按规则处理（有翻译）", () => {
    const task = {
      message: "raw message",
      error_code: null,
      errors: [{ error_code: "VALIDATION_ERROR" }],
    };
    expect(getTaskErrorMessage(task, t)).toBe("因子流水线启动失败：参数校验未通过");
  });

  it("errors[0].error_code 存在但无翻译 → 回退到 message", () => {
    const task = {
      message: "fallback message",
      error_code: null,
      errors: [{ error_code: "UNKNOWN_TO_I18N" }],
    };
    expect(getTaskErrorMessage(task, t)).toBe("fallback message");
  });

  it("errors[0].error_code 存在但无翻译 → 回退到 message（按规则 2，不检查乱码）", () => {
    // 规则 3 引用规则 1-2：error_code 存在但无翻译时返回 message，
    // 乱码检测仅在无 error_code 时（规则 4-5）触发
    const task = {
      message: "乱码\uFFFD消息",
      error_code: null,
      errors: [{ error_code: "UNKNOWN_TO_I18N" }],
    };
    expect(getTaskErrorMessage(task, t)).toBe("乱码\uFFFD消息");
  });

  it("errors[0] 无 error_code 字段 → 检查 message 是否乱码", () => {
    const task = {
      message: "正常中文消息",
      error_code: null,
      errors: [{ code: "LEGACY_CODE", error: "legacy" }],
    };
    expect(getTaskErrorMessage(task, t)).toBe("正常中文消息");
  });

  it("errors 为空数组且 message 有效 → 返回 message", () => {
    const task = {
      message: "正常消息",
      error_code: null,
      errors: [],
    };
    expect(getTaskErrorMessage(task, t)).toBe("正常消息");
  });

  it("所有字段为空/缺失 → 返回空字符串", () => {
    const task = {
      message: null,
      error_code: null,
      errors: [],
    };
    expect(getTaskErrorMessage(task, t)).toBe("");
  });
});

describe("getTaskErrorMessage with real i18n integration", () => {
  // 验证与真实 i18n 模块的集成（确保翻译键存在）
  it("使用真实 t() 时 DB_LOCK_TIMEOUT 返回中文翻译", async () => {
    const { t } = await import("../../i18n");
    const task = {
      message: "raw backend message",
      error_code: "DB_LOCK_TIMEOUT",
    };
    const result = getTaskErrorMessage(task, t);
    expect(result).toContain("因子仓库");
    expect(result).not.toBe("error_code_DB_LOCK_TIMEOUT");
  });

  it("使用真实 t() 时 UNKNOWN_ERROR 返回中文翻译", async () => {
    const { t } = await import("../../i18n");
    const task = {
      message: "\uFFFD乱码消息\uFFFD",
      error_code: null,
    };
    const result = getTaskErrorMessage(task, t);
    expect(result).toContain("因子流水线");
    expect(result).not.toBe("error_code_UNKNOWN_ERROR");
  });
});
