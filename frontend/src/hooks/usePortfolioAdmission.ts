import { useCallback, useSyncExternalStore } from "react";
import { api } from "../api/client";
import type { InspectionRuntime } from "../types/portfolioInspection";
import { subscribeInspectionChanges } from "../utils/inspectionFreshness";
import { portfolioAdmissionView, type PortfolioAdmissionSnapshot } from "../utils/portfolioAdmission";

const EMPTY: PortfolioAdmissionSnapshot = {runtime:null,loading:true,error:null};
type Entry = { snapshot:PortfolioAdmissionSnapshot; listeners:Set<()=>void>; epoch:number;
  pending:Promise<InspectionRuntime>|null; detach?:()=>void };
const entries = new Map<number, Entry>();
function entry(id:number):Entry {
  if (!entries.has(id)) entries.set(id,{snapshot:EMPTY,listeners:new Set(),epoch:0,pending:null});
  return entries.get(id)!;
}
function update(value:Entry,snapshot:PortfolioAdmissionSnapshot) {
  value.snapshot=snapshot;value.listeners.forEach(listener=>listener());
}
function valid(value:InspectionRuntime,id:number) {
  return value?.portfolio_id===id && typeof value.current_state==="string" && typeof value.generated_at==="string" &&
    value.permissions && ["allow_new_buys","allow_risk_exits","allow_auto_recovery","requires_manual_ack"].every(
      key=>typeof value.permissions[key as keyof typeof value.permissions]==="boolean" || value.permissions[key as keyof typeof value.permissions]===null) &&
    value.recovery_summary && typeof value.recovery_summary.can_confirm==="boolean" && Array.isArray(value.recovery_summary.checks) && value.recovery_summary.checks.length>0 &&
    (value.recovery_summary.phase==null || typeof value.recovery_summary.phase==="string") &&
    value.recovery_summary.checks.every(check=>check && typeof check.key==="string" &&
      (typeof check.passed==="boolean" || check.passed===null)) &&
    value.capability_reasons && typeof value.capability_reasons==="object" && Object.values(value.capability_reasons).every(
      reasons=>Array.isArray(reasons) && reasons.every(reason=>typeof reason==="string"));
}

/** One bounded read per portfolio, shared by header, selector and inspection. No writes. */
export function refreshPortfolioAdmission(id:number,force=false):Promise<InspectionRuntime> {
  const value=entry(id);
  if (value.pending && !force) return value.pending;
  const token=++value.epoch;
  const current=()=>entries.get(id)===value && value.epoch===token;
  update(value,{runtime:null,loading:true,error:null});
  const work=(async()=>{
    let timer:ReturnType<typeof setTimeout>|undefined;
    try {
      await Promise.resolve();
      if (navigator.onLine===false) throw new Error("网络已断开，当前准入待重新核实");
      const result=await Promise.race([
        Promise.resolve().then(()=>api.getPortfolioInspectionRuntime(id)),
        new Promise<never>((_,reject)=>{timer=setTimeout(()=>reject(new Error("当前运行检查读取超时")),10000);}),
      ]);
      if (!valid(result,id)) throw new Error("返回的运行检查不完整或不属于当前组合");
      if (!current()) throw new Error("运行检查已更新，请重新读取当前依据");
      if (current()) update(value,{runtime:result,loading:false,error:null});
      return result;
    } catch(error) {
      if (current()) update(value,{runtime:null,loading:false,error:error instanceof Error?error.message:"当前运行检查读取失败"});
      throw error;
    } finally {
      if(timer!==undefined)clearTimeout(timer);
      if(current())value.pending=null;
    }
  })();
  value.pending=work;return work;
}

function subscribe(id:number,listener:()=>void) {
  if(!id)return()=>{};
  const value=entry(id);value.listeners.add(listener);
  if(value.listeners.size===1) {
    const refresh=()=>void refreshPortfolioAdmission(id,true).catch(()=>{});
    const offline=()=>{value.epoch++;value.pending=null;update(value,{runtime:null,loading:false,error:"网络已断开，当前准入待重新核实"});};
    const visible=()=>{
      if(document.visibilityState==="hidden") {value.epoch++;value.pending=null;update(value,EMPTY);}
      else refresh();
    };
    const timer=setInterval(()=>{if(document.visibilityState!=="hidden")refresh();},30000);
    const unlisten=subscribeInspectionChanges(id,refresh);
    window.addEventListener("focus",refresh);window.addEventListener("online",refresh);window.addEventListener("offline",offline);
    document.addEventListener("visibilitychange",visible);
    value.detach=()=>{clearInterval(timer);unlisten();window.removeEventListener("focus",refresh);window.removeEventListener("online",refresh);
      window.removeEventListener("offline",offline);document.removeEventListener("visibilitychange",visible);};
    void refreshPortfolioAdmission(id).catch(()=>{});
  }
  return()=>{value.listeners.delete(listener);if(!value.listeners.size){value.epoch++;value.detach?.();entries.delete(id);}};
}

export default function usePortfolioAdmission(id:number) {
  const listen=useCallback((listener:()=>void)=>subscribe(id,listener),[id]);
  const get=useCallback(()=>id?entry(id).snapshot:EMPTY,[id]);
  const snapshot=useSyncExternalStore(listen,get,()=>EMPTY);
  return {...snapshot,view:portfolioAdmissionView(snapshot)};
}
