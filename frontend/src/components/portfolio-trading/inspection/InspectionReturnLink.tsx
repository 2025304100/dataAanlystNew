import { Button } from "antd";
import { useApp } from "../../../context/AppContext";
import { requestPortfolioSubtab } from "../../../utils/navigate";
import { inspectionDestination, readInspectionReturn } from "./inspectionNavigation";

export default function InspectionReturnLink() {
  const ctx=useApp();
  const saved=ctx.portfolioId&&ctx.portfolios.some(row=>row.id===ctx.portfolioId)?readInspectionReturn(ctx.portfolioId):null;
  if(!saved)return null;
  return <section className="panel" style={{margin:"12px 0",padding:16}} aria-label="返回检查与对账引导">
    <nav aria-label="跨模块操作路径" className="pi-return-breadcrumb">组合交易 / 检查与对账 <span>→</span> {inspectionDestination(saved.destination)}</nav>
    <strong>已进入独立管理模块，原检查进度已保留</strong><p>完成后返回原组合、原步骤与浏览位置；筛选、标的选择、任务和审核草稿保留，当前证据会重新核实。</p>
    <Button type="primary" onClick={()=>{ctx.setActiveSubTab("governance:overview");ctx.setActiveTab("portfolio");requestPortfolioSubtab("governance:overview");}}>返回检查与对账并重检</Button>
  </section>;
}
