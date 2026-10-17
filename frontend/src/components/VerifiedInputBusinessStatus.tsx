import { useState } from "react";
import { Alert, Button, Descriptions, Modal, Space, Table, Typography } from "antd";
import { api } from "../api/client";
import { factorLabel, t } from "../i18n";
import type { InputBusinessStatus } from "../types/inputReplay";

export default function VerifiedInputBusinessStatus() {
  const [open,setOpen]=useState(false);
  const [busy,setBusy]=useState(false);
  const [data,setData]=useState<InputBusinessStatus|null>(null);
  const [error,setError]=useState<string>();
  const load=async()=>{
    setBusy(true);setError(undefined);setData(null);
    try {
      const bundles=await api.listVerifiedInputBundles();
      if(!bundles.length)throw new Error(t("inputImport.noBundle"));
      setData(await api.getInputBusinessStatus(bundles[0].bundle_id));
    }catch(reason){setError(String(reason));}finally{setBusy(false);}
  };
  return <>
    <Button onClick={()=>{setOpen(true);void load();}}>{t("inputImport.open")}</Button>
    <Modal title={t("inputImport.title")} open={open} onCancel={()=>setOpen(false)} width={920} footer={<Button loading={busy} onClick={()=>void load()}>{t("inputImport.refresh")}</Button>}>
      <Space direction="vertical" style={{width:"100%"}} size={16}>
        <Alert type="info" showIcon message={t("inputImport.boundary")} description={t("inputImport.coverageHelp")} />
        {error&&<Alert type="error" showIcon message={error}/>}
        {data&&<>
          <Descriptions bordered size="small" column={2} items={[
            {key:"scope",label:t("inputImport.scope"),children:`${data.fixed_symbols} · ${data.first_date} → ${data.last_date}`},
            {key:"date",label:t("factorCoverageCalcDate"),children:data.calculation_date||"—"},
            {key:"bars",label:t("inputImport.bars"),children:`${data.business_counts.bars.toLocaleString()} / ${data.warehouse_counts.raw_daily_bars.toLocaleString()}`},
            {key:"financial",label:t("inputImport.financial"),children:`${data.business_counts.financial.toLocaleString()} / ${data.warehouse_counts.raw_financial_reports.toLocaleString()}`},
            {key:"flow",label:t("inputImport.flow"),children:`${data.business_counts.flow.toLocaleString()} / ${data.warehouse_counts.raw_fund_flows.toLocaleString()}`},
          ]}/>
          <Typography.Text type="secondary">{t("inputImport.countHelp")}</Typography.Text>
          <Table<InputBusinessStatus["fixed_pool_coverage"][number]> size="small" pagination={false} rowKey="code" dataSource={data.fixed_pool_coverage} columns={[
            {title:t("factorCoverageName"),dataIndex:"code",render:(value: string)=>factorLabel(value)},
            {title:t("inputImport.fixed"),render:(_,row)=>`${row.eligible} / ${row.denominator}`},
            {title:t("inputImport.market"),render:(_,row)=>{const market=data.market_coverage.find(m=>m.factor_code===row.code);return market?`${market.eligible_symbols} / ${market.universe_symbols} · ${(market.coverage*100).toFixed(2)}%`:"—";}},
          ]}/>
          <Typography.Text type="secondary" style={{overflowWrap:"anywhere"}}>{t("inputReplay.fingerprint")}: {data.bundle_manifest_sha256}</Typography.Text>
        </>}
        {busy&&<Typography.Text>{t("inputImport.loading")}</Typography.Text>}
      </Space>
    </Modal>
  </>;
}
