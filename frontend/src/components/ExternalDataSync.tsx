import { useState } from "react";
import { Button, Card, Checkbox, Select, Space, Tooltip, message, Alert, Collapse } from "antd";
import { QuestionCircleOutlined, SyncOutlined, DatabaseOutlined, FundOutlined, RiseOutlined } from "@ant-design/icons";
import { t, template } from "../i18n";
import { api } from "../api/client";

type SyncSource = "watchlist" | "positions" | "all";

interface SyncResult {
  total: number;
  success: number;
  skipped: number;
  failed: number;
  errors: string[];
}

function resultParams(r: SyncResult): Record<string, string | number> {
  return { total: r.total, success: r.success, skipped: r.skipped, failed: r.failed };
}

export default function ExternalDataSync() {
  const [source, setSource] = useState<SyncSource>("watchlist");
  const [includeNorthbound, setIncludeNorthbound] = useState(true);
  const [syncingFund, setSyncingFund] = useState(false);
  const [syncingFlow, setSyncingFlow] = useState(false);
  const [syncingEtf, setSyncingEtf] = useState(false);
  const [lastResult, setLastResult] = useState<{ label: string; result: SyncResult } | null>(null);

  const handleSyncFundamental = async () => {
    setSyncingFund(true);
    setLastResult(null);
    try {
      const result = await api.syncFundamental(source);
      setLastResult({ label: t("extSyncFundamental"), result });
      if (result.failed === 0) {
        message.success(template("extSyncResult", resultParams(result)));
      } else {
        message.warning(template("extSyncResult", resultParams(result)));
      }
    } catch (e: any) {
      message.error(template("extSyncFailed", { message: e?.message || String(e) }));
    } finally {
      setSyncingFund(false);
    }
  };

  const handleSyncCapitalFlow = async () => {
    setSyncingFlow(true);
    setLastResult(null);
    try {
      const result = await api.syncCapitalFlow(source, includeNorthbound);
      setLastResult({ label: t("extSyncCapitalFlow"), result });
      if (result.failed === 0) {
        message.success(template("extSyncResult", resultParams(result)));
      } else {
        message.warning(template("extSyncResult", resultParams(result)));
      }
    } catch (e: any) {
      message.error(template("extSyncFailed", { message: e?.message || String(e) }));
    } finally {
      setSyncingFlow(false);
    }
  };

  const handleSyncEtf = async () => {
    setSyncingEtf(true);
    setLastResult(null);
    try {
      const result = await api.syncEtfIndicators(source);
      setLastResult({ label: t("extSyncEtf"), result });
      if (result.failed === 0) {
        message.success(template("extSyncResult", resultParams(result)));
      } else {
        message.warning(template("extSyncResult", resultParams(result)));
      }
    } catch (e: any) {
      message.error(template("extSyncFailed", { message: e?.message || String(e) }));
    } finally {
      setSyncingEtf(false);
    }
  };

  return (
    <div className="external-data-sync-section">
      <Card
        size="small"
        title={
          <Space>
            <DatabaseOutlined />
            <span>{t("extSectionTitle")}</span>
            <Tooltip title={t("extSectionDesc")}>
              <span style={{ cursor: "help", color: "#888" }}>
                <QuestionCircleOutlined />
              </span>
            </Tooltip>
          </Space>
        }
      >
        <div style={{ marginBottom: 16, color: "#666", fontSize: 13 }}>{t("extSectionDesc")}</div>

        <div style={{ marginBottom: 16, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <Space>
            <span>{t("extSourceLabel")}:</span>
            <Select
              value={source}
              onChange={(v) => setSource(v)}
              style={{ width: 140 }}
              options={[
                { value: "watchlist", label: t("extSourceWatchlist") },
                { value: "positions", label: t("extSourcePositions") },
                { value: "all", label: t("extSourceAll") },
              ]}
            />
          </Space>
          <Checkbox checked={includeNorthbound} onChange={(e) => setIncludeNorthbound(e.target.checked)}>
            {t("extIncludeNorthbound")}
          </Checkbox>
        </div>

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <Card
            size="small"
            style={{ flex: "1 1 240px", minWidth: 240 }}
            title={
              <Space>
                <FundOutlined />
                <span>{t("extSyncFundamental")}</span>
              </Space>
            }
          >
            <div style={{ marginBottom: 12, color: "#888", fontSize: 12 }}>{t("extSyncFundamentalDesc")}</div>
            <Button
              type="primary"
              icon={<SyncOutlined spin={syncingFund} />}
              loading={syncingFund}
              onClick={handleSyncFundamental}
            >
              {syncingFund ? t("extSyncing") : t("extSyncFundamental")}
            </Button>
          </Card>

          <Card
            size="small"
            style={{ flex: "1 1 240px", minWidth: 240 }}
            title={
              <Space>
                <RiseOutlined />
                <span>{t("extSyncCapitalFlow")}</span>
              </Space>
            }
          >
            <div style={{ marginBottom: 12, color: "#888", fontSize: 12 }}>{t("extSyncCapitalFlowDesc")}</div>
            <Button
              type="primary"
              icon={<SyncOutlined spin={syncingFlow} />}
              loading={syncingFlow}
              onClick={handleSyncCapitalFlow}
            >
              {syncingFlow ? t("extSyncing") : t("extSyncCapitalFlow")}
            </Button>
          </Card>

          <Card
            size="small"
            style={{ flex: "1 1 240px", minWidth: 240 }}
            title={
              <Space>
                <DatabaseOutlined />
                <span>{t("extSyncEtf")}</span>
              </Space>
            }
          >
            <div style={{ marginBottom: 12, color: "#888", fontSize: 12 }}>{t("extSyncEtfDesc")}</div>
            <Button
              type="primary"
              icon={<SyncOutlined spin={syncingEtf} />}
              loading={syncingEtf}
              onClick={handleSyncEtf}
            >
              {syncingEtf ? t("extSyncing") : t("extSyncEtf")}
            </Button>
          </Card>
        </div>

        {lastResult && (
          <div style={{ marginTop: 16 }}>
            <Alert
              type={lastResult.result.failed === 0 ? "success" : "warning"}
              showIcon
              message={`${lastResult.label}: ${template("extSyncResult", resultParams(lastResult.result))}`}
              description={
                lastResult.result.errors.length > 0 ? (
                  <Collapse
                    size="small"
                    ghost
                    items={[
                      {
                        key: "errors",
                        label: `${t("extErrorsTitle")} (${lastResult.result.errors.length})`,
                        children: (
                          <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12, color: "#b42318" }}>
                            {lastResult.result.errors.map((err, i) => (
                              <li key={i}>{err}</li>
                            ))}
                          </ul>
                        ),
                      },
                    ]}
                  />
                ) : null
              }
            />
          </div>
        )}
      </Card>
    </div>
  );
}
