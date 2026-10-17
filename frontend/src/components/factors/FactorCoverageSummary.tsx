import { Popover, Table, Typography } from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";
import type { FactorOverview } from "../../api/client";
import { factorLabel, t } from "../../i18n";

export default function FactorCoverageSummary({ overview }: { overview: FactorOverview | null }) {
  const rows = overview?.factor_coverage?.filter(row => row.model_enabled !== false) ?? [];
  const average = overview?.model_average_coverage;
  const display = typeof average === "number" ? average : overview?.average_coverage;
  return <>
    <span>{t("factorModelCoverage")} <Popover trigger={["click", "hover"]} title={t("factorCoverageDetails")} content={<div style={{ width: "min(580px, 80vw)" }}>
      <p>{t("factorCoverageScopeHelp")}</p>
      <p>{t("factorCoverageBarDate")}: {overview?.latest_trade_date || "—"} · {t("factorCoverageCalcDate")}: {overview?.latest_factor_date || "—"}</p>
      <Table size="small" pagination={false} rowKey="factor_code" dataSource={rows} columns={[
        { title: t("factorCoverageName"), dataIndex: "factor_code", render: value => factorLabel(value) },
        { title: t("factorCoverageRatio"), dataIndex: "coverage", render: value => `${(value * 100).toFixed(1)}%` },
        { title: t("factorCoverageSymbols"), render: (_, row) => `${row.eligible_symbols} / ${row.universe_symbols}` },
        { title: t("factorCoverageCalcDate"), dataIndex: "latest_trade_date" },
      ]} />
    </div>}><button type="button" aria-label={t("factorCoverageDetails")} style={{ border: 0, background: "none", cursor: "pointer", color: "inherit", padding: 2 }}><QuestionCircleOutlined /></button></Popover></span>
    <strong><Typography.Text type={typeof display === "number" && display < .7 ? "warning" : undefined}>{typeof display === "number" ? `${(display * 100).toFixed(1)}%` : "—"}</Typography.Text></strong>
  </>;
}
