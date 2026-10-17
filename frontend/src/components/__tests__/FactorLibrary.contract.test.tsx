import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const { api, notify } = vi.hoisted(() => ({ api: { scoringListFactorDefinitions: vi.fn(), scoringGetFactorUsage: vi.fn(), scoringListFactorDrafts: vi.fn(), scoringApproveFactorDraft: vi.fn(), scoringRejectFactorDraft: vi.fn() }, notify: {error:vi.fn(),success:vi.fn(),warning:vi.fn()} }));
vi.mock("../../api/client", () => ({api}));
vi.mock("../../context/AppContext", () => ({useApp:()=>({locale:"zh-CN"})}));
vi.mock("antd", async () => { const actual=await vi.importActual<typeof import("antd")>("antd"); return {...actual, App:{...actual.App,useApp:()=>({message:notify})}}; });
vi.mock("../../i18n", () => ({t:(key:string)=>key,template:(key:string)=>key,factorLabel:(_:string,name:string)=>name,factorCategoryLabel:(v:string)=>v,factorDirectionLabel:(v:string)=>v}));
import FactorLibrary from "../factors/FactorLibrary";
const row = (code:string) => ({id:1,code,name:code,lifecycle_status:"active",active_version_id:104,shadow_version_id:null,latest_version_id:104,latest_version_no:3,latest_evaluation:null,updated_at:null});
const props = {onViewDetail:vi.fn(),onEditFactor:vi.fn(),onNewFactor:vi.fn()};
beforeEach(()=>{
  vi.clearAllMocks();
  api.scoringListFactorDefinitions.mockResolvedValue({items:[row("CURRENT")],total:1});
  api.scoringGetFactorUsage.mockImplementation(async(code:string)=>({code,active_version:3,in_factor_sets:[],in_models:[],coverage_30d:null,ic_mean_30d:null,days_in_production:null}));
  api.scoringListFactorDrafts.mockResolvedValue([]);
});
afterEach(cleanup);
it("opens real advanced filters instead of leaving More filters inert",async()=>{
  const user=userEvent.setup(); render(<FactorLibrary {...props}/>);
  await user.click(await screen.findByRole("button",{name:/factorLibraryMoreFilters/}));
  expect(screen.getByRole("combobox",{name:"factorCenterLibraryOriginFilter"})).toBeInTheDocument();
});

it("shows version numbers and requires real evaluation evidence instead of inferring a pass from active status",async()=>{
 render(<FactorLibrary {...props}/>);
 expect(await screen.findByText("v3")).toBeInTheDocument();
 expect(screen.queryByText("v104")).not.toBeInTheDocument();
 expect(screen.queryByText("通过")).not.toBeInTheDocument();
 expect(screen.getByText("factorCenterLibraryNoEvaluation")).toBeInTheDocument();
});

it("does not let a late old search replace current results or rescan all statistics on search",async()=>{
 let finishOld!:(v:unknown)=>void;
 api.scoringListFactorDefinitions.mockImplementation((query:{search?:string,page_size:number})=>{
  if(query.search==="old") return new Promise(resolve=>{finishOld=resolve;});
  return Promise.resolve({items:[row(query.search==="new"?"NEW":"CURRENT")],total:1});
 });
 render(<FactorLibrary {...props}/>);
 await screen.findAllByText("CURRENT");
 const input=screen.getByPlaceholderText("factorLibrarySearchPlaceholder");
 fireEvent.change(input,{target:{value:"old"}});fireEvent.keyDown(input,{key:"Enter",code:"Enter"});
 await waitFor(()=>expect(finishOld).toBeTypeOf("function"));
 fireEvent.change(input,{target:{value:"new"}});fireEvent.keyDown(input,{key:"Enter",code:"Enter"});
 await screen.findAllByText("NEW");
 await act(async()=>{finishOld({items:[row("OLD")],total:1});});
 expect(screen.queryByText("OLD")).not.toBeInTheDocument();
 expect(screen.getAllByText("NEW").length).toBeGreaterThan(0);
 expect(api.scoringListFactorDefinitions.mock.calls.filter(([query])=>query.page_size===100)).toHaveLength(1);
});

it("opens prefetched usage without a duplicate request or polling a stale cache",async()=>{
 let finish!:(v:unknown)=>void;
 api.scoringGetFactorUsage.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
 render(<FactorLibrary {...props}/>);
 await waitFor(()=>expect(api.scoringGetFactorUsage).toHaveBeenCalledTimes(1));
 fireEvent.click(screen.getByTestId("btn-usage-CURRENT"));
 await act(async()=>finish({code:"CURRENT",name:"CURRENT",in_factor_sets:[],in_models:[],coverage_30d:null,ic_mean_30d:null}));
 expect(await screen.findByTestId("usage-sub-tabs-wrapper")).toBeInTheDocument();
 expect(api.scoringGetFactorUsage).toHaveBeenCalledTimes(1);
});

it("does not report draft approval when the source changed and the server leaves it submitted",async()=>{
 api.scoringListFactorDrafts.mockResolvedValue([{draft_no:"DRAFT",suggested_code:"SOURCE",source_module:"custom_indicators",review_status:"submitted"}]);
 api.scoringApproveFactorDraft.mockResolvedValue({review_status:"submitted",review_msg:"upstream_indicator_changed"});
 render(<FactorLibrary {...props}/>);
 fireEvent.click(await screen.findByRole("tab",{name:/草稿审批/}));
 fireEvent.click(await screen.findByRole("button",{name:/通过/}));
 await waitFor(()=>expect(notify.warning).toHaveBeenCalledWith("factorCenterLibraryUpstreamChanged"));
 expect(notify.success).not.toHaveBeenCalled();
});

it("requires a rejection reason before submitting a draft review",async()=>{
 api.scoringListFactorDrafts.mockResolvedValue([{draft_no:"DRAFT",suggested_code:"SOURCE",source_module:"custom_indicators",review_status:"submitted"}]);
 render(<FactorLibrary {...props}/>);
 fireEvent.click(await screen.findByRole("tab",{name:/草稿审批/}));
 fireEvent.click(await screen.findByRole("button",{name:/驳回/}));
 expect(screen.getByRole("button",{name:"确认驳回"})).toBeDisabled();
 expect(api.scoringRejectFactorDraft).not.toHaveBeenCalled();
});

it("formats draft dates and labels known sources and stored factor codes honestly",async()=>{
 api.scoringListFactorDrafts.mockResolvedValue([{draft_no:"DATED",suggested_code:"SOURCE",source_module:"custom_indicators",review_status:"applied",
   submitted_at:"2026-10-15T08:09:10",applied_at:"2026-10-15T09:10:11",promoted_factor_id:12,promoted_factor_version:1,promoted_factor_code:"stored_source"}]);
 render(<FactorLibrary {...props}/>);
 fireEvent.click(await screen.findByRole("tab",{name:/草稿审批/}));
 expect(await screen.findByText("factorLabDraftSourceCustomIndicators")).toBeInTheDocument();
 expect(screen.getByText("10-15 08:09")).toBeInTheDocument();
 expect(screen.getByText("10-15 09:10")).toBeInTheDocument();
 expect(screen.getByText("factorLabDraftStoredCode")).toBeInTheDocument();
 expect(screen.queryByText("custom_indicators")).not.toBeInTheDocument();
 expect(screen.queryByText(/2026-10-15T|生产：|正式 code/)).not.toBeInTheDocument();
});

it("clearing filters during the search debounce restores the list instead of leaving an invalidated request spinning",async()=>{
 render(<FactorLibrary {...props}/>);
 await screen.findAllByText("CURRENT");
 fireEvent.change(screen.getByPlaceholderText("factorLibrarySearchPlaceholder"),{target:{value:"pending"}});
 fireEvent.click(screen.getByRole("button",{name:"factorCenterLibraryResetFilters"}));
 expect((await screen.findAllByText("CURRENT")).length).toBeGreaterThan(0);
});

it("explains an old running service instead of calling absent version evidence not evaluated",async()=>{
 const old={id:1,code:"OLD_SERVICE",name:"Old service",lifecycle_status:"active"};
 api.scoringListFactorDefinitions.mockResolvedValue({items:[old],total:1});
 render(<FactorLibrary {...props}/>);
 expect(await screen.findByText("factorCenterLibraryServicePendingDesc")).toBeInTheDocument();
 expect(screen.queryByText("factorCenterLibraryNoEvaluation")).not.toBeInTheDocument();
 fireEvent.click(screen.getByTestId("btn-usage-OLD_SERVICE"));
 expect(api.scoringGetFactorUsage).not.toHaveBeenCalled();
});
