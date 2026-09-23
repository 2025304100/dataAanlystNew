import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 字段校验 API 契约测试（T30 补测，2026-09-21）。
 *
 * 为什么必须有这个文件：`MiningFieldStep.test.tsx` 把整个 `./fieldApi` mock 掉了，
 * 于是"前端发的 payload 与后端契约是否一致"**没有任何测试覆盖**。真实缺陷正是
 * 从这里漏过去的：前端发 `{draft_id: null, selected_fields: [...]}`，
 * 后端 `ValidationCreate` 要 `{draft_id(必填), config, fields}` → 422 → 按钮点了没反应。
 *
 * 本文件只 mock `api/client`（网络边界），**保留 fieldApi 的真实实现**，
 * 用来钉住两件事：
 *   1. 请求体字段名与后端 `ValidationCreate` 一致；
 *   2. 后端原始响应能被归一化成前端 `ValidationStatus`（含 pass/warn/block 三态）。
 */
const { requestJson } = vi.hoisted(() => ({ requestJson: vi.fn() }));

vi.mock("../../../../../api/client", () => ({ requestJson }));

import { createValidation, getValidation, normalizeValidation } from "./fieldApi";
import type { RawValidationProgress } from "./fieldApi";

/** 取最近一次 requestJson 调用的 body（JSON 解析） */
function lastBody(): Record<string, unknown> {
  const calls = requestJson.mock.calls;
  const init = calls[calls.length - 1]?.[1] as { body?: string } | undefined;
  return JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("createValidation 请求体对齐后端 ValidationCreate", () => {
  it("发送 draft_id / config / fields（不是 selected_fields / config_hash）", async () => {
    requestJson.mockResolvedValue({
      task_id: "val-1", reused: false, config_hash: "h1",
      total_shards: 1, status: "queued",
    });
    await createValidation({ selected_fields: ["close", "pe_ttm"] });

    const [url, init] = requestJson.mock.calls[0] as [string, { method?: string }];
    expect(url).toBe("/api/v1/factor-mining/validations");
    expect(init?.method).toBe("POST");

    const body = lastBody();
    expect(body.fields).toEqual(["close", "pe_ttm"]);
    expect(body.config).toEqual({ selected_fields: ["close", "pe_ttm"] });
    // 后端 draft_id 必填且 min_length=1 —— 绝不能是 null / 空串
    expect(typeof body.draft_id).toBe("string");
    expect(String(body.draft_id).length).toBeGreaterThan(0);
    // 旧字段名一旦回归，后端会直接 422
    expect(body).not.toHaveProperty("selected_fields");
    expect(body).not.toHaveProperty("config_hash");
  });

  it("draft_id 在同一浏览器内稳定（后端按 draft_id+config_hash 幂等复用任务）", async () => {
    requestJson.mockResolvedValue({ task_id: "val-1", status: "queued" });
    await createValidation({ selected_fields: ["close"] });
    const first = lastBody().draft_id;
    await createValidation({ selected_fields: ["close"] });
    const second = lastBody().draft_id;
    expect(second).toBe(first);
    expect(window.localStorage.getItem("mining_wizard_draft_id")).toBe(String(first));
  });

  it("显式传入 draft_id 时优先使用（便于草稿化场景）", async () => {
    requestJson.mockResolvedValue({ task_id: "val-2", status: "queued" });
    await createValidation({ selected_fields: ["close"], draft_id: "draft-x" });
    expect(lastBody().draft_id).toBe("draft-x");
  });
});

describe("normalizeValidation 三态映射（向导 §5.1）", () => {
  const base: RawValidationProgress = {
    found: true,
    task_id: "val-1",
    status: "done",
    completed_shards: 3,
    total_shards: 3,
    report: { verdict: "pass", verdict_label_zh: "通过", summary_zh: "全部通过" },
  };

  it("done + pass → passed 且进度按分片归一", () => {
    const s = normalizeValidation(base);
    expect(s.status).toBe("passed");
    expect(s.passed).toBe(true);
    expect(s.progress).toEqual({ done: 3, total: 3 });
    expect(s.verdict_label_zh).toBe("通过");
  });

  it("done + block → blocked，且阻断项带字段/原因/当前值/要求值", () => {
    const s = normalizeValidation({
      ...base,
      report: {
        verdict: "block",
        verdict_label_zh: "阻断",
        shards: [
          {
            field: "roe_ttm",
            status: "done",
            result: { verdict: "block", reason_zh: "覆盖率不足", coverage: 0.612 },
          },
          { field: "close", status: "done", result: { verdict: "pass", coverage: 0.99 } },
        ],
      },
    });
    expect(s.status).toBe("blocked");
    expect(s.passed).toBe(false);
    expect(s.blocked).toHaveLength(1);
    expect(s.blocked[0].field).toBe("roe_ttm");
    expect(s.blocked[0].issue).toBe("覆盖率不足");
    expect(s.blocked[0].current).toBe(0.612);
    expect(s.blocked[0].required).toBe(0.8);
  });

  it("done + warn → 终态 passed 但 passed=false，并把告警逐字段列出", () => {
    const s = normalizeValidation({
      ...base,
      report: {
        verdict: "warn",
        verdict_label_zh: "警告",
        summary_zh: "1 个字段覆盖率偏低",
        shards: [
          { field: "pe_ttm", status: "done", result: { verdict: "warn", reason_zh: "覆盖率 0.75" } },
        ],
      },
    });
    expect(s.status).toBe("passed");
    expect(s.passed).toBe(false);
    expect(s.warnings.join("|")).toContain("pe_ttm");
    expect(s.warnings[0]).toBe("1 个字段覆盖率偏低");
    expect(s.verdict_label_zh).toBe("警告");
  });

  it("分片 failed 视同阻断（如实反映失败字段，不静默放过）", () => {
    const s = normalizeValidation({
      ...base,
      report: {
        verdict: "pass",
        failed_shards: ["close"],
        shards: [{ field: "close", status: "failed", error: "DuckDB read timeout" }],
      },
    });
    expect(s.status).toBe("blocked");
    expect(s.blocked[0].issue).toBe("DuckDB read timeout");
  });

  it("running / failed 原样映射（轮询终态判定依赖它）", () => {
    expect(normalizeValidation({ ...base, status: "running", report: null }).status).toBe("running");
    expect(normalizeValidation({ ...base, status: "failed", report: null }).status).toBe("failed");
  });

  it("把分片实测元数据提取为 field_meta（Step3 覆盖率/最新日期/可用区间的唯一来源）", () => {
    const s = normalizeValidation({
      ...base,
      report: {
        verdict: "pass",
        shards: [
          {
            field: "close",
            status: "done",
            result: {
              verdict: "pass",
              coverage: 0.999,
              min_date: "2021-01-04",
              max_date: "2026-08-21",
              total_rows: 11540000,
              non_null_rows: 11533460,
            },
          },
          { field: "pe_ttm", status: "done", result: { verdict: "warn", coverage: 0.75 } },
        ],
      },
    });
    expect(s.field_meta?.close).toEqual({
      coverage: 0.999,
      min_date: "2021-01-04",
      max_date: "2026-08-21",
      total_rows: 11540000,
      non_null_rows: 11533460,
      verdict: "pass",
    });
    expect(s.field_meta?.pe_ttm.coverage).toBe(0.75);
    expect(s.field_meta?.pe_ttm.min_date).toBeNull();
  });
});

describe("getValidation 走真实实现", () => {
  it("把后端原始响应归一化后再返回", async () => {
    requestJson.mockResolvedValue({
      found: true,
      task_id: "val-9",
      status: "done",
      completed_shards: 2,
      total_shards: 2,
      valid_until: "2026-09-22T07:00:00",
      report: {
        verdict: "pass",
        verdict_label_zh: "通过",
        summary_zh: "全部通过",
        shards: [{ field: "close", status: "done", result: { verdict: "pass", coverage: 0.99 } }],
      },
    } satisfies RawValidationProgress);

    const s = await getValidation("val-9");
    expect(requestJson).toHaveBeenCalledWith("/api/v1/factor-mining/validations/val-9");
    expect(s.status).toBe("passed");
    expect(s.passed).toBe(true);
    expect(s.valid_until).toBe("2026-09-22T07:00:00");
  });
});
