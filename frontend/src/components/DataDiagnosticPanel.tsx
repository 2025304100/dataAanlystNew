import { useEffect, useState } from "react";
import { Alert, Button, Card, Empty, Progress, Select, Space, Spin, Tag, message } from "antd";
import { ReloadOutlined, ToolOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import { t } from "../i18n";
import type { Symbol } from "../types";

type DiagnosticData = {
  symbol_id: number;
  symbol: string;
  name: string;
  asset_type: string;
  market: string;
  summary: {
    total_bars: number;
    earliest_bar: string | null;
    latest_bar: string | null;
    latest_age_days: number | null;
    total_scores: number;
    latest_score: string | null;
    score_age_days: number | null;
  };
  coverage: Record<string, { bar_count: number; expected_bars: number; coverage_pct: number }>;
  score_coverage: Record<string, { score_count: number; bar_count: number; coverage_pct: number }>;
  issues: Array<{ type: string; severity: string; message: string; period?: string; latest_date?: string }>;
  suggestions: string[];
};

type DataDiagnosticPanelProps = {
  symbolId?: number | null;
  onOpenHistoryInit?: (context?: { symbolId?: number | null; symbolLabel?: string | null; repairMode?: "both" | "bars" | "scores" }) => void;
};

const PERIOD_KEYS: Record<string, string> = {
  "1m": "diagPeriod1m",
  "1q": "diagPeriod1q",
  "1y": "diagPeriod1y",
  "3y": "diagPeriod3y",
};

const ISSUE_TYPE_KEYS: Record<string, { key: string; color: string }> = {
  missing_bars: { key: "diagMissingBars", color: "red" },
  stale_bars: { key: "diagStaleBars", color: "orange" },
  low_coverage: { key: "diagLowCoverage", color: "orange" },
  missing_scores: { key: "diagMissingScores", color: "gold" },
  low_score_coverage: { key: "diagLowScoreCoverage", color: "gold" },
};

function coverageColor(pct: number): string {
  if (pct >= 80) return "#52c41a";
  if (pct >= 50) return "#faad14";
  return "#ff4d4f";
}

export default function DataDiagnosticPanel({ symbolId: propSymbolId = null, onOpenHistoryInit }: DataDiagnosticPanelProps) {
  const ctx = useApp();
  const [symbolId, setSymbolId] = useState<number | null>(propSymbolId ?? ctx.activeSymbolId);
  const [symbolOptions, setSymbolOptions] = useState<Symbol[]>([]);
  const [data, setData] = useState<DiagnosticData | null>(null);
  const [loading, setLoading] = useState(false);

  const loadSymbols = async (keyword?: string) => {
    try {
      const rows = await api.getSymbols(keyword, { page: 1, pageSize: 30 });
      setSymbolOptions((prev) => {
        const map = new Map<number, Symbol>();
        [...prev, ...(rows as Symbol[])].forEach((item) => map.set(item.id, item));
        return Array.from(map.values());
      });
    } catch { /* ignore */ }
  };

  const loadDiagnostic = async (sid: number) => {
    setLoading(true);
    setData(null);
    try {
      const result = await api.getSymbolDataHealth(sid);
      setData(result as DiagnosticData);
    } catch (error: any) {
      message.error(error?.message || t("diagLoadFailed"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadSymbols().catch(() => {});
  }, []);

  useEffect(() => {
    if (propSymbolId != null) {
      setSymbolId(propSymbolId);
      return;
    }
    if (ctx.activeSymbolId && !symbolId) {
      setSymbolId(ctx.activeSymbolId);
    }
  }, [ctx.activeSymbolId, propSymbolId, symbolId]);

  useEffect(() => {
    if (symbolId) {
      loadDiagnostic(symbolId);
    }
  }, [symbolId]);

  const handleOpenRepair = (mode: "bars" | "scores" | "both") => {
    if (!symbolId || !onOpenHistoryInit) return;
    const sym = symbolOptions.find((s) => s.id === symbolId);
    const label = sym ? `${sym.symbol} | ${sym.name}` : null;
    onOpenHistoryInit({ symbolId, symbolLabel: label, repairMode: mode });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Card size="small" title={t("diagTitle")}>
        <Space wrap style={{ marginBottom: 16 }}>
          <Select
            showSearch
            allowClear
            placeholder={t("diagSelectSymbol")}
            value={symbolId ?? undefined}
            filterOption={false}
            onSearch={(v) => loadSymbols(v).catch(() => {})}
            onChange={(v) => setSymbolId(v ?? null)}
            options={symbolOptions.map((item) => ({ label: item.symbol + " | " + item.name, value: item.id }))}
            style={{ width: 260 }}
          />
          {symbolId && <Button size="small" icon={<ReloadOutlined />} onClick={() => loadDiagnostic(symbolId)}>{t("refresh")}</Button>}
        </Space>

        {!symbolId && <Empty description={t("diagSelectSymbol")} />}

        {symbolId && loading && <Spin style={{ display: "block", textAlign: "center", padding: 40 }} />}

        {data && !loading && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {/* Summary */}
            <Card size="small" title={t("diagSummary")}>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
                <div>
                  <div className="metric-label">{t("diagTotalBars")}</div>
                  <strong>{data.summary.total_bars.toLocaleString()}</strong>
                </div>
                <div>
                  <div className="metric-label">{t("diagEarliestBar")}</div>
                  <strong>{data.summary.earliest_bar || "-"}</strong>
                </div>
                <div>
                  <div className="metric-label">{t("diagLatestBar")}</div>
                  <strong>{data.summary.latest_bar || "-"}</strong>
                  {data.summary.latest_age_days != null && <span className="item-subline"> ({data.summary.latest_age_days} {t("diagAgeDays")})</span>}
                </div>
                <div>
                  <div className="metric-label">{t("diagTotalScores")}</div>
                  <strong>{data.summary.total_scores.toLocaleString()}</strong>
                </div>
                <div>
                  <div className="metric-label">{t("diagLatestScore")}</div>
                  <strong>{data.summary.latest_score || "-"}</strong>
                  {data.summary.score_age_days != null && <span className="item-subline"> ({data.summary.score_age_days} {t("diagAgeDays")})</span>}
                </div>
              </div>
            </Card>

            {/* Coverage by period */}
            <Card size="small" title={t("diagCoverage")}>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 12 }}>
                {(["1m", "1q", "1y", "3y"] as const).map((period) => {
                  const cov = data.coverage[period];
                  const scov = data.score_coverage[period];
                  if (!cov) return null;
                  return (
                    <div key={period} style={{ border: "1px solid #f0f0f0", borderRadius: 8, padding: 12 }}>
                      <div style={{ fontWeight: 600, marginBottom: 8 }}>{t(PERIOD_KEYS[period] || period)}</div>
                      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                        <span className="metric-label">{t("diagBarCount")}</span>
                        <span>{cov.bar_count} / {cov.expected_bars}</span>
                      </div>
                      <Progress percent={cov.coverage_pct} size="small" strokeColor={coverageColor(cov.coverage_pct)} />
                      {scov && (
                        <>
                          <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8, marginBottom: 4 }}>
                            <span className="metric-label">{t("diagScoreCount")}</span>
                            <span>{scov.score_count} / {scov.bar_count}</span>
                          </div>
                          <Progress percent={scov.coverage_pct} size="small" strokeColor={coverageColor(scov.coverage_pct)} />
                        </>
                      )}
                    </div>
                  );
                })}
              </div>
            </Card>

            {/* Issues */}
            <Card size="small" title={t("diagIssues")}>
              {data.issues.length > 0 ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {data.issues.map((issue, idx) => {
                    const meta = ISSUE_TYPE_KEYS[issue.type];
                    const issueLabel = meta ? t(meta.key) : issue.type;
                    const issueColor = meta?.color || "default";
                    return (
                      <Alert
                        key={idx}
                        type={issue.severity === "error" ? "error" : "warning"}
                        showIcon
                        message={<Space><Tag color={issueColor}>{issueLabel}</Tag><span>{issue.message}</span></Space>}
                      />
                    );
                  })}
                </div>
              ) : (
                <Alert type="success" showIcon message={t("diagNoIssues")} />
              )}
            </Card>

            {/* Suggestions & Actions */}
            {data.suggestions.length > 0 && (
              <Card size="small" title={t("diagSuggestions")}>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {data.suggestions.map((s, idx) => <div key={idx}>{s}</div>)}
                  {onOpenHistoryInit && (
                    <Space wrap style={{ marginTop: 8 }}>
                      <Button icon={<ToolOutlined />} onClick={() => handleOpenRepair("bars")}>{t("diagRepairBars")}</Button>
                      <Button icon={<ToolOutlined />} onClick={() => handleOpenRepair("scores")}>{t("diagRepairScores")}</Button>
                      <Button type="primary" icon={<ToolOutlined />} onClick={() => handleOpenRepair("both")}>{t("diagRepairBoth")}</Button>
                    </Space>
                  )}
                </div>
              </Card>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
