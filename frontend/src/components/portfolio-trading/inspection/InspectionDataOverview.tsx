import { useEffect, useRef, useState } from "react";
import { Button, Input } from "antd";
import type { InspectionSymbol, PortfolioInspectionDiagnostics } from "../../../types/portfolioInspection";
import InspectionDrawer from "./InspectionDrawer";
import {readInspectionBrowse,writeInspectionBrowse} from "./inspectionBrowseState";

interface Props {
  portfolioId:number;
  data: PortfolioInspectionDiagnostics | null;
  loading: boolean;
  error?: string;
  onReload: () => void;
  onRepair: () => void;
  onHistory: (symbol: InspectionSymbol) => void;
  onAnalyse: (symbol: InspectionSymbol) => void;
  visible: boolean;
  onOverlayChange: (open: boolean) => void;
}
const healthy = (status: string) => ["PASSED","NOT_APPLICABLE","NOT_DUE"].includes(status);
const labels: Record<string,string> = {PASSED:"有效",NOT_APPLICABLE:"不适用",NOT_DUE:"尚未到期",MISSING:"缺失",STALE:"过期",READ_ERROR:"读取失败",VERSION_MISMATCH:"版本不符",FUTURE_DATA:"时点不符",CALENDAR_UNKNOWN:"日历待核实",UNKNOWN:"待核实"};
export default function InspectionDataOverview({portfolioId,data,loading,error,onReload,onRepair,onHistory,onAnalyse,visible,onOverlayChange}:Props) {
  const saved=useRef<{query?:string;symbolId?:number|null}>(readInspectionBrowse(portfolioId,"data-filter")??(()=>{try{const value=JSON.parse(sessionStorage.getItem("portfolio-inspection-diagnosis-draft-v1")??"null");return value?.portfolioId===portfolioId&&Date.now()-value.savedAt<30*60*1000?value:{};}catch{return {};}})());
  const [query,setQuery]=useState(typeof saved.current.query==="string"?saved.current.query:"");
  const [symbolId,setSymbolId]=useState<number|null>(Number.isSafeInteger(saved.current.symbolId)&&Number(saved.current.symbolId)>0?Number(saved.current.symbolId):null);
  useEffect(()=>{writeInspectionBrowse(portfolioId,"data-filter",{query,symbolId});},[portfolioId,query,symbolId]);
  useEffect(()=>{onOverlayChange(visible&&symbolId!==null);return()=>onOverlayChange(false);},[visible,symbolId,onOverlayChange]);
  const symbols=data?.symbols??[];
  const failures=symbols.filter(symbol=>symbol.evidence.some(row=>!healthy(row.status)));
  const rows=symbols.filter(symbol=>`${symbol.name} ${symbol.symbol}`.toLowerCase().includes(query.toLowerCase()));
  const selected=symbols.find(symbol=>symbol.symbol_id===symbolId);
  const close=()=>{setSymbolId(null);onOverlayChange(false);};
  return <section aria-label="当前数据诊断汇总">
    <div className={`pi-callout ${failures.length?"pi-warning":"pi-success"}`}><strong>{failures.length} / {symbols.length} 个标的有待核实数据</strong><p>在这里查看具体原因；批量补齐行情、指标与评分在数据中心完成，返回后重新检查运行条件。</p></div>
    {loading&&<p role="status">正在读取当前数据证据…</p>}
    {error&&<div className="pi-callout pi-error" role="alert">{error}<Button onClick={onReload}>重新读取</Button></div>}
    {data?.evidence.filter(row=>!healthy(row.status)).map(row=><div className="pi-callout pi-warning" key={row.evidence_id}><strong>{row.label}</strong><p>{row.reason}</p></div>)}
    <div className="pi-panel-actions"><Button type="primary" onClick={onRepair}>前往数据中心批量修复</Button><span>进入独立数据管理工作区，当前检查进度会保留。</span></div>
    <Input aria-label="搜索待核实标的" value={query} onChange={event=>setQuery(event.target.value)} placeholder="按标的名称或代码查找"/>
    <div className="pi-task-list">{rows.map(symbol=><article className="pi-task-row" key={symbol.symbol_id}><div><h3>{symbol.name} · {symbol.symbol}</h3><p>{symbol.evidence.filter(row=>!healthy(row.status)).map(row=>row.label).join("、")||"本次检查已满足"}</p><small>应到交易日 {symbol.expected_trade_date??"待核实"}</small></div><Button onClick={()=>{setSymbolId(symbol.symbol_id);onOverlayChange(true);}}>查看标的证据</Button></article>)}</div>
    {!loading&&!rows.length&&<p className="pi-empty">当前条件没有匹配标的，可调整搜索或重新读取诊断。</p>}
    <InspectionDrawer open={symbolId!==null&&visible} active={visible} scrollKey={`${portfolioId}:${symbolId}`} zIndex={1500} title={selected?`${selected.name} · 数据检查详情`:"标的数据检查详情"}
      browseScope={{portfolioId,key:`data-symbol:${symbolId}`}}
      context="检查与对账 / 当前数据 / 标的证据" onClose={close} onBack={close} backLabel="返回数据汇总">
      {selected?<><p>应到交易日 {selected.expected_trade_date??"待核实"} · 行情 {selected.actual_bar_trade_date??"未读取"} · 评分 {selected.actual_score_trade_date??"未读取"}</p>
        {selected.evidence.map(row=><article className={`pi-callout ${healthy(row.status)?"pi-success":"pi-warning"}`} key={row.evidence_id}><strong>{row.label} · {labels[row.status]??"待核实"}</strong><p>{row.reason}</p><small>来源：{row.source_label} · 实际日期 {row.actual_trade_date??"未提供"} · 应到日期 {row.expected_trade_date??selected.expected_trade_date??"待核实"}</small></article>)}
        <div className="pi-panel-actions"><Button onClick={onRepair}>前往数据中心处理</Button><Button onClick={()=>onHistory(selected)}>该标的相关历史处理</Button><Button onClick={()=>onAnalyse(selected)}>AI 分析此标的缺口</Button></div></>:<p role="status">{loading?"正在读取该标的当前证据…":"该标的已不在当前诊断范围，请返回汇总重新选择。"}</p>}
    </InspectionDrawer>
  </section>;
}
