import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "antd";
import { api } from "../../../api/client";
import { useApp } from "../../../context/AppContext";
import { openSettingsSection } from "../../../utils/navigate";
import type { InspectionSymbol, PortfolioInspectionDiagnostics } from "../../../types/portfolioInspection";
import InspectionDataPanel from "./InspectionDataPanel";
import InspectionDrawer from "./InspectionDrawer";
import InspectionHistoryPanel from "./InspectionHistoryPanel";
import { INSPECTION_RETURN_KEY, readInspectionReturn } from "./inspectionNavigation";
import "./inspection.css";
import useInspectionPalette from "./useInspectionPalette";
import {AcceptanceNotice} from './AcceptanceControls';

/** Full-page batch tooling in the existing data management domain. */
export default function InspectionRepairWorkspace({portfolioId}:{portfolioId:number}) {
  const ctx=useApp();
  const palette=useInspectionPalette();
  const [data,setData]=useState<PortfolioInspectionDiagnostics|null>(null);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState<string|null>(null);
  const [history,setHistory]=useState<InspectionSymbol|null>(null);
  const [historyDetail,setHistoryDetail]=useState(false);
  const request=useRef(0);
  const reload=useCallback(async()=>{
    const token=++request.current;setLoading(true);setError(null);
    try { const result=await api.getPortfolioInspectionDiagnostics(portfolioId);
      if(token!==request.current)return;
      if(result.portfolio_id!==portfolioId||!Array.isArray(result.symbols)||!result.repair_plan)throw new Error("数据诊断不属于当前组合或材料不完整");
      setData(result);
    }catch(cause){if(token===request.current)setError(cause instanceof Error?cause.message:"数据诊断暂不可读");}
    finally{if(token===request.current)setLoading(false);}
  },[portfolioId]);
  useEffect(()=>{void reload();return()=>{++request.current;};},[reload]);
  const navigate=(target:string)=>{
    const saved=readInspectionReturn(portfolioId);
    if(saved)try{sessionStorage.setItem(INSPECTION_RETURN_KEY,JSON.stringify({...saved,destination:target}));}catch{/* Keep the destination usable. */}
    if(["strategy","members","backtest"].includes(target)){ctx.setActiveSubTab(target);ctx.setActiveTab("portfolio");return;}
    openSettingsSection(target.startsWith("settings:")?target.slice(9):/factor|scoring|model/.test(target)?"factor-center":"data-center");
    ctx.setActiveTab("settings");
  };
  return <section className="pi-workbench pi-repair-workspace" style={palette} aria-label="组合数据批量修复工作区"><AcceptanceNotice/>
    <header className="pi-page-head"><div><h2>组合数据修复</h2><p>当前组合 {ctx.portfolios.find(row=>row.id===portfolioId)?.name??"当前组合"} · 行情、指标与评分批量治理</p></div><Button onClick={()=>void reload()} loading={loading}>重新读取数据诊断</Button></header>
    <InspectionDataPanel key={portfolioId} portfolioId={portfolioId} data={data} loading={loading} error={error} onReload={reload} onHistory={symbol=>setHistory(symbol??null)} onNavigate={navigate} covered={Boolean(history)}/>
    <InspectionDrawer open={Boolean(history)} active={!historyDetail} title="该标的相关处理记录" context="数据中心 / 组合数据修复 / 历史记录" zIndex={1460}
      onClose={()=>setHistory(null)} onBack={()=>setHistory(null)} backLabel="返回数据修复">
      {history&&<InspectionHistoryPanel portfolioId={portfolioId} onOverlayChange={setHistoryDetail} context={{security_ids:[`${history.market}:${history.symbol}`],trade_date:history.expected_trade_date??undefined,issue_id:history.evidence.find(row=>!["PASSED","NOT_APPLICABLE","NOT_DUE"].includes(row.status))?.evidence_id,label:`${history.name}的数据问题`}}/>}
    </InspectionDrawer>
  </section>;
}
