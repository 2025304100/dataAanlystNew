import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import InspectionEvidencePanel from "../InspectionEvidencePanel";
import {INSPECTION_CHANGE_KEY} from "../../../../utils/inspectionFreshness";
const {api}=vi.hoisted(()=>({api:{getPortfolioEvidenceSupplements:vi.fn(),submitPortfolioEvidenceBatch:vi.fn(),reviewPortfolioEvidenceSupplement:vi.fn()}}));
vi.mock("../../../../api/client",()=>({api}));
const sourceHash="a".repeat(64);
const candidate=(n:number)=>({candidate_id:`order:${n}:evidence:ev-${n}`,source_hash:sourceHash,order_id:n,evidence_id:`ev-${n}`,run_id:"real-run",symbol_id:n,
  symbol:`TEST${n}`,name:`隔离标的${n}`,market:"CN",trade_date:"2026-10-20",execution_date:"2026-10-21",order_status:"filled",quantity:100,filled_quantity:100,
  fill_count:1,ledger_count:1,match_reasons:["原订单保留确切计划键"],effect:"只补关联，仍需重检"});
const pending={id:"retained-request",candidate_id:candidate(1).candidate_id,source_hash:sourceHash,order_id:1,created_by:"human",created_at:"2026-10-23",note:"本人已核查原计划与实际订单来源",decision:null,reviewed_by:null,review_note:null,status:"PENDING_REVIEW",usable:false};
const page=()=>({portfolio_id:1,trade_date:"2026-10-20",baseline_id:"retained-baseline",generation:1,candidates:[candidate(1),candidate(2)],candidate_count:2,requests:[],limitations:[]});
beforeEach(()=>{vi.resetAllMocks();api.getPortfolioEvidenceSupplements.mockResolvedValue(page());api.submitPortfolioEvidenceBatch.mockResolvedValue({items:[{request_id:"one"},{request_id:"two"}]});});
afterEach(cleanup);
function show(extra={}){const onChanged=vi.fn(async()=>{});const onRecheck=vi.fn(async()=>{});const onHistory=vi.fn();const onBack=vi.fn();const onConfirmationChange=vi.fn();
  const view=render(<InspectionEvidencePanel portfolioId={1} tradeDate="2026-10-20" visible onChanged={onChanged} onRecheck={onRecheck} onHistory={onHistory} onBack={onBack} onConfirmationChange={onConfirmationChange} confirmationReady {...extra}/>);
  return{...view,onChanged,onRecheck,onBack,onConfirmationChange};}

it("cancels a frozen association review on a cross-tab update and needs fresh source selection",async()=>{
  show();await screen.findByText("隔离标的1 · TEST1");
  fireEvent.change(screen.getByRole("textbox",{name:"来源核查说明"}),{target:{value:"保留已经核对的原始订单来源说明"}});
  fireEvent.click(screen.getByRole("checkbox",{name:"选择当前交易日全部候选"}));fireEvent.click(screen.getByRole("button",{name:/预览登记所选补证/}));
  await waitFor(()=>expect(screen.getByRole("dialog")).toBeVisible());
  fireEvent(window,new StorageEvent("storage",{key:`${INSPECTION_CHANGE_KEY}1`,newValue:JSON.stringify({version:1,id:crypto.randomUUID(),source:"remote-evidence",portfolioId:1,at:Date.now()})}));
  expect(await screen.findByTestId("inspection-stale-notice")).toBeVisible();expect(screen.queryByText("隔离标的1 · TEST1")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"重新读取当前依据"}));expect(await screen.findByText("隔离标的1 · TEST1")).toBeVisible();
  expect(screen.getByRole("textbox",{name:"来源核查说明"})).toHaveValue("保留已经核对的原始订单来源说明");
  expect(api.submitPortfolioEvidenceBatch).not.toHaveBeenCalled();expect(api.reviewPortfolioEvidenceSupplement).not.toHaveBeenCalled();
});

it("ignores a late failed read after a newer source read succeeds",async()=>{
  let fail!:(error:Error)=>void;api.getPortfolioEvidenceSupplements.mockImplementationOnce(()=>new Promise((_resolve,reject)=>{fail=reject;}));
  const props={portfolioId:1,tradeDate:"2026-10-20",onChanged:vi.fn(async()=>{}),onRecheck:vi.fn(async()=>{}),onHistory:vi.fn(),onBack:vi.fn(),onConfirmationChange:vi.fn(),confirmationReady:true};
  const view=render(<InspectionEvidencePanel {...props} visible/>);await waitFor(()=>expect(api.getPortfolioEvidenceSupplements).toHaveBeenCalledTimes(1));
  view.rerender(<InspectionEvidencePanel {...props} visible={false}/>);view.rerender(<InspectionEvidencePanel {...props} visible/>);
  await screen.findByText("隔离标的1 · TEST1");await act(async()=>fail(new Error("旧请求延迟失败")));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();expect(screen.getByText("隔离标的1 · TEST1")).toBeVisible();
});

it("does not end the newer loading state when an older read finishes",async()=>{
  let finishOld!:(value:ReturnType<typeof page>)=>void,finishNew!:(value:ReturnType<typeof page>)=>void;
  api.getPortfolioEvidenceSupplements.mockImplementationOnce(()=>new Promise(resolve=>{finishOld=resolve;})).mockImplementationOnce(()=>new Promise(resolve=>{finishNew=resolve;}));
  const props={portfolioId:1,tradeDate:"2026-10-20",onChanged:vi.fn(async()=>{}),onRecheck:vi.fn(async()=>{}),onHistory:vi.fn(),onBack:vi.fn(),onConfirmationChange:vi.fn(),confirmationReady:true};
  const view=render(<InspectionEvidencePanel {...props} visible/>);await waitFor(()=>expect(api.getPortfolioEvidenceSupplements).toHaveBeenCalledTimes(1));
  view.rerender(<InspectionEvidencePanel {...props} visible={false}/>);view.rerender(<InspectionEvidencePanel {...props} visible/>);await waitFor(()=>expect(api.getPortfolioEvidenceSupplements).toHaveBeenCalledTimes(2));
  await act(async()=>finishOld(page()));expect(screen.getByRole("button",{name:/刷新实际补证来源/})).toHaveClass("ant-btn-loading");
  await act(async()=>finishNew(page()));expect(await screen.findByText("隔离标的1 · TEST1")).toBeVisible();
});

it("clears old candidates when the current read fails and recovers with a read-only retry",async()=>{
  show();await screen.findByText("隔离标的1 · TEST1");api.getPortfolioEvidenceSupplements.mockRejectedValueOnce(new Error("当前来源不可读"));
  fireEvent.click(await screen.findByRole("button",{name:"刷新实际补证来源"}));await screen.findByText("当前来源不可读");expect(screen.queryByText("隔离标的1 · TEST1")).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button",{name:"刷新实际补证来源"}));expect(await screen.findByText("隔离标的1 · TEST1")).toBeVisible();expect(api.submitPortfolioEvidenceBatch).not.toHaveBeenCalled();
});

it("previews a multi-order batch and sends the exact source hashes only after acknowledgement",async()=>{
  const view=show();await screen.findByText("隔离标的1 · TEST1");fireEvent.click(screen.getByLabelText("选择当前交易日全部候选"));
  fireEvent.change(screen.getByLabelText("来源核查说明"),{target:{value:"本人核实两笔原订单与确切计划来源，登记补证"}});
  fireEvent.click(screen.getByRole("button",{name:"预览登记所选补证（2 笔）"}));const dialog=await screen.findByRole("dialog");
  expect(api.submitPortfolioEvidenceBatch).not.toHaveBeenCalled();expect(within(dialog).getByRole("button",{name:"登记补证申请"})).toBeDisabled();
  fireEvent.click(within(dialog).getByRole("checkbox"));fireEvent.click(within(dialog).getByRole("button",{name:"登记补证申请"}));
  await waitFor(()=>expect(api.submitPortfolioEvidenceBatch).toHaveBeenCalledWith(1,expect.objectContaining({trade_date:"2026-10-20",
    items:[{candidate_id:candidate(1).candidate_id,expected_source_hash:sourceHash},{candidate_id:candidate(2).candidate_id,expected_source_hash:sourceHash}]})));
  await waitFor(()=>expect(view.onChanged).toHaveBeenCalledTimes(1));expect(view.onRecheck).not.toHaveBeenCalled();
});

it("keeps source-conflict drafts and never reports a successful association",async()=>{
  api.submitPortfolioEvidenceBatch.mockRejectedValue(new Error("实际来源已变化，请重新核查"));show();await screen.findByText("隔离标的1 · TEST1");
  fireEvent.click(screen.getByLabelText("选择当前交易日全部候选"));fireEvent.change(screen.getByLabelText("来源核查说明"),{target:{value:"核对原始订单和原计划来源的说明需要保留"}});
  fireEvent.click(screen.getByRole("button",{name:"预览登记所选补证（2 笔）"}));const dialog=await screen.findByRole("dialog");fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button",{name:"登记补证申请"}));expect(await within(dialog).findByRole("alert")).toHaveTextContent("实际来源已变化");
  expect(within(dialog).getByRole("checkbox")).not.toBeChecked();fireEvent.click(within(dialog).getByRole("button",{name:/取\s*消/}));
  expect(screen.getByLabelText("来源核查说明")).toHaveValue("核对原始订单和原计划来源的说明需要保留");
});

it("reviews a persisted request with a human reason then offers the real recheck",async()=>{
  api.getPortfolioEvidenceSupplements.mockResolvedValue({...page(),requests:[pending]});api.reviewPortfolioEvidenceSupplement.mockResolvedValue({recheck_required:true,trading_resumed:false});
  const view=show();fireEvent.click(await screen.findByRole("button",{name:"审核关联这笔原订单"}));const dialog=await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("补证审核理由"),{target:{value:"本人核实确切原订单与原决策和实际流水，批准关联"}});fireEvent.click(within(dialog).getByRole("checkbox"));fireEvent.click(within(dialog).getByRole("button",{name:"保存本次审核"}));
  await waitFor(()=>expect(api.reviewPortfolioEvidenceSupplement).toHaveBeenCalledWith(1,"retained-request",expect.objectContaining({decision:"approve",expected_source_hash:sourceHash,acknowledge_scope:true})));
  await waitFor(()=>expect(view.onChanged).toHaveBeenCalledTimes(1));fireEvent.click(screen.getByRole("button",{name:"重跑关联日核对"}));expect(view.onRecheck).toHaveBeenCalledTimes(1);
});

it("shows unknown evidence instead of inviting a manual source ID",async()=>{
  api.getPortfolioEvidenceSupplements.mockResolvedValue({...page(),candidates:[],candidate_count:0});const view=show();
  expect(await screen.findByText("尚未找到满足条件的自动执行来源")).toBeVisible();expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"返回执行与对账"}));expect(view.onBack).toHaveBeenCalled();
});
