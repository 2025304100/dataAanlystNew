import {useEffect,useMemo} from 'react';
import {api} from '../../../api/client';
import {createInspectionEvidenceMonitor} from '../../../utils/inspectionEvidenceMonitor';
import {invalidateInspectionHere,registerInspectionReviewGuard,setInspectionVerifying} from '../../../utils/inspectionFreshness';

export default function useInspectionEvidenceMonitor(portfolioId:number) {
  const monitor=useMemo(()=>createInspectionEvidenceMonitor(portfolioId,{
    runtime:()=>api.getPortfolioInspectionRuntime(portfolioId),job:()=>api.getPortfolioInspectionJob(portfolioId),
    report:date=>api.previewPortfolioReconciliation(portfolioId,date),
  },()=>invalidateInspectionHere(portfolioId),value=>setInspectionVerifying(portfolioId,value)),[portfolioId]);
  useEffect(()=>{const unregister=registerInspectionReviewGuard(portfolioId,monitor.guard),stop=monitor.attach();return()=>{unregister();stop();};},[monitor,portfolioId]);
  return monitor;
}
