import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../../i18n";

const mock = vi.hoisted(() => ({listVerifiedInputBundles:vi.fn(),scoringListModels:vi.fn(),createInputReplay:vi.fn(),
  getInputReplayTask:vi.fn(),getInputReplayReport:vi.fn(),activateScoringModel:vi.fn()}));
vi.mock("../../api/client", () => ({api:mock}));
import VerifiedInputReplay from "../VerifiedInputReplay";

const requestId="a".repeat(32);
const bundle={bundle_id:"fixed",label:"固定股票池凭证",dataset_id:"recent",manifest_sha256:"b".repeat(64),
  financial_rows:1150,financial_symbols:129,flow_rows:364,flow_symbols:13,suggested_model_id:"research"};
const report={replay_id:requestId,research_only:true,production_scores_published:0,last_date:"2026-10-16",
  frozen_symbol_count:129,scored_symbols:128,bundle_manifest_sha256:bundle.manifest_sha256,dataset_manifest_sha256:"c".repeat(64),
  input_counts:{raw_daily_bars:3592,raw_financial_reports:1150,raw_fund_flows:364},
  model:{intercept:.1},gaps:{no_flow_symbols:Array.from({length:116},(_,n)=>String(n))},
  coverage:[{code:"turnover_z20",version:3,eligible:128,denominator:129,model_feature:true}],
  rows:[{symbol:"000001",score:63,linear_signal:.11,features:{turnover_z20:{version:3,raw_value:2,normalized_value:-1,coefficient:-.01,contribution:.01}},factor_observations:[],missing_features:[]}]};

describe("verified input chain",()=>{
  beforeEach(()=>{
    vi.clearAllMocks();localStorage.clear();setLocale("zh-CN");
    mock.listVerifiedInputBundles.mockResolvedValue([bundle]);
    mock.scoringListModels.mockResolvedValue([{id:"research",name:"研究模型",readiness_code:"MODEL_RESEARCH_DATASET_ONLY"}]);
    mock.createInputReplay.mockResolvedValue({id:requestId,status:"queued",stage:"queued",percent:0});
    mock.getInputReplayTask.mockResolvedValue({id:requestId,status:"done",stage:"done",percent:100});
    mock.getInputReplayReport.mockResolvedValue(report);
  });
  afterEach(()=>{cleanup();localStorage.clear();});

  it("opening is read-only; explicit verification shows real inputs and excludes production activation",async()=>{
    render(<VerifiedInputReplay/>);
    expect(mock.listVerifiedInputBundles).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button',{name:'开始验收'}));
    const start=await screen.findByRole('button',{name:'核验输入并计算研究评分'});
    await waitFor(()=>expect(start).toBeEnabled());
    expect(mock.createInputReplay).not.toHaveBeenCalled();
    await act(async()=>{fireEvent.click(start);});
    await waitFor(()=>expect(screen.getByText('研究预览已生成，生产评分仍未发布')).toBeVisible());
    expect(mock.createInputReplay.mock.calls[0][0]).toMatchObject({bundle_id:'fixed',model_id:'research',manifest_sha256:bundle.manifest_sha256});
    expect(mock.createInputReplay.mock.calls[0][0].request_id).toMatch(/^[a-f0-9]{32}$/);
    expect(screen.getByText('3,592')).toBeVisible();
    expect(screen.getByText('000001')).toBeVisible();
    expect(mock.activateScoringModel).not.toHaveBeenCalled();
  });

  it("recovers an existing task without submitting another check",async()=>{
    localStorage.setItem('verified_input_replay_request_v1',JSON.stringify({bundle_id:'fixed',manifest_sha256:bundle.manifest_sha256,model_id:'research',request_id:requestId}));
    render(<VerifiedInputReplay/>);fireEvent.click(screen.getByRole('button',{name:'开始验收'}));
    await waitFor(()=>expect(screen.getByText('研究预览已生成，生产评分仍未发布')).toBeVisible());
    expect(mock.getInputReplayTask).toHaveBeenCalledWith(requestId);
    expect(mock.createInputReplay).not.toHaveBeenCalled();
  });

  it("does not create a replacement request after an uncertain submission",async()=>{
    mock.createInputReplay.mockRejectedValue(new Error('network unavailable'));
    render(<VerifiedInputReplay/>);fireEvent.click(screen.getByRole('button',{name:'开始验收'}));
    const start=await screen.findByRole('button',{name:'核验输入并计算研究评分'});
    await waitFor(()=>expect(start).toBeEnabled());
    await act(async()=>{fireEvent.click(start);});
    await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'恢复同一次验收请求'}));});
    expect(mock.createInputReplay.mock.calls[0][0]).toEqual(mock.createInputReplay.mock.calls[1][0]);
  });
});
