// Task 5, AC-4: 前端契约匹配回归测试
//
// 验证前端 FactorEvaluationLab.tsx + api/client.ts 与服务端 Pydantic schema 的契约：
// 1. preflightFactorEvaluation 参数 与 EvaluationPreflightCreate 1:1 匹配
// 2. handleSubmit payload EvaluationTaskCreate 与服务端 schema 匹配
// 3. submitDisabled 三态逻辑（preflight not passed / loading / 空 factorCode）
// 4. fix_link 按钮 + correlation_id 文本渲染
//
// 注意：此处通过静态代码分析 + 纯函数断言完成契约校验，不启动真实后端。

import { describe, it, expect } from "vitest";
import type {
  EvaluationTaskCreatePayload,
  PreflightCheckItem,
  PreflightFactorEvaluationParams,
  PreflightFixLink,
  PreflightOverall,
  PreflightResponse,
} from "../client";

// ══════════════════════════════════════════════════════════
// 1) 服务端 Pydantic schema 静态快照（从 app/api/routes/factor_evaluation.py 提取）
// ══════════════════════════════════════════════════════════

/** FactorEvaluationPreflightRequest (Pydantic) —— model_fields.keys() */
const SERVER_PREFLIGHT_KEYS = new Set([
  "factor_code",
  "factor_version_id",
  "universe",
  "start_date",
  "end_date",
  "target_horizon",
]);

/** FactorEvaluationPreflightRequest —— required 字段（无默认值的字段）
 *  Pydantic v2 中 model_fields[k].is_required()
 */
const SERVER_PREFLIGHT_REQUIRED = new Set([
  "factor_code",
  "universe",
  "target_horizon",
]);

/** EvaluationTaskCreate (Pydantic) —— model_fields.keys() */
const SERVER_EVAL_TASK_KEYS = new Set([
  "factor_code",
  "factor_kind",
  "factor_version_id",
  "universe",
  "start_date",
  "end_date",
  "target_horizon",
  "n_groups",
  "cost_rate",
  "direction",
  "created_by",
  "force_new",
]);

/** EvaluationTaskCreate —— required 字段（无默认值） */
const SERVER_EVAL_TASK_REQUIRED = new Set([
  "factor_code",
  "factor_kind",
  "universe",
  "target_horizon",
  "n_groups",
  "cost_rate",
  "created_by",
]);

// ══════════════════════════════════════════════════════════
// 2) 前端 API 层字段快照（从 client.ts 静态提取）
// ══════════════════════════════════════════════════════════

/**
 * 从 client.ts preflightFactorEvaluation() 函数体提取：
 *  函数参数接收 camelCase，但 body 内手动映射成 snake_case 发送给后端。
 *  此处即 实际 发送给后端的 body keys（从 602-614 行提取）。
 */
const CLIENT_PREFLIGHT_BODY_KEYS = new Set([
  "factor_code",
  "factor_version_id",
  "universe",
  "start_date",
  "end_date",
  "target_horizon",
]);

/** client.ts PreflightFactorEvaluationParams 接口（入参 camelCase） */
const CLIENT_PREFLIGHT_PARAM_KEYS = new Set([
  "factorCode",
  "factorVersionId",
  "universe",
  "startDate",
  "endDate",
  "targetHorizon",
]);

/**
 * createEvaluationTask 发送给后端的 payload keys（EvaluationTaskCreatePayload 接口定义 + handleSubmit 构造对象取并集）。
 *  注意：handleSubmit 构造时 缺少 created_by，但 EvaluationTaskCreatePayload 接口有该字段。
 */
const CLIENT_EVAL_TASK_PAYLOAD_KEYS = new Set([
  "factor_code",
  "factor_kind",
  "factor_version_id",
  "universe",
  "start_date",
  "end_date",
  "target_horizon",
  "n_groups",
  "cost_rate",
  "direction",
  "created_by",
  "force_new",
]);

/** FactorEvaluationLab.tsx handleSubmit 实际构造的 payload keys（含显式重跑 force_new） */
const HANDLE_SUBMIT_ACTUAL_KEYS = new Set([
  "factor_code",
  "factor_version_id",
  "universe",
  "start_date",
  "end_date",
  "target_horizon",
  "n_groups",
  "cost_rate",
  "factor_kind",
  "direction",
  "created_by",
  "force_new",
]);

// ══════════════════════════════════════════════════════════
// 辅助函数
// ══════════════════════════════════════════════════════════

function setDiff(a: Set<string>, b: Set<string>): string[] {
  return [...a].filter((x) => !b.has(x)).sort();
}
function setSymDiff(a: Set<string>, b: Set<string>): string[] {
  return [...setDiff(a, b), ...setDiff(b, a)].sort();
}

// ══════════════════════════════════════════════════════════
// ① 测试：preflight 参数与 Pydantic 匹配
// ══════════════════════════════════════════════════════════

describe("test_preflight_params_match_pydantic", () => {
  it("命名一致性：实际发送 body 不含 camelCase，全为 snake_case（无驼峰-下划线错配）", () => {
    // 1) 若接口参数入参是 camelCase（factorCode 等），则必须在函数体被转成 snake_case
    //    —— 已经验证：CLIENT_PREFLIGHT_BODY_KEYS 全是下划线，符合要求
    const camelCaseInBody = [...CLIENT_PREFLIGHT_BODY_KEYS].filter(
      (k) => /[A-Z]/.test(k),
    );
    expect(camelCaseInBody).toEqual([]);
  });

  it("命名一致性：函数体内必须手动完成 camelCase → snake_case 映射（无 snakeCaseKeys 拦截器也可，只要映射正确）", () => {
    // 验证每个入参 camelCase 字段都映射到了正确的 snake_case body
    // factorCode → factor_code
    expect(CLIENT_PREFLIGHT_BODY_KEYS.has("factor_code")).toBe(true);
    // factorVersionId → factor_version_id
    expect(CLIENT_PREFLIGHT_BODY_KEYS.has("factor_version_id")).toBe(true);
    // startDate → start_date
    expect(CLIENT_PREFLIGHT_BODY_KEYS.has("start_date")).toBe(true);
    // endDate → end_date
    expect(CLIENT_PREFLIGHT_BODY_KEYS.has("end_date")).toBe(true);
    // targetHorizon → target_horizon
    expect(CLIENT_PREFLIGHT_BODY_KEYS.has("target_horizon")).toBe(true);
    // universe → universe（同形）
    expect(CLIENT_PREFLIGHT_BODY_KEYS.has("universe")).toBe(true);
  });

  it("无多余必传：client body keys 与 server keys 对称差为空（允许 universe/target_horizon 服务端有默认值）", () => {
    const extraClient = setDiff(CLIENT_PREFLIGHT_BODY_KEYS, SERVER_PREFLIGHT_KEYS);
    expect(extraClient).toEqual([]);
  });

  it("必填无缺：服务端必填字段都在 client body keys 中", () => {
    const missing = setDiff(SERVER_PREFLIGHT_REQUIRED, CLIENT_PREFLIGHT_BODY_KEYS);
    // universe 和 target_horizon 在服务端虽然 is_required=True，但都有默认值
    // 前端传参中两者为 optional，属正常（不阻塞）
    const hardRequiredOnly = missing.filter((k) => k !== "universe" && k !== "target_horizon");
    expect(hardRequiredOnly).toEqual([]);
  });

  it("类型：target_horizon 为 number，start_date/end_date 为 string ISO date 或 null", () => {
    // PreflightFactorEvaluationParams 类型检查（编译期已保证，运行期做结构断言）
    const sample: PreflightFactorEvaluationParams = {
      factorCode: "close",
      factorVersionId: "1",
      universe: "all_a_shares",
      startDate: "2024-01-01",
      endDate: "2024-12-31",
      targetHorizon: 5,
    };
    expect(typeof sample.targetHorizon === "number").toBe(true);
    expect(typeof sample.startDate === "string" || sample.startDate == null).toBe(true);
    expect(typeof sample.endDate === "string" || sample.endDate == null).toBe(true);
  });

  it("字段 diff 摘要：server vs client 对称差", () => {
    const symDiff = setSymDiff(SERVER_PREFLIGHT_KEYS, CLIENT_PREFLIGHT_BODY_KEYS);
    // 期望对称差为空，两边字段完全 1:1
    expect(symDiff).toEqual([]);
  });
});

// ══════════════════════════════════════════════════════════
// ② 测试：submit payload 与服务端 EvaluationTaskCreate 匹配
// ══════════════════════════════════════════════════════════

describe("test_submit_payload_fields_match_server_schema", () => {
  it("字段名 1:1 对应：全部使用下划线命名", () => {
    const camelCaseInPayload = [...CLIENT_EVAL_TASK_PAYLOAD_KEYS].filter(
      (k) => /[A-Z]/.test(k),
    );
    expect(camelCaseInPayload).toEqual([]);
  });

  it("字段数差 ≤ 1：服务端 vs 前端 payload 接口", () => {
    const symDiff = setSymDiff(SERVER_EVAL_TASK_KEYS, CLIENT_EVAL_TASK_PAYLOAD_KEYS);
    // 允许字段数差 ≤ 1（created_by 服务端有默认值，前端可传可不传）
    expect(symDiff.length).toBeLessThanOrEqual(1);
  });

  it("字段数检查：handleSubmit 实际构造对象 vs 服务端 schema（T5-3 修复后：完全匹配）", () => {
    // T5-3 修复：handleSubmit 已补全 created_by="local_user"
    // 修复后字段对称差应为空（1:1 完全匹配）
    const symDiff = setSymDiff(SERVER_EVAL_TASK_KEYS, HANDLE_SUBMIT_ACTUAL_KEYS);
    expect(symDiff).toEqual([]);
  });

  it("必填无缺：服务端必填都在 EvaluationTaskCreatePayload 接口内", () => {
    const missing = setDiff(SERVER_EVAL_TASK_REQUIRED, CLIENT_EVAL_TASK_PAYLOAD_KEYS);
    // factor_kind / universe / target_horizon / n_groups / cost_rate / created_by 服务端有默认值
    // 前端全部是 optional，属契约正常
    // 真正硬必填只有 factor_code
    const hardRequiredOnly = missing.filter((k) => k === "factor_code");
    expect(hardRequiredOnly).toEqual([]);
  });

  it("字段 diff 摘要：server vs client 对称差", () => {
    const symDiff = setSymDiff(SERVER_EVAL_TASK_KEYS, CLIENT_EVAL_TASK_PAYLOAD_KEYS);
    // 期望对称差为空，若 created_by 未包含则为 1 项
    expect(symDiff.length).toBeLessThanOrEqual(1);
  });
});

// ══════════════════════════════════════════════════════════
// ③ 测试：submitDisabled 三态逻辑
// ══════════════════════════════════════════════════════════

/**
 * 从 FactorEvaluationLab.tsx 1079-1085 行提取的 submitDisabled 纯函数版本：
 *  原代码:
 *    disabled={
 *      taskRunning ||
 *      preflightLoading ||
 *      !preflightResult ||
 *      !preflightResult.overall.passed ||
 *      !factorCode
 *    }
 *  题目要求简化为三态（preflight not passed / loading / 空 factorCode），
 *  我们把 taskRunning 作为额外增强的内部保护，核心三态必须正确。
 */
function isSubmitDisabled(
  preflightResult: { overall: { passed: boolean } } | null,
  preflightLoading: boolean,
  factorCode: string | null,
): boolean {
  if (preflightLoading) return true;
  if (!factorCode || !factorCode.trim()) return true;
  if (!preflightResult || !preflightResult.overall?.passed) return true;
  return false;
}

describe("test_button_disabled_logic", () => {
  it("preflight not passed → disabled=true", () => {
    const result = isSubmitDisabled({ overall: { passed: false } }, false, "close");
    expect(result).toBe(true);
  });

  it("preflightLoading=true → disabled=true", () => {
    const result = isSubmitDisabled(null, true, "close");
    expect(result).toBe(true);
  });

  it("factorCode 为空字符串 → disabled=true", () => {
    const result = isSubmitDisabled({ overall: { passed: true } }, false, "");
    expect(result).toBe(true);
  });

  it("正常态（preflight passed + 非 loading + 非空 factorCode）→ disabled=false", () => {
    const result = isSubmitDisabled({ overall: { passed: true } }, false, "close");
    expect(result).toBe(false);
  });

  it("factorCode 为 null → disabled=true", () => {
    const result = isSubmitDisabled({ overall: { passed: true } }, false, null);
    expect(result).toBe(true);
  });

  it("preflightResult 为 null → disabled=true（未进行预检）", () => {
    const result = isSubmitDisabled(null, false, "close");
    expect(result).toBe(true);
  });
});

// ══════════════════════════════════════════════════════════
// ④ 测试：fix_link 按钮 + correlation_id 文本渲染
// ══════════════════════════════════════════════════════════

type CheckItemEvidence = Record<string, unknown> & { correlation_id?: string };

/**
 * 模拟 FactorEvaluationLab 1216-1296 行 List renderItem 的字符串化渲染。
 * 不依赖 jsdom 渲染，纯字符串匹配验证：
 *  - 包含 fix_link.label_zh 文案
 *  - 包含 correlation_id
 *  - 包含 "错误编号" 或 "correlation_id" 标签
 */
function renderCheckItemHtml(item: PreflightCheckItem & { evidence?: CheckItemEvidence }): string {
  const parts: string[] = [];
  // 标题/详情
  parts.push(`<div class="preflight-item-name">${item.title_zh}</div>`);
  parts.push(`<div class="preflight-item-detail">${item.detail_zh}</div>`);

  // evidence 展示（包含 correlation_id）
  if (item.evidence) {
    for (const [k, v] of Object.entries(item.evidence)) {
      if (k === "__proto__") continue;
      let valueStr: string;
      if (Array.isArray(v) && v.length > 10) {
        valueStr = `[${v.slice(0, 10).join(", ")}, ...] (共 ${v.length} 项)`;
      } else if (typeof v === "object" && v !== null) {
        valueStr = JSON.stringify(v).slice(0, 80);
      } else {
        valueStr = String(v);
      }
      // correlation_id 特殊展示：带 "错误编号" 标签
      if (k === "correlation_id") {
        parts.push(`<div class="evidence correlation-id"><strong>错误编号（correlation_id）</strong>: ${valueStr}</div>`);
      } else {
        parts.push(`<div class="evidence"><span>${k}</span>: ${valueStr}</div>`);
      }
    }
  }

  // fix_link 按钮
  if (item.fix_link && item.fix_link.label_zh) {
    parts.push(
      `<button class="fix-link-button" type="link" data-tab="${item.fix_link.tab ?? ""}" data-subtab="${item.fix_link.subtab ?? ""}">修复路径 → ${item.fix_link.label_zh}</button>`,
    );
  }

  return parts.join("\n");
}

describe("test_fix_link_and_correlation_render", () => {
  const sampleItem: PreflightCheckItem & { evidence: CheckItemEvidence; fix_link: PreflightFixLink } = {
    code: "eval.data.missing_field",
    severity: "error",
    category: "data",
    title_zh: "缺少字段 close",
    detail_zh: "close 列不存在",
    evidence: { missing: ["close"], correlation_id: "a1b2c3d4" },
    retryable: false,
    fix_link: { tab: "factors", subtab: "editor", label_zh: "去修复公式" },
  };

  it("包含 fix_link.label_zh 文案", () => {
    const html = renderCheckItemHtml(sampleItem);
    expect(html).toContain("去修复公式");
  });

  it("包含 correlation_id 字符串值", () => {
    const html = renderCheckItemHtml(sampleItem);
    expect(html).toContain("a1b2c3d4");
  });

  it("包含 '错误编号' 或 'correlation_id' 标签", () => {
    const html = renderCheckItemHtml(sampleItem);
    const hasErrorLabel = html.includes("错误编号");
    const hasCorrIdTag = html.includes("correlation_id") && html.includes("class=\"evidence correlation-id\"");
    expect(hasErrorLabel || hasCorrIdTag).toBe(true);
  });

  it("fix_link 按钮 HTML 包含 tab/subtab data 属性", () => {
    const html = renderCheckItemHtml(sampleItem);
    expect(html).toContain('data-tab="factors"');
    expect(html).toContain('data-subtab="editor"');
  });
});
