import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import InspectionHistoryPanel from "../InspectionHistoryPanel";
import {writeInspectionBrowse} from "../inspectionBrowseState";
import type { HistoryBatch, HistoryDetail, HistoryMaterial, HistoryObject, HistoryPage, InspectionHistoryApi } from "../historyTypes";

afterEach(()=>{cleanup();sessionStorage.clear();});

const object = (id: string, result = "failed"): HistoryObject => ({
  object_id: id, security_id: `CN:${id}`, code: id, name: id === "000001" ? "平安银行" : "浦发银行",
  name_source: "audit", trade_date: "2026-10-19", security_index_status: "known", trade_date_index_status: "known",
  result: result as HistoryObject["result"], source_event_ids: [1], references: [],
});
const batch = (id = "audit-1", title = "真实数据阻断处理", pid = 1): HistoryBatch => ({
  batch_id: id, portfolio_id: pid, title, occurred_at: "2026-10-22T10:30:20+08:00", occurred_from: "2026-10-22T10:30:20+08:00",
  display_timezone: "Asia/Shanghai", operators: ["reviewer-1"], actions: ["DATA_BLOCK_RESOLUTION"],
  review_decision: "approved", execution_result: "failed", event_count: 2,
  total_object_count: 2, matched_object_count: 1, matched_security_count: 1,
  objects: [object("000001"), object("600000", "dependency_blocked")], matched_objects: [object("000001")],
  references: [], index_warnings: [], read_only: true,
});
const page = (items = [batch()], extra: Partial<HistoryPage> = {}): HistoryPage => ({
  items, total: items.length, page: 1, page_size: 20, page_count: 1, has_more: false, snapshot_event_id: 8,
  filters: {}, relation_status: "not_requested", read_only: true, ...extra,
});
const detail = (): HistoryDetail => ({ ...batch(), references: [{ kind: "report", ref: "original-report", version: "v2",
  retention_status: "archived", source_event_id: 1, read_only: true }], events: [{
  id: 1, action: "DATA_BLOCK_RESOLUTION", action_label: "原始审核", occurred_at: "2026-10-22T10:30:20+08:00",
  operator_id: "reviewer-1", before: { portfolio_state: "DATA_BLOCKED" }, after: { portfolio_state: "DATA_BLOCKED" },
  attributes: { review_note: "当时同意修复但后续任务失败" }, review_decision: "approved", execution_result: "unknown", references: [], material_warnings: [],
}] });
function fixture(): InspectionHistoryApi {
  return {
    getPortfolioInspectionHistory: vi.fn().mockResolvedValue(page()),
    getPortfolioInspectionHistorySecurities: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    getPortfolioInspectionHistoryDetail: vi.fn().mockResolvedValue(detail()),
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => { resolve = res; });
  return { promise, resolve };
}

it("retries all restored history pages after a later hydration page fails",async()=>{
  writeInspectionBrowse(1,"history:{}",{draft:{securityIds:[],tradeFrom:"2026-09-30",tradeTo:"",occurredFrom:"",occurredTo:"",actions:[],operator:"",keyword:"",actorType:"",failureCode:""},applied:{trade_date_from:"2026-09-30"},pages:2,related:false,more:false,endPinned:false,detailBatch:null,materialReference:null});
  const historyApi=fixture();let secondAttempts=0;
  vi.mocked(historyApi.getPortfolioInspectionHistory).mockImplementation(async(_pid,query)=>{if(query?.page===2){if(++secondAttempts===1)throw new Error("恢复第二页失败");return page([batch("audit-2","恢复的第二页")],{page:2,total:2});}return page([batch()],{total:2,has_more:true});});
  render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi}/>);await screen.findByText("恢复第二页失败");fireEvent.click(screen.getByRole("button",{name:"保留筛选重试"}));
  expect(await screen.findByRole("button",{name:/恢复的第二页/})).toBeVisible();expect(secondAttempts).toBe(2);
});

it("restores each history context's draft and applied filters after visiting another object's history",async()=>{
  const historyApi=fixture();
  const view=render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi}/>);
  await screen.findByRole("button",{name:/真实数据阻断处理/});
  fireEvent.change(screen.getByLabelText("所属交易日起点"),{target:{value:"2026-09-30"}});
  fireEvent.click(screen.getByRole("button",{name:"查询"}));
  await waitFor(()=>expect(historyApi.getPortfolioInspectionHistory).toHaveBeenLastCalledWith(1,expect.objectContaining({trade_date_from:"2026-09-30"})));
  fireEvent.change(screen.getByLabelText("所属交易日终点"),{target:{value:"2026-10-23"}});
  view.rerender(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} context={{security_ids:["CN:000001"],trade_date:"2026-10-19"}}/>);
  await waitFor(()=>expect(screen.getByLabelText("所属交易日起点")).toHaveValue("2026-10-19"));
  view.rerender(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi}/>);
  await waitFor(()=>expect(screen.getByLabelText("所属交易日起点")).toHaveValue("2026-09-30"));
  expect(screen.getByLabelText("所属交易日终点")).toHaveValue("2026-10-23");
  // The unsubmitted end date is retained without silently applying it.
  await waitFor(()=>expect(historyApi.getPortfolioInspectionHistory).toHaveBeenLastCalledWith(1,expect.objectContaining({trade_date_from:"2026-09-30",trade_date_to:undefined})));
});

it("keeps loaded history pages on reopen when the event watermark is unchanged",async()=>{
  const historyApi=fixture();
  vi.mocked(historyApi.getPortfolioInspectionHistory).mockImplementation(async(_pid,query)=>query?.page===2?page([batch("audit-2","第二页批次")],{page:2,total:2}):page([batch()],{total:2,has_more:true}));
  const view=render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi}/>);
  fireEvent.click(await screen.findByRole("button",{name:"加载更多历史批次"}));
  await screen.findByRole("button",{name:/第二页批次/});
  view.rerender(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} visible={false}/>);
  view.rerender(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} visible/>);
  await waitFor(()=>expect(historyApi.getPortfolioInspectionHistory).toHaveBeenCalledTimes(3));
  expect(screen.getByRole("button",{name:/第二页批次/})).toBeVisible();
  expect(screen.queryByRole("button",{name:"加载更多历史批次"})).not.toBeInTheDocument();
});

it("restores filters and loaded pages after a remount by reading every page at a fresh watermark",async()=>{
  const historyApi=fixture();
  vi.mocked(historyApi.getPortfolioInspectionHistory).mockImplementation(async(_pid,query)=>query?.page===2?page([batch("audit-2","保留的第二页")],{page:2,total:2,snapshot_event_id:9}):page([batch()],{total:2,has_more:true,snapshot_event_id:9}));
  const first=render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi}/>);
  await screen.findByRole("button",{name:"加载更多历史批次"});
  fireEvent.change(screen.getByLabelText("所属交易日起点"),{target:{value:"2026-09-30"}});fireEvent.click(screen.getByRole("button",{name:"查询"}));
  await waitFor(()=>expect(historyApi.getPortfolioInspectionHistory).toHaveBeenLastCalledWith(1,expect.objectContaining({trade_date_from:"2026-09-30"})));
  fireEvent.click(await screen.findByRole("button",{name:"加载更多历史批次"}));await screen.findByRole("button",{name:/保留的第二页/});first.unmount();
  vi.mocked(historyApi.getPortfolioInspectionHistory).mockClear();
  render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi}/>);
  expect(screen.getByLabelText("所属交易日起点")).toHaveValue("2026-09-30");await screen.findByRole("button",{name:/保留的第二页/});
  expect(historyApi.getPortfolioInspectionHistory).toHaveBeenNthCalledWith(1,1,expect.objectContaining({page:1,snapshot_event_id:undefined,trade_date_from:"2026-09-30"}));
  expect(historyApi.getPortfolioInspectionHistory).toHaveBeenNthCalledWith(2,1,expect.objectContaining({page:2,snapshot_event_id:9}));
});

it("restores a material view by re-reading its exact reference instead of caching the material",async()=>{
  const historyApi=fixture();
  historyApi.getPortfolioInspectionHistoryMaterial=vi.fn().mockResolvedValue({portfolio_id:1,reference:detail().references[0],status:"missing",material_kind:"report",title:"原始报告",explanation:"原材料未保留",data:{},original_audit:null,missing_sources:[],read_only:true});
  const first=render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi}/>);
  fireEvent.click(await screen.findByRole("button",{name:/真实数据阻断处理/}));fireEvent.click(await screen.findByRole("button",{name:/原报告：.*original-report/}));
  await screen.findByLabelText("历史引用只读材料");first.unmount();vi.mocked(historyApi.getPortfolioInspectionHistoryMaterial).mockClear();
  render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi}/>);await screen.findByLabelText("历史引用只读材料");
  expect(historyApi.getPortfolioInspectionHistoryMaterial).toHaveBeenCalledWith(1,detail().references[0],8);
});

it("opens batch details and original material in separate overlays while retaining the list's applied dates",async()=>{
  const historyApi=fixture();
  historyApi.getPortfolioInspectionHistoryMaterial=vi.fn().mockResolvedValue({portfolio_id:1,reference:detail().references[0],status:"missing",material_kind:"report",title:"原始报告",explanation:"原文件缺失，旧审计保留",data:{},original_audit:null,missing_sources:["原文件"],read_only:true});
  render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} context={{trade_date:"2026-10-19"}}/>);
  const title=await screen.findByRole("button",{name:/真实数据阻断处理/});
  const card=title.closest("article")!;
  fireEvent.click(title);
  const record=screen.getByRole("dialog");
  expect(await within(record).findByText("当时理由：当时同意修复但后续任务失败")).toBeVisible();
  expect(card.querySelector(".pi-history-detail")).toBeNull();
  fireEvent.click(within(record).getByRole("button",{name:/原报告.*original-report/}));
  expect(await screen.findByLabelText("历史引用只读材料")).toHaveTextContent("确切来源缺失");
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button",{name:"返回处置详情并保留位置"}));
  fireEvent.click(screen.getByRole("button",{name:"返回历史列表并保留筛选"}));
  expect(screen.getByLabelText("所属交易日起点")).toHaveValue("2026-10-19");
  expect(screen.getByLabelText("所属交易日终点")).toHaveValue("2026-10-19");
});

it("shows approved and failed separately, retains full batch and original report, and returns to the issue", async () => {
  const historyApi = fixture(), onReturn = vi.fn();
  render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} onReturn={onReturn}
    context={{ issue_id: "issue-1", security_ids: ["CN:000001", "CN:600000"], trade_date: "2026-10-19", label: "数据阻断" }} />);
  expect(await screen.findByText("真实数据阻断处理")).toBeVisible();
  expect(screen.getByText("同意")).toBeVisible();
  expect(screen.getAllByText("失败").length).toBeGreaterThan(0);
  expect(historyApi.getPortfolioInspectionHistory).toHaveBeenCalledWith(1, expect.objectContaining({
    issue_id: "issue-1", security_id: ["CN:000001", "CN:600000"], trade_date_from: "2026-10-19", trade_date_to: "2026-10-19",
  }));
  fireEvent.click(screen.getByRole("button", { name: /真实数据阻断处理/ }));
  expect(await screen.findByText("当时理由：当时同意修复但后续任务失败")).toBeVisible();
  expect(screen.getByText("original-report")).toBeVisible();
  fireEvent.click(screen.getByText("本批完整对象（2 个，含未命中对象）"));
  expect(screen.getByText("浦发银行")).toBeVisible();
  expect(screen.getByText(/材料状态：已归档/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "返回当前问题" }));
  expect(onReturn).toHaveBeenCalledTimes(1);
});

it("keeps both time meanings and clears only structured relation conditions", async () => {
  const historyApi = fixture(), onClearRelation = vi.fn();
  render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} onClearRelation={onClearRelation}
    context={{ issue_id: "issue-1", security_ids: ["CN:000001"], trade_date: "2026-09-19" }} />);
  await screen.findByText("真实数据阻断处理");
  fireEvent.change(screen.getByLabelText("发生时间起点"), { target: { value: "2026-10-21T08:00" } });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() => expect(historyApi.getPortfolioInspectionHistory).toHaveBeenLastCalledWith(1, expect.objectContaining({
    trade_date_from: "2026-09-19", occurred_from: "2026-10-21T08:00:00+08:00", issue_id: "issue-1",
  })));
  fireEvent.click(screen.getByRole("button", { name: "清除关联筛选" }));
  await waitFor(() => expect(historyApi.getPortfolioInspectionHistory).toHaveBeenLastCalledWith(1, expect.objectContaining({
    security_id: ["CN:000001"], trade_date_from: "2026-09-19", occurred_from: "2026-10-21T08:00:00+08:00",
  })));
  const calls = vi.mocked(historyApi.getPortfolioInspectionHistory).mock.calls;
  const query = calls[calls.length - 1]?.[1];
  expect(query?.issue_id).toBeUndefined(); expect(onClearRelation).toHaveBeenCalledTimes(1);
});

it("appends batches with stable event watermark and deduplicates batch IDs", async () => {
  const historyApi = fixture();
  vi.mocked(historyApi.getPortfolioInspectionHistory)
    .mockResolvedValueOnce(page([batch()], { total: 2, has_more: true, page_count: 2 }))
    .mockResolvedValueOnce(page([batch(), batch("audit-2", "第二个真实批次")], { total: 2, page: 2 }));
  render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} />);
  fireEvent.click(await screen.findByRole("button", { name: "加载更多历史批次" }));
  expect(await screen.findByText("第二个真实批次")).toBeVisible();
  expect(screen.getAllByText("真实数据阻断处理")).toHaveLength(1);
  expect(historyApi.getPortfolioInspectionHistory).toHaveBeenLastCalledWith(1, expect.objectContaining({ page: 2, snapshot_event_id: 8 }));
});

it("refreshes new material records on return without retaining the old page watermark or losing pinned filters", async () => {
  const historyApi=fixture(), clock=vi.spyOn(Date,"now").mockReturnValue(Date.parse("2026-10-22T02:30:00Z"));
  const context={report_ref:"reviewed-report",report_version:"v1",trade_date:"2026-09-29",security_ids:["CN:000001"]};
  try {
    vi.mocked(historyApi.getPortfolioInspectionHistory).mockResolvedValueOnce(page([batch()],{has_more:true}));
    const view=render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} context={context} visible/>);
    fireEvent.click(await screen.findByRole("button",{name:"加载更多历史批次"}));
    await waitFor(()=>expect(historyApi.getPortfolioInspectionHistory).toHaveBeenCalledTimes(2));
    await screen.findByText("真实数据阻断处理");
    view.rerender(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} context={context} visible={false}/>);
    clock.mockReturnValue(Date.parse("2026-10-22T02:32:00Z"));
    vi.mocked(historyApi.getPortfolioInspectionHistory).mockResolvedValue(page([batch("audit-2","新登记的材料核查")],{snapshot_event_id:9}));
    view.rerender(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} context={context} visible/>);
    expect(await screen.findByText("新登记的材料核查")).toBeVisible();
    expect(historyApi.getPortfolioInspectionHistory).toHaveBeenLastCalledWith(1,expect.objectContaining({page:1,snapshot_event_id:undefined,report_ref:"reviewed-report",report_version:"v1",trade_date_from:"2026-09-29",security_id:["CN:000001"],occurred_to:"2026-10-22T10:32:59+08:00"}));
    fireEvent.change(screen.getByLabelText("发生时间终点"),{target:{value:"2026-10-22T10:31"}});
    fireEvent.click(screen.getByRole("button",{name:"查询"}));
    await waitFor(()=>expect(historyApi.getPortfolioInspectionHistory).toHaveBeenLastCalledWith(1,expect.objectContaining({occurred_to:"2026-10-22T10:31:59+08:00"})));
    view.rerender(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} context={context} visible={false}/>);
    clock.mockReturnValue(Date.parse("2026-10-22T02:34:00Z"));
    view.rerender(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} context={context} visible/>);
    await waitFor(()=>expect(historyApi.getPortfolioInspectionHistory).toHaveBeenCalledTimes(5));
    expect(historyApi.getPortfolioInspectionHistory).toHaveBeenLastCalledWith(1,expect.objectContaining({occurred_to:"2026-10-22T10:31:59+08:00"}));
  } finally {clock.mockRestore();}
});

it("distinguishes a failed read from an empty related query and retries without clearing filters", async () => {
  const historyApi = fixture();
  vi.mocked(historyApi.getPortfolioInspectionHistory).mockRejectedValueOnce(new Error("数据库暂不可用"))
    .mockResolvedValueOnce(page([], { relation_status: "filtered_out", empty_reason: "当前筛选无匹配记录" }));
  render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} context={{ issue_id: "issue-1", trade_date: "2026-10-19" }} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("历史记录读取失败");
  expect(screen.queryByText("当前问题暂无关联历史")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "保留筛选重试" }));
  expect(await screen.findByText("当前筛选无匹配记录")).toBeVisible();
  expect(historyApi.getPortfolioInspectionHistory).toHaveBeenLastCalledWith(1, expect.objectContaining({ issue_id: "issue-1", trade_date_from: "2026-10-19" }));
});

it("ignores a late response from a previous portfolio and clears its query references", async () => {
  const historyApi = fixture(), old = deferred<HistoryPage>();
  vi.mocked(historyApi.getPortfolioInspectionHistory).mockImplementation(pid => pid === 1 ? old.promise : Promise.resolve(page([batch("audit-2", "组合二记录", 2)])));
  const view = render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} context={{ issue_id: "old-issue" }} />);
  view.rerender(<InspectionHistoryPanel portfolioId={2} historyApi={historyApi} />);
  expect(await screen.findByText("组合二记录")).toBeVisible();
  await act(async () => old.resolve(page([batch("audit-1", "组合一旧材料")])));
  expect(screen.queryByText("组合一旧材料")).not.toBeInTheDocument();
  expect(screen.queryByText(/old-issue/)).not.toBeInTheDocument();
  const calls = vi.mocked(historyApi.getPortfolioInspectionHistory).mock.calls;
  expect(calls[calls.length - 1]?.[1].issue_id).toBeUndefined();
});

it("opens the exact original reference in place with honest missing-source status", async () => {
  const historyApi = fixture();
  historyApi.getPortfolioInspectionHistoryMaterial = vi.fn().mockResolvedValue({ portfolio_id: 1,
    reference: detail().references[0], status: "partial", material_kind: "audit_snapshot", title: "原报告索引及当时审计快照",
    explanation: "完整原报告未保留，当时决定不变。", data: { report_version: "v2", verdict: "EVIDENCE_INCOMPLETE" },
    missing_sources: ["完整原报告 original-report / 版本 v2"], original_audit: { review_note: "原始理由" }, read_only: true });
  render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} />);
  fireEvent.click(await screen.findByRole("button", { name: /真实数据阻断处理/ }));
  fireEvent.click(await screen.findByRole("button", { name: /原报告：.*original-report/ }));
  expect(await screen.findByLabelText("历史引用只读材料")).toHaveTextContent("原始材料不完整");
  expect(screen.getByText("完整原报告未保留，当时决定不变。")).toBeVisible();
  expect(historyApi.getPortfolioInspectionHistoryMaterial).toHaveBeenCalledWith(1, detail().references[0], 8);
  expect(screen.getByText(/未知不会填成通过/)).toBeVisible();
});

it("keeps loaded batches when append fails and retries the same page at the same watermark", async () => {
  const historyApi = fixture();
  vi.mocked(historyApi.getPortfolioInspectionHistory)
    .mockResolvedValueOnce(page([batch()], { total: 2, has_more: true, page_count: 2 }))
    .mockRejectedValueOnce(new Error("第二页读取失败"))
    .mockResolvedValueOnce(page([batch("audit-2", "第二页恢复")], { total: 2, page: 2 }));
  render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} />);
  fireEvent.click(await screen.findByRole("button", { name: "加载更多历史批次" }));
  expect(await screen.findByText("第二页读取失败")).toBeVisible();
  expect(screen.getByText("真实数据阻断处理")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "保留筛选重试" }));
  expect(await screen.findByText("第二页恢复")).toBeVisible();
  expect(screen.getByText("真实数据阻断处理")).toBeVisible();
  expect(historyApi.getPortfolioInspectionHistory).toHaveBeenLastCalledWith(1, expect.objectContaining({ page: 2, snapshot_event_id: 8 }));
});

it("does not show late material from a different portfolio", async () => {
  const historyApi = fixture(), old = deferred<HistoryMaterial>();
  historyApi.getPortfolioInspectionHistoryMaterial = vi.fn().mockReturnValue(old.promise);
  const view = render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} />);
  fireEvent.click(await screen.findByRole("button", { name: /真实数据阻断处理/ }));
  fireEvent.click(await screen.findByRole("button", { name: /原报告：.*original-report/ }));
  vi.mocked(historyApi.getPortfolioInspectionHistory).mockResolvedValue(page([batch("audit-2", "新组合记录", 2)]));
  view.rerender(<InspectionHistoryPanel portfolioId={2} historyApi={historyApi} />);
  await screen.findByText("新组合记录");
  await act(async () => old.resolve({ portfolio_id: 1, reference: detail().references[0], status: "available", material_kind: "immutable_report", title: "上个组合原报告", explanation: "旧材料", data: {}, original_audit: {}, missing_sources: [], read_only: true }));
  expect(screen.queryByText("上个组合原报告")).not.toBeInTheDocument();
});

it("exports all matching evidence using the active watermark and reports failures separately", async () => {
  const historyApi = fixture();
  historyApi.exportPortfolioInspectionHistory = vi.fn().mockRejectedValue(new Error("导出服务不可用"));
  render(<InspectionHistoryPanel portfolioId={1} historyApi={historyApi} context={{ issue_id: "real-issue" }} />);
  await screen.findByText("真实数据阻断处理");
  fireEvent.click(screen.getByRole("button", { name: "导出当前筛选证据包" }));
  expect(await screen.findByText(/证据包导出失败：导出服务不可用/)).toBeVisible();
  expect(historyApi.exportPortfolioInspectionHistory).toHaveBeenCalledWith(1, expect.objectContaining({ issue_id: "real-issue", snapshot_event_id: 8 }));
  expect(screen.getByText("真实数据阻断处理")).toBeVisible();
});
