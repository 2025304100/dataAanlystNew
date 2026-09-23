import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";

/**
 * C2：因子基础配置设置栏（纯渲染测试：挂载渲染 + 空态）。
 */
import FactorBasicSettings from "./FactorBasicSettings";

describe("FactorBasicSettings（C2）", () => {
  it("挂载渲染（信息区 + 表单控件）", () => {
    render(<FactorBasicSettings config={{ defaultDirection: "long", maxComplexity: 8, warehousePath: "/data/wh", language: "zh" }} />);
    expect(document.querySelector("[data-factor-basic-settings]")).toBeTruthy();
    expect(document.querySelector("[data-factor-basic-max-complexity]")).toBeTruthy();
    expect(document.querySelector("[data-factor-basic-direction]")).toBeTruthy();
  });

  it("无配置 → 空态占位（不崩页）", () => {
    render(<FactorBasicSettings />);
    expect(document.querySelector("[data-factor-basic-settings]")).toBeTruthy();
    expect(document.querySelector("[data-factor-basic-empty]")).toBeTruthy();
  });
});