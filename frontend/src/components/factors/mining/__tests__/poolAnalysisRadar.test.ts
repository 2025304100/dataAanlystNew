// 回归：风格雷达在"无可画维度"时必须走空态，而不是把空 indicator 交给 echarts。
//
// 现场成因（拟真实拍复现）：数仓未初始化时 build_analysis 返回的 style_exposure
// 五个维度全部 available:false → availableStyles 为空数组 → 旧代码仍然生成
// "series.data 有 1 项 + radar.indicator 为空"的 option，echarts 内部
// SeriesData.each 抛 TypeError: Cannot read properties of undefined (reading 'push')，
// 用户点开候选池分析看板就是一片空白。修复前用真实数据在浏览器里拍到了崩溃，
// 修复后同一份数据正常渲染（见 docs/拟真走查与全量验证-2026-09-30.md）。
import { describe, expect, it } from "vitest";

import { radarOption } from "../wizard/step1/PoolAnalysisModal";

describe("radarOption", () => {
  it("没有可画维度时不产出 series，交给 MiningChart 的空态占位", () => {
    const option = radarOption([]);
    expect(option.series).toBeUndefined();
    // 也不能留一个空 indicator 给 echarts
    expect(option.radar).toBeUndefined();
  });

  it("有可画维度时 indicator 与数据点数量一致", () => {
    const dims = [
      { label: "成长", score: 0.4 },
      { label: "价值", score: 0.7 },
      { label: "质量", score: 0.2 },
    ];
    const option = radarOption(dims);
    const radar = option.radar as { indicator: unknown[] };
    const series = option.series as Array<{ data: Array<{ value: number[] }> }>;

    expect(radar.indicator).toHaveLength(3);
    expect(series[0].data[0].value).toEqual([0.4, 0.7, 0.2]);
  });

  it("单个维度可画时也不会出现“有数据无坐标轴”的形状", () => {
    const option = radarOption([{ label: "动量", score: 0.5 }]);
    const radar = option.radar as { indicator: unknown[] };
    expect(radar.indicator).toHaveLength(1);
    expect((option.series as Array<{ data: unknown[] }>).length).toBe(1);
  });
});
