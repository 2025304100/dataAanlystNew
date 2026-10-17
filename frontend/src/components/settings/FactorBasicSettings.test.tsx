import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { t } from "../../i18n";
import UnsupportedFeaturesToggle from "./UnsupportedFeaturesToggle";

const { getConfig } = vi.hoisted(() => ({ getConfig: vi.fn() }));
vi.mock("../../api/client", () => ({ api: { getFactorBasicConfig: getConfig } }));

/**
 * C2：因子基础配置设置栏（纯渲染测试：挂载渲染 + 空态）。
 */
import FactorBasicSettings from "./FactorBasicSettings";

describe("FactorBasicSettings（C2）", () => {
  beforeEach(() => { window.localStorage.clear(); getConfig.mockReset(); });

  it("默认只展示真实功能状态和仓库信息，隐藏未落库配置", () => {
    render(<FactorBasicSettings config={{ defaultDirection: "long", maxComplexity: 8, warehousePath: "/data/wh", language: "zh" }} />);
    expect(document.querySelector("[data-factor-basic-settings]")).toBeTruthy();
    expect(screen.getByText("/data/wh")).toBeInTheDocument();
    expect(document.querySelector("[data-factor-basic-max-complexity]")).toBeNull();
    expect(document.querySelector("[data-factor-basic-direction]")).toBeNull();
  });

  it("无配置 → 空态占位（不崩页）", async () => {
    getConfig.mockRejectedValueOnce(new Error("isolated offline"));
    render(<FactorBasicSettings />);
    expect(document.querySelector("[data-factor-basic-settings]")).toBeTruthy();
    await waitFor(() => expect(document.querySelector("[data-factor-basic-empty]")).toHaveTextContent("isolated offline"));
  });

  it("共享展示开关只显示禁用的演示配置，不会开启编辑或保存", () => {
    render(<><UnsupportedFeaturesToggle /><FactorBasicSettings config={{ warehousePath: "/data/wh", defaultDirection: "long", maxComplexity: 8 }} /></>);
    fireEvent.click(screen.getByRole("switch", { name: t("settingsShowUnsupported") }));
    expect(document.querySelector("[data-factor-basic-max-complexity]")).toBeDisabled();
    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(getConfig).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("switch", { name: t("settingsShowUnsupported") }));
    expect(document.querySelector("[data-factor-basic-max-complexity]")).toBeNull();
  });
});
