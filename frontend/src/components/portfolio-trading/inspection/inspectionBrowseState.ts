/** Tab-local presentation state only. Reports, permissions and approvals are always re-read. */
export const INSPECTION_BROWSE_TTL = 24 * 60 * 60 * 1000;
const PREFIX = "portfolio-inspection-browse-v1:";
const validPortfolio = (id:number) => Number.isSafeInteger(id) && id > 0;
export function readInspectionBrowse<T>(portfolioId:number, scope:string):T|null {
  if (!validPortfolio(portfolioId)) return null;
  try {
    const item=JSON.parse(sessionStorage.getItem(`${PREFIX}${portfolioId}:${encodeURIComponent(scope)}`)??"null");
    const age=Date.now()-item?.savedAt;
    return item?.version===1&&item.portfolioId===portfolioId&&item.scope===scope&&age>=0&&age<INSPECTION_BROWSE_TTL ? item.value as T : null;
  } catch { return null; }
}
export function writeInspectionBrowse(portfolioId:number, scope:string, value:unknown) {
  if (!validPortfolio(portfolioId)) return;
  try { sessionStorage.setItem(`${PREFIX}${portfolioId}:${encodeURIComponent(scope)}`,JSON.stringify({version:1,portfolioId,scope,savedAt:Date.now(),value})); } catch { /* Browsing remains usable without storage. */ }
}
const LOCATION="portfolio-inspection-location-v1";
export function setInspectionLocation(portfolioId:number, active:boolean) {
  if (!validPortfolio(portfolioId)) return;
  try { sessionStorage.setItem(LOCATION,JSON.stringify({portfolioId,active,savedAt:Date.now()})); } catch { /* Optional reload location. */ }
}
export function readInspectionReloadLocation():number|null {
  try {
    const explicitTab=new URLSearchParams(window.location.search).get("tab");
    if(explicitTab&&explicitTab!=="portfolio")return null;
    if(performance.getEntriesByType("navigation")[0]?.entryType!=="navigation" || (performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming)?.type!=="reload")return null;
    const item=JSON.parse(sessionStorage.getItem(LOCATION)??"null"),age=Date.now()-item?.savedAt;
    return item?.active===true&&validPortfolio(item.portfolioId)&&age>=0&&age<INSPECTION_BROWSE_TTL ? item.portfolioId : null;
  } catch { return null; }
}
