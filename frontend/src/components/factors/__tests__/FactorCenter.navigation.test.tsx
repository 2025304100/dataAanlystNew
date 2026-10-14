import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { navigate } from "../../../utils/navigate";

vi.mock("../../../context/AppContext", () => ({ useApp: () => ({}) }));
vi.mock("../../../i18n", () => ({ t: (key: string) => key }));
vi.mock("../FactorLibrary", () => ({ default: () => <div>因子库页面</div> }));
vi.mock("../FactorEvaluationLab", () => ({ default: () => null }));
vi.mock("../FactorShadowLab", () => ({ default: () => null }));
vi.mock("../FactorModelPage", () => ({ default: () => <div>模型训练与激活页面</div> }));
vi.mock("../FactorDetail", () => ({ default: () => null }));
vi.mock("../FactorEditor", () => ({ default: () => null }));
vi.mock("../FactorDraftConfirmModal", () => ({ default: () => null }));

import FactorCenter from "../FactorCenter";

beforeEach(() => window.localStorage.clear());
afterEach(() => window.localStorage.clear());

describe("因子流水线到模型页的导航", () => {
  it("点击前往因子中心后打开模型页，可继续训练或激活", () => {
    render(<><button onClick={() => navigate("/factors?next=pipeline")}>前往因子中心</button><FactorCenter /></>);
    expect(screen.getByText("因子库页面")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "前往因子中心" }));

    expect(screen.getByText("模型训练与激活页面")).toBeVisible();
    expect(screen.getByText("因子库页面")).not.toBeVisible();
  });

  it("因子中心尚未挂载时，跳转信息仍能在挂载后恢复", () => {
    act(() => navigate("/factors?set_id=frozen-1&next=pipeline"));
    render(<FactorCenter />);
    expect(screen.getByText("模型训练与激活页面")).toBeVisible();
    expect(window.localStorage.getItem("settings_active_section")).toBe("factor-center");
    expect(window.location.hash).toContain("set_id=frozen-1");
  });

  it("因子库的模型反查链接留在应用内，并进入模型页", () => {
    render(<><button onClick={() => navigate("/settings/factors?model_id=trained-1")}>查看关联模型</button><FactorCenter /></>);
    fireEvent.click(screen.getByRole("button", { name: "查看关联模型" }));
    expect(screen.getByText("模型训练与激活页面")).toBeVisible();
    expect(window.location.hash).toContain("model_id=trained-1");
  });
});
