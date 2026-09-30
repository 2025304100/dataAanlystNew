// 内部代码 → 用户可读名称 的标签层测试。
//
// 这里刻意**不 mock i18n**：拟真走查暴露的问题就是"界面上出现内部 code"，
// 如果测试把 t() 换成"返回键名"的假实现，就永远验不到"字典里到底有没有文案"
// （返回 `taskTypeApiProbe` 这种键名同样算泄露）。
import { describe, expect, it } from "vitest";

import { KNOWN_TASK_TYPES, taskTypeCodeForTooltip, taskTypeLabel } from "../taskTypeLabel";
import { KNOWN_SCAN_SCOPES, describeScanScope } from "../scanScopeLabel";
import { govStatusLabel, KNOWN_GOV_STATUSES } from "../govStatusLabel";

/** 后端真实会产生 task_type 的取值（与 app/ 里的 task_type= 字面量对齐；
 *  Python 侧守护 test_backend_task_types_have_frontend_labels 负责防漂移）。 */
const BACKEND_TASK_TYPES = [
  "market_data_sync",
  "history_initialization",
  "universe_incremental_sync",
  "universe_sync",
  "universe_backfill",
  "universe_industry_backfill",
  "universe_range_repair",
  "universe_smart_sync",
  "data_mirror",
  "index_daily_sync",
  "index_prices_sync",
  "macro_update",
  "factor_pipeline",
  "factor_mining",
  "factor_mining_validation",
  "discovery_mining",
  "discovery_fast_scan",
  "discovery",
  "discovery_data_prep",
  "financial_report_sync",
  "hot_rank_snapshot",
  "lhb_institution_sync",
  "tail_proxy_snapshot",
  "external_api_probe",
  "external_data_sync",
  "wp5_evaluation",
  "portfolio_auto_trade",
  "portfolio_equity_snapshot",
];

describe("taskTypeLabel", () => {
  it("后端每一种 task_type 都能拿到中文名称，且不等于内部 code", () => {
    for (const taskType of BACKEND_TASK_TYPES) {
      const label = taskTypeLabel(taskType);
      expect(label, `task_type=${taskType} 没有可用文案`).toBeTruthy();
      expect(label).not.toBe(taskType);
      // 键名回显（例如 "taskTypeApiProbe"）也算泄露：说明字典里没这条文案
      expect(label).not.toMatch(/^taskType[A-Z]/);
      // 内部 code 的形态（snake_case / UPPER_SNAKE）不允许出现在正文里
      expect(label).not.toMatch(/^[a-z][a-z0-9_]*_[a-z0-9_]+$/);
    }
  });

  it("接口探测这种新增类型显示为可读名称（拟真走查曾在任务中心看到原值）", () => {
    expect(taskTypeLabel("external_api_probe")).toBe("接口探测");
  });

  it("external_sync_ 动态类型不再把内部后缀拼进文案", () => {
    const label = taskTypeLabel("external_sync_lhb_institution");
    expect(label).not.toContain("lhb_institution");
    expect(label).toBe("外部数据同步");
  });

  it("未知类型显示为通用名称，原 code 只进 tooltip", () => {
    expect(taskTypeLabel("some_brand_new_type")).toBe("其他任务");
    expect(taskTypeLabel("")).toBe("其他任务");
    expect(taskTypeLabel(null)).toBe("其他任务");
    expect(taskTypeCodeForTooltip("some_brand_new_type")).toBe("some_brand_new_type");
    // 命中映射时不需要 tooltip 里的技术细节
    expect(taskTypeCodeForTooltip("external_api_probe")).toBeNull();
  });

  it("映射表覆盖的每一种类型都有对应文案（防新增键忘了配 i18n）", () => {
    for (const taskType of KNOWN_TASK_TYPES) {
      expect(taskTypeLabel(taskType)).not.toBe(taskType);
    }
  });
});

describe("describeScanScope", () => {
  it("扫描记录的 scope 显示为中文覆盖范围", () => {
    expect(describeScanScope("cn_stock")).toBe("A股股票");
    expect(describeScanScope("cn_etf")).toBe("A股ETF");
  });

  it("未收录的取值返回 null（由界面显示占位，不回退成内部 code）", () => {
    expect(describeScanScope("hk_stock")).toBeNull();
    expect(describeScanScope(null)).toBeNull();
    expect(describeScanScope("")).toBeNull();
  });

  it("大小写与空格不影响识别", () => {
    expect(describeScanScope(" CN_STOCK ")).toBe("A股股票");
  });

  it("已收录清单非空且都能拿到文案", () => {
    expect(KNOWN_SCAN_SCOPES.length).toBeGreaterThan(0);
    for (const scope of KNOWN_SCAN_SCOPES) {
      expect(describeScanScope(scope)).toBeTruthy();
    }
  });
});

// 治理阻断状态：拟真走查在组合交易多个子页看到过 RECONCILIATION_BLOCKED 原值，
// 并拼进了提示语「门禁阻断：RECONCILIATION_BLOCKED」。
describe("govStatusLabel", () => {
  it("client.ts 声明的每个枚举值都有可读文案，且不等于原值", () => {
    const declared = ["READY", "DATA_INCOMPLETE_PAUSED", "RECONCILIATION_BLOCKED", "MODEL_INACTIVE", "SCORE_STALE"];
    for (const status of declared) {
      const label = govStatusLabel(status);
      expect(label).toBeTruthy();
      expect(label).not.toBe(status);
      expect(label).not.toMatch(/^[A-Z][A-Z0-9_]+$/);
      expect(label).not.toMatch(/^govStatus[A-Z]/); // 键名回显也算泄露
    }
  });

  it("具体状态说得清发生了什么", () => {
    expect(govStatusLabel("RECONCILIATION_BLOCKED")).toBe("昨日对账存在差异，已被治理保护");
    expect(govStatusLabel("READY")).toBe("就绪（可下单）");
  });

  it("未收录状态不返回枚举原值，只给通用文案；原值单独取用做 tooltip", () => {
    expect(govStatusLabel("SOMETHING_NEW")).toBe("治理状态未识别");
    expect(govStatusLabel(null)).toBe("治理状态未识别");
  });

  it("已收录清单非空且都能拿到文案", () => {
    expect(KNOWN_GOV_STATUSES.length).toBeGreaterThan(0);
    for (const status of KNOWN_GOV_STATUSES) {
      expect(govStatusLabel(status)).toBeTruthy();
    }
  });
});
