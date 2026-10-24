import {beforeEach,afterEach,expect,it,vi} from "vitest";
import {readInspectionBrowse,writeInspectionBrowse,INSPECTION_BROWSE_TTL,readInspectionReloadLocation,setInspectionLocation} from "../inspectionBrowseState";

beforeEach(()=>{sessionStorage.clear();});afterEach(()=>vi.restoreAllMocks());
it("separates portfolio and view scopes and refuses expired, future, or damaged envelopes",()=>{
 writeInspectionBrowse(1,"history",{date:"2026-09-30"});writeInspectionBrowse(2,"history",{date:"2026-10-20"});
 expect(readInspectionBrowse(1,"history")).toEqual({date:"2026-09-30"});expect(readInspectionBrowse(2,"history")).toEqual({date:"2026-10-20"});expect(readInspectionBrowse(1,"data")).toBeNull();
 const now=Date.now();vi.spyOn(Date,"now").mockReturnValue(now+INSPECTION_BROWSE_TTL+1);expect(readInspectionBrowse(1,"history")).toBeNull();
 vi.mocked(Date.now).mockReturnValue(now-100);expect(readInspectionBrowse(1,"history")).toBeNull();
 sessionStorage.setItem("portfolio-inspection-browse-v1:1:history","{broken");expect(readInspectionBrowse(1,"history")).toBeNull();
});
it("restores the reload location only for an active inspection and respects an explicit tab",()=>{
 vi.spyOn(performance,"getEntriesByType").mockReturnValue([{entryType:"navigation",type:"reload"}] as unknown as PerformanceEntry[]);
 setInspectionLocation(2,true);expect(readInspectionReloadLocation()).toBe(2);setInspectionLocation(2,false);expect(readInspectionReloadLocation()).toBeNull();
 setInspectionLocation(2,true);history.replaceState(null,"","?tab=settings");expect(readInspectionReloadLocation()).toBeNull();history.replaceState(null,"","?tab=portfolio");expect(readInspectionReloadLocation()).toBe(2);history.replaceState(null,"","/");
});
it("does not fail browsing when session storage is blocked",()=>{
 vi.spyOn(Storage.prototype,"setItem").mockImplementation(()=>{throw new Error("blocked");});expect(()=>writeInspectionBrowse(1,"data",{query:"ETF"})).not.toThrow();
});
