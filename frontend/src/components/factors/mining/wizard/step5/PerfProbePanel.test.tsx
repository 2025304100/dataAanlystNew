import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";

/**
 * T32 补充：Step5 性能探针面板（§8.3.6，调试态）。
 *
 * 覆盖：
 *   1. 无数据 → 不渲染；
 *   2. 六阶段耗时条 + 子表达式总数/唯一数 + G2 命中率摘要；
 *   3. 多代 → G2 命中率折线出现；
 *   4. 每代缓存校验状态：✅（passed=1）/ ❌（passed=0 代次标红 + 警示行）。
 */
import PerfProbePanel from "./PerfProbePanel";
import type { PerfProbeGeneration } from "./runTypes";

const GENS: PerfProbeGeneration[] = [
  {
    generation: 0,
    probe_data_load_ms: 10, probe_ast_eval_ms: 5, probe_subexpr_compute_ms: 40,
    probe_factor_assemble_ms: 12, probe_metric_calc_ms: 22, probe_db_write_ms: 8,
    probe_subexpr_total: 120, probe_subexpr_unique: 84, probe_g2_hit_rate: 0.61,
    cache_validation_passed: 1, cache_validation_max_diff: 0,
  },
  {
    generation: 1,
    probe_data_load_ms: 9, probe_ast_eval_ms: 6, probe_subexpr_compute_ms: 55,
    probe_factor_assemble_ms: 14, probe_metric_calc_ms: 26, probe_db_write_ms: 7,
    probe_subexpr_total: 152, probe_subexpr_unique: 91, probe_g2_hit_rate: 0.78,
    cache_validation_passed: 0, cache_validation_max_diff: 1.2e-9,
  },
];

describe("PerfProbePanel（§8.3.6）", () => {
  it("无代际数据时不渲染", () => {
    render(<PerfProbePanel generations={[]} />);
    expect(document.querySelector("[data-perf-panel]")).toBeNull();
  });

  it("渲染六阶段耗时条 + 子表达式统计 + G2 命中率摘要", () => {
    render(<PerfProbePanel generations={GENS} />);
    expect(document.querySelector("[data-perf-panel]")).toBeTruthy();
    // 六阶段耗时段齐全
    for (const key of [
      "probe_data_load_ms", "probe_ast_eval_ms", "probe_subexpr_compute_ms",
      "probe_factor_assemble_ms", "probe_metric_calc_ms", "probe_db_write_ms",
    ]) {
      expect(document.querySelector(`[data-perf-stage="${key}"]`)).toBeTruthy();
    }
    // 最近一代子表达式 总数/唯一 + G2
    expect(document.querySelector("[data-perf-subexpr-total]")?.textContent).toContain("152");
    expect(document.querySelector("[data-perf-subexpr-unique]")?.textContent).toContain("91");
    expect(document.querySelector("[data-perf-g2]")?.textContent).toContain("78.0");
  });

  it("多代渲染 G2 命中率曲线（SVG polyline）", () => {
    render(<PerfProbePanel generations={GENS} />);
    expect(document.querySelector("[data-perf-curve]")).toBeTruthy();
    expect(document.querySelector("[data-perf-curve] polyline")).toBeTruthy();
  });

  it("每代缓存校验状态：passed=1 ✅ / passed=0 该行标红 + ⚠ 警示", () => {
    render(<PerfProbePanel generations={GENS} />);
    expect(document.querySelector('[data-cache-ok="true"]')).toBeTruthy();
    expect(document.querySelector('[data-cache-ok="false"]')).toBeTruthy();
    expect(document.querySelector('[data-perf-row="1"]')?.className).toContain("is-cache-fail");
    // 警示文案（存在未通过代次）
    const warn = document.querySelector("[data-perf-cache-warn]");
    expect(warn).toBeTruthy();
  });
});