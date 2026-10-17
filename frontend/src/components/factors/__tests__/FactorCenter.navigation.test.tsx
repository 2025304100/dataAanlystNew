import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { navigate } from "../../../utils/navigate";

vi.mock("../../../context/AppContext", () => ({ useApp: () => ({}) }));
vi.mock("../../../i18n", () => ({ t: (key: string) => key }));
vi.mock("../FactorLibrary", () => ({ default: () => <div>因子库页面</div> }));
vi.mock("../FactorEvaluationLab", () => ({ default: (props:{initialFactorCode?:string,initialVersionId?:number,onSelectionChange?:(value:{factorCode:string,versionId:number})=>void}) => <><div>评估页面 {props.initialFactorCode} {props.initialVersionId}</div><button onClick={()=>props.onSelectionChange?.({factorCode:"MANUAL",versionId:29})}>手选版本</button></> }));
vi.mock("../FactorShadowLab", () => ({ default: () => null }));
vi.mock("../FactorModelPage", () => ({ default: () => <div>模型训练与激活页面</div> }));
vi.mock("../FactorDetail", () => ({ default: (props:{factorCode:string}) => <div>因子详情 {props.factorCode}</div> }));
vi.mock("../FactorEditor", () => ({ default: () => null }));
vi.mock("../FactorDraftConfirmModal", () => ({ default: (props:{open:boolean,auditId?:number}) => props.open ? <div>草案确认 {props.auditId}</div> : null }));

import FactorCenter from "../FactorCenter";

beforeEach(() => window.localStorage.clear());
afterEach(() => window.localStorage.clear());

describe("因子流水线到模型页的导航", () => {
  it("点击前往因子中心后打开模型页，可继续训练或激活", () => {
    render(<><button onClick={() => navigate("/factors?next=pipeline")}>前往因子中心</button><FactorCenter /></>);
    expect(screen.getByText("因子库页面")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "前往因子中心" }));

    expect(screen.getByText("模型训练与激活页面")).toBeVisible();
    expect(screen.queryByText("因子库页面")).not.toBeInTheDocument();
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

  it("只挂载当前工作区，评估跳转保留所选因子和版本", () => {
    render(<FactorCenter/>);
    expect(screen.queryByText(/评估页面/)).not.toBeInTheDocument();
    act(()=>window.dispatchEvent(new CustomEvent("factor-center:navigate",{detail:{target:"evaluation",factor_code:"factor_A",version_id:27}})));
    expect(screen.getByText("评估页面 factor_A 27")).toBeVisible();
    expect(screen.queryByText("因子库页面")).not.toBeInTheDocument();
  });

  it("兼容已入库草稿的旧跳转事件并打开正确详情",async()=>{
    render(<FactorCenter/>);
    act(()=>window.dispatchEvent(new CustomEvent("factor-center:navigate",{detail:{tab:"library",code:"approved_A"}})));
    await waitFor(()=>expect(screen.getByText("因子详情 approved_A")).toBeVisible());
  });

  it("手选因子和版本在切换工作区后保留",()=>{
    render(<FactorCenter/>);
    fireEvent.click(screen.getByRole("button",{name:/factorCenterTabEvaluation/}));
    fireEvent.click(screen.getByRole("button",{name:"手选版本"}));
    fireEvent.click(screen.getByRole("button",{name:/factorCenterTabModel/}));
    fireEvent.click(screen.getByRole("button",{name:/factorCenterTabEvaluation/}));
    expect(screen.getByText("评估页面 MANUAL 29")).toBeVisible();
  });

  it("助手草案事件保留审计ID，进入对应确认流程",()=>{
    render(<FactorCenter/>);
    act(()=>window.dispatchEvent(new CustomEvent("open-factor-draft",{detail:{auditId:37}})));
    expect(screen.getByText("草案确认 37")).toBeInTheDocument();
  });
});
