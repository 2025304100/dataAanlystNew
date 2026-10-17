import {act,cleanup,fireEvent,render,screen,waitFor} from "@testing-library/react";
import {afterEach,beforeEach,expect,it,vi} from "vitest";
const {api,notify}=vi.hoisted(()=>({api:{scoringGetFactorDefinition:vi.fn(),listFactorVersions:vi.fn(),getFactorTransitionHistory:vi.fn(),getFactorReferences:vi.fn(),executeFactorTransition:vi.fn()},notify:{error:vi.fn(),success:vi.fn()}}));
vi.mock("../../api/client",()=>({api}));
vi.mock("../../context/AppContext",()=>({useApp:()=>({locale:"zh-CN"})}));
vi.mock("../../i18n",()=>({t:(key:string)=>key,factorLabel:(_:string,name:string)=>name,factorCategoryLabel:(v:string)=>v,factorDirectionLabel:(v:string)=>v}));
vi.mock("antd",async()=>{const actual=await vi.importActual<typeof import("antd")>("antd");return {...actual,App:{...actual.App,useApp:()=>({message:notify})}};});
import FactorDetail from "../factors/FactorDetail";
const makeFactor=(code:string)=>({code,name:code,lifecycle_status:"draft",latest_version_id:37,latest_version_no:3});
const props={onOpenEditor:vi.fn(),onBack:vi.fn(),onChanged:vi.fn()};
beforeEach(()=>{
 vi.clearAllMocks();
 api.scoringGetFactorDefinition.mockImplementation(async(code:string)=>makeFactor(code));
 api.listFactorVersions.mockImplementation(async(code:string)=>[{id:37,factor_code:code,version:3,formula_expr:"close",validation_status:"valid",is_latest:true}]);
 api.getFactorTransitionHistory.mockResolvedValue([]);
 api.getFactorReferences.mockResolvedValue({factor_version:3,is_immutable:false,referenced_by_evaluations:0,referenced_by_factor_sets:0,referenced_by_model_runs:0});
});
afterEach(cleanup);
it("loads real compilation status and reference counts for the latest draft version",async()=>{
 render(<FactorDetail factorCode="FACTOR" {...props}/>);
 await waitFor(()=>expect(api.getFactorReferences).toHaveBeenCalledWith("FACTOR",37));
 expect(screen.getByText("factorValidation_valid")).toBeInTheDocument();
});
it("a late detail response cannot replace a newly selected factor",async()=>{
 let finish!:(v:unknown)=>void;
 api.scoringGetFactorDefinition.mockImplementation((code:string)=>code==="OLD"?new Promise(resolve=>{finish=resolve;}):Promise.resolve(makeFactor(code)));
 const view=render(<FactorDetail factorCode="OLD" {...props}/>);
 view.rerender(<FactorDetail factorCode="NEW" {...props}/>);
 await screen.findAllByText("NEW");
 await act(async()=>finish(makeFactor("OLD")));
 expect(screen.queryByText("OLD")).not.toBeInTheDocument();
 expect(screen.getAllByText("NEW").length).toBeGreaterThan(0);
});
it("keeps a failed lifecycle transition open with the reason available for correction",async()=>{
 api.executeFactorTransition.mockResolvedValue({success:false,error:"reference_blocked"});
 render(<FactorDetail factorCode="FACTOR" {...props}/>);
 fireEvent.click(await screen.findByRole("button",{name:"factorActionSubmitCandidate"}));
 fireEvent.change(screen.getByPlaceholderText("factorTransitionReasonPlaceholder"),{target:{value:"checked"}});
 fireEvent.click(screen.getByRole("button",{name:"factorTransitionOk"}));
 await waitFor(()=>expect(notify.error).toHaveBeenCalledWith("reference_blocked"));
 expect(screen.getByPlaceholderText("factorTransitionReasonPlaceholder")).toHaveValue("checked");
 expect(props.onChanged).not.toHaveBeenCalled();
});
