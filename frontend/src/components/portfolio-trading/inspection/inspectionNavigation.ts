export const INSPECTION_RETURN_KEY="portfolio-inspection-return-v1";
export interface InspectionReturnContext {
  portfolioId:number;
  step:number;
  panel?:string;
  layers?:string[];
  savedAt:number;
  destination?:string;
  [key:string]:unknown;
}
export function readInspectionReturn(portfolioId:number):InspectionReturnContext|null {
  try {
    const value=JSON.parse(sessionStorage.getItem(INSPECTION_RETURN_KEY)??"null");
    const age=Date.now()-value?.savedAt;
    if(value?.portfolioId===portfolioId&&Number.isInteger(value.step)&&value.step>=0&&value.step<=3&&age>=0&&age<30*60*1000)return value;
  } catch { /* Context is optional and is never an authorization token. */ }
  return null;
}
export function inspectionDestination(target?:string) {
  if(target==="settings:data-center:inspection-repair")return "数据中心 / 组合数据修复";
  if(target?.includes("data"))return "设置 / 数据中心";
  if(target?.includes("schedules"))return "设置 / 自动计划";
  if(target?.includes("factor")||target?.includes("model")||target?.includes("scoring"))return "设置 / 因子与模型中心";
  return ({strategy:"组合交易 / 策略规则",members:"组合交易 / 持仓成员",backtest:"组合交易 / 回测中心"} as Record<string,string>)[target??""]??"独立管理页面";
}
