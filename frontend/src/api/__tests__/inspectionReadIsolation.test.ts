import {afterEach,expect,it,vi} from "vitest";
import {api,requestJson} from "../client";

afterEach(()=>vi.unstubAllGlobals());
const reference={kind:"report",ref:"read-only-report",source_event_id:1,version:"v1",retention_status:"archived",read_only:true} as const;
const reads:Array<[string,()=>Promise<unknown>]>= [
 ["same-date preview",()=>api.previewPortfolioReconciliation(1,"2026-10-20")],
 ["runtime",()=>api.getPortfolioInspectionRuntime(1)],
 ["status",()=>api.getPortfolioStatus(1)],
 ["actions",()=>api.getPortfolioActionItems(1)],
 ["binding",()=>api.getPortfolioModelBindingReview(1)],
 ["coverage",()=>api.getPortfolioInspectionCoverage(1)],
 ["activation review",()=>api.getPortfolioCoverageActivationReview(1)],
 ["diagnostics",()=>api.getPortfolioInspectionDiagnostics(1)],
 ["queue",()=>api.getPortfolioInspectionQueue(1)],
 ["job",()=>api.getPortfolioInspectionJob(1)],
 ["original recheck request",()=>api.getPortfolioRecheckRequest(1,"frozen-request-key")],
 ["evidence",()=>api.getPortfolioEvidenceSupplements(1,"2026-10-20")],
 ["history",()=>api.getPortfolioInspectionHistory(1,{trade_date_from:"2026-10-20"})],
 ["history securities",()=>api.getPortfolioInspectionHistorySecurities(1)],
 ["history material",()=>api.getPortfolioInspectionHistoryMaterial(1,reference,1)],
 ["history detail",()=>api.getPortfolioInspectionHistoryDetail(1,"batch-1")],
 ["engine status",()=>api.getG7OperationalStatus(1)],
];
it.each(reads)("starts independent %s reads for separate view intents",async(_label,read)=>{
 const fetchMock=vi.fn().mockResolvedValue({ok:true,json:async()=>({})});vi.stubGlobal("fetch",fetchMock);
 await Promise.all([read(),read()]);expect(fetchMock).toHaveBeenCalledTimes(2);
 expect(fetchMock.mock.calls.every(call=>!call[1]?.method||call[1].method==="GET")).toBe(true);
});
it("sends the original source recheck key and date on the legacy endpoint",async()=>{
 const fetchMock=vi.fn().mockResolvedValue({ok:true,json:async()=>({})});vi.stubGlobal("fetch",fetchMock);
 await api.recheckPortfolioSources(1,"2026-09-29","frozen-request-key");
 expect(fetchMock.mock.calls[0][0]).toContain('/portfolios/1/inspection/reconciliation-job/recheck');
 expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({trade_date:"2026-09-29",request_key:"frozen-request-key"});
});
it("keeps the global GET deduplication policy unchanged",async()=>{
 const fetchMock=vi.fn().mockResolvedValue({ok:true,json:async()=>({})});vi.stubGlobal("fetch",fetchMock);
 await Promise.all([requestJson("/inspection-general-policy-probe"),requestJson("/inspection-general-policy-probe")]);expect(fetchMock).toHaveBeenCalledTimes(1);
});
