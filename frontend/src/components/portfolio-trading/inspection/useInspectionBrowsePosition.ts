import {useLayoutEffect,useRef,type RefObject} from "react";
import {readInspectionBrowse,writeInspectionBrowse} from "./inspectionBrowseState";

export type BrowseScope={portfolioId:number;key:string};
type Position={top:number;open:Array<{index:number;label:string}>};
/** Restores after async rows and collapsed sections have reached their real size. */
export default function useInspectionBrowsePosition(content:RefObject<HTMLDivElement>,scope:BrowseScope|undefined,active:boolean,initialTop:number,onScroll?:(top:number)=>void,containerSelector=".ant-drawer-body") {
  const identity=scope?`${scope.portfolioId}:${scope.key}`:"";
  const memory=useRef<{identity:string;position:Position}|null>(null);
  const notify=useRef(onScroll);notify.current=onScroll;
  useLayoutEffect(()=>{
    const node=content.current,body=node?.closest<HTMLElement>(containerSelector);
    if(!node||!body||!active)return;
    const saved=identity&&scope?readInspectionBrowse<Position>(scope.portfolioId,`position:${scope.key}`):null;
    if(memory.current?.identity!==identity)memory.current={identity,position:{top:initialTop||((saved&&Number.isFinite(saved.top)&&saved.top>=0)?saved.top:0),open:Array.isArray(saved?.open)?saved.open.filter(item=>item&&Number.isInteger(item.index)&&typeof item.label==="string"):[]}};
    const current=memory.current!.position;
    let restoring=true,applying=false;
    let disclosuresRestored=current.open.length===0;
    const sections=()=>[...node.querySelectorAll<HTMLDetailsElement>("details")];
    const persist=()=>{
      if(applying)return;
      const items=sections();
      if(disclosuresRestored&&items.length)current.open=items.flatMap((item,index)=>item.open?[{index,label:item.querySelector("summary")?.textContent??""}]:[]);
      if(scope)writeInspectionBrowse(scope.portfolioId,`position:${scope.key}`,current);
    };
    const restore=()=>{
      applying=true;
      for(const entry of current.open){const item=sections()[entry.index];if(item&&item.querySelector("summary")?.textContent===entry.label)item.open=true;}
      if(sections().length)disclosuresRestored=true;
      applying=false;
      if(restoring){body.scrollTop=current.top;if(Math.abs(body.scrollTop-current.top)<2)restoring=false;}
    };
    const remember=()=>{if(restoring)return;current.top=body.scrollTop;notify.current?.(current.top);persist();};
    const intent=()=>{restoring=false;};
    const toggle=()=>{if(!applying){if(!restoring)current.top=body.scrollTop;persist();}};
    const leave=()=>{if(!restoring)current.top=body.scrollTop;persist();};
    restore();const frame=requestAnimationFrame(restore);
    const observer=typeof ResizeObserver!=="undefined"?new ResizeObserver(restore):null;observer?.observe(node);
    body.addEventListener("scroll",remember);body.addEventListener("wheel",intent,{passive:true});body.addEventListener("touchstart",intent,{passive:true});
    node.addEventListener("toggle",toggle,true);
    window.addEventListener("pagehide",leave);
    return()=>{cancelAnimationFrame(frame);observer?.disconnect();body.removeEventListener("scroll",remember);body.removeEventListener("wheel",intent);body.removeEventListener("touchstart",intent);node.removeEventListener("toggle",toggle,true);window.removeEventListener("pagehide",leave);leave();};
  });
}
