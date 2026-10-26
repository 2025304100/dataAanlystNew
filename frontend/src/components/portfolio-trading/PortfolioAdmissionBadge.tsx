import usePortfolioAdmission from "../../hooks/usePortfolioAdmission";

const colors = {allowed:{fg:"#86efac",bg:"rgba(16,185,129,.15)"},
  checking:{fg:"#94a3b8",bg:"rgba(107,114,128,.15)"},unknown:{fg:"#94a3b8",bg:"rgba(107,114,128,.15)"},
  blocked:{fg:"#fca5a5",bg:"rgba(220,38,38,.15)"},review:{fg:"#fcd34d",bg:"rgba(245,158,11,.15)"},
  waiting:{fg:"#93c5fd",bg:"rgba(59,130,246,.15)"}};
export default function PortfolioAdmissionBadge({portfolioId,onInspect}:{portfolioId:number;onInspect?:()=>void}) {
  const {view}=usePortfolioAdmission(portfolioId);const color=colors[view.kind];
  return <span data-testid={`portfolio-admission-${portfolioId}`} data-admission-state={view.kind}
    aria-label={`实时准入：${view.title}`} title={view.hint}
    onClick={onInspect?event=>{event.stopPropagation();onInspect();}:undefined}
    style={{display:"inline-flex",alignItems:"center",gap:5,fontSize:11,fontWeight:500,padding:"2px 8px",
      borderRadius:6,border:`1px solid ${color.fg}`,color:color.fg,background:color.bg,cursor:onInspect?"pointer":"inherit"}}>
    <span aria-hidden="true" style={{width:6,height:6,borderRadius:3,background:color.fg}}/>{view.title}</span>;
}
