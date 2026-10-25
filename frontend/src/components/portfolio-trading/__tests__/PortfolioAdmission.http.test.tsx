import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AppContext, type AppContextValue } from "../../../context/AppContext";
import PortfolioTradingShell from "../PortfolioTradingShell";
import { setLocale } from "../../../i18n";

const portfolios=[1,2].map(id=>({id,name:`组合${id===1?"甲":"乙"}`,account_type:"simulated",asset_scope:"stock",
  auto_trade_enabled:1,portfolio_status:"READY",created_at:"2026-10-20T08:00:00Z"}));
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{"Content-Type":"application/json"}});
function runtime(id:number,allowed=false,phase="BLOCKED") {
  return {portfolio_id:id,current_state:"READY",generated_at:new Date().toISOString(),dimensions:{},
    permissions:{allow_new_buys:allowed,allow_auto_recovery:allowed,allow_risk_exits:true,requires_manual_ack:false},
    capability_reasons:{allow_new_buys:[allowed?"当日数据与绑定通过":"评分落后两个交易日"]},
    recovery_summary:{can_confirm:phase==="REVIEW_REQUIRED",review_required:false,phase,
      blockers:allowed?[]:["评分落后两个交易日"],checks:[{key:"data",passed:allowed,reason:"真实检查结果"}]}};
}
let readRuntime:(id:number)=>Promise<Response>;
let calls:Array<{url:string;method:string}>;
beforeEach(()=>{
  sessionStorage.clear();localStorage.clear();setLocale("zh-CN");calls=[];
  readRuntime=async id=>json(runtime(id,id===2));
  vi.stubGlobal("fetch",vi.fn(async(input,init)=>{
    const url=String(input);calls.push({url,method:init?.method??"GET"});
    const id=Number(url.match(/portfolios\/(\d+)/)?.[1]??1);
    if(url.includes("/inspection/runtime"))return readRuntime(id);
    if(url.includes("/inspection-acceptance/status"))return json({mode:"application",read_only:false});
    if(url.endsWith("/status"))return json({portfolio_id:id,current_state:"READY",allowed_transitions:[]});
    if(url.includes("/action-items"))return json({portfolio_id:id,items:[],summary:{ready:true,blockers:0},checks:{enabled:true,account_ready:true,data_ready:true,source_ready:true,schedule_ready:true}});
    if(url.includes("/inspection/reconciliation-job"))return json({portfolio_id:id,enabled:false,queue:{portfolio_id:id,days:[],limitations:[]}});
    if(url.includes("/inspection/diagnostics"))return json({portfolio_id:id,symbols:[],evidence:[],repair_plan:{operations:[],executable_count:0,disabled_count:0}});
    if(url.includes("/reconciliation/preview"))return json({portfolio_id:id,trade_date:"2026-10-22",items:[],zero_sum_check_passed:true});
    if(url.includes("/model-binding-review"))return json({portfolio_id:id,can_confirm:false,requires_confirmation:false,blockers:[]});
    if(url.includes("/performance"))return json({stats:{total_return_pct:0}});
    if(url.includes("/g7/"))return json({operational_blockers:[],resume_blockers:[],pending_orders_by_status:{}});
    return json([]);
  }));
});
afterEach(()=>{cleanup();vi.useRealTimers();vi.restoreAllMocks();vi.unstubAllGlobals();sessionStorage.clear();localStorage.clear();});
function Page() {
  const [id,setId]=useState(1);
  const value={portfolios,portfolioId:id,activeSubTab:"governance",switchPortfolio:async(next:number)=>setId(next),
    setActiveSubTab:()=>{},loadWorkbench:async()=>{},showToast:()=>{},deletePortfolio:async()=>false} as unknown as AppContextValue;
  return <AppContext.Provider value={value}><PortfolioTradingShell/></AppContext.Provider>;
}
async function openList(){fireEvent.click(screen.getByRole("button",{name:/^组合甲 模拟/}));await screen.findByRole("listbox");}

it("stale Score overrides persisted READY consistently in header, list and inspection using GET only",async()=>{
  render(<Page/>);
  await screen.findByRole("heading",{name:"新买入已暂停",level:1});await openList();
  await waitFor(()=>expect(screen.getAllByTestId("portfolio-admission-1")).toHaveLength(2));
  screen.getAllByTestId("portfolio-admission-1").forEach(badge=>expect(badge).toHaveTextContent("新买入已暂停"));
  expect(await screen.findByTestId("portfolio-admission-2")).toHaveTextContent("当前运行条件已满足");
  expect(screen.queryByText("可以运行")).not.toBeInTheDocument();
  expect(screen.getByRole("button",{name:"添加候选标的"})).toBeDisabled();
  expect(screen.getByLabelText("风险退出权限")).toHaveTextContent("允许");
  expect(screen.getByLabelText("新买入权限")).toHaveTextContent("不允许");
  expect(calls.every(call=>call.method==="GET")).toBe(true);
});

it("healthy runtime permits the same display in all three places, and shares the selected portfolio read",async()=>{
  readRuntime=async id=>json(runtime(id,true));render(<Page/>);
  await screen.findByRole("heading",{name:"当前运行条件已满足",level:1});await openList();
  screen.getAllByTestId("portfolio-admission-1").forEach(badge=>expect(badge).toHaveTextContent("当前运行条件已满足"));
  expect(calls.filter(call=>call.url.endsWith("/portfolios/1/inspection/runtime"))).toHaveLength(1);
});

it("HTTP failure never falls back to green READY, and a fresh read recovers the real blocked state",async()=>{
  readRuntime=async()=>json({detail:"unavailable"},503);render(<Page/>);
  await screen.findByRole("heading",{name:"当前交易状态待核实",level:1});await openList();
  screen.getAllByTestId("portfolio-admission-1").forEach(badge=>expect(badge).toHaveTextContent("当前交易状态待核实"));
  readRuntime=async id=>json(runtime(id));fireEvent(window,new Event("focus"));
  await screen.findByRole("heading",{name:"新买入已暂停",level:1});
  screen.getAllByTestId("portfolio-admission-1").forEach(badge=>expect(badge).toHaveTextContent("新买入已暂停"));
});

it.each(["wrong-owner","incomplete","unknown-permission"])("%s runtime remains unknown in every display",async mode=>{
  readRuntime=async id=>json(mode==="wrong-owner"?runtime(id+100,true):mode==="incomplete"?{portfolio_id:id,current_state:"READY"}:
    {...runtime(id,true),permissions:{...runtime(id,true).permissions,allow_new_buys:null}});
  render(<Page/>);await screen.findByRole("heading",{name:"当前交易状态待核实",level:1});await openList();
  screen.getAllByTestId("portfolio-admission-1").forEach(badge=>expect(badge).toHaveTextContent("当前交易状态待核实"));
  expect(screen.queryByText("可以运行")).not.toBeInTheDocument();
});

it("switching portfolios discards a late healthy response for the previous portfolio",async()=>{
  let resolve!:(value:Response)=>void;const pending=new Promise<Response>(done=>{resolve=done;});
  readRuntime=id=>id===1?pending:Promise.resolve(json(runtime(2)));
  render(<Page/>);await openList();
  const option=await screen.findByRole("option",{name:/组合乙/});fireEvent.click(option);
  await waitFor(()=>expect(screen.getByRole("button",{name:/^组合乙 模拟/})).toHaveTextContent("新买入已暂停"));
  await act(async()=>resolve(json(runtime(1,true))));
  expect(screen.getByRole("button",{name:/^组合乙 模拟/})).toHaveTextContent("新买入已暂停");
});

it("inspection refresh updates header and the already-open list without a write or a reload",async()=>{
  readRuntime=async id=>json(runtime(id,true));render(<Page/>);
  await screen.findByRole("heading",{name:"当前运行条件已满足",level:1});await openList();
  readRuntime=async id=>json(runtime(id));
  fireEvent.click(screen.getByRole("button",{name:"重新检测"}));
  await screen.findByRole("heading",{name:"新买入已暂停",level:1});
  screen.getAllByTestId("portfolio-admission-1").forEach(badge=>expect(badge).toHaveTextContent("新买入已暂停"));
  expect(calls.every(call=>call.method==="GET")).toBe(true);
});

it("going offline removes an old green badge immediately and keeps the risk-exit capability separate",async()=>{
  readRuntime=async id=>json(runtime(id,true));render(<Page/>);
  await screen.findByRole("heading",{name:"当前运行条件已满足",level:1});await openList();
  vi.spyOn(window.navigator,"onLine","get").mockReturnValue(false);fireEvent(window,new Event("offline"));
  await screen.findByRole("heading",{name:"当前交易状态待核实",level:1});
  screen.getAllByTestId("portfolio-admission-1").forEach(badge=>expect(badge).not.toHaveAttribute("data-admission-state","allowed"));
});

it.each([
  ["REVIEW_REQUIRED","阻断已清除，等待本人审核"],
  ["WAITING_COVERAGE_START","等待运行条件到期"],
])("%s is presented identically without treating persisted READY as permission",async(phase,title)=>{
  readRuntime=async id=>json({...runtime(id,false,phase),recovery_summary:{...runtime(id,false,phase).recovery_summary,
    checks:[{key:"data",passed:true,reason:"当日数据已核验"},{key:"binding",passed:true,reason:"绑定已核验"},
      {key:"reconciliation",passed:phase.startsWith("WAITING")?null:true,reason:"范围尚未到期"}]}});
  render(<Page/>);await screen.findByRole("heading",{name:title,level:1});await openList();
  screen.getAllByTestId("portfolio-admission-1").forEach(badge=>expect(badge).toHaveTextContent(title));
});

it("malformed check evidence is unknown instead of crashing or displaying green",async()=>{
  readRuntime=async id=>json({...runtime(id,true),recovery_summary:{...runtime(id,true).recovery_summary,checks:[null]}});
  render(<Page/>);await screen.findByRole("heading",{name:"当前交易状态待核实",level:1});await openList();
  screen.getAllByTestId("portfolio-admission-1").forEach(badge=>expect(badge).toHaveTextContent("当前交易状态待核实"));
});

it("a newer same-portfolio read wins over a late healthy response",async()=>{
  let resolve!:(value:Response)=>void;const pending=new Promise<Response>(done=>{resolve=done;});
  readRuntime=id=>id===1?pending:Promise.resolve(json(runtime(id)));
  render(<Page/>);await openList();
  readRuntime=async id=>json(runtime(id));fireEvent(window,new Event("focus"));
  await screen.findByRole("heading",{name:"新买入已暂停",level:1});
  await act(async()=>resolve(json(runtime(1,true))));
  screen.getAllByTestId("portfolio-admission-1").forEach(badge=>expect(badge).toHaveTextContent("新买入已暂停"));
});

it("a timed-out runtime is unknown and its eventual healthy response cannot revive green",async()=>{
  vi.useFakeTimers();
  let resolve!:(value:Response)=>void;const pending=new Promise<Response>(done=>{resolve=done;});
  readRuntime=id=>id===1?pending:Promise.resolve(json(runtime(id)));
  render(<Page/>);
  await act(async()=>{await vi.advanceTimersByTimeAsync(10001);});
  expect(screen.getByRole("heading",{name:"当前交易状态待核实",level:1})).toBeInTheDocument();
  expect(screen.getByTestId("portfolio-admission-1")).not.toHaveAttribute("data-admission-state","allowed");
  await act(async()=>resolve(json(runtime(1,true))));
  expect(screen.getByRole("heading",{name:"当前交易状态待核实",level:1})).toBeInTheDocument();
});


it.each(["empty-checks","invalid-phase"])("%s evidence is unknown in all three displays",async mode=>{
  readRuntime=async id=>json({...runtime(id,true),recovery_summary:{...runtime(id,true).recovery_summary,
    ...(mode==="empty-checks"?{checks:[]}:{phase:42})}});
  render(<Page/>);await screen.findByRole("heading",{name:"当前交易状态待核实",level:1});await openList();
  screen.getAllByTestId("portfolio-admission-1").forEach(badge=>expect(badge).toHaveTextContent("当前交易状态待核实"));
  expect(screen.getByRole("button",{name:"添加候选标的"})).toBeDisabled();
});
