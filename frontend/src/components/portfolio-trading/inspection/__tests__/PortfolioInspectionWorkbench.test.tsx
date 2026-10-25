import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import PortfolioInspectionWorkbench from "../PortfolioInspectionWorkbench";
import InspectionRepairWorkspace from "../InspectionRepairWorkspace";
import {readInspectionBrowse,writeInspectionBrowse} from "../inspectionBrowseState";
import {installInspectionSubmissionRuntime} from "../../../../test/inspectionSubmissionRuntime";
import {repairTaskId,sendRepairSubmission,readRepairSubmission} from "../repairSubmissionJournal";
import {INSPECTION_CHANGE_KEY} from "../../../../utils/inspectionFreshness";

const { mockApi, toast } = vi.hoisted(() => ({ mockApi: {
  getPortfolioMaterialInvestigations: vi.fn(), recordPortfolioMaterialInvestigation: vi.fn(), getPortfolioEvidenceSupplements: vi.fn(),
  getPortfolioActionItems: vi.fn(), getPortfolioStatus: vi.fn(), getPortfolioInspectionRuntime: vi.fn(),
  getPortfolioInspectionDiagnostics: vi.fn(), getPortfolioModelBindingReview: vi.fn(), previewPortfolioReconciliation: vi.fn(),
  getPortfolioInspectionHistory: vi.fn(), getPortfolioInspectionHistorySecurities: vi.fn(),
  submitPortfolioInspectionRepair: vi.fn(), getPortfolioInspectionRepairTask: vi.fn(), retryPortfolioInspectionRepair: vi.fn(),
  confirmPortfolioReconciliation: vi.fn(), confirmPortfolioInspectionGuide: vi.fn(), transitionPortfolioState: vi.fn(), createAISession: vi.fn(), analysePortfolioInspection: vi.fn(), getPortfolioInspectionRepairTasks: vi.fn(), cancelPortfolioInspectionRepair: vi.fn(), getPortfolioInspectionJob: vi.fn(), getPortfolioInspectionQueue: vi.fn(),
}, toast: vi.fn() }));
vi.mock("../../../../api/client", () => ({ api: mockApi }));
vi.mock("../../../../context/AppContext", () => ({ useApp: () => ({ showToast: toast, portfolios:[{id:1,name:"隔离组合"}], setActiveTab: vi.fn(),setActiveSubTab:vi.fn() }) }));

function evidence(id: number) { return { evidence_id: `${id}:daily_bar`, symbol_id: id, check_code: "DAILY_BAR", label: "日线行情", status: "MISSING", reason: "应到交易日的日线尚未到达", source_label: "真实行情表", depends_on: [] }; }
function diagnostics(planId = "test-plan-1") {
  return { portfolio_id: 1, snapshot_id: `snapshot-${planId}`, generated_at: "2026-10-22T02:00:00Z", summary: {}, evidence: [],
    symbols: [1,2].map(id => ({ symbol_id: id, symbol: `TEST${id}`, name: `隔离标的${id}`, market: "CN", expected_trade_date: "2026-10-21", scoring: {}, evidence: [evidence(id)] })),
    repair_plan: { plan_id: planId, snapshot_id: `snapshot-${planId}`, generated_at: "2026-10-22T02:00:00Z", limitations: [],
      operations: [1,2].map(id => ({ operation_id: `sync-${id}`, symbol_id: id, symbol: `TEST${id}`, label: "补齐日线", trade_date: "2026-10-21", enabled: true, depends_on: [], expected_effect: "重检实际日线" })) } };
}
function runtime(canConfirm = false) { return { portfolio_id: 1, generated_at: "2026-10-22T02:00:00Z", current_state: "RECONCILIATION_BLOCKED", dimensions: {},
  permissions: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: true },
  capability_reasons: { allow_new_buys: ["缺少本日执行证据"], allow_risk_exits: ["风险退出通道可用"], allow_auto_recovery: ["必须本人审核"] },
  recovery_summary: { can_confirm: canConfirm, review_required: true, confirmation_action: "RECONCILIATION_CONFIRM", blockers: canConfirm ? [] : ["先完成证据核对"], checks: [
    { key: "reconciliation", passed: canConfirm, reason: "核实当日执行" }, { key: "data", passed: canConfirm, reason: "核实两个实际标的" },
    { key: "binding", passed: true, reason: "绑定已接受" }, { key: "runtime", passed: true, reason: "允许本人审核" } ] } }; }
function report(passed = false) { return { portfolio_id: 1, trade_date: "2026-10-21", report_id: "test-report", report_version: "evidence-v1", read_only: true,
  zero_sum_check_passed: passed, differences_found: false, missing_evidence: passed ? [] : [{ code: "DECISION_RUN_NOT_FOUND", detail: "尚未找到当日执行来源" }], items: [] }; }
function page() { return { portfolio_id: 1, items: [], total: 0, page: 1, page_size: 20, page_count: 0, has_more: false, snapshot_event_id: 0, filters: {}, relation_status: "no_related_history", read_only: true }; }
let stopSubmissionRuntime:()=>void;
const savedRepairTasks=new Map<string,any>();
beforeEach(() => {
  stopSubmissionRuntime=installInspectionSubmissionRuntime();localStorage.clear();savedRepairTasks.clear();
  sessionStorage.clear();
  vi.resetAllMocks();
  vi.spyOn(window,"scrollTo").mockImplementation(()=>{});
  mockApi.getPortfolioMaterialInvestigations.mockImplementation((pid,date,material)=>Promise.resolve({portfolio_id:pid,trade_date:date,material,items:[],has_more:false,read_only:true}));
  mockApi.getPortfolioEvidenceSupplements.mockImplementation((pid,date)=>Promise.resolve({portfolio_id:pid,trade_date:date,candidates:[],candidate_count:0,requests:[],limitations:[]}));
  mockApi.getPortfolioInspectionRepairTasks.mockResolvedValue({ portfolio_id: 1, items: [], next_cursor: null });
  mockApi.getPortfolioInspectionJob.mockResolvedValue({ portfolio_id: 1, enabled: true, reason: "按真实证据扫描", queue: { portfolio_id:1, days:[], limitations:[], verified_through:null } });
  mockApi.getPortfolioInspectionQueue.mockResolvedValue({portfolio_id:1,days:[],limitations:[]});
  mockApi.getPortfolioActionItems.mockResolvedValue({ items: [{ id: "real-data-issue", code: "MARKET_DATA_STALE", family: "data-freshness", title: "数据需要核实", impact: "两个标的日线缺失", severity: "blocker" }], summary: {} });
  mockApi.getPortfolioStatus.mockResolvedValue({ portfolio_id: 1, current_state: "RECONCILIATION_BLOCKED", allowed_transitions: [] });
  mockApi.getPortfolioInspectionRuntime.mockResolvedValue(runtime()); mockApi.getPortfolioInspectionDiagnostics.mockResolvedValue(diagnostics());
  mockApi.getPortfolioModelBindingReview.mockResolvedValue({ portfolio_id: 1, can_confirm: false, before: null, after: null, blockers: [], change_summary: "无待接受变更" });
  mockApi.previewPortfolioReconciliation.mockResolvedValue(report()); mockApi.getPortfolioInspectionHistory.mockResolvedValue(page());
  mockApi.getPortfolioInspectionHistorySecurities.mockResolvedValue({ items: [], total: 0 });
  mockApi.submitPortfolioInspectionRepair.mockImplementation(async(pid:number,request:any)=>{const task={task_id:await repairTaskId(pid,request.idempotency_key),portfolio_id:pid,status:"done",stage:"done",percent:100,total:2,processed:2,ok_count:1,failed_count:1,message:"一项失败，成功步骤已保留",objects:[],retryable_operation_ids:["sync-2"],audit_refs:[]};savedRepairTasks.set(task.task_id,task);return task;});
  mockApi.getPortfolioInspectionRepairTask.mockImplementation(async(_pid:number,id:string)=>{const task=savedRepairTasks.get(id);if(!task)throw {status_code:404};return task;});
});
afterEach(()=>{cleanup();stopSubmissionRuntime();});
function openPage() { return render(<PortfolioInspectionWorkbench portfolioId={1} renderDualRun={() => <p>实际双跑能力</p>} />); }
function otherTabChanged(pid=1){fireEvent(window,new StorageEvent("storage",{key:`${INSPECTION_CHANGE_KEY}${pid}`,newValue:JSON.stringify({version:1,id:crypto.randomUUID(),source:"other-tab",portfolioId:pid,at:Date.now()})}));}

async function recheckCurrentEvidence() {
  const button = await screen.findByRole("button", { name: "重新检测" });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
}

it("waits for a valid portfolio identity before mounting any backend observer",async()=>{
  const view=render(<PortfolioInspectionWorkbench portfolioId={0} renderDualRun={()=>null}/>);
  expect(screen.getByRole("status")).toHaveTextContent("正在确定当前组合");expect(mockApi.getPortfolioInspectionRuntime).not.toHaveBeenCalled();expect(mockApi.previewPortfolioReconciliation).not.toHaveBeenCalled();
  view.rerender(<PortfolioInspectionWorkbench portfolioId={1} renderDualRun={()=>null}/>);await screen.findByText("新买入已暂停");expect(mockApi.getPortfolioInspectionRuntime).toHaveBeenCalledWith(1);
});
it("expires an already acknowledged report offline and requires rereading and new consent after reconnect",async()=>{
  let online=true;const spy=vi.spyOn(navigator,"onLine","get").mockImplementation(()=>online);
  try{
    mockApi.getPortfolioInspectionRuntime.mockResolvedValue(runtime(true));mockApi.previewPortfolioReconciliation.mockResolvedValue(report(true));
    openPage();fireEvent.click(screen.getByRole("button",{name:"每日对账"}));fireEvent.click(await screen.findByRole("button",{name:"审核本次对账结果"}));
    const dialog=screen.getByRole("dialog");await waitFor(()=>expect(within(dialog).getByRole("textbox")).toBeVisible());
    fireEvent.change(within(dialog).getByRole("textbox"),{target:{value:"保留本人已经核实但尚未提交的审核说明"}});fireEvent.click(within(dialog).getByRole("checkbox"));
    online=false;fireEvent(window,new Event("offline"));expect(await screen.findByTestId("inspection-stale-notice")).toBeVisible();
    online=true;fireEvent(window,new Event("online"));await waitFor(()=>expect(mockApi.getPortfolioInspectionRuntime.mock.calls.length).toBeGreaterThan(1));
    expect(screen.getByTestId("inspection-stale-notice")).toBeVisible();fireEvent.click(screen.getByRole("button",{name:"重新读取当前依据"}));await waitFor(()=>expect(screen.queryByTestId("inspection-stale-notice")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button",{name:"审核本次对账结果"}));const fresh=screen.getByRole("dialog");await waitFor(()=>expect(within(fresh).getByRole("checkbox")).not.toBeChecked());
    expect(within(fresh).getByRole("textbox")).toHaveValue("保留本人已经核实但尚未提交的审核说明");expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
  }finally{spy.mockRestore();}
});

it("never replays a repair submission after a lost response and offers GET-only original task records",async()=>{
  const saved={task_id:"accepted-but-reply-lost",portfolio_id:1,status:"running",stage:"read-only-test",percent:10,total:2,processed:0,ok_count:0,failed_count:0,objects:[],retryable_operation_ids:[],message:"已经保存的原修复任务"};
  mockApi.submitPortfolioInspectionRepair.mockImplementation(async(pid:number,request:any)=>{saved.task_id=await repairTaskId(pid,request.idempotency_key);mockApi.getPortfolioInspectionRepairTasks.mockResolvedValue({portfolio_id:1,items:[saved],next_cursor:null});mockApi.getPortfolioInspectionRepairTask.mockResolvedValue(saved);throw Object.assign(new Error("response lost"),{status_code:0});});
  render(<InspectionRepairWorkspace portfolioId={1}/>);fireEvent.click(await screen.findByRole("button",{name:"预览批量修复（2 步）"}));
  const dialog=screen.getByRole("dialog");await waitFor(()=>expect(dialog).toBeVisible());fireEvent.click(within(dialog).getByRole("checkbox"));
  act(()=>{fireEvent.click(within(dialog).getByRole("button",{name:"确认执行本批计划"}));fireEvent.click(within(dialog).getByRole("button",{name:"确认执行本批计划"}));});await within(dialog).findByText(/提交结果暂时无法确认/);
  expect(within(dialog).getByRole("checkbox")).not.toBeChecked();fireEvent(window,new Event("online"));fireEvent(window,new Event("focus"));fireEvent(window,new Event("pageshow"));
  fireEvent.click(within(dialog).getByRole("button",{name:"读取原任务记录"}));expect(await screen.findByRole("heading",{name:"已经保存的原修复任务"})).toBeVisible();
  expect(mockApi.submitPortfolioInspectionRepair).toHaveBeenCalledTimes(1);expect(mockApi.retryPortfolioInspectionRepair).not.toHaveBeenCalled();
});
it("preserves the frozen request identity for an explicit re-confirmation after a lost reply",async()=>{
  mockApi.submitPortfolioInspectionRepair.mockRejectedValueOnce(Object.assign(new Error("503"),{status_code:503}));
  render(<InspectionRepairWorkspace portfolioId={1}/>);fireEvent.click(await screen.findByRole("button",{name:"预览批量修复（2 步）"}));
  let dialog=screen.getByRole("dialog");await waitFor(()=>expect(dialog).toBeVisible());fireEvent.click(within(dialog).getByRole("checkbox"));fireEvent.click(within(dialog).getByRole("button",{name:"确认执行本批计划"}));await within(dialog).findByText(/提交结果暂时无法确认/);
  const original=mockApi.submitPortfolioInspectionRepair.mock.calls[0][1];fireEvent.click(within(dialog).getByRole("button",{name:"Close"}));
  await waitFor(()=>expect(dialog).not.toBeVisible());fireEvent.click(await screen.findByRole("button",{name:"核对原请求并重试"}));dialog=screen.getByRole("dialog");await waitFor(()=>expect(dialog).toBeVisible());
  expect(within(dialog).getByRole("checkbox")).not.toBeChecked();expect(within(dialog).getByText(/保留的原请求范围/)).toBeVisible();fireEvent.click(within(dialog).getByRole("checkbox"));fireEvent.click(within(dialog).getByRole("button",{name:"确认执行本批计划"}));
  await waitFor(()=>expect(mockApi.submitPortfolioInspectionRepair).toHaveBeenCalledTimes(2));expect(mockApi.submitPortfolioInspectionRepair.mock.calls[1][1]).toEqual(original);
});
it("restores an unresolved request after remount and locates its exact completed task outside the recent page",async()=>{
  const p={request:{plan_id:'test-plan-1',snapshot_id:'snapshot-test-plan-1',generated_at:'2026-10-22T02:00:00Z',operation_ids:['sync-1'],confirmed:true as const,idempotency_key:'inspection-persistent-remount'},operations:diagnostics().repair_plan.operations as any,assetCount:1,retryTaskId:null,retryCount:0};
  await expect(sendRepairSubmission(1,p,async()=>{throw new Error('lost');},mockApi.getPortfolioInspectionRepairTask)).rejects.toThrow();
  const originalId=await repairTaskId(1,p.request.idempotency_key);
  const first=render(<InspectionRepairWorkspace portfolioId={1}/>);await screen.findByText(/暂未查到原任务/);expect(screen.getByRole('button',{name:'预览批量修复（2 步）'})).toBeDisabled();first.unmount();
  const exact={task_id:originalId,portfolio_id:1,status:'done',stage:'done',message:'已找回完成的确切原任务',objects:[],retryable_operation_ids:[],percent:100,total:1,processed:1,ok_count:1,failed_count:0};savedRepairTasks.set(originalId,exact);
  mockApi.getPortfolioInspectionRepairTasks.mockResolvedValue({portfolio_id:1,items:[{...exact,task_id:'unrelated-newer-task',status:'running',message:'其他日期的较新任务'}],next_cursor:'more-pages'});
  render(<InspectionRepairWorkspace portfolioId={1}/>);await screen.findByRole('heading',{name:'已找回完成的确切原任务'});
  expect(mockApi.getPortfolioInspectionRepairTask).toHaveBeenCalledWith(1,originalId);expect(screen.queryByRole('dialog')).not.toBeInTheDocument();expect(readRepairSubmission(1)?.request.idempotency_key).toBe(p.request.idempotency_key);expect(mockApi.submitPortfolioInspectionRepair).not.toHaveBeenCalled();
});
it("keeps the shared original key through lookup failure and rereads it without creating a new request",async()=>{
  const p={request:{plan_id:'test-plan-1',snapshot_id:'snapshot-test-plan-1',generated_at:'2026-10-22T02:00:00Z',operation_ids:['sync-1'],confirmed:true as const,idempotency_key:'inspection-lookup-retry'},operations:diagnostics().repair_plan.operations as any,assetCount:1,retryTaskId:null,retryCount:0};
  await expect(sendRepairSubmission(1,p,async()=>{throw new Error('lost');},mockApi.getPortfolioInspectionRepairTask)).rejects.toThrow();mockApi.getPortfolioInspectionRepairTask.mockRejectedValueOnce(Object.assign(new Error('unavailable'),{status_code:503}));
  render(<InspectionRepairWorkspace portfolioId={1}/>);await screen.findByText(/原任务暂时无法核实/);expect(screen.getByRole('button',{name:'预览批量修复（2 步）'})).toBeDisabled();expect(readRepairSubmission(1)?.request.idempotency_key).toBe(p.request.idempotency_key);
  const id=await repairTaskId(1,p.request.idempotency_key);savedRepairTasks.set(id,{task_id:id,portfolio_id:1,status:'running',message:'读取恢复后的原任务',objects:[],retryable_operation_ids:[],percent:0,total:1,processed:0,ok_count:0,failed_count:0});
  fireEvent.click(screen.getByRole('button',{name:'重新读取原请求对应任务'}));await screen.findByRole('heading',{name:'读取恢复后的原任务'});expect(mockApi.submitPortfolioInspectionRepair).not.toHaveBeenCalled();expect(mockApi.retryPortfolioInspectionRepair).not.toHaveBeenCalled();
});
it("shows a direct original-request recovery entry on the inspection page without dispatching",async()=>{
  const p={request:{plan_id:'test-plan-1',snapshot_id:'snapshot-test-plan-1',generated_at:'2026-10-22T02:00:00Z',operation_ids:['sync-1'],confirmed:true as const,idempotency_key:'inspection-main-entry'},operations:diagnostics().repair_plan.operations as any,assetCount:1,retryTaskId:null,retryCount:0};
  await expect(sendRepairSubmission(1,p,async()=>{throw new Error('lost');},mockApi.getPortfolioInspectionRepairTask)).rejects.toThrow();openPage();
  fireEvent.click(await screen.findByRole('button',{name:'查看原请求与任务'}));
  expect(JSON.parse(sessionStorage.getItem('portfolio-inspection-return-v1')!)).toMatchObject({portfolioId:1,destination:'settings:data-center:inspection-repair'});expect(mockApi.submitPortfolioInspectionRepair).not.toHaveBeenCalled();
});

it("revalidates server evidence before submitting an old review even without a live originating tab",async()=>{
  mockApi.getPortfolioInspectionRuntime.mockResolvedValue(runtime(true));mockApi.previewPortfolioReconciliation.mockResolvedValue(report(true));
  openPage();fireEvent.click(screen.getByRole("button",{name:"每日对账"}));fireEvent.click(await screen.findByRole("button",{name:"审核本次对账结果"}));
  const dialog=screen.getByRole("dialog");await waitFor(()=>expect(within(dialog).getByRole("textbox")).toBeVisible());
  fireEvent.change(within(dialog).getByRole("textbox"),{target:{value:"本人已核实当前日期的执行记录及资金持仓"}});fireEvent.click(within(dialog).getByRole("checkbox"));
  mockApi.previewPortfolioReconciliation.mockResolvedValue({...report(true),report_version:"task-finished-after-origin-closed"});
  fireEvent.click(within(dialog).getByRole("button",{name:"确认本次审核"}));expect(await screen.findByTestId("inspection-stale-notice")).toBeVisible();
  expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();expect(mockApi.transitionPortfolioState).not.toHaveBeenCalled();
});

it("invalidates an open review from another tab, preserves its date, and requires fresh consent",async()=>{
  mockApi.getPortfolioInspectionRuntime.mockResolvedValue(runtime(true));mockApi.previewPortfolioReconciliation.mockResolvedValue(report(true));
  openPage();fireEvent.click(screen.getByRole("button",{name:"每日对账"}));
  fireEvent.click(await screen.findByRole("button",{name:"审核本次对账结果"}));
  const dialog=screen.getByRole("dialog");await waitFor(()=>expect(within(dialog).getByRole("textbox")).toBeVisible());
  fireEvent.change(within(dialog).getByRole("textbox"),{target:{value:"保留本人已经填写但尚未提交的审核理由"}});fireEvent.click(within(dialog).getByRole("checkbox"));
  otherTabChanged();expect(await screen.findByTestId("inspection-stale-notice")).toBeVisible();
  expect(screen.getByRole("button",{name:"审核本次对账结果"})).toBeDisabled();expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
  mockApi.previewPortfolioReconciliation.mockResolvedValue({...report(true),report_id:"latest-report",report_version:"latest-v2"});
  fireEvent.click(screen.getByRole("button",{name:"重新读取当前依据"}));await waitFor(()=>expect(screen.queryByTestId("inspection-stale-notice")).not.toBeInTheDocument());
  expect(screen.getByTestId("recon-verdict")).toHaveTextContent("2026-10-21");fireEvent.click(screen.getByRole("button",{name:"审核本次对账结果"}));
  const freshDialog=screen.getByRole("dialog");await waitFor(()=>expect(within(freshDialog).getByRole("checkbox")).not.toBeChecked());
  expect(within(freshDialog).getByRole("button",{name:"确认本次审核"})).toBeDisabled();expect(within(freshDialog).getByRole("textbox")).toHaveValue("保留本人已经填写但尚未提交的审核理由");
});

it("keeps the expired guard through failed refresh and another update during an in-flight read",async()=>{
  openPage();await screen.findByText("新买入已暂停");otherTabChanged(2);expect(screen.queryByTestId("inspection-stale-notice")).not.toBeInTheDocument();
  otherTabChanged();mockApi.getPortfolioInspectionRuntime.mockRejectedValueOnce(new Error("fresh runtime failed"));
  fireEvent.click(screen.getByRole("button",{name:"重新读取当前依据"}));await waitFor(()=>expect(mockApi.getPortfolioInspectionRuntime).toHaveBeenCalledTimes(2));await waitFor(()=>expect(screen.getByRole("button",{name:"重新读取当前依据"})).not.toHaveClass("ant-btn-loading"));expect(screen.getByTestId("inspection-stale-notice")).toBeVisible();
  let finish!:(value:any)=>void;mockApi.getPortfolioInspectionRuntime.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
  fireEvent.click(screen.getByRole("button",{name:"重新读取当前依据"}));await waitFor(()=>expect(finish).toBeDefined());otherTabChanged();await act(async()=>finish(runtime(true)));
  expect(screen.getByTestId("inspection-stale-notice")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"重新读取当前依据"}));await waitFor(()=>expect(screen.queryByTestId("inspection-stale-notice")).not.toBeInTheDocument());
  expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();expect(mockApi.transitionPortfolioState).not.toHaveBeenCalled();
});

it("does not replace a newer job read with an older workbench-wide result",async()=>{
  let finish!:(value:any)=>void;
  const queue={portfolio_id:1,days:[{trade_date:"2026-10-21",status:"EVIDENCE_INCOMPLETE",reason:"最新来源"}],limitations:[]};
  mockApi.getPortfolioInspectionJob.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;})).mockResolvedValue({portfolio_id:1,enabled:true,queue,reason:"最新作业"});
  mockApi.getPortfolioInspectionQueue.mockResolvedValue(queue);
  openPage();await waitFor(()=>expect(mockApi.getPortfolioInspectionJob).toHaveBeenCalledTimes(1));fireEvent.click(screen.getByRole("button",{name:"查看作业"}));
  expect(await screen.findByRole("button",{name:"核查 2026-10-21"})).toBeVisible();
  await act(async()=>finish({portfolio_id:1,enabled:false,queue:{portfolio_id:1,days:[],limitations:[]},reason:"旧作业"}));
  expect(screen.getByRole("button",{name:"核查 2026-10-21"})).toBeVisible();expect(screen.queryByText("旧作业")).not.toBeInTheDocument();
});

it("keeps each portfolio's search and drawer on return without flashing the previous portfolio",async()=>{
  mockApi.getPortfolioActionItems.mockImplementation(async(pid:number)=>({portfolio_id:pid,items:[],summary:{}}));
  mockApi.getPortfolioStatus.mockImplementation(async(pid:number)=>({portfolio_id:pid,current_state:"RECONCILIATION_BLOCKED",allowed_transitions:[]}));
  mockApi.getPortfolioInspectionRuntime.mockImplementation(async(pid:number)=>({...runtime(),portfolio_id:pid}));
  mockApi.getPortfolioInspectionDiagnostics.mockImplementation(async(pid:number)=>({...diagnostics(),portfolio_id:pid}));
  mockApi.getPortfolioModelBindingReview.mockImplementation(async(pid:number)=>({portfolio_id:pid,can_confirm:false,before:null,after:null,blockers:[],change_summary:"无变更"}));
  mockApi.previewPortfolioReconciliation.mockImplementation(async(pid:number,date?:string)=>({...report(),portfolio_id:pid,trade_date:date??(pid===1?"2026-10-21":"2026-10-20")}));
  mockApi.getPortfolioInspectionJob.mockImplementation(async(pid:number)=>({portfolio_id:pid,enabled:false,queue:{portfolio_id:pid,days:[],limitations:[]}}));
  const view=openPage();fireEvent.click(await screen.findByRole("button",{name:"打开数据修复"}));
  fireEvent.change(screen.getByRole("textbox",{name:"搜索待核实标的"}),{target:{value:"TEST1"}});
  view.rerender(<PortfolioInspectionWorkbench portfolioId={2} renderDualRun={()=>null}/>);
  expect(screen.queryByRole("textbox",{name:"搜索待核实标的"})).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button",{name:"打开数据修复"}));expect(screen.getByRole("textbox",{name:"搜索待核实标的"})).toHaveValue("");
  fireEvent.change(screen.getByRole("textbox",{name:"搜索待核实标的"}),{target:{value:"TEST2"}});
  view.rerender(<PortfolioInspectionWorkbench portfolioId={1} renderDualRun={()=>null}/>);
  expect(await screen.findByRole("textbox",{name:"搜索待核实标的"})).toHaveValue("TEST1");
  expect(readInspectionBrowse<{query:string}>(2,"data-filter")?.query).toBe("TEST2");
});

it("restores the selected day after remount but fetches fresh evidence and never restores confirmation",async()=>{
  writeInspectionBrowse(1,"workbench",{portfolioId:1,layers:["reconciliation"],panel:"reconciliation",selectedTradeDate:"2026-09-30",step:3,modal:"resume",ack:true,verifiedProofs:{reconciliation:"old"}});
  mockApi.previewPortfolioReconciliation.mockImplementation(async(pid:number,date?:string)=>({...report(),portfolio_id:pid,trade_date:date??"2026-10-21"}));
  openPage();expect(await screen.findByTestId("recon-verdict")).toHaveTextContent("2026-09-30");
  expect(mockApi.previewPortfolioReconciliation).toHaveBeenCalledWith(1,"2026-09-30");
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"Close"}));fireEvent.click(screen.getByRole("button",{name:"每日对账"}));
  await waitFor(()=>expect(mockApi.previewPortfolioReconciliation).toHaveBeenLastCalledWith(1,"2026-09-30"));
});

it("restores a closed workbench without inventing an active recovery guide",async()=>{
  writeInspectionBrowse(1,"workbench",{portfolioId:1,layers:[],panel:null,selectedTradeDate:"2026-09-30",step:3});
  mockApi.previewPortfolioReconciliation.mockImplementation(async(pid:number,date?:string)=>({...report(),portfolio_id:pid,trade_date:date??"2026-10-21"}));
  openPage();await screen.findByText("新买入已暂停");expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"每日对账"}));await screen.findByTestId("recon-verdict");
  expect(screen.queryByRole("button",{name:"返回状态审核并刷新"})).not.toBeInTheDocument();
});

it("opens daily reconciliation immediately and closing during a read never reopens it",async()=>{
  mockApi.getPortfolioActionItems.mockResolvedValue({items:[{id:"day",family:"reconciliation",code:"EVIDENCE_INCOMPLETE",trade_date:"2026-10-19",title:"核对待完成",severity:"blocker"}],summary:{}});
  openPage();await screen.findByText("新买入已暂停");
  let finish!:(value:ReturnType<typeof report>)=>void;
  mockApi.previewPortfolioReconciliation.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
  fireEvent.click(screen.getByRole("button",{name:"每日对账"}));
  expect(await screen.findByText("正在读取本日核对材料…")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"Close"}));
  await act(async()=>finish({...report(),trade_date:"2026-10-19"}));
  expect(screen.queryByRole("combobox",{name:"核对交易日"})).not.toBeInTheDocument();
  expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
});

it("leaves a visible retry path when opening a date fails",async()=>{
  mockApi.getPortfolioActionItems.mockResolvedValue({items:[{id:"day",family:"reconciliation",code:"EVIDENCE_INCOMPLETE",trade_date:"2026-10-19",title:"核对待完成",severity:"blocker"}],summary:{}});
  openPage();await screen.findByText("新买入已暂停");
  mockApi.previewPortfolioReconciliation.mockRejectedValueOnce(new Error("本日来源读取失败"));
  fireEvent.click(screen.getByRole("button",{name:"每日对账"}));
  expect(await screen.findByText("本日来源读取失败")).toBeVisible();
  expect(screen.getByRole("button",{name:"重新读取核对证据"})).toBeVisible();
  expect(screen.getByRole("combobox",{name:"核对交易日"}).closest(".ant-select")).toHaveTextContent("2026-10-19");
});

it("ignores an older date response after the user chooses a newer date",async()=>{
  const dates=["2026-10-19","2026-10-20"];
  mockApi.getPortfolioActionItems.mockResolvedValue({items:[{id:"day",family:"reconciliation",code:"EVIDENCE_INCOMPLETE",trade_date:dates[0],title:"核对待完成",severity:"blocker"}],summary:{}});
  mockApi.getPortfolioInspectionJob.mockResolvedValue({portfolio_id:1,enabled:true,queue:{portfolio_id:1,days:dates.map(trade_date=>({trade_date,status:"EVIDENCE_INCOMPLETE"})),limitations:[]}});
  openPage();await screen.findByText("新买入已暂停");
  let oldResponse!:(value:ReturnType<typeof report>)=>void;
  mockApi.previewPortfolioReconciliation.mockImplementation((_pid:number,date:string)=>date===dates[0]?new Promise(resolve=>{oldResponse=resolve;}):Promise.resolve({...report(),trade_date:date}));
  fireEvent.click(screen.getByRole("button",{name:"每日对账"}));
  const select=await screen.findByRole("combobox",{name:"核对交易日"});
  fireEvent.mouseDown(select.closest('.ant-select-selector')!);
  fireEvent.click(await screen.findByText("2026-10-20 · 材料待核实"));
  await waitFor(()=>expect(screen.getByTestId("recon-verdict")).toHaveTextContent(dates[1]));
  await act(async()=>oldResponse({...report(),trade_date:dates[0]}));
  expect(screen.getByTestId("recon-verdict")).toHaveTextContent(dates[1]);
  expect(select.closest('.ant-select')).toHaveTextContent(dates[1]);
});

it("keeps the last report version during rapid A-B-C-A date changes, including stale errors",async()=>{
  const dates=["2026-10-19","2026-10-20","2026-10-21"];
  mockApi.getPortfolioActionItems.mockResolvedValue({items:[{id:"day",family:"reconciliation",code:"EVIDENCE_INCOMPLETE",trade_date:dates[0],title:"核对待完成",severity:"blocker"}],summary:{}});
  mockApi.getPortfolioInspectionJob.mockResolvedValue({portfolio_id:1,enabled:true,queue:{portfolio_id:1,days:dates.map(trade_date=>({trade_date,status:"EVIDENCE_INCOMPLETE"})),limitations:[]}});
  openPage();await screen.findByText("新买入已暂停");
  const pending:Array<{date:string;resolve:(value:any)=>void;reject:(error:Error)=>void}>=[];
  mockApi.previewPortfolioReconciliation.mockImplementation((_pid:number,date:string)=>new Promise((resolve,reject)=>pending.push({date,resolve,reject})));
  fireEvent.click(screen.getByRole("button",{name:"每日对账"}));await waitFor(()=>expect(pending).toHaveLength(1));
  for(const date of [dates[1],dates[2],dates[0]]){fireEvent.mouseDown(screen.getByRole("combobox",{name:"核对交易日"}).closest('.ant-select-selector')!);fireEvent.click(await screen.findByText(`${date} · 材料待核实`));}
  await waitFor(()=>expect(pending).toHaveLength(4));
  await act(async()=>pending[3].resolve({...report(),trade_date:dates[0],report_id:"newest-report",report_version:"v4"}));
  await screen.findByTestId("recon-verdict");
  await act(async()=>{pending[0].resolve({...report(),trade_date:dates[0],report_id:"old-same-date",report_version:"v1"});pending[1].reject(new Error("旧日期失败"));pending[2].resolve({...report(),trade_date:dates[2],report_id:"old-other-date",report_version:"v3"});});
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();expect(screen.getByTestId("recon-verdict")).toHaveTextContent(dates[0]);fireEvent.click(screen.getByRole("button",{name:"本日处理记录"}));
  await waitFor(()=>expect(mockApi.getPortfolioInspectionHistory).toHaveBeenCalledWith(1,expect.objectContaining({trade_date_from:dates[0],report_ref:"newest-report",report_version:"v4"})));
});

it("opens binding details from the review in a separate drawer and returns to the original final step",async()=>{
  const value=runtime();value.recovery_summary.checks.find(row=>row.key==="binding")!.passed=false;
  mockApi.getPortfolioInspectionRuntime.mockResolvedValue(value);
  openPage();fireEvent.click(await screen.findByRole("button",{name:"运行状态审核"}));
  fireEvent.click(screen.getByRole("button",{name:"处理运行绑定"}));
  expect(await screen.findByRole("heading",{name:"审核模型与策略运行绑定"})).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"返回运行状态审核"}));
  expect(screen.getByRole("button",{name:/本人审核恢复/})).toHaveAttribute("aria-current","step");
  expect(screen.getByRole("region",{name:"审核剩余问题"})).toBeVisible();
});

it("uses separate detail overlays, closes back to the filtered parent and saves a concrete cross-module return context",async()=>{
  openPage();fireEvent.click(await screen.findByRole("button",{name:"运行状态审核"}));
  expect(screen.getByRole("region",{name:"当前审核结论"})).toBeVisible();
  expect(screen.getByRole("region",{name:"审核剩余问题"})).toBeVisible();
  fireEvent.click(within(screen.getByRole("navigation",{name:"恢复运行步骤"})).getByRole("button",{name:/批量修复数据/}));
  fireEvent.click(screen.getByRole("button",{name:"查看当前数据缺口"}));
  fireEvent.change(screen.getByRole("textbox",{name:"搜索待核实标的"}),{target:{value:"TEST1"}});
  fireEvent.click(screen.getByRole("button",{name:"查看标的证据"}));
  const detail=screen.getByRole("dialog");
  expect(within(detail).getByRole("heading",{name:/隔离标的1.*数据检查详情/})).toBeVisible();
  expect(screen.queryByRole("textbox",{name:"搜索待核实标的"})).not.toBeInTheDocument();
  fireEvent.click(within(detail).getByRole("button",{name:"返回数据汇总"}));
  expect(screen.getByRole("textbox",{name:"搜索待核实标的"})).toHaveValue("TEST1");
  fireEvent.click(screen.getByRole("button",{name:"前往数据中心批量修复"}));
  const saved=JSON.parse(sessionStorage.getItem("portfolio-inspection-return-v1")!);
  expect(saved).toEqual(expect.objectContaining({portfolioId:1,panel:"data",layers:["guide","data"],step:1,destination:"settings:data-center:inspection-repair"}));
  expect(mockApi.submitPortfolioInspectionRepair).not.toHaveBeenCalled();
  expect(mockApi.transitionPortfolioState).not.toHaveBeenCalled();
});

it("restores the original drawer stack and filter after cross-module work instead of forcing a different guide step",async()=>{
  sessionStorage.setItem("portfolio-inspection-return-v1",JSON.stringify({portfolioId:1,panel:"data",layers:["guide","data"],step:2,selectedTradeDate:"2026-10-19",reviewNote:"保留原审核理由",savedAt:Date.now()}));
  sessionStorage.setItem("portfolio-inspection-diagnosis-draft-v1",JSON.stringify({portfolioId:1,query:"TEST1",savedAt:Date.now()}));
  render(<PortfolioInspectionWorkbench portfolioId={1} navigation={{tab:"state",requestId:1}} renderDualRun={()=>null}/>);
  expect(await screen.findByRole("textbox",{name:"搜索待核实标的"})).toHaveValue("TEST1");
  fireEvent.click(screen.getByRole("button",{name:"返回运行状态审核"}));
  expect(screen.getByRole("button",{name:/确认运行绑定/})).toHaveAttribute("aria-current","step");
  expect(mockApi.previewPortfolioReconciliation).toHaveBeenCalledWith(1,"2026-10-19");
  expect(mockApi.confirmPortfolioInspectionGuide).not.toHaveBeenCalled();
});

it("keeps data and status work separate from historical reconciliation without dropping the final prerequisite",async()=>{
  openPage();
  expect(await screen.findByRole("region",{name:"数据修复与状态审核模块"})).toBeVisible();
  expect(screen.getByRole("region",{name:"执行与对账模块"})).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"运行状态审核"}));
  const nav=screen.getByRole("navigation",{name:"恢复运行步骤"});
  expect(within(nav).getAllByRole("button")).toHaveLength(3);
  expect(within(nav).queryByRole("button",{name:/核实执行/})).not.toBeInTheDocument();
  expect(screen.queryByTestId("reconcile-panel")).not.toBeInTheDocument();
  expect(screen.queryByText("还需要核实的材料")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("执行对账前置条件")).not.toBeInTheDocument();
  expect(screen.queryByRole("button",{name:"查看执行对账"})).not.toBeInTheDocument();
  fireEvent.click(within(nav).getByRole("button",{name:/确认运行绑定/}));
  expect(screen.getByText("无待接受变更")).toBeVisible();
  expect(screen.queryByLabelText("执行对账前置条件")).not.toBeInTheDocument();
  expect(screen.queryByText("执行对账仍有审核前置条件")).not.toBeInTheDocument();
  expect(screen.queryByRole("button",{name:"查看执行对账"})).not.toBeInTheDocument();
  fireEvent.click(within(nav).getByRole("button",{name:/本人审核恢复/}));
  expect(screen.getByRole("button",{name:"审核并确认恢复"})).toBeDisabled();
  const reconciliationCheck=screen.getByText("执行对账",{exact:true}).closest("div") as HTMLElement;
  expect(within(reconciliationCheck).getByText("未通过")).toBeVisible();
  expect(within(reconciliationCheck).getByText(/2026-10-21 的历史执行资料不完整/)).toBeVisible();
  const review=screen.getByRole("region",{name:"审核剩余问题"});
  expect(within(review).queryByText("绑定已接受")).not.toBeInTheDocument();
  expect(screen.getByRole("region",{name:"当前审核结论"})).toBeVisible();
  expect(screen.getByRole("region",{name:"确认后的影响"})).toHaveTextContent("解除本次对应的对账暂停");
  fireEvent.click(within(reconciliationCheck).getByRole("button",{name:"查看历史缺口"}));
  expect(await screen.findByRole("region",{name:"历史缺口详情"})).toBeVisible();
  expect(screen.queryByRole("navigation",{name:"恢复运行步骤"})).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox",{name:"材料核查说明"})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"返回运行状态审核"}));
  expect(screen.getByRole("button",{name:"审核并确认恢复"})).toBeDisabled();
  expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
  expect(mockApi.transitionPortfolioState).not.toHaveBeenCalled();
});

it("explains a reused historical task without turning dispatch completion into execution or recovery proof", async () => {
  const detail="portfolio_id=1 trade_date=2026-09-29 无 SUCCEEDED auto_simulation DecisionRun";
  mockApi.previewPortfolioReconciliation.mockResolvedValue({...report(),
    missing_evidence:[{code:"DECISION_RUN_NOT_FOUND",detail}],
    execution_expected:null, execution_expected_evidence:[{status:"UNKNOWN"}],
    checks:[{check_code:"POSITIONS",status:"NOT_CHECKED"}],
    items:[{dimension:"DECISION_RUN_NOT_FOUND",expected_value:null,actual_value:null,diff_value:null,explain_note:detail}],activity_evidence:[
    {evidence_id:"orders", source_code:"sim_orders",source_label:"模拟订单",query_status:"QUERIED",count:0,reason:"本日查询成功，无订单",source_record_refs:[],records:[]},
    {evidence_id:"dispatch",source_code:"execution_dispatch_history",source_label:"自动调度与原任务历史",query_status:"CONTEXT_ONLY",count:1,
      reason:"旧任务完成不代表本日执行完成",source_record_refs:["scheduled_task_run:494","async_task:actual-old-task"],
      records:[{id:494,task_id:"actual-old-task",task_status:"done",dispatch_created_at:"2026-09-29T06:55:01",task_created_at:"2026-08-18T05:59:20",task_finished_at:"2026-08-18T06:00:00",
        stale_terminal_task_reused:true,original_scoped_result:{portfolio_results:[{portfolio_id:1,error:"原任务行情过期"}]}}]},
    {evidence_id:"ledger",source_code:"cash_ledger",source_label:"现金流水账",query_status:"READ_ERROR",count:null,reason:"读取失败不能视为无活动",source_record_refs:[],records:[]},
  ]});
  openPage();fireEvent.click(await screen.findByRole("button",{name:"每日对账"}));
  expect(await screen.findByText("历史执行资料不完整")).toBeVisible();
  expect(screen.queryByText("当天的自动运行记录与决策明细")).not.toBeInTheDocument();
  expect(screen.queryByText("当天生效的自动接管与调度配置")).not.toBeInTheDocument();
  expect(screen.queryByText("该交易日的持仓明细")).not.toBeInTheDocument();
  expect(screen.queryByRole("button",{name:"核查实际来源并补证关联"})).not.toBeInTheDocument();
  expect(screen.getByRole("button",{name:"本日处理记录"})).toBeEnabled();
  const panel=screen.getByTestId("reconcile-panel");
  expect(panel).toHaveTextContent("历史缺口尚未解决");
  expect(panel).not.toHaveTextContent(/portfolio_id|trade_date|DecisionRun|auto_simulation|SUCCEEDED|test-report|evidence-v1/);
  expect(within(panel).queryByRole("table")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"查看并处理历史缺口"}));
  fireEvent.click(screen.getByText("查看系统已检查的来源明细"));
  expect(await screen.findByText("本次调度引用了以前已结束的任务")).toBeVisible();
  expect(screen.getByText(/模拟订单 · 0 条 · 已读取当日来源/)).toBeVisible();
  expect(screen.getByText(/现金流水账 · 数量未知 · 读取失败/)).toBeVisible();
  expect(screen.getByRole("region",{name:"历史缺口详情"})).toHaveTextContent("尚未找到当天可核验的自动运行记录或决策明细");
  expect(screen.getByText("处理结束")).toBeVisible();
  expect(screen.getByText("原任务中本组合的结果：原任务行情过期")).toBeVisible();
  expect(screen.queryByRole("button",{name:"审核本次对账结果"})).not.toBeInTheDocument();
  expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
});

it("consolidates three missing sources in one history-gap drawer without individual registration and returns from history and empty supplements", async () => {
  mockApi.previewPortfolioReconciliation.mockResolvedValue({...report(),trade_date:"2026-09-29",execution_expected_evidence:[{status:"UNKNOWN"}],checks:[{check_code:"POSITIONS",status:"NOT_CHECKED"}]});
  openPage(); fireEvent.click(await screen.findByRole("button",{name:"每日对账"}));
  expect(screen.queryByRole("list",{name:"系统发现的历史缺口"})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"查看并处理历史缺口"}));
  const gap=await screen.findByRole("region",{name:"历史缺口详情"});
  const list=within(gap).getByRole("list",{name:"系统发现的历史缺口"});
  for(const title of ["当天的自动运行记录与决策明细","当天生效的自动接管与调度配置","该交易日的持仓明细"]) {
    const row=within(list).getByRole("listitem",{name:title});
    expect(row).toBeVisible();
    fireEvent.click(within(row).getByText("查看这项依据的来源与查找方法"));
  }
  expect(within(gap).queryByRole("textbox")).not.toBeInTheDocument();
  expect(within(gap).queryByRole("radio")).not.toBeInTheDocument();
  fireEvent.click(within(gap).getByRole("button",{name:"查看该日查找与处理记录"}));
  await waitFor(()=>expect(mockApi.getPortfolioInspectionHistory).toHaveBeenCalledWith(1,expect.objectContaining({trade_date_from:"2026-09-29",trade_date_to:"2026-09-29",report_ref:"test-report",report_version:"evidence-v1"})));
  fireEvent.click(screen.getByRole("button",{name:"返回原问题处理"}));
  expect(within(screen.getByRole("listitem",{name:"该交易日的持仓明细"})).getByText("可核验的原始资料需要包含什么")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"查看可关联的原订单"}));
  fireEvent.click(await screen.findByRole("button",{name:"返回历史缺口，查看后续处理"}));
  expect(screen.getByRole("region",{name:"历史缺口后续处理"})).toBeVisible();
  expect(screen.queryByRole("textbox",{name:"材料核查说明"})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"返回执行与对账"}));
  expect(screen.getByRole("combobox",{name:"核对交易日"})).toBeVisible();
  expect(mockApi.recordPortfolioMaterialInvestigation).not.toHaveBeenCalled();
  expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
  expect(mockApi.transitionPortfolioState).not.toHaveBeenCalled();
});

it("refreshes actual history-gap sources and locates existing history using the refreshed report without creating notes",async()=>{
  openPage();fireEvent.click(await screen.findByRole("button",{name:"每日对账"}));
  fireEvent.click(screen.getByRole("button",{name:"查看并处理历史缺口"}));
  mockApi.previewPortfolioReconciliation.mockResolvedValue({...report(),report_version:"refreshed-evidence"});
  fireEvent.click(screen.getByRole("button",{name:"重新读取现有来源"}));
  await waitFor(()=>expect(mockApi.previewPortfolioReconciliation).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole("button",{name:"查看该日查找与处理记录"}));
  await waitFor(()=>expect(mockApi.getPortfolioInspectionHistory).toHaveBeenCalledWith(1,expect.objectContaining({report_version:"refreshed-evidence"})));
  expect(mockApi.getPortfolioMaterialInvestigations).not.toHaveBeenCalled();
  expect(mockApi.recordPortfolioMaterialInvestigation).not.toHaveBeenCalled();
  expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
});

it("restores a legacy per-material return trail as the consolidated gap drawer without reviving registration forms",async()=>{
  sessionStorage.setItem("portfolio-inspection-return-v1",JSON.stringify({portfolioId:1,panel:"material",layers:["guide","reconciliation","material"],step:3,selectedTradeDate:"2026-09-29",materialSelection:{kind:"schedule",view:"handle",origin:"reconciliation"},savedAt:Date.now()}));
  sessionStorage.setItem("inspection-material-draft:1:2026-09-29:schedule",JSON.stringify({note:"先前留下的查找说明草稿应保留，但不再作为逐项审核流程",savedAt:Date.now()}));
  mockApi.previewPortfolioReconciliation.mockResolvedValue({...report(),trade_date:"2026-09-29",execution_expected_evidence:[{status:"UNKNOWN"}]});
  openPage();expect(await screen.findByRole("region",{name:"历史缺口详情"})).toBeVisible();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  expect(sessionStorage.getItem("inspection-material-draft:1:2026-09-29:schedule")).toContain("先前留下的查找说明");
  fireEvent.click(screen.getByRole("button",{name:"返回执行与对账"}));
  fireEvent.click(screen.getByRole("button",{name:"返回状态审核并刷新"}));
  expect(screen.getByRole("button",{name:/本人审核恢复/})).toHaveAttribute("aria-current","step");
  expect(screen.getByRole("button",{name:"审核并确认恢复"})).toBeDisabled();
  expect(mockApi.recordPortfolioMaterialInvestigation).not.toHaveBeenCalled();
  expect(mockApi.confirmPortfolioInspectionGuide).not.toHaveBeenCalled();
});

it("keeps not-yet-arrived execution results in normal waiting without presenting a historical-gap task",async()=>{
  mockApi.previewPortfolioReconciliation.mockResolvedValue({...report(),verdict:"WAITING_EXECUTION",work_state:"waiting_execution",execution_expected_evidence:[{status:"UNKNOWN"}],checks:[{check_code:"POSITIONS",status:"NOT_CHECKED"}]});
  openPage();fireEvent.click(await screen.findByRole("button",{name:"每日对账"}));
  expect(await screen.findByText("等待当天执行结果")).toBeVisible();
  expect(screen.queryByRole("button",{name:"查看并处理历史缺口"})).not.toBeInTheDocument();
  expect(screen.queryByRole("list",{name:"系统发现的历史缺口"})).not.toBeInTheDocument();
  expect(mockApi.recordPortfolioMaterialInvestigation).not.toHaveBeenCalled();
  expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
});

it("keeps an unreadable restored history gap explicit and retryable without granting approval",async()=>{
  sessionStorage.setItem("portfolio-inspection-return-v1",JSON.stringify({portfolioId:1,panel:"material",layers:["reconciliation","material"],selectedTradeDate:"2026-09-29",savedAt:Date.now()}));
  mockApi.previewPortfolioReconciliation.mockRejectedValueOnce(new Error("历史来源暂时不可读"));
  mockApi.previewPortfolioReconciliation.mockResolvedValue({...report(),trade_date:"2026-09-29"});
  openPage();expect(await screen.findByText("历史缺口暂时无法读取")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"重新读取历史缺口"}));
  expect(await screen.findByRole("region",{name:"历史缺口详情"})).toBeVisible();
  expect(mockApi.recordPortfolioMaterialInvestigation).not.toHaveBeenCalled();
  expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
});

it("takes legitimate waiting to actual job progress without inviting another recovery approval", async () => {
  const value:any=runtime(true); value.recovery_summary={...value.recovery_summary,can_confirm:false,review_required:false,phase:"WAITING_EXECUTION",recommended_action:"WAIT_EXECUTION"};
  mockApi.getPortfolioInspectionRuntime.mockResolvedValue(value); mockApi.getPortfolioActionItems.mockResolvedValue({items:[],summary:{}});
  mockApi.previewPortfolioReconciliation.mockResolvedValue({...report(),work_state:"waiting_execution"});
  openPage(); fireEvent.click(await screen.findByRole("button",{name:"查看等待进度"}));
  expect(await screen.findByText("计划已开启")).toBeVisible();
  expect(screen.queryByRole("button",{name:"审核并确认恢复"})).not.toBeInTheDocument();
  expect(mockApi.transitionPortfolioState).not.toHaveBeenCalled();
});

it("keeps engine admission and restricted symbols out of daily portfolio blockers", async () => {
  const value=runtime(true); value.permissions.allow_new_buys=true;
  mockApi.getPortfolioInspectionRuntime.mockResolvedValue(value);
  mockApi.getPortfolioActionItems.mockResolvedValue({items:[{id:"engine-only",source:"g7_ops",family:"g7_ops:review",code:"G5_PENDING",title:"新引擎准入待核实",impact:"仅影响切换新引擎",severity:"blocker",portfolio_wide:false,scope:{kind:"engine_admission"}}],summary:{}});
  openPage(); expect(await screen.findByText("当前运行条件已满足")).toBeVisible();
  expect(screen.getByText("当前没有需要处理的阻断")).toBeVisible();
  expect(screen.getByText(/其他提醒与信息 · 1 项/)).toBeVisible();
});

it("opens the actual trade date from a separate historical reconciliation task", async () => {
  mockApi.getPortfolioActionItems.mockResolvedValue({items:[{id:"day-a",source:"reconciliation",family:"recon:1:baseline:1:2026-10-19",code:"EVIDENCE_INCOMPLETE",trade_date:"2026-10-19",title:"旧日证据待核实",impact:"核对原交易日",severity:"blocker"}],summary:{}});
  openPage(); fireEvent.click(await screen.findByRole("button",{name:"每日对账"}));
  await waitFor(()=>expect(mockApi.previewPortfolioReconciliation).toHaveBeenCalledWith(1,"2026-10-19"));
});

it("preserves hidden selections until explicitly restricted and restores a persisted task without submitting", async () => {
  mockApi.getPortfolioInspectionRepairTasks.mockResolvedValue({ portfolio_id:1, next_cursor:null, items:[{ task_id:"saved-task", portfolio_id:1, status:"failed", message:"已保存的真实修复任务", objects:[], retryable_operation_ids:[], percent:50, total:2, processed:1, ok_count:1, failed_count:1 }] });
  render(<InspectionRepairWorkspace portfolioId={1}/>); await screen.findByRole("textbox",{name:"搜索待修复标的"});
  fireEvent.change(screen.getByRole("textbox", {name:"搜索待修复标的"}), {target:{value:"TEST1"}});
  expect(screen.getByText(/含筛选外 1 步/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name:"仅保留可见选择"}));
  expect(screen.getByRole("button", {name:"预览批量修复（1 步）"})).toBeEnabled();
  fireEvent.click(screen.getByText(/已持久保存的修复任务/));
  fireEvent.click(screen.getByRole("button", {name:"查看原任务"}));
  expect(screen.getByRole("heading", {name:"已保存的真实修复任务"})).toBeVisible();
  expect(mockApi.submitPortfolioInspectionRepair).not.toHaveBeenCalled();
});

it("retains batch workspace filters and the explicit selection after leaving and mounting the data domain again",async()=>{
  const view=render(<InspectionRepairWorkspace portfolioId={1}/>);
  fireEvent.change(await screen.findByRole("textbox",{name:"搜索待修复标的"}),{target:{value:"TEST1"}});
  fireEvent.click(screen.getByRole("button",{name:"仅保留可见选择"}));
  view.unmount();
  render(<InspectionRepairWorkspace portfolioId={1}/>);
  expect(await screen.findByRole("textbox",{name:"搜索待修复标的"})).toHaveValue("TEST1");
  expect(screen.getByRole("button",{name:"预览批量修复（1 步）"})).toBeEnabled();
  expect(mockApi.submitPortfolioInspectionRepair).not.toHaveBeenCalled();
});

it("uses server-generated AI context and never executes arbitrary model suggestions", async () => {
  mockApi.analysePortfolioInspection.mockResolvedValue({response:{answer:"先核实上游输入",warnings:[],evidence:[{content:"日线尚未到达",source:"真实行情表"}],suggested_actions:[{action_type:"RUN_SQL",description:"不可信任的模型动作"}],metadata:{inspection_snapshot_id:"server-evidence",available_actions:[{action_type:"PREVIEW_DATA_REPAIR"}],unknowns:["缺少执行回报"]}}});
  openPage(); fireEvent.click(screen.getByRole("button", {name:"AI 辅助"})); fireEvent.click(screen.getByRole("button", {name:"分析当前阻断"}));
  expect(await screen.findByText("先核实上游输入")).toBeVisible();
  expect(mockApi.analysePortfolioInspection).toHaveBeenCalledWith(1, expect.not.objectContaining({message:expect.anything()}));
  expect(screen.queryByRole("button", {name:"查看依据与正式处理入口"})).not.toBeInTheDocument();
  expect(mockApi.submitPortfolioInspectionRepair).not.toHaveBeenCalled();
  await recheckCurrentEvidence();
  expect(await screen.findByText(/本次建议需要重新分析/)).toBeVisible();
});

it("does not revive an old AI suggestion that finishes after a newer same-portfolio inspection", async () => {
  let resolveAI!: (value: any) => void;
  mockApi.analysePortfolioInspection.mockReturnValue(new Promise(resolve => {resolveAI=resolve;}));
  openPage(); fireEvent.click(screen.getByRole("button", {name:"AI 辅助"})); fireEvent.click(screen.getByRole("button", {name:"分析当前阻断"}));
  await waitFor(() => expect(mockApi.analysePortfolioInspection).toHaveBeenCalledTimes(1));
  mockApi.getPortfolioInspectionDiagnostics.mockResolvedValue(diagnostics("newer-evidence"));
  await recheckCurrentEvidence();
  await waitFor(() => expect(mockApi.getPortfolioInspectionDiagnostics).toHaveBeenCalledTimes(2));
  await act(async () => resolveAI({response:{answer:"旧事实的分析",evidence:[],warnings:[],suggested_actions:[{action_type:"PREVIEW_DATA_REPAIR",description:"旧计划"}],metadata:{available_actions:[{action_type:"PREVIEW_DATA_REPAIR"}]}}}));
  expect(await screen.findByText(/本次建议需要重新分析/)).toBeVisible();
  expect(screen.getByRole("button", {name:"查看依据与正式处理入口"})).toBeDisabled();
});

it("keeps persisted task read and stop controls available when current diagnostics cannot be read", async () => {
  const saved={task_id:"active-saved",portfolio_id:1,status:"running",stage:"sync",percent:30,total:2,processed:0,ok_count:0,failed_count:0,message:"原任务仍在执行",objects:[],retryable_operation_ids:[],can_cancel:true};
  mockApi.getPortfolioInspectionRepairTasks.mockResolvedValue({portfolio_id:1,items:[saved],next_cursor:null});
  mockApi.cancelPortfolioInspectionRepair.mockResolvedValue({...saved,status:"cancelled",can_cancel:false,cancel_requested:true});
  render(<InspectionRepairWorkspace portfolioId={1}/>); await screen.findByRole("textbox",{name:"搜索待修复标的"});
  fireEvent.click(screen.getByRole("button", {name:"本批结果"}));
  expect(await screen.findByRole("heading", {name:"原任务仍在执行"})).toBeVisible();
  mockApi.getPortfolioInspectionDiagnostics.mockRejectedValue(new Error("当前诊断存储暂不可读"));
  fireEvent.click(screen.getByRole("button", {name:"重新读取数据诊断"}));
  expect(await screen.findByText("当前诊断存储暂不可读")).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name:"停止尚未开始的步骤"}));
  await waitFor(() => expect(mockApi.cancelPortfolioInspectionRepair).toHaveBeenCalledWith(1,"active-saved"));
});

it("aggregates multiple assets, preserves search across a history detour, and locates actual evidence IDs", async () => {
  openPage();
  fireEvent.click(await screen.findByRole("button", { name: "整合查看与批量处理" }));
  const search = screen.getByRole("textbox", { name: "搜索待核实标的" });
  fireEvent.change(search, { target: { value: "TEST1" } });
  fireEvent.click(screen.getByRole("button", { name: "查看标的证据" }));
  fireEvent.click(screen.getByRole("button", { name: "该标的相关历史处理" }));
  await waitFor(() => expect(mockApi.getPortfolioInspectionHistory).toHaveBeenCalledWith(1, expect.objectContaining({ issue_id: "1:daily_bar", security_id: ["CN:TEST1"], trade_date_from: "2026-10-21", trade_date_to: "2026-10-21" })));
  fireEvent.click(screen.getByRole("button", { name: "返回原问题处理" }));
  fireEvent.click(screen.getByRole("button", {name:"返回数据汇总"}));
  expect(screen.getByRole("textbox", { name: "搜索待核实标的" })).toHaveValue("TEST1");
  expect(screen.getByRole("button",{name:"查看标的证据"})).toBeVisible();
  expect(mockApi.submitPortfolioInspectionRepair).not.toHaveBeenCalled();
});

it("freezes the previewed batch when newer diagnostics arrive and preserves successful partial results", async () => {
  const view = render(<InspectionRepairWorkspace portfolioId={1}/>); await screen.findByRole("textbox",{name:"搜索待修复标的"});
  fireEvent.click(screen.getByRole("button", { name: "预览批量修复（2 步）" }));
  const dialog = await screen.findByRole("dialog");
  await waitFor(() => expect(within(dialog).getByText("确认本批数据修复计划")).toBeVisible());
  mockApi.getPortfolioInspectionDiagnostics.mockResolvedValue(diagnostics("test-plan-new"));
  fireEvent.click(screen.getByRole("button", { name: "重新读取数据诊断" }));
  await waitFor(() => expect(mockApi.getPortfolioInspectionDiagnostics).toHaveBeenCalledTimes(2));
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认执行本批计划" }));
  await waitFor(() => expect(mockApi.submitPortfolioInspectionRepair).toHaveBeenCalledWith(1, expect.objectContaining({ plan_id: "test-plan-1", snapshot_id: "snapshot-test-plan-1", operation_ids: ["sync-1", "sync-2"], confirmed: true })));
  expect(await screen.findByText("一项失败，成功步骤已保留")).toBeVisible();
  expect(screen.getByRole("button", { name: "仅重试失败与依赖受阻步骤" })).toBeEnabled();
  view.unmount();
});

it("retains a new partial task in saved history and explains only the failed source without stale waiting copy",async()=>{
  const reason="ETF前复权行情获取失败：行情服务连接中断，本次未写入该标的行情；未使用不复权数据替代。";
  mockApi.submitPortfolioInspectionRepair.mockImplementation(async(pid:number,request:any)=>{const result={task_id:await repairTaskId(pid,request.idempotency_key),portfolio_id:1,status:"partial",stage:"done",percent:100,total:2,processed:2,ok_count:1,failed_count:1,message:"部分对象未完成；成功产物已保留",retryable_operation_ids:["sync-2"],audit_refs:[],objects:[
    {operation_id:"sync-1",label:"补齐 平安银行 日K行情",status:"succeeded",reinspection_status:"PASSED"},
    {operation_id:"sync-2",label:"补齐 沪深300ETF 日K行情",symbol:"510300",status:"failed",reinspection_status:"STILL_BLOCKED",start_date:"2026-10-21",end_date:"2026-10-23",reason,source_label:"东方财富 · ETF前复权日K",next_action:"稍后仅重试本标的失败步骤，已成功的其他标的不必重跑。"},
  ]};savedRepairTasks.set(result.task_id,result);return result;});
  render(<InspectionRepairWorkspace portfolioId={1}/>);
  fireEvent.click(await screen.findByRole("button",{name:"预览批量修复（2 步）"}));
  const confirm=await screen.findByRole("dialog");
  fireEvent.click(within(confirm).getByRole("checkbox"));
  fireEvent.click(within(confirm).getByRole("button",{name:"确认执行本批计划"}));
  expect(await screen.findByText("已持久保存的修复任务 · 1 条")).toBeVisible();
  expect(screen.getByText(reason)).toBeVisible();
  expect(screen.queryByText("等待任务更新")).not.toBeInTheDocument();
  expect(screen.getByText("目标范围已重新核验，本步骤没有剩余数据缺口。")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"查看失败详情"}));
  const failure=await screen.findByRole("region",{name:"修复步骤失败详情"});
  expect(within(failure).getByText("东方财富 · ETF前复权日K")).toBeVisible();
  expect(within(failure).getByText(/稍后仅重试本标的/)).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"返回本批结果"}));
  expect(screen.getByRole("button",{name:"仅重试失败与依赖受阻步骤"})).toBeEnabled();
  fireEvent.click(screen.getByText("已持久保存的修复任务 · 1 条"));
  fireEvent.click(screen.getByRole("button",{name:"查看原任务"}));
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button",{name:"查看失败详情"}));
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button",{name:"返回原任务详情"}));
  expect(within(screen.getByRole("dialog")).getByRole("button",{name:"继续处理此原任务"})).toBeVisible();
  expect(mockApi.submitPortfolioInspectionRepair).toHaveBeenCalledTimes(1);
  expect(mockApi.retryPortfolioInspectionRepair).not.toHaveBeenCalled();
  expect(mockApi.transitionPortfolioState).not.toHaveBeenCalled();
});

it("shows unknown when runtime cannot be read and keeps recovery disabled", async () => {
  mockApi.getPortfolioInspectionRuntime.mockRejectedValue(new Error("临时读取失败"));
  openPage();
  expect(await screen.findByText("当前交易状态待核实")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "运行状态审核" }));
  fireEvent.click(screen.getByRole("button", { name: /本人审核恢复/ }));
  expect(screen.getByRole("button", { name: "审核并确认恢复" })).toBeDisabled();
  expect(mockApi.transitionPortfolioState).not.toHaveBeenCalled();
});

it("requires an explicit human reason and submits the report version actually reviewed", async () => {
  mockApi.getPortfolioInspectionRuntime.mockResolvedValue(runtime(true)); mockApi.previewPortfolioReconciliation.mockResolvedValue(report(true));
  openPage(); fireEvent.click(screen.getByRole("button", { name: "每日对账" }));
  fireEvent.click(await screen.findByRole("button", { name: "审核本次对账结果" }));
  const dialog = screen.getByRole("dialog");
  await waitFor(() => expect(within(dialog).getByText("审核并确认当前对账结果")).toBeVisible());
  expect(within(dialog).getByRole("button", { name: "确认本次审核" })).toBeDisabled();
  fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "已核对执行回报以及当前资金和持仓" } });
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认本次审核" }));
  await waitFor(() => expect(mockApi.confirmPortfolioReconciliation).toHaveBeenCalledWith(1, expect.objectContaining({ trade_date: "2026-10-21", report_id: "test-report", report_version: "evidence-v1", force_skip: false })));
});

it("keeps a complete manual repair path when AI is unavailable", async () => {
  mockApi.analysePortfolioInspection.mockRejectedValue(new Error("未配置 AI 接口")); openPage();
  fireEvent.click(screen.getByRole("button", { name: "AI 辅助" }));
  fireEvent.click(screen.getByRole("button", { name: "分析当前阻断" }));
  expect(await screen.findByText("未配置 AI 接口")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "开始引导修复" }));
  expect(await screen.findByRole("navigation", { name: "恢复运行步骤" })).toBeVisible();
  expect(mockApi.submitPortfolioInspectionRepair).not.toHaveBeenCalled();
});

function guidedRuntime(dataProof = "data-proof-1") {
  const value: any = runtime(true);
  value.recovery_summary.confirmation_action = "RESUME_ADMIN_PAUSE";
  value.guide = { schema_version:"inspection-guide-v1", snapshot_id:"a".repeat(64), can_confirm:true,
    steps:["reconciliation","data","binding","runtime"].map((key,i)=>({ key,label:key,status:i===3?"INCOMPLETE":i===2?"NOT_APPLICABLE":"PASSED",
      reason:value.recovery_summary.checks[i].reason,proof_hash:i===1?dataProof:`proof-${key}`,proof:{check:{trade_date:"2026-10-21",report_id:i===0?"real-report":undefined,report_version:"real-version"}},tasks:[],next_action:"OPEN_STEP"})),
    completion_proofs:[],effects:{target_state:"READY",current_permissions:value.permissions,historical_unknown_retained:true,force_recovery_enabled:false}};
  value.guide.completion_proofs=value.guide.steps.slice(0,3);
  return value;
}

it("renders server nonapplicability and reopens only the proof that changed",async()=>{
  mockApi.getPortfolioInspectionRuntime.mockResolvedValue(guidedRuntime());openPage();fireEvent.click(await screen.findByRole("button",{name:"开始恢复引导"}));
  expect(screen.getByRole("button",{name:/确认运行绑定.*本次不适用/})).toBeVisible();
  mockApi.getPortfolioInspectionRuntime.mockResolvedValue(guidedRuntime("data-proof-2"));await recheckCurrentEvidence();
  await waitFor(()=>expect(screen.getByRole("button",{name:/批量修复数据.*依据变化，需重检/})).toBeVisible());
  fireEvent.click(screen.getByRole("button",{name:/批量修复数据/}));expect(screen.getByText("本步来源或依据已变化")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"重新核实本步依据"}));await waitFor(()=>expect(screen.getByRole("button",{name:/批量修复数据.*已满足/})).toBeVisible());
  expect(mockApi.confirmPortfolioInspectionGuide).not.toHaveBeenCalled();
});

it("binds final human review to all three server proofs and keeps a conflicted reason",async()=>{
  mockApi.getPortfolioInspectionRuntime.mockResolvedValue(guidedRuntime());
  mockApi.confirmPortfolioInspectionGuide.mockRejectedValue(new Error("步骤依据已变化，请重新核实当前材料"));
  openPage();fireEvent.click(await screen.findByRole("button",{name:"开始恢复引导"}));fireEvent.click(screen.getByRole("button",{name:"审核并确认恢复"}));
  const title=await screen.findByText("本人审核并恢复当前范围");const dialog=title.closest('[role="dialog"]') as HTMLElement;
  fireEvent.click(within(dialog).getByText("查看本次审核引用的核验结果"));
  await waitFor(()=>expect(within(dialog).getByText("本次审核的完成证明")).toBeVisible());
  fireEvent.change(within(dialog).getByRole("textbox"),{target:{value:"本人核查本组合三项完成证明与当前适用范围后批准"}});
  fireEvent.click(within(dialog).getByRole("checkbox"));fireEvent.click(within(dialog).getByRole("button",{name:"确认本次审核"}));
  await waitFor(()=>expect(mockApi.confirmPortfolioInspectionGuide).toHaveBeenCalledWith(1,expect.objectContaining({expected_snapshot_id:"a".repeat(64),expected_action:"RESUME_ADMIN_PAUSE",acknowledge_current_proofs:true})));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("步骤依据已变化");expect(within(dialog).getByRole("textbox")).toHaveValue("本人核查本组合三项完成证明与当前适用范围后批准");
  expect(within(dialog).getByRole("checkbox")).not.toBeChecked();expect(mockApi.transitionPortfolioState).not.toHaveBeenCalled();
});

it("labels unfinished completion proofs as pending and separates a finished scan from waiting evidence",async()=>{
  const value=guidedRuntime(); value.guide.can_confirm=false; value.recovery_summary.can_confirm=false;
  value.guide.steps[0].status="INCOMPLETE"; value.guide.completion_proofs=value.guide.steps.slice(0,3);
  mockApi.getPortfolioInspectionRuntime.mockResolvedValue(value);
  mockApi.getPortfolioInspectionJob.mockResolvedValue({portfolio_id:1,enabled:true,status:"done",reason:"尚无已接受账户基线",
    next_run_at:null,task_id:"real-check-task",queue:null,check_summary:{passed_count:0,waiting_count:1,difference_count:0,error_count:0}});
  openPage();fireEvent.click(await screen.findByRole("button",{name:"开始恢复引导"}));
  fireEvent.click(screen.getByRole("button",{name:/本人审核恢复/}));
  expect(screen.getByRole("region",{name:"审核剩余问题"})).toHaveTextContent("执行对账");
  expect(screen.queryByText("查看本次审核的完成依据")).not.toBeInTheDocument();
  expect(screen.getByRole("button",{name:"审核并确认恢复"})).toBeDisabled();
  fireEvent.click(screen.getByRole("button",{name:"Close"}));
  fireEvent.click(screen.getByRole("button",{name:"查看作业"}));
  expect(await screen.findByText(/通过 0 · 等待材料 1/)).toBeVisible();
  expect(mockApi.confirmPortfolioInspectionGuide).not.toHaveBeenCalled();
});

it("combines multiple dates into one task and keeps the selected date when opening related history",async()=>{
  const dates=["2026-10-19","2026-10-21"];
  mockApi.getPortfolioActionItems.mockResolvedValue({items:dates.map(day=>({id:`day-${day}`,source:"reconciliation",family:`recon:1:legacy:0:${day}`,
    code:"EVIDENCE_INCOMPLETE",trade_date:day,title:`${day} 对账尚未完成`,impact:"核查真实来源",severity:"blocker"})),summary:{}});
  mockApi.getPortfolioInspectionQueue.mockResolvedValue({portfolio_id:1,coverage_start:null,verified_through:null,target_trade_date:dates[0],calendar_status:"KNOWN",limitations:[],
    days:dates.map(day=>({trade_date:day,status:"EVIDENCE_INCOMPLETE",reason:"核查真实来源"}))});
  mockApi.previewPortfolioReconciliation.mockImplementation((_pid:number,day?:string)=>Promise.resolve({...report(),trade_date:day??dates[0]}));
  mockApi.getPortfolioInspectionJob.mockResolvedValue({portfolio_id:1,enabled:true,reason:"按日核对",queue:{portfolio_id:1,days:dates.map(trade_date=>({trade_date,status:"EVIDENCE_INCOMPLETE",reason:"核查真实来源"})),limitations:[]}});
  openPage();expect(await screen.findByText("2 个交易日待核对")).toBeVisible();
  expect(screen.getAllByRole("button",{name:"每日对账"})).toHaveLength(1);
  fireEvent.click(screen.getByRole("button",{name:"查看作业"}));
  fireEvent.click(screen.getByText("逐日核对记录",{exact:true}));
  const row=(await screen.findByRole("cell",{name:dates[1]})).closest("tr") as HTMLElement;
  fireEvent.click(within(row).getByRole("button",{name:"核查此交易日"}));
  await waitFor(()=>expect(mockApi.previewPortfolioReconciliation).toHaveBeenCalledWith(1,dates[1]));
  fireEvent.click(await screen.findByRole("button",{name:"查看本日处理记录"}));
  await waitFor(()=>expect(mockApi.getPortfolioInspectionHistory).toHaveBeenCalledWith(1,expect.objectContaining({trade_date_from:dates[1],trade_date_to:dates[1]})));
});

it("opens a separate same-day quick check over the job and returns without reusing the underlying reconciliation drawer",async()=>{
  const queue={portfolio_id:1,days:[{trade_date:"2026-10-21",status:"EVIDENCE_INCOMPLETE",reason:"缺少当天材料"}],limitations:[]};
  mockApi.getPortfolioInspectionJob.mockResolvedValue({portfolio_id:1,enabled:true,reason:"按日核对",queue});mockApi.getPortfolioInspectionQueue.mockResolvedValue(queue);
  openPage();fireEvent.click(await screen.findByRole("button",{name:"每日对账"}));
  fireEvent.click(screen.getByRole("button",{name:"自动核对作业"}));
  const job=screen.getByRole("heading",{name:"自动对账作业与逐日记录"}).closest('[role="dialog"]') as HTMLElement;
  const trigger=await within(job).findByRole("button",{name:"核查 2026-10-21"});
  let finish!: (value:ReturnType<typeof report>)=>void;
  mockApi.previewPortfolioReconciliation.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
  trigger.focus();fireEvent.click(trigger);
  const quick=within(document.querySelector('.pi-quick-check-root') as HTMLElement).getByRole("dialog");
  await waitFor(()=>expect(within(quick).getByText("正在读取本日核对材料…")).toBeVisible());
  expect(job).toBeVisible();
  expect(screen.queryByRole("combobox",{name:"核对交易日"})).not.toBeInTheDocument();
  finish({...report(),trade_date:"2026-10-21"});
  await within(quick).findByText("历史执行资料不完整");
  expect(mockApi.previewPortfolioReconciliation).toHaveBeenLastCalledWith(1,"2026-10-21");
  fireEvent.click(within(quick).getByRole("button",{name:/^关\s*闭$/}));
  await waitFor(()=>expect(trigger).toHaveFocus());
  expect(within(job).getByRole("heading",{name:"自动对账作业与逐日记录"})).toBeVisible();
  fireEvent.click(screen.getByText("逐日核对记录",{exact:true}));
  fireEvent.click(screen.getByRole("button",{name:"核查此交易日"}));
  const next=within(document.querySelector('.pi-quick-check-root') as HTMLElement).getByRole("dialog");
  await within(next).findByText("历史执行资料不完整");
  fireEvent.click(within(next).getByRole("button",{name:/^关\s*闭$/}));
  expect(screen.getByRole("button",{name:"核查此交易日"})).toBeVisible();
  fireEvent.click(within(job).getByRole("button",{name:"Close"}));
  expect(screen.getByRole("combobox",{name:"核对交易日"})).toBeInTheDocument();
  expect(mockApi.confirmPortfolioReconciliation).not.toHaveBeenCalled();
});
